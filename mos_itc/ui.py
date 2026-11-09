"""Dependency-free curses menus; regular prompts keep editors and SSH usable."""

import curses
import getpass
import contextlib
import datetime as dt
import io
from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys as python_sys
import tempfile
import textwrap
import threading
import urllib.request

from . import (
    core,
    web,
    ops,
    recovery,
    users,
    databases,
    postgresql,
    backups,
    settings,
    network,
    updates,
    transfers,
)
from .presentation import Table, table, rows as aligned_rows, size
from .core import Error, STATE, CONF, SITES
from .i18n import Translator
from .terminal import Terminal, Cancelled, WORDMARK, INSTITUTE, DISPLAY_AUTHOR, clean


class UI:
    def __init__(self, system, language=None):
        self.sys = system
        try:
            saved_language = system.data().get("language", "en")
        except PermissionError:
            saved_language = "en"
        self.lang = language or saved_language
        self.translator = Translator(self.lang)
        self.terminal = None
        self.activity = deque(maxlen=500)
        self.notice = ""
        self.context = {}

    def t(self, key, **values):
        return self.translator(key, **values)

    def banner(self):
        color = (
            python_sys.stdout.isatty() or os.environ.get("MOS_MOTD") == "1"
        ) and not os.environ.get("NO_COLOR")
        for i, line in enumerate(WORDMARK.splitlines()):
            ink = "\033[38;2;27;57;106m" if i < 2 else "\033[38;2;17;117;51m"
            print((ink if color else "") + line + ("\033[0m" if color else ""))
        command = "\033[38;2;17;117;51msudo mos-itc\033[0m" if color else "sudo mos-itc"
        print("Control panel: " + command)

    def session(self, action):
        if (
            self.terminal
            or not python_sys.stdin.isatty()
            or os.environ.get("TERM") in (None, "", "dumb")
        ):
            return action()
        threading.Thread(target=self.load_context, daemon=True).start()
        ui = self

        class Output(io.TextIOBase):
            def write(self, text):
                if text.strip():
                    ui.activity.extend(clean(text).splitlines())
                return len(text)

            def flush(self):
                pass

        def run(window):
            self.terminal = Terminal(window, self.t, self.sys.dry)
            self.terminal.context = self.context
            self.sys.progress = self.terminal.progress
            self.sys.output = self.notify
            try:
                with contextlib.redirect_stdout(Output()):
                    return action()
            finally:
                self.sys.progress = None
                self.sys.output = None
                self.terminal.close()
                self.terminal = None

        return curses.wrapper(run)

    def notify(self, text):
        text = clean(self.t(str(text)))
        self.activity.extend(text.splitlines())
        self.notice = text
        if self.terminal:
            self.terminal.status = text.splitlines()[-1] if text else self.t("ui_ready")
        else:
            print(text)

    def menu(self, title, options, selected=None, details=None):
        if self.terminal:
            return self.terminal.menu(title, options, selected, detail=details or "")
        print("\n" + self.t(title))
        for i, option in enumerate(options):
            print(f" {i+1}. {self.t(option)}")
        if selected is not None:
            raw = input(
                self.t("selected_numbers_comma_separated_blank_cancels") + ": "
            ).strip()
            return {int(n) - 1 for n in raw.split(",") if n.strip()} if raw else None
        raw = input("> ").strip()
        return int(raw) - 1 if raw.isdigit() and 0 < int(raw) <= len(options) else None

    def ask(self, label, default="", secret=False):
        if self.terminal:
            return self.terminal.input(label, default, secret)
        prompt = self.t(label) + (f" [{default}]" if default else "") + ": "
        return (getpass.getpass(prompt) if secret else input(prompt)).strip() or default

    def yes(self, label):
        if self.terminal:
            choice = self.menu(label, ["ui_no", "ui_yes"])
            if choice is None:
                raise Cancelled
            return choice == 1
        return input(self.t(label) + " [y/N / s]: ").strip().lower() in (
            "y",
            "yes",
            "s",
            "si",
            "sí",
        )

    def confirm(self, message, word=None, details=""):
        message = self.t(message)
        if self.terminal:
            text = message + ("\n\n" + details if details else "")
            if (
                self.terminal.document("ui_review", text, ("ui_cancel", "ui_apply"))
                != 1
            ):
                self.operation_cancelled = True
                return False
            return (
                self.ask(self.t("ui_type_confirmation", value=word)) == word
                if word
                else True
            )
        print("\n" + clean(message) + "\n" + clean(details))
        word = word or self.t("yes")
        return input(f'{self.t("type")} {word}: ').strip() == word

    def show(self, text, title="ui_report", artwork=False):
        if self.terminal:
            return self.terminal.document(
                title,
                text if isinstance(text, Table) else self.t(str(text)),
                artwork=artwork,
            )
        print(clean(self.t(str(text))))

    def perform(self, action, allow_pending=False, backup=False):
        self.operation_cancelled = False
        try:
            with core.lock(self.sys.dry or backup):
                if (
                    self.sys.exists(STATE / "ssh-pending.json")
                    and action != self.ssh
                    and not allow_pending
                ):
                    ops.recover_ssh(self.sys)
                    if self.sys.exists(STATE / "ssh-pending.json"):
                        raise Error("confirm_or_roll_back_the_pending_ssh_change_first")
                result = action()
            if self.terminal:
                self.terminal.status = self.t(
                    "cancelled" if self.operation_cancelled else "ui_success"
                )
                self.terminal.status_error = False
            return result
        except (Cancelled, KeyboardInterrupt):
            self.notify("cancelled")
        except (Error, ValueError, OSError) as exc:
            message = exc.render(self.lang) if isinstance(exc, Error) else str(exc)
            self.activity.append(message)
            if self.terminal:
                self.terminal.status = self.t("ui_failed")
                self.terminal.status_error = True
            self.show(message, "error")

    def editor(self, label, initial=""):
        self.notify(self.t(label) + " — nano: Ctrl+O, Enter, Ctrl+X")
        if self.sys.dry:
            print(self.t("preview_editor"))
            return initial
        if not shutil.which("nano"):
            raise Error("error_install_nano_first")
        with tempfile.TemporaryDirectory(prefix="mos-editor-") as tmp:
            p = Path(tmp) / "content.pem"
            self.sys.write(p, initial)
            # --ignorercfiles / restricted: don't execute user editor configuration.
            if self.terminal:
                self.terminal.win.refresh()
                curses.def_prog_mode()
                if not curses.isendwin():
                    curses.endwin()
            try:
                result = subprocess.run(
                    ["nano", "--ignorercfiles", "--restricted", str(p)],
                    env=core.SAFE_ENV,
                )
            finally:
                if self.terminal:
                    curses.reset_prog_mode()
                    curses.curs_set(0)
                    self.terminal.win.clearok(True)
                    self.terminal.win.touchwin()
                    # reset_prog_mode restores tty settings; refresh also leaves
                    # curses' ended state before another editor is launched.
                    self.terminal.win.refresh()
            if result.returncode:
                raise Error("error_editor_cancelled")
            if p.is_symlink():
                raise Error("error_editor_target_became_a_symlink")
            return p.read_text()

    def select_site(self):
        sites = self.sys.data()["sites"]
        if not sites:
            raise Error(self.t("no_websites_yet"))
        keys = sorted(sites)
        choice = self.menu(
            "select_website",
            [
                f'{k}  | PHP {sites[k].get("php") or "static"} | {sites[k]["ssl"]}'
                for k in keys
            ],
        )
        return sites[keys[choice]] if choice is not None else None

    def select_php(self, current=None):
        versions = core.php_versions(self.sys, True)
        if self.terminal:
            self.terminal.positions["php_version"] = (
                versions.index(current) + 1 if current in versions else len(versions)
            )
        choice = self.menu("php_version", ["static_website"] + versions)
        if choice is None:
            raise Cancelled
        return versions[choice - 1] if choice else None

    def packages(self):
        choice = self.menu(
            "installed_packages",
            [
                "manage_package_selection",
                "all_installed_debian_packages",
                "refresh_apt_indexes",
                "back",
            ],
        )
        if choice == 0:
            self.package_selection()
        elif choice == 1:
            self.installed_inventory()
        elif choice == 2 and self.confirm(
            "apt_update_refresh_available_package_indexes"
        ):
            core.refresh(self.sys)

    def package_selection(self):
        catalog = core.package_catalog(self.sys)
        inventory = {
            p: core.installed(self.sys, p) for _, packages in catalog for p in packages
        }
        before = {
            i
            for i, (_, packages) in enumerate(catalog)
            if any(inventory[p] for p in packages)
        }
        labels = [
            " / ".join(self.t(part) for part in label.split("|"))
            + ("  [" + self.t("installed") + "]" if i in before else "")
            for i, (label, _) in enumerate(catalog)
        ]
        selected = self.menu("manage_package_selection", labels, before)
        if selected is None:
            return
        if any(i < 0 or i >= len(catalog) for i in selected):
            raise Error("invalid_selection")
        add = [p for i in selected for p in catalog[i][1] if not inventory[p]]
        remove = [p for i in before - selected for p in catalog[i][1] if inventory[p]]
        purge = False
        if remove:
            kind = self.menu(
                "installed_packages",
                [
                    "remove_packages_only_keep_configuration_and_data",
                    "purge_package_configuration_retain_website_database_data",
                    "back",
                ],
            )
            if kind in (None, 2):
                return
            purge = kind == 1
        args, simulation = core.apt_plan(self.sys, add, remove, purge)
        details = (
            "INSTALL: "
            + ", ".join(add)
            + "\nREMOVE: "
            + ", ".join(remove)
            + "\n\n"
            + simulation
        )
        if args and self.confirm(
            "apply_the_displayed_apt_plan_configurations_and_data_are_separate",
            details=details,
        ):
            core.apply_packages(self.sys, args)

    def install_web(self):
        choice = self.menu(
            "install_web_server",
            ["default_installation", "custom_installation", "back"],
        )
        if choice == 1:
            return self.package_selection()
        if choice != 0:
            return
        if not self.confirm("refresh_apt_and_prepare_the_default_web_stack"):
            return
        core.refresh(self.sys)
        versions = core.php_versions(self.sys)
        if not versions and not self.sys.dry:
            raise Error("php_unavailable")
        packages = core.BASE + [
            package for v in versions for package in core.php_packages(self.sys, v)
        ]
        args, simulation = core.apt_plan(self.sys, packages)
        details = (
            self.t(
                "php_packages_available_versions", versions=", ".join(versions) or "—"
            )
            + "\n\n"
            + simulation
        )
        if self.confirm("install_the_default_web_stack_shown_above", details=details):
            core.apply_packages(self.sys, args)
            if versions:
                self.sys.run(
                    [
                        "update-alternatives",
                        "--set",
                        "php",
                        "/usr/bin/php" + versions[-1],
                    ],
                    mutate=True,
                )
            if self.yes("automatic_configuration"):
                self.configure()

    def configure(self):
        if ops.profile_applied(self.sys):
            return self.show(
                self.t("settings_already_configured"), "automatic_configuration"
            )
        if not core.php_versions(self.sys, True) and not core.installed(
            self.sys, "apache2"
        ):
            choice = self.menu(
                "setup_stack_first", ["install_web_server", "setup_system_only", "back"]
            )
            if choice in (None, 2):
                return
            if choice == 0:
                return self.install_web()
        zone = ops.timezone(self.ask("timezone", "America/Mexico_City"))
        host = self.ask("hostname", "mos-server")
        versions = core.php_versions(self.sys, True)
        i = self.menu("default_php_for_cli", ["keep_current_default"] + versions)
        version = versions[i - 1] if i else None
        if self.confirm(
            self.t(
                "confirm_production_profile",
                timezone=zone,
                hostname=host,
                php=version or "—",
            )
        ):
            ops.auto_configure(self.sys, zone, host, version)
            if self.yes("apply_firewall_fail2ban_profile"):
                self.apply_security()

    def websites(self):
        while True:
            sites = self.sys.data()["sites"]
            hosts = sorted(sites)
            labels = aligned_rows(
                [
                    (
                        h,
                        sites[h].get("php") or self.t("static_website"),
                        self.ssl_label(sites[h]["ssl"]),
                    )
                    for h in hosts
                ],
                [48, 10, 14],
            )
            options = (
                ["create_website"]
                + labels
                + ["import_existing_static_php_website_beta", "back"]
            )
            choice = self.menu(
                "websites",
                options,
                details={
                    label: self.site_summary(sites[h])
                    for h, label in zip(hosts, labels)
                },
            )
            if choice is None or choice == len(options) - 1:
                return
            if choice == 0:
                self.perform(self.create_website)
            elif choice == len(options) - 2:
                self.perform(self.import_website)
            else:
                self.website_actions(hosts[choice - 1])

    def ssl_label(self, mode):
        return {
            "none": self.t("no_tls_http"),
            "custom": self.t("custom_certificate"),
            "letsencrypt": "Let’s Encrypt",
        }.get(mode, mode)

    def site_summary(self, site):
        pairs = [
            ("domain", site["domain"]),
            ("php_version", site.get("php") or self.t("static_website")),
            ("document_root", site["root"]),
            ("ssl_tls_mode", self.ssl_label(site["ssl"])),
            ("linux_user", site["user"]),
            ("timezone", site.get("timezone", "UTC")),
            ("ui_aliases", ", ".join(site.get("aliases", [])) or "—"),
        ]
        return table(
            [self.t("field"), self.t("value")], [(self.t(k), v) for k, v in pairs]
        )

    def create_website(self):
        host = core.domain(self.ask("domain"))
        www = self.yes("add_www_alias")
        version = self.select_php()
        laravel = self.yes("laravel_layout_public_directory") if version else False
        zone = ops.timezone(
            self.ask("timezone", self.sys.data().get("timezone", "America/Mexico_City"))
        )
        database = self.yes("create_a_mariadb_database")
        if not self.confirm(
            self.t(
                "confirm_create_website",
                domain=host,
                php=version or self.t("static_website"),
                www=www,
                database=database,
            )
        ):
            return
        site = web.create_site(self.sys, host, version, www, laravel, zone)
        if database:
            self.notify(
                self.t(
                    "database_credentials_saved",
                    path=web.create_database(self.sys, site),
                )
            )
        self.notify(self.t("ui_site_created", domain=host))
        self.configure_ssl(site)

    def website_actions(self, host):
        while host in self.sys.data()["sites"]:
            site = self.sys.data()["sites"][host]
            entries = [
                ("ui_site_details", lambda: self.show(self.site_summary(site), host)),
                ("edit_website", lambda: self.edit_site(site)),
                ("configure_website_ssl", lambda: self.configure_ssl(site)),
                ("site_logs", lambda: self.site_logs(site)),
                ("git_deployment", lambda: self.deploy_site(site)),
                ("website_ssh_sftp_access", lambda: self.site_student_access(site)),
                ("db_manage", lambda: self.database_site(site)),
                ("delete_website", lambda: self.remove_site(site)),
            ]
            choice = self.menu(
                host,
                [k for k, _ in entries] + ["back"],
                details={k: self.site_summary(site) for k, _ in entries},
            )
            if choice is None or choice == len(entries):
                return
            if choice in (0, 3, 5, 6):
                entries[choice][1]()
            else:
                self.perform(entries[choice][1])

    def edit_site(self, site):
        choice = self.menu(
            "edit_website",
            ["php_version", "document_root", "ui_aliases", "timezone", "back"],
        )
        if choice in (None, 4):
            return
        updated = dict(site)
        if choice == 0:
            updated["php"] = self.select_php(current=site.get("php"))
        elif choice == 1:
            updated["root"] = str(
                core.safe_path(self.ask("document_root", site["root"]))
            )
        elif choice == 2:
            raw = self.ask("ui_aliases", ", ".join(site.get("aliases", [])))
            updated["aliases"] = [
                core.domain(alias.strip()) for alias in raw.split(",") if alias.strip()
            ]
        elif choice == 3:
            updated["timezone"] = ops.timezone(
                self.ask("timezone", site.get("timezone", "UTC"))
            )
        if self.confirm("ui_confirm_site_changes", details=self.site_summary(updated)):
            web.apply_site(self.sys, updated, site)

    def remove_site(self, site):
        mode = self.menu(
            "delete_website",
            [
                "keep_website_files_and_database",
                "erase_website_files_and_database",
                "back",
            ],
        )
        if mode in (0, 1) and self.confirm(
            self.t("remove_website_domain", domain=site["domain"]),
            site["domain"] if mode == 1 else None,
        ):
            web.delete_site(self.sys, site, erase=mode == 1)

    def database_site(self, site):
        if site.get("database"):
            return self.database_detail(databases, site["database"])

        def create():
            if self.confirm("create_a_mariadb_database"):
                web.create_database(self.sys, site)

        self.perform(create)

    def database_manager(self):
        engines = [("MariaDB", databases), ("PostgreSQL", postgresql)]
        while True:
            choice = self.menu("db_manage", [label for label, _ in engines] + ["back"])
            if choice in (None, 2):
                return
            label, engine = engines[choice]
            if self.perform(lambda: self.ensure_database_engine(engine)):
                self.database_inventory(engine, label)

    def ensure_database_engine(self, engine):
        package = "postgresql" if engine is postgresql else "mariadb-server"
        if core.installed(self.sys, package):
            return True
        if not self.confirm(self.t("db_install_engine", package=package)):
            return False
        core.refresh(self.sys)
        args, plan = core.apt_plan(self.sys, [package])
        if not self.confirm("apply_package_installation", details=plan):
            return False
        core.apply_packages(self.sys, args)
        return True

    def database_inventory(self, engine, label):
        while True:
            rows = self.perform(lambda: engine.inventory(self.sys))
            if rows is None:
                return
            names = [r["database"] for r in rows]
            options = (
                ["db_create"]
                + aligned_rows(
                    [
                        (
                            r["database"],
                            r["site"]
                            or self.t("db_managed" if r["managed"] else "db_external"),
                            size(r["bytes"]),
                        )
                        for r in rows
                    ],
                    [38, 32, 12],
                )
                + ["back"]
            )
            choice = self.menu(label, options)
            if choice is None or choice == len(options) - 1:
                return
            if choice == 0:
                self.perform(lambda: self.create_database(engine))
            elif not rows[choice - 1]["managed"]:
                self.perform(lambda: self.database_external(engine, names[choice - 1]))
            else:
                self.database_detail(engine, names[choice - 1])

    def create_database(self, engine):
        database = core.name(self.ask("db_name"))
        if self.confirm(self.t("db_confirm_create", database=database)):
            engine.create(self.sys, database)

    def database_detail(self, engine, database):
        while database in engine.managed(self.sys):
            choice = self.menu(
                database,
                [
                    "db_tables",
                    "db_users",
                    "db_export",
                    "db_delete",
                    "sql_terminal",
                    "local_exports",
                    "back",
                ],
            )
            if choice in (None, 6):
                return
            if choice == 4:
                self.perform(lambda: self.sql_terminal(engine, database))
                continue
            if choice == 5:
                self.export_inventory()
                continue
            if choice == 0:
                self.perform(
                    lambda: self.show(
                        self.t("db_tables_hint")
                        + "\n\n"
                        + self.database_table(
                            engine, engine.tables(self.sys, database)
                        ),
                        database,
                    )
                )
            elif choice == 1:
                self.database_users(engine, database)
            elif choice == 2:
                self.perform(lambda: self.show(self.export_dump(engine, database)))
            else:

                def remove():
                    if self.confirm(
                        self.t("db_confirm_delete", database=database), database
                    ):
                        self.show(
                            self.t(
                                "db_dump_saved",
                                path=(
                                    engine.remove(
                                        self.sys,
                                        database,
                                        force=self.yes("db_force_connections"),
                                    )
                                    if engine is postgresql
                                    else engine.remove(self.sys, database)
                                ),
                            )
                        )

                self.perform(remove)

    def database_role(self):
        roles = list(databases.ROLES)
        choice = self.menu(
            "db_permissions",
            ["db_role_" + role for role in roles] + ["back"],
            details={
                "db_role_read": self.t("db_read_hint"),
                "db_role_write": self.t("db_write_hint"),
                "db_role_admin": self.t("db_admin_hint"),
            },
        )
        if choice is None or choice == len(roles):
            raise Cancelled
        return roles[choice]

    def database_users(self, engine, database):
        while True:
            accounts = engine.accounts(self.sys, database)
            rows = (
                ["db_user_create", "db_user_existing"]
                + aligned_rows(
                    [
                        (
                            a["username"],
                            self.t("db_role_" + a["role"]),
                            self.t("db_primary") if a["primary"] else "",
                        )
                        for a in accounts
                    ],
                    [28, 24, 20],
                )
                + ["back"]
            )
            choice = self.menu(self.t("db_users_title", database=database), rows)
            if choice is None or choice == len(rows) - 1:
                return
            if choice == 0:

                def create():
                    username = core.name(self.ask("db_username"))
                    role = self.database_role()
                    if self.confirm(
                        self.t(
                            "db_confirm_user",
                            username=username,
                            database=database,
                            role=self.t("db_role_" + role),
                        )
                    ):
                        engine.create_user(self.sys, database, username, role)
                        self.database_access(engine, database, username)

                self.perform(create)
            elif choice == 1:

                def existing():
                    key = (
                        "postgresql_accounts"
                        if engine is postgresql
                        else "database_accounts"
                    )
                    candidates = sorted(
                        set(self.sys.data().get(key, {}))
                        - {a["username"] for a in accounts}
                    )
                    if not candidates:
                        return self.show(self.t("db_no_other_users"))
                    selected = self.menu("db_user_existing", candidates + ["back"])
                    if selected is None or selected == len(candidates):
                        return
                    username = candidates[selected]
                    role = self.database_role()
                    if self.confirm(
                        self.t(
                            "db_confirm_user",
                            username=username,
                            database=database,
                            role=self.t("db_role_" + role),
                        )
                    ):
                        engine.grant(self.sys, database, username, role)

                self.perform(existing)
            else:
                self.database_user_actions(engine, database, accounts[choice - 2])

    def database_access(self, engine, database, username):
        if self.confirm("db_reveal_warning"):
            # Deliberately bypass notify/activity: credentials stay out of logs.
            self.show(
                table(
                    [self.t("field"), self.t("value")],
                    engine.credentials(self.sys, database, username).items(),
                ),
                "db_access",
            )

    def database_user_actions(self, engine, database, account):
        username = account["username"]
        rows = ["db_access", "db_password"]
        if not account["primary"]:
            rows += ["db_permissions", "db_revoke", "db_user_delete"]
        choice = self.menu(username + "@" + database, rows + ["back"])
        if choice is None or choice == len(rows):
            return

        def action():
            if choice == 0:
                self.database_access(engine, database, username)
            elif choice == 1:
                if self.confirm(self.t("db_password_warning", username=username)):
                    engine.rotate_password(self.sys, database, username)
                    self.database_access(engine, database, username)
            elif choice == 2:
                role = self.database_role()
                if self.confirm(
                    self.t(
                        "db_confirm_user",
                        username=username,
                        database=database,
                        role=self.t("db_role_" + role),
                    )
                ):
                    engine.grant(self.sys, database, username, role)
            elif choice == 3:
                if self.confirm(
                    self.t("db_confirm_revoke", username=username, database=database),
                    username,
                ):
                    engine.revoke(self.sys, database, username)
            elif choice == 4:
                if self.confirm(
                    self.t("db_confirm_user_delete", username=username), username
                ):
                    engine.delete_user(self.sys, database, username)

        self.perform(action)

    def deploy_site(self, site):
        repo = self.ask("public_git_https_url")
        ref = self.ask("branch_or_tag", "main")
        if not self.confirm(
            self.t("confirm_git_clone", repository=repo, ref=ref, user=site["user"])
        ):
            return
        target = ops.deploy(self.sys, site, repo, ref)
        if site.get("php") and self.yes("run_composer_install_as_site_user"):
            self.sys.run(
                [
                    "runuser",
                    "-u",
                    site["user"],
                    "--",
                    f'php{site["php"]}',
                    "/usr/bin/composer",
                    "--working-dir=" + str(target),
                    "install",
                    "--no-dev",
                    "--optimize-autoloader",
                    "--no-interaction",
                ],
                mutate=True,
                timeout=1800,
            )
        sub = self.ask(
            "enter_release_public_subdirectory_empty_for_static",
            "public" if site.get("laravel") else "",
        )
        if sub and not re.fullmatch(r"[A-Za-z0-9_/-]+", sub):
            raise Error("error_invalid_public_subdirectory")
        root = core.safe_path(str(target / sub))
        if not self.sys.dry and not root.is_dir():
            raise Error("error_document_root_does_not_exist_in_release")
        # Apache needs traversal to the public directory, not read access to .env.
        ancestors = [root, *root.parents]
        for folder in ancestors:
            if folder == Path(SITES / site["domain"]):
                break
            if (
                folder == target
                or target in folder.parents
                or folder.name == "releases"
            ):
                self.sys.run(["setfacl", "-m", "u:www-data:--x", folder], mutate=True)
        self.sys.run(["setfacl", "-R", "-m", "u:www-data:r-X", root], mutate=True)
        if self.yes("activate_this_release"):
            web.apply_site(self.sys, {**site, "root": str(root)}, site)
        self.notify(self.t("release_ready_notice", path=target))

    def access_site(self, site):
        mode = self.menu(
            "website_ssh_sftp_access",
            [
                "disabled",
                "ssh_sftp",
                "sftp_only_filesystem_permissions_no_chroot",
                "back",
            ],
        )
        if mode in (0, 1, 2):
            key = self.ask("public_key") if mode else None
            if self.confirm(
                self.t(
                    "confirm_site_access",
                    mode=self.t(
                        [
                            "disabled",
                            "ssh_sftp",
                            "sftp_only_filesystem_permissions_no_chroot",
                        ][mode]
                    ),
                    user=site["user"],
                )
            ):
                if key:
                    ops.add_key(self.sys, site["user"], key)
                ops.site_access(self.sys, site, ["disabled", "ssh", "sftp"][mode])

    def import_website(self):
        print(self.t("website_import_notice"))
        source = core.safe_path(self.ask("import_source_document_root"))
        conf = core.safe_path(self.ask("import_source_configuration_file"))
        if (
            conf.parent != Path("/etc/apache2/sites-available")
            or conf.suffix != ".conf"
            or conf.name.startswith("mos-")
        ):
            raise Error("error_select_an_unmanaged_sites_available_conf_file")
        if not self.sys.dry and (not source.is_dir() or not conf.is_file()):
            raise Error("error_source_path_configuration_not_found")
        host = core.domain(self.ask("domain"))
        version = self.select_php()
        if not self.confirm(
            self.t(
                "confirm_beta_import",
                source=source,
                target=SITES / host,
                config=conf.name,
                domain=host,
            ),
            host,
        ):
            return
        self.sys.run(["a2dissite", conf.stem], mutate=True)
        saved = self.sys.read(conf)
        # Temporarily hide only the selected source during duplicate-name detection.
        self.sys.write(
            STATE / "import-backups" / (core.stamp() + "-" + conf.name), saved
        )
        self.sys.delete(conf)
        try:
            site = web.create_site(self.sys, host, version)
            if not self.sys.dry:
                dest = Path(site["root"])
                # Do not follow imported symlinks or copy device nodes/setuid bits.
                for item in source.rglob("*"):
                    if item.is_symlink() or (not item.is_file() and not item.is_dir()):
                        raise Error(
                            "Import refuses symlinks and special files: " + str(item)
                        )
                shutil.copytree(source, dest, dirs_exist_ok=True)
            self.sys.run(
                ["chown", "-R", f'{site["user"]}:{site["user"]}', site["root"]],
                mutate=True,
            )
            self.sys.run(["chmod", "-R", "u+rwX,go-rwx", site["root"]], mutate=True)
            self.sys.run(
                ["setfacl", "-R", "-m", "u:www-data:r-X", site["root"]], mutate=True
            )
        except BaseException:
            self.sys.write(conf, saved, 0o644)
            if host in self.sys.data()["sites"]:
                web.delete_site(self.sys, self.sys.data()["sites"][host], False)
            self.sys.run(["a2ensite", conf.stem], mutate=True)
            self.sys.run(["apache2ctl", "configtest"], mutate=True)
            self.sys.run(["systemctl", "reload", "apache2"], mutate=True)
            raise
        self.sys.write(conf, saved, 0o644)
        print(self.t("website_import_complete"))

    def configure_ssl(self, site=None):
        site = site or self.select_site()
        if not site:
            return
        choice = self.menu(
            "ssl_tls_mode",
            ["no_tls_http", "Let’s Encrypt", "custom_certificate", "back"],
            details={
                key: self.site_summary(site)
                for key in ("no_tls_http", "Let’s Encrypt", "custom_certificate")
            },
        )
        if choice == 0 and self.confirm(
            self.t("switch_domain_to_http_without_tls", domain=site["domain"])
        ):
            web.apply_site(self.sys, {**site, "ssl": "none", "certificate": ""}, site)
        elif choice == 1:
            email = self.ask("email")
            if self.confirm(self.t("confirm_letsencrypt_terms")):
                web.letsencrypt(self.sys, site, email)
        elif choice == 2:
            labels = sorted(self.sys.data()["certificates"])
            if not labels:
                raise Error("import_a_custom_certificate_first")
            i = self.menu(
                "select_certificate",
                labels,
                details={
                    label: self.certificate_summary(
                        label, self.sys.data()["certificates"][label]
                    )
                    for label in labels
                },
            )
            if i is not None and self.confirm(
                self.t(
                    "confirm_assign_certificate",
                    certificate=labels[i],
                    domain=site["domain"],
                )
            ):
                web.apply_site(
                    self.sys, {**site, "ssl": "custom", "certificate": labels[i]}, site
                )

    def ssl(self):
        return self.section(
            "ssl_certificates",
            [
                ("certificate_inventory", lambda: self.ssl_action(0)),
                ("certificate_add", lambda: self.ssl_action(1)),
                ("configure_website_ssl", lambda: self.ssl_action(2)),
                ("renew_let_s_encrypt_certificates", lambda: self.ssl_action(3)),
                ("ssl_schedule", self.ssl_schedule),
            ],
        )

    def certificate_summary(self, label, info):
        sites = web.certificate_users(self.sys, label)
        fields = [
            ("certificate_name", label),
            ("cert_domains", ", ".join(info.get("domains", [])) or "—"),
            ("cert_subject", info.get("subject", "—")),
            ("cert_issuer", info.get("issuer", "—")),
            ("cert_valid_from", info.get("valid_from", "—")),
            ("cert_expires", info.get("expires", "—")),
            ("cert_fingerprint", info.get("fingerprint", "—")),
            (
                "cert_assigned_sites",
                ", ".join(site["domain"] for site in sites)
                or (
                    label.removeprefix("Let’s Encrypt: ")
                    if label.startswith("Let’s Encrypt: ")
                    else self.t("cert_unused")
                ),
            ),
        ]
        if info.get("days_left") is not None:
            days = info["days_left"]
            fields.insert(
                1,
                (
                    "cert_status",
                    (
                        self.t("cert_expired")
                        if days < 0
                        else self.t("cert_expires_days", days=days)
                    ),
                ),
            )
        return table(
            [self.t("field"), self.t("value")], [(self.t(k), v) for k, v in fields]
        )

    def certificate_inventory(self):
        while True:
            labels = sorted(self.sys.data()["certificates"])
            letsencrypt = {
                "Let’s Encrypt: " + path.name: path / "cert.pem"
                for path in Path("/etc/letsencrypt/live").glob("*")
                if (path / "cert.pem").is_file()
            }
            labels += sorted(letsencrypt)
            inventory = {}
            certificate_rows = []
            for label in labels:
                cert_path = letsencrypt.get(
                    label, CONF / "certificates" / label / "cert.pem"
                )
                info = (
                    web.certificate_info(self.sys, cert_path)
                    if cert_path.exists()
                    else self.sys.data()["certificates"][label]
                )
                inventory[label] = info
                days = info.get("days_left")
                expiry = (
                    self.t("cert_expired")
                    if days is not None and days < 0
                    else (
                        self.t("cert_expires_days", days=days)
                        if days is not None
                        else self.t("cert_unknown_expiry")
                    )
                )
                certificate_rows.append(
                    (
                        label,
                        (
                            "Let’s Encrypt"
                            if label in letsencrypt
                            else self.t("custom_certificate")
                        ),
                        expiry,
                    )
                )
            rows = (
                ["cert_domain_security"]
                + aligned_rows(certificate_rows, [42, 20, 28])
                + ["back"]
            )
            choice = self.menu("certificate_inventory", rows)
            if choice is None or choice == len(rows) - 1:
                return
            if choice == 0:
                lines = [
                    f'{site["domain"]}  |  {site["ssl"]}  |  {site.get("certificate") or "—"}'
                    for site in self.sys.data()["sites"].values()
                ]
                self.show(
                    (
                        table(
                            [self.t("domain"), "SSL/TLS", self.t("certificate_name")],
                            [line.split("  |  ") for line in lines],
                        )
                        if lines
                        else self.t("no_websites_yet")
                    ),
                    "cert_domain_security",
                )
                continue
            label = labels[choice - 1]
            info = inventory[label]
            days = info.get("days_left")
            summary = self.certificate_summary(label, info)
            if label in letsencrypt:
                action = self.menu(
                    label,
                    ["cert_view_details", "renew_let_s_encrypt_certificates", "back"],
                )
                if action == 0:
                    self.show(summary, label)
                elif action == 1:
                    self.ssl_action(3)
                continue
            action = self.menu(
                label,
                ["cert_view_details", "cert_replace", "cert_assign", "back"],
                details={
                    key: summary
                    for key in ("cert_view_details", "cert_replace", "cert_assign")
                },
            )
            if action == 0:
                self.show(summary, label)
            elif action == 1:
                self.certificate_import_form(label)
            elif action == 2:
                site = self.select_site()
                if site and self.confirm(
                    self.t(
                        "confirm_assign_certificate",
                        certificate=label,
                        domain=site["domain"],
                    ),
                    details=summary,
                ):
                    web.apply_site(
                        self.sys, {**site, "ssl": "custom", "certificate": label}, site
                    )

    def certificate_import_form(self, label):
        values = list(web.certificate_contents(self.sys, label))
        fields = ["certificate_pem", "private_key_pem", "ca_bundle_pem_optional"]
        hints = ["cert_certificate_help", "cert_key_help", "cert_chain_help"]
        while True:
            rows = [
                self.t(
                    "cert_field_status",
                    field=self.t(field),
                    status=self.t(
                        "cert_ready"
                        if value.strip()
                        else "cert_optional_empty" if index == 2 else "cert_missing"
                    ),
                )
                for index, (field, value) in enumerate(zip(fields, values))
            ]
            rows += ["cert_validate_save", "ui_cancel"]
            details = {row: self.t(hint) for row, hint in zip(rows, hints)}
            details["cert_validate_save"] = self.t("cert_review_help")
            choice = self.menu(label, rows, details=details)
            if choice in (None, 4):
                return
            if choice < 3:
                mode = self.menu(
                    fields[choice],
                    ["cert_paste_nano", "cert_import_file", "cert_clear", "back"],
                    details={
                        "cert_paste_nano": self.t(hints[choice]),
                        "cert_import_file": self.t(hints[choice]),
                    },
                )
                try:
                    if mode == 0:
                        values[choice] = self.editor(fields[choice], values[choice])
                    elif mode == 1:
                        path = self.ask(
                            self.t("cert_path_for", field=self.t(fields[choice]))
                        )
                        values[choice] = web.read_pem_file(path)
                    elif mode == 2:
                        values[choice] = ""
                    if choice == 0 and values[0].strip():
                        leaf, embedded_chain = web.normalize_certificates(values[0])
                        if embedded_chain:
                            values[0], values[2] = leaf, embedded_chain
                except (Error, ValueError, OSError) as exc:
                    self.show(
                        exc.render(self.lang) if isinstance(exc, Error) else str(exc),
                        "error",
                    )
                continue
            if not values[0].strip() or not values[1].strip():
                self.show(self.t("cert_required_fields"), "error")
                continue
            try:
                metadata = web.validate_certificate(self.sys, *values, label=label)
                summary = self.certificate_summary(label, metadata)
                if not self.confirm(
                    self.t("confirm_certificate_import", certificate=label),
                    details=summary,
                ):
                    continue
                web.import_certificate(self.sys, label, *values)
            except Error as exc:
                self.show(
                    self.t("cert_validation_failed") + "\n\n" + exc.render(self.lang),
                    "error",
                )
                continue
            self.notify(self.t("cert_saved", name=label))
            return

    def ssl_action(self, choice):
        if choice == 0:
            self.certificate_inventory()
        elif choice == 1:
            label = core.name(self.ask("certificate_name"))
            if label in self.sys.data()["certificates"]:
                raise Error("cert_exists_inventory")
            self.certificate_import_form(label)
        elif choice == 2:
            self.configure_ssl()
        elif choice == 3 and self.confirm(
            "run_certbot_renew_only_certificates_due_for_renewal"
        ):
            self.show(
                self.sys.run(["certbot", "renew"], mutate=True, timeout=1800),
                "ssl_certificates",
            )

    def apply_security(self):
        ports = ops.ssh_ports(self.sys)
        if self.confirm(self.t("confirm_security_profile", ports=",".join(ports))):
            ops.security(self.sys, ports)

    def security(self):
        return self.section(
            "manage_security",
            [
                ("apply_firewall_fail2ban_profile", lambda: self.security_action(0)),
                ("run_clamav_scan_now", lambda: self.security_action(1)),
                ("configure_scheduled_scan", lambda: self.security_action(2)),
                ("clamav_report", lambda: self.security_action(3)),
                ("fail2ban_report", lambda: self.security_action(4)),
                ("firewall_rules", self.firewall_menu),
                ("fail2ban_settings", self.fail2ban_menu),
            ],
        )

    def security_action(self, choice):
        if choice == 0:
            self.apply_security()
        elif choice == 1 and self.confirm("confirm_clamav_scan"):
            ops.scan(self.sys)
        elif choice == 2:
            if self.confirm("schedule_a_daily_clamav_scan_at_03_30_server_time"):
                ops.install_timer(
                    self.sys,
                    "clamav",
                    "*-*-* 03:30:00",
                    "/usr/local/bin/mos-itc internal scan",
                    "malware scan",
                )
        elif choice == 3:
            self.show(
                self.sys.read(
                    STATE / "reports/clamav.log", self.t("no_scan_report_yet")
                )[-12000:]
            )
        elif choice == 4:
            self.show(self.sys.run(["fail2ban-client", "status", "sshd"], check=False))
        elif choice == 5:
            self.show(self.sys.run(["ufw", "status", "verbose"], check=False))

    def ssh(self):
        return self.section(
            "ssh_configuration",
            [
                ("add_public_ssh_key", lambda: self.ssh_action(0)),
                ("change_ssh_settings", lambda: self.ssh_action(1)),
                ("roll_back_pending_ssh_change", lambda: self.ssh_action(2)),
            ],
        )

    def ssh_action(self, choice):
        if choice == 0:
            user = core.name(self.ask("linux_user"))
            key = self.ask("public_key")
            if self.confirm(self.t("add_public_ssh_key_for_user", user=user)):
                ops.add_key(self.sys, user, key)
        elif choice == 1:
            port = int(self.ask("ssh_port", "22"))
            password = self.yes("allow_password_login")
            if self.confirm(self.t("confirm_ssh_change", port=port, password=password)):
                token = ops.change_ssh(self.sys, port, password)
                self.show(
                    self.t("ssh_confirmation_instructions", port=port, token=token),
                    "ssh_configuration",
                )
        elif choice == 2 and self.confirm(
            "restore_ssh_settings_saved_before_the_pending_change"
        ):
            ops.recover_ssh(self.sys, True)

    def performance(self):
        return self.section(
            "view_performance",
            [
                ("dashboard", self.live_dashboard),
                ("hourly_timeline_7_days", lambda: self.performance_action(1)),
                ("active_services", lambda: self.performance_action(2)),
                ("firewall_rules", lambda: self.performance_action(3)),
                ("clamav_report", lambda: self.performance_action(4)),
                ("fail2ban_report", lambda: self.performance_action(5)),
            ],
        )

    def live_dashboard(self):
        # Read-only monitoring must not hold the global mutation lock.
        from .terminal import dashboard_lines

        def services():
            return ops.dashboard_services(core.System(dry=self.sys.dry))

        if self.terminal:
            self.terminal.monitor(ops.metrics, services, ops.memory_processes)
        else:
            data = ops.metrics()
            self.show(
                "\n".join(
                    dashboard_lines(
                        data,
                        None,
                        [data["cpu"]],
                        services(),
                        ops.memory_processes(),
                        self.t,
                    )
                ),
                "dashboard",
            )

    def performance_action(self, choice):
        if choice == 0:
            if self.terminal:
                self.live_dashboard()
            else:
                self.show(json.dumps(ops.metrics(), indent=2))
        elif choice == 1:
            records = json.loads(self.sys.read(STATE / "metrics.json", "[]"))
            if not records:
                return self.show(self.t("no_metrics_yet"))
            values = [
                (
                    r["time"][:16] + " UTC",
                    bar(r["cpu"], 10),
                    bar(100 * r["ram_used"] / max(1, r["ram_total"]), 10),
                    bar(100 * r["disk_used"] / max(1, r["disk_total"]), 10),
                )
                for r in records
            ]
            self.show(
                table([self.t("time"), "CPU", "RAM", self.t("dashboard_disk")], values),
                "hourly_timeline_7_days",
            )
        elif choice == 2:
            output = self.sys.run(
                [
                    "systemctl",
                    "list-units",
                    "--type=service",
                    "--state=running",
                    "--no-legend",
                    "--plain",
                    "--no-pager",
                ],
                check=False,
                timeout=5,
            )
            values = [
                line.split(None, 4) for line in output.splitlines() if line.strip()
            ]
            self.show(
                table(
                    [self.t("service"), self.t("status"), self.t("description")],
                    [
                        (r[0], r[3], r[4] if len(r) > 4 else "")
                        for r in values
                        if len(r) >= 4
                    ],
                ),
                "active_services",
            )
        elif choice in (3, 4, 5):
            if choice == 3:
                self.show(self.sys.run(["ufw", "status", "verbose"], check=False))
            elif choice == 4:
                self.show(
                    self.sys.read(
                        STATE / "reports/clamav.log", self.t("no_scan_report_yet")
                    )[-12000:]
                )
            else:
                self.show(
                    self.sys.run(["fail2ban-client", "status", "sshd"], check=False)
                )

    def backups(self):
        while True:
            profiles = backups.profiles(self.sys)
            rows = (
                ["backup_add", "manual_archive", "local_exports"]
                + [
                    self.t(p["label"])
                    + "  ·  "
                    + self.t("backup_storage_" + p["transport"])
                    for p in profiles
                ]
                + ["backup_guide", "back"]
            )
            choice = self.menu(
                "backups",
                rows,
                details={
                    "backup_add": self.t("backup_wizard_hint"),
                    "backup_guide": self.t("backup_guide_text"),
                },
            )
            if choice is None or choice == len(rows) - 1:
                return
            if choice == 0:
                self.perform(self.backup_wizard, backup=True)
            elif choice == 1:
                self.perform(self.manual_archive, backup=True)
            elif choice == 2:
                self.export_inventory()
            elif choice == len(rows) - 2:
                self.show(self.t("backup_guide_text"), "backup_guide")
            else:
                self.backup_profile(profiles[choice - 3]["id"])

    def backup_dependencies(self):
        missing = [p for p in ("restic", "rclone") if not core.installed(self.sys, p)]
        if missing:
            if not self.confirm(
                self.t("backup_install_tools", packages=", ".join(missing))
            ):
                raise Cancelled
            with core.lock(self.sys.dry):
                core.refresh(self.sys)
                args, plan = core.apt_plan(self.sys, missing)
                if not self.confirm("apply_package_installation", details=plan):
                    raise Cancelled
                core.apply_packages(self.sys, args)

    def backup_runtime(self):
        if backups.compatible_rclone(self.sys):
            return self.show(self.t("backup_runtime_ready"), "backup_setup")
        if not self.confirm("backup_runtime_install"):
            raise Cancelled
        self.notify("backup_runtime_downloading")
        backups.install_rclone_runtime(self.sys)
        self.show(
            self.t("backup_runtime_ready")
            + "\n"
            + self.sys.run([backups.rclone_program(self.sys), "version"]).splitlines()[
                0
            ],
            "backup_setup",
        )

    def backup_sources(self, profile=None):
        options = [
            "backup_content_recommended",
            "backup_content_web",
            "backup_content_databases",
            "backup_content_custom",
            "back",
        ]
        choice = self.menu(
            "backup_what",
            options,
            details={key: self.t(key + ".description") for key in options[:-1]},
        )
        if choice in (None, 4):
            raise Cancelled
        if choice == 0:
            return ["/etc", str(STATE), str(SITES)], True
        if choice == 1:
            return [str(SITES)], False
        if choice == 2:
            return [], True
        paths = ["/etc", str(SITES), str(STATE), "/home", "/root"]
        current = set((profile or {}).get("paths", []))
        selected = self.menu(
            "backup_select_paths",
            paths,
            selected={i for i, p in enumerate(paths) if p in current},
        )
        if selected is None:
            raise Cancelled
        result = [paths[i] for i in sorted(selected)]
        extra = self.ask("backup_extra_paths")
        result += [v.strip() for v in extra.split(",") if v.strip()]
        databases = self.yes("backup_include_databases")
        return backups.validate_sources(result, databases), databases

    def backup_scan_host(self, host, port):
        keys = self.sys.run(
            ["ssh-keyscan", "-T", "8", "-p", str(port), host], check=False, timeout=15
        )
        keys = (
            "\n".join(
                line for line in keys.splitlines() if line and not line.startswith("#")
            )
            + "\n"
        )
        if not keys.strip():
            raise Error("backup_host_key_missing")
        with tempfile.TemporaryDirectory(prefix="mos-host-key-") as tmp:
            path = Path(tmp) / "known_hosts"
            self.sys.write(path, keys)
            fingerprints = (
                self.sys.run(["ssh-keygen", "-lf", path])
                if not self.sys.dry
                else "PREVIEW"
            )
        if not self.confirm("backup_host_key_trust", details=fingerprints):
            raise Cancelled
        return keys

    def backup_wizard(self, existing=None):
        self.backup_dependencies()
        if existing and existing["id"] == "legacy":
            raise Error("backup_legacy_readonly")
        identifier = (
            existing["id"] if existing else core.name(self.ask("backup_identifier"))
        )
        label = self.ask("backup_label", existing["label"] if existing else identifier)
        kinds = [
            "s3",
            "s3-compatible",
            "sftp",
            "ftps",
            "ftps-implicit",
            "ftp",
            "rclone",
        ]
        choice = self.menu(
            "backup_storage_type",
            ["backup_storage_" + k for k in kinds] + ["back"],
            details={
                "backup_storage_" + k: self.t("backup_storage_" + k + ".description")
                for k in kinds
            },
        )
        if choice is None or choice == len(kinds):
            raise Cancelled
        kind = kinds[choice]
        if kind in backups.FILE_TRANSPORTS and not backups.compatible_rclone(self.sys):
            self.backup_runtime()
        fields = {}
        attachments = {}
        if kind in ("s3", "s3-compatible"):
            if kind == "s3-compatible":
                fields["endpoint"] = self.ask("backup_s3_endpoint")
            fields["bucket"] = self.ask("backup_bucket")
            fields["prefix"] = self.ask(
                "backup_remote_prefix", "mos-itc/" + os.uname().nodename
            )
            fields["region"] = self.ask(
                "backup_region", "us-east-1" if kind == "s3" else ""
            )
            fields["access_key"] = self.ask("backup_access_key", secret=True)
            fields["secret_key"] = self.ask("backup_secret_key", secret=True)
        elif kind == "rclone":
            fields["repository"] = self.ask(
                "backup_existing_rclone", "rclone:remote:mos-itc/" + identifier
            )
            fields["config_file"] = self.ask(
                "backup_existing_rclone_config", str(CONF / "rclone.conf")
            )
        else:
            if kind == "ftp" and not self.confirm("backup_plain_ftp_warning"):
                raise Cancelled
            fields["host"] = self.ask("backup_host")
            fields["port"] = int(
                self.ask(
                    "port",
                    (
                        "22"
                        if kind == "sftp"
                        else "990" if kind == "ftps-implicit" else "21"
                    ),
                )
            )
            fields["user"] = self.ask("backup_remote_user")
            fields["path"] = self.ask(
                "backup_remote_directory", "mos-itc/" + identifier
            )
            if kind == "sftp":
                attachments["known_hosts"] = self.backup_scan_host(
                    fields["host"], fields["port"]
                )
                fields["known_hosts_file"] = str(
                    backups.folder(identifier) / "known_hosts"
                )
                auth = self.menu(
                    "backup_auth", ["backup_auth_password", "backup_auth_key", "back"]
                )
                if auth in (None, 2):
                    raise Cancelled
                if auth == 1:
                    key = core.safe_path(self.ask("backup_key_path"))
                    attachments["identity"] = web.read_pem_file(key)
                    fields["key_file"] = str(backups.folder(identifier) / "identity")
                    fields["key_password"] = self.ask(
                        "backup_key_passphrase", secret=True
                    )
                else:
                    fields["password"] = self.ask("backup_remote_password", secret=True)
            else:
                fields["password"] = self.ask("backup_remote_password", secret=True)
        if kind in ("s3-compatible", "ftps", "ftps-implicit"):
            ca = self.ask("backup_ca_path")
            if ca:
                attachments["ca.pem"] = web.read_pem_file(core.safe_path(ca))
        password = self.ask(
            (
                "backup_encryption_password_keep"
                if existing
                else "backup_encryption_password"
            ),
            secret=True,
        )
        if existing and not password:
            password = self.sys.read(backups.folder(identifier) / "password").rstrip(
                "\n"
            )
        elif password != self.ask("repeat_password", secret=True):
            raise Error("passwords_do_not_match")
        if existing:
            paths, databases = existing.get("paths", []), existing.get(
                "databases", True
            )
        else:
            paths, databases = self.backup_sources()
        if kind in ("s3", "s3-compatible"):
            destination = (
                backups.endpoint(
                    fields.get("endpoint")
                    or "https://s3." + fields["region"] + ".amazonaws.com"
                )
                + "/"
                + fields["bucket"]
                + "/"
                + fields["prefix"]
            )
        elif kind == "rclone":
            destination = fields["repository"]
        else:
            destination = (
                kind
                + "://"
                + fields["host"]
                + ":"
                + str(fields["port"])
                + "/"
                + fields["path"].lstrip("/")
            )
        summary = self.t(
            "backup_review_text",
            destination=destination,
            label=label,
            transport=kind,
            paths="\n".join(paths) or "—",
            databases=self.t("ui_yes" if databases else "ui_no"),
        )
        if not self.confirm("backup_save_destination", details=summary):
            return
        profile = backups.create(
            self.sys,
            identifier,
            label,
            password,
            kind,
            fields,
            paths,
            databases,
            attachments,
            replace=bool(existing),
        )
        self.show(self.t("backup_password_keep"), "backup_setup")
        self.show(self.t(backups.test_connection(self.sys, profile)), "backup_setup")
        choice = self.menu(
            "backup_initialize_choice",
            ["backup_new_repository", "backup_use_existing", "backup_do_later"],
            details={
                "backup_new_repository": self.t("backup_new_repository.description"),
                "backup_use_existing": self.t("backup_use_existing.description"),
            },
        )
        if choice == 0 and self.confirm("backup_init_warning"):
            self.show(backups.operate(self.sys, identifier, "init"), "backup_setup")
        elif choice == 1:
            rows = backups.snapshots(self.sys, profile)
            self.show(self.t("backup_connected", count=len(rows)), "backup_setup")
        self.show(self.t("backup_setup_complete"), "backup_setup")

    def backup_summary(self, profile):
        state = backups.status(self.sys, profile)
        schedule = self.t("backup_schedule_off")
        kind = profile.get("schedule_kind")
        if kind in ("daily", "weekly", "hours"):
            schedule = self.t(
                "backup_schedule_" + kind + "_value",
                value=profile.get("schedule_value", ""),
            )
        elif profile.get("schedule"):
            schedule = profile["schedule"]
        return self.t(
            "backup_summary_text",
            label=profile["label"],
            transport=profile["transport"],
            destination=profile.get("destination", profile["repository"]),
            paths="\n".join(profile.get("paths", [])) or "—",
            databases=self.t("ui_yes" if profile.get("databases", True) else "ui_no"),
            schedule=schedule,
            state=self.t("backup_state_" + state.get("state", "never")),
            action=(
                self.t("backup_action_" + state["action"])
                if state.get("action")
                else "—"
            ),
            last_backup=state.get("last_backup") or "—",
            time=state.get("time", "—"),
        )

    def backup_profile(self, identifier):
        entries = [
            "backup_summary",
            "back_up_now",
            "backup_copies_restore",
            "backup_schedule",
            "backup_what",
            "backup_maintenance",
            "backup_events",
            "backup_guide",
            "backup_edit_connection",
            "back",
        ]
        while True:
            profile = backups.load(self.sys, identifier)
            choice = self.menu(
                profile["label"],
                entries,
                details={
                    "backup_summary": self.backup_summary(profile),
                    "back_up_now": self.t("backup_now_hint"),
                    "backup_schedule": self.t("backup_schedule_hint"),
                },
            )
            if choice in (None, len(entries) - 1):
                return
            if choice == 0:
                self.show(self.backup_summary(profile), "backup_summary")
            elif choice == 1:

                def run():
                    if self.confirm(
                        "backup_run_review", details=self.backup_summary(profile)
                    ):
                        self.show(
                            backups.operate(self.sys, identifier, "backup"),
                            "back_up_now",
                        )

                self.perform(run, backup=True)
            elif choice == 2:
                self.perform(lambda: self.backup_restore(profile), backup=True)
            elif choice == 3:
                self.perform(lambda: self.backup_schedule(profile), backup=True)
            elif choice == 4:

                def sources():
                    paths, db = self.backup_sources(profile)
                    if self.confirm("backup_save_sources", details="\n".join(paths)):
                        backups.save_sources(self.sys, profile, paths, db)

                self.perform(sources, backup=True)
            elif choice == 5:
                self.backup_maintenance(profile)
            elif choice == 6:
                rows = [
                    json.loads(line)
                    for line in backups.events(self.sys, profile).splitlines()
                    if line.strip()
                ]
                text = "\n\n".join(
                    r.get("time", "")[:19]
                    + " UTC · "
                    + self.t("backup_action_" + r.get("action", ""))
                    + " · "
                    + self.t("backup_state_" + r["state"])
                    + f' · {r.get("duration",0)} s'
                    + ("\n" + self.t(r["error"]) if r.get("error") else "")
                    for r in rows
                )
                self.show(text or self.t("no_operations_yet"), "backup_events")
            elif choice == 7:
                self.show(self.t("backup_guide_text"), "backup_guide")
            elif choice == 8:
                if self.confirm("backup_edit_warning"):
                    self.perform(lambda: self.backup_wizard(profile), backup=True)

    def backup_restore(self, profile):
        rows = backups.snapshots(self.sys, profile)
        if not rows:
            return self.show(self.t("backup_no_copies"))
        rows = sorted(rows, key=lambda r: r.get("time", ""), reverse=True)
        choice = self.menu(
            "backup_choose_copy",
            [
                r.get("time", "")[:19]
                + " UTC  ·  "
                + r.get("hostname", "")
                + "  ·  "
                + r["id"][:8]
                for r in rows
            ]
            + ["back"],
        )
        if choice is None or choice == len(rows):
            return
        row = rows[choice]
        action = self.menu(
            "backup_copy_actions",
            ["backup_copy_details", "backup_restore_copy", "snapshot_delete", "back"],
        )
        if action == 0:
            self.show(
                table([self.t("field"), self.t("value")], row.items()),
                "backup_copy_details",
            )
        elif action == 1:
            target = self.ask(
                "backup_restore_directory", "/srv/mos-restore-" + row["id"][:8]
            )
            if self.confirm(
                "backup_restore_warning",
                details=self.t(
                    "backup_restore_review", snapshot=row["id"][:8], target=target
                ),
            ):
                self.show(
                    backups.operate(
                        self.sys, profile["id"], "restore", target, row["id"]
                    ),
                    "backup_restore_copy",
                )
                self.show(
                    self.t("backup_restore_done", target=target), "backup_restore_copy"
                )

        elif action == 2 and self.confirm("snapshot_delete_warning", row["id"][:8]):
            self.show(
                backups.operate(self.sys, profile["id"], "forget", snapshot=row["id"]),
                "snapshot_delete",
            )

    def backup_schedule(self, profile):
        kinds = ["daily", "weekly", "hours", "off"]
        choice = self.menu(
            "backup_schedule", ["backup_schedule_" + k for k in kinds] + ["back"]
        )
        if choice in (None, 4):
            return
        kind = kinds[choice]
        value = ""
        if kind in ("daily", "weekly"):
            value = self.ask("backup_time", "02:30")
        elif kind == "hours":
            intervals = [1, 2, 3, 4, 6, 8, 12]
            selected = self.menu(
                "backup_interval", [str(h) + " h" for h in intervals] + ["back"]
            )
            if selected is None or selected == len(intervals):
                return
            value = str(intervals[selected])
        if kind != "off":
            backups.calendar(kind, value)
        preview = (
            self.t("backup_schedule_" + kind + "_value", value=value)
            if kind != "off"
            else self.t("backup_schedule_off")
        )
        if self.confirm(
            "backup_confirm_schedule",
            details=preview + "\n" + self.t("backup_schedule_hint"),
        ):
            backups.schedule(self.sys, profile, kind, value)

    def backup_maintenance(self, profile):
        actions = [
            ("backup_test_connection", None),
            ("backup_runtime_tools", "runtime"),
            ("initialize_restic_repository", "init"),
            ("check_repository", "check"),
            ("backup_check_data", "check-data"),
            ("backup_locks", "locks"),
            ("backup_unlock", "unlock"),
            ("backup_repair_index", "repair-index"),
        ]
        while True:
            choice = self.menu(
                "backup_maintenance", [key for key, _ in actions] + ["back"]
            )
            if choice is None or choice == len(actions):
                return
            label, action = actions[choice]

            def perform():
                if action == "runtime":
                    return self.backup_runtime()
                if action is None:
                    return self.show(
                        self.t(backups.test_connection(self.sys, profile)), label
                    )
                warning = {
                    "init": "backup_init_warning",
                    "check": "backup_check_warning",
                    "check-data": "backup_check_data_warning",
                    "locks": "backup_locks_warning",
                    "unlock": "backup_unlock_warning",
                    "repair-index": "backup_repair_index_warning",
                }[action]
                word = (
                    "UNLOCK"
                    if action == "unlock"
                    else "REPAIR" if action == "repair-index" else None
                )
                if self.confirm(warning, word):
                    self.show(
                        backups.operate(self.sys, profile["id"], action)
                        or self.t("ui_success"),
                        label,
                    )

            self.perform(perform, backup=True)

    def speed(self):
        if not core.installed(self.sys, "speedtest-cli"):
            if not self.confirm("install_speedtest_cli_from_apt"):
                return
            core.refresh(self.sys)
            args, plan = core.apt_plan(self.sys, ["speedtest-cli"])
            if not self.confirm("apply_package_installation", details=plan):
                return
            core.apply_packages(self.sys, args)
        if not self.confirm("confirm_speedtest"):
            return
        print(self.t("testing_download_upload_and_latency"))
        result = ops.speedtest(self.sys)
        if result.get("preview"):
            print(self.t("speedtest_preview_notice"))
        else:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                speed_card(result, self.lang)
            self.show(output.getvalue(), "make_a_speed_test")

    def uninstall(self):
        from .cli import uninstall

        choice = self.menu(
            "uninstall_mos_itc",
            [
                "remove_mos_only_keep_services_and_websites",
                "remove_mos_managed_websites_and_selected_services",
                "back",
            ],
        )
        if choice in (0, 1):
            if choice == 1:
                for host in list(self.sys.data()["sites"]):
                    if self.confirm(
                        self.t(
                            "permanently_erase_files_and_database_for_domain",
                            domain=host,
                        ),
                        host,
                    ):
                        web.delete_site(self.sys, self.sys.data()["sites"][host], True)
                self.package_selection()
            if self.confirm("remove_mos_itc_application_and_its_timers", "UNINSTALL"):
                uninstall(self.sys)
                return True
        return False

    def section(self, title, entries):
        while True:
            choice = self.menu(title, [key for key, _ in entries] + ["back"])
            if choice is None or choice == len(entries):
                return
            action = entries[choice][1]
            if action in (
                self.ssh,
                self.security,
                self.ssl,
                self.live_dashboard,
                self.advanced_settings,
                self.php_settings,
                self.restore_points,
                self.firewall_menu,
                self.fail2ban_menu,
                self.app_update,
            ):
                result = action()
            else:
                result = self.perform(
                    action, allow_pending=title == "ssh_configuration"
                )

            if result is True and action in (
                self.uninstall,
                self.advanced_settings,
                self.app_update,
            ):
                return True

    def software(self):
        return self.section(
            "ui_software",
            [
                ("app_update", self.app_update),
                ("ui_package_updates", self.package_updates),
                ("manage_package_selection", self.package_selection),
                ("install_web_server", self.install_web),
                ("all_installed_debian_packages", self.installed_inventory),
                ("apt_sources", self.apt_sources),
                ("php_settings", self.php_settings),
            ],
        )

    def package_updates(self):
        if not self.confirm("ui_refresh_updates"):
            return
        core.refresh(self.sys)
        args, plan = core.upgrade_plan(self.sys)
        if args and self.confirm("ui_confirm_updates", details=plan):
            core.apply_packages(self.sys, args)
            self.show(core.health_report(self.sys), "ui_health_checks")
        elif not args:
            self.show(self.t("ui_no_updates"), "ui_package_updates")

    def administrator_password(self):
        password = self.ask("users_password", secret=True)
        if password != self.ask("repeat_password", secret=True):
            raise Error("passwords_do_not_match")
        return users.validate_password(password)

    def create_administrator(self):
        missing = [
            package
            for package in ("sudo", "openssh-server")
            if not core.installed(self.sys, package)
        ]
        if missing:
            core.refresh(self.sys)
            args, plan = core.apt_plan(self.sys, missing)
            if not self.confirm("users_install_dependencies", details=plan):
                return
            core.apply_packages(self.sys, args)
        username = core.name(self.ask("linux_user"))
        password = self.administrator_password()
        needs_key = not users.password_login_allowed(self.sys)
        if needs_key:
            self.show(self.t("users_key_required"), "admin_users")
        public_key = (
            self.ask("public_key") if needs_key or self.yes("users_add_key") else ""
        )
        if not self.confirm(self.t("users_confirm_create", username=username)):
            return
        account = users.create_administrator(self.sys, username, password, public_key)
        port = ops.ssh_ports(self.sys)[0]
        self.show(
            self.t("users_created", username=account["username"], port=port),
            "admin_users",
        )

    def administrator_actions(self, account):
        username = account["username"]
        action = self.menu(
            username, ["add_public_ssh_key", "users_change_password", "back"]
        )
        if action == 0:
            key = self.ask("public_key")
            if self.confirm(self.t("add_public_ssh_key_for_user", user=username)):
                users.add_public_key(self.sys, username, key)
        elif action == 1:
            password = self.administrator_password()
            if self.confirm(self.t("users_confirm_password", username=username)):
                users.change_password(self.sys, username, password)

    def admin_users(self):
        while True:
            accounts = users.inventory(self.sys)
            labels = aligned_rows(
                [
                    (
                        a["username"],
                        self.t("account_" + a["kind"]),
                        a["workspace"] or a["home"],
                    )
                    for a in accounts
                ],
                [24, 20, 44],
            )
            choice = self.menu(
                "admin_users",
                ["users_create", "student_create"] + labels + ["back"],
                details={label: self.t("users_ssh_settings_hint") for label in labels},
            )
            if choice is None or choice == len(accounts) + 2:
                return
            if choice == 0:
                self.perform(self.create_administrator)
            elif choice == 1:
                self.perform(self.create_student)
            else:
                self.ssh_account_actions(accounts[choice - 2])

    def security_center(self):
        self.section(
            "ui_security_access",
            [
                ("manage_security", self.security),
                ("ssl_certificates", self.ssl),
            ],
        )

    def settings(self):
        return self.section(
            "ui_settings",
            [
                ("automatic_configuration", self.configure),
                ("system_settings", self.system_settings),
                ("ssh_configuration", self.ssh),
                ("php_settings", self.php_settings),
                ("download_settings", self.download_settings),
                ("language", self.change_language),
                ("advanced_settings", self.advanced_settings),
            ],
        )

    def restore_points(self):
        while True:
            points = recovery.list_points()
            if not points:
                return self.show(self.t("ui_no_restore_points"))
            labels = aligned_rows(
                [(p["name"], size(p.get("bytes", 0))) for p in points]
            )
            index = self.menu(
                "ui_restore_points",
                labels + ["back"],
                details={r: self.t("restore_points_help") for r in labels},
            )
            if index is None or index == len(points):
                return
            point = points[index]["name"]
            action = self.menu(
                point, ["restore_contents", "restore_extract", "delete", "back"]
            )
            if action == 0:
                self.show(recovery.contents(point), "restore_contents")
            elif action == 1:
                target = self.ask(
                    "restore_directory_must_not_exist",
                    "/srv/mos-recovery-" + point.replace(":", "-"),
                )
                if self.confirm(
                    self.t("ui_extract_restore_point", name=point, target=target)
                ):
                    self.perform(
                        lambda: self.notify(str(recovery.extract_point(point, target)))
                    )
            elif action == 2 and self.confirm("delete_restore_point", point):
                self.perform(lambda: recovery.remove(point))

    def change_language(self):
        language = self.menu("language", ["English", "Español latinoamericano"])
        if language is not None:
            self.lang = ["en", "es"][language]
            self.translator = Translator(self.lang)
            data = self.sys.data()
            data["language"] = self.lang
            self.sys.save(data)

    def about(self):
        license_text = (Path(__file__).resolve().parent.parent / "LICENSE").read_text()
        text = (
            self.t("ui_about_project", author=core.AUTHOR, version=core.VERSION)
            + "\n\n"
            + license_text
        )
        self.show(text, "about", artwork=True)

    def overview(self):
        from .terminal import dashboard_lines

        data = self.sys.data()
        sample = ops.metrics()
        info = self.context.get("network") or network.inventory()
        public = (
            self.context.get("public")
            or network.public(data.get("public_ip", ""))
            or info.get("public")
            or network.ec2_public()
        )
        self.context.update(network=info, public=public)
        summary = []
        for line in dashboard_lines(sample, None, [sample["cpu"]], [], [], self.t):
            if line.startswith(self.t("dashboard_cpu_history")):
                break
            summary.append(line)
        body = "\n".join(summary)
        addresses = [
            (
                a["interface"],
                f"{a['address']}/{a['prefix']}",
                self.t("public_address" if a["public"] else "private_ip"),
            )
            for a in info["addresses"]
        ]
        address_table = table(
            [self.t("interface"), self.t("address"), self.t("type")], addresses
        )
        body += "\n\n" + table(
            [self.t("field"), self.t("value")],
            [
                (
                    self.t("operating_system"),
                    core.os_info().get("PRETTY_NAME", "Linux"),
                ),
                (
                    self.t("public_address"),
                    self.context.get("public") or self.t("not_detected"),
                ),
                (
                    self.t("gateway"),
                    ", ".join(
                        r["gateway"] + " (" + r["interface"] + ")"
                        for r in info["routes"]
                    )
                    or "—",
                ),
                (self.t("timezone"), data.get("timezone", "UTC")),
                (self.t("websites"), len(data["sites"])),
                (self.t("certificate_inventory"), len(data["certificates"])),
            ],
        )
        body += "\n\n" + address_table
        self.show(body, "ui_overview")

    def main(self):
        return self.session(self.main_loop)

    def load_context(self):
        try:
            info = network.inventory()
            self.context.update(
                network=info,
                public=network.public(self.sys.data().get("public_ip", ""))
                or info["public"]
                or network.ec2_public(),
            )
            self.context["update"] = updates.latest()
        except (OSError, ValueError, KeyError):
            pass

    def outside(self, command, env=None):
        if self.sys.dry:
            return self.notify(" ".join(map(str, command)))
        if self.terminal:
            curses.def_prog_mode()
            if not curses.isendwin():
                curses.endwin()
        try:
            return subprocess.run(
                [str(x) for x in command], env=env or core.SAFE_ENV
            ).returncode
        finally:
            if self.terminal:
                curses.reset_prog_mode()
                curses.curs_set(0)
                self.terminal.win.clearok(True)
                self.terminal.win.refresh()

    def site_logs(self, site):
        mode = self.menu("site_logs", ["logs_recent", "logs_follow", "back"])
        if mode in (None, 2):
            return
        kind = self.menu("site_logs", ["logs_access", "logs_errors", "back"])
        if kind in (None, 2):
            return
        path = Path("/var/log/apache2") / (
            "mos-"
            + core.domain(site["domain"])
            + ("-access.log" if kind == 0 else "-error.log")
        )
        if not path.exists():
            return self.show(self.t("logs_not_yet"))
        if mode == 0:
            self.show(self.sys.run(["tail", "-n", "200", "--", path]), "site_logs")
        else:
            self.show(self.t("logs_follow_help"), "site_logs")
            self.outside(["tail", "-n", "50", "-F", "--", path])

    def database_table(self, engine, output):
        values = [r.split("\t") for r in output.splitlines() if r]
        count = 4 if engine is databases else 3
        for r in values:
            if len(r) == count:
                try:
                    r[-1] = size(r[-1])
                except ValueError:
                    pass
        headers = (
            [self.t("db_tables")]
            + (["Engine"] if count == 4 else [])
            + [self.t("estimated_rows"), self.t("size")]
        )
        return (
            table(headers, [r for r in values if len(r) == count])
            if values
            else self.t("db_no_tables")
        )

    def database_external(self, engine, database):
        choice = self.menu(
            database,
            ["db_introspect", "sql_terminal", "back"],
            details={
                "db_introspect": self.t("db_introspect_help"),
                "sql_terminal": self.t("sql_help"),
            },
        )
        if choice == 0:
            tables, grants = engine.inspect(self.sys, database)
            self.show(
                self.database_table(engine, tables)
                + "\n\n"
                + table(
                    [self.t("linux_user"), self.t("db_permissions")],
                    [
                        line.split("\t")
                        for line in grants.splitlines()
                        if len(line.split("\t")) == 2
                    ],
                ),
                database,
            )
        elif choice == 1:
            self.sql_terminal(engine, database)

    def sql_terminal(self, engine, database):
        self.show(self.t("sql_help"), "sql_terminal")
        while True:
            query = self.editor("sql_terminal", "SELECT 1;\n")
            if not query.strip():
                return
            if not self.confirm("sql_execute", details=query):
                return
            try:
                result = engine.console(self.sys, database, query)
            except Error as exc:
                self.show(str(exc), "sql_terminal")
            else:
                self.show(result or self.t("sql_done"), "sql_terminal")
            if not self.yes("sql_another"):
                return

    def export_dump(self, engine, database):
        path = engine.dump(self.sys, database)
        if self.yes("download_create_link"):
            self.download_link(transfers.register(self.sys, path))
        return self.t("db_dump_saved", path=path)

    def export_inventory(self):
        while True:
            transfers.cleanup(self.sys)
            items = transfers.records(self.sys)
            dumps = sorted((STATE / "database-dumps").glob("*.sql"))
            entries = [
                ("download", key, item["name"], item["size"])
                for key, item in items.items()
            ]
            entries += [
                ("dump", str(path), path.name, path.stat().st_size)
                for path in dumps
                if path.is_file() and not path.is_symlink()
            ]
            if not entries:
                return self.show(self.t("no_exports"), "local_exports")
            labels = aligned_rows([(r[2], size(r[3])) for r in entries], [56, 12])
            details = {
                label: table(
                    [self.t("field"), self.t("value")],
                    [
                        (self.t("file"), entry[2]),
                        (self.t("size"), size(entry[3])),
                        (
                            self.t("download_expires"),
                            (
                                dt.datetime.fromtimestamp(items[entry[1]]["expires"])
                                .astimezone()
                                .isoformat(timespec="seconds")
                                if entry[0] == "download"
                                else self.t("export_retained")
                            ),
                        ),
                    ],
                )
                for label, entry in zip(labels, entries)
            }
            choice = self.menu("local_exports", labels + ["back"], details=details)
            if choice is None or choice == len(entries):
                return
            kind, key, filename, _ = entries[choice]
            action = self.menu(filename, ["download_create_link", "delete", "back"])
            if action == 0:
                identifier = (
                    key if kind == "download" else transfers.register(self.sys, key)
                )
                self.perform(lambda: self.download_link(identifier))
            elif action == 1 and self.confirm("delete_export", filename):
                self.perform(
                    lambda: (
                        transfers.remove(self.sys, key)
                        if kind == "download"
                        else self.delete_export_source(key)
                    )
                )

    def delete_export_source(self, path):
        for identifier, item in transfers.records(self.sys).items():
            if item.get("source") == path:
                transfers.remove(self.sys, identifier)
        self.sys.delete(path)

    def download_link(self, identifier):
        sites = [
            site
            for site in self.sys.data()["sites"].values()
            if site["ssl"] in ("custom", "letsencrypt")
        ]
        options = [s["domain"] + " (HTTPS)" for s in sites] + ["download_http", "back"]
        choice = self.menu("download_transport", options)
        if choice is None or choice == len(options) - 1:
            return
        site = sites[choice] if choice < len(sites) else None
        if site is None and not self.confirm("download_http_warning"):
            return
        server, url = transfers.publish(self.sys, identifier, site)
        self.show(
            self.t(
                "download_link_ready",
                url=url,
                port=(
                    (443 if server["tls"] else 80)
                    if server.get("proxy")
                    else server["port"]
                ),
                minutes=self.sys.data().get("download_minutes", 60),
            ),
            "download_create_link",
        )

    def manual_archive(self):
        paths, sql = self.backup_sources()
        kind = self.menu("manual_archive", ["ZIP", "tar.gz", "back"])
        if kind in (None, 2):
            return
        if self.confirm("archive_confirm", details="\n".join(paths)):
            identifier = transfers.archive(
                self.sys, paths, ["zip", "tar.gz"][kind], sql
            )
            self.download_link(identifier)

    def system_settings(self):
        current = self.sys.data()
        host = self.ask("hostname", current.get("hostname", os.uname().nodename))
        zone = ops.timezone(self.ask("timezone", current.get("timezone", "UTC")))
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
            raise Error("settings_invalid")
        if not self.confirm(
            "system_settings_warning",
            details=table(
                [self.t("field"), self.t("value")],
                [(self.t("hostname"), host), (self.t("timezone"), zone)],
            ),
        ):
            return
        was_applied = ops.profile_applied(self.sys)
        old_host = os.uname().nodename
        old_zone = self.sys.run(
            ["timedatectl", "show", "--property=Timezone", "--value"]
        ).strip()
        hosts = self.sys.read("/etc/hosts")
        line = "127.0.1.1\t" + host
        hosts = (
            re.sub(r"^127\.0\.1\.1\s+.*$", line, hosts, flags=re.M)
            if re.search(r"^127\.0\.1\.1\s", hosts, re.M)
            else hosts.rstrip() + "\n" + line + "\n"
        )
        try:
            with self.sys.transaction(["/etc/hosts", STATE / "state.json"]):
                self.sys.write("/etc/hosts", hosts, 0o644)
                self.sys.run(["hostnamectl", "set-hostname", host], mutate=True)
                self.sys.run(["timedatectl", "set-timezone", zone], mutate=True)
                current.update(
                    hostname=host,
                    timezone=zone,
                    automatic_configuration_applied=was_applied,
                )
                self.sys.save(current)
        except BaseException:
            self.sys.run(
                ["hostnamectl", "set-hostname", old_host], mutate=True, check=False
            )
            if old_zone:
                self.sys.run(
                    ["timedatectl", "set-timezone", old_zone], mutate=True, check=False
                )
            raise
        self.show(self.t("settings_saved"))

    def download_settings(self):
        minutes = int(
            self.ask(
                "download_minutes", str(self.sys.data().get("download_minutes", 60))
            )
        )
        if not 1 <= minutes <= 1440:
            raise Error("settings_invalid")
        address = self.ask(
            "public_ip",
            self.sys.data().get("public_ip", self.context.get("public", "")),
        )
        if address and not network.public(address):
            raise Error("settings_invalid")
        if self.confirm("save_settings"):
            data = self.sys.data()
            data.update(download_minutes=minutes, public_ip=address)
            self.sys.save(data)
            self.context["public"] = address

    def installed_inventory(self):
        output = self.sys.run(
            [
                "dpkg-query",
                "-W",
                "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n",
            ]
        )
        self.show(
            table(
                [self.t("package"), self.t("version"), self.t("status")],
                [
                    line.split("\t")
                    for line in output.splitlines()
                    if len(line.split("\t")) == 3
                ],
            ),
            "all_installed_debian_packages",
        )

    def apt_sources(self):
        paths = (
            [Path("/etc/apt/sources.list")]
            + sorted(Path("/etc/apt/sources.list.d").glob("*.list"))
            + sorted(Path("/etc/apt/sources.list.d").glob("*.sources"))
        )
        index = self.menu("apt_sources", [str(p) for p in paths] + ["back"])
        if index is None or index == len(paths):
            return
        path = paths[index]
        content = self.editor("apt_sources", self.sys.read(path))
        if self.confirm("apt_sources_warning", details=content):
            with self.sys.transaction([path]):
                self.sys.write(path, content, 0o644)
                core.repository_check(self.sys)
                core.refresh(self.sys)
            self.show(self.t("settings_saved"))

    def app_update(self):
        if self.sys.exists(STATE / "ssh-pending.json"):
            raise Error("confirm_or_roll_back_the_pending_ssh_change_first")
        try:
            new = updates.latest()
        except (OSError, ValueError):
            return self.show(self.t("app_update_failed"))
        self.context["update"] = new
        if not new:
            return self.show(self.t("app_up_to_date"))
        if not self.confirm(
            "app_update_confirm", details=core.VERSION + " → " + new["tag"]
        ):
            return
        with tempfile.TemporaryDirectory(prefix="mos-upgrade-") as tmp:
            path = Path(tmp) / "install.sh"
            url = f"https://raw.githubusercontent.com/{updates.REPOSITORY}/{new['sha']}/install.sh"
            self.sys.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    url,
                    "-o",
                    path,
                ],
                timeout=60,
            )
            result = self.outside(
                [
                    "bash",
                    path,
                    "--repo",
                    updates.REPOSITORY,
                    "--ref",
                    new["sha"],
                    "--upgrade",
                    "--app-update",
                ]
            )
        if result != 0:
            return self.show(self.t("app_update_failed"))
        installed = self.sys.run(["/usr/local/bin/mos-itc", "--version"]).strip()
        if installed == new["tag"].lstrip("v"):
            self.restart = True
            return True
        self.show(self.t("app_update_failed"))

    def php_settings(self):
        versions = core.php_versions(self.sys, True)
        i = self.menu("php_settings", versions + ["back"])
        if i is None or i == len(versions):
            return
        version = versions[i]
        i = self.menu("php_settings", ["PHP-FPM", "PHP CLI", "back"])
        if i in (None, 2):
            return
        sapi = ("fpm", "cli")[i]
        values = settings.php_values(self.sys, version, sapi)
        while True:
            keys = list(values)
            i = self.menu(
                "php_settings",
                aligned_rows([(k, values[k]) for k in keys])
                + ["save_settings", "php_advanced", "back"],
            )
            if i is None or i == len(keys) + 2:
                return
            if i < len(keys):
                values[keys[i]] = self.ask(keys[i], values[keys[i]])
            elif i == len(keys):
                if self.confirm("php_warning"):
                    self.perform(
                        lambda: settings.php_save(self.sys, version, sapi, values)
                    )
            else:
                which = self.menu(
                    "php_advanced", ["php.ini", "99-mos-production.ini", "back"]
                )
                if which in (None, 2):
                    continue
                path = settings.php_path(self.sys, version, sapi)
                if which == 0:
                    path = path.parent.parent / "php.ini"
                content = self.editor("php_advanced", self.sys.read(path))
                if self.confirm("php_warning"):
                    self.perform(
                        lambda: settings.php_write(
                            self.sys, version, sapi, content, main=which == 0
                        )
                    )
                values = settings.php_values(self.sys, version, sapi)

    def ssl_schedule(self):
        self.show(self.t("ssl_schedule_help"), "ssl_schedule")
        value = self.ask(
            "time_hh_mm_server_timezone", self.sys.data().get("renewal_time", "03:00")
        )
        if self.confirm("save_settings"):
            settings.renewal(self.sys, value)

    def firewall_menu(self):
        while True:
            output = self.sys.run(["ufw", "status", "numbered"], check=False)
            choice = self.menu(
                "firewall_rules",
                ["firewall_view", "firewall_add", "firewall_delete", "back"],
                details={
                    k: output
                    for k in ("firewall_view", "firewall_add", "firewall_delete")
                },
            )
            if choice in (None, 3):
                return
            if choice == 0:
                self.show(output, "firewall_rules")
            elif choice == 1:
                action = self.menu("firewall_add", ["allow", "deny", "limit", "back"])
                if action in (None, 3):
                    continue
                port = self.ask("port")
                proto = self.menu("protocol", ["TCP", "UDP", "back"])
                if proto in (None, 2):
                    continue
                source = self.ask("source_network", "any")
                if self.confirm(
                    "firewall_confirm",
                    details=f"{['allow','deny','limit'][action]} {source} → {port}/{['tcp','udp'][proto]}",
                ):
                    self.perform(
                        lambda: settings.firewall(
                            self.sys,
                            ["allow", "deny", "limit"][action],
                            port,
                            ["tcp", "udp"][proto],
                            source,
                        )
                    )
            else:
                number = int(self.ask("firewall_rule_number"))
                self.show(self.t("firewall_ssh_protection"))
                line = next(
                    (
                        line
                        for line in output.splitlines()
                        if re.match(r"^\[\s*" + str(number) + r"\]", line)
                    ),
                    None,
                )
                if not line:
                    raise Error("settings_invalid")
                # Ambiguous app profiles and ranges may cover SSH: refuse those.
                target = re.sub(r"^\[\s*\d+\]\s*", "", line).split()[0]
                if not re.fullmatch(r"\d+(?:/(?:tcp|udp))?", target) or target.split(
                    "/"
                )[0] in ops.ssh_ports(self.sys):
                    raise Error("firewall_ssh_protected")
                if self.confirm("firewall_confirm", details=line):
                    self.perform(
                        lambda: self.sys.run(
                            ["ufw", "--force", "delete", str(number)], mutate=True
                        )
                    )

    def fail2ban_menu(self):
        choice = self.menu(
            "fail2ban_settings",
            ["fail2ban_report", "fail2ban_edit", "fail2ban_advanced", "back"],
        )
        if choice in (None, 3):
            return
        if choice == 0:
            return self.show(self.sys.run(["fail2ban-client", "status"], check=False))
        if choice == 1:
            jail = self.ask("fail2ban_jail", "sshd")
            retry = int(self.ask("fail2ban_retries", "5"))
            find = self.ask("fail2ban_findtime", "10m")
            ban = self.ask("fail2ban_bantime", "1h")
            enabled = self.yes("fail2ban_enabled")
            if self.confirm("save_settings"):
                self.perform(
                    lambda: settings.fail2ban(self.sys, jail, retry, find, ban, enabled)
                )
        else:
            path = Path("/etc/fail2ban/jail.d/90-mos-custom.local")
            content = self.editor(
                "fail2ban_advanced", self.sys.read(path, "# MOS custom jails\n")
            )
            if self.confirm("save_settings"):

                def apply():
                    with self.sys.transaction([path], [["fail2ban-client", "-t"]]):
                        self.sys.write(path, content, 0o644)
                    self.sys.run(["systemctl", "restart", "fail2ban"], mutate=True)

                self.perform(apply)

    def advanced_settings(self):
        return self.section(
            "advanced_settings",
            [
                ("ui_restore_points", self.restore_points),
                ("apt_sources", self.apt_sources),
                (
                    "operation_history",
                    lambda: self.show(
                        self.sys.read(
                            STATE / "operations.jsonl", self.t("no_operations_yet")
                        )[-16000:]
                    ),
                ),
                ("ui_session_log", lambda: self.show("\n".join(self.activity))),
                ("uninstall_mos_itc", self.uninstall),
                ("rick", self.rick),
            ],
        )

    def rick(self):
        # Display the response as terminal art. Never execute downloaded content.
        self.outside(
            [
                "curl",
                "--no-progress-meter",
                "--max-time",
                "90",
                "https://ascii.live/rick",
            ]
        )

    def student_dependencies(self):
        required = ["acl", "sudo", "openssh-server"]
        packages = [p for p in required if not core.installed(self.sys, p)]
        if packages:
            core.refresh(self.sys)
            args, plan = core.apt_plan(self.sys, packages)
            if not self.confirm("student_dependencies", details=plan):
                raise Cancelled
            core.apply_packages(self.sys, args)

    def create_student(self, site=None):
        self.show(self.t("student_help"), "student_create")
        username = core.name(self.ask("linux_user"))
        workspace = None
        if site:
            workspace = SITES / site["domain"]
        else:
            sites = list(self.sys.data()["sites"].values())
            choice = self.menu(
                "student_workspace",
                ["student_personal"] + [s["domain"] for s in sites] + ["back"],
            )
            if choice is None or choice == len(sites) + 1:
                return
            if choice:
                workspace = SITES / sites[choice - 1]["domain"]
        allowed = users.password_login_allowed(self.sys)
        password = (
            self.administrator_password()
            if allowed and self.yes("users_set_password")
            else ""
        )
        key = (
            self.ask("public_key") if not password or self.yes("users_add_key") else ""
        )
        if self.confirm(
            "student_confirm",
            details=username + " → " + str(workspace or Path("/home") / username),
        ):
            self.student_dependencies()
            users.create_student(self.sys, username, password, workspace, key)
            self.show(self.t("student_created", username=username), "student_create")

    def site_student_access(self, site):
        action = self.menu(
            "website_ssh_sftp_access",
            ["student_create", "student_site_owner", "admin_users", "back"],
            details={
                "student_create": self.t("student_help"),
                "student_site_owner": self.t("student_owner_help"),
                "admin_users": self.t("users_ssh_settings_hint"),
            },
        )
        if action == 0:
            self.perform(lambda: self.create_student(site))
        elif action == 1:
            from . import workspaces

            keys = users.public_keys(self.sys, site["user"])
            for key in keys:
                ops.key_text(key)
            if self.confirm(
                "student_confirm",
                details=site["user"] + " → " + str(SITES / site["domain"]),
            ):

                def activate():
                    self.student_dependencies()
                    workspaces.assign(self.sys, site["user"], SITES / site["domain"])
                    for key in keys:
                        users.add_public_key(self.sys, site["user"], key)

                self.perform(activate)
        elif action == 2:
            self.admin_users()

    def reassign_student(self, account):
        from . import workspaces

        sites = list(self.sys.data()["sites"].values())
        choice = self.menu(
            "student_workspace",
            ["student_existing_home"] + [site["domain"] for site in sites] + ["back"],
        )
        if choice is None or choice == len(sites) + 1:
            return
        workspace = (
            Path(account["home"])
            if choice == 0
            else SITES / sites[choice - 1]["domain"]
        )
        keys = users.public_keys(self.sys, account["username"])
        for key in keys:
            ops.key_text(key)
        if self.confirm(
            "student_reassign_warning",
            details=account["username"] + " → " + str(workspace),
        ):

            def assign():
                self.student_dependencies()
                workspaces.assign(self.sys, account["username"], workspace)
                for key in keys:
                    users.add_public_key(self.sys, account["username"], key)

            self.perform(assign)

    def ssh_account_actions(self, account):
        username = account["username"]
        while True:
            student = self.sys.data().get("student_accounts", {}).get(username)
            options = ["users_keys", "add_public_ssh_key", "users_change_password"]
            if student is not None:
                options += [
                    "users_suspend" if student.get("enabled", True) else "users_enable",
                    "student_workspace",
                ]
            elif account["kind"] in ("user", "site"):
                options.append("users_assign_workspace")
            choice = self.menu(
                username,
                options + ["back"],
                details={"users_keys": self.t("users_ssh_settings_hint")},
            )
            if choice is None or choice == len(options):
                return
            if choice >= 3:
                if options[choice] in ("student_workspace", "users_assign_workspace"):
                    self.reassign_student(account)
                elif self.confirm("users_toggle_warning", username):
                    self.perform(
                        lambda: users.student_enabled(
                            self.sys, username, not student.get("enabled", True)
                        )
                    )
                continue
            if choice == 0:
                keys = users.public_keys(self.sys, username)
                labels = [
                    f"{i+1}. {key.split()[0]} … {key.split()[-1]}"
                    for i, key in enumerate(keys)
                ]
                index = self.menu("users_keys", labels + ["back"])
                if index is not None and index < len(keys):
                    action = self.menu(
                        "users_keys", ["cert_view_details", "delete", "back"]
                    )
                    if action == 0:
                        self.show(keys[index], "users_keys")
                    elif action == 1 and self.confirm(
                        "users_delete_key", details=keys[index]
                    ):
                        if len(keys) == 1 and not users.password_login_allowed(
                            self.sys
                        ):
                            raise Error("users_last_key")
                        self.perform(
                            lambda: users.save_public_keys(
                                self.sys, username, keys[:index] + keys[index + 1 :]
                            )
                        )
            elif choice == 1:
                key = self.ask("public_key")
                if self.confirm(self.t("add_public_ssh_key_for_user", user=username)):
                    self.perform(lambda: users.add_public_key(self.sys, username, key))
            else:
                if not users.password_login_allowed(self.sys):
                    self.show(self.t("users_password_ssh_disabled"))
                password = self.administrator_password()
                if self.confirm(self.t("users_confirm_password", username=username)):
                    self.perform(
                        lambda: users.change_password(self.sys, username, password)
                    )

    def main_loop(self):
        entries = [
            ("ui_overview", self.overview),
            ("websites", self.websites),
            ("db_manage", self.database_manager),
            ("ui_software", self.software),
            ("admin_users", self.admin_users),
            ("ui_security_access", self.security_center),
            ("backups", self.backups),
            ("view_performance", self.performance),
            ("make_a_speed_test", self.speed),
            ("ui_settings", self.settings),
            ("about", self.about),
            ("exit", None),
        ]
        while True:
            try:
                choice = self.menu("main_menu", [key for key, _ in entries])
                if choice in (None, len(entries) - 1):
                    return
                action = entries[choice][1]
                if action in (
                    self.software,
                    self.security_center,
                    self.settings,
                    self.websites,
                    self.database_manager,
                    self.admin_users,
                    self.backups,
                    self.performance,
                ):
                    if action():
                        return
                else:
                    self.perform(action)
            except (Cancelled, KeyboardInterrupt):
                self.notify("cancelled")
            except (Error, ValueError, OSError) as exc:
                self.show(
                    exc.render(self.lang) if isinstance(exc, Error) else str(exc),
                    "error",
                )


def bar(percent, width=25):
    percent = max(0.0, min(100.0, float(percent)))
    count = round(width * percent / 100)
    return "[" + "█" * count + "░" * (width - count) + f"] {percent:5.1f}%"


def speed_card(result, language="en"):
    translate = Translator(language)
    down, up = translate("download").upper(), translate("upload").upper()
    lines = [
        "MOS ITC · " + translate("speedtest_title"),
        "",
        f'{down:<12} {result["download_mbps"]:>10.2f} Mbps',
        f'{up:<12} {result["upload_mbps"]:>10.2f} Mbps',
        f'{"PING":<12} {result["ping_ms"]:>10.2f} ms',
        "",
        "ISP: " + clean(result["isp"]),
        translate("server")
        + ": "
        + clean(result["server"])
        + ", "
        + clean(result["country"]),
    ]
    color = python_sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    width = min(68, max(40, shutil.get_terminal_size().columns - 4))
    border = "+" + "-" * (width + 2) + "+"
    print(("\033[38;2;17;117;51m" if color else "") + border)
    for line in lines:
        print("| " + line[:width].ljust(width) + " |")
    print(border + ("\033[0m" if color else ""))
