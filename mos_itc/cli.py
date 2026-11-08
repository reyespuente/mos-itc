"""Entry points. Interactive and scheduled operations use the same code paths."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import core, ops, web, recovery
from .core import Error, System, STATE
from .ui import UI


def uninstall(system):
    if system.exists(STATE / "ssh-pending.json"):
        raise Error("error_confirm_or_roll_back_the_pending_ssh_change")
    for label in ("metrics", "backup", "clamav", "ssh-recovery"):
        system.run(
            ["systemctl", "disable", "--now", f"mos-{label}.timer"],
            mutate=True,
            check=False,
        )
        for suffix in ("service", "timer"):
            system.delete(f"/etc/systemd/system/mos-{label}.{suffix}")
    system.run(["systemctl", "daemon-reload"], mutate=True)
    system.audit("MOS application uninstalled; services and retained data preserved")
    system.delete("/usr/local/bin/mos")
    system.delete("/usr/local/bin/mos-itc")
    system.delete("/etc/update-motd.d/00-mos-itc")
    diversion_state = STATE / "motd-diversions.json"
    for original in json.loads(system.read(diversion_state, "[]")):
        if original in ("/etc/update-motd.d/00-header",):
            system.run(
                ["dpkg-divert", "--local", "--rename", "--remove", original],
                mutate=True,
            )
    system.delete(diversion_state)
    target = Path("/opt/mos-itc")
    if target.is_symlink():
        raise Error("error_unexpected_application_directory_symlink")
    if system.dry:
        print(
            "[PREVIEW] remove /opt/mos-itc; preserve /etc/mos-itc and /var/lib/mos-itc"
        )
    elif target.exists():
        shutil.rmtree(target)
    print(
        "MOS removed. Services/websites remain. Preserved secrets/state: /etc/mos-itc and /var/lib/mos-itc. Certbot renewal hooks remain functional."
    )


def parser():
    p = argparse.ArgumentParser(
        prog="mos-itc",
        description="MOS ITC — Manage your Own Server (Debian/Ubuntu CLI toolbox)",
    )
    p.add_argument("--version", action="version", version=core.VERSION)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without system mutations, root or OS restrictions",
    )
    p.add_argument("--language", choices=["en", "es"])
    p.add_argument(
        "--allow-external-repositories",
        action="store_true",
        help="Explicitly allow existing third-party APT sources at your own responsibility; MOS never adds repositories",
    )
    sub = p.add_subparsers(dest="command")
    sub.add_parser("menu", help="Interactive arrow-key menus (default)")
    sub.add_parser("banner", help="Show ITC ASCII art without root")
    sub.add_parser("doctor", help="Show host capabilities without changes or root")
    sub.add_parser("metrics", help="Current CPU, RAM, disk and network JSON")
    sub.add_parser(
        "speedtest", help="Install speedtest-cli if approved; run and display results"
    )
    sub.add_parser("configure", help="Interactive production profile")
    sub.add_parser("packages", help="Package inventory and checklist")
    sub.add_parser("install-web", help="Default/custom web stack wizard")
    site = sub.add_parser("website")
    s = site.add_subparsers(dest="action", required=True)
    s.add_parser("list")
    create = s.add_parser("create")
    create.add_argument("domain")
    create.add_argument("--php", help="Installed PHP-FPM branch, e.g. 8.5")
    create.add_argument("--www", action="store_true")
    create.add_argument("--laravel", action="store_true")
    create.add_argument("--timezone", default="UTC")
    create.add_argument("--database", action="store_true")
    s.add_parser("manage", help="Interactive website menu")
    sub.add_parser("ssl", help="Certificate manager")
    backup = sub.add_parser("backup")
    b = backup.add_subparsers(dest="action", required=True)
    for action in ("configure", "init", "run", "snapshots", "check"):
        b.add_parser(action)
    restore = b.add_parser("restore")
    restore.add_argument("target", help="New directory; existing targets are refused")
    ssh = sub.add_parser("ssh")
    s = ssh.add_subparsers(dest="action", required=True)
    s.add_parser("manage")
    s.add_parser("rollback")
    confirm = s.add_parser("confirm")
    confirm.add_argument("token")
    sub.add_parser("security")
    sub.add_parser("databases", help="Manage MariaDB and PostgreSQL databases")
    sub.add_parser("users", help="Manage local SSH administrators")
    restore_point = sub.add_parser(
        "restore-point", help="Installer configuration recovery"
    )
    point_commands = restore_point.add_subparsers(dest="action", required=True)
    point_commands.add_parser("list")
    extract = point_commands.add_parser("extract")
    extract.add_argument("name")
    extract.add_argument(
        "target", help="New directory for recovery; live files are never replaced"
    )
    sub.add_parser("uninstall")
    internal = sub.add_parser("internal", help="Scheduled systemd tasks")
    internal.add_argument("action", choices=["metrics", "scan", "ssh-recover"])
    return p


def doctor(system):
    print(
        json.dumps(
            {
                "os": core.os_info(),
                "python": sys.version.split()[0],
                "root": os.geteuid() == 0,
                "systemd": Path("/run/systemd/system").exists(),
                "tools": {
                    k: bool(shutil.which(k))
                    for k in (
                        "apt-get",
                        "apache2ctl",
                        "certbot",
                        "restic",
                        "rclone",
                        "speedtest-cli",
                        "nano",
                    )
                },
                "php_available": core.php_versions(system),
                "php_installed": core.php_versions(system, True),
                "version": core.VERSION,
            },
            indent=2,
        )
    )


def main(argv=None):
    args = parser().parse_args(argv)
    system = System(args.dry_run, args.allow_external_repositories)
    ui = None
    try:
        ui = UI(system, args.language)
        if args.command == "banner":
            ui.banner()
            return 0
        if args.command == "doctor":
            doctor(system)
            return 0
        if args.command == "metrics":
            print(json.dumps(ops.metrics(), indent=2))
            return 0
        if not system.dry:
            if os.geteuid() != 0:
                raise Error("error_run_sudo_mos_for_administration_or_python3_m")
            core.require_supported()
            # Private keys, database dumps and backups are always private by default.
            os.umask(0o077)
            if args.allow_external_repositories and args.command != "internal":
                if not ui.confirm("confirm_external_repositories"):
                    return 1
        # One process at a time; systemd retries on its next run if a task collides.
        # ponytail: global lock; split per-resource locks only if fleet workloads need concurrency.
        recovery = args.command == "internal" and args.action == "ssh-recover"
        interactive = (
            args.command
            in (
                None,
                "menu",
                "install-web",
                "packages",
                "configure",
                "ssl",
                "security",
                "speedtest",
                "uninstall",
                "users",
                "databases",
            )
            or (args.command == "ssh" and args.action == "manage")
            or (args.command == "website" and args.action == "manage")
            or (args.command == "backup" and args.action == "configure")
        )
        with core.lock(system.dry or interactive or recovery):
            if system.exists(STATE / "ssh-pending.json") and args.command not in (
                "ssh",
                "internal",
            ):
                ops.recover_ssh(system)
                if args.command not in (None, "menu") and system.exists(
                    STATE / "ssh-pending.json"
                ):
                    raise Error(
                        "error_confirm_or_roll_back_the_pending_ssh_change_first"
                    )
            if args.command in (None, "menu"):
                ui.main()
            elif args.command == "install-web":
                ui.session(lambda: ui.perform(ui.install_web))
            elif args.command == "packages":
                ui.session(ui.software)
            elif args.command == "configure":
                ui.session(lambda: ui.perform(ui.configure))
            elif args.command == "ssl":
                ui.session(ui.ssl)
            elif args.command == "databases":
                ui.session(ui.database_manager)
            elif args.command == "users":
                ui.session(ui.admin_users)
            elif args.command == "security":
                ui.session(ui.security_center)
            elif args.command == "speedtest":
                ui.session(lambda: ui.perform(ui.speed))
            elif args.command == "website":
                if args.action == "list":
                    print(json.dumps(system.data()["sites"], indent=2))
                elif args.action == "manage":
                    ui.session(ui.websites)
                elif args.action == "create":
                    host = core.domain(args.domain)
                    zone = ops.timezone(args.timezone)
                    if args.php:
                        core.php_version(args.php)
                    if args.laravel and not args.php:
                        raise Error("error_laravel_requires_php")
                    if ui.confirm(
                        ui.t(
                            "confirm_cli_site",
                            domain=host,
                            php=args.php or "static",
                            www=args.www,
                            timezone=zone,
                            database=args.database,
                        )
                    ):
                        site = web.create_site(
                            system, host, args.php, args.www, args.laravel, zone
                        )
                        if args.database:
                            print(
                                ui.t(
                                    "database_credentials_saved",
                                    path=web.create_database(system, site),
                                )
                            )
            elif args.command == "backup":
                if args.action == "configure":
                    ui.session(ui.backups)
                elif args.action == "restore":
                    if ui.confirm(
                        ui.t("restore_latest_snapshot_to_path", path=args.target)
                    ):
                        print(ops.backup(system, "restore", args.target))
                else:
                    print(
                        ops.backup(
                            system, "backup" if args.action == "run" else args.action
                        )
                    )
            elif args.command == "ssh":
                if args.action == "manage":
                    ui.session(ui.ssh)
                elif args.action == "confirm":
                    ops.confirm_ssh(system, args.token)
                    print(ui.t("ssh_confirmed"))
                elif ui.confirm("confirm_ssh_rollback"):
                    ops.recover_ssh(system, True)
            elif args.command == "restore-point":
                if args.action == "list":
                    print(json.dumps(recovery.list_points(), indent=2))
                elif ui.confirm(
                    ui.t("ui_extract_restore_point", name=args.name, target=args.target)
                ):
                    print(recovery.extract_point(args.name, args.target))
            elif args.command == "uninstall":
                ui.session(lambda: ui.perform(ui.uninstall))
            elif args.command == "internal":
                if args.action == "metrics":
                    ops.record_metrics(system)
                elif args.action == "scan":
                    ops.scan(system)
                else:
                    ops.recover_ssh(system)
        return 0
    except (Error, ValueError, OSError) as exc:
        message = exc.render(ui.lang) if isinstance(exc, Error) and ui else str(exc)
        print("MOS ITC: " + message, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
