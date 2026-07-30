#!/usr/bin/env python3
"""
DB Ripper (CLI)
===============
Plain command-line version -- no web server, no curses, just
plain text prompts you answer by typing numbers/letters and
pressing Enter.
 
Run:            python3 db_ripper_cli.py
Check version:  python3 db_ripper_cli.py --version
Custom targets: python3 db_ripper_cli.py --config path/to/profiles.json
                (or drop a file named db_ripper_profiles.json next to this
                script / in the current directory -- it's picked up
                automatically. See db_ripper_profiles.example.json.)
 
Steps:
  1. Type the path to an extraction folder or a .zip file.
  2. Pick iOS or Android.
  3. Pick which databases to pull (comma-separated numbers, or "all").
  4. Confirm and it copies everything (plus WAL/SHM sidecars) into an
     Exported/ subfolder.
"""
 
import argparse
import copy
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
 
APP_NAME = "DB Ripper"
__version__ = "1.1.0"
 
ASCII_BANNER = r"""
    ____  ____     ____  ________  ____  __________
   / __ \/ __ )   / __ \/  _/ __ \/ __ \/ ____/ __ \
  / / / / __  |  / /_/ // // /_/ / /_/ / __/ / /_/ /
 / /_/ / /_/ /  / _, _// // ____/ ____/ /___/ _, _/
/_____/_____/  /_/ |_/___/_/   /_/   /_____/_/ |_|
"""
 
CONFIG_FILENAME = "db_ripper_profiles.json"

# ---------------------------------------------------------------------------
# DEFAULT DATABASE PROFILES  (built-in fallback -- edit freely, or override
# without touching this file at all by supplying a JSON config; see
# load_database_profiles() below and db_ripper_profiles.example.json)
#
# Each entry:
#   name   - filename (or folder name) to search for
#   note   - description shown in the UI
#   type   - "file" (default) or "folder"
#   parent - optional: folder targets only matched when their immediate
#             parent directory has this name (case-insensitive)
# ---------------------------------------------------------------------------
DEFAULT_DATABASE_PROFILES = {
    "ios": {
        "label": "iOS",
        "databases": [
            {"name": "Cache.sqlite",               "note": "Safari / WebKit cache"},
            {"name": "Cloud-V2.sqlite",            "note": "iCloud Photos metadata"},
            {"name": "Photos.sqlite",              "note": "Photos library"},
            {"name": "sms.db",                     "note": "iMessage / SMS messages"},
            {"name": "AddressBook.sqlitedb",       "note": "Contacts"},
            {"name": "AddressBookImages.sqlitedb", "note": "Contact photos"},
            {"name": "CallHistory.storedata",      "note": "Call log"},
            {"name": "interactionC.db",            "note": "Contacts / interaction graph"},
            {"name": "healthdb_secure.sqlite",     "note": "Apple Health"},
            {"name": "TCC.db",                     "note": "Privacy & permissions (TCC)"},
            {"name": "knowledgeC.db",              "note": "App usage / Screen Time"},
            {"name": "Notes.sqlite",               "note": "Notes app"},
            {"name": "consolidated.db",            "note": "Legacy location cache"},
            {"name": "Calendar.sqlitedb",          "note": "Calendar events"},
            {"name": "DCIM",                       "note": "Camera roll photos & videos",
             "type": "folder", "parent": "Media"},
        ],
    },
    "android": {
        "label": "Android",
        "databases": [
            {"name": "contacts2.db",  "note": "Contacts provider"},
            {"name": "mmssms.db",     "note": "SMS / MMS messages"},
            {"name": "calendar.db",   "note": "Calendar provider"},
            {"name": "telephony.db",  "note": "Call log / telephony"},
            {"name": "external.db",   "note": "Media store (external storage)"},
            {"name": "internal.db",   "note": "Media store (internal storage)"},
            {"name": "browser2.db",   "note": "Browser history & bookmarks"},
            {"name": "webview.db",    "note": "WebView data"},
            {"name": "downloads.db",  "note": "Download manager"},
            {"name": "snapshots.db", "note": "Recent apps snapshots"},
        ],
    },
    "common": {
        "label": "Third-Party (cross-platform)",
        "databases": [
            {"name": "ChatStorage.sqlite", "note": "WhatsApp messages (iOS)"},
            {"name": "ContactsV2.sqlite",  "note": "WhatsApp contacts (iOS)"},
            {"name": "msgstore.db",        "note": "WhatsApp messages (Android)"},
            {"name": "wa.db",              "note": "WhatsApp contacts (Android)"},
            {"name": "signal.db",          "note": "Signal messages (Android)"},
            {"name": "telegram.db",        "note": "Telegram cache"},
            {"name": "kik.sqlite",         "note": "Kik messages"},
            {"name": "directMessages.db",  "note": "Instagram / Snap direct messages"},
        ],
    },
}

# The active profile set. Starts as the built-in defaults; may be replaced or
# extended at startup by load_database_profiles() if a JSON config is found.
DATABASE_PROFILES = DEFAULT_DATABASE_PROFILES


def _validate_profiles(data):
    """Raises ValueError with a human-readable message if `data` isn't a
    valid profiles structure. Returns `data` unchanged if it is."""
    if not isinstance(data, dict) or not data:
        raise ValueError("config root must be a non-empty JSON object mapping "
                          "profile keys (e.g. \"ios\") to profile definitions.")
    for key, profile in data.items():
        if key.startswith("_"):
            continue  # convention: leading-underscore keys are ignored notes/comments
        if not isinstance(profile, dict) or "databases" not in profile:
            raise ValueError(f'profile "{key}" must be an object with a "databases" list.')
        if not isinstance(profile["databases"], list) or not profile["databases"]:
            raise ValueError(f'profile "{key}".databases must be a non-empty list.')
        for i, entry in enumerate(profile["databases"]):
            if not isinstance(entry, dict) or not entry.get("name"):
                raise ValueError(f'profile "{key}".databases[{i}] must be an '
                                  f'object with at least a "name" field.')
            if entry.get("type") not in (None, "file", "folder"):
                raise ValueError(f'profile "{key}".databases[{i}] "type" must '
                                  f'be "file" or "folder" if given.')
    return data


def load_database_profiles(config_path=None):
    """
    Builds the active DATABASE_PROFILES, optionally overlaying a user-supplied
    JSON config on top of the built-in defaults.

    Lookup order when `config_path` is not explicitly given:
      1. db_ripper_profiles.json next to this script
      2. db_ripper_profiles.json in the current working directory
      3. built-in defaults only (no config file found -- not an error)

    A config's top-level keys that match an existing profile ("ios",
    "android", "common") replace that profile's database list. New keys
    become additional selectable extraction types alongside iOS/Android.

    Returns (profiles, message). `message` is None when only built-in
    defaults were used; otherwise a short string describing what happened,
    prefixed with "!" if the config could not be used (in which case
    `profiles` still falls back to the built-in defaults).
    """
    profiles = copy.deepcopy(DEFAULT_DATABASE_PROFILES)

    if config_path:
        candidates = [Path(config_path)]
    else:
        candidates = [
            Path(__file__).resolve().parent / CONFIG_FILENAME,
            Path.cwd() / CONFIG_FILENAME,
        ]

    chosen = next((c for c in candidates if c.is_file()), None)
    if chosen is None:
        if config_path:
            return profiles, f"! Config file not found: {config_path} -- using built-in defaults."
        return profiles, None

    try:
        with open(chosen, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = _validate_profiles(data)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return profiles, f"! Could not load {chosen}: {e} -- using built-in defaults."

    for key, profile in data.items():
        if key.startswith("_"):
            continue
        if key in profiles:
            profiles[key]["databases"] = profile["databases"]
            if "label" in profile:
                profiles[key]["label"] = profile["label"]
        else:
            profiles[key] = {"label": profile.get("label", key), "databases": profile["databases"]}

    return profiles, f"Loaded custom database profiles from {chosen}"



# ---------------------------------------------------------------------------
# Helpers (business logic -- identical to the Flask and TUI versions)
# ---------------------------------------------------------------------------

def inspect_path(raw: str):
    if not raw:
        raise ValueError("No path provided.")
    p = Path(raw).expanduser()
    if not p.exists():
        raise ValueError(f"Path not found: {raw}")
    p = p.resolve()
    if p.is_dir():
        return "folder", p
    if p.is_file() and p.suffix.lower() == ".zip":
        if not zipfile.is_zipfile(p):
            raise ValueError(f"{p.name} is not a valid zip archive.")
        return "zip", p
    raise ValueError("Path must be a folder or a .zip file.")


def _split_targets(selected_items):
    file_targets = {}
    folder_targets = {}
    for item in selected_items:
        name = item["name"]
        if item.get("type") == "folder":
            folder_targets[name.lower()] = item
        else:
            file_targets[name.lower()] = name
    return file_targets, folder_targets


SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def copy_from_folder(root: Path, selected_items, dest_root: Path):
    file_targets, folder_targets = _split_targets(selected_items)

    found_files = {n: [] for n in file_targets.values()}
    found_dirs  = {entry["name"]: [] for entry in folder_targets.values()}

    for dirpath, _dirnames, filenames in os.walk(root):
        dp = Path(dirpath)

        key = dp.name.lower()
        if key in folder_targets:
            entry = folder_targets[key]
            parent_req = entry.get("parent", "").lower()
            if not parent_req or dp.parent.name.lower() == parent_req:
                found_dirs[entry["name"]].append(dp)

        for fname in filenames:
            k = fname.lower()
            if k in file_targets:
                found_files[file_targets[k]].append(dp / fname)

    results = []

    for name, matches in found_files.items():
        if not matches:
            results.append({"name": name, "status": "not_found",
                            "detail": "No matching file found."})
            continue
        multiple = len(matches) > 1
        for idx, match in enumerate(matches):
            stem, suffix = Path(name).stem, Path(name).suffix
            dest_name = f"{stem}__{match.parent.name or f'src{idx}'}{suffix}" if multiple else name
            dest = dest_root / dest_name
            try:
                same = match.resolve() == dest.resolve()
            except OSError:
                same = False
            if same:
                results.append({"name": dest_name, "status": "already_present",
                                "detail": str(match)})
            else:
                try:
                    shutil.copy2(match, dest)
                    results.append({"name": dest_name, "status": "copied",
                                    "detail": f"{match}  ->  {dest}"})
                except OSError as e:
                    results.append({"name": dest_name, "status": "error", "detail": str(e)})

            # SQLite write-ahead log (-wal) and shared-memory (-shm) sidecars:
            # only copied when actually present alongside the matched database,
            # since most closed/checkpointed databases won't have one.
            for sidecar_suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar_src = match.parent / (match.name + sidecar_suffix)
                if not sidecar_src.is_file():
                    continue
                sidecar_dest_name = dest_name + sidecar_suffix
                sidecar_dest = dest_root / sidecar_dest_name
                try:
                    sidecar_same = sidecar_src.resolve() == sidecar_dest.resolve()
                except OSError:
                    sidecar_same = False
                if sidecar_same:
                    results.append({"name": sidecar_dest_name, "status": "already_present",
                                    "detail": str(sidecar_src)})
                    continue
                try:
                    shutil.copy2(sidecar_src, sidecar_dest)
                    results.append({"name": sidecar_dest_name, "status": "copied",
                                    "detail": f"{sidecar_src}  ->  {sidecar_dest}"})
                except OSError as e:
                    results.append({"name": sidecar_dest_name, "status": "error", "detail": str(e)})

    for name, matches in found_dirs.items():
        if not matches:
            results.append({"name": name, "status": "not_found",
                            "detail": f'No folder named "{name}" found in extraction.'})
            continue
        multiple = len(matches) > 1
        for idx, src_dir in enumerate(matches):
            tag = src_dir.parent.name or f"src{idx}"
            dest_name = f"{name}__{tag}" if multiple else name
            dest = dest_root / dest_name
            try:
                if dest.exists() and dest.resolve() == src_dir.resolve():
                    results.append({"name": dest_name, "status": "already_present",
                                    "detail": str(src_dir)})
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src_dir, dest)
                file_count = sum(1 for _ in dest.rglob("*") if _.is_file())
                results.append({"name": dest_name, "status": "copied",
                                "detail": f"{src_dir}  ->  {dest}  ({file_count} files)"})
            except OSError as e:
                results.append({"name": dest_name, "status": "error", "detail": str(e)})

    return results


def copy_from_zip(zip_path: Path, selected_items, dest_root: Path):
    file_targets, folder_targets = _split_targets(selected_items)

    found_files        = {n: [] for n in file_targets.values()}
    found_folder_files = {entry["name"]: [] for entry in folder_targets.values()}

    results = []
    entries_by_path = {}  # lower(full zip path) -> ZipInfo, used for WAL/SHM sidecar lookup

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            entries_by_path[info.filename.lower()] = info

            parts = PurePosixPath(info.filename).parts
            if not parts:
                continue

            basename = parts[-1]

            bkey = basename.lower()
            if bkey in file_targets:
                found_files[file_targets[bkey]].append(info)

            for i, part in enumerate(parts[:-1]):
                k = part.lower()
                if k not in folder_targets:
                    continue
                entry = folder_targets[k]
                parent_req = entry.get("parent", "").lower()
                if parent_req:
                    if i == 0 or parts[i - 1].lower() != parent_req:
                        continue
                canonical = entry["name"]
                rel_parts = parts[i + 1:]
                rel = str(PurePosixPath(*rel_parts))
                found_folder_files[canonical].append((info, rel))

        for name, matches in found_files.items():
            if not matches:
                results.append({"name": name, "status": "not_found",
                                "detail": "No matching entry in zip."})
                continue
            multiple = len(matches) > 1
            for idx, info in enumerate(matches):
                stem, suffix = Path(name).stem, Path(name).suffix
                tag = PurePosixPath(info.filename).parent.name or f"entry{idx}"
                dest_name = f"{stem}__{tag}{suffix}" if multiple else name
                dest = dest_root / dest_name
                try:
                    with zf.open(info) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    results.append({"name": dest_name, "status": "copied",
                                    "detail": f"{zip_path.name}:{info.filename}  ->  {dest}"})
                except (OSError, zipfile.BadZipFile) as e:
                    results.append({"name": dest_name, "status": "error", "detail": str(e)})

                # SQLite write-ahead log (-wal) and shared-memory (-shm) sidecars:
                # only copied when actually present alongside the matched database
                # inside the zip, since most closed/checkpointed databases won't
                # have one.
                for sidecar_suffix in SQLITE_SIDECAR_SUFFIXES:
                    sidecar_info = entries_by_path.get((info.filename + sidecar_suffix).lower())
                    if sidecar_info is None:
                        continue
                    sidecar_dest_name = dest_name + sidecar_suffix
                    sidecar_dest = dest_root / sidecar_dest_name
                    try:
                        with zf.open(sidecar_info) as src, open(sidecar_dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        results.append({"name": sidecar_dest_name, "status": "copied",
                                        "detail": f"{zip_path.name}:{sidecar_info.filename}  ->  {sidecar_dest}"})
                    except (OSError, zipfile.BadZipFile) as e:
                        results.append({"name": sidecar_dest_name, "status": "error", "detail": str(e)})

        for name, file_list in found_folder_files.items():
            if not file_list:
                results.append({"name": name, "status": "not_found",
                                "detail": f'No folder named "{name}" found in zip.'})
                continue

            dest_dir = dest_root / name
            dest_dir.mkdir(parents=True, exist_ok=True)

            file_count = 0
            errors = []
            for info, rel in file_list:
                dest_file = dest_dir / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info) as src, open(dest_file, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    file_count += 1
                except (OSError, zipfile.BadZipFile) as e:
                    errors.append({"name": f"{name}/{rel}", "status": "error",
                                   "detail": str(e)})

            results.extend(errors)
            if file_count:
                results.append({"name": name, "status": "copied",
                                "detail": f"{zip_path.name} -> {dest_dir}  ({file_count} files)"})

    return results


# ---------------------------------------------------------------------------
# Plain-text prompts
# ---------------------------------------------------------------------------

def ask_extraction_path():
    while True:
        raw = input("\nPath to extraction (folder or .zip file): ").strip().strip('"')
        try:
            return inspect_path(raw)
        except ValueError as e:
            print(f"  ! {e}")


def ask_extraction_type():
    types = [k for k in DATABASE_PROFILES if k != "common"]
    print("\nExtraction type:")
    for i, key in enumerate(types, start=1):
        print(f"  {i}. {DATABASE_PROFILES[key]['label']}")
    while True:
        choice = input(f"Choose 1-{len(types)}: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(types):
            return types[int(choice) - 1]
        print("  ! Enter a number from the list.")


def ask_databases(dtype):
    dbs = DATABASE_PROFILES[dtype]["databases"] + DATABASE_PROFILES["common"]["databases"]
    print(f"\nDatabases available ({DATABASE_PROFILES[dtype]['label']} + Third-Party):")
    for i, d in enumerate(dbs, start=1):
        print(f"  {i:>2}. {d['name']:<28} {d['note']}")
    print("\nType the numbers you want, separated by commas (e.g. 1,3,5),")
    print('or type "all" to select everything.')
    while True:
        choice = input("Selection: ").strip().lower()
        if choice == "all":
            return dbs
        picks = []
        bad = False
        for part in choice.split(","):
            part = part.strip()
            if not part.isdigit() or not (1 <= int(part) <= len(dbs)):
                bad = True
                break
            picks.append(dbs[int(part) - 1])
        if bad or not picks:
            print("  ! Enter valid numbers from the list, comma-separated, or \"all\".")
            continue
        return picks


def confirm(kind, path, dtype, selected, dest_root):
    print("\n" + "-" * 60)
    print(f"Extraction : {path}")
    print(f"Type       : {'folder' if kind == 'folder' else 'zip archive'} / {DATABASE_PROFILES[dtype]['label']}")
    print(f"Selected   : {len(selected)} database target(s)")
    for d in selected:
        print(f"             - {d['name']}")
    print(f"Destination: {dest_root}")
    print("-" * 60)
    answer = input("Proceed? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def print_results(results, dest_root):
    print("\n" + "=" * 60)
    print(f" RESULTS  ->  {dest_root}")
    print("=" * 60)
    labels = {
        "copied": "COPIED",
        "already_present": "ALREADY PRESENT",
        "not_found": "NOT FOUND",
        "error": "ERROR",
    }
    for r in results:
        tag = labels.get(r["status"], r["status"].upper())
        print(f"[{tag:<16}] {r['name']}")
        print(f"                   {r['detail']}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_one_job():
    kind, path = ask_extraction_path()
    dtype = ask_extraction_type()
    selected = ask_databases(dtype)

    dest_root = (path / "Exported") if kind == "folder" else (path.parent / "Exported")

    if not confirm(kind, path, dtype, selected, dest_root):
        print("Cancelled.")
        return

    dest_root.mkdir(parents=True, exist_ok=True)
    print("\nCopying...")
    if kind == "folder":
        results = copy_from_folder(path, selected, dest_root)
    else:
        results = copy_from_zip(path, selected, dest_root)

    print_results(results, dest_root)


def main():
    print("=" * 60)
    print(f" {APP_NAME.upper()}  v{__version__}  (command-line)")
    print("=" * 60)
    while True:
        run_one_job()
        again = input("\nRun another job? [y/N]: ").strip().lower()
        if again not in ("y", "yes"):
            print("Done.")
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="db_ripper_cli.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{ASCII_BANNER}\n{APP_NAME} v{__version__} -- command-line tool "
                    f"for locating mobile-extraction artifacts.")
    parser.add_argument("--version", "-v", action="store_true",
                        help="print the version number and exit")
    parser.add_argument("--config", "-c", metavar="PATH",
                        help="path to a custom database-profiles JSON file "
                             "(default: look for db_ripper_profiles.json next "
                             "to this script or in the current directory)")
    args = parser.parse_args()
 
    if args.version:
        print(f"{APP_NAME} {__version__}")
        sys.exit(0)
 
    DATABASE_PROFILES, config_message = load_database_profiles(args.config)
    if config_message:
        print(config_message)
 
    main()
