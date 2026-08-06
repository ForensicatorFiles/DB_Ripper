# DB Ripper

```
=====================================================
    ____  ____     ____  ________  ____  __________
   / __ \/ __ )   / __ \/  _/ __ \/ __ \/ ____/ __ \
  / / / / __  |  / /_/ // // /_/ / /_/ / __/ / /_/ /
 / /_/ / /_/ /  / _, _// // ____/ ____/ /___/ _, _/
/_____/_____/  /_/ |_/___/_/   /_/   /_____/_/ |_|
-----------------------------------------------------
 Database Extraction Toolkit  --  v1.0.0
=====================================================
```

A dependency-free terminal tool for locating and extracting SQLite databases — including their WAL/SHM sidecars — out of iOS and Android mobile forensic extractions.

Point it at a folder or a `.zip` of an extraction, pick iOS or Android, choose which databases you want, and it recursively finds and copies matches (plus any active `-wal`/`-shm` files sitting next to them) into an `Exported/` folder.

Ships in two forms:

| | Description |
|---|---|
| `db_ripper_tui.py` | Full-screen terminal UI — arrow-key navigation, checkbox database picker, live scanning/copying progress with a checklist and progress bar |
| `db_ripper_cli.py` | Plain linear prompts — type a path, pick a number, confirm. No navigation, nothing to explain. |

Both use the exact same matching/copy logic under the hood and produce identical results — pick whichever fits how you work.

---

## Why

Full logical or file-system extractions bury the databases you actually care about (`sms.db`, `Cache.sqlite`, `contacts2.db`, WhatsApp's `msgstore.db`, etc.) somewhere in a tree of thousands of files. DB Ripper walks the extraction for you and pulls out just the matches — including the WAL/SHM sidecars that hold un-checkpointed writes and are easy to leave behind by accident.

This is a **triage and collection convenience tool**, not a forensic acquisition tool. Files are copied with `shutil.copy2` (metadata-preserving) but are not hashed. Treat it as a fast first pass and follow your normal verification process on the output.

---

## Requirements

- Python 3.9+
- Nothing else. Both scripts use only the standard library — no `pip install`, no virtualenv, no `requirements.txt`.
- * There are some versions of Python that do not have curses natively. On Windows, run 

```
pip install windows-curses
```

The TUI requires a real terminal (curses needs a TTY) — it won't run inside most IDE consoles or non-interactive shells. The CLI runs anywhere Python does, including over SSH or piped input.

---

## Quick start

```bash
git clone https://github.com/<your-username>/db-ripper.git
cd db-ripper

# Terminal UI
python3 db_ripper_tui.py

# or, plain command-line prompts
python3 db_ripper_cli.py
```

### TUI controls

| Key | Action |
|---|---|
| `↑`/`↓` or `j`/`k` | Move cursor |
| `Enter` | Open folder / confirm selection |
| `Space` | Toggle checkbox (database picker) |
| `a` | Toggle all (database picker) |
| `o` | Select current folder as the extraction root (browser) |
| `p` | Type a path manually (browser) |
| `d` | Jump to a drive / mount point (browser) |
| `q` / `Esc` | Back / quit |

### CLI flow

Answer four prompts in order: extraction path → iOS/Android → comma-separated database numbers (or `all`) → confirm. Results print as a plain list when it's done.

---

## What it does, step by step

1. **Browse to an extraction** — a folder or a `.zip` archive. Zips are read directly via their internal file listing, so you don't need to fully extract a large archive to disk first.
2. **Pick the extraction type** — iOS or Android. This selects which platform-specific database profile gets merged into the picker.
3. **Select which databases to pull** — a checklist combining the platform profile with a shared cross-platform "third-party app" profile (WhatsApp, Signal, Telegram, etc.). Defaults to everything selected.
4. **Copy** — the tool walks the extraction and copies every match into `Exported/`, right next to the source. If a filename shows up in more than one place (common in iOS backups), each copy is suffixed with its parent folder name so nothing silently overwrites anything else. The TUI shows this happening live: a scanning phase with a running file/folder count, then a copying phase with a per-target checklist and progress bar.
5. **Review results** — each target shows as `COPIED`, `ALREADY_PRESENT` (source and destination already match — safe to re-run), `NOT_FOUND`, or `ERROR`, with a detail line for each.

### WAL/SHM sidecars

Whenever a database file is matched, DB Ripper also checks the same directory (or the same path inside a zip) for `<name>-wal` and `<name>-shm` and copies them too, if present. It's silent when they're not there — most closed/checkpointed databases won't have one — but when they are, skipping them means parsing a database that's missing its most recent writes.

### Folder targets

A few artifacts worth pulling — like the iOS Camera Roll (`DCIM`) — are entire directory trees, not single files. These are matched and copied recursively, and can optionally be constrained to a specific parent directory name to cut down on false positives from unrelated folders that happen to share a name.

---

## Custom database profiles

The built-in iOS/Android/common target lists live in the script as plain Python, but you don't have to edit the source to add your own. Drop a JSON file at:

- `db_ripper_profiles.json` next to the script, **or**
- `db_ripper_profiles.json` in your current working directory, **or**
- any path you point to explicitly:

```bash
python3 db_ripper_tui.py --config path/to/profiles.json
python3 db_ripper_cli.py --config path/to/profiles.json
```

A config's top-level keys that match a built-in profile (`ios`, `android`, `common`) **replace** that profile's database list. Keys that don't match become brand-new selectable extraction types alongside iOS/Android — handy for a case-specific target list.

```json
{
  "ios": {
    "databases": [
      { "name": "MyCase.sqlite", "note": "Custom database specific to this case" }
    ]
  },
  "case_2026_special": {
    "label": "Case #2026-114 Special Targets",
    "databases": [
      { "name": "weirdapp.db", "note": "One-off app relevant only to this case" },
      { "name": "Evidence", "note": "Custom evidence folder", "type": "folder", "parent": "AppData" }
    ]
  }
}
```

See [`db_ripper_profiles.example.json`](./db_ripper_profiles.example.json) for a fuller example, including how to extend rather than fully replace a profile. Keys starting with `_` (e.g. `"_comment"`) are ignored, as a lightweight way to leave notes in an otherwise comment-less file. If a config is missing, malformed, or fails validation, DB Ripper prints a warning and falls back to the built-in defaults rather than crashing.

**Database entry fields:**

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Filename (or folder name) to search for |
| `note` | No | Description shown in the picker UI |
| `type` | No | `"file"` (default) or `"folder"` |
| `parent` | No | Folder targets only match when their immediate parent directory has this name (case-insensitive) |

---

## Versioning

DB Ripper follows [Semantic Versioning](https://semver.org/). Check what you're running with:

```bash
python3 db_ripper_tui.py --version
python3 db_ripper_cli.py --version
```

See [`CHANGELOG.md`](./CHANGELOG.md) for release history. GitHub releases are tagged to match (`v1.1.0`, etc.) — pin to a tag instead of tracking `main` if you want a known-good version on an exam workstation.

---

## Roadmap

Not yet implemented — see [`CHANGELOG.md`](./CHANGELOG.md) for the current list:

- Optional hash-on-copy (MD5/SHA-256) with a manifest file
- Batch mode for running the same profile against multiple extractions unattended

- I plan to release a blog post related to the tool. I'll update this with a direct link but until then feel free to check out the blog [here](https://forensicatorfiles.wixsite.com/home)!

---

## License

MIT License

Copyright (c) 2026 Forensicator Files

## Disclaimer

⚠️ Disclaimer: This tool is provided as-is, for educational and lawful forensic use, with no warranty of accuracy or fitness for purpose. Always independently verify results against source data before relying on them for investigative or legal purposes. Not legal advice. Use at your own risk.

[![BuyMeACoffee](https://raw.githubusercontent.com/pachadotdev/buymeacoffee-badges/main/bmc-black.svg)](https://www.buymeacoffee.com/forensicatorfiles)
