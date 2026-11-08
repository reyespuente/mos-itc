"""Local SSH administrators: standard accounts and the distribution sudo policy."""

from pathlib import Path

from . import ops
from .core import Error, name, stamp

SUDOERS = Path("/etc/sudoers.d")


def local_accounts(sys):
    accounts = {}
    for line in sys.read("/etc/passwd").splitlines():
        fields = line.split(":")
        if len(fields) == 7 and fields[2].isdigit() and fields[3].isdigit():
            accounts[fields[0]] = {
                "username": fields[0],
                "uid": int(fields[2]),
                "gid": int(fields[3]),
                "home": fields[5],
                "shell": fields[6],
            }
    return accounts


def administrators(sys):
    group = sys.run(["getent", "group", "sudo"], check=False).strip().split(":")
    if len(group) != 4:
        return []
    members = set(filter(None, group[3].split(",")))
    return sorted(
        (
            account
            for account in local_accounts(sys).values()
            if account["uid"] >= 1000
            and account["uid"] < 65534
            and not account["username"].startswith("mos_")
            and (account["username"] in members or account["gid"] == int(group[2]))
        ),
        key=lambda account: account["username"],
    )


def validate_password(value):
    if len(value) < 8 or len(value) > 256 or any(c in value for c in "\r\n\x00"):
        raise Error("users_password_invalid")
    return value


def password_login_allowed(sys):
    config = sys.run(["/usr/sbin/sshd", "-T"])
    return "passwordauthentication yes\n" in config


def require_admin(sys, username):
    name(username)
    if username not in {account["username"] for account in administrators(sys)}:
        raise Error("users_select_local_admin")


def change_password(sys, username, password):
    require_admin(sys, username)
    validate_password(password)
    sys.run(["chpasswd"], input=username + ":" + password + "\n", mutate=True)
    sys.audit("Administrator password changed: " + username)


def add_public_key(sys, username, key):
    require_admin(sys, username)
    ops.add_key(sys, username, key)


def create_administrator(sys, username, password, public_key=""):
    username = name(username)
    validate_password(password)
    if username.startswith("mos_"):
        raise Error("users_reserved_name")
    home = Path("/home") / username
    policy = SUDOERS / ("90-mos-itc-" + username)
    if (
        sys.run(["getent", "passwd", username], check=False).strip()
        or sys.exists(home)
        or home.is_symlink()
        or sys.exists(policy)
        or policy.is_symlink()
    ):
        raise Error("users_account_exists")
    if public_key:
        public_key = ops.key_text(public_key)
    if not password_login_allowed(sys) and not public_key:
        raise Error("users_key_required")
    sys.run(["visudo", "-c"])
    sys.run(["getent", "group", "sudo"])
    sys.run(
        [
            "useradd",
            "--create-home",
            "--home-dir",
            home,
            "--shell",
            "/bin/bash",
            "--groups",
            "sudo",
            username,
        ],
        mutate=True,
    )
    try:
        with sys.transaction([policy], [["visudo", "-c"]]):
            sys.write(
                policy, f"# Managed by MOS ITC\n{username} ALL=(ALL:ALL) ALL\n", 0o440
            )
            sys.run(["chpasswd"], input=username + ":" + password + "\n", mutate=True)
            if public_key:
                ops.add_key(sys, username, public_key)
            sys.run(["sudo", "-l", "-U", username], mutate=True)
    except BaseException:
        # The home did not exist before this operation; never remove an old account.
        sys.run(
            ["usermod", "--lock", "--shell", "/usr/sbin/nologin", username],
            mutate=True,
            check=False,
        )
        sys.run(["gpasswd", "--delete", username, "sudo"], mutate=True, check=False)
        sys.run(["userdel", "--remove", username], mutate=True, check=False)
        sys.audit("Administrator creation failed; cleanup attempted: " + username)
        raise
    sys.audit("Local SSH administrator created: " + username + " at " + stamp())
    return {"username": username, "home": str(home), "shell": "/bin/bash", "sudo": True}
