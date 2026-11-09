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
    require_account(sys, username)
    validate_password(password)
    sys.run(["chpasswd"], input=username + ":" + password + "\n", mutate=True)
    sys.audit("Administrator password changed: " + username)


def add_public_key(sys, username, key):
    require_account(sys, username)
    if username in sys.data().get("student_accounts", {}):
        keys = public_keys(sys, username)
        key = ops.key_text(key).strip()
        if key not in keys:
            save_public_keys(sys, username, keys + [key])
    else:
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


def inventory(sys):
    sites = {site["user"] for site in sys.data()["sites"].values()}
    admins = {a["username"] for a in administrators(sys)}
    students = sys.data().get("student_accounts", {})
    return [
        dict(
            a,
            kind=(
                "admin"
                if a["username"] in admins
                else (
                    "student"
                    if a["username"] in students
                    else "site" if a["username"] in sites else "user"
                )
            ),
            workspace=students.get(a["username"], {}).get("workspace", ""),
        )
        for a in local_accounts(sys).values()
        if 1000 <= a["uid"] < 65534 or a["username"] in sites
    ]


def require_account(sys, username):
    name(username)
    if username not in {a["username"] for a in inventory(sys)}:
        raise Error("users_select_local_account")


def key_file(sys, username):
    require_account(sys, username)
    if username in sys.data().get("student_accounts", {}):
        return Path("/etc/ssh/mos-itc-keys") / username
    return Path(local_accounts(sys)[username]["home"]) / ".ssh/authorized_keys"


def public_keys(sys, username):
    path = key_file(sys, username)
    if username in sys.data().get("student_accounts", {}):
        content = sys.read(path)
    else:
        content = sys.run(
            [
                "runuser",
                "-u",
                username,
                "--",
                "python3",
                "-c",
                "import os,sys; p=sys.argv[1]; fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); print(os.read(fd,1048576).decode())",
                path,
            ],
            check=False,
        )
    return [
        line
        for line in content.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def save_public_keys(sys, username, keys):
    path = key_file(sys, username)
    for key in keys:
        ops.key_text(key)
    content = "\n".join(keys) + "\n"
    if username in sys.data().get("student_accounts", {}):
        sys.write(path, content, 0o644)
    else:
        script = "import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW); os.write(fd,sys.stdin.read().encode()); os.close(fd)"
        sys.run(
            ["runuser", "-u", username, "--", "python3", "-c", script, path],
            input=content,
            mutate=True,
        )


def create_student(sys, username, password, workspace, key=""):
    from . import workspaces

    username = name(username)
    if username.startswith("mos_") or username in local_accounts(sys):
        raise Error("users_account_exists")
    if password:
        validate_password(password)
    if not password_login_allowed(sys) and not key:
        raise Error("users_key_required")
    if key:
        ops.key_text(key)
    home = Path("/home") / username
    if home.exists() or home.is_symlink():
        raise Error("users_account_exists")
    sys.run(
        [
            "useradd",
            "--create-home",
            "--home-dir",
            home,
            "--shell",
            "/usr/sbin/nologin",
            username,
        ],
        mutate=True,
    )
    try:
        if password:
            sys.run(["chpasswd"], input=username + ":" + password + "\n", mutate=True)
        else:
            import secrets

            # A nonempty random password hash allows key authentication, never a known password.
            sys.run(
                ["chpasswd"],
                input=username + ":" + secrets.token_urlsafe(64) + "\n",
                mutate=True,
            )
        if workspace is None:
            workspace = home
            sys.mkdir(workspace, 0o750)
            sys.run(["chmod", "0750", workspace], mutate=True)
            sys.run(["chown", username + ":" + username, workspace], mutate=True)
        workspaces.assign(sys, username, workspace)
        if key:
            sys.write(key_file(sys, username), key + "\n", 0o644)
    except BaseException:
        sys.run(
            ["usermod", "--lock", "--shell", "/usr/sbin/nologin", username],
            mutate=True,
            check=False,
        )
        raise
    sys.audit("Normal Linux SSH user created: " + username)


def student_enabled(sys, username, enabled):

    require_account(sys, username)
    data = sys.data()
    if username not in data.get("student_accounts", {}):
        raise Error("users_select_local_account")
    sys.run(
        [
            "usermod",
            "--unlock" if enabled else "--lock",
            "--shell",
            "/bin/bash" if enabled else "/usr/sbin/nologin",
            username,
        ],
        mutate=True,
    )
    data["student_accounts"][username]["enabled"] = bool(enabled)
    sys.save(data)
    sys.audit(
        "Student access " + ("enabled: " if enabled else "suspended: ") + username
    )
