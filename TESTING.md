# Validation and VM acceptance

## Automated checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mos_itc
bash -n install.sh
```

Tests cover input boundaries, APT dependency-removal protection, official-source
checks, dry-run behavior, file rollback, duplicate domains, per-site PHP paths,
real OpenSSL key mismatch detection, SSH confirmation/recovery, metrics retention,
speed measurement conversion, JSON translation parity/parameters, Python 3.9
syntax, and generated Apache HTTP syntax when Apache is installed. Apache's test
uses a temporary configuration without opening a listening socket.

CI runs offline tests on multiple Python versions. It is not a substitute for
installing the tool on the target distributions. Speed tests and remote backups
are not performed by CI.

## Disposable VM checklist

Take an offline `clean-install` snapshot first. Use a local VM console for SSH
changes. Begin with Debian 13 and Ubuntu Server 24.04, 2 vCPU, 3 GiB RAM and a
40 GiB QCOW2 disk; run one VM at a time on an 8 GiB host.

1. Cancel the installer before acceptance; verify no packages/files were changed.
2. Accept the license, install, and verify `sudo mos-itc doctor` and `sudo mos-itc banner`.
3. Open English and Spanish menus; scroll package trees and toggle a selection.
4. Install the default stack. Review the simulation and installed PHP versions.
5. Run automatic configuration, using the correct timezone/hostname. Check
   Apache, PHP, MariaDB and all enabled timers with `systemctl`.
6. Create two PHP sites. Verify vhost routing using `curl --resolve`, then attempt
   cross-site file access as each Linux account. Verify a public symlink cannot
   expose a file owned by a different site. Check .env and directory listing denial.
7. Create a MariaDB database; verify only its site account can read `.database.json`.
8. Deploy a public Git test project. Verify Composer executes as the site user and
   the document root remains inside that site home. Test Laravel public/storage.
9. Import a matching custom certificate and chain. Reject a mismatched key,
   expired certificate, wrong hostname and replacement breaking an existing alias.
10. Test Let's Encrypt on a real VPS/domain with public TCP/80; validate renewal
    with Certbot's dry-run. A local NAT VM alone cannot satisfy public HTTP-01.
11. Add/test an administrator SSH key. Change SSH port and let the recovery timer
    expire. Verify the old port returns. Repeat and confirm from a new connection.
    Test recovery over a reboot, and password-login disablement separately.
12. Verify provider-firewall and UFW rules separately; confirm SSH remains open.
    Validate Fail2ban's sshd jail without banning your only administration address.
13. Update ClamAV signatures and run a scan. Check a safe antivirus test sample,
    permission errors and exit codes without using real malware.
14. Run speedtest-cli; check Mbps conversion and graceful network-service failure.
15. Back up to a dedicated test repository, modify a file/database, back up again,
    list snapshots and restore into a new directory. Recover a database from its
    SQL dump. Verify the Restic password and Rclone config are not world-readable.
16. Check 168-hour retention and one sample per UTC hour with `mos-metrics.timer`.
17. Reject removal of a PHP version used by a managed site. Review actual APT
    package/dependency plans, remove versus purge behavior, and database preservation.
18. Test beta import on a copy of an existing site; compare content and routing.
19. Remove only MOS; verify sites/services and Certbot renewal still work. Restore
    the clean VM and test explicit site/data deletion and selected package removal.

## Target matrix

| OS | State |
|---|---|
| Ubuntu 26.04.1 | Real KVM acceptance run completed; see TEST_RESULTS.md |
| Debian 13 / Ubuntu 24.04 | Separate VM acceptance pending |
| Debian 12 / Ubuntu 22.04 | Separate VM acceptance pending |
| Debian 11 | Compatibility target only; review extended security coverage |

Do not mark a row validated until the checks were actually completed on that OS.
The initial release is a testable beta, not a production certification.
