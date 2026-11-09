"""Normal Linux accounts with a chosen home and shared site permissions."""

from pathlib import Path
from .core import Error, name, safe_path


def assign(system, username, workspace):
    username = name(username)
    workspace = safe_path(workspace)
    if (
        workspace.is_symlink()
        or workspace.resolve() != workspace
        or not workspace.is_dir()
    ):
        raise Error("student_invalid_workspace")
    record = system.run(["getent", "passwd", username]).strip().split(":")
    if len(record) != 7 or int(record[2]) == 0:
        raise Error("student_invalid_workspace")
    config = Path("/etc/ssh/sshd_config.d") / ("20-mos-" + username + ".conf")
    keys = Path("/etc/ssh/mos-itc-keys") / username
    system.mkdir(keys.parent, 0o755)
    if not system.exists(keys):
        system.write(keys, "", 0o644)
    data = system.data()
    previous = data.get("student_accounts", {}).get(username, {})
    enabled = previous.get("enabled", True)
    # Grant access to the selected tree; never change its owner or move its files.
    owner = workspace.stat().st_uid
    acl = f"u:{username}:rwX,d:u:{username}:rwx"
    if owner != int(record[2]):
        acl += f",u:{owner}:rwX,d:u:{owner}:rwx"
    system.run(["setfacl", "-R", "-P", "-m", acl, workspace], mutate=True)
    for parent in workspace.parents:
        if parent != Path("/"):
            system.run(["setfacl", "-m", f"u:{username}:--x", parent], mutate=True)
    with system.transaction([config], [["/usr/sbin/sshd", "-t"]]):
        system.write(
            config,
            f"""Match User {username}
    AuthorizedKeysFile {keys}
    DisableForwarding yes
Match all
""",
            0o644,
        )
    # Remove only the obsolete MOS launcher/policy from the unreleased candidate.
    system.delete("/etc/sudoers.d/91-mos-student-" + username)
    system.delete("/usr/local/libexec/mos-student-" + username)
    system.run(
        [
            "usermod",
            "--home",
            workspace,
            "--shell",
            "/bin/bash" if enabled else "/usr/sbin/nologin",
            username,
        ],
        mutate=True,
    )
    system.run(["systemctl", "reload", "ssh"], mutate=True)
    data.setdefault("student_accounts", {})[username] = {
        "workspace": str(workspace),
        "uid": int(record[2]),
        "enabled": enabled,
    }
    for site in data["sites"].values():
        if site["user"] == username:
            site["access"] = "ssh"
    system.save(data)
    system.audit("Linux home/site access assigned: " + username)
