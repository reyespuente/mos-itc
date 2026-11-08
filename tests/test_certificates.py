"""Certificate trust, replacement safety and real external-editor regressions."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from mos_itc import core, web
from mos_itc.core import Error, System
from mos_itc.ui import UI


LEAF = "-----BEGIN CERTIFICATE-----\nAQID\n-----END CERTIFICATE-----\n"
CHAIN = "-----BEGIN CERTIFICATE-----\nBAUG\n-----END CERTIFICATE-----\n"


class CertificateFormTests(unittest.TestCase):
    def test_fullchain_is_split_and_duplicate_intermediates_removed(self):
        cert, chain = web.normalize_certificates(
            "\ufeff" + (LEAF + CHAIN).replace("\n", "\r\n"), CHAIN
        )
        self.assertEqual(cert, LEAF)
        self.assertEqual(chain, CHAIN)

    def test_csr_and_private_key_are_not_accepted_as_certificate(self):
        for kind in ("CERTIFICATE REQUEST", "PRIVATE KEY"):
            with self.assertRaises(Error):
                web.normalize_certificates(
                    f"-----BEGIN {kind}-----\nAQID\n-----END {kind}-----"
                )

    def test_regular_file_limit_and_binary_file_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "file.pem"
            p.write_bytes(b"\xff\x00")
            with self.assertRaises(Error):
                web.read_pem_file(str(p))
            p.write_bytes(b"A" * (web.MAX_PEM_BYTES + 1))
            with self.assertRaisesRegex(Error, "1 MiB"):
                web.read_pem_file(str(p))
            with self.assertRaises(Error):
                web.read_pem_file(tmp)

    def test_old_inventory_keeps_intermediates_when_editing(self):
        system = System(dry=True)
        p = web.CONF / "certificates/example"
        system.overlay.update(
            {
                str(p / "cert.pem"): LEAF,
                str(p / "key.pem"): "private fixture",
                str(p / "fullchain.pem"): LEAF + CHAIN,
            }
        )
        self.assertEqual(
            web.certificate_contents(system, "example"),
            (LEAF, "private fixture", CHAIN),
        )

    def test_missing_fields_do_not_launch_validation_or_save(self):
        ui = UI(System(dry=True), "es")
        with (
            patch.object(ui, "menu", side_effect=[3, 4]),
            patch.object(ui, "show") as show,
            patch.object(web, "validate_certificate") as validate,
            patch.object(web, "import_certificate") as save,
        ):
            ui.certificate_import_form("example")
        self.assertTrue(show.called)
        validate.assert_not_called()
        save.assert_not_called()

    def test_failed_validation_keeps_draft_and_private_key_out_of_messages(self):
        ui = UI(System(dry=True), "en")
        values = (LEAF, "PRIVATE TEST FIXTURE", CHAIN)
        with (
            patch.object(web, "certificate_contents", return_value=values),
            patch.object(ui, "menu", side_effect=[3, 3, 4]) as menu,
            patch.object(ui, "show") as show,
            patch.object(
                web,
                "validate_certificate",
                side_effect=Error("cert_expected_private_key"),
            ) as validate,
            patch.object(web, "import_certificate") as save,
        ):
            ui.certificate_import_form("example")
        self.assertEqual(validate.call_count, 2)
        for call in validate.call_args_list:
            self.assertEqual(call.args[1:], values)
        self.assertNotIn(
            "PRIVATE TEST FIXTURE", str(menu.call_args_list) + str(show.call_args_list)
        )
        save.assert_not_called()


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL not installed")
class CertificateCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.TemporaryDirectory(prefix="mos-cert-tests-")
        cls.pki = Path(cls.files.name)

        def run(args):
            subprocess.run(
                ["openssl"] + list(map(str, args)), check=True, capture_output=True
            )

        p = cls.pki
        run(
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=MOS Test CA",
                "-keyout",
                p / "ca.key",
                "-out",
                p / "ca.pem",
            ]
        )
        run(
            [
                "req",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-subj",
                "/CN=MOS Intermediate",
                "-keyout",
                p / "inter.key",
                "-out",
                p / "inter.csr",
            ]
        )
        (p / "inter.ext").write_text(
            "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        )
        run(
            [
                "x509",
                "-req",
                "-in",
                p / "inter.csr",
                "-CA",
                p / "ca.pem",
                "-CAkey",
                p / "ca.key",
                "-CAcreateserial",
                "-days",
                "2",
                "-extfile",
                p / "inter.ext",
                "-out",
                p / "inter.pem",
            ]
        )
        (p / "leaf.ext").write_text(
            "subjectAltName=DNS:demo.example.org,DNS:*.example.org\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
        )
        for label in ("first", "second"):
            run(
                [
                    "req",
                    "-newkey",
                    "ec",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-nodes",
                    "-subj",
                    "/CN=demo.example.org",
                    "-keyout",
                    p / (label + ".key"),
                    "-out",
                    p / (label + ".csr"),
                ]
            )
            run(
                [
                    "x509",
                    "-req",
                    "-in",
                    p / (label + ".csr"),
                    "-CA",
                    p / "inter.pem",
                    "-CAkey",
                    p / "inter.key",
                    "-CAcreateserial",
                    "-days",
                    "1",
                    "-extfile",
                    p / "leaf.ext",
                    "-out",
                    p / (label + ".pem"),
                ]
            )

    @classmethod
    def tearDownClass(cls):
        cls.files.cleanup()

    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        root = Path(self.files.name)
        patches = [
            patch.object(core, "STATE", root / "state"),
            patch.object(web, "STATE", root / "state"),
            patch.object(web, "CONF", root / "conf"),
            patch.object(web, "installed", return_value=False),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.system = System()
        original = self.system.run

        def run(args, **kwargs):
            return original(
                args,
                env={**core.SAFE_ENV, "SSL_CERT_FILE": str(self.pki / "ca.pem")},
                **kwargs,
            )

        self.system.run = run

    def bundle(self, label="first"):
        return (
            (self.pki / (label + ".pem")).read_text(),
            (self.pki / (label + ".key")).read_text(),
            (self.pki / "inter.pem").read_text(),
        )

    def test_fullchain_validation_metadata_and_private_permissions(self):
        cert, key, chain = self.bundle()
        info = web.import_certificate(self.system, "example", cert + chain, key)
        self.assertEqual(info["domains"], ["demo.example.org", "*.example.org"])
        self.assertTrue(info["fingerprint"])
        self.assertNotIn("PRIVATE KEY", json.dumps(info))
        target = web.CONF / "certificates/example"
        self.assertEqual((target / "key.pem").stat().st_mode & 0o777, 0o600)
        self.assertEqual((target / "chain.pem").read_text(), chain)
        self.assertEqual((target / "fullchain.pem").read_text(), cert + chain)

    def test_missing_intermediate_fails_without_changing_inventory(self):
        cert, key, _ = self.bundle()
        with self.assertRaises(Error):
            web.import_certificate(self.system, "example", cert, key)
        self.assertFalse(self.system.data()["certificates"])
        self.assertFalse((web.CONF / "certificates/example").exists())

    def test_replacement_must_cover_every_assigned_domain(self):
        web.import_certificate(self.system, "example", *self.bundle())
        data = self.system.data()
        data["sites"]["wrong.test"] = {
            "domain": "wrong.test",
            "ssl": "custom",
            "certificate": "example",
        }
        self.system.save(data)
        old = (web.CONF / "certificates/example/cert.pem").read_bytes()
        with self.assertRaises(Error):
            web.import_certificate(self.system, "example", *self.bundle("second"))
        self.assertEqual((web.CONF / "certificates/example/cert.pem").read_bytes(), old)

    def test_reload_failure_restores_files_and_inventory(self):
        web.import_certificate(self.system, "example", *self.bundle())
        paths = [
            web.CONF / "certificates/example" / name
            for name in ("cert.pem", "key.pem", "chain.pem", "fullchain.pem")
        ] + [core.STATE / "state.json"]
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        before = {str(path): digest(path) for path in paths}
        run = self.system.run
        reloads = []

        def fail_first_reload(args, **kwargs):
            if args[0] == "apache2ctl":
                return ""
            if list(args) == ["systemctl", "reload", "apache2"]:
                reloads.append(args)
                if len(reloads) == 1:
                    raise Error("reload failed")
                return ""
            return run(args, **kwargs)

        with (
            patch.object(web, "installed", return_value=True),
            patch.object(self.system, "run", side_effect=fail_first_reload),
        ):
            with self.assertRaisesRegex(Error, "reload failed"):
                web.import_certificate(self.system, "example", *self.bundle("second"))
        self.assertEqual(before, {str(path): digest(path) for path in paths})
        self.assertEqual(len(reloads), 2)


@unittest.skipUnless(
    os.name == "posix" and shutil.which("nano"), "Native nano/PTY unavailable"
)
class NativeEditorTests(unittest.TestCase):
    def test_three_consecutive_nano_sessions_resume_curses(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        script = """
import curses
from mos_itc.core import System
from mos_itc.ui import UI
from mos_itc.terminal import Terminal
ui=UI(System())
def run(win):
 ui.terminal=Terminal(win,ui.t,preview=True)
 try:
  win.refresh()
  for i,label in enumerate(('certificate_pem','private_key_pem','ca_bundle_pem_optional')):
   assert ui.editor(label,'public editor fixture '+str(i)+'\\n')=='public editor fixture '+str(i)+'\\n'
   assert not curses.isendwin(), 'curses remained suspended after nano'
 finally:
  ui.terminal.close()
curses.wrapper(run)
print('EDITOR_SEQUENCE_OK',flush=True)
"""
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            os.execve(
                sys.executable,
                [sys.executable, "-c", script],
                {**os.environ, "TERM": "xterm-256color", "NO_COLOR": "1"},
            )
        output = bytearray()
        pending = b""
        editors = 0
        status = None
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    pending += chunk.lower()
                    while b"gnu nano" in pending and editors < 3:
                        pending = pending.split(b"gnu nano", 1)[1]
                        os.write(fd, b"\x18")
                        editors += 1
                ended, code = os.waitpid(pid, os.WNOHANG)
                if ended:
                    status = code
                    break
            if status is None:
                ended, code = os.waitpid(pid, os.WNOHANG)
                if ended:
                    status = code
            self.assertEqual(editors, 3, output.decode(errors="replace")[-2500:])
            self.assertIn(
                b"EDITOR_SEQUENCE_OK", output, output.decode(errors="replace")[-2500:]
            )
            if status is None:
                _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            if status is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGTERM)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
