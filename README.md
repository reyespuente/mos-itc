# MOS ITC

**Manage your Own Server — Instituto Tecnológico de Celaya**

A Python terminal toolbox for administering Debian and Ubuntu web servers.
Created by **Reyes Guadalupe Puente Balcazar** as an initiative at the Instituto
Tecnológico de Celaya to make server administration easier from one place.
Free to use under the MIT license. This is not a claim of official ITC endorsement.

**Version: 0.2.1-beta.1.** Start on a disposable VM. Offline tests and Ubuntu 26.04 VM acceptance results are included; the complete
distribution matrix has not yet been validated.

The terminal banner uses ITC's published blue `#1B396A` and green `#117533`:
[institutional identity](https://celaya.tecnm.mx/identidad-grafica/).
Set `NO_COLOR=1` for monochrome output.

```text
 __  __  ___  ____    ___ _____ ____
|  \/  |/ _ \/ ___|  |_ _|_   _/ ___|
| |\/| | | | \___ \   | |  | || |
| |  | | |_| |___) |  | |  | || |___
|_|  |_|\___/|____/  |___| |_| \____|
```

## Try the interface without installing anything

From this directory:

```bash
python3 -m mos_itc --dry-run
python3 -m mos_itc --dry-run --language es
python3 -m mos_itc banner
python3 -m mos_itc doctor
```

Preview runs on Linux Mint too. It reads real package inventory but replaces
mutating operations with printed plans and in-memory file changes. It does not
run a network speed test, generate mock measurements or install services.

Menus support arrows, Enter, Escape and Page Up/Down. Checklists use Space to
toggle and Tab to reach the pinned Apply button. Forms and review buttons remain
in the same screen; there are no extra “press Enter to continue” pauses.
Small/dumb terminals can use numbered input. A 90×30 terminal is comfortable.

## Install inside the Debian/Ubuntu VM

Target systems: x86_64 with systemd and APT:

* Debian 11, 12 and 13.
* Ubuntu Server 22.04, 24.04 and 26.04 LTS.

Start validation with Debian 13 and Ubuntu 24.04. Debian 11 has left its normal
LTS period; its presence in the target matrix does not guarantee security support.
Do not install the administration backend directly on your Linux Mint workstation.

Copy this directory into the VM, enter it, then run:

```bash
sudo bash install.sh
sudo mos-itc
```

The installer asks whether to continue, shows attribution and the complete MIT
license, requires `ACCEPT`, checks the host, refreshes APT and installs the small
Python CLI runtime. Installing/configuring the web stack is a separate menu action.
The main command is `mos-itc` in `/usr/local/bin`; `mos` remains a compatibility
alias. No shell profile edits or blanket sudo permissions are needed. SSH login
shows only the MOS ITC wordmark and `Control panel: sudo mos-itc`, with the command
in ITC green. System update and reboot notices remain available. The main app
shows the institute and Reyes Puente; About contains full attribution and MIT.

Before package or code changes, the installer creates a root-only restore point
under `/var/backups/mos-itc/restore-points`: `/etc`, package inventory, manual package
selection, service inventory, and the previous app when present. This is a
configuration recovery point, **not a disk snapshot or a database backup**.

```bash
sudo mos-itc restore-point list
sudo mos-itc restore-point extract POINT_NAME /srv/mos-recovery
sudo bash install.sh --upgrade
```

Extraction requires a new directory and never overwrites the running system.
Symbolic/hard links are listed in `LINKS.json` instead of followed. Inspect files,
permissions, ownership, service state and required package versions before copying
anything back. Use Restic for website/database data and a provider snapshot when
full-machine rollback is required. Keep restore points private: `/etc` contains keys.

A standalone copy of the installer can fetch a published release automatically:

```bash
sudo bash install.sh --repo reyespuente/mos-itc --ref v0.2.1-beta.1
```

Download the installer as a file before running it. Interactive input is required;
`curl | bash` is intentionally refused. Prefer a reviewed release/commit. Source: https://github.com/reyespuente/mos-itc. Releases are not signed; there is no automatic update service.

## Features

| Menu | Behavior |
|---|---|
| Install web server | Default stack or custom package checklist; APT simulation and approval before applying |
| Installed packages | Grouped package tree, complete dpkg inventory, install/remove/purge plans |
| Websites | Create/edit/remove Apache hosts, optional `www`, static/PHP/Laravel layout, per-site database |
| Git deployment | Clone public HTTPS Git repositories as the site user into versioned releases; optional Composer install and activation |
| SSL certificates | HTTP, Let's Encrypt HTTP-01, custom PEM with optional CA bundle, shared wildcard assignments, expiry warnings |
| Automatic configuration | Timezone, hostname, PHP CLI default, production PHP settings, Apache hardening, scheduled metrics and scans |
| Security | UFW rules preserving existing rules, Fail2ban SSH jail, scheduled/manual ClamAV reports |
| SSH administrators | Create local SSH accounts with password-required full sudo; change passwords and add public keys |
| SSH | Public keys, per-site SSH/SFTP access, configurable port and password authentication, automatic rollback |
| Performance | Live CPU/RAM/disk bars, network counters, active services and hourly history for seven days |
| Backups | Restic encryption/incremental snapshots using Rclone, SFTP or S3-compatible storage; database dumps and daily scheduling |
| Make a speed test | Install `speedtest-cli` from APT after approval, then display download/upload Mbps, ping, ISP and server in a bordered card |
| Language | English and Latin American Spanish from separate JSON catalogs |
| Uninstall | Remove MOS while retaining services, or explicitly remove managed sites/data and select packages to uninstall |

Default stack: Apache, MariaDB, available PHP-FPM versions with CLI, MySQL, curl,
mbstring, XML, zip, GD, intl, bcmath and OPcache; Composer, Certbot, Node.js/npm,
ClamAV, Fail2ban, Git, unzip, curl, ACL tools, nano, Rclone, Restic, UFW and OpenSSL.
Redis, PostgreSQL, Nginx and speedtest-cli are selectable. Nginx package management
is provided; **MOS-managed virtual hosts currently use Apache**. The menu blocks
an Apache/Nginx port collision instead of installing a competing listener.

### PHP availability

MOS does not silently add a PPA or third-party repository. It discovers installed
and available `phpX.Y-fpm` packages from dpkg/APT, sorts branches numerically, and
checks which extension packages actually exist. There is no hardcoded upper PHP
branch. The newest available branch becomes the CLI default after installation;
websites keep their own selected FPM version.

Software & updates includes a simulated, reviewed upgrade with removals blocked.
MOS simulates the plan again immediately before applying it and aborts if package
or dependency versions changed. Existing configuration is kept, and Apache/PHP/SSH
configuration checks run after a requested update. Future upstream breaking changes
cannot be guaranteed compatible: validate new releases on your staging VM first.

Existing nonstandard sources or custom mirrors require the explicit
`--allow-external-repositories` flag and a responsibility acknowledgement.
This permits already-configured sources; it does not install or endorse a PPA.

### Websites and access

Website homes live under `/srv/mos/sites/DOMAIN`, with a deterministic Linux
account for each domain. Accounts start without interactive login. Apache uses
ACLs for public-file access; PHP-FPM runs under the site's own account, with a
private socket and site-specific temp/session directories. Document roots must
stay inside that site's home. Site accounts receive no sudo privileges.

Creating a Laravel layout prepares `public/`; it does not create an application.
Git deployment can run Composer as the site account. Configure `.env`, writable
storage, migrations, queues and application-specific tasks separately. Private
Git authentication and Node.js reverse-proxy hosting are not implemented in this
beta. Ordinary SSH/SFTP and user-run Git remain available.

The beta import workflow copies an existing static/PHP document root to a new
managed site and disables the old vhost, retaining its files/configuration.
It refuses symbolic links and special files, does not migrate custom Apache
directives, TLS or existing database credentials, and needs a separate backup.

### Certificates

The certificate form shows separate fields for the **certificate**, **private key**,
and optional **intermediate CA bundle**. Select a field to paste/edit PEM in nano
or import an existing file from the server. A fullchain is split automatically.
Review public certificate details and affected sites before applying; private keys
are never shown in summaries. Invalid input keeps the draft for correction.
Both RSA and EC private keys are accepted. Encrypted private keys are refused for
unattended Apache startup. Chains must validate against the server trust store.
Replacement checks Apache and restores the previous files/state if reload fails.

A custom wildcard can be assigned to multiple matching domains. Replacement checks
all existing assignments before saving. Warnings appear within 30 days of expiry.
Let's Encrypt issuance uses HTTP-01: real DNS and externally accessible TCP/80 are
required. Custom wildcards are supported; automated DNS-01 wildcard issuance is
not included. Renewals use Certbot's timer and a configuration-checking deploy hook.

### SSH administrators

Use **SSH administrators** or `sudo mos-itc users` to create an account with a
home directory, Bash and full sudo requiring its password. Enter the password
twice and optionally add a public SSH key. A key is required when the SSH daemon
disables password login. Passwords are not stored in MOS state or command arguments.
Existing local administrators can receive new keys or password changes. Site
accounts and root are excluded. Creating an account does not change SSH policy.

### SSH recovery

When changing SSH remotely, preserve session information:

```bash
sudo --preserve-env=SSH_CONNECTION mos-itc ssh manage
```

MOS arms a persistent recovery timer before applying changes. Open a **new SSH
connection** on the selected port, then use the exact token MOS prints:

```bash
sudo --preserve-env=SSH_CONNECTION mos-itc ssh confirm TOKEN_PRINTED_BY_MOS
```

If unconfirmed, old SSH settings return after 180 seconds. Existing firewall
rules, including the old SSH allow rule, are retained. Cloud-provider firewalls
are independent: open the new port there first. See [SECURITY.md](SECURITY.md).

Both service-based SSH and Ubuntu socket activation are supported. Socket changes
preserve existing bind addresses and update a dedicated systemd drop-in. The
recovery timer restores both the daemon configuration and socket configuration.
MOS refuses a socket restart if its KillMode could terminate established sessions.

SFTP-only means forced internal SFTP and no forwarding; it is **not a chroot**.
Use stronger isolation before hosting mutually untrusted student workloads.

### Backups

Install the backup packages, configure Rclone's remote in nano or use a native
S3/SFTP Restic URL, choose a strong password, then explicitly initialize the repo.
Example repository forms:

```text
rclone:remote:mos-itc
s3:https://s3.example.org/bucket/mos-itc
sftp:backup@backup.example.org:/srv/backups/mos-itc
```

Backups include `/etc`, MOS state and managed site homes. MariaDB and PostgreSQL
are exported before backup when installed; live database directories are not used
as a substitute for database dumps. MariaDB's transactional dump does not guarantee
consistency for nontransactional tables or across external application services.
Coordinate application writes when an application-consistent restore is required.

Daily scheduling is opt-in. No automatic pruning runs. Restore goes to a new
directory and never overwrites `/` or an existing target. File/database restoration
and service activation require administrator review. Keep the Restic password
offline and practice restoration.

## Commands

```bash
mos-itc --help
sudo mos-itc --language es
sudo mos-itc install-web
sudo mos-itc website create demo.example.org --php 8.4 --www --database
sudo mos-itc website list
sudo mos-itc ssl
sudo mos-itc speedtest
sudo mos-itc backup run
sudo mos-itc backup snapshots
sudo mos-itc backup check
sudo mos-itc backup restore /srv/mos-restore
sudo mos-itc uninstall
```

Global options come before the command. Interactive confirmations remain enabled;
scheduled backup/scan/metrics operations are authorized when their timer is enabled.
The early CLI is not yet a fully unattended provisioning API.

## Project layout

```text
languages/
  en.json                English messages, stable keys and named parameters
  es.json                Latin American Spanish, same keys/parameters
mos_itc/
  cli.py                 Arguments, privilege checks, command routing
  core.py                Process/file boundary, locks, APT planning
  i18n.py                Catalog loading and English fallback
  web.py                 Sites, PHP-FPM, databases, certificates
  users.py               Local SSH administrators and password-required sudo
  ops.py                 Security, SSH, telemetry, backups, deployment
  ui.py                  Screen flows, forms and result cards
  terminal.py            Shared curses navigation, inputs and review widgets
  recovery.py            Installer restore-point inspection and safe extraction
assets/itc-logo.json      Colored ASCII emblem derived from the supplied reference
tests/test_mos.py         Offline safety and configuration tests
install.sh               Interactive installer and optional GitHub download
```

Installed application: `/opt/mos-itc`. Configuration/secrets: `/etc/mos-itc`.
State/history/reports: `/var/lib/mos-itc`. Configuration backups:
`/var/lib/mos-itc/config-backups`. Retained secrets/state are not erased by
application-only uninstallation. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[TESTING.md](TESTING.md) before changing or deploying the tool.

Author: Reyes Guadalupe Puente Balcazar. License: [MIT](LICENSE).

## Publishing

Publish changes to https://github.com/reyespuente/mos-itc including `.github/`.
Exclude runtime files and credentials; `.gitignore` is included. The GitHub
installer accepts your actual repository and release/commit through `--repo` and
`--ref`. Push the release tag before using it with the installer.
