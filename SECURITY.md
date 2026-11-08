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
  This is shared-kernel hosting, **not a container sandbox**. FPM pools and
  `open_basedir` do not fully isolate hostile code. Untrusted student workloads
  should use stronger isolation before being offered a hosting service.
* SFTP-only mode disables shell commands and forwarding; it is not a filesystem
  chroot. Kernel permissions still govern accessible paths. Site users never get sudo.
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
external databases are inventory-only. Additional credentials are root-only;
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
