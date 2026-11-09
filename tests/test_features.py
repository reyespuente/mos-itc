"""Regressions for the 0.4 upgrade boundary and new administrative controls."""

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mos_itc import (
    core,
    ops,
    settings,
    transfers,
    updates,
    network,
    users,
    workspaces,
    backups,
)
from mos_itc.presentation import size, table, rows
from test_mos import Preview


class Features(unittest.TestCase):
    def test_runtime_and_package_version_are_the_same(self):
        from mos_itc import __version__

        self.assertEqual(core.VERSION, __version__)

    def test_table_alignment_sizes_and_narrow_layout(self):
        self.assertEqual(size(1048576), "1.0 MiB")
        output = table(
            ["Name", "Size"],
            [["one", "1.0 MiB"], ["a very long table name " * 4, "2.0 GiB"]],
            width=45,
        )
        self.assertTrue(all(len(line) <= 45 for line in output.splitlines()))
        positions = {line.index("│") for line in output.splitlines() if "│" in line}
        self.assertEqual(len(positions), 1)
        self.assertTrue(all(len(line) <= 31 for line in output.fit(31).splitlines()))
        self.assertEqual(rows([["a", "b"], ["long", "c"]])[0].index("b"), 6)

    def test_pre_release_comparison_and_invalid_tags(self):
        self.assertGreater(updates.version("v0.5.0"), updates.version("v0.5.0-beta.9"))
        self.assertGreater(updates.version("v0.5.1-beta.1"), updates.version("v0.5.0"))
        self.assertIsNone(updates.version("main; touch /tmp/bad"))
        self.assertEqual(network.public("10.0.0.1"), "")
        self.assertEqual(network.public("8.8.8.8"), "8.8.8.8")

    def test_previous_auto_configuration_is_not_reapplied(self):
        system = Preview()
        system.save(dict(system.data(), hostname="legacy", timezone="UTC"))
        with self.assertRaises(core.Error):
            ops.auto_configure(system, "UTC", "new")
        self.assertFalse(system.commands)

    def test_firewall_ssh_rule_is_protected(self):
        system = core.System(dry=True)
        with patch.object(ops, "ssh_ports", return_value=["2222"]):
            with self.assertRaises(core.Error):
                settings.firewall(system, "deny", "2222", "tcp", "any")
            settings.firewall(system, "allow", "443", "tcp", "192.0.2.0/24")
        self.assertEqual(
            system.commands[-1],
            [
                "ufw",
                "allow",
                "from",
                "192.0.2.0/24",
                "to",
                "any",
                "port",
                "443",
                "proto",
                "tcp",
            ],
        )

    def test_idrive_legacy_profile_uses_runtime_compatibility_without_editing(self):
        system = Preview()
        profile = {
            "id": "old",
            "transport": "s3-compatible",
            "repository": "rclone:storage:bucket/repo",
        }
        path = core.CONF / "backups/old/rclone.conf"
        original = "[storage]\ntype=s3\nendpoint=https://s3.us-west-2.idrivee2.com\n"
        system.write(path, original)
        env = backups.environment(system, profile)
        self.assertEqual(env["RCLONE_S3_UPLOAD_CUTOFF"], "0")
        self.assertEqual(env["RCLONE_S3_USE_MULTIPART_ETAG"], "false")
        self.assertEqual(system.read(path), original)

    def test_download_signatures_bind_id_and_expiry(self):
        key = b"private"
        identifier = "a" * 32
        token = transfers.signature(key, identifier, 123)
        self.assertEqual(
            token,
            hmac.new(key, (identifier + "/123").encode(), hashlib.sha256).hexdigest(),
        )
        self.assertNotEqual(token, transfers.signature(key, identifier, 124))
        self.assertNotEqual(token, transfers.signature(key, "b" * 32, 123))

    def test_cleanup_only_removes_expired_owned_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            exports.mkdir()
            old = "a" * 32
            active = "b" * 32
            (exports / (old + ".bin")).write_text("old")
            (exports / (active + ".bin")).write_text("active")
            original = root / "original.sql"
            original.write_text("retain")
            registry = root / "downloads.json"
            registry.write_text(
                json.dumps(
                    {old: {"expires": 0}, active: {"expires": time.time() + 3600}}
                )
            )
            with patch.object(transfers, "STATE", root), patch.object(
                core, "STATE", root
            ), patch.object(transfers, "REGISTRY", registry), patch.object(
                transfers, "EXPORTS", exports
            ):
                transfers.cleanup(core.System())
            self.assertFalse((exports / (old + ".bin")).exists())
            self.assertTrue((exports / (active + ".bin")).exists())
            self.assertTrue(original.exists())

    def test_normal_user_gets_a_home_and_no_privileged_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = Preview()
            calls = []

            def run(args, **kwargs):
                calls.append([str(x) for x in args])
                if args[0] == "getent":
                    return "student:x:1005:1005::/home/student:/bin/bash"
                return ""

            system.run = run
            workspaces.assign(system, "student", tmp)
            config = system.read("/etc/ssh/sshd_config.d/20-mos-student.conf")
            self.assertNotIn("ForceCommand", config)
            self.assertNotIn("ChrootDirectory", config)
            self.assertIn(
                ["usermod", "--home", tmp, "--shell", "/bin/bash", "student"], calls
            )
            self.assertFalse(system.read("/etc/sudoers.d/91-mos-student-student"))
            self.assertFalse(any(command[0] == "docker" for command in calls))
            self.assertTrue(
                any(command[:3] == ["setfacl", "-R", "-P"] for command in calls)
            )

    def test_php_invalid_value_cannot_inject_another_directive(self):
        system = Preview()
        values = dict(settings.PHP_FIELDS, memory_limit="128M\ndisplay_errors=On")
        with self.assertRaises(core.Error):
            settings.php_save(system, "8.4", "fpm", values)

    def test_prior_system_only_settings_allow_the_first_full_profile(self):
        system = Preview()
        data = system.data()
        data.update(
            hostname="base-only", timezone="UTC", automatic_configuration_applied=False
        )
        system.save(data)
        self.assertFalse(ops.profile_applied(system))
        data["automatic_configuration_applied"] = True
        system.save(data)
        self.assertTrue(ops.profile_applied(system))

    def test_download_port_reused_and_published_transport_not_changed(self):
        system = Preview()
        system.write("/etc/apache2/apache2.conf", "# test")
        old = {
            "host": "8.8.8.8",
            "tls": False,
            "bind": "127.0.0.1",
            "port": 25123,
            "proxy": True,
        }
        system.write(transfers.STATE / "download-server.json", json.dumps(old))
        system.write(
            transfers.REGISTRY,
            json.dumps({"a" * 32: {"expires": time.time() + 60, "published": True}}),
        )
        with patch.object(
            network, "inventory", return_value={"public": "8.8.8.8"}
        ), patch.object(system, "run", return_value="active\n"):
            self.assertEqual(transfers.start(system)["port"], 25123)
        with patch.object(
            network, "inventory", return_value={"public": "9.9.9.9"}
        ), patch.object(system, "run", return_value="active\n"):
            with self.assertRaisesRegex(core.Error, "another address"):
                transfers.start(system)
        self.assertEqual(
            json.loads(system.read(transfers.STATE / "download-server.json")), old
        )

    def test_nested_lock_keeps_other_processes_out_and_releases_after_error(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "operation.lock"
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; from mos_itc import core; "
                f"core.STATE=Path({temporary!r}); "
                f"context=core.lock(path=Path({str(path)!r})); context.__enter__()",
            ]
            with patch.object(core, "STATE", root):
                with self.assertRaisesRegex(ValueError, "test"):
                    with core.lock():
                        with core.lock():
                            self.assertNotEqual(
                                subprocess.run(command, capture_output=True).returncode,
                                0,
                            )
                        self.assertNotEqual(
                            subprocess.run(command, capture_output=True).returncode, 0
                        )
                        raise ValueError("test")
                self.assertEqual(
                    subprocess.run(command, capture_output=True).returncode, 0
                )

    def test_proxy_link_uses_standard_http_port_and_reserved_path(self):
        system = Preview()
        system.write("/etc/apache2/apache2.conf", "# test")
        identifier = "a" * 32
        system.write(
            transfers.REGISTRY,
            json.dumps(
                {identifier: {"expires": int(time.time()) + 60, "published": False}}
            ),
        )
        with patch.object(transfers, "secret", return_value=b"test"), patch.object(
            core, "lock", return_value=__import__("contextlib").nullcontext()
        ):
            url = transfers.link(
                system,
                identifier,
                {"host": "8.8.8.8", "port": 25123, "tls": False, "proxy": True},
            )
        self.assertTrue(url.startswith("http://8.8.8.8/mos-downloads/"))
        self.assertNotIn(":25123", url)
        transfers.apache_route(system, {"port": 25123})
        route = system.read(transfers.APACHE_ROUTE)
        self.assertIn('ProxyPass "/mos-downloads/" "http://127.0.0.1:25123/"', route)
        self.assertNotIn('ProxyPass "/"', route)
        count = len(system.calls)
        transfers.apache_route(system, {"port": 25123})
        self.assertEqual(len(system.calls), count)

    def test_legacy_published_links_are_not_silently_migrated(self):
        system = Preview()
        system.write("/etc/apache2/apache2.conf", "# test")
        system.write(
            transfers.STATE / "download-server.json",
            json.dumps(
                {"host": "8.8.8.8", "tls": False, "bind": "0.0.0.0", "port": 25123}
            ),
        )
        system.write(
            transfers.REGISTRY,
            json.dumps({"a" * 32: {"expires": time.time() + 60, "published": True}}),
        )
        with patch.object(network, "inventory", return_value={"public": "8.8.8.8"}):
            with self.assertRaises(core.Error):
                transfers.start(system)
        self.assertNotIn(str(transfers.APACHE_ROUTE), system.overlay)

    def test_download_route_exempts_managed_https_redirects(self):
        system = Preview()
        system.write("/etc/apache2/apache2.conf", "# test")
        data = system.data()
        data["sites"]["demo.example.org"] = {"domain": "demo.example.org"}
        system.save(data)
        path = Path("/etc/apache2/sites-available/mos-demo.example.org.conf")
        original = "# Managed by MOS ITC\n  RewriteRule ^ https://demo.example.org%{REQUEST_URI} [R=301,L,NE]\n"
        system.write(path, original)
        transfers.apache_route(system, {"port": 25123})
        self.assertIn("RewriteCond %{REQUEST_URI} !^/mos-downloads/", system.read(path))
        self.assertIn("RewriteRule ^ https://demo.example.org", system.read(path))

    def test_download_route_is_rolled_back_if_apache_validation_fails(self):
        system = Preview()
        system.write("/etc/apache2/apache2.conf", "# test")
        system.write(transfers.APACHE_ROUTE, "# previous valid route")

        def run(command, **kwargs):
            if command == ["apache2ctl", "configtest"]:
                raise core.Error("invalid test config")
            return ""

        with patch.object(system, "run", side_effect=run):
            with self.assertRaises(core.Error):
                transfers.apache_route(system, {"port": 25123})
        self.assertEqual(system.read(transfers.APACHE_ROUTE), "# previous valid route")

    def test_uninstall_stops_download_daemon_and_removes_its_route(self):
        from mos_itc.cli import uninstall

        system = Preview()
        system.write(transfers.APACHE_ROUTE, "# owned test route")
        uninstall(system)
        self.assertFalse(system.exists(transfers.APACHE_ROUTE))
        self.assertTrue(
            any(
                command == ["systemctl", "disable", "--now", "mos-downloads.service"]
                for command, _ in system.calls
            )
        )

    def test_sql_output_is_bounded_without_retaining_the_temporary_file(self):
        import sys
        from mos_itc.databases import query_result

        result = query_result(
            core.System(), [sys.executable, "-c", 'print("a"*300000)'], ""
        )
        self.assertLess(len(result), 263000)
        self.assertIn("truncated", result)

    def test_no_package_updates_produces_a_visible_report(self):
        from mos_itc.ui import UI

        ui = UI(Preview(), "es")
        with patch.object(ui, "confirm", return_value=True), patch.object(
            core, "refresh"
        ), patch.object(core, "upgrade_plan", return_value=([], "")), patch.object(
            ui, "show"
        ) as show:
            ui.package_updates()
        show.assert_called_once_with(ui.t("ui_no_updates"), "ui_package_updates")

    def test_php_pool_uses_saved_limits_and_retains_site_isolation(self):
        from test_mos import fixture_site
        from mos_itc import web

        site = fixture_site()
        text = web.pool(
            site,
            {"memory_limit": "256M", "post_max_size": "64M", "date.timezone": "UTC"},
        )
        self.assertIn("php_admin_value[memory_limit] = 256M", text)
        self.assertIn("php_admin_value[post_max_size] = 64M", text)
        self.assertIn("php_admin_value[open_basedir]", text)
        self.assertIn(
            "php_admin_value[date.timezone] = " + site.get("timezone", "UTC"), text
        )

    def test_app_update_pins_the_commit_and_requests_a_clean_restart(self):
        from mos_itc.ui import UI

        system = Preview()
        ui = UI(system, "es")
        target = {"tag": "v0.5.0", "sha": "a" * 40}

        def run(args, **kwargs):
            return "0.5.0\n" if args[-1] == "--version" else ""

        with patch.object(updates, "latest", return_value=target), patch.object(
            ui, "confirm", return_value=True
        ), patch.object(system, "run", side_effect=run), patch.object(
            ui, "outside", return_value=0
        ) as outside:
            self.assertTrue(ui.app_update())
        arguments = outside.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--ref") + 1], target["sha"])
        self.assertIn("--app-update", arguments)
        self.assertTrue(ui.restart)

    def test_pending_ssh_change_blocks_the_app_updater(self):
        from mos_itc.ui import UI

        system = Preview()
        system.write(core.STATE / "ssh-pending.json", "{}")
        with patch.object(updates, "latest") as latest:
            with self.assertRaises(core.Error):
                UI(system).app_update()
        latest.assert_not_called()
