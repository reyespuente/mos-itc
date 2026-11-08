"""Navigation and compatibility regressions; no root or network required."""

import curses
import json
from pathlib import Path
import subprocess
import sys
import unittest
import contextlib
import io
import tempfile
import tarfile
from unittest.mock import Mock, patch

from mos_itc import core
from mos_itc.core import Error, System
from mos_itc.i18n import Translator
from mos_itc.terminal import Terminal, ASSETS
from mos_itc.ui import UI


class Window:
    def __init__(self, keys=(), height=32, width=110):
        self.keys = iter(keys)
        self.height, self.width = height, width
        self.cells = {}

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.cells = {}

    def addnstr(self, y, x, text, width, style=0):
        for i, char in enumerate(text[:width]):
            self.cells[y, x + i] = (char, style)

    def refresh(self):
        pass

    def getch(self):
        return next(self.keys)


def terminal(keys=(), height=32, width=110):
    term = object.__new__(Terminal)
    term.win = Window(keys, height, width)
    term.t = Translator("es")
    term.preview = True
    term.status = "Listo"
    term.status_error = False
    term.positions = {}
    term.logo = json.loads((ASSETS / "itc-logo.json").read_text())
    term.ink = {key: index for index, key in enumerate("WGRYH", 7)}
    term.style = lambda pair=1, bold=False: pair
    return term


class NavigationTests(unittest.TestCase):
    def test_login_banner_contains_only_wordmark_and_control_panel(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            UI(System(dry=True)).banner()
        text = out.getvalue()
        self.assertIn("Control panel: sudo mos-itc", text)
        self.assertNotIn("Reyes", text)
        self.assertNotIn("Instituto", text)

    def test_menu_keeps_selection_after_return(self):
        term = terminal([curses.KEY_DOWN, 10, 10])
        options = ["websites", "about", "exit"]
        self.assertEqual(term.menu("main_menu", options), 1)
        self.assertEqual(term.menu("main_menu", options), 1)

    def test_review_defaults_to_cancel_and_uses_action_buttons(self):
        term = terminal([10])
        self.assertEqual(
            term.document("ui_review", "Test", ("ui_cancel", "ui_apply")), 0
        )
        term = terminal([curses.KEY_RIGHT, 10])
        self.assertEqual(
            term.document("ui_review", "Test", ("ui_cancel", "ui_apply")), 1
        )

    def test_checklist_has_pinned_apply_button_and_tab_navigation(self):
        term = terminal([curses.KEY_DOWN, 32, 9, 10], height=24, width=80)
        options = ["Apache", "PHP", "MariaDB"]
        self.assertEqual(term.menu("ui_software", options, selected=set()), {1})
        row = "".join(term.win.cells.get((20, x), (" ", 0))[0] for x in range(80))
        self.assertIn("Aplicar cambios", row)

    def test_too_small_review_never_applies_hidden_action(self):
        term = terminal([curses.KEY_RIGHT, 10, 27], height=10, width=30)
        self.assertIsNone(term.document("ui_review", "Test", ("ui_cancel", "ui_apply")))

    def test_normal_confirmation_needs_no_typed_yes(self):
        ui = UI(System(dry=True), "es")
        ui.terminal = Mock()
        ui.terminal.document.return_value = 1
        with patch.object(
            ui, "ask", side_effect=AssertionError("Unexpected extra input")
        ):
            self.assertTrue(ui.confirm("ui_confirm_site_changes"))

    def test_destructive_confirmation_keeps_explicit_name_check(self):
        ui = UI(System(dry=True), "es")
        ui.terminal = Mock()
        ui.terminal.document.return_value = 1
        with patch.object(ui, "ask", return_value="wrong"):
            self.assertFalse(ui.confirm("delete_website", word="example.org"))

    def test_submenu_stays_open_after_action(self):
        ui = UI(System(dry=True), "es")
        action = Mock()
        with patch.object(ui, "menu", side_effect=[0, 1]) as menu:
            ui.section("test", [("about", action)])
        action.assert_called_once()
        self.assertEqual(menu.call_count, 2)

    def test_home_branding_order_and_logo_on_80_by_24(self):
        term = terminal(height=24, width=80)
        term.draw_menu("main_menu", ["websites", "about", "exit"], 0)
        lines = [
            "".join(term.win.cells.get((y, x), (" ", 0))[0] for x in range(80))
            for y in range(24)
        ]
        self.assertIn("Instituto Tecnológico de Celaya", lines[1])
        self.assertIn("Reyes Puente", lines[3])
        self.assertNotIn("Guadalupe", "\n".join(lines))
        self.assertTrue(
            any(
                style in range(7, 12)
                for char, style in term.win.cells.values()
                if char != " "
            )
        )

    def test_no_pause_api_or_press_enter_to_continue_catalog(self):
        self.assertFalse(hasattr(UI, "pause"))
        self.assertNotIn("press_enter_to_continue", Translator("en").english)


class UpgradeTests(unittest.TestCase):
    def test_restore_point_extracts_without_following_links_or_overwriting(self):
        from mos_itc import recovery

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            point = root / "points/20261108T000000Z"
            point.mkdir(parents=True)
            with tarfile.open(point / "etc.tar.gz", "w:gz") as archive:
                content = b"original configuration"
                entry = tarfile.TarInfo("etc/example.conf")
                entry.size = len(content)
                entry.mode = 0o600
                archive.addfile(entry, io.BytesIO(content))
                entry = tarfile.TarInfo("etc/link")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/shadow"
                archive.addfile(entry)
            with patch.object(recovery, "POINTS", root / "points"):
                target = recovery.extract_point(point.name, str(root / "recovered"))
                self.assertEqual((target / "etc/example.conf").read_bytes(), content)
                self.assertFalse((target / "etc/link").exists())
                self.assertTrue(json.loads((target / "LINKS.json").read_text()))
                with self.assertRaises(Error):
                    recovery.extract_point(point.name, str(target))

    def test_socket_activation_change_and_rollback_restore_both_files(self):
        from mos_itc import ops

        system = System(dry=True)
        calls = []

        def run(args, **kwargs):
            args = [str(x) for x in args]
            calls.append(args)
            if args[:3] == ["systemctl", "is-active", "ssh.socket"]:
                return "active\n"
            if "--property=KillMode" in args:
                return "process\n"
            if "--property=Listen" in args:
                return "0.0.0.0:22 (Stream)\n[::]:22 (Stream)\n"
            return ""

        system.run = run
        old = "[Socket]\nListenStream=22\n"
        system.write(ops.SSH_SOCKET_FILE, old)
        ops.change_ssh(system, 2222, password=True)
        self.assertIn("ListenStream=[::]:2222", system.read(ops.SSH_SOCKET_FILE))
        self.assertIn(["systemctl", "restart", "ssh.socket", "ssh.service"], calls)
        ops.recover_ssh(system, force=True)
        self.assertEqual(system.read(ops.SSH_SOCKET_FILE), old)

    def test_public_key_writer_adds_real_newlines_without_duplicates(self):
        from mos_itc import ops
        import tempfile

        system = System(dry=True)
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test"
        with tempfile.TemporaryDirectory() as tmp:
            captured = []

            def run(args, **kwargs):
                if args[0] == "getent":
                    return f"test:x:1000:1000::" + tmp + ":/bin/bash\n"
                captured.append(args)
                return ""

            system.run = run
            ops.add_key(system, "test", key)
            script = captured[-1][-2]
            folder = Path(tmp) / ".ssh"
            for item in (key, key, key.replace(" test", " second")):
                subprocess.run(
                    [sys.executable, "-c", script, str(folder)],
                    input=item + "\n",
                    text=True,
                    check=True,
                )
            lines = (folder / "authorized_keys").read_text().splitlines()
            self.assertEqual(lines, [key, key.replace(" test", " second")])

    def test_future_php_branches_are_discovered_and_sorted_numerically(self):
        system = System(dry=True)

        def run(args, **kwargs):
            if args[0] == "dpkg-query":
                return "php8.4-fpm\tii \nphp8.2-fpm\trc \n"
            if args[1] == "pkgnames":
                return "php8.5-fpm\nphp9.0-fpm\nphp8.10-fpm\nphp8.6-fpm\nphpBAD-fpm\n"
            return "Candidate: 1.0\n"

        system.run = run
        self.assertEqual(
            core.php_versions(system), ["8.4", "8.5", "8.6", "8.10", "9.0"]
        )
        self.assertEqual(core.php_versions(system, True), ["8.4"])

    def test_change_between_review_and_apply_is_blocked(self):
        system = System(dry=True)
        args = ["apt-get", "install", "apache2"]
        core.remember_plan(system, args, "Inst apache2 [2.4.1] (2.4.2 Debian:stable)\n")
        with (
            patch.object(core, "repository_check"),
            patch.object(
                system,
                "run",
                return_value="Inst apache2 [2.4.1] (2.4.3 Debian:stable)\n",
            ) as run,
        ):
            with self.assertRaisesRegex(Error, "changed the package plan"):
                core.apply_packages(system, args)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("mutate", run.call_args.kwargs)

    def test_update_refuses_package_removal(self):
        system = System(dry=True)
        with patch.object(system, "run", return_value="Remv important-package [1]\n"):
            with self.assertRaisesRegex(Error, "remove packages"):
                core.upgrade_plan(system)

    def test_progress_keeps_reading_stdout_and_stdin(self):
        system = System()
        ticks = []
        system.progress = lambda message, elapsed: ticks.append(elapsed)
        result = system.run(
            [
                sys.executable,
                "-c",
                "import sys,time; value=sys.stdin.read(); time.sleep(.3); print(value)",
            ],
            input="sample",
            timeout=3,
        )
        self.assertEqual(result.strip(), "sample")
        self.assertGreaterEqual(len(ticks), 2)


if __name__ == "__main__":
    unittest.main()
