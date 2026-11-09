"""System profiles, SSH recovery, metrics, speed tests and encrypted backups."""

import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import tempfile
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import (
    CONF,
    STATE,
    SITES,
    Error,
    SAFE_ENV,
    installed,
    name,
    stamp,
    safe_path,
    php_versions,
    lock,
)


def timezone(value):
    if not re.fullmatch(r"[A-Za-z0-9_+/-]+", value):
        raise Error("error_invalid_timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise Error("error_unknown_timezone_example_america_mexico_city") from exc
    return value


def install_timer(sys, label, calendar, command, description):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,50}", label):
        raise Error("error_invalid_timer_name")
    base = Path("/etc/systemd/system") / ("mos-" + label)
    sys.write(
        str(base) + ".service",
        f"""[Unit]
Description=MOS ITC {description}
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
UMask=0077
Nice=10
IOSchedulingClass=idle
ExecStart={command}
""",
        0o644,
    )
    sys.write(
        str(base) + ".timer",
        f"""[Unit]
Description=MOS ITC {description}
[Timer]
OnCalendar={calendar}
Persistent=true
RandomizedDelaySec=60
[Install]
WantedBy=timers.target
""",
        0o644,
    )
    sys.run(["systemctl", "daemon-reload"], mutate=True)
    sys.run(["systemctl", "enable", "--now", f"mos-{label}.timer"], mutate=True)


def auto_configure(sys, zone, hostname, default_php=None):
    timezone(zone)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname):
        raise Error("error_invalid_hostname")
    sys.run(["timedatectl", "set-timezone", zone], mutate=True)
    sys.run(["hostnamectl", "set-hostname", hostname], mutate=True)
    hosts = sys.read("/etc/hosts")
    line = "127.0.1.1\t" + hostname
    hosts = (
        re.sub(r"^127\.0\.1\.1\s+.*$", line, hosts, flags=re.M)
        if re.search(r"^127\.0\.1\.1\s", hosts, re.M)
        else hosts.rstrip() + "\n" + line + "\n"
    )
    with sys.transaction(["/etc/hosts"]):
        sys.write("/etc/hosts", hosts, 0o644)
    for v in php_versions(sys, True):
        for sapi in ("cli", "fpm"):
            ini = Path(f"/etc/php/{v}/{sapi}/conf.d/99-mos-production.ini")
            content = f""" ; Managed by MOS ITC
date.timezone = {zone}
expose_php = Off
display_errors = Off
log_errors = On
allow_url_include = Off
memory_limit = 128M
upload_max_filesize = 32M
post_max_size = 40M
max_execution_time = 60
session.cookie_httponly = 1
session.cookie_samesite = Lax
opcache.enable = 1
opcache.validate_timestamps = 1
"""
            with sys.transaction(
                [ini], [[f"php-fpm{v}", "-t"]] if sapi == "fpm" else []
            ):
                sys.write(ini, content, 0o644)
        sys.run(["systemctl", "reload-or-restart", f"php{v}-fpm"], mutate=True)
    if default_php:
        if default_php not in php_versions(sys, True) and not sys.dry:
            raise Error("error_selected_php_version_is_not_installed")
        sys.run(
            ["update-alternatives", "--set", "php", f"/usr/bin/php{default_php}"],
            mutate=True,
        )
    if installed(sys, "apache2") or sys.dry:
        p = Path("/etc/apache2/conf-available/mos-hardening.conf")
        with sys.transaction([p], [["apache2ctl", "configtest"]]):
            sys.write(
                p,
                """ServerTokens Prod
ServerSignature Off
TraceEnable Off
<IfModule mod_headers.c>
Header always set X-Content-Type-Options "nosniff"
Header always set Referrer-Policy "strict-origin-when-cross-origin"
</IfModule>
""",
                0o644,
            )
        sys.run(["a2enconf", "mos-hardening"], mutate=True)
        sys.run(["apache2ctl", "configtest"], mutate=True)
        sys.run(["systemctl", "reload-or-restart", "apache2"], mutate=True)
    install_timer(
        sys,
        "metrics",
        "hourly",
        "/usr/local/bin/mos-itc internal metrics",
        "hourly metrics",
    )
    if installed(sys, "clamav") or sys.dry:
        install_timer(
            sys,
            "clamav",
            "*-*-* 03:30:00",
            "/usr/local/bin/mos-itc internal scan",
            "scheduled malware scan",
        )
        sys.run(["systemctl", "enable", "--now", "clamav-freshclam"], mutate=True)
    data = sys.data()
    data.update({"timezone": zone, "hostname": hostname})
    sys.save(data)
    sys.audit("Production profile applied")


def ssh_ports(sys):
    output = sys.run(["/usr/sbin/sshd", "-T"], check=False)
    ports = re.findall(r"^port (\d+)$", output, re.M)
    return sorted(set(ports)) or ["22"]


def security(sys, ports=None):
    """Retain remote SSH access before enabling default-deny firewall."""
    ports = list(ports or ssh_ports(sys))
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) == 4 and connection[3].isdigit() and connection[3] not in ports:
        ports.append(connection[3])
    p = Path("/etc/fail2ban/jail.d/mos.local")
    with sys.transaction([p], [["fail2ban-client", "-t"]]):
        sys.write(
            p,
            f"""[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
backend = systemd
[sshd]
enabled = true
port = {','.join(ports)}
""",
            0o644,
        )
    sys.run(["systemctl", "enable", "--now", "fail2ban"], mutate=True)
    sys.run(["systemctl", "restart", "fail2ban"], mutate=True)
    for port in ports:
        sys.run(["ufw", "allow", port + "/tcp"], mutate=True)
    for port in ("80/tcp", "443/tcp"):
        sys.run(["ufw", "allow", port], mutate=True)
    sys.run(["ufw", "default", "deny", "incoming"], mutate=True)
    sys.run(["ufw", "default", "allow", "outgoing"], mutate=True)
    # No reset: retain all pre-existing administrator rules.
    sys.run(["ufw", "--force", "enable"], mutate=True)
    sys.audit("Firewall and Fail2ban profile applied")


def key_text(value):
    value = value.strip()
    if "\n" in value or not re.fullmatch(
        r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: [^\r\n]*)?",
        value,
    ):
        raise Error("error_paste_one_openssh_public_key_without_authorized_keys")
    return value + "\n"


def add_key(sys, user, key):
    name(user)
    key = key_text(key)
    record = sys.run(["getent", "passwd", user]).strip().split(":")
    if len(record) != 7:
        raise Error("error_user_not_found")
    home = safe_path(record[5])
    if not sys.dry:
        with tempfile.TemporaryDirectory(prefix="mos-key-") as tmp:
            p = Path(tmp) / "key.pub"
            sys.write(p, key)
            sys.run(["ssh-keygen", "-lf", p])
    folder = home / ".ssh"
    if folder.is_symlink() or (folder / "authorized_keys").is_symlink():
        raise Error("error_ssh_directory_key_file_cannot_be_a_symbolic")
    # Edit as the account, rather than allowing an account-controlled directory to
    # redirect a root write through a symlink race.
    script = """import os,sys
from pathlib import Path
p=Path(sys.argv[1]); p.mkdir(mode=0o700,exist_ok=True)
if p.is_symlink(): raise SystemExit("symlink refused")
os.chmod(p,0o700)
fd=os.open(p/'authorized_keys',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'r+') as f:
 key=sys.stdin.read(); old=f.read()
 if key.strip() not in old.splitlines(): f.write(('' if not old or old.endswith('\\n') else '\\n')+key)
 os.fchmod(f.fileno(),0o600)
"""
    sys.run(
        ["runuser", "-u", user, "--", "python3", "-c", script, str(folder)],
        input=key,
        mutate=True,
    )
    sys.audit("SSH public key added for " + user)


def site_access(sys, site, mode):
    if mode not in ("disabled", "ssh", "sftp"):
        raise Error("error_invalid_access_mode")
    if "Include /etc/ssh/sshd_config.d/*.conf" not in sys.read("/etc/ssh/sshd_config"):
        raise Error("error_sshd_config_must_include_etc_ssh_sshd_config")
    p = Path("/etc/ssh/sshd_config.d") / ("20-mos-" + site["user"] + ".conf")
    with sys.transaction([p], [["/usr/sbin/sshd", "-t"]]):
        if mode == "sftp":
            sys.write(
                p,
                f'Match User {site["user"]}\n    ForceCommand internal-sftp\n    DisableForwarding yes\nMatch all\n',
                0o644,
            )
        else:
            sys.delete(p)
    sys.run(["systemctl", "reload", "ssh"], mutate=True)
    sys.run(
        [
            "usermod",
            "--shell",
            "/usr/sbin/nologin" if mode == "disabled" else "/bin/bash",
            site["user"],
        ],
        mutate=True,
    )
    data = sys.data()
    data["sites"][site["domain"]]["access"] = mode
    sys.save(data)
    sys.audit("Website access mode " + mode + ": " + site["domain"])


SSH_FILE = Path("/etc/ssh/sshd_config.d/00-mos-global.conf")
SSH_SOCKET_FILE = Path("/etc/systemd/system/ssh.socket.d/99-mos-itc.conf")


def socket_addresses(sys):
    listeners = sys.run(
        ["systemctl", "show", "ssh.socket", "--property=Listen", "--value"]
    )
    addresses = re.findall(r"(\[[0-9a-fA-F:.%]+\]|[0-9.]+):\d+\s+\(Stream\)", listeners)
    if not addresses:
        raise Error("ui_ssh_unknown_socket")
    return sorted(set(addresses))


def activate_ssh(sys, socket_mode):
    if socket_mode:
        sys.run(["systemctl", "daemon-reload"], mutate=True)
        sys.run(["systemctl", "restart", "ssh.socket", "ssh.service"], mutate=True)
    else:
        sys.run(["systemctl", "reload", "ssh"], mutate=True)


def change_ssh(sys, port, password=True):
    if not 1 <= int(port) <= 65535:
        raise Error("error_ssh_port_must_be_1_65535")
    if sys.exists(STATE / "ssh-pending.json"):
        raise Error("error_an_ssh_change_is_pending_confirm_or_roll")
    socket_mode = (
        sys.run(["systemctl", "is-active", "ssh.socket"], check=False).strip()
        == "active"
    )
    addresses = []
    if socket_mode:
        kill_mode = sys.run(
            ["systemctl", "show", "ssh.service", "--property=KillMode", "--value"]
        ).strip()
        if kill_mode != "process":
            raise Error("ui_ssh_unsafe_restart")
        addresses = socket_addresses(sys)
    if not sys.dry:
        if "Include /etc/ssh/sshd_config.d/*.conf" not in sys.read(
            "/etc/ssh/sshd_config"
        ):
            raise Error("error_sshd_config_must_include_its_d_directory_before")
        # Multiplexed/matching overrides must be examined using sshd -T separately.
        if not password:
            keys = list(Path("/root/.ssh").glob("authorized_keys")) + list(
                Path("/home").glob("*/.ssh/authorized_keys")
            )
            if not any(p.stat().st_size for p in keys):
                raise Error("error_install_and_test_an_administrator_public_key_before")
    token = secrets.token_hex(12)
    previous = sys.read(SSH_FILE) if sys.exists(SSH_FILE) else None
    pending = {
        "token": token,
        "previous": previous,
        "connection": os.environ.get("SSH_CONNECTION", ""),
        "expires": time.time() + 180,
        "port": int(port),
        "socket_mode": socket_mode,
        "socket_previous": (
            sys.read(SSH_SOCKET_FILE) if sys.exists(SSH_SOCKET_FILE) else None
        ),
    }
    # Persistent recovery timer survives a reboot, unlike only systemd-run.
    sys.write(STATE / "ssh-pending.json", json.dumps(pending))
    sys.write(
        "/etc/systemd/system/mos-ssh-recovery.service",
        """[Unit]
Description=MOS SSH recovery
[Service]
Type=oneshot
ExecStart=/usr/local/bin/mos-itc internal ssh-recover
Restart=on-failure
RestartSec=5
""",
        0o644,
    )
    date = dt.datetime.fromtimestamp(pending["expires"], dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    sys.write(
        "/etc/systemd/system/mos-ssh-recovery.timer",
        f"""[Unit]
Description=MOS SSH automatic rollback
[Timer]
OnCalendar={date}
Persistent=true
AccuracySec=1s
[Install]
WantedBy=timers.target
""",
        0o644,
    )
    sys.run(["systemctl", "daemon-reload"], mutate=True)
    sys.run(["systemctl", "enable", "--now", "mos-ssh-recovery.timer"], mutate=True)
    try:
        with sys.transaction(
            [SSH_FILE] + ([SSH_SOCKET_FILE] if socket_mode else []),
            [["/usr/sbin/sshd", "-t"]],
        ):
            sys.write(
                SSH_FILE,
                f'Port {int(port)}\nPasswordAuthentication {"yes" if password else "no"}\nKbdInteractiveAuthentication {"yes" if password else "no"}\nPermitRootLogin prohibit-password\nPubkeyAuthentication yes\n',
                0o644,
            )
            if socket_mode:
                socket_config = "[Socket]\nListenStream=\n" + "".join(
                    f"ListenStream={address}:{int(port)}\n" for address in addresses
                )
                sys.write(SSH_SOCKET_FILE, socket_config, 0o644)
            if not sys.dry:
                effective = sys.run(["/usr/sbin/sshd", "-T"])
                effective_ports = set(re.findall(r"^port (\d+)$", effective, re.M))
                effective_password = "yes" if password else "no"
                if (
                    effective_ports != {str(port)}
                    or f"passwordauthentication {effective_password}\n" not in effective
                ):
                    raise Error(
                        "error_another_ssh_setting_overrides_the_port_change_was"
                    )
        if installed(sys, "ufw"):
            sys.run(["ufw", "allow", str(port) + "/tcp"], mutate=True)
        activate_ssh(sys, socket_mode)
    except BaseException:
        recover_ssh(sys, force=True)
        raise
    sys.audit("SSH change pending verification")
    return token


def recover_ssh(sys, force=False):
    with lock(sys.dry, STATE / "ssh-recovery.lock"):
        return _recover_ssh(sys, force)


def _recover_ssh(sys, force=False):
    if not sys.exists(STATE / "ssh-pending.json"):
        return
    pending = json.loads(sys.read(STATE / "ssh-pending.json"))
    if not force and time.time() < pending["expires"]:
        return
    if pending["previous"] is None:
        sys.delete(SSH_FILE)
    else:
        sys.write(SSH_FILE, pending["previous"], 0o644)
    if pending.get("socket_mode"):
        if pending.get("socket_previous") is None:
            sys.delete(SSH_SOCKET_FILE)
        else:
            sys.write(SSH_SOCKET_FILE, pending["socket_previous"], 0o644)
    sys.run(["/usr/sbin/sshd", "-t"], mutate=True)
    activate_ssh(sys, pending.get("socket_mode", False))
    clear_ssh_recovery(sys)
    sys.audit("SSH settings rolled back")


def clear_ssh_recovery(sys):
    sys.run(["systemctl", "disable", "--now", "mos-ssh-recovery.timer"], mutate=True)
    sys.delete(STATE / "ssh-pending.json")


def confirm_ssh(sys, token):
    with lock(sys.dry, STATE / "ssh-recovery.lock"):
        return _confirm_ssh(sys, token)


def _confirm_ssh(sys, token):
    if not sys.exists(STATE / "ssh-pending.json"):
        raise Error("error_no_pending_ssh_change")
    pending = json.loads(sys.read(STATE / "ssh-pending.json"))
    if time.time() >= pending["expires"]:
        _recover_ssh(sys, True)
        raise Error("error_ssh_confirmation_expired_previous_settings_restored")
    if not secrets.compare_digest(token, pending["token"]):
        raise Error("error_wrong_confirmation_token")
    conn = os.environ.get("SSH_CONNECTION", "")
    if pending["connection"]:
        if (
            not conn
            or conn == pending["connection"]
            or conn.split()[-1] != str(pending["port"])
        ):
            raise Error("error_confirm_from_a_new_ssh_connection_to_the")
    clear_ssh_recovery(sys)
    sys.audit("SSH change confirmed")


def cpu_ticks():
    parts = [
        int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
    ]
    return sum(parts), parts[3] + parts[4]


def metrics():
    total1, idle1 = cpu_ticks()
    time.sleep(0.25)
    total2, idle2 = cpu_ticks()
    mem = {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
    }
    disk = shutil.disk_usage("/")
    net = {}
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        iface, values = line.split(":", 1)
        fields = values.split()
        net[iface.strip()] = {"received": int(fields[0]), "sent": int(fields[8])}
    return {
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cpu": round(100 * (1 - (idle2 - idle1) / max(1, total2 - total1)), 1),
        "ram_total": mem["MemTotal"],
        "ram_used": mem["MemTotal"] - mem["MemAvailable"],
        "disk_total": disk.total,
        "disk_used": disk.used,
        "network": net,
        "load": os.getloadavg(),
        "host": socket.gethostname(),
        "cores": os.cpu_count() or 1,
        "uptime": float(Path("/proc/uptime").read_text().split()[0]),
        "swap_total": mem.get("SwapTotal", 0),
        "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
        "ram_available": mem["MemAvailable"],
        "processes": sum(p.name.isdigit() for p in Path("/proc").iterdir()),
        "inodes_total": os.statvfs("/").f_files,
        "inodes_free": os.statvfs("/").f_ffree,
        "monotonic": time.monotonic(),
    }


def dashboard_services(system):
    output = system.run(
        [
            "systemctl",
            "show",
            "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState",
            "apache2.service",
            "nginx.service",
            "mariadb.service",
            "postgresql.service",
            "postgresql@*.service",
            "php*-fpm.service",
            "ssh.service",
            "fail2ban.service",
            "clamav-freshclam.service",
            "mos-metrics.timer",
        ],
        check=False,
    )
    services = []
    for block in output.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("LoadState") == "loaded":
            services.append(
                {
                    "name": fields["Id"],
                    "state": fields.get("ActiveState", "unknown"),
                    "detail": fields.get("SubState", ""),
                }
            )
    return services


def memory_processes(limit=5):
    processes = []
    pagesize = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            rss = int((entry / "statm").read_text().split()[1]) * pagesize
            processes.append(
                {
                    "pid": int(entry.name),
                    "name": (entry / "comm").read_text().strip(),
                    "rss": rss,
                }
            )
        except (OSError, ValueError, IndexError):
            continue
    return sorted(processes, key=lambda p: p["rss"], reverse=True)[:limit]


def record_metrics(sys):
    rows = json.loads(sys.read(STATE / "metrics.json", "[]"))
    sample = metrics()
    # At most one sample per UTC hour; retain seven days even after long outages.
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    rows = [
        r
        for r in rows
        if dt.datetime.fromisoformat(r["time"]) >= cutoff
        and r["time"][:13] != sample["time"][:13]
    ]
    sys.write(STATE / "metrics.json", json.dumps((rows + [sample])[-168:]))


def scan(sys):
    sys.mkdir(STATE / "reports")
    # Exclude virtual filesystems and mounted remote data; report infections, never delete.
    args = [
        "clamscan",
        "--recursive",
        "--infected",
        "--cross-fs=no",
        "--exclude-dir=^/proc",
        "--exclude-dir=^/sys",
        "--exclude-dir=^/dev",
        "--exclude-dir=^/run",
        "--exclude-dir=^/var/lib/clamav",
        "--log=" + str(STATE / "reports/clamav.log"),
        "/",
    ]
    sys.run(args, mutate=True, timeout=21600, allowed_codes=(0, 1))
    sys.audit("ClamAV scan finished")


def speedtest(sys):
    output = sys.run(["speedtest-cli", "--secure", "--json"], timeout=180, mutate=True)
    if sys.dry:
        return {"preview": True}
    try:
        data = json.loads(output)
        down, up, ping = (
            float(data["download"]),
            float(data["upload"]),
            float(data["ping"]),
        )
        import math

        if not all(math.isfinite(x) and x >= 0 for x in (down, up, ping)):
            raise ValueError("Invalid measurement")
        result = {
            "download_mbps": round(down / 1_000_000, 2),
            "upload_mbps": round(up / 1_000_000, 2),
            "ping_ms": round(ping, 2),
            "server": str(data.get("server", {}).get("name", "?")),
            "country": str(data.get("server", {}).get("country", "?")),
            "isp": str(data.get("client", {}).get("isp", "?")),
            "time": stamp(),
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise Error("error_speedtest_cli_returned_invalid_results") from exc
    sys.write(STATE / "reports/speedtest.json", json.dumps(result, indent=2))
    return result


def backup_env(sys):
    data = json.loads(sys.read(CONF / "backup.json", "{}"))
    if not data.get("repository"):
        raise Error("error_configure_a_restic_repository_first")
    env = {
        **SAFE_ENV,
        "RESTIC_REPOSITORY": data["repository"],
        "RESTIC_PASSWORD_FILE": str(CONF / "restic-password"),
        "RCLONE_CONFIG": str(CONF / "rclone.conf"),
    }
    env.update(
        {
            k: str(v)
            for k, v in data.get("s3", {}).items()
            if k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION")
        }
    )
    return data, env


def configure_backup(sys, repository, password, s3=None):
    if not repository.startswith(("rclone:", "s3:", "sftp:")) or any(
        c in repository for c in "\r\n\x00"
    ):
        raise Error("error_use_rclone_remote_path_s3_https_endpoint_bucket")
    if len(password) < 12 or "\n" in password:
        raise Error("error_restic_password_at_least_12_characters_one_line")
    if sys.exists(CONF / "backup.json"):
        raise Error("error_a_backup_repository_is_already_configured_edit_backup")
    sys.write(CONF / "restic-password", password + "\n")
    sys.write(
        CONF / "backup.json",
        json.dumps({"repository": repository, "s3": s3 or {}}, indent=2),
    )
    if not sys.exists(CONF / "rclone.conf"):
        sys.write(CONF / "rclone.conf", "# Configure your rclone remote here\n")


def backup(sys, action="backup", target=None, repository=None, snapshot="latest"):
    from . import backups

    profile = backups.load(sys, repository)
    if action == "snapshots":
        return json.dumps(backups.snapshots(sys, profile), indent=2)
    return backups.operate(sys, profile["id"], action, target, snapshot)


def deploy(sys, site, repository, ref="main"):
    if not re.fullmatch(
        r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+(?:\.git)?", repository
    ):
        raise Error("error_use_a_public_https_git_repository_url_without")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,100}", ref) or ".." in ref:
        raise Error("error_invalid_git_branch_tag")
    home = SITES / site["domain"]
    target = home / "releases" / stamp()
    sys.mkdir(home / "releases", 0o750)
    sys.run(["chown", f'{site["user"]}:{site["user"]}', home / "releases"], mutate=True)
    sys.run(
        [
            "runuser",
            "-u",
            site["user"],
            "--",
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            "--",
            repository,
            target,
        ],
        mutate=True,
        timeout=900,
    )
    # No automatic Composer/npm scripts: deployment code executes as the site user.
    return target
