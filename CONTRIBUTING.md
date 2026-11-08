# Contributing

MOS ITC is a local Debian/Ubuntu administration tool. Keep changes understandable
to the administrator who will have to recover a server over SSH.

* Use Python 3.9+ standard library unless a dependency solves a concrete problem.
* Keep system operations in `core.py`, Apache/TLS in `web.py`, other service
  operations in `ops.py`, and terminal interaction in `ui.py`.
* Use argument lists for subprocesses. Do not introduce `shell=True` or interpolate
  user input into shell snippets, SQL identifiers, Apache directives or paths.
* Every new mutation needs a useful preview and a confirmation at its entry point.
  Scheduled operations are explicitly authorized when their timer is enabled.
* Validate configuration before reloading. Never claim that package installation
  is transactional: APT maintainer scripts cannot be generally rolled back.
* Do not store private keys, passwords, `.env` files, database dumps or machine
  state in this repository. Do not include credentials in command arguments/logs.
* Place user-facing messages in `languages/en.json` and `languages/es.json`.
  Keep existing keys stable and use named placeholders such as `{domain}`.
  Preserve placeholders between languages; translate whole sentences instead of
  joining translated fragments. Product names and native command output remain
  unchanged. The bootstrap shell installer is bilingual before Python is installed.
* Add meaningful checks for trust boundaries, recovery and generated configuration.
  Do not mock away the behavior a test is supposed to verify.

Run checks from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mos_itc
bash -n install.sh
```

Run destructive/manual integration tests only on disposable VMs. The supported
OS list is a target matrix, not evidence that every release has been tested.
