"""Dependency-free curses menus; regular prompts keep editors and SSH usable."""

import curses
import getpass
import contextlib
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

from . import core, web, ops, recovery, users
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
            return self.terminal.document(title, self.t(str(text)), artwork=artwork)
        print(clean(self.t(str(text))))

    def perform(self, action, allow_pending=False):
        self.operation_cancelled = False
        try:
            with core.lock(self.sys.dry):
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
            self.show(
                self.sys.run(
                    [
                        "dpkg-query",
                        "-W",
                        "-f=${binary:Package}\t${Version}\t${Status}\n",
                    ]
                )
            )
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
            rows = ["create_website"] + [
                f"{host}  ·  "
                + (sites[host].get("php") or self.t("static_website"))
                + "  ·  "
                + sites[host]["ssl"]
                for host in hosts
            ]
            rows += ["import_existing_static_php_website_beta", "back"]
            choice = self.menu("websites", rows)
            if choice is None or choice == len(rows) - 1:
                return
            if choice == 0:
                self.perform(self.create_website)
            elif choice == len(rows) - 2:
                self.perform(self.import_website)
            else:
                self.website_actions(hosts[choice - 1])

    def site_summary(self, site):
        pairs = [
            ("domain", site["domain"]),
            ("php_version", site.get("php") or self.t("static_website")),
            ("document_root", site["root"]),
            ("ssl_tls_mode", site["ssl"]),
            ("linux_user", site["user"]),
            ("timezone", site.get("timezone", "UTC")),
            ("ui_aliases", ", ".join(site.get("aliases", [])) or "—"),
        ]
        return "\n".join(f"{self.t(label)}: {value}" for label, value in pairs)

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
            rows = [
                "ui_site_details",
                "edit_website",
                "configure_website_ssl",
                "git_deployment",
                "website_ssh_sftp_access",
                "ui_create_database",
                "delete_website",
                "back",
            ]
            choice = self.menu(host, rows)
            if choice in (None, 7):
                return
            actions = [
                lambda: self.show(self.site_summary(site), host),
                lambda: self.edit_site(site),
                lambda: self.configure_ssl(site),
                lambda: self.deploy_site(site),
                lambda: self.access_site(site),
                lambda: self.database_site(site),
                lambda: self.remove_site(site),
            ]
            self.perform(actions[choice])

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
            self.show(
                self.t("ui_database_exists", database=site["database"]), site["domain"]
            )
            return
        if self.confirm("create_a_mariadb_database"):
            self.notify(
                self.t(
                    "database_credentials_saved",
                    path=web.create_database(self.sys, site),
                )
            )

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
            i = self.menu("select_certificate", labels)
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
                ("import_replace_custom_certificate", lambda: self.ssl_action(1)),
                ("configure_website_ssl", lambda: self.ssl_action(2)),
                ("renew_let_s_encrypt_certificates", lambda: self.ssl_action(3)),
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
                ", ".join(site["domain"] for site in sites) or self.t("cert_unused"),
            ),
        ]
        return "\n".join(f"{self.t(key)}: {value}" for key, value in fields)

    def certificate_inventory(self):
        while True:
            labels = sorted(self.sys.data()["certificates"])
            inventory = {}
            certificate_rows = []
            for label in labels:
                cert_path = CONF / "certificates" / label / "cert.pem"
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
                    self.t("cert_inventory_row", name=label, expiry=expiry)
                )
            rows = ["cert_domain_security"] + certificate_rows + ["back"]
            choice = self.menu("certificate_inventory", rows)
            if choice is None or choice == len(rows) - 1:
                return
            if choice == 0:
                lines = [
                    f'{site["domain"]}  |  {site["ssl"]}  |  {site.get("certificate") or "—"}'
                    for site in self.sys.data()["sites"].values()
                ]
                self.show(
                    "\n".join(lines) or self.t("no_websites_yet"),
                    "cert_domain_security",
                )
                continue
            label = labels[choice - 1]
            info = inventory[label]
            days = info.get("days_left")
            summary = self.certificate_summary(label, info)
            if days is not None and days <= 30:
                summary = (
                    self.t("certificate_expiry_warning", name=label, days=days)
                    + "\n\n"
                    + summary
                )
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
            self.certificate_import_form(core.name(self.ask("certificate_name")))
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
                ("firewall_rules", lambda: self.security_action(5)),
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
                ("live_metrics", lambda: self.performance_action(0)),
                ("hourly_timeline_7_days", lambda: self.performance_action(1)),
                ("active_services", lambda: self.performance_action(2)),
                ("firewall_rules", lambda: self.performance_action(3)),
                ("clamav_report", lambda: self.performance_action(4)),
                ("fail2ban_report", lambda: self.performance_action(5)),
            ],
        )

    def performance_action(self, choice):
        if choice == 0:
            if self.terminal:
                self.terminal.monitor(ops.metrics)
            else:
                self.show(json.dumps(ops.metrics(), indent=2))
        elif choice == 1:
            rows = json.loads(self.sys.read(STATE / "metrics.json", "[]"))
            self.show(
                "\n".join(
                    f'{r["time"][:16]} UTC | CPU {bar(r["cpu"], 10)} RAM {100*r["ram_used"]/r["ram_total"]:5.1f}% DISK {100*r["disk_used"]/r["disk_total"]:5.1f}%'
                    for r in rows
                )
                or self.t("metrics_no_samples")
            )
        elif choice == 2:
            self.show(
                self.sys.run(
                    [
                        "systemctl",
                        "list-units",
                        "--type=service",
                        "--state=running",
                        "--no-pager",
                    ],
                    check=False,
                )
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
        return self.section(
            "backups",
            [
                ("configure_repository", lambda: self.backups_action(0)),
                ("edit_rclone_configuration_in_nano", lambda: self.backups_action(1)),
                ("initialize_restic_repository", lambda: self.backups_action(2)),
                ("back_up_now", lambda: self.backups_action(3)),
                ("list_snapshots", lambda: self.backups_action(4)),
                ("check_repository", lambda: self.backups_action(5)),
                ("schedule_daily_backup", lambda: self.backups_action(6)),
                ("restore_latest_to_new_directory", lambda: self.backups_action(7)),
            ],
        )

    def backups_action(self, choice):
        if choice == 0:
            repo = self.ask("restic_repository", "rclone:remote:mos-itc")
            password = self.ask("restic_password", secret=True)
            if password != self.ask("repeat_password", secret=True):
                raise Error("passwords_do_not_match")
            s3 = {}
            if repo.startswith("s3:"):
                s3 = {
                    k: self.ask(k, secret="SECRET" in k)
                    for k in (
                        "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY",
                        "AWS_DEFAULT_REGION",
                    )
                }
            if self.confirm("confirm_backup_config"):
                ops.configure_backup(self.sys, repo, password, s3)
        elif choice == 1:
            content = self.editor(
                "rclone_configuration", self.sys.read(CONF / "rclone.conf")
            )
            if self.confirm("save_rclone_configuration"):
                self.sys.write(CONF / "rclone.conf", content)
        elif choice in (2, 3, 4, 5):
            action = {2: "init", 3: "backup", 4: "snapshots", 5: "check"}[choice]
            if self.confirm(self.t("restic_action_action", action=action)):
                self.show(ops.backup(self.sys, action))
        elif choice == 6:
            ops.backup_env(self.sys)
            if self.confirm("confirm_backup_schedule"):
                ops.install_timer(
                    self.sys,
                    "backup",
                    "*-*-* 02:30:00",
                    "/usr/local/bin/mos-itc backup run",
                    "remote backup",
                )
        elif choice == 7:
            target = self.ask("restore_directory_must_not_exist", "/srv/mos-restore")
            if self.confirm(self.t("restore_latest_snapshot_to_path", path=target)):
                self.show(ops.backup(self.sys, "restore", target))

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
            if action in (self.ssh, self.security, self.ssl):
                action()
            else:
                self.perform(action, allow_pending=title == "ssh_configuration")

    def software(self):
        self.section(
            "ui_software",
            [
                ("install_web_server", self.install_web),
                ("manage_package_selection", self.package_selection),
                ("ui_package_updates", self.package_updates),
                (
                    "all_installed_debian_packages",
                    lambda: self.show(
                        self.sys.run(
                            [
                                "dpkg-query",
                                "-W",
                                "-f=${binary:Package}\t${Version}\t${Status}\n",
                            ]
                        )
                    ),
                ),
                ("refresh_apt_indexes", lambda: core.refresh(self.sys)),
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
            self.notify("ui_no_updates")

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
            accounts = users.administrators(self.sys)
            choice = self.menu(
                "admin_users",
                ["users_create"] + [a["username"] for a in accounts] + ["back"],
            )
            if choice is None or choice == len(accounts) + 1:
                return
            if choice == 0:
                self.perform(self.create_administrator)
            else:
                self.perform(lambda: self.administrator_actions(accounts[choice - 1]))

    def security_center(self):
        self.section(
            "ui_security_access",
            [
                ("ssh_configuration", self.ssh),
                ("manage_security", self.security),
                ("ssl_certificates", self.ssl),
            ],
        )

    def settings(self):
        while True:
            choice = self.menu(
                "ui_settings",
                [
                    "automatic_configuration",
                    "language",
                    "operation_history",
                    "ui_session_log",
                    "ui_restore_points",
                    "uninstall_mos_itc",
                    "back",
                ],
            )
            if choice in (None, 6):
                return
            if choice == 0:
                self.perform(self.configure)
            elif choice == 1:
                self.perform(self.change_language)
            elif choice == 2:
                self.show(
                    self.sys.read(
                        STATE / "operations.jsonl", self.t("no_operations_yet")
                    )[-16000:],
                    "operation_history",
                )
            elif choice == 3:
                self.show("\n".join(self.activity), "ui_session_log")
            elif choice == 4:
                self.restore_points()
            elif choice == 5 and self.perform(self.uninstall):
                return True

    def restore_points(self):
        points = recovery.list_points()
        if not points:
            self.show(self.t("ui_no_restore_points"))
            return
        choice = self.menu("ui_restore_points", [p["name"] for p in points] + ["back"])
        if choice is None or choice == len(points):
            return
        point = points[choice]["name"]
        target = self.ask(
            "restore_directory_must_not_exist", "/srv/mos-recovery-" + point
        )
        if self.confirm(self.t("ui_extract_restore_point", name=point, target=target)):
            self.perform(
                lambda: self.notify(str(recovery.extract_point(point, target)))
            )

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
        data = self.sys.data()
        text = self.t(
            "ui_overview_text",
            websites=len(data["sites"]),
            certificates=len(data["certificates"]),
            hostname=data.get("hostname", os.uname().nodename),
            timezone=data.get("timezone", "UTC"),
        )
        self.show(text, "ui_overview")

    def main(self):
        return self.session(self.main_loop)

    def main_loop(self):
        entries = [
            ("ui_overview", self.overview),
            ("websites", self.websites),
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
    return "[" + "#" * count + "-" * (width - count) + f"] {percent:5.1f}%"


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
