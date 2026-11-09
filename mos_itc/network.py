"""Host addresses and routes, with optional EC2 IMDSv2 discovery."""

import ipaddress
import json
import subprocess
import urllib.request


def public(value):
    try:
        return (
            str(ipaddress.ip_address(value))
            if ipaddress.ip_address(value).is_global
            else ""
        )
    except ValueError:
        return ""


def inventory():
    result = {"addresses": [], "routes": [], "public": ""}
    for command, key in (
        (["ip", "-j", "address", "show"], "addresses"),
        (["ip", "-j", "route", "show", "default"], "routes"),
        (["ip", "-j", "-6", "route", "show", "default"], "routes"),
    ):
        try:
            data = json.loads(
                subprocess.run(
                    command, capture_output=True, text=True, timeout=3, check=True
                ).stdout
            )
            if key == "addresses":
                for interface in data:
                    for address in interface.get("addr_info", []):
                        if address.get("scope") != "host":
                            value = address["local"]
                            result[key].append(
                                {
                                    "interface": interface["ifname"],
                                    "address": value,
                                    "prefix": address["prefixlen"],
                                    "public": bool(public(value)),
                                }
                            )
                            result["public"] = result["public"] or public(value)
            else:
                result[key] += [
                    {
                        "interface": r.get("dev", "—"),
                        "gateway": r.get("gateway", "on-link"),
                    }
                    for r in data
                ]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return result


def ec2_public():
    # No credentials or instance role endpoints; never use an HTTP proxy here.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with opener.open(request, timeout=1) as response:
            token = response.read(1024).decode()
        request = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/public-ipv4",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with opener.open(request, timeout=1) as response:
            return public(response.read(128).decode().strip())
    except (OSError, ValueError):
        return ""
