# Changelog

## 0.4.0-beta.1

* Add guided S3/S3-compatible, SFTP, FTP/FTPS and existing-Rclone backup profiles,
  connection tests and editing, content presets and configurable schedules.
* Add snapshot selection for verified recovery into a new private directory.
* Add repository locks, interruption-aware status, redacted event history,
  stale-only unlocking and explicitly confirmed index repair.
* Export local PostgreSQL clusters by port; fail rather than silently skip missing
  selected paths or failed database exports.
* Offer a verified private Rclone component for tested transport compatibility,
  preserving the APT package and legacy repository settings.
* Handle IDrive ETags through multipart content-checksum metadata and bound
  initialization time; keep content validation enabled.
* Preserve the Ubuntu MOTD header without dpkg-divert renaming an Essential file.
* Validate Debian EC2 mirror lists in both the installer and package manager.

## 0.3.0-beta.1

* Add MariaDB and PostgreSQL database menus, SQL exports and confirmed deletion.
* Add multiple local users per database, scoped permission profiles, password
  rotation, shared accounts, revocation and protected credential display.
* Expand the live dashboard with load, uptime, swap, inodes, network rates,
  service states and largest-memory processes; avoid locking mutation tasks.
* Use standard terminal palette slots for compatibility across emulators.
* Simplify the README and add a current interface capture.

## 0.2.1-beta.1

* Fix repeated nano sessions crashing curses during certificate import.
* Add certificate drafts, per-field PEM guidance, server-file imports, automatic
  fullchain separation and public metadata review. Preserve drafts after errors.
* Validate all assigned domains on certificate replacement; roll back certificate
  files and inventory if Apache validation/reload fails.
* Add local SSH administrators with password-required full sudo, public keys and
  password changes. Keep root, site accounts and SSH authentication policy separate.
* Add native terminal, real OpenSSL and administrator safety tests.
