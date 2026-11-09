# Security

This is an early beta administration tool with root-level effects. Test releases
on disposable VMs and keep working provider-console access for remote servers.
An initiative created at ITC does not imply official institutional endorsement.

Report vulnerabilities privately to the repository owner using GitHub private
vulnerability reporting once it is enabled. No contact address is invented here.
Do not open public issues containing keys, passwords, dumps or exploit details.

## SSH administrators

Accounts explicitly created through the administrator menu receive unrestricted
sudo with password authentication. Treat them as trusted server owners. MOS does
not store their passwords or pass them in command arguments. Creation preserves
SSH authentication policy and requires a public key when password login is disabled.
Website accounts do not receive these privileges. Root is not disabled automatically.

## Boundaries

* Root owns the installed code, system configuration and state. No blanket sudoers
  permissions are granted to Python or site accounts.
* PHP runs under separate site accounts and FPM sockets. File permissions,
  restricted Apache overrides and per-site temporary/session paths reduce exposure.
  This PHP hosting path remains on the host, as in version 0.4. FPM pools and `open_basedir` do not fully isolate hostile
  application code. Use a separate application runtime or VM for untrusted PHP.
* SFTP-only mode disables shell commands and forwarding; it is not a filesystem
  chroot. Kernel permissions still govern accessible paths. Existing accounts
  retain their policy on upgrade. Normal SSH/SFTP accounts receive no general sudo; their selected home
  and file permissions do not constitute a filesystem jail.
* Custom certificates must match the private key, be current, match assigned
  hostnames and validate against the host trust store with the provided chain.
  Install private organizational CAs into that trust store explicitly if needed.
  Private keys are unencrypted PEM on disk, protected by root-only permissions,
  so Apache can start without a manual passphrase.
* APT sources are allowlisted conservatively. The explicit external-source flag
  permits existing custom sources but does not create or endorse any repository.
* SSH changes arm a persistent 180-second recovery timer before reloading. Keep a
  second session and provider console available. Use
  `sudo --preserve-env=SSH_CONNECTION mos` when changing settings remotely, and
  preserve the same variable when confirming from a new connection. Without it,
  MOS operates in console mode and cannot verify which SSH session is confirming.
* Configuration backups and database dumps contain secrets and are root-only.
  Off-host Restic backups are encrypted; store the recovery password separately.
* `speedtest-cli` is a third-party test client. The test transfers traffic and
  exposes the public IP to the selected service; failures and approximate results
  are reported without fabricated measurements.

Deleting a site does not revoke unrelated database users, Git credentials or
certificates created outside MOS. Package removal and purge run distribution
maintainer scripts; back up data before removal. MOS does not automatically
delete arbitrary system directories or perform `apt autoremove`.

Installer recovery points contain private system configuration. They are root-only,
are retained on app removal, and are extracted only into a new directory for review.
They do not capture website data, live database state, or an entire disk.

## Database accounts

The database manager changes only MOS-registered databases and accounts. Existing
external local databases support read-only schema/grant inspection without adoption.
The explicitly confirmed SQL console uses the local administrative account and
can mutate data; it has bounded output and a query timeout. Queries/results are
not added to operation history. Additional credentials are root-only;
website primary MariaDB credentials retain their site-user ownership. SQL password
statements use stdin and sanitized errors, never process arguments or activity logs.
Credentials are explicitly revealed on screen and can still be captured by terminal
recording. SSH and database users are separate.

MariaDB grants escape database-name underscores to avoid wildcard access. Older
MOS-created primary account grants are not silently migrated: review existing
grants when upgrading. PostgreSQL administration is database ownership through
a group role, without superuser or server role-creation rights; automatic grants
cover public-schema owner-created objects. PostgreSQL uses the default local cluster
and existing authentication rules. No remote database exposure is enabled.

## Guided backup storage

Profiles and credentials are root-only under `/etc/mos-itc/backups/ID`. Rclone
password obfuscation is reversible. Restic encrypts repository contents; keep
its password off-server too. FTP does not protect login credentials in transit.
TLS and SSH host identity checks stay enabled. Private CAs are scoped to a profile.
Credentials stay out of argv, operation history and review summaries.

Each repository has a local operation lock in addition to Restic remote locks.
MOS never uses `unlock --remove-all` or automatically repairs a repository. Check
other hosts before removing stale locks. Cancellation and timeout terminate the
Restic/Rclone process group. Restores use a new root-controlled directory and
verification; they do not import SQL or overwrite live configuration.

The tested official Rclone component can be explicitly installed using HTTPS and
SHA-256. It does not replace the APT binary or silently update itself. Checksums
do not constitute signature verification. The component must be root-owned and
not writable by other users.

## Normal Linux SSH/SFTP accounts

New non-administrator accounts receive no sudo policy or privileged launcher.
Their home is the selected personal/site directory, with ACL access for shared
site work. Key and password administration remains in MOS; SSH configuration is
checked before reload. Root-owned public key files live under
`/etc/ssh/mos-itc-keys`.

There is no container, chroot or shell sandbox. Users can access other paths when
Linux filesystem permissions allow it. Website FPM isolation continues to use
separate accounts, filesystem permissions and open_basedir. Selecting a home is
not a guarantee of confinement. Existing sudo privileges outside MOS are not
silently changed when assigning an existing account.

## Temporary exports

Only MOS-managed export files are registered for download. Each URL is signed
with a root-only random HMAC key and binds the file ID and expiry. No directory
listing or arbitrary-path handler is provided. The private daemon does not log
URLs; existing Apache access logs may include them, so keep those logs private.
The registry and copies are private. Apache terminates HTTPS with an existing
site's certificate; HTTP requires a separate explicit warning. Anyone holding a valid
link can download the file until it expires. Provider firewall rules remain the
administrator's responsibility.

Cleanup and export registration share a bounded metadata lock. Expired downloads
and abandoned staging files are removed, while retained SQL exports require
explicit deletion. Apache shares ports 80/443 through a reserved download route;
the backend listens only on loopback. Existing legacy links keep their listening
port and expiration until revoked or expired before migration.
The local download daemon does not replace Restic encryption or a secure transport.

## Application upgrades

The update checker reads tags only from the fixed project repository. The chosen
tag is resolved to its immutable commit before the HTTPS source download. A
separate confirmation accepts the upgrade and license. This trusts the project
repository and GitHub HTTPS; it is not a claim of independently signed releases.
The installer locks before capturing prior code/configuration and rejects pending
SSH changes. New features do not automatically reconfigure existing sites.
