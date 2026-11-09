"""Check published tags; upgrades always use a fixed GitHub commit."""

import json
import re
import urllib.request
from .core import VERSION

REPOSITORY = "reyespuente/mos-itc"


def version(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?", value)
    if not match:
        return None
    a, b, c, stage, n = match.groups()
    return (
        int(a),
        int(b),
        int(c),
        {"alpha": 0, "beta": 1, "rc": 2, None: 3}[stage],
        int(n or 0),
    )


def latest():
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/tags?per_page=100",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MOS-ITC"},
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        tags = json.loads(response.read(1024 * 1024))
    valid = [
        tag
        for tag in tags
        if version(tag.get("name", ""))
        and re.fullmatch("[a-f0-9]{40}", tag.get("commit", {}).get("sha", ""))
    ]
    tag = max(valid, key=lambda t: version(t["name"])) if valid else None
    if tag and version(tag["name"]) > version(VERSION):
        return {"tag": tag["name"], "sha": tag["commit"]["sha"]}
    return None
