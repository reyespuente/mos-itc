"""Private export files and expiring, signed single-file download links."""

import hashlib
import contextlib
import fcntl
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

from . import core, network, web
from .core import STATE, CONF, Error

EXPORTS = STATE / "exports"
REGISTRY = STATE / "downloads.json"
SERVICE = "mos-downloads"
PREFIX = "/mos-downloads/"
APACHE_ROUTE = Path("/etc/apache2/conf-enabled/mos-downloads.conf")


def records(system):
    return json.loads(system.read(REGISTRY, "{}"))


def cleanup(system, wait=10):
    if system.dry:
        return
    with core.lock(path=STATE / "downloads.lock", wait=wait):
        data = records(system)
        for identifier, item in list(data.items()):
            if item["expires"] <= time.time():
                path = EXPORTS / (identifier + ".bin")
                if path.is_file() or path.is_symlink():
                    path.unlink()
                del data[identifier]
        if EXPORTS.is_dir() and not EXPORTS.is_symlink():
            for path in EXPORTS.iterdir():
                if (
                    re.fullmatch(r"[a-f0-9]{32}\.bin", path.name)
                    and path.stem not in data
                ):
                    path.unlink(missing_ok=True)
                elif (
                    path.name.startswith("archive-")
                    and path.is_dir()
                    and not path.is_symlink()
                ):
                    marker = path / ".active"
                    if not marker.exists():
                        if time.time() - path.stat().st_mtime > 600:
                            shutil.rmtree(path)
                        continue
                    fd = os.open(marker, os.O_RDWR | os.O_NOFOLLOW)
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        shutil.rmtree(path)
                    finally:
                        os.close(fd)
        system.write(REGISTRY, json.dumps(data))


def remove(system, identifier):
    if not re.fullmatch("[a-f0-9]{32}", identifier):
        raise Error("settings_invalid")
    with core.lock(system.dry, path=STATE / "downloads.lock", wait=10):
        data = records(system)
        data.pop(identifier, None)
        system.delete(EXPORTS / (identifier + ".bin"))
        system.write(REGISTRY, json.dumps(data))


def register(system, source, filename=None):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise Error("download_invalid_file")
    if not any(
        source.resolve().is_relative_to(root.resolve())
        for root in (STATE / "database-dumps", EXPORTS)
    ):
        raise Error("download_invalid_file")
    identifier = secrets.token_hex(16)
    ttl = int(system.data().get("download_minutes", 60))
    if not 1 <= ttl <= 1440:
        raise Error("settings_invalid")
    system.mkdir(EXPORTS, 0o700)
    target = EXPORTS / (identifier + ".bin")
    with core.lock(path=STATE / "downloads.lock", wait=10):
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as src, target.open("xb") as dest:
                shutil.copyfileobj(src, dest)
            target.chmod(0o600)
            item = {
                "name": Path(filename or source.name).name,
                "expires": int(time.time()) + 60 * ttl,
                "size": target.stat().st_size,
                "source": str(source),
                "published": False,
            }
            data = records(system)
            data[identifier] = item
            system.write(REGISTRY, json.dumps(data))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    from .ops import install_timer

    install_timer(
        system,
        "download-cleanup",
        "*:0/10",
        "/usr/local/bin/mos-itc internal download-cleanup",
        "expired download cleanup",
    )
    return identifier


def archive(system, paths, kind, databases=False):
    from .backups import validate_sources, database_exports

    paths = validate_sources(paths, databases)
    if kind not in ("zip", "tar.gz"):
        raise Error("settings_invalid")
    system.mkdir(EXPORTS, 0o700)
    with tempfile.TemporaryDirectory(
        prefix="archive-", dir=EXPORTS
    ) as tmp, contextlib.ExitStack() as stack:
        root = Path(tmp)
        active = stack.enter_context((root / ".active").open("w"))
        fcntl.flock(active, fcntl.LOCK_EX)
        sources = [Path(p) for p in paths]
        if databases:
            sources += [Path(p) for p in database_exports(system, root / "SQL")]
        if not sources or any(not p.exists() for p in sources):
            raise Error("backup_source_missing")
        output = root / ("backup." + kind)
        excluded = (
            EXPORTS.resolve(),
            (STATE / "restic-cache").resolve(),
            (STATE / "backup-staging").resolve(),
        )

        def omit(path):
            return any(
                path.resolve().is_relative_to(p) for p in excluded
            ) and not path.is_relative_to(root / "SQL")

        if kind == "tar.gz":
            with tarfile.open(output, "w:gz") as tar:

                def keep(info):
                    return None if omit(Path("/") / info.name) else info

                for source in sources:
                    tar.add(source, arcname=str(source).lstrip("/"), filter=keep)
        else:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for source in sources:
                    for path in [source] if source.is_file() else source.rglob("*"):
                        if not omit(path) and not path.is_symlink() and path.is_file():
                            z.write(path, str(path).lstrip("/"))
        return register(system, output, "mos-backup-" + core.stamp() + "." + kind)


def secret(system):
    path = CONF / "download-signing-key"
    with core.lock(system.dry, path=STATE / "downloads.lock", wait=10):
        if not system.exists(path):
            system.write(path, secrets.token_hex(32))
        return bytes.fromhex(system.read(path).strip())


def signature(key, identifier, expires):
    return hmac.new(key, f"{identifier}/{expires}".encode(), hashlib.sha256).hexdigest()


def start(system, site=None):
    if not system.exists("/etc/apache2/apache2.conf"):
        raise Error("download_apache_needed")
    if (
        not system.dry
        and system.run(["systemctl", "is-active", "apache2"], check=False).strip()
        != "active"
    ):
        raise Error("download_apache_needed")
    config = {"tls": False, "proxy": True}
    if site:
        cert, key = web.tls_paths(system, site)
        config.update(
            tls=True,
            cert=str(cert),
            key=str(key),
            host=site["domain"],
            cert_digest=hashlib.sha256(cert.read_bytes()).hexdigest(),
        )
    else:
        info = network.inventory()
        config["host"] = (
            network.public(system.data().get("public_ip", ""))
            or info["public"]
            or network.ec2_public()
        )
        if not config["host"]:
            raise Error("download_public_ip_needed")
    secret(system)
    config["bind"] = "127.0.0.1"
    previous = json.loads(system.read(STATE / "download-server.json", "{}"))
    if previous:
        same = all(previous.get(k) == v for k, v in config.items())
        same_endpoint = (
            previous.get("host") == config["host"]
            and previous.get("tls") == config["tls"]
            and previous.get("proxy", False) == config["proxy"]
        )
        active = (
            system.run(
                ["systemctl", "is-active", "mos-downloads.service"], check=False
            ).strip()
            == "active"
        )
        if same and active:
            apache_route(system, previous)
            return previous
        if not same_endpoint and any(
            item["expires"] > time.time() and item.get("published", True)
            for item in records(system).values()
        ):
            raise Error("download_transport_in_use")
        if same_endpoint:
            config["port"] = previous.get("port", 0)
    # The daemon chooses a free port itself and records the actual bound socket.
    system.write(CONF / "download-server.json", json.dumps(config))
    unit = """[Unit]
Description=MOS ITC temporary downloads
After=network.target
[Service]
Type=simple
ExecStart=/usr/local/bin/mos-itc internal downloads
Restart=on-failure
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/var/lib/mos-itc
[Install]
WantedBy=multi-user.target
"""
    system.write("/etc/systemd/system/mos-downloads.service", unit, 0o644)
    system.run(["systemctl", "daemon-reload"], mutate=True)
    system.delete(STATE / "download-server.json")
    system.run(["systemctl", "restart", "mos-downloads.service"], mutate=True)
    for _ in range(50):
        if system.exists(STATE / "download-server.json"):
            running = json.loads(system.read(STATE / "download-server.json"))
            apache_route(system, running)
            return running
        time.sleep(0.1)
    raise Error("download_start_failed")


def apache_route(system, server):
    """Share Apache's listeners; only the signed-download prefix is proxied."""
    if not system.exists("/etc/apache2/apache2.conf"):
        raise Error("download_apache_needed")
    port = int(server["port"])
    route = f"""# Managed by MOS ITC: private backend, existing HTTP/HTTPS listeners.
ProxyRequests Off
ProxyPass "{PREFIX}" "http://127.0.0.1:{port}/" connectiontimeout=5 timeout=60
<Location "{PREFIX}">
    Require all granted
</Location>
"""
    edits = {}
    # Existing MOS sites may redirect all HTTP requests to HTTPS. Exempt only
    # the reserved route, preserving the remainder of their configuration.
    for site in system.data()["sites"].values():
        host = core.domain(site["domain"])
        path = Path("/etc/apache2/sites-available") / ("mos-" + host + ".conf")
        content = system.read(path, "")
        rule = "  RewriteRule ^ https://" + host + "%{REQUEST_URI} [R=301,L,NE]"
        if (
            content.startswith("# Managed by MOS ITC")
            and rule in content
            and "!^/mos-downloads/" not in content
        ):
            edits[path] = content.replace(
                rule, "  RewriteCond %{REQUEST_URI} !^/mos-downloads/\n" + rule
            )
    if system.read(APACHE_ROUTE, "") == route and not edits:
        return
    system.run(["a2enmod", "proxy", "proxy_http"], mutate=True)
    with system.transaction([APACHE_ROUTE, *edits]):
        for path, content in edits.items():
            system.write(path, content, 0o644)
        system.write(APACHE_ROUTE, route, 0o644)
        system.run(["apache2ctl", "configtest"], mutate=True)
        system.run(["systemctl", "reload", "apache2"], mutate=True)


def link(system, identifier, server):
    with core.lock(path=STATE / "downloads.lock", wait=10):
        data = records(system)
        if identifier not in data or data[identifier]["expires"] <= time.time():
            raise Error("download_invalid_file")
        item = data[identifier]
        item["published"] = True
        system.write(REGISTRY, json.dumps(data))
    host = server["host"]
    host = "[" + host + "]" if ":" in host else host
    token = signature(secret(system), identifier, item["expires"])
    endpoint = host + ("" if server.get("proxy") else ":" + str(server["port"]))
    prefix = PREFIX if server.get("proxy") else "/"
    return f"{'https' if server['tls'] else 'http'}://{endpoint}{prefix}{identifier}/{item['expires']}/{token}"


def publish(system, identifier, site=None):
    with core.lock(system.dry, path=STATE / "download-server.lock", wait=10):
        server = start(system, site)
        return server, link(system, identifier, server)


def serve():
    system = core.System()
    config = json.loads(system.read(CONF / "download-server.json"))
    key = secret(system)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log bearer URLs.

        def do_GET(self):
            match = re.fullmatch(
                r"/([a-f0-9]{32})/([0-9]{10})/([a-f0-9]{64})", urlsplit(self.path).path
            )
            if not match:
                return self.send_error(404)
            identifier, expires, token = match.groups()
            item = records(system).get(identifier)
            if (
                not item
                or int(expires) != item["expires"]
                or int(expires) <= time.time()
                or not hmac.compare_digest(
                    token, signature(key, identifier, int(expires))
                )
            ):
                return self.send_error(404)
            try:
                fd = os.open(
                    EXPORTS / (identifier + ".bin"), os.O_RDONLY | os.O_NOFOLLOW
                )
                with os.fdopen(fd, "rb") as source:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header(
                        "Content-Length", str(os.fstat(source.fileno()).st_size)
                    )
                    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", item["name"])
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{safe}"'
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    shutil.copyfileobj(source, self.wfile)
            except (OSError, ConnectionError):
                pass

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

    context = None
    if config["tls"] and not config.get("proxy"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config["cert"], config["key"])

    class Server(http.server.HTTPServer):
        address_family = socket.AF_INET6 if ":" in config["bind"] else socket.AF_INET

        def server_bind(self):
            if self.address_family == socket.AF_INET6:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            try:
                if context:
                    connection = context.wrap_socket(connection, server_side=True)
                return connection, address
            except BaseException:
                connection.close()
                raise

    server = Server((config["bind"], config.get("port", 0)), Handler)
    server.timeout = 1
    config["port"] = server.server_port
    system.write(STATE / "download-server.json", json.dumps(config))
    last = 0
    try:
        while True:
            server.handle_request()
            if time.monotonic() - last > 30:
                try:
                    cleanup(system, wait=0)
                except Error as exc:
                    if exc.args[0] != "error_another_mos_operation_is_running":
                        raise
                last = time.monotonic()
    finally:
        server.server_close()
