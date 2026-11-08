"""Administrator creation must grant sudo without leaking credentials or replacing users."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mos_itc import users
from mos_itc.core import Error, System


class Accounts(System):
    def __init__(self):
        super().__init__(dry=True)
        self.calls = []
        self.inputs = []
        self.failure = None
        self.password_auth = True
        self.existing = False

    def run(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append(args)
        if kwargs.get("input") is not None:
            self.inputs.append(kwargs["input"])
        if args[0] == self.failure:
            raise Error("test command failure")
        if args[:3] == ["getent", "passwd", "itcadmin"]:
            return (
                "itcadmin:x:1001:1001::/home/itcadmin:/bin/bash\n"
                if self.existing
                else ""
            )
        if args[:3] == ["getent", "group", "sudo"]:
            return "sudo:x:27:reyes\n"
        if args[:2] == ["/usr/sbin/sshd", "-T"]:
            return (
                "passwordauthentication "
                + ("yes" if self.password_auth else "no")
                + "\n"
            )
        return ""


class AdministratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        item = patch.object(users, "SUDOERS", Path(self.temp.name))
        item.start()
        self.addCleanup(item.stop)
        self.system = Accounts()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_create_has_home_bash_group_and_password_required_sudo(self):
        password = "sample admin password"
        account = users.create_administrator(self.system, "itcadmin", password)
        self.assertIn(
            [
                "useradd",
                "--create-home",
                "--home-dir",
                "/home/itcadmin",
                "--shell",
                "/bin/bash",
                "--groups",
                "sudo",
                "itcadmin",
            ],
            self.system.calls,
        )
        self.assertIn("itcadmin:" + password + "\n", self.system.inputs)
        policy = self.system.read(users.SUDOERS / "90-mos-itc-itcadmin")
        self.assertIn("itcadmin ALL=(ALL:ALL) ALL", policy)
        self.assertNotIn("NOPASSWD", policy)
        self.assertNotIn(password, str(self.system.calls) + json.dumps(account))
        self.assertTrue(account["sudo"])

    def test_existing_account_and_home_are_not_replaced(self):
        self.system.existing = True
        with self.assertRaises(Error):
            users.create_administrator(self.system, "itcadmin", "example password")
        self.assertFalse(any(call[0] == "useradd" for call in self.system.calls))
        self.system.existing = False
        with patch.object(
            self.system, "exists", side_effect=lambda p: str(p) == "/home/itcadmin"
        ):
            with self.assertRaises(Error):
                users.create_administrator(self.system, "itcadmin", "example password")

    def test_password_line_injection_and_reserved_name_are_rejected(self):
        for password in ("short", "longenough\nroot:newpass", "longenough\x00suffix"):
            with self.assertRaises(Error):
                users.validate_password(password)
        with self.assertRaises(Error):
            users.create_administrator(self.system, "mos_site", "example password")
        self.assertFalse(any(call[0] == "useradd" for call in self.system.calls))

    def test_key_only_server_requires_public_key_before_creating_user(self):
        self.system.password_auth = False
        with self.assertRaisesRegex(Error, "public key"):
            users.create_administrator(self.system, "itcadmin", "example password")
        self.assertFalse(any(call[0] == "useradd" for call in self.system.calls))

    def test_key_is_assigned_to_new_account(self):
        self.system.password_auth = False
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA fixture"
        with patch.object(users.ops, "add_key") as add:
            users.create_administrator(self.system, "itcadmin", "example password", key)
        add.assert_called_once_with(self.system, "itcadmin", key + "\n")

    def test_failed_creation_removes_new_sudo_policy_and_account(self):
        self.system.failure = "chpasswd"
        with self.assertRaisesRegex(Error, "test command failure"):
            users.create_administrator(self.system, "itcadmin", "example password")
        self.assertIn(
            ["usermod", "--lock", "--shell", "/usr/sbin/nologin", "itcadmin"],
            self.system.calls,
        )
        self.assertIn(["userdel", "--remove", "itcadmin"], self.system.calls)
        self.assertFalse(self.system.exists(users.SUDOERS / "90-mos-itc-itcadmin"))

    def test_root_is_not_an_editable_administrator(self):
        with patch.object(
            users, "administrators", return_value=[{"username": "itcadmin"}]
        ):
            with self.assertRaises(Error):
                users.change_password(self.system, "root", "example password")
        self.assertFalse(any(call[0] == "chpasswd" for call in self.system.calls))

    def test_administrator_inventory_filters_system_and_website_accounts(self):
        passwd = "root:x:0:0::/root:/bin/bash\nreyes:x:1000:1000::/home/reyes:/bin/bash\nmos_site:x:1001:27::/srv/site:/bin/bash\nprimary:x:1002:27::/home/primary:/bin/bash\n"
        with patch.object(self.system, "read", return_value=passwd):
            self.assertEqual(
                [a["username"] for a in users.administrators(self.system)],
                ["primary", "reyes"],
            )

    @unittest.skipUnless(shutil.which("visudo"), "visudo not installed")
    def test_generated_sudo_policy_passes_real_visudo_parser(self):
        users.create_administrator(self.system, "itcadmin", "example password")
        p = Path(self.temp.name) / "policy"
        p.write_text(self.system.read(users.SUDOERS / "90-mos-itc-itcadmin"))
        p.chmod(0o440)
        result = subprocess.run(
            ["visudo", "-c", "-f", str(p)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
