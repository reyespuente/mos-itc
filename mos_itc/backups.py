"""Guided storage profiles and Restic operations with bounded, per-repository locks."""

import configparser
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from urllib.parse import urlsplit

from . import core
from .core import CONF, STATE, SITES, Error, SAFE_ENV, name, safe_path


RCLONE_BASELINE = (1, 76, 0)
RCLONE_RELEASE = "1.76.0"
RCLONE_PRIVATE = Path("/usr/local/lib/mos-itc/rclone")
FILE_TRANSPORTS = ("sftp", "ftp", "ftps", "ftps-implicit")


def rclone_program(system):
    if not system.dry and RCLONE_PRIVATE.exists():
        st = RCLONE_PRIVATE.stat()
        if (
            RCLONE_PRIVATE.is_symlink()
            or RCLONE_PRIVATE.resolve() != RCLONE_PRIVATE
            or st.st_uid != 0
            or st.st_mode & 0o022
        ):
            raise Error("backup_runtime_unsafe")
        return str(RCLONE_PRIVATE)
    return shutil.which("rclone", path=SAFE_ENV["PATH"]) or "rclone"


def compatible_rclone(system):
    if system.dry:
        return True
    output = system.run([rclone_program(system), "version"])
    match = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", output)
    return bool(match and tuple(map(int, match.groups())) >= RCLONE_BASELINE)


def install_rclone_runtime(system):
    """Install the tested upstream component alongside, never over, the APT binary."""
    if system.dry:
        system.note("backup_runtime_preview")
        return
    if os.geteuid() != 0:
        raise Error("backup_runtime_unsafe")
    filename = f"rclone-v{RCLONE_RELEASE}-linux-amd64.zip"
    base = f"https://downloads.rclone.org/v{RCLONE_RELEASE}/"

    def request(url):
        # The public download service rejects urllib's default User-Agent.
        return urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=30
        )

    try:
        with request(base + "SHA256SUMS") as response:
            checks = response.read(1024 * 1024).decode()
        hashes = [
            line.split()[0]
            for line in checks.splitlines()
            if len(line.split()) >= 2
            and line.split()[-1].lstrip("*") == filename
            and re.match(r"^[a-fA-F0-9]{64}\s", line)
        ]
        if len(hashes) != 1:
            raise Error("backup_runtime_checksum")
        with tempfile.TemporaryDirectory(prefix="mos-rclone-") as temporary:
            archive = Path(temporary) / "rclone.zip"
            digest = hashlib.sha256()
            size = 0
            with request(base + filename) as response, archive.open("wb") as target:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > 128 * 1024 * 1024:
                        raise Error("backup_runtime_checksum")
                    digest.update(block)
                    target.write(block)
            if digest.hexdigest() != hashes[0]:
                raise Error("backup_runtime_checksum")
            with core.lock():
                parent = RCLONE_PRIVATE.parent
                system.mkdir(parent, 0o755)
                if (
                    parent.is_symlink()
                    or parent.resolve() != parent
                    or parent.stat().st_uid != 0
                    or parent.stat().st_mode & 0o022
                ):
                    raise Error("backup_runtime_unsafe")
                fd, path = tempfile.mkstemp(prefix=".rclone-", dir=parent)
                try:
                    with (
                        os.fdopen(fd, "wb") as target,
                        zipfile.ZipFile(archive) as zipped,
                    ):
                        entry = zipped.getinfo(
                            f"rclone-v{RCLONE_RELEASE}-linux-amd64/rclone"
                        )
                        if entry.file_size > 256 * 1024 * 1024:
                            raise Error("backup_runtime_checksum")
                        with zipped.open(entry) as source:
                            shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                        os.fchmod(target.fileno(), 0o755)
                    version = system.run([path, "version"])
                    if not version.startswith("rclone v" + RCLONE_RELEASE + "\n"):
                        raise Error("backup_runtime_checksum")
                    os.replace(path, RCLONE_PRIVATE)
                finally:
                    Path(path).unlink(missing_ok=True)
                system.write(
                    STATE / "backup-runtime.json",
                    json.dumps(
                        {
                            "rclone": RCLONE_RELEASE,
                            "sha256": hashes[0],
                            "source": base + filename,
                        }
                    ),
                )
    except Error:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise Error("backup_runtime_download_failed") from None


def folder(identifier):
    return CONF / "backups" / name(identifier)


def profiles(system):
    result = []
    base = CONF / "backups"
    paths = set(base.glob("*/config.json"))
    paths.update(
        Path(p)
        for p in system.overlay
        if p.startswith(str(base) + "/")
        and p.endswith("/config.json")
        and system.overlay[p]
    )
    for path in sorted(paths):
        result.append(json.loads(system.read(path)))
    if system.exists(CONF / "backup.json"):
        old = json.loads(system.read(CONF / "backup.json"))
        result.insert(
            0,
            dict(
                old,
                id="legacy",
                label="backup_legacy",
                transport="legacy",
                paths=["/etc", str(STATE)]
                + ([str(SITES)] if system.exists(SITES) or system.dry else []),
                databases=True,
            ),
        )
    return result


def load(system, identifier=None):
    rows = profiles(system)
    if identifier is None:
        # Preserve old systemd jobs after adding a second destination.
        legacy = next((p for p in rows if p["id"] == "legacy"), None)
        if legacy:
            return legacy
        if len(rows) == 1:
            return rows[0]
    for profile in rows:
        if profile["id"] == identifier:
            return profile
    raise Error("backup_select_repository")


def text(value, allow_empty=False):
    if (
        not isinstance(value, str)
        or any(c in value for c in "\r\n\x00")
        or (not value and not allow_empty)
    ):
        raise Error("backup_invalid_field")
    return value


def endpoint(value):
    value = text(value)
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise Error("backup_https_endpoint")
    return value.rstrip("/")


def remote_path(value, absolute=False):
    value = text(value).strip()
    if not re.fullmatch(
        r"/?[A-Za-z0-9][A-Za-z0-9._/-]{0,240}", value
    ) or ".." in value.split("/"):
        raise Error("backup_invalid_remote_path")
    if not absolute and value.startswith("/"):
        raise Error("backup_invalid_remote_path")
    return value.rstrip("/")


def obscure(system, password):
    password = text(password)
    if system.dry:
        return "PREVIEW"
    return system.run(
        [rclone_program(system), "obscure", "-"], input=password + "\n"
    ).strip()


def render_config(settings):
    config = configparser.ConfigParser(interpolation=None)
    config["storage"] = {k: text(str(v), True) for k, v in settings.items()}
    out = io.StringIO()
    config.write(out)
    return out.getvalue()


def storage(system, kind, fields):
    """Return an INI and a remote path; no credential is put into argv."""
    if kind in ("s3", "s3-compatible"):
        bucket = text(fields["bucket"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise Error("backup_invalid_bucket")
        prefix = remote_path(fields["prefix"])
        region = text(fields.get("region", ""), True)
        if kind == "s3" and not re.fullmatch(r"[a-z0-9-]+", region):
            raise Error("backup_invalid_field")
        address = endpoint(
            fields.get("endpoint") or f"https://s3.{region}.amazonaws.com"
        )
        settings = {
            "type": "s3",
            "provider": "AWS" if kind == "s3" else "Other",
            "access_key_id": text(fields["access_key"]),
            "secret_access_key": text(fields["secret_key"]),
            "region": region,
            "endpoint": address,
            "force_path_style": "true",
        }
        host = urlsplit(address).hostname or ""
        if kind == "s3-compatible" and re.search(
            r"(?:^|\.)idrivee2(?:-\d+)?\.com$", host.lower()
        ):
            # IDrive ETags may describe encrypted data rather than plaintext MD5.
            # Multipart uploads preserve content MD5 in object metadata instead;
            # keep that verification, without changing bucket encryption.
            settings.update(upload_cutoff="0", use_multipart_etag="false")
        return (
            render_config(settings),
            bucket + "/" + prefix,
            address + "/" + bucket + "/" + prefix,
        )
    if kind in ("sftp", "ftp", "ftps", "ftps-implicit"):
        host = text(fields["host"])
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise Error("backup_invalid_field")
        port = int(
            fields.get(
                "port", 22 if kind == "sftp" else 990 if kind == "ftps-implicit" else 21
            )
        )
        if not 1 <= port <= 65535:
            raise Error("backup_invalid_field")
        settings = {
            "type": "sftp" if kind == "sftp" else "ftp",
            "host": host,
            "user": text(fields["user"]),
            "port": str(port),
        }
        if kind == "sftp":
            settings["known_hosts_file"] = text(fields["known_hosts_file"])
            if fields.get("key_file"):
                settings["key_file"] = text(fields["key_file"])
                if fields.get("key_password"):
                    settings["key_file_pass"] = obscure(system, fields["key_password"])
            else:
                settings["pass"] = obscure(system, fields["password"])
        else:
            settings["pass"] = obscure(system, fields["password"])
            settings["tls"] = "true" if kind == "ftps-implicit" else "false"
            settings["explicit_tls"] = "true" if kind == "ftps" else "false"
        path = remote_path(fields["path"], absolute=True)
        return (
            render_config(settings),
            path,
            f"{kind}://{host}:{port}/{path.lstrip('/')}",
        )
    raise Error("backup_invalid_field")


def validate_sources(paths, databases=False):
    result = list(dict.fromkeys(str(safe_path(p)) for p in paths))
    if not result and not databases:
        raise Error("backup_no_sources")
    for value in result:
        if value == "/" or any(
            value == p or value.startswith(p + "/")
            for p in ("/proc", "/sys", "/dev", "/run")
        ):
            raise Error("backup_invalid_source")
    return result


def create(
    system,
    identifier,
    label,
    password,
    kind,
    fields,
    paths,
    databases=True,
    attachments=None,
    replace=False,
):
    identifier = name(identifier)
    current = next((p for p in profiles(system) if p["id"] == identifier), None)
    if identifier == "legacy" or (current and not replace) or (replace and not current):
        raise Error("backup_profile_exists")
    password = text(password)
    if len(password) < 12:
        raise Error("error_restic_password_at_least_12_characters_one_line")
    base = folder(identifier)
    paths = validate_sources(paths, databases)
    if str(SITES) in paths and not system.exists(SITES):
        system.mkdir(SITES)
    if kind == "rclone":
        remote = text(fields["repository"])
        if not re.fullmatch(
            r"rclone:[A-Za-z0-9_-]+:/?[A-Za-z0-9][A-Za-z0-9_./-]*", remote
        ) or ".." in remote.split("/"):
            raise Error("backup_invalid_remote_path")
        source = safe_path(fields.get("config_file", str(CONF / "rclone.conf")))
        content = system.read(source)
        if not content:
            raise Error("backup_rclone_config_missing")
        address, repository = remote, remote
    else:
        content, path, address = storage(system, kind, fields)
        repository = "rclone:storage:" + path
    profile = {
        "id": identifier,
        "label": text(label),
        "transport": kind,
        "destination": address,
        "repository": repository,
        "paths": paths,
        "databases": bool(databases),
        "schedule": None,
    }
    old_lock = repository_lock(system, current) if current else contextlib.nullcontext()
    with old_lock, core.lock(system.dry):
        if system.exists(base / "config.json") and not replace:
            raise Error("backup_profile_exists")
        files = [
            base / filename
            for filename in (
                "config.json",
                "password",
                "rclone.conf",
                "known_hosts",
                "identity",
                "ca.pem",
            )
        ]
        with system.transaction(files):
            for filename, value in (attachments or {}).items():
                if filename not in ("known_hosts", "identity", "ca.pem"):
                    raise Error("backup_invalid_field")
                system.write(base / filename, value)
            if current:
                for filename in ("known_hosts", "identity", "ca.pem"):
                    if filename not in (attachments or {}):
                        system.delete(base / filename)
                for key in ("schedule", "schedule_kind", "schedule_value"):
                    if key in current:
                        profile[key] = current[key]
                if current.get("destination") != address:
                    system.run(
                        [
                            "systemctl",
                            "disable",
                            "--now",
                            "mos-backup-" + identifier + ".timer",
                        ],
                        mutate=True,
                        check=False,
                    )
                    profile.update(
                        schedule=None, schedule_kind="off", schedule_value=""
                    )
                    system.delete(STATE / "backup-status" / (identifier + ".json"))
            system.write(base / "password", password + "\n")
            system.write(base / "rclone.conf", content)
            system.write(base / "config.json", json.dumps(profile, indent=2) + "\n")
    return profile


def environment(system, profile):
    base = CONF if profile["id"] == "legacy" else folder(profile["id"])
    env = dict(
        SAFE_ENV,
        RESTIC_REPOSITORY=profile["repository"],
        RESTIC_PASSWORD_FILE=str(
            base / ("restic-password" if profile["id"] == "legacy" else "password")
        ),
        RCLONE_CONFIG=str(base / "rclone.conf"),
        RESTIC_CACHE_DIR=str(STATE / "restic-cache"),
    )
    if system.exists(base / "ca.pem"):
        env["RCLONE_CA_CERT"] = str(base / "ca.pem")
    private = rclone_program(system)
    if private == str(RCLONE_PRIVATE):
        env["PATH"] = str(RCLONE_PRIVATE.parent) + ":" + SAFE_ENV["PATH"]
    env.update(
        {
            k: str(v)
            for k, v in profile.get("s3", {}).items()
            if k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION")
        }
    )
    return env


def repository_key(profile):
    # All profiles pointing to the same repository share a local lock.
    return hashlib.sha256(
        profile.get("destination", profile["repository"]).encode()
    ).hexdigest()[:24]


@contextlib.contextmanager
def repository_lock(system, profile):
    directory = STATE / "backup-locks"
    system.mkdir(directory, 0o700)
    with core.lock(system.dry, directory / (repository_key(profile) + ".lock")):
        yield


def run_restic(system, profile, arguments, timeout=7200):
    if profile.get("transport") in FILE_TRANSPORTS and not compatible_rclone(system):
        raise Error("backup_rclone_upgrade_required")
    flags = system.run(["restic", "help"])
    retry = (
        ["--retry-lock", "30s"]
        if "--retry-lock" in flags and arguments[0] not in ("unlock", "list", "cat")
        else []
    )
    try:
        return system.run(
            ["restic"] + retry + arguments,
            env=environment(system, profile),
            mutate=True,
            timeout=timeout,
            process_group=True,
        )
    except Error as exc:
        # Never record transport credentials, decrypted settings or password prompts.
        message = str(exc).lower()
        if "timed out" in message or "timeout" in message:
            raise Error("backup_operation_timeout") from None
        if "md5 hashes differ" in message or "corrupted on transfer" in message:
            raise Error("backup_checksum_mismatch") from None
        if "locked" in message or "unable to create lock" in message:
            raise Error("backup_repository_locked") from None
        if "wrong password" in message or "no key found" in message:
            raise Error("backup_wrong_password") from None
        raise Error("backup_operation_failed") from None


def status(system, profile):
    data = json.loads(
        system.read(STATE / "backup-status" / (profile["id"] + ".json"), "{}")
    )
    if data.get("state") == "running":
        try:
            current = (
                Path(f'/proc/{int(data["pid"])}/stat')
                .read_text()
                .split(")", 1)[1]
                .split()[19]
            )
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            alive = current == data.get("process_start") and boot == data.get("boot")
        except (OSError, ValueError, KeyError):
            alive = False
        if not alive:
            data["state"] = "interrupted"
    return data


def events(system, profile):
    lines = system.read(
        STATE / "backup-events" / (profile["id"] + ".jsonl"), ""
    ).splitlines()
    return "\n".join(lines[-100:])


def record(system, profile, details):
    location = STATE / "backup-status" / (profile["id"] + ".json")
    system.write(location, json.dumps(details))
    if details["state"] != "running" and not system.dry:
        directory = STATE / "backup-events"
        system.mkdir(directory, 0o700)
        fd = os.open(
            directory / (profile["id"] + ".jsonl"),
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "a") as out:
            out.write(json.dumps(details) + "\n")


def snapshots(system, profile):
    with repository_lock(system, profile):
        return json.loads(
            run_restic(system, profile, ["snapshots", "--json"], 300) or "[]"
        )


def test_connection(system, profile):
    if profile.get("transport") in FILE_TRANSPORTS and not compatible_rclone(system):
        raise Error("backup_rclone_upgrade_required")
    with repository_lock(system, profile):
        env = environment(system, profile)
        if profile["repository"].startswith("rclone:"):
            remote = profile["repository"][7:]
            token = ".mos-test-" + secrets_token()
            path = remote.rstrip("/") + "/" + token
            try:
                system.run(
                    [rclone_program(system), "mkdir", remote],
                    env=env,
                    mutate=True,
                    timeout=90,
                    process_group=True,
                )
                system.run(
                    [rclone_program(system), "rcat", path],
                    input=token,
                    env=env,
                    mutate=True,
                    timeout=90,
                    process_group=True,
                )
                result = system.run(
                    [rclone_program(system), "cat", path],
                    env=env,
                    mutate=True,
                    timeout=90,
                    process_group=True,
                )
                if not system.dry and result != token:
                    raise Error("backup_connection_failed")
                system.run(
                    [rclone_program(system), "deletefile", path],
                    env=env,
                    mutate=True,
                    timeout=90,
                    process_group=True,
                )
            except Error:
                raise Error("backup_connection_failed") from None
        else:
            run_restic(system, profile, ["snapshots", "--json"], 90)
    return "backup_connection_ok"


def secrets_token():
    import secrets

    return secrets.token_hex(12)


def database_exports(system, stage):
    system.mkdir(stage, 0o700)
    result = []
    # Detect server binaries, including installations without a metapackage.
    if shutil.which("mariadb-dump") and shutil.which("mariadbd"):
        target = stage / "mariadb.sql"
        system.run_to_file(
            [
                "mariadb-dump",
                "--all-databases",
                "--single-transaction",
                "--routines",
                "--events",
                "--triggers",
            ],
            target,
        )
        result.append(str(target))
    if shutil.which("pg_lsclusters"):
        rows = system.run(["pg_lsclusters", "--no-header"]).splitlines()
        for line in rows:
            parts = line.split()
            if len(parts) < 4 or not parts[0].isdigit() or not parts[2].isdigit():
                continue
            if not parts[3].startswith("online"):
                raise Error("backup_database_offline")
            target = stage / ("postgresql-" + parts[2] + ".sql")
            system.run_to_file(
                ["runuser", "-u", "postgres", "--", "pg_dumpall", "--port", parts[2]],
                target,
            )
            result.append(str(target))
    return result


def restore_target(target):
    target = safe_path(target)
    if (
        target == Path("/")
        or target.exists()
        or target.is_symlink()
        or target.resolve() != target
    ):
        raise Error("error_restore_to_a_new_directory_then_inspect_files")
    for parent in target.parents:
        if (
            not parent.is_dir()
            or parent.stat().st_uid != 0
            or parent.stat().st_mode & 0o022
        ):
            raise Error("backup_restore_private_parent")
    return target


def operate(system, identifier, action, target=None, snapshot="latest"):
    profile = load(system, identifier)
    if action == "restore":
        target = restore_target(target or "/srv/mos-restore")
        if snapshot != "latest" and not re.fullmatch(r"[a-f0-9]{8,64}", snapshot):
            raise Error("backup_invalid_field")
    with repository_lock(system, profile):
        started = time.monotonic()
        info = {
            "action": action,
            "state": "running",
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "pid": os.getpid(),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "process_start": Path("/proc/self/stat")
            .read_text()
            .split(")", 1)[1]
            .split()[19],
        }
        info["last_backup"] = status(system, profile).get("last_backup", "")
        record(system, profile, info)
        try:
            if action == "backup":
                paths = validate_sources(
                    profile.get("paths", []), profile.get("databases", True)
                )
                stage = STATE / "backup-staging" / profile["id"]
                with core.lock(system.dry):
                    if stage.exists() and not system.dry:
                        if stage.is_symlink():
                            raise Error("backup_invalid_source")
                        shutil.rmtree(stage)
                    exports = (
                        database_exports(system, stage)
                        if profile.get("databases", True)
                        else []
                    )
                    if exports:
                        paths.append(str(stage))
                    existing = [p for p in paths if system.exists(p) or system.dry]
                    if len(existing) != len(paths):
                        raise Error("backup_source_missing")
                    if not existing:
                        raise Error("backup_no_sources")
                    manifest = {
                        "paths": paths,
                        "database_exports": exports,
                        "time": info["time"],
                        "host": os.uname().nodename,
                    }
                    system.write(
                        stage / "MOS-RESTORE.json", json.dumps(manifest, indent=2)
                    )
                    if str(stage) not in existing:
                        existing.append(str(stage))
                output = run_restic(
                    system,
                    profile,
                    ["backup"]
                    + existing
                    + [
                        "--exclude",
                        str(STATE / "restic-cache"),
                        "--tag",
                        "mos-itc",
                        "--tag",
                        profile["id"],
                    ],
                )
            elif action == "restore":
                # A private, newly reserved directory avoids overwriting live data.
                if not system.dry:
                    target.mkdir(mode=0o700)
                try:
                    output = run_restic(
                        system,
                        profile,
                        ["restore", snapshot, "--target", str(target), "--verify"],
                    )
                finally:
                    if not system.dry and target.is_dir() and not target.is_symlink():
                        target.chmod(0o700)
            elif action in ("init", "check", "unlock", "check-data", "repair-index"):
                args = {
                    "init": ["init"],
                    "check": ["check"],
                    "check-data": ["check", "--read-data"],
                    "unlock": ["unlock"],
                }.get(action)
                if action == "repair-index":
                    help_text = system.run(["restic", "help"])
                    args = (
                        ["repair", "index"]
                        if re.search(r"^\s+repair\s", help_text, re.M)
                        else ["rebuild-index"]
                    )
                output = run_restic(
                    system,
                    profile,
                    args,
                    timeout=(
                        180 if action == "init" else 120 if action == "unlock" else 7200
                    ),
                )
            elif action == "locks":
                output = run_restic(
                    system, profile, ["list", "locks", "--no-lock"], 120
                )
                rows = []
                for lock_id in output.splitlines()[:20]:
                    if re.fullmatch(r"[a-f0-9]{64}", lock_id.strip()):
                        rows.append(
                            run_restic(
                                system,
                                profile,
                                ["cat", "lock", lock_id.strip(), "--no-lock"],
                                120,
                            )
                        )
                output = "\n".join(rows) or output
            else:
                raise Error("error_invalid_backup_action")
            info.update(state="success", duration=round(time.monotonic() - started, 2))
            if action == "backup":
                info["last_backup"] = info["time"]
            if action == "restore":
                info.update(target=str(target), snapshot=snapshot)
            record(system, profile, info)
            return output
        except BaseException as exc:
            code = (
                exc.args[0]
                if isinstance(exc, Error) and exc.args
                else "backup_operation_failed"
            )
            info["error"] = (
                code
                if re.fullmatch(r"(?:backup_|error_)[a-z0-9_]+", str(code))
                else "backup_operation_failed"
            )
            info.update(
                state=(
                    "cancelled"
                    if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "failed"
                ),
                duration=round(time.monotonic() - started, 2),
            )
            record(system, profile, info)
            raise


def save_sources(system, profile, paths, databases):
    paths = validate_sources(paths, databases)
    if profile["id"] == "legacy":
        raise Error("backup_legacy_readonly")
    with repository_lock(system, profile), core.lock(system.dry):
        profile = dict(profile, paths=paths, databases=bool(databases))
        system.write(
            folder(profile["id"]) / "config.json", json.dumps(profile, indent=2)
        )


def calendar(kind, value):
    if kind in ("daily", "weekly"):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise Error("backup_invalid_time")
        return ("Mon " if kind == "weekly" else "") + "*-*-* " + value + ":00"
    if kind == "hours" and str(value) in ("1", "2", "3", "4", "6", "8", "12"):
        return "*-*-* 00/" + str(value) + ":00:00"
    raise Error("backup_invalid_time")


def schedule(system, profile, kind, value=""):
    from .ops import install_timer

    identifier = profile["id"]
    label = "backup" if identifier == "legacy" else "backup-" + identifier
    # install_timer accepts conservative alphabetic names; profile IDs can contain digits.
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", identifier):
        raise Error("backup_invalid_field")
    with repository_lock(system, profile), core.lock(system.dry):
        if kind == "off":
            system.run(
                ["systemctl", "disable", "--now", "mos-" + label + ".timer"],
                mutate=True,
            )
            spec = None
        else:
            spec = calendar(kind, value)
            install_timer(
                system,
                label,
                spec,
                "/usr/local/bin/mos-itc backup run --repository " + identifier,
                "backup " + identifier,
            )
            system.run(["systemctl", "restart", "mos-" + label + ".timer"], mutate=True)
        if identifier != "legacy":
            profile = dict(
                profile, schedule=spec, schedule_kind=kind, schedule_value=str(value)
            )
            system.write(
                folder(identifier) / "config.json", json.dumps(profile, indent=2)
            )
    return spec
