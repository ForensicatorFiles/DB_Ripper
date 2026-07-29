# Changelog

All notable changes to DB Ripper (`db_ripper_tui.py` and `db_ripper_cli.py`) are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — breaking changes (e.g. database-profile format changes, command-line usage changes)
- **MINOR** — new features that are backward-compatible (e.g. new detection logic, new screens)
- **PATCH** — bug fixes and other backward-compatible changes that aren't new features

Both the TUI and CLI ship from the same version number, since they share the same underlying
matching/copy logic and are released together.

---

## [Unreleased]

Planned for a future release — not yet implemented:

- Optional hash-on-copy (MD5/SHA-256) with a manifest file written alongside `Exported/`
- Batch mode for running the same profile against multiple extractions unattended

---

## [1.1.0] - 2026-07-29

### Added
- JSON config file support for defining custom database profiles without editing the Python
  source: `load_database_profiles()` looks for `db_ripper_profiles.json` next to the script or in
  the current directory, or an explicit path via `--config` / `-c`.
  - A config's top-level keys that match a built-in profile (`ios`, `android`, `common`) replace
    that profile's database list; new keys become additional selectable extraction types.
  - Leading-underscore keys (e.g. `"_comment"`) are ignored, as a lightweight way to leave notes
    in an otherwise comment-less JSON file.
  - Malformed or schema-invalid config files fail safely: a warning is printed and the tool falls
    back to the built-in defaults rather than crashing.
- `db_ripper_profiles.example.json` added to the repo, demonstrating overriding an existing
  profile, extending the shared "common" profile, and adding an entirely new case-specific
  profile.
- `--config` / `-c` flag on both `db_ripper_tui.py` and `db_ripper_cli.py`.

---

## [1.0.0] - 2026-07-22

Initial public release, both versions renamed from "Extraction Database Locator" to **DB Ripper**.

### Added
- Browse-to-extraction flow supporting both plain folders and `.zip` archives, without needing to
  fully extract a zip to disk first.
- iOS and Android database profiles, plus a shared cross-platform "third-party app" profile
  (WhatsApp, Signal, Telegram, etc.).
- Folder-type targets (e.g. iOS `DCIM`), copied recursively and constrainable to a specific
  parent directory to reduce false positives.
- Collision-safe destination naming when the same filename is matched in more than one location
  in the extraction.
- Automatic WAL (`-wal`) and SHM (`-shm`) sidecar detection and copy for every matched database
  file, so write-ahead-log data isn't silently left behind.
- Live two-phase progress display (scanning, then copying) with a per-target checklist and
  progress bar, run on a background thread so the UI stays responsive.
- `--version` / `-v` flag on both entry points.

### Changed
- Renamed the project and both entry-point scripts from "Extraction Database Locator" /
  `app_tui.py` / `app_cli.py` to **DB Ripper** / `db_ripper_tui.py` / `db_ripper_cli.py`.

### Notes
- This is a triage/collection convenience tool, not a forensic acquisition tool: files are copied
  with `shutil.copy2` (metadata-preserving) but not hashed. See "Unreleased" above for planned
  hash-on-copy support.
