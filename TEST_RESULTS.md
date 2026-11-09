# Validation record — 0.4.0-beta.1

Validated November 8, 2026 (America/Mexico_City). **98 local tests passed, no
skips**; compilation, Python 3.9 syntax, installer shell syntax and catalog parity
also passed. On the disposable Ubuntu 26.04.1 AWS guest, 98 tests ran with two
Apache parser tests skipped because Apache was not installed. On a second,
fresh Debian 13.4 AWS guest, all 98 tests passed without skips (Python 3.13).

* Fresh installation cancellation left the app uninstalled. Accepted fresh
  installation and upgrades created configuration recovery points.
* App-only uninstall restored the original Ubuntu login header. A fresh reinstall
  verified the revised Essential-file handling without the dpkg-divert warning.
  Stored backup configuration and passwords retained the same hashes.
* S3 tests used dedicated random prefixes in the supplied IDrive e2 test bucket,
  without modifying unrelated repositories. Write/read/delete, initialization,
  encrypted backup, selected snapshot restore, SHA-256 comparison and full data
  verification passed.
* The actual guided S3 form, credential redaction and repository initialization
  passed, followed by the recommended content backup and the wizard-selected
  restore. Recovered files matched SHA-256; /etc/hostname and the restore manifest
  were present, and the target directory remained mode 0700.
* IDrive ETags caused false MD5 mismatches with the newer generic S3 transport.
  Multipart content-checksum metadata resolved initialization and passed verified
  backup/restore and full-data checking without disabling content verification
  or changing bucket encryption. Initialization now has a 180-second deadline.
* A missing selected source aborted before remote transfer, recorded failure, and
  succeeded after correction. Reconnection to an existing repository with its
  original password and shared local lock identity passed.
* A real active Restic lock blocked an exclusive check. Abruptly terminated only
  the test holder, removed the stale lock without --remove-all, then checked the
  repository successfully. Duplicate local operations were rejected.
* Daily, weekly and hourly systemd calendars were configured and validated; test
  schedules were disabled afterwards.
* Distribution Rclone 1.60.1 hung on SFTP restore. The verified private 1.76.0
  component installed by MOS passed SFTP, FTP, explicit FTPS and implicit FTPS
  write/read/delete, initialization, backup, selected restore SHA-256 and full
  data checking. No comparison-only transport overrides were needed in the final
  pass. Destination directories were created by MOS.
* FTP test servers listened only on loopback. Temporary users and server processes
  were cleaned up. SFTP pinned host identities. FTPS rejected an untrusted test
  certificate and passed with an explicitly trusted test CA, with TLS checks enabled.
* MOS independently downloaded, verified SHA-256 and atomically installed its
  private upstream Rclone component without replacing the APT package. Runtime
  checksum rejection, profile editing and legacy config selection passed locally.
* Debian EC2's official `mirror+file` source lists initially blocked installation.
  The installer and application now inspect every listed mirror, accept official
  Debian cloud destinations and still reject mixed, empty or untrusted lists.
  Installer cancellation, fresh installation and an upgrade passed on Debian.
* Debian passed the guided S3 connection, initialization, recommended backup,
  selected restore, SHA-256 comparison, full check and existing-repository reconnect.
  Native SSH menus and the contents summary opened on both distributions.
* Real Debian MariaDB and PostgreSQL services passed account creation, read/write
  privilege boundaries, cross-database isolation, password rotation and cleanup.
  A database-only backup exported both engines; after deleting the two probe
  databases, manually importing the recovered full SQL dumps restored their exact
  original data. The full PostgreSQL import reported only the expected duplicate
  built-in postgres role; no other SQL import errors occurred.
* Debian daily, Monday-weekly and six-hour timers were enabled and checked, then
  disabled. A stopped PostgreSQL cluster prevented the backup; after restarting
  that cluster, a new backup completed successfully and recorded success.

Limits: no production server was changed. Internet S3 validation covered IDrive e2;
other providers require their own acceptance run. File transports used controlled
loopback services. SQL export and manual import were validated on Debian; the
Ubuntu AWS guest had no database engines installed. Restores extract and
verify files; importing SQL and reactivating services remain administrator tasks.
Other distributions and future upstream versions require staging validation.

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
