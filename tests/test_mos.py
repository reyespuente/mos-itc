"""Offline checks for safety boundaries and generated server configurations."""

import ast
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import string
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mos_itc import core, web, ops
from mos_itc.core import Error, System
from mos_itc.i18n import Translator, LANGUAGES
from mos_itc.ui import UI, speed_card
from mos_itc.terminal import Cancelled


class Preview(System):
    """Dry-run with deterministic inventory, independent of the developer's host."""

    def __init__(self):
        super().__init__(dry=True)
        self.overlay[str(core.STATE / "state.json")] = json.dumps(
            {"sites": {}, "certificates": {}, "language": "en"}
        )
        self.calls = []

    def run(self, args, **kwargs):
        args = [str(x) for x in args]
        self.calls.append((args, kwargs))
        if args[0] == "apt-cache":
            return "  Candidate: 1.0\n"
        if args[0] == "apt-get" and "--simulate" in args:
            return "Inst apache2 (2.4 Debian:stable)\n"
        return ""


class SafetyTests(unittest.TestCase):
    def test_domain_validation(self):
        self.assertEqual(core.domain("DEMO.Example.org."), "demo.example.org")
        self.assertEqual(core.domain("música.example.org"), "xn--msica-7ua.example.org")
        for value in (
            "x;id.example.org",
            "../../etc",
            "-demo.example.org",
            "*.example.org",
            "127.0.0.1",
            "a..example.org",
            "a.example.org\nServerAlias evil.org",
        ):
            with self.subTest(value=value), self.assertRaises(Error):
                core.domain(value)

    def test_paths_and_names_reject_injection(self):
        for value in (
            "/etc/../root",
            "/srv/a b",
            "/srv/a\nfoo",
            "/srv/$(id)",
            "relative",
        ):
            with self.assertRaises(Error):
                core.safe_path(value)
        with self.assertRaises(Error):
            core.name("root;id")

    def test_site_path_cannot_escape_home(self):
        site = fixture_site()
        site["root"] = "/etc"
        with self.assertRaises(Error):
            web.validate_site(site)

    def test_preview_never_executes_mutating_subprocess(self):
        system = System(dry=True)
        with patch(
            "subprocess.run", side_effect=AssertionError("A subprocess was executed")
        ):
            system.run(["apt-get", "install", "apache2"], mutate=True)
            system.write("/etc/mos-preview-test", "nothing")
        self.assertEqual(system.read("/etc/mos-preview-test"), "nothing")
        self.assertFalse(Path("/etc/mos-preview-test").exists())

    def test_transaction_restores_content_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = base / "config"
            p.write_text("before")
            p.chmod(0o640)
            with patch.object(core, "STATE", base / "state"):
                system = System()
                with self.assertRaises(Error):
                    with system.transaction([p]):
                        system.write(p, "after", 0o600)
                        raise Error("invalid config")
                self.assertEqual(p.read_text(), "before")
                self.assertEqual(p.stat().st_mode & 0o777, 0o640)
                self.assertEqual(
                    len(list((base / "state/config-backups").iterdir())), 1
                )

    def test_managed_write_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original"
            original.write_text("safe")
            link = Path(tmp) / "link"
            link.symlink_to(original)
            with self.assertRaises(Error):
                System().write(link, "changed")
            self.assertEqual(original.read_text(), "safe")

    def test_official_repository_hosts(self):
        self.assertTrue(core.official_uri("https://deb.debian.org/debian"))
        self.assertTrue(core.official_uri("http://mx.archive.ubuntu.com/ubuntu"))
        for uri in (
            "https://archive.ubuntu.com.evil.org",
            "file:/tmp/repo",
            "https://evil.org/archive.ubuntu.com",
            "https://ppa.launchpad.net/php",
        ):
            self.assertFalse(core.official_uri(uri))

    def test_external_sources_require_opt_in(self):
        system = Preview()
        with patch.object(
            system, "read", return_value="deb https://evil.example/repo stable main\n"
        ):
            with self.assertRaises(Error):
                core.repository_check(system)
            system.external = True
            core.repository_check(system)

    def test_apt_rejects_implicit_dependency_removal(self):
        system = Preview()

        def run(args, **kwargs):
            if args[0] == "apt-cache":
                return "Candidate: 1\n"
            if args[0] == "apt-get":
                return "Remv apache2 [2.4]\nRemv important-app [1]\n"
            return ""

        system.run = run
        with self.assertRaisesRegex(Error, "dependent packages"):
            core.apt_plan(system, remove=["apache2"])

    def test_php_in_use_cannot_be_removed(self):
        system = Preview()
        data = system.data()
        data["sites"]["demo.example.org"] = fixture_site()
        system.save(data)
        with self.assertRaisesRegex(Error, "required by managed"):
            core.apt_plan(system, remove=["php8.4-fpm"])

    def test_create_preview_and_apache_validation_failure(self):
        system = Preview()
        site = web.create_site(system, "demo.example.org", "8.4", True)
        self.assertEqual(site["aliases"], ["www.demo.example.org"])
        self.assertEqual(system.data()["sites"]["demo.example.org"]["php"], "8.4")
        original = system.read(web.paths(site)[0])
        calls = system.calls

        def run(args, **kwargs):
            args = [str(a) for a in args]
            calls.append((args, kwargs))
            if args == ["apache2ctl", "configtest"]:
                raise Error("Invalid Apache configuration")
            return ""

        system.run = run
        with self.assertRaises(Error):
            web.apply_site(
                system,
                {**site, "root": str(core.SITES / site["domain"] / "other")},
                site,
            )
        self.assertEqual(system.read(web.paths(site)[0]), original)
        self.assertEqual(system.data()["sites"][site["domain"]]["root"], site["root"])

    def test_duplicate_alias_edit_is_rejected(self):
        system = Preview()
        data = system.data()
        data["sites"]["other.example.org"] = {
            **fixture_site(),
            "domain": "other.example.org",
            "aliases": ["www.example.org"],
        }
        system.save(data)
        with self.assertRaisesRegex(Error, "already belongs"):
            web.apply_site(system, {**fixture_site(), "aliases": ["www.example.org"]})

    def test_php_pool_uses_independent_user_and_temporary_paths(self):
        config = web.pool(fixture_site())
        self.assertIn("user = mos_demo", config)
        self.assertIn(
            "session.save_path] = /srv/mos/sites/demo.example.org/sessions", config
        )
        self.assertIn("listen.mode = 0660", config)
        self.assertNotIn("open_basedir] = /tmp", config)

    def test_speedtest_converts_bits_per_second(self):
        system = Preview()
        system.dry = False
        payload = {
            "download": 123_450_000,
            "upload": 44_400_000,
            "ping": 9.876,
            "server": {"name": "Test", "country": "MX"},
            "client": {"isp": "Example"},
        }
        with (
            patch.object(system, "run", return_value=json.dumps(payload)),
            patch.object(system, "write"),
        ):
            result = ops.speedtest(system)
        self.assertEqual(result["download_mbps"], 123.45)
        self.assertEqual(result["upload_mbps"], 44.4)
        self.assertEqual(result["ping_ms"], 9.88)
        with patch.object(
            system, "run", return_value='{"download":NaN,"upload":1,"ping":1}'
        ):
            with self.assertRaises(Error):
                ops.speedtest(system)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            speed_card(result, "es")
        self.assertIn("DESCARGA", out.getvalue())

    def test_restore_refuses_live_root(self):
        system = Preview()
        system.overlay[str(CONF_PATH)] = json.dumps({"repository": "rclone:test:path"})
        with self.assertRaises(Error):
            ops.backup(system, "restore", "/")

    def test_ssh_confirmation_requires_new_connection(self):
        system = Preview()
        pending = {
            "token": "abc",
            "connection": "1.2.3.4 1234 5.6.7.8 22",
            "port": 2222,
            "expires": __import__("time").time() + 180,
        }
        system.write(STATE_PATH, json.dumps(pending))
        with patch.dict(os.environ, {"SSH_CONNECTION": pending["connection"]}):
            with self.assertRaisesRegex(Error, "NEW SSH"):
                ops.confirm_ssh(system, "abc")
        with patch.dict(os.environ, {"SSH_CONNECTION": "1.2.3.4 9876 5.6.7.8 2222"}):
            ops.confirm_ssh(system, "abc")
        self.assertFalse(system.exists(STATE_PATH))

    def test_ssh_expiration_restores_previous_config(self):
        system = Preview()
        pending = {
            "token": "abc",
            "previous": "Port 22\n",
            "connection": "",
            "port": 2222,
            "expires": 0,
        }
        system.write(STATE_PATH, json.dumps(pending))
        ops.recover_ssh(system)
        self.assertEqual(system.read(ops.SSH_FILE), "Port 22\n")
        self.assertFalse(system.exists(STATE_PATH))

    def test_metrics_keep_seven_days_and_deduplicate_hour(self):
        system = Preview()
        now = dt.datetime.now(dt.timezone.utc)
        stale = {"time": (now - dt.timedelta(days=8)).isoformat()}
        current = {**ops.metrics(), "time": now.isoformat()}
        system.write(core.STATE / "metrics.json", json.dumps([stale, current]))
        with patch.object(ops, "metrics", return_value=current):
            ops.record_metrics(system)
        self.assertEqual(len(json.loads(system.read(core.STATE / "metrics.json"))), 1)


CONF_PATH = core.CONF / "backup.json"
STATE_PATH = core.STATE / "ssh-pending.json"


def fixture_site():
    return {
        "domain": "demo.example.org",
        "user": "mos_demo",
        "root": "/srv/mos/sites/demo.example.org/www",
        "php": "8.4",
        "aliases": [],
        "ssl": "none",
        "timezone": "America/Mexico_City",
    }


class LanguageTests(unittest.TestCase):
    def test_catalogs_have_same_keys_and_named_parameters(self):
        catalogs = {
            language: json.loads((LANGUAGES / f"{language}.json").read_text())
            for language in ("en", "es")
        }
        self.assertEqual(catalogs["en"].keys(), catalogs["es"].keys())
        formatter = string.Formatter()
        for key in catalogs["en"]:
            fields = lambda text: {
                n for _, n, _, _ in formatter.parse(text) if n is not None
            }
            self.assertEqual(
                fields(catalogs["en"][key]), fields(catalogs["es"][key]), key
            )
        self.assertEqual(Translator("es")("main_menu"), "Menú principal")
        self.assertEqual(Translator("invalid")("main_menu"), "Main menu")

    def test_ui_translation_references_exist(self):
        catalog = Translator().english
        tree = ast.parse(Path("mos_itc/ui.py").read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "t"
                and node.args
            ):
                if isinstance(node.args[0], ast.Constant):
                    self.assertIn(node.args[0].value, catalog)

    def test_php_picker_escape_cancels_instead_of_switching_to_static(self):
        ui = UI(Preview())
        with (
            patch.object(core, "php_versions", return_value=["8.4"]),
            patch.object(ui, "menu", return_value=None),
        ):
            with self.assertRaises(Cancelled):
                ui.select_php()

    def test_source_remains_compatible_with_python_39(self):
        for path in Path("mos_itc").glob("*.py"):
            ast.parse(path.read_text(), feature_version=(3, 9))


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL not installed")
class OpenSSLTests(unittest.TestCase):
    def test_certificate_hostname_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "ec",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=demo.example.org",
                    "-addext",
                    "subjectAltName=DNS:demo.example.org",
                    "-keyout",
                    str(p / "key"),
                    "-out",
                    str(p / "cert"),
                ],
                check=True,
                capture_output=True,
            )
            system = System()
            run = system.run

            def trusted_run(args, **kwargs):
                return run(
                    args,
                    env={**core.SAFE_ENV, "SSL_CERT_FILE": str(p / "cert")},
                    **kwargs,
                )

            system.run = trusted_run
            web.verify_certificate(system, p / "cert", "demo.example.org")
            with self.assertRaises(Error):
                web.verify_certificate(system, p / "cert", "wrong.example.org")

    def test_reject_mismatched_real_rsa_certificate_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=demo.example.org",
                    "-keyout",
                    str(p / "key"),
                    "-out",
                    str(p / "cert"),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "genpkey",
                    "-algorithm",
                    "EC",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-out",
                    str(p / "other"),
                ],
                check=True,
                capture_output=True,
            )
            with self.assertRaisesRegex(Error, "do not match"):
                web.import_certificate(
                    System(),
                    "test",
                    (p / "cert").read_text(),
                    (p / "other").read_text(),
                )


@unittest.skipUnless(
    Path("/usr/lib/apache2/modules/mod_mpm_event.so").exists()
    and shutil.which("apache2"),
    "Apache not installed",
)
class ApacheTests(unittest.TestCase):
    def test_generated_http_vhost_passes_real_apache_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            modules = [
                "mpm_event",
                "authz_core",
                "authz_host",
                "proxy",
                "proxy_fcgi",
                "alias",
                "rewrite",
                "dir",
            ]
            config = f'ServerRoot "{p}"\nDefaultRuntimeDir "{p}"\nPidFile "{p}/apache.pid"\nErrorLog "{p}/main-error.log"\nDefine APACHE_LOG_DIR "{p}"\nServerName localhost\n'
            for module in modules:
                config += f"LoadModule {module}_module /usr/lib/apache2/modules/mod_{module}.so\n"
            config += web.vhost(Preview(), fixture_site())
            (p / "httpd.conf").write_text(config)
            result = subprocess.run(
                ["apache2", "-t", "-f", str(p / "httpd.conf")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(
        shutil.which("openssl")
        and Path("/usr/lib/apache2/modules/mod_ssl.so").exists(),
        "OpenSSL/Apache TLS module missing",
    )
    def test_generated_https_vhost_passes_real_apache_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            certs = p / "certificates/test"
            certs.mkdir(parents=True)
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "ec",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=demo.example.org",
                    "-keyout",
                    str(certs / "key.pem"),
                    "-out",
                    str(certs / "fullchain.pem"),
                ],
                check=True,
                capture_output=True,
            )
            system = Preview()
            data = system.data()
            data["certificates"]["test"] = {}
            system.save(data)
            config = f'ServerRoot "{p}"\nDefaultRuntimeDir "{p}"\nErrorLog "{p}/main.log"\nDefine APACHE_LOG_DIR "{p}"\nServerName localhost\n'
            for module in [
                "mpm_event",
                "authz_core",
                "authz_host",
                "proxy",
                "proxy_fcgi",
                "alias",
                "rewrite",
                "dir",
                "ssl",
            ]:
                config += f"LoadModule {module}_module /usr/lib/apache2/modules/mod_{module}.so\n"
            with patch.object(web, "CONF", p):
                config += web.vhost(
                    system, {**fixture_site(), "ssl": "custom", "certificate": "test"}
                )
            (p / "httpd.conf").write_text(config)
            result = subprocess.run(
                ["apache2", "-t", "-f", str(p / "httpd.conf")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
