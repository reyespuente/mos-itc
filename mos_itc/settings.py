"""Validated edits for service configuration, retaining rollback on failure."""

from pathlib import Path
import re
from . import core, ops
from .core import Error

PHP_FIELDS = {
    "memory_limit": "128M",
    "upload_max_filesize": "32M",
    "post_max_size": "40M",
    "max_execution_time": "60",
    "max_input_time": "60",
    "max_input_vars": "1000",
    "date.timezone": "UTC",
    "display_errors": "Off",
    "log_errors": "On",
    "opcache.enable": "1",
    "session.cookie_httponly": "1",
    "session.cookie_samesite": "Lax",
}


def php_path(system, version, sapi):
    if version not in core.php_versions(system, True) or sapi not in ("fpm", "cli"):
        raise Error("settings_invalid")
    return Path("/etc/php") / version / sapi / "conf.d/99-mos-production.ini"


def php_values(system, version, sapi):
    content = system.read(php_path(system, version, sapi))
    return {
        key: (
            re.search(r"^\s*" + re.escape(key) + r"\s*=\s*([^;\n]+)", content, re.M)[
                1
            ].strip()
            if re.search(r"^\s*" + re.escape(key) + r"\s*=\s*([^;\n]+)", content, re.M)
            else value
        )
        for key, value in PHP_FIELDS.items()
    }


def php_save(system, version, sapi, values):
    if set(values) != set(PHP_FIELDS):
        raise Error("settings_invalid")
    for key, value in values.items():
        if key == "date.timezone":
            ops.timezone(value)
        elif (
            key.endswith("_limit")
            or key.endswith("_filesize")
            or key == "post_max_size"
        ):
            if not re.fullmatch(r"(?:-1|[1-9][0-9]{0,5}[KMG]?)", value):
                raise Error("settings_invalid")
        elif key in ("display_errors", "log_errors"):
            if value not in ("On", "Off"):
                raise Error("settings_invalid")
        elif key == "session.cookie_samesite":
            if value not in ("Lax", "Strict", "None"):
                raise Error("settings_invalid")
        elif not re.fullmatch(r"[0-9]{1,7}", value):
            raise Error("settings_invalid")
    content = (
        "; Managed by MOS ITC\n"
        + "\n".join(f"{k} = {v}" for k, v in values.items())
        + "\n"
    )
    if sapi == "fpm":
        from . import web

        sites = [
            site
            for site in system.data()["sites"].values()
            if site.get("php") == version
        ]
        path = php_path(system, version, sapi)
        with system.transaction(
            [path] + [web.paths(site)[1] for site in sites],
            [[f"php-fpm{version}", "-t"]],
        ):
            system.write(path, content, 0o644)
            for site in sites:
                system.write(web.paths(site)[1], web.pool(site, values), 0o644)
        system.run(["systemctl", "reload", f"php{version}-fpm"], mutate=True)
        data = system.data()
        data.setdefault("php_profiles", {})[version] = values
        system.save(data)
    else:
        php_write(system, version, sapi, content)


def php_write(system, version, sapi, content, main=False):
    path = php_path(system, version, sapi)
    if main:
        path = path.parent.parent / "php.ini"
    checks = (
        [[f"php-fpm{version}", "-t"]]
        if sapi == "fpm"
        else [[f"php{version}", "-r", "exit(0);"]]
    )
    with system.transaction([path], checks):
        system.write(path, content, 0o644)
    if sapi == "fpm":
        system.run(["systemctl", "reload", f"php{version}-fpm"], mutate=True)


def renewal(system, value):
    from .backups import calendar

    spec = calendar("daily", value)
    override = Path("/etc/systemd/system/certbot.timer.d/90-mos-schedule.conf")
    with system.transaction([override]):
        system.write(
            override,
            f"[Timer]\nOnCalendar=\nOnCalendar={spec}\nRandomizedDelaySec=0\nPersistent=true\n",
            0o644,
        )
        system.run(["systemctl", "daemon-reload"], mutate=True)
        system.run(["systemctl", "enable", "--now", "certbot.timer"], mutate=True)
        system.run(["systemctl", "restart", "certbot.timer"], mutate=True)
    data = system.data()
    data["renewal_time"] = value
    system.save(data)


def firewall(system, action, port, protocol, source):
    import ipaddress

    if (
        action not in ("allow", "deny", "limit")
        or protocol not in ("tcp", "udp")
        or not 1 <= int(port) <= 65535
    ):
        raise Error("settings_invalid")
    if source != "any":
        source = str(ipaddress.ip_network(source, strict=False))
    if action != "allow" and str(port) in ops.ssh_ports(system):
        raise Error("firewall_ssh_protected")
    system.run(
        [
            "ufw",
            action,
            "from",
            source,
            "to",
            "any",
            "port",
            str(int(port)),
            "proto",
            protocol,
        ],
        mutate=True,
    )


def fail2ban(system, jail, retries, findtime, bantime, enabled):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", jail) or not 1 <= int(retries) <= 100:
        raise Error("settings_invalid")
    if not all(
        re.fullmatch(r"[1-9][0-9]{0,7}[smhdw]?", value) for value in (findtime, bantime)
    ):
        raise Error("settings_invalid")
    path = Path("/etc/fail2ban/jail.d/90-mos-" + jail + ".local")
    try:
        with system.transaction([path]):
            system.write(
                path,
                f"[{jail}]\nenabled = {str(enabled).lower()}\nmaxretry = {int(retries)}\nfindtime = {findtime}\nbantime = {bantime}\n",
                0o644,
            )
            system.run(["fail2ban-client", "-t"])
            ops.restart_fail2ban(system)
    except BaseException:
        system.run(["systemctl", "restart", "fail2ban"], mutate=True, check=False)
        raise
