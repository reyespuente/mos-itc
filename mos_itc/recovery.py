"""Inspect installer restore points and recover files without overwriting a host."""

import json
import os
from pathlib import Path
import re
import tarfile
import shutil

from .core import Error, safe_path, STATE
from .backups import restore_target

POINTS = Path("/var/backups/mos-itc/restore-points")


def list_points():
    result = []
    for base, prefix in ((POINTS, ""), (STATE / "config-backups", "config:")):
        if not base.exists():
            continue
        for path in sorted(base.iterdir(), reverse=True):
            if not path.is_dir() or path.is_symlink():
                continue
            result.append(
                {
                    "name": prefix + path.name,
                    "files": sorted(f.name for f in path.iterdir()),
                    "bytes": sum(
                        f.stat().st_size
                        for f in path.rglob("*")
                        if f.is_file() and not f.is_symlink()
                    ),
                }
            )
    return result


def extract_point(label, target):
    point = point_path(label)
    target = restore_target(target)
    target.mkdir(mode=0o700)
    if label.startswith("config:"):
        for source in point.rglob("*"):
            if source.is_file() and not source.is_symlink():
                dest = target / source.relative_to(point)
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, dest)
                dest.chmod(0o600)
        return target
    links = []
    for archive in ("etc.tar.gz", "previous-app.tar.gz"):
        file = point / archive
        if not file.exists():
            continue
        with tarfile.open(file) as tar:
            for entry in tar:
                relative = Path(entry.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise Error("ui_invalid_restore_point")
                dest = target / relative
                if entry.isdir():
                    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                elif entry.isfile():
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(
                        dest,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                        0o600,
                    )
                    with os.fdopen(fd, "wb") as out, tar.extractfile(entry) as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                    dest.chmod(entry.mode & 0o777)
                else:
                    links.append(
                        {
                            "path": entry.name,
                            "target": entry.linkname,
                            "type": entry.type.decode(errors="replace"),
                        }
                    )
    (target / "LINKS.json").write_text(json.dumps(links, indent=2))
    inventory = target / "inventory"
    inventory.mkdir(mode=0o700)
    for filename in (
        "dpkg-status",
        "packages.tsv",
        "manual-packages.txt",
        "services.txt",
        "mos-state.json",
    ):
        source = point / filename
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, inventory / filename)
            (inventory / filename).chmod(0o600)
    return target


def point_path(label):
    if label.startswith("config:"):
        name = label[7:]
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z", name):
            raise Error("ui_invalid_restore_point")
        path = STATE / "config-backups" / name
    else:
        if not re.fullmatch(r"[0-9TZ-]+", label):
            raise Error("ui_invalid_restore_point")
        path = POINTS / label
    if path.is_symlink() or not path.is_dir():
        raise Error("ui_invalid_restore_point")
    return path


def contents(label):
    path = point_path(label)
    if label.startswith("config:"):
        return "\n".join(
            str(p.relative_to(path))
            for p in sorted(path.rglob("*"))
            if p.is_file() and not p.is_symlink()
        )
    lines = []
    for file in sorted(path.iterdir()):
        if file.is_symlink() or not file.is_file():
            continue
        lines.append(file.name)
        if file.name in ("etc.tar.gz", "previous-app.tar.gz"):
            with tarfile.open(file) as tar:
                lines.extend("  " + member.name for member in tar)
    return "\n".join(lines)


def remove(label):
    shutil.rmtree(point_path(label))
