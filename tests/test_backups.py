"""Storage validation, repository isolation, recovery and scheduling boundaries."""

import contextlib
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mos_itc import backups, core
from mos_itc.core import Error
from test_mos import Preview


class BackupTests(unittest.TestCase):
    def setUp(self):
        # Tests must never discover the repositories configured on the test host.
        directory = tempfile.TemporaryDirectory(prefix="mos-backup-unit-")
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        for module in (core, backups):
            for attribute, child in (("CONF", "conf"), ("STATE", "state")):
                patcher = patch.object(module, attribute, base / child)
                patcher.start()
                self.addCleanup(patcher.stop)

    def create(self, system, identifier="test-one"):
        return backups.create(
            system,
            identifier,
            "Test",
            "a-long-test-password",
            "s3-compatible",
            {
                "endpoint": "storage.example",
                "bucket": "tests",
                "prefix": identifier,
                "region": "",
                "access_key": "ACCESS",
                "secret_key": "SECRET",
            },
            ["/etc"],
            False,
        )

    def test_storage_validation_and_no_credentials_in_arguments(self):
        system = Preview()
        profile = self.create(system)
        self.assertEqual(profile["repository"], "rclone:storage:tests/test-one")
        self.assertNotIn("SECRET", str(profile))
        config = system.read(backups.folder("test-one") / "rclone.conf")
        self.assertIn("secret_access_key = SECRET", config)
        self.assertNotIn("SECRET", str(system.calls))
        with self.assertRaises(Error):
            self.create(system)
        for value in (
            "http://storage.example",
            "https://user:pass@storage.example",
            "https://storage.example/bucket",
            "https://storage.example?x=y",
        ):
            with self.subTest(value=value), self.assertRaises(Error):
                backups.endpoint(value)
        self.assertEqual(backups.endpoint("storage.example"), "https://storage.example")

    def test_rejects_ini_injection_and_path_traversal(self):
        for value in ("secret\n[evil]", "hello\x00world"):
            with self.assertRaises(Error):
                backups.render_config({"pass": value})
        for path in ("../repo", "bucket/../repo", "repo\nother", "/bucket"):
            with self.assertRaises(Error):
                backups.remote_path(path)
        for path in (
            "/",
            "/proc",
            "/proc/stat",
            "/sys/kernel",
            "/dev/sda",
            "/run/test",
        ):
            with self.assertRaises(Error):
                backups.validate_sources([path])
        with self.assertRaises(Error):
            backups.validate_sources([], False)

    def test_sftp_pins_known_hosts_and_ftps_keeps_tls_verification(self):
        system = Preview()
        settings = {
            "host": "backup.example",
            "user": "backup",
            "password": "secret",
            "path": "backups/mos",
            "known_hosts_file": "/etc/mos-itc/known_hosts",
        }
        config, _, _ = backups.storage(system, "sftp", settings)
        self.assertIn("known_hosts_file = /etc/mos-itc/known_hosts", config)
        for kind, key in (
            ("ftps", "explicit_tls = true"),
            ("ftps-implicit", "tls = true"),
        ):
            config, _, _ = backups.storage(system, kind, settings)
            self.assertIn(key, config)
            self.assertNotIn("no_check_certificate", config)
        self.assertNotIn("secret", str(system.calls))

    def test_multiple_profiles_require_explicit_selection(self):
        system = Preview()
        self.create(system)
        self.create(system, "test-two")
        with self.assertRaises(Error):
            backups.load(system)
        self.assertEqual(backups.load(system, "test-two")["id"], "test-two")

    def test_legacy_profile_preserves_password_and_repository(self):
        system = Preview()
        system.overlay[str(core.CONF / "backup.json")] = json.dumps(
            {
                "repository": "s3:https://example.test/bucket",
                "s3": {"AWS_ACCESS_KEY_ID": "old"},
            }
        )
        profile = backups.load(system)
        self.assertEqual(profile["id"], "legacy")
        env = backups.environment(system, profile)
        self.assertEqual(
            env["RESTIC_PASSWORD_FILE"], str(core.CONF / "restic-password")
        )
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "old")

    def test_schedule_validation(self):
        self.assertEqual(backups.calendar("daily", "02:30"), "*-*-* 02:30:00")
        self.assertEqual(backups.calendar("weekly", "04:10"), "Mon *-*-* 04:10:00")
        self.assertEqual(backups.calendar("hours", "6"), "*-*-* 00/6:00:00")
        for kind, value in (
            ("daily", "25:00"),
            ("daily", "1:00"),
            ("hours", "0"),
            ("hours", "1\nExecStart=bad"),
        ):
            with self.assertRaises(Error):
                backups.calendar(kind, value)
        if shutil.which("systemd-analyze"):
            for kind, value in (
                ("daily", "02:30"),
                ("weekly", "04:10"),
                ("hours", "6"),
            ):
                subprocess.run(
                    ["systemd-analyze", "calendar", backups.calendar(kind, value)],
                    check=True,
                    capture_output=True,
                )

    def test_unlock_never_forces_and_errors_do_not_leak_secrets(self):
        system = Preview()
        profile = self.create(system)
        backups.operate(system, profile["id"], "unlock")
        arguments = " ".join(str(call[0]) for call in system.calls)
        self.assertNotIn("--remove-all", arguments)
        self.assertNotIn("--no-lock", arguments)
        with patch.object(
            system,
            "run",
            side_effect=["--retry-lock", Error("SECRET repository is already locked")],
        ):
            with self.assertRaises(Error) as result:
                backups.run_restic(system, profile, ["check"])
        self.assertIn("locked", str(result.exception))
        self.assertNotIn("SECRET", str(result.exception))

    def test_running_status_detects_process_death(self):
        system = Preview()
        profile = self.create(system)
        location = core.STATE / "backup-status" / ("test-one.json")
        system.overlay[str(location)] = json.dumps(
            {
                "state": "running",
                "pid": 999999999,
                "boot": "missing",
                "process_start": "missing",
            }
        )
        self.assertEqual(backups.status(system, profile)["state"], "interrupted")

    def test_restore_refuses_existing_root_symlinks_and_writable_parent(self):
        for value in ("/", "/etc", "/tmp/new-mos-restore"):
            with self.assertRaises(Error):
                backups.restore_target(value)
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to("/srv")
            with self.assertRaises(Error):
                backups.restore_target(str(link / "new"))

    def test_same_repository_has_same_local_lock(self):
        profile = {
            "destination": "https://example.test/bucket/repo",
            "repository": "rclone:storage:bucket/repo",
        }
        self.assertEqual(
            backups.repository_key(dict(profile, id="a")),
            backups.repository_key(dict(profile, id="b")),
        )

    def test_events_keep_complete_json_records(self):
        system = Preview()
        profile = self.create(system)
        rows = [
            {
                "action": "backup",
                "state": "success",
                "time": "2026-11-08T00:00:00Z",
                "duration": 2,
                "message": "x" * 400,
            }
            for _ in range(120)
        ]
        system.overlay[str(core.STATE / "backup-events" / "test-one.jsonl")] = (
            "\n".join(json.dumps(r) for r in rows)
        )
        actual = [
            json.loads(line) for line in backups.events(system, profile).splitlines()
        ]
        self.assertEqual(len(actual), 100)

    def test_legacy_job_still_selects_legacy_after_adding_a_profile(self):
        system = Preview()
        self.create(system)
        system.overlay[str(core.CONF / "backup.json")] = json.dumps(
            {"repository": "rclone:old:repo"}
        )
        self.assertEqual(backups.load(system)["id"], "legacy")

    def test_old_rclone_is_rejected_for_file_transport(self):
        system = Preview()
        profile = self.create(system)
        system.dry = False
        with patch.object(system, "run", return_value="rclone v1.60.1-DEV\n"):
            self.assertFalse(backups.compatible_rclone(system))
            with self.assertRaises(Error) as error:
                backups.run_restic(system, dict(profile, transport="sftp"), ["check"])
        self.assertEqual(error.exception.args[0], "backup_rclone_upgrade_required")
        with patch.object(system, "run", return_value="rclone v1.76.0\n"):
            self.assertTrue(backups.compatible_rclone(system))

    def test_checksum_failure_never_replaces_existing_runtime(self):
        system = Preview()
        system.dry = False
        checks = "0" * 64 + "  rclone-v1.76.0-linux-amd64.zip\n"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rclone"
            target.write_text("previous binary")
            with (
                patch.object(backups, "RCLONE_PRIVATE", target),
                patch.object(backups.os, "geteuid", return_value=0),
                patch.object(
                    backups.urllib.request,
                    "urlopen",
                    side_effect=[
                        io.BytesIO(checks.encode()),
                        io.BytesIO(b"invalid archive"),
                    ],
                ),
            ):
                with self.assertRaises(Error) as error:
                    backups.install_rclone_runtime(system)
            self.assertEqual(error.exception.args[0], "backup_runtime_checksum")
            self.assertEqual(target.read_text(), "previous binary")

    def test_reconfigure_keeps_one_profile_and_uses_private_ca(self):
        system = Preview()
        profile = self.create(system)
        fields = {
            "endpoint": "storage.example",
            "bucket": "tests",
            "prefix": "test-one",
            "region": "",
            "access_key": "OTHER",
            "secret_key": "NEWSECRET",
        }
        updated = backups.create(
            system,
            profile["id"],
            "Edited",
            "a-long-test-password",
            "s3-compatible",
            fields,
            ["/etc"],
            False,
            {"ca.pem": "test CA"},
            replace=True,
        )
        self.assertEqual(len(backups.profiles(system)), 1)
        self.assertEqual(updated["repository"], profile["repository"])
        self.assertEqual(
            backups.environment(system, updated)["RCLONE_CA_CERT"],
            str(backups.folder(profile["id"]) / "ca.pem"),
        )
        self.assertNotIn("NEWSECRET", str(updated))

    def test_guided_s3_setup_and_edit_never_show_credentials(self):
        from mos_itc.ui import UI

        system = Preview()
        ui = UI(system, "es")
        shown = []
        reviews = []
        values = {
            "backup_identifier": "wizard",
            "backup_label": "Storage",
            "backup_s3_endpoint": "storage.example",
            "backup_bucket": "tests",
            "backup_remote_prefix": "wizard",
            "backup_region": "region",
            "backup_access_key": "ACCESS_PRIVATE",
            "backup_secret_key": "SECRET_PRIVATE",
            "backup_ca_path": "",
            "backup_encryption_password": "LONG_PASSWORD_PRIVATE",
            "repeat_password": "LONG_PASSWORD_PRIVATE",
        }

        def ask(label, default="", secret=False):
            if label in (
                "backup_access_key",
                "backup_secret_key",
                "backup_encryption_password",
            ):
                self.assertTrue(secret)
            return values.get(label, default)

        def confirm(message, *args, **kwargs):
            reviews.append(str(message) + str(kwargs))
            return True

        with (
            patch.object(ui, "backup_dependencies"),
            patch.object(ui, "ask", side_effect=ask),
            patch.object(ui, "menu", side_effect=[1, 0, 2]),
            patch.object(ui, "confirm", side_effect=confirm),
            patch.object(
                ui,
                "show",
                side_effect=lambda value, *args, **kw: shown.append(str(value)),
            ),
        ):
            ui.backup_wizard()
        profile = backups.load(system, "wizard")
        self.assertEqual(profile["repository"], "rclone:storage:tests/wizard")
        for secret in ("ACCESS_PRIVATE", "SECRET_PRIVATE", "LONG_PASSWORD_PRIVATE"):
            self.assertNotIn(secret, " ".join(shown + reviews))
        self.assertIn("https://storage.example/tests/wizard", " ".join(reviews))
        values["backup_encryption_password_keep"] = ""
        with (
            patch.object(ui, "backup_dependencies"),
            patch.object(ui, "ask", side_effect=ask),
            patch.object(ui, "menu", side_effect=[1, 2]),
            patch.object(ui, "confirm", return_value=True),
            patch.object(ui, "show"),
            patch.object(ui, "backup_sources") as sources,
        ):
            ui.backup_wizard(profile)
        sources.assert_not_called()
        self.assertEqual(
            system.read(backups.folder("wizard") / "password").strip(),
            "LONG_PASSWORD_PRIVATE",
        )

    def test_installer_validates_each_debian_cloud_mirror(self):
        source = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
        block = source[
            source.index("official_source() {") : source.index(
                'for uri in "${source_uris[@]}"; do'
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            mirrors = Path(tmp) / "mirrors"
            mirrors.mkdir()
            listing = mirrors / "debian.list"
            block = block.replace("/etc/apt/mirrors/", str(mirrors) + "/")
            command = block + '\napproved_source "$1"'
            uri = "mirror+file://" + str(listing)
            for content, accepted in (
                ("# cloud mirrors\nhttps://cdn-aws.deb.debian.org/debian\n", True),
                ("https://deb.debian.org/debian\nhttps://example.org/debian\n", False),
                ("# no mirrors\n", False),
                ("https://deb.debian.org.evil.example/debian\n", False),
            ):
                listing.write_text(content)
                result = subprocess.run(
                    ["bash", "-c", command, "test", uri], capture_output=True
                )
                self.assertEqual(result.returncode == 0, accepted)
            listing.unlink()
            self.assertNotEqual(
                subprocess.run(
                    ["bash", "-c", command, "test", uri], capture_output=True
                ).returncode,
                0,
            )

    def test_installer_motd_preserves_header_without_essential_rename(self):
        installer = Path(__file__).resolve().parent.parent / "install.sh"
        source = installer.read_text()
        start = source.index("# Brand the login welcome")
        end = source.index("printf '\\nMOS ITC installed", start)
        block = source[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            motd = root / "motd"
            state = root / "state"
            motd.mkdir()
            state.mkdir()
            header = motd / "00-header"
            header.write_text("#!/bin/sh\necho original\n")
            header.chmod(0o755)
            block = block.replace("/etc/update-motd.d", str(motd)).replace(
                "/var/lib/mos-itc", str(state)
            )
            helper = """dpkg-divert() {
                printf '%s\\n' "$*" >> "$MOS_TEST_LOG"
                if [[ "$1" == '--list' ]]; then
                    if [[ -f "$MOS_TEST_DIVERSION" ]]; then echo 'local diversion'; fi
                else
                    touch "$MOS_TEST_DIVERSION"
                fi
            }
"""
            env = dict(
                os.environ,
                MOS_TEST_LOG=str(root / "calls"),
                MOS_TEST_DIVERSION=str(root / "diversion"),
            )
            for _ in range(2):
                subprocess.run(
                    ["bash", "-euc", helper + block],
                    env=env,
                    check=True,
                    capture_output=True,
                )
            self.assertEqual(
                (motd / "00-header.distrib").read_text(), "#!/bin/sh\necho original\n"
            )
            self.assertIn("exit 0", header.read_text())
            self.assertIn("--no-rename", (root / "calls").read_text().split())
            self.assertNotIn("--rename", (root / "calls").read_text().split())

    def test_idrive_keeps_content_checksums_without_treating_etags_as_md5(self):
        system = Preview()
        fields = {
            "endpoint": "s3.us-west-2.idrivee2.com",
            "bucket": "tests",
            "prefix": "sample",
            "region": "us-west-2",
            "access_key": "ACCESS",
            "secret_key": "SECRET",
        }
        config, _, _ = backups.storage(system, "s3-compatible", fields)
        self.assertIn("upload_cutoff = 0", config)
        self.assertIn("use_multipart_etag = false", config)
        self.assertNotIn("disable_checksum", config)
        other, _, _ = backups.storage(
            system, "s3-compatible", dict(fields, endpoint="idrivee2.com.example.org")
        )
        self.assertNotIn("upload_cutoff", other)

    def test_initialization_has_a_short_explicit_deadline(self):
        system = Preview()
        profile = self.create(system)
        with patch.object(backups, "run_restic", return_value="initialized") as run:
            backups.operate(system, profile["id"], "init")
        self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_all_missing_sources_abort_before_remote_calls(self):
        system = core.System()
        profile = self.create(system)
        missing = str(core.CONF.parent / "missing-source")
        backups.save_sources(system, profile, [missing], False)
        with self.assertRaises(Error) as result:
            backups.operate(system, profile["id"], "backup")
        self.assertEqual(result.exception.args[0], "backup_source_missing")
        self.assertEqual(backups.status(system, profile)["state"], "failed")
        self.assertEqual(system.commands, [])

    def test_cancelled_child_process_group_is_terminated(self):
        system = core.System()
        with self.assertRaises(Error):
            system.run(
                [
                    "python3",
                    "-c",
                    'import subprocess,time; subprocess.Popen(["sleep","20"]); time.sleep(20)',
                ],
                timeout=0.1,
                process_group=True,
            )
