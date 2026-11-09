# Validation record — 0.5.0-beta.1

Validated November 9, 2026 (America/Mexico_City). **121 local tests passed**.
The installed candidate passed all 121 on Debian 13.4 / Python 3.13. Ubuntu
26.04.1 / Python 3.14 ran 121 with two Apache parser tests skipped because Apache
was absent. Python 3.9 syntax, compilation, shell syntax and language parity passed.
GitHub CI for this release remains pending publication.

Download/locking audit on November 9:

* Debian: real signed downloads through Apache HTTP 80 and HTTPS 443, including
  a managed HTTP vhost with HTTPS redirects. Existing IP/domain responses matched.
  Altered and expired tokens returned 404, expired files were removed, and the
  backend listened only on loopback.
* A temporary real website was created and removed on Debian inside nested
  operation locks; the temporary website data was erased afterward.
* Regression tests cover nested lock ownership, exclusion of other processes,
  release after exceptions, rollback of rejected Apache configuration, idempotent
  route setup, preservation of legacy links and service/route cleanup on uninstall.
* Both test instances accepted the application upgrade. No pending SSH change or
  active MOS operation lock was found during inspection; the historical reported
  message was not reproduced. Messages now distinguish active operations from
  SSH changes awaiting verification.
* This audit focused on downloads, locks, SSH pending-change handling and uninstall;
  it is not a guarantee that every possible server configuration is supported.

* Upgraded existing 0.4 installations on both disposable EC2 guests. Existing
  configuration/site/credential file hashes matched before and after; prior code
  and configuration recovery archives were created.
* A representative Debian 0.4 fixture included HTTPS static/PHP8.4 sites, a custom
  certificate, both database engines, database accounts, an SSH administrator and
  the original automatic profile. Its 22 files matched after upgrade; HTTPS,
  PHP/FPM, the existing administrator login and both databases remained usable.
* The corrected account model passed on both EC2 guests: normal Bash SSH,
  personal home directories, two accounts reading/writing the same site, native
  SFTP and effective denial of sudo. Existing candidate test accounts were
  converted without moving their files. No container runtime is required or
  installed by the release. The home is a starting directory, not confinement.
  The corrected package was then installed and retested on both rebooted guests:
  all 115 tests passed on Debian, and Ubuntu retained only the two Apache skips.
  Existing configuration/site hashes matched exactly across this final upgrade.
* HTTP and HTTPS downloads passed content checks, strict TLS verification,
  signature-tampering rejection, revocation, stable-port reuse and expiration.
  ZIP/tar.gz contents and staging cleanup passed. Test HTTPS used a proper private
  test CA. Public Internet download reachability behind provider firewalls was not
  claimed; transfer checks used the guest's loopback listener.
* Existing 0.4 S3 repositories remained readable. Added a test snapshot, deleted
  only that new snapshot, pruned and checked the repository; prior snapshots were
  retained. Older IDrive profiles used runtime compatibility flags without changing
  stored configuration or passwords. Transient SQL preparation is removed on exit.
* Both SQL consoles and external local database inspection passed. PostgreSQL
  deletion with an active query terminated that connection after the SQL safety copy.
* PHP form changes affected a real FPM site, survived pool regeneration, and were
  restored to their previous values. Reapplying the legacy automatic profile was refused.
* Both guests passed protected UFW add/delete, refused SSH denial, Fail2ban live
  parameter checks and a configurable daily Certbot timer. Actual SSH port changes
  armed persistent recovery; expiring the test metadata and triggering its service
  restored port 22 and the exact original SSH files. Control connections survived.
* Both recovery-point types were listed and inspected. Configuration recovery
  extracted into a new mode-0700 directory; only a generated test point was deleted.
* Native SSH main/overview/dashboard screens opened, and terminal resizing through
  140x42, 110x32, 80x24, 58x20 and 40x15 completed without a traceback. CLI captures
  are included in assets. Installer Enter cancellation was checked at initial,
  license and upgrade approvals without creating a recovery point.
* Published-tag discovery worked without performing an unconfirmed upgrade.
  The in-app updater's pinned-commit handoff/restart is regression-tested; the
  public 0.5 download cannot be exercised until its tag is published.

Limits: no production server was changed. Live Let's Encrypt issuance/renewal
against a public owned domain was not exercised; timer configuration and custom
certificate replacement were tested. PHP hosting keeps the prior host FPM runtime;
normal Linux accounts use filesystem permissions, not a jail. Other distributions,
providers and future upstream versions still require their own acceptance checks.

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
