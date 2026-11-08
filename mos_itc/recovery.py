"""Inspect installer restore points and recover files without overwriting a host."""

import json
import os
from pathlib import Path
import re
import tarfile
import shutil

from .core import Error, safe_path

POINTS = Path("/var/backups/mos-itc/restore-points")


def list_points():
    if not POINTS.exists():
        return []
    return [
        {"name": p.name, "files": sorted(f.name for f in p.iterdir())}
        for p in sorted(POINTS.iterdir(), reverse=True)
        if p.is_dir() and not p.is_symlink()
    ]


def extract_point(label, target):
    if not re.fullmatch(r"[0-9TZ-]+", label):
        raise Error("ui_invalid_restore_point")
    point = POINTS / label
    target = safe_path(target)
    if point.is_symlink() or not point.is_dir() or target.exists():
        raise Error("ui_restore_point_target")
    target.mkdir(parents=True, mode=0o700)
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
