"""Small system boundary: argv-only execution, protected writes and APT plans."""

import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

from . import __version__ as VERSION

AUTHOR = "Reyes Guadalupe Puente Balcazar"
STATE = Path("/var/lib/mos-itc")
CONF = Path("/etc/mos-itc")
SITES = Path("/srv/mos/sites")
SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C.UTF-8",
    "HOME": "/root" if os.geteuid() == 0 else os.path.expanduser("~"),
    "TERM": os.environ.get("TERM", "xterm"),
}


class Error(Exception):
    def render(self, language="en"):
        from .i18n import Translator

        return Translator(language)(super().__str__())

    def __str__(self):
        return self.render()


def domain(value):
    value = value.strip().rstrip(".").lower()
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise Error("error_invalid_domain") from exc
    if (
        len(value) > 253
        or "." not in value
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
            for p in value.split(".")
        )
        or value.split(".")[-1].isdigit()
    ):
        raise Error("error_enter_a_dns_name_for_example_demo_example")
    return value


def name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", value):
        raise Error("error_use_a_lowercase_name_letters_digits_or_max")
    return value


def safe_path(value):
    p = Path(value)
    if (
        not p.is_absolute()
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(p))
        or ".." in p.parts
    ):
        raise Error("error_use_an_absolute_path_without_spaces_or_special")
    return p


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


class System:
    def __init__(self, dry=False, external=False):
        self.dry = dry
        self.external = external
        self.overlay = {}
        self.commands = []
        self.progress = None
        self.output = None
        self.plans = {}

    def note(self, text):
        if self.output:
            self.output(text)
        else:
            print(text)

    def communicate(self, process, message, timeout, input=None):
        started = time.monotonic()
        try:
            while True:
                if self.progress:
                    self.progress(message, time.monotonic() - started)
                try:
                    return process.communicate(
                        input=input,
                        timeout=min(
                            0.2, max(0.01, timeout - (time.monotonic() - started))
                        ),
                    )
                except subprocess.TimeoutExpired:
                    input = None
                    if time.monotonic() - started >= timeout:
                        raise
        except BaseException:
            if getattr(process, "mos_process_group", False):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                if getattr(process, "mos_process_group", False):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.communicate()
            raise

    def run(
        self,
        args,
        *,
        mutate=False,
        check=True,
        timeout=120,
        input=None,
        env=None,
        allowed_codes=(0,),
        process_group=False,
    ):
        args = [str(x) for x in args]
        self.commands.append(args)
        if self.dry and mutate:
            self.note("  [PREVIEW] " + " ".join(args))
            return ""
        try:
            if self.progress or process_group:
                with subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env or SAFE_ENV,
                    start_new_session=process_group,
                ) as process:
                    process.mos_process_group = process_group
                    stdout, stderr = self.communicate(
                        process, " ".join(args), timeout, input
                    )
                    result = subprocess.CompletedProcess(
                        args, process.returncode, stdout, stderr
                    )
            else:
                result = subprocess.run(
                    args,
                    input=input,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=env or SAFE_ENV,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error(f"{args[0]}: {exc}") from exc
        if check and result.returncode not in allowed_codes:
            # Credentials must never be passed in argv, and output is not persisted.
            raise Error(
                f"{args[0]} failed ({result.returncode}):\n{result.stderr[-3000:]}\n{result.stdout[-1000:]}"
            )
        return result.stdout

    def run_to_file(self, args, path, timeout=1800):
        if self.dry:
            self.note(f"  [PREVIEW] database dump -> {path}")
            return
        self.mkdir(Path(path).parent, 0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as out:
                if self.progress:
                    with subprocess.Popen(
                        [str(a) for a in args],
                        stdout=out,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        text=True,
                        env=SAFE_ENV,
                    ) as process:
                        _, stderr = self.communicate(process, "Database dump", timeout)
                        result = subprocess.CompletedProcess(
                            args, process.returncode, None, stderr
                        )
                else:
                    result = subprocess.run(
                        [str(a) for a in args],
                        stdout=out,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=SAFE_ENV,
                        timeout=timeout,
                    )
            if result.returncode:
                raise Error("Database dump failed: " + result.stderr[-2000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error("error_database_dump_did_not_complete") from exc

    def read(self, path, default=""):
        p = Path(path)
        if str(p) in self.overlay:
            return self.overlay[str(p)] or default
        try:
            return p.read_text()
        except FileNotFoundError:
            return default

    def exists(self, path):
        key = str(path)
        return (
            self.overlay[key] is not None
            if key in self.overlay
            else Path(path).exists()
        )

    def mkdir(self, path, mode=0o750):
        if not self.dry:
            p = Path(path)
            created = not p.exists()
            p.mkdir(parents=True, exist_ok=True, mode=mode)
            if created:
                p.chmod(mode)

    def write(self, path, content, mode=0o600):
        p = Path(path)
        if self.dry:
            self.overlay[str(p)] = content
            self.note(f"  [PREVIEW] write {p}")
            return
        if p.is_symlink():
            raise Error(f"Refusing to overwrite a symbolic link: {p}")
        self.mkdir(p.parent)
        fd, tmp = tempfile.mkstemp(prefix=".mos-", dir=p.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, path):
        if self.dry:
            self.overlay[str(path)] = None
            self.note(f"  [PREVIEW] remove {path}")
        else:
            Path(path).unlink(missing_ok=True)

    def data(self):
        empty = '{"sites":{},"certificates":{},"language":"en"}'
        try:
            text = self.read(STATE / "state.json", empty)
        except PermissionError:
            if not self.dry:
                raise
            text = empty
        return json.loads(text)

    def save(self, data):
        self.write(STATE / "state.json", json.dumps(data, indent=2) + "\n")

    def audit(self, action):
        if not self.dry:
            self.mkdir(STATE)
            p = STATE / "operations.jsonl"
            fd = os.open(
                p, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "a") as out:
                out.write(json.dumps({"time": stamp(), "action": action}) + "\n")

    @contextlib.contextmanager
    def transaction(self, paths, validators=()):
        """Rollback file edits on validation failure; package actions aren't reversible."""
        previous = {
            str(p): (
                (
                    self.read(p),
                    Path(p).stat().st_mode & 0o777 if Path(p).exists() else 0o600,
                )
                if self.exists(p)
                else None
            )
            for p in paths
        }
        backup = STATE / "config-backups" / stamp()
        for p, old in previous.items():
            if old:
                self.write(backup / p.lstrip("/"), old[0], 0o600)
        try:
            yield
            for command in validators:
                self.run(command, mutate=True)
        except BaseException:
            for p, old in previous.items():
                if old is None:
                    self.delete(p)
                else:
                    self.write(p, old[0], old[1])
            raise


_held_locks = threading.local()


@contextlib.contextmanager
def lock(dry=False, path=None, wait=0):
    if dry:
        yield
        return
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = Path(path or STATE / "operation.lock").absolute()
    identity = (os.getpid(), str(path))
    held = getattr(_held_locks, "paths", None)
    if held is None:
        held = _held_locks.paths = set()
    # A nested action in the same thread already owns the OS lock. Other
    # threads and processes must still acquire it independently.
    if identity in held:
        yield
        return
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise Error("error_another_mos_operation_is_running") from exc
                time.sleep(0.05)
        held.add(identity)
        try:
            yield
        finally:
            held.remove(identity)


def os_info():
    info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip('"')
    return info


def require_supported():
    info = os_info()
    allowed = {"debian": {"11", "12", "13"}, "ubuntu": {"22.04", "24.04", "26.04"}}
    if info.get("VERSION_ID") not in allowed.get(info.get("ID"), set()):
        raise Error("error_supported_targets_debian_11_12_13_and_ubuntu")
    import platform

    if platform.machine() != "x86_64":
        raise Error("error_this_beta_targets_x86_64_only")


def official_uri(uri):
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    return parsed.scheme in ("http", "https") and (
        host
        in {
            "deb.debian.org",
            "security.debian.org",
            "archive.ubuntu.com",
            "security.ubuntu.com",
            "ports.ubuntu.com",
            "old-releases.ubuntu.com",
        }
        or host.endswith(".archive.ubuntu.com")
        or host.endswith(".deb.debian.org")
    )


def approved_source(system, uri):
    if official_uri(uri):
        return True
    if re.fullmatch(r"mirror\+file:///etc/apt/mirrors/[A-Za-z0-9_.-]+", uri):
        path = Path(uri.removeprefix("mirror+file://"))
        if path.is_symlink():
            return False
        entries = [
            line.split()[0]
            for line in system.read(path).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        return bool(entries) and all(official_uri(entry) for entry in entries)
    return False


def repository_check(sys):
    """Refuse external active sources unless the operator explicitly opts in."""
    bad = []
    files = [Path("/etc/apt/sources.list")]
    files += list(Path("/etc/apt/sources.list.d").glob("*.list"))
    files += list(Path("/etc/apt/sources.list.d").glob("*.sources"))
    for file in files:
        content = sys.read(file)
        if file.suffix == ".sources":
            for paragraph in re.split(r"\n\s*\n", content):
                if re.search(r"^Enabled:\s*no\s*$", paragraph, re.M | re.I):
                    continue
                # Debian822 allows folded URI lists.
                fields = re.sub(r"\n[ \t]+", " ", paragraph)
                for match in re.finditer(r"^URIs:\s*(.+)$", fields, re.M | re.I):
                    bad.extend(
                        uri for uri in match[1].split() if not approved_source(sys, uri)
                    )
        else:
            for line in content.splitlines():
                if re.match(r"^\s*deb(?:-src)?\s", line):
                    parts = re.sub(r"\[[^]]*\]", "", line).split()
                    if len(parts) > 1 and not approved_source(sys, parts[1]):
                        bad.append(parts[1])
    if bad and not sys.external:
        raise Error(
            "External/custom APT sources detected: "
            + ", ".join(sorted(set(bad)))
            + "\nDisable them or explicitly use --allow-external-repositories at your own responsibility."
        )


def installed(sys, package):
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package):
        raise Error("error_invalid_package")
    return (
        sys.run(["dpkg-query", "-W", "-f=${Status}", package], check=False).strip()
        == "install ok installed"
    )


def available(sys, package):
    text = sys.run(["apt-cache", "policy", package], check=False)
    match = re.search(r"Candidate:\s*(\S+)", text)
    return bool(match and match[1] != "(none)")


def php_version(value):
    if not re.fullmatch(r"[1-9]\d?\.\d{1,2}", str(value)) or tuple(
        map(int, value.split("."))
    ) < (7, 4):
        raise Error("error_unsupported_php_version")
    return value


def php_versions(sys, installed_only=False):
    output = sys.run(
        ["dpkg-query", "-W", "-f=${Package}\t${db:Status-Abbrev}\n", "php*-fpm"],
        check=False,
    )
    present = set(re.findall(r"^php(\d+\.\d+)-fpm\s+ii", output, re.M))
    if installed_only:
        versions = present
    else:
        candidates = sys.run(["apt-cache", "pkgnames", "php"], check=False)
        found = set(re.findall(r"^php(\d+\.\d+)-fpm$", candidates, re.M))
        versions = present | {v for v in found if available(sys, f"php{v}-fpm")}
    return sorted(
        (v for v in versions if tuple(map(int, v.split("."))) >= (7, 4)),
        key=lambda v: tuple(map(int, v.split("."))),
    )


PHP_EXTENSIONS = [
    "cli",
    "fpm",
    "mysql",
    "curl",
    "mbstring",
    "xml",
    "zip",
    "gd",
    "intl",
    "bcmath",
    "opcache",
]
BASE = [
    "apache2",
    "mariadb-server",
    "composer",
    "certbot",
    "python3-certbot-apache",
    "nodejs",
    "npm",
    "clamav",
    "clamav-freshclam",
    "fail2ban",
    "git",
    "unzip",
    "curl",
    "acl",
    "nano",
    "rclone",
    "restic",
    "ufw",
    "openssl",
]


def php_packages(sys, version):
    # Some PHP releases bundle extensions that older releases package separately.
    return [
        f"php{version}-{extension}"
        for extension in PHP_EXTENSIONS
        if available(sys, f"php{version}-{extension}")
        or installed(sys, f"php{version}-{extension}")
    ]


def package_catalog(sys):
    rows = [
        ("category_web|Apache", ["apache2"]),
        ("category_web|nginx_conflict", ["nginx"]),
        ("category_database|MariaDB", ["mariadb-server"]),
        ("category_database|PostgreSQL", ["postgresql"]),
        ("category_cache|Redis", ["redis-server"]),
    ]
    for version in php_versions(sys):
        rows.append((f"category_php|{version}", php_packages(sys, version)))
    rows += [
        ("category_tools|Composer", ["composer"]),
        ("category_tools|Node.js + npm", ["nodejs", "npm"]),
        ("SSL / Certbot", ["certbot", "python3-certbot-apache"]),
        ("category_security|ClamAV", ["clamav", "clamav-freshclam"]),
        ("category_security|Fail2ban", ["fail2ban"]),
        ("category_security|UFW", ["ufw"]),
        ("category_backups|Restic + Rclone", ["restic", "rclone"]),
        ("category_network|Speedtest CLI", ["speedtest-cli"]),
    ]
    return rows


def apt_plan(sys, add=(), remove=(), purge=False):
    add, remove = sorted(set(add)), sorted(set(remove))
    if set(add) & set(remove):
        raise Error("error_a_package_cannot_be_installed_and_removed_together")
    for pkg in add + remove:
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", pkg):
            raise Error("error_invalid_package_name")
    data = sys.data()
    used = {s.get("php") for s in data["sites"].values()}
    for pkg in remove:
        if (pkg in ("apache2", "mariadb-server") and data["sites"]) or any(
            pkg.startswith(f"php{v}-") for v in used if v
        ):
            raise Error(
                f"{pkg} is required by managed websites. Remove/migrate those websites first."
            )
    if "nginx" in add and ("apache2" in add or installed(sys, "apache2")):
        raise Error("error_apache_already_uses_ports_80_443_remove_apache")
    if "apache2" in add and installed(sys, "nginx"):
        raise Error("error_nginx_is_installed_resolve_the_port_conflict_before")
    missing = [p for p in add if not available(sys, p)]
    if missing and not sys.dry:
        raise Error("No APT candidate for: " + ", ".join(missing))
    if not add and not remove:
        return [], "No changes"
    args = ["apt-get", "--simulate", "--no-install-recommends", "install"]
    if purge:
        args += ["--purge"]
    args += add + [p + "-" for p in remove]
    output = sys.run(args, check=not sys.dry)
    # Do not silently remove unselected packages through APT dependency resolution.
    removed = set(re.findall(r"^(?:Remv|Purg)\s+(\S+)", output, re.M))
    extra = removed - set(remove)
    if extra:
        raise Error(
            "APT would also remove dependent packages: "
            + ", ".join(sorted(extra))
            + ". Select these explicitly after reviewing dependencies."
        )
    return remember_plan(sys, [x for x in args if x != "--simulate"], output), output


def plan_signature(output):
    return sorted(
        line.strip()
        for line in output.splitlines()
        if re.match(r"^(Inst|Remv|Purg|Conf)\s", line)
    )


def remember_plan(sys, args, output):
    sys.plans[tuple(args)] = plan_signature(output)
    return args


def upgrade_plan(sys):
    args = ["apt-get", "--no-remove", "--with-new-pkgs", "upgrade"]
    output = sys.run(args[:1] + ["--simulate"] + args[1:], check=not sys.dry)
    if re.search(r"^(Remv|Purg)\s", output, re.M):
        raise Error("ui_update_removal_blocked")
    if not re.search(r"^Inst\s", output, re.M):
        return [], output
    return remember_plan(sys, args, output), output


def health_report(sys):
    rows = []
    checks = []
    if installed(sys, "apache2"):
        checks.append(["apache2ctl", "configtest"])
    checks += [[f"php-fpm{v}", "-t"] for v in php_versions(sys, True)]
    if installed(sys, "openssh-server"):
        checks.append(["/usr/sbin/sshd", "-t"])
    for command in checks:
        try:
            sys.run(command, mutate=True)
            rows.append("OK  " + " ".join(command))
        except Error as exc:
            rows.append("ERROR  " + str(exc))
    return "\n".join(rows) or "No installed web/SSH services to validate"


def apply_packages(sys, args):
    if args:
        repository_check(sys)
        expected = sys.plans.get(tuple(args))
        if expected is None:
            raise Error("ui_plan_missing")
        current = sys.run(args[:1] + ["--simulate"] + args[1:], check=not sys.dry)
        if plan_signature(current) != expected:
            raise Error("ui_plan_changed")
        # Keep administrator configuration on package upgrades; MOS uses dedicated drop-ins.
        options = [
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
        ]
        sys.run(
            args + options + ["-y"],
            mutate=True,
            timeout=3600,
            env={**SAFE_ENV, "DEBIAN_FRONTEND": "noninteractive"},
        )
        sys.audit("APT package plan applied")


def refresh(sys):
    repository_check(sys)
    sys.run(["apt-get", "update"], mutate=True, timeout=900)
