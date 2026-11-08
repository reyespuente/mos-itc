# Changelog

## 0.2.1-beta.1

* Fix repeated nano sessions crashing curses during certificate import.
* Add certificate drafts, per-field PEM guidance, server-file imports, automatic
  fullchain separation and public metadata review. Preserve drafts after errors.
* Validate all assigned domains on certificate replacement; roll back certificate
  files and inventory if Apache validation/reload fails.
* Add local SSH administrators with password-required full sudo, public keys and
  password changes. Keep root, site accounts and SSH authentication policy separate.
* Add native terminal, real OpenSSL and administrator safety tests.
