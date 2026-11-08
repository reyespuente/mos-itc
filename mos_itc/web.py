"""Apache virtual hosts, per-site PHP-FPM pools and certificate inventory."""

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import tempfile

from .core import (
    CONF,
    STATE,
    SITES,
    Error,
    domain,
    name,
    safe_path,
    installed,
    php_versions,
    stamp,
    php_version,
)


def site_user(host):
    return "mos_" + hashlib.sha256(host.encode()).hexdigest()[:12]


def paths(site):
    out = [Path("/etc/apache2/sites-available") / ("mos-" + site["domain"] + ".conf")]
    if site.get("php"):
        out.append(Path(f'/etc/php/{site["php"]}/fpm/pool.d/mos-{site["domain"]}.conf'))
    return out


def validate_site(site):
    host = domain(site["domain"])
    if host != site["domain"]:
        raise Error("error_domain_must_be_normalized")
    name(site["user"])
    home = SITES / host
    root = safe_path(site["root"])
    if root != home and home not in root.parents:
        raise Error("Document root must stay inside this website home: " + str(home))
    if root.exists() and not root.resolve().is_relative_to(home.resolve()):
        raise Error("error_document_root_resolves_outside_the_website_home")
    for alias in site.get("aliases", []):
        domain(alias)
    if site.get("php"):
        php_version(site["php"])
    if site.get("ssl") not in ("none", "letsencrypt", "custom"):
        raise Error("error_unknown_tls_mode")
    return site


def pool(site):
    home = SITES / site["domain"]
    return f"""[mos-{site['domain']}]
user = {site['user']}
group = {site['user']}
listen = /run/php/mos-{site['domain']}.sock
listen.owner = www-data
listen.group = www-data
listen.mode = 0660
pm = ondemand
pm.max_children = {int(site.get('workers', 3))}
pm.process_idle_timeout = 15s
pm.max_requests = 500
clear_env = yes
security.limit_extensions = .php
php_admin_flag[display_errors] = off
php_admin_flag[log_errors] = on
php_admin_flag[expose_php] = off
php_admin_flag[allow_url_include] = off
php_admin_value[error_log] = {home}/logs/php-error.log
php_admin_value[open_basedir] = {home}:/usr/share/php:/etc/ssl/certs
php_admin_value[upload_tmp_dir] = {home}/tmp
php_admin_value[sys_temp_dir] = {home}/tmp
php_admin_value[session.save_path] = {home}/sessions
php_admin_value[memory_limit] = 128M
php_admin_value[upload_max_filesize] = 32M
php_admin_value[post_max_size] = 40M
php_admin_value[max_execution_time] = 60
php_admin_flag[session.cookie_httponly] = on
php_admin_value[session.cookie_samesite] = Lax
php_admin_value[date.timezone] = {site.get('timezone', 'UTC')}
"""


def tls_paths(sys, site):
    if site["ssl"] == "letsencrypt":
        p = Path("/etc/letsencrypt/live") / site["domain"]
        return p / "fullchain.pem", p / "privkey.pem"
    cert = name(site.get("certificate", ""))
    if cert not in sys.data()["certificates"]:
        raise Error("error_certificate_not_found")
    p = CONF / "certificates" / cert
    return p / "fullchain.pem", p / "key.pem"


def verify_certificate(sys, cert, host=None, chain=None):
    args = ["openssl", "verify", "-purpose", "sslserver"]
    if host:
        args += ["-verify_hostname", domain(host)]
    if chain:
        args += ["-untrusted", str(chain)]
    return sys.run(args + [str(cert)])


def vhost(sys, site):
    validate_site(site)
    host, root = site["domain"], site["root"]
    alias = (
        "ServerAlias " + " ".join(site.get("aliases", []))
        if site.get("aliases")
        else ""
    )
    php = (
        f"""<FilesMatch "\\.php$">
    SetHandler "proxy:unix:/run/php/mos-{host}.sock|fcgi://localhost/"
  </FilesMatch>"""
        if site.get("php")
        else """<FilesMatch "\\.(php|phtml|phar)$">
    Require all denied
  </FilesMatch>"""
    )
    common = f"""  ServerName {host}
  {alias}
  DocumentRoot {root}
  DirectoryIndex {'index.php index.html' if site.get('php') else 'index.html'}
  ErrorLog ${{APACHE_LOG_DIR}}/mos-{host}-error.log
  CustomLog ${{APACHE_LOG_DIR}}/mos-{host}-access.log combined
  <Directory {root}>
    Options -Indexes -FollowSymLinks +SymLinksIfOwnerMatch
    AllowOverride {'FileInfo AuthConfig Limit Options=Indexes,MultiViews' if site.get('php') else 'None'}
    Require all granted
  </Directory>
  <FilesMatch "^\\.(?!well-known)">
    Require all denied
  </FilesMatch>
  <DirectoryMatch "/\\.(?!well-known(?:/|$))">
    Require all denied
  </DirectoryMatch>
  {php}
"""
    acme = f"""  Alias /.well-known/acme-challenge/ /var/lib/mos-itc/acme/{host}/.well-known/acme-challenge/
  <Directory /var/lib/mos-itc/acme/{host}/.well-known/acme-challenge>
    Options None
    AllowOverride None
    Require all granted
  </Directory>
"""
    http = f"<VirtualHost *:80>\n{common}{acme}</VirtualHost>\n"
    if site["ssl"] == "none":
        return "# Managed by MOS ITC\n" + http
    cert, key = tls_paths(sys, site)
    redirect = f"""<VirtualHost *:80>
  ServerName {host}
  {alias}
{acme}  RewriteEngine On
  RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
  RewriteRule ^ https://{host}%{{REQUEST_URI}} [R=301,L,NE]
</VirtualHost>
"""
    return f"""# Managed by MOS ITC
{redirect}<VirtualHost *:443>
{common}  SSLEngine on
  SSLCertificateFile {cert}
  SSLCertificateKeyFile {key}
  SSLProtocol -all +TLSv1.2 +TLSv1.3
</VirtualHost>
"""


def ensure_apache(sys):
    if not sys.dry and not installed(sys, "apache2"):
        raise Error("error_install_apache_first")
    sys.run(
        ["a2enmod", "proxy_fcgi", "setenvif", "rewrite", "ssl", "headers"], mutate=True
    )


def apply_site(sys, site, old=None):
    validate_site(site)
    occupied = {
        h
        for s in sys.data()["sites"].values()
        if s["domain"] != site["domain"]
        for h in [s["domain"]] + s.get("aliases", [])
    }
    if occupied.intersection([site["domain"]] + site.get("aliases", [])):
        raise Error("error_domain_or_alias_already_belongs_to_another_website")
    if site["ssl"] != "none":
        cert, key = tls_paths(sys, site)
        if not sys.dry:
            for host in [site["domain"]] + site.get("aliases", []):
                verify_certificate(sys, cert, host, cert)
            if not key.exists():
                raise Error("error_tls_private_key_not_found")
    if site.get("php") and not sys.dry and site["php"] not in php_versions(sys, True):
        raise Error("error_install_the_selected_php_fpm_version_first")
    ensure_apache(sys)
    edited = list(dict.fromkeys(paths(site) + (paths(old) if old else [])))
    validators = [["apache2ctl", "configtest"]]
    versions = {s["php"] for s in (site, old) if s and s.get("php")}
    validators += [[f"php-fpm{v}", "-t"] for v in sorted(versions)]
    try:
        with sys.transaction(edited):
            sys.write(edited[0], vhost(sys, site), 0o644)
            if site.get("php"):
                sys.write(paths(site)[1], pool(site), 0o644)
            for p in paths(old) if old else []:
                if p not in paths(site):
                    sys.delete(p)
            sys.run(["a2ensite", "mos-" + site["domain"]], mutate=True)
            for command in validators:
                sys.run(command, mutate=True)
    except BaseException:
        if not old:
            sys.run(["a2dissite", "mos-" + site["domain"]], mutate=True, check=False)
        raise
    for version in sorted(versions):
        sys.run(["systemctl", "reload-or-restart", f"php{version}-fpm"], mutate=True)
    sys.run(["systemctl", "reload-or-restart", "apache2"], mutate=True)
    data = sys.data()
    data["sites"][site["domain"]] = site
    sys.save(data)
    sys.audit("Website configuration: " + site["domain"])


def create_site(sys, host, version=None, www=False, laravel=False, timezone="UTC"):
    host = domain(host)
    data = sys.data()
    if host in data["sites"]:
        raise Error("error_website_already_managed_use_edit")
    aliases = ["www." + host] if www else []
    occupied = {
        h for s in data["sites"].values() for h in [s["domain"]] + s.get("aliases", [])
    }
    if occupied.intersection([host] + aliases):
        raise Error("error_domain_or_alias_already_assigned")
    # Existing vhosts require explicit adoption instead of an accidental duplicate.
    if not sys.dry:
        for p in Path("/etc/apache2/sites-available").glob("*.conf"):
            hosts = re.findall(r"^\s*Server(?:Name|Alias)\s+(.+)$", p.read_text(), re.M)
            if any(
                h in [x for line in hosts for x in line.split()]
                for h in [host] + aliases
            ):
                raise Error(
                    "Domain exists in " + str(p) + ". Use the beta import workflow."
                )
    user = site_user(host)
    home = SITES / host
    if home.exists() and not sys.dry:
        raise Error("error_site_directory_already_exists_refusing_to_adopt_data")
    if not sys.dry and sys.run(["getent", "passwd", user], check=False):
        raise Error("error_generated_linux_account_already_exists")
    site = {
        "domain": host,
        "user": user,
        "root": str(home / ("public" if laravel else "www")),
        "php": version,
        "ssl": "none",
        "aliases": aliases,
        "timezone": timezone,
        "created": stamp(),
        "laravel": laravel,
    }
    validate_site(site)
    ensure_apache(sys)
    sys.mkdir("/srv/mos", 0o755)
    sys.mkdir(SITES, 0o711)
    sys.run(
        [
            "useradd",
            "--create-home",
            "--home-dir",
            home,
            "--shell",
            "/usr/sbin/nologin",
            user,
        ],
        mutate=True,
    )
    sys.run(["chmod", "0750", home], mutate=True)
    for folder in [
        site["root"],
        str(home / "logs"),
        str(home / "tmp"),
        str(home / "sessions"),
    ]:
        sys.mkdir(folder, 0o750)
        sys.run(["chown", f"{user}:{user}", folder], mutate=True)
    sys.run(["setfacl", "-m", "u:www-data:--x", home], mutate=True)
    sys.run(["setfacl", "-R", "-m", "u:www-data:r-X", site["root"]], mutate=True)
    sys.run(["setfacl", "-m", "d:u:www-data:r-X", site["root"]], mutate=True)
    acme = Path("/var/lib/mos-itc/acme") / host / ".well-known/acme-challenge"
    sys.mkdir(acme, 0o755)
    for folder in [STATE, STATE / "acme", STATE / "acme" / host, acme.parent, acme]:
        sys.run(["setfacl", "-m", "u:www-data:--x", folder], mutate=True)
    sys.run(["setfacl", "-m", "u:www-data:r-x", acme], mutate=True)
    # Static placeholder: no phpinfo() or exposed system information.
    index = Path(site["root"]) / "index.html"
    sys.write(
        index,
        f"<!doctype html><title>{host}</title><h1>{host}</h1><p>Ready for deployment · MOS ITC</p>\n",
        0o644,
    )
    sys.run(["chown", f"{user}:{user}", index], mutate=True)
    apply_site(sys, site)
    return site


def create_database(sys, site):
    db = site["user"]
    password = secrets.token_urlsafe(32)
    sql = (
        f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
        f"CREATE USER '{db}'@'localhost' IDENTIFIED BY '{password}';\n"
        f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{db}'@'localhost';\n"
    )
    sys.run(["mariadb", "--protocol=socket"], input=sql, mutate=True)
    secret = SITES / site["domain"] / ".database.json"
    sys.write(
        secret,
        json.dumps(
            {"database": db, "username": db, "password": password, "host": "localhost"},
            indent=2,
        ),
    )
    sys.run(["chown", f'{site["user"]}:{site["user"]}', secret], mutate=True)
    data = sys.data()
    data["sites"][site["domain"]]["database"] = db
    sys.save(data)
    return str(secret)


def letsencrypt(sys, site, email):
    if not re.fullmatch(
        r"[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email
    ):
        raise Error("error_invalid_email_address")
    # The live HTTP vhost must exist before HTTP-01 validation.
    args = [
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        str(STATE / "acme" / site["domain"]),
        "--cert-name",
        site["domain"],
        "--non-interactive",
        "--agree-tos",
        "--email",
        email,
    ]
    for host in [site["domain"]] + site.get("aliases", []):
        args += ["-d", host]
    sys.run(args, mutate=True, timeout=900)
    updated = {**site, "ssl": "letsencrypt", "certificate": ""}
    apply_site(sys, updated, site)
    hook = Path("/etc/letsencrypt/renewal-hooks/deploy/mos-apache")
    sys.write(
        hook,
        "#!/bin/sh\nset -eu\n/usr/sbin/apache2ctl configtest\n/bin/systemctl reload apache2\n",
        0o700,
    )
    sys.run(["systemctl", "enable", "--now", "certbot.timer"], mutate=True)


def import_certificate(sys, label, cert_text, key_text, chain_text=""):
    label = name(label)
    # Never log/persist an invalid private key. Verification uses private temp files.
    if sys.dry:
        print("  [PREVIEW] validate certificate, key, chain and permissions")
        metadata = {"subject": "PREVIEW", "expires": "PREVIEW"}
    else:
        with tempfile.TemporaryDirectory(prefix="mos-cert-") as tmp:
            p = Path(tmp)
            for file, text in [
                ("cert.pem", cert_text),
                ("key.pem", key_text),
                ("chain.pem", chain_text),
            ]:
                sys.write(p / file, text)
            sys.run(
                [
                    "openssl",
                    "pkey",
                    "-in",
                    p / "key.pem",
                    "-passin",
                    "pass:",
                    "-check",
                    "-noout",
                ]
            )
            cert_pub = sys.run(
                ["openssl", "x509", "-in", p / "cert.pem", "-pubkey", "-noout"]
            )
            key_pub = sys.run(
                ["openssl", "pkey", "-in", p / "key.pem", "-passin", "pass:", "-pubout"]
            )
            if cert_pub != key_pub:
                raise Error("error_certificate_and_private_key_do_not_match")
            sys.run(
                ["openssl", "x509", "-in", p / "cert.pem", "-checkend", "0", "-noout"]
            )
            for site in sys.data()["sites"].values():
                if site.get("ssl") == "custom" and site.get("certificate") == label:
                    for host in [site["domain"]] + site.get("aliases", []):
                        verify_certificate(
                            sys,
                            p / "cert.pem",
                            host,
                            p / "chain.pem" if chain_text.strip() else None,
                        )
            verify_certificate(
                sys,
                p / "cert.pem",
                chain=p / "chain.pem" if chain_text.strip() else None,
            )
            metadata = certificate_info(sys, p / "cert.pem")
    target = CONF / "certificates" / label
    sys.mkdir(target, 0o700)
    with sys.transaction(
        [target / x for x in ("cert.pem", "key.pem", "fullchain.pem")]
    ):
        sys.write(target / "cert.pem", cert_text, 0o644)
        sys.write(target / "key.pem", key_text)
        sys.write(
            target / "fullchain.pem",
            cert_text.rstrip() + "\n" + chain_text.strip() + "\n",
            0o644,
        )
        if not sys.dry and installed(sys, "apache2"):
            sys.run(["apache2ctl", "configtest"], mutate=True)
    data = sys.data()
    data["certificates"][label] = metadata
    sys.save(data)
    if installed(sys, "apache2"):
        sys.run(["systemctl", "reload", "apache2"], mutate=True)
    sys.audit("Custom certificate imported: " + label)


def certificate_info(sys, path):
    output = sys.run(["openssl", "x509", "-in", path, "-noout", "-subject", "-enddate"])
    expires = next(
        (x.split("=", 1)[1] for x in output.splitlines() if x.startswith("notAfter=")),
        "",
    )
    try:
        date = dt.datetime.strptime(expires, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=dt.timezone.utc
        )
        days = (date - dt.datetime.now(dt.timezone.utc)).days
    except ValueError:
        days = None
    return {
        "subject": output.splitlines()[0] if output else "",
        "expires": expires,
        "days_left": days,
    }


def delete_site(sys, site, erase=False):
    host = domain(site["domain"])
    sys.run(["a2dissite", "mos-" + host], mutate=True)
    try:
        with sys.transaction(paths(site), [["apache2ctl", "configtest"]]):
            for p in paths(site):
                sys.delete(p)
    except BaseException:
        sys.run(["a2ensite", "mos-" + host], mutate=True)
        raise
    sys.run(["systemctl", "reload", "apache2"], mutate=True)
    if site.get("php"):
        sys.run(
            ["systemctl", "reload-or-restart", "php" + site["php"] + "-fpm"],
            mutate=True,
        )
    # Login credentials are locked even when data is preserved.
    sys.run(
        ["usermod", "--shell", "/usr/sbin/nologin", "--lock", site["user"]], mutate=True
    )
    if erase:
        home = SITES / host
        if home.is_symlink():
            raise Error("error_refusing_to_erase_a_symlink")
        if not sys.dry:
            shutil.rmtree(home)
        if site.get("database"):
            db = name(site["database"])
            sys.run(
                ["mariadb", "--protocol=socket"],
                input=f"DROP DATABASE `{db}`; DROP USER IF EXISTS '{db}'@'localhost';",
                mutate=True,
            )
        sys.run(["userdel", site["user"]], mutate=True)
    data = sys.data()
    del data["sites"][host]
    sys.save(data)
    sys.audit(
        "Website removed: "
        + host
        + (" including data" if erase else " (data retained)")
    )


def discover(sys):
    """Conservative beta inventory; never silently rewrite pre-existing vhosts."""
    managed = {str(paths(s)[0]) for s in sys.data()["sites"].values()}
    return [
        {"file": str(p), "config": sys.read(p)}
        for p in sorted(Path("/etc/apache2/sites-available").glob("*.conf"))
        if str(p) not in managed
    ]
