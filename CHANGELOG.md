# Changelog

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
