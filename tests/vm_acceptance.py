"""Destructive acceptance checks, explicitly restricted to an identified KVM VM.

Run with sudo on a disposable VM, never on a workstation or production server.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/opt/mos-itc")
from mos_itc import core, ops, web
from mos_itc.core import System, Error

ROOT = Path("/var/lib/mos-itc-vm-tests")
REPORT = ROOT / "results.json"
HOSTS = ("itc-one.mos.test", "itc-two.mos.test")


def guard(expected_ip):
    ipaddress.ip_address(expected_ip)
    assert os.geteuid() == 0, "Root required on the test VM"
    assert (
        core.os_info().get("ID") == "ubuntu"
    ), "This acceptance run is for the authorized Ubuntu VM"
    virt = subprocess.run(
        ["systemd-detect-virt"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert virt in ("kvm", "qemu"), "Only a disposable KVM/QEMU guest is allowed"
    interfaces = json.loads(
        subprocess.run(
            ["ip", "-j", "address"], capture_output=True, text=True, check=True
        ).stdout
    )
    actual = {
        entry["local"] for iface in interfaces for entry in iface.get("addr_info", [])
    }
    assert (
        expected_ip in actual
    ), "The expected VM address is absent; aborting without changes"


def run_stage(stage, system, results):
    if stage == "install":
        core.refresh(system)
        versions = core.php_versions(system)
        assert versions, "No official PHP candidates found"
        packages = (
            core.BASE
            + ["speedtest-cli"]
            + [p for v in versions for p in core.php_packages(system, v)]
        )
        args, plan = core.apt_plan(system, packages)
        (ROOT / "apt-install-plan.txt").write_text(plan)
        core.apply_packages(system, args)
        ops.auto_configure(system, "America/Mexico_City", "mos-itc-test", versions[-1])
        health = core.health_report(system)
        assert "ERROR" not in health, health
        results["php"] = versions
        results["health"] = health
    elif stage == "web":
        version = core.php_versions(system, True)[-1]
        for host in HOSTS:
            if host not in system.data()["sites"]:
                web.create_site(system, host, version, timezone="America/Mexico_City")
        one, two = [system.data()["sites"][host] for host in HOSTS]
        secret = core.SITES / HOSTS[1] / "private.txt"
        system.write(secret, "private-test-value")
        system.run(["chown", two["user"] + ":" + two["user"], secret], mutate=True)
        code = (
            '<?php header("Content-Type: application/json"); echo json_encode(["php"=>PHP_VERSION,"cross_site"=>@file_get_contents('
            + json.dumps(str(secret))
            + ")]);"
        )
        system.write(Path(one["root"]) / "index.php", code, 0o644)
        output = system.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--resolve",
                HOSTS[0] + ":80:127.0.0.1",
                "http://" + HOSTS[0] + "/",
            ]
        )
        page = json.loads(output)
        assert page["php"].startswith(version + "."), page
        assert page["cross_site"] is False, "Cross-site PHP read succeeded"
        code = system.run(
            ["runuser", "-u", one["user"], "--", "test", "-r", secret], check=False
        )
        denied = subprocess.run(
            ["runuser", "-u", one["user"], "--", "test", "-r", str(secret)]
        ).returncode
        assert denied != 0, "Cross-site account read succeeded"
        system.write(Path(one["root"]) / ".env", "DO_NOT_EXPOSE=1", 0o644)
        for path in ("/.env", "/.git/config"):
            if ".git" in path:
                system.mkdir(Path(one["root"]) / ".git", 0o755)
                system.write(
                    Path(one["root"]) / ".git/config", "DO_NOT_EXPOSE=1", 0o644
                )
            code = system.run(
                [
                    "curl",
                    "--silent",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}",
                    "--resolve",
                    HOSTS[0] + ":80:127.0.0.1",
                    "http://" + HOSTS[0] + path,
                ]
            )
            assert code == "403", (path, code)
        if not one.get("database"):
            web.create_database(system, one)
        credentials = json.loads((core.SITES / HOSTS[0] / ".database.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "client.cnf"
            system.write(
                conf,
                "[client]\nuser="
                + credentials["username"]
                + "\npassword="
                + credentials["password"]
                + "\nhost=localhost\n",
            )
            visible = system.run(
                [
                    "mariadb",
                    "--defaults-extra-file=" + str(conf),
                    "--batch",
                    "--skip-column-names",
                    "-e",
                    "SHOW DATABASES",
                ]
            )
            assert credentials["database"] in visible
        results["http_php_isolation"] = "passed"
        results["hidden_files"] = "passed"
        results["database"] = "passed"
        # Trust a disposable local CA to exercise full custom-certificate validation.
        pki = ROOT / "pki"
        system.mkdir(pki, 0o700)
        if not (pki / "ca.pem").exists():
            system.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "2",
                    "-subj",
                    "/CN=MOS ITC VM Test CA",
                    "-keyout",
                    pki / "ca.key",
                    "-out",
                    pki / "ca.pem",
                ],
                mutate=True,
            )
            system.run(
                [
                    "openssl",
                    "req",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-subj",
                    "/CN=*.mos.test",
                    "-keyout",
                    pki / "site.key",
                    "-out",
                    pki / "site.csr",
                ],
                mutate=True,
            )
            system.write(
                pki / "extensions",
                "subjectAltName=DNS:*.mos.test\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n",
            )
            system.run(
                [
                    "openssl",
                    "x509",
                    "-req",
                    "-in",
                    pki / "site.csr",
                    "-CA",
                    pki / "ca.pem",
                    "-CAkey",
                    pki / "ca.key",
                    "-CAcreateserial",
                    "-days",
                    "2",
                    "-extfile",
                    pki / "extensions",
                    "-out",
                    pki / "site.pem",
                ],
                mutate=True,
            )
        system.write(
            "/usr/local/share/ca-certificates/mos-itc-vm-test.crt",
            (pki / "ca.pem").read_text(),
            0o644,
        )
        system.run(["update-ca-certificates"], mutate=True, timeout=180)
        web.import_certificate(
            system,
            "vm-wildcard",
            (pki / "site.pem").read_text(),
            (pki / "site.key").read_text(),
            (pki / "ca.pem").read_text(),
        )
        for host in HOSTS:
            site = system.data()["sites"][host]
            web.apply_site(
                system, {**site, "ssl": "custom", "certificate": "vm-wildcard"}, site
            )
            code = system.run(
                [
                    "curl",
                    "--silent",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}",
                    "--resolve",
                    host + ":443:127.0.0.1",
                    "https://" + host + "/",
                ]
            )
            assert code == "200", (host, code)
        results["shared_wildcard_https"] = "passed"
    elif stage == "backup":
        repo = ROOT / "restic-repository"
        if not (core.CONF / "backup.json").exists():
            ops.configure_backup(
                system, "rclone:vm-local:" + str(repo), secrets.token_urlsafe(32)
            )
            system.write(core.CONF / "rclone.conf", "[vm-local]\ntype = local\n")
        if not (repo / "config").exists():
            ops.backup(system, "init")
        ops.backup(system, "backup")
        ops.backup(system, "check")
        target = "/srv/mos-itc-test-restore-" + core.stamp()
        ops.backup(system, "restore", target)
        assert (Path(target) / "srv/mos/sites" / HOSTS[0] / "www/index.php").exists()
        assert (
            Path(target) / "var/lib/mos-itc/database-dumps/mariadb.sql"
        ).stat().st_size > 0
        results["restic_rclone_backup_restore"] = (
            "passed (local backend; no external credentials)"
        )
    elif stage == "updates":
        core.refresh(system)
        args, plan = core.upgrade_plan(system)
        (ROOT / "apt-upgrade-plan.txt").write_text(plan)
        if args:
            core.apply_packages(system, args)
        health = core.health_report(system)
        assert "ERROR" not in health, health
        for host in HOSTS:
            code = system.run(
                [
                    "curl",
                    "--silent",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}",
                    "--resolve",
                    host + ":443:127.0.0.1",
                    "https://" + host + "/",
                ]
            )
            assert code == "200", (host, code)
        results["package_updates"] = (
            "passed" if args else "no pending applicable updates"
        )
        results["post_update_health"] = health
    elif stage == "security":
        ops.security(system)
        output = system.run(["fail2ban-client", "status", "sshd"])
        assert "sshd" in output
        firewall = system.run(["ufw", "status"])
        assert "Status: active" in firewall and "22/tcp" in firewall
        ops.record_metrics(system)
        assert json.loads((core.STATE / "metrics.json").read_text())
        system.run(["systemctl", "is-active", "mos-metrics.timer", "mos-clamav.timer"])
        results["firewall_fail2ban_metrics"] = "passed"
    elif stage == "speed":
        result = ops.speedtest(system)
        assert result["download_mbps"] >= 0 and result["upload_mbps"] >= 0
        results["speedtest"] = {
            key: result[key] for key in ("download_mbps", "upload_mbps", "ping_ms")
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument(
        "--stage",
        choices=["install", "web", "backup", "updates", "security", "speed", "all"],
        default="all",
    )
    args = parser.parse_args()
    guard(args.expected_ip)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    results = (
        json.loads(REPORT.read_text())
        if REPORT.exists()
        else {"os": core.os_info()["PRETTY_NAME"], "expected_ip": args.expected_ip}
    )
    system = System()
    stages = (
        ["install", "web", "backup", "updates", "security", "speed"]
        if args.stage == "all"
        else [args.stage]
    )
    for stage in stages:
        print("START " + stage, flush=True)
        try:
            run_stage(stage, system, results)
            results[stage + "_status"] = "passed"
            print("PASS " + stage, flush=True)
        except Exception as exc:
            results[stage + "_status"] = "failed: " + str(exc)
            print("FAIL " + stage + ": " + str(exc), flush=True)
            raise
        finally:
            system.write(REPORT, json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
