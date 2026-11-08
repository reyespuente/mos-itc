# Validation record — 0.3.0-beta.1

Local suite: **75 passed, no skips**, including input/permission boundaries,
metadata rollback, password redaction, APT approval and network-rate calculations.
Python compilation, Python 3.9 syntax, installer syntax and translation parity passed.

On the authorized Ubuntu 26.04.1 KVM guest (192.168.122.39):

* MariaDB and PostgreSQL 18 were exercised against real local services. Temporary
  databases and users were removed after verification. PostgreSQL was installed
  only in this disposable guest, not on the Linux Mint workstation.
* Both engines passed database/user creation, read-only SELECT and rejected writes,
  allowed row writes and rejected schema creation by writer accounts.
* Administrators created tables but could not create unrelated databases. New
  PostgreSQL owner-created tables/sequences inherited the configured grants.
* Cross-database access was denied, including a MariaDB underscore lookalike.
  Sharing one user across databases and revoking one grant preserved the others.
* Permission downgrade took effect on a fresh connection. Password rotation
  rejected the old password and accepted the new one. SQL exports were nonempty
  and mode 0600. User/database deletion and cleanup passed.
* All 75 automated tests passed on the VM's Python 3.14, with no skips.
* Installer upgrade to 0.3.0-beta.1 created a configuration restore point and
  preserved the existing two HTTPS sites. Native menus and live dashboard opened.
* Real CPU/memory/swap/uptime/process/service data were checked. Palette behavior
  was verified in xterm-256color; a native Konsole session was not available.

Limits: the database manager targets the default local MariaDB service and
PostgreSQL cluster. Existing external databases are inventory-only. PostgreSQL
automatic object grants target public; external schemas, role changes and custom
clusters require separate review. The real server was not changed by these tests.

# Validation record — 0.2.1-beta.1

Checked on November 8, 2026. Local offline suite: **63 passed, no skips**,
including Python 3.9 syntax, catalog parity, OpenSSL chain/key checks, rollback
and native PTY/nano repeated-editor regression.

On the authorized Ubuntu 26.04.1 KVM guest (`192.168.122.39`, Python 3.14):

* Installer upgrade to 0.2.1-beta.1 preserved existing sites and created a recovery point.
* All 20 certificate/administrator regression tests passed, including three
  consecutive real nano sessions and real visudo validation.
* Replaced the wildcard certificate shared by two existing sites; Apache reloaded
  and both trusted HTTPS requests returned 200. Private key permissions were 0600.
* Created a temporary Bash/home/sudo administrator, connected over SSH using its
  public key, and verified password-required sudo before and after password change.
* Removed the temporary account and sudo policy, restored the scheduled ClamAV
  scan and refreshed metrics. No server services were installed on the Mint host.

The real server has not been updated by these checks. Public Let's Encrypt,
other operating-system releases and future upstream changes remain outside this
validation. The previous release's broader acceptance record follows.

# Validation record — 0.2.0-beta.1

Validated on the explicitly authorized local Ubuntu 26.04.1 KVM guest. The Linux
Mint workstation was used for editing and offline checks only; no server packages
or host network configuration were changed there.

| Check | Result |
|---|---|
| Offline unit/integration tests | 43 passed, no skips |
| Python 3.9 syntax and compilation | Passed |
| Installer shell syntax | Passed |
| English/Spanish keys and named placeholders | Passed |
| Native curses screen and navigation | Opened and exercised; no continuation pauses |
| Default stack from official Ubuntu sources | Installed successfully; PHP 8.5 discovered dynamically |
| Real Apache PHP requests | Passed |
| Per-site Unix/PHP read isolation | Passed |
| `.env` and `.git` HTTP access | Denied as expected |
| MariaDB site account/database | Created and accessed successfully |
| Custom wildcard shared by two HTTPS sites | Passed with a disposable trusted test CA |
| Restic + Rclone backup/check/restore | Passed using Rclone local backend and real database dump |
| Real package upgrades | Applied; configuration tests and both HTTPS sites still passed |
| UFW, Fail2ban and hourly metrics timers | Passed |
| ClamAV engine/signatures and website scan | Passed; engine 1.5.4, 3,628,258 signatures, 13 files, zero infections |
| Real speedtest-cli network test | Passed; measured values stored only on the test VM |
| Ubuntu socket-activated SSH port change | New key-authenticated connection to port 2222 passed |
| SSH automatic rollback | Waited 180 seconds; new connection to original port 22 passed |
| SSH confirmation from a new connection | Passed |
| Application upgrade retaining sites/settings | Passed |
| `mos-itc` command and `mos` alias | Installed and checked |
| Minimal login banner | Wordmark plus `Control panel: sudo mos-itc`; no personal/institute name |
| Installer restore-point creation | Created before upgrade; extraction checked separately |

Limits: public Let's Encrypt issuance was not attempted on the private NAT guest.
External S3/SFTP/Rclone credentials were not supplied; only a local Rclone-backed
repository was tested. Other supported OS releases and future breaking upstream
changes need their own staging validation. This is a tested beta, not a universal
production certification. The test guest contains two demo sites and a disposable
CA/certificate used for these checks.
