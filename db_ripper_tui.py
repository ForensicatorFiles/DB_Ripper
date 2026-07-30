#!/usr/bin/env python3
"""
DB Ripper (TUI)
===============
Terminal UI for forensic examiners to:
  1. Browse to an extraction on this computer -- a folder or .zip archive
  2. Pick the extraction type (Android / iOS)
  3. Select which databases / folders to pull
  4. Recursively locate and copy them (plus WAL/SHM sidecars) into an
     Exported/ subfolder, with a live progress view while it runs
 
Run:            python3 db_ripper_tui.py
Check version:  python3 db_ripper_tui.py --version
Custom targets: python3 db_ripper_tui.py --config path/to/profiles.json
                (or drop a file named db_ripper_profiles.json next to this
                script / in the current directory -- it's picked up
                automatically. See db_ripper_profiles.example.json.)
 
Controls (shown in the footer of every screen):
  Up/Down or j/k   move cursor
  Enter            open folder / confirm selection
  Space            toggle checkbox (database picker)
  a                toggle all (database picker)
  o                select current folder as the extraction root (browser)
  p                type a path manually (browser)
  d                jump to a drive / mount point (browser)
  q / Esc          back / quit
"""
 
import argparse
import copy
import curses
import json
import os
import string
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
import shutil
 
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
# Every function below reads this module-level name at call time, so
# reassigning it before the UI starts is enough to make custom profiles take
# effect everywhere.
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
# Helpers (business logic -- unchanged from the Flask version)
# ---------------------------------------------------------------------------

def list_drives():
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            p = f"{letter}:\\"
            if os.path.exists(p):
                drives.append({"name": p, "path": p})
    else:
        drives.append({"name": "/", "path": "/"})
        for base in ("/mnt", "/media", "/Volumes"):
            bp = Path(base)
            if bp.is_dir():
                try:
                    for sub in sorted(bp.iterdir()):
                        if sub.is_dir() and not sub.name.startswith("."):
                            drives.append({"name": str(sub), "path": str(sub)})
                except (PermissionError, OSError):
                    pass
    return drives


def list_directory(path: Path):
    entries = []
    try:
        items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return entries
    for item in items:
        try:
            if item.name.startswith("."):
                continue
            if item.is_dir():
                entries.append({"name": item.name, "path": str(item), "type": "dir"})
            elif item.is_file() and item.suffix.lower() == ".zip":
                entries.append({"name": item.name, "path": str(item), "type": "zip"})
        except (PermissionError, OSError):
            pass
    return entries


def inspect_path(raw: str):
    if not raw:
        raise ValueError("No path provided.")
    p = Path(raw)
    if not p.exists():
        raise ValueError(f"Path not found: {raw}")
    p = p.resolve()
    if p.is_dir():
        return "folder", p
    if p.is_file() and p.suffix.lower() == ".zip":
        if not zipfile.is_zipfile(p):
            raise ValueError(f"{p.name} is not a valid zip archive.")
        return "zip", p
    raise ValueError("Select a folder or a .zip file.")


def _split_targets(selected_items):
    """Partition selection into file targets and folder targets.

    file_targets:   { lower_name -> canonical_name }
    folder_targets: { lower_name -> full entry dict }
    """
    file_targets = {}
    folder_targets = {}
    for item in selected_items:
        if isinstance(item, dict):
            name = item["name"]
            if item.get("type") == "folder":
                folder_targets[name.lower()] = item
            else:
                file_targets[name.lower()] = name
        else:
            file_targets[item.lower()] = item
    return file_targets, folder_targets


SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _aggregate_outcome(outcomes):
    """Collapse the outcomes of possibly-multiple matches for one target
    into a single status for the live checklist (full detail per-match
    still lives in the final results list)."""
    if not outcomes:
        return "not_found"
    if "error" in outcomes:
        return "error"
    if all(o == "already_present" for o in outcomes):
        return "already_present"
    return "copied"


class ProgressTracker:
    """Thread-safe state shared between the copy worker thread and the
    curses screen that renders it live. The copy functions call the small
    `mark_*` / `set_*` methods as they work; the UI thread reads a
    `snapshot()` every redraw.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.phase = "scanning"          # "scanning" -> "copying" -> "done"
        self.scanned_dirs = 0
        self.scanned_files = 0
        self.current_path = ""           # what's being examined right now
        self.current_target = ""         # which target name is being copied right now
        self.targets = []                # ordered list of target display names
        self.status = {}                 # name -> pending/located/copying/copied/already_present/not_found/error
        self.match_count = {}            # name -> number of matches found during scan
        self.done = False
        self.results = None

    def init_targets(self, names):
        with self._lock:
            self.targets = list(dict.fromkeys(names))  # de-dupe, keep order
            self.status = {n: "pending" for n in self.targets}
            self.match_count = {n: 0 for n in self.targets}

    def scan_tick(self, path, is_dir):
        with self._lock:
            if is_dir:
                self.scanned_dirs += 1
            else:
                self.scanned_files += 1
            self.current_path = str(path)

    def mark_located(self, name):
        with self._lock:
            self.match_count[name] = self.match_count.get(name, 0) + 1
            if self.status.get(name) == "pending":
                self.status[name] = "located"

    def set_phase(self, phase):
        with self._lock:
            self.phase = phase

    def set_current_target(self, name):
        with self._lock:
            self.current_target = name
            if self.status.get(name) in ("pending", "located"):
                self.status[name] = "copying"

    def set_status(self, name, status):
        with self._lock:
            self.status[name] = status

    def finish(self, results):
        with self._lock:
            self.results = results
            self.phase = "done"
            self.done = True

    def snapshot(self):
        with self._lock:
            return {
                "phase": self.phase,
                "scanned_dirs": self.scanned_dirs,
                "scanned_files": self.scanned_files,
                "current_path": self.current_path,
                "current_target": self.current_target,
                "targets": list(self.targets),
                "status": dict(self.status),
                "match_count": dict(self.match_count),
                "done": self.done,
            }


def copy_from_folder(root: Path, selected_items, dest_root: Path, progress: "ProgressTracker" = None):
    file_targets, folder_targets = _split_targets(selected_items)

    found_files = {n: [] for n in file_targets.values()}
    found_dirs  = {entry["name"]: [] for entry in folder_targets.values()}

    if progress:
        progress.init_targets(list(found_files.keys()) + list(found_dirs.keys()))
        progress.set_phase("scanning")

    for dirpath, _dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        if progress:
            progress.scan_tick(dp, is_dir=True)

        # folder target match
        key = dp.name.lower()
        if key in folder_targets:
            entry = folder_targets[key]
            parent_req = entry.get("parent", "").lower()
            if not parent_req or dp.parent.name.lower() == parent_req:
                found_dirs[entry["name"]].append(dp)
                if progress:
                    progress.mark_located(entry["name"])

        # file target match
        for fname in filenames:
            k = fname.lower()
            if k in file_targets:
                found_files[file_targets[k]].append(dp / fname)
                if progress:
                    progress.mark_located(file_targets[k])

    if progress:
        progress.set_phase("copying")

    results = []

    # copy files
    for name, matches in found_files.items():
        if not matches:
            results.append({"name": name, "status": "not_found",
                            "detail": "No matching file found."})
            if progress:
                progress.set_status(name, "not_found")
            continue
        multiple = len(matches) > 1
        target_outcomes = []
        for idx, match in enumerate(matches):
            stem, suffix = Path(name).stem, Path(name).suffix
            dest_name = f"{stem}__{match.parent.name or f'src{idx}'}{suffix}" if multiple else name
            dest = dest_root / dest_name
            if progress:
                progress.set_current_target(name)
                progress.scan_tick(dest_name, is_dir=False)
            try:
                same = match.resolve() == dest.resolve()
            except OSError:
                same = False
            if same:
                results.append({"name": dest_name, "status": "already_present",
                                "detail": str(match)})
                target_outcomes.append("already_present")
            else:
                try:
                    shutil.copy2(match, dest)
                    results.append({"name": dest_name, "status": "copied",
                                    "detail": f"{match}  ->  {dest}"})
                    target_outcomes.append("copied")
                except OSError as e:
                    results.append({"name": dest_name, "status": "error", "detail": str(e)})
                    target_outcomes.append("error")

            # SQLite write-ahead log (-wal) and shared-memory (-shm) sidecars:
            # only copied when actually present alongside the matched database,
            # since most closed/checkpointed databases won't have one.
            for sidecar_suffix in SQLITE_SIDECAR_SUFFIXES:
                sidecar_src = match.parent / (match.name + sidecar_suffix)
                if not sidecar_src.is_file():
                    continue
                sidecar_dest_name = dest_name + sidecar_suffix
                sidecar_dest = dest_root / sidecar_dest_name
                if progress:
                    progress.scan_tick(sidecar_dest_name, is_dir=False)
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
        if progress:
            progress.set_status(name, _aggregate_outcome(target_outcomes))

    # copy folder targets
    for name, matches in found_dirs.items():
        if not matches:
            results.append({"name": name, "status": "not_found",
                            "detail": f'No folder named "{name}" found in extraction.'})
            if progress:
                progress.set_status(name, "not_found")
            continue
        multiple = len(matches) > 1
        target_outcomes = []
        for idx, src_dir in enumerate(matches):
            tag = src_dir.parent.name or f"src{idx}"
            dest_name = f"{name}__{tag}" if multiple else name
            dest = dest_root / dest_name
            if progress:
                progress.set_current_target(name)
                progress.scan_tick(dest_name, is_dir=True)
            try:
                if dest.exists() and dest.resolve() == src_dir.resolve():
                    results.append({"name": dest_name, "status": "already_present",
                                    "detail": str(src_dir)})
                    target_outcomes.append("already_present")
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src_dir, dest)
                file_count = sum(1 for _ in dest.rglob("*") if _.is_file())
                results.append({"name": dest_name, "status": "copied",
                                "detail": f"{src_dir}  ->  {dest}  ({file_count} files)"})
                target_outcomes.append("copied")
            except OSError as e:
                results.append({"name": dest_name, "status": "error", "detail": str(e)})
                target_outcomes.append("error")
        if progress:
            progress.set_status(name, _aggregate_outcome(target_outcomes))

    if progress:
        progress.finish(results)
    return results


def copy_from_zip(zip_path: Path, selected_items, dest_root: Path, progress: "ProgressTracker" = None):
    file_targets, folder_targets = _split_targets(selected_items)

    found_files        = {n: [] for n in file_targets.values()}
    # list of (ZipInfo, rel_path_str) pairs for each folder target
    found_folder_files = {entry["name"]: [] for entry in folder_targets.values()}

    if progress:
        progress.init_targets(list(found_files.keys()) + list(found_folder_files.keys()))
        progress.set_phase("scanning")

    results = []
    entries_by_path = {}  # lower(full zip path) -> ZipInfo, used for WAL/SHM sidecar lookup

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if progress:
                progress.scan_tick(info.filename, is_dir=False)
            entries_by_path[info.filename.lower()] = info

            # Normalise: PurePosixPath strips leading ./ and redundant slashes
            parts = PurePosixPath(info.filename).parts
            if not parts:
                continue

            basename = parts[-1]

            # file match on basename
            bkey = basename.lower()
            if bkey in file_targets:
                found_files[file_targets[bkey]].append(info)
                if progress:
                    progress.mark_located(file_targets[bkey])

            # folder match: search all directory components (exclude the filename)
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
                # relative path inside the matched folder (everything after it)
                rel_parts = parts[i + 1:]
                rel = str(PurePosixPath(*rel_parts))
                found_folder_files[canonical].append((info, rel))
                if progress:
                    progress.mark_located(canonical)

        if progress:
            progress.set_phase("copying")

        # copy files
        for name, matches in found_files.items():
            if not matches:
                results.append({"name": name, "status": "not_found",
                                "detail": "No matching entry in zip."})
                if progress:
                    progress.set_status(name, "not_found")
                continue
            multiple = len(matches) > 1
            target_outcomes = []
            for idx, info in enumerate(matches):
                stem, suffix = Path(name).stem, Path(name).suffix
                tag = PurePosixPath(info.filename).parent.name or f"entry{idx}"
                dest_name = f"{stem}__{tag}{suffix}" if multiple else name
                dest = dest_root / dest_name
                if progress:
                    progress.set_current_target(name)
                    progress.scan_tick(dest_name, is_dir=False)
                try:
                    with zf.open(info) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    results.append({"name": dest_name, "status": "copied",
                                    "detail": f"{zip_path.name}:{info.filename}  ->  {dest}"})
                    target_outcomes.append("copied")
                except (OSError, zipfile.BadZipFile) as e:
                    results.append({"name": dest_name, "status": "error", "detail": str(e)})
                    target_outcomes.append("error")

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
                    if progress:
                        progress.scan_tick(sidecar_dest_name, is_dir=False)
                    try:
                        with zf.open(sidecar_info) as src, open(sidecar_dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        results.append({"name": sidecar_dest_name, "status": "copied",
                                        "detail": f"{zip_path.name}:{sidecar_info.filename}  ->  {sidecar_dest}"})
                    except (OSError, zipfile.BadZipFile) as e:
                        results.append({"name": sidecar_dest_name, "status": "error", "detail": str(e)})
            if progress:
                progress.set_status(name, _aggregate_outcome(target_outcomes))

        # copy folder targets
        for name, file_list in found_folder_files.items():
            if not file_list:
                results.append({"name": name, "status": "not_found",
                                "detail": f'No folder named "{name}" found in zip.'})
                if progress:
                    progress.set_status(name, "not_found")
                continue

            if progress:
                progress.set_current_target(name)

            dest_dir = dest_root / name
            dest_dir.mkdir(parents=True, exist_ok=True)

            file_count = 0
            errors = []
            for info, rel in file_list:
                dest_file = dest_dir / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                if progress:
                    progress.scan_tick(rel, is_dir=False)
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
            if progress:
                progress.set_status(name, "error" if errors and not file_count else
                                    ("copied" if file_count else "error"))

    if progress:
        progress.finish(results)
    return results


# ---------------------------------------------------------------------------
# Curses UI plumbing
# ---------------------------------------------------------------------------

COLOR_HEADER = 1
COLOR_NORMAL = 2
COLOR_SELECT = 3
COLOR_OK     = 4
COLOR_WARN   = 5
COLOR_ERR    = 6
COLOR_ACCENT = 7
COLOR_DIM    = 8


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_NORMAL, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(COLOR_OK,     curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_WARN,   curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_ERR,    curses.COLOR_RED, -1)
    curses.init_pair(COLOR_ACCENT, curses.COLOR_BLUE, -1)
    curses.init_pair(COLOR_DIM,    curses.COLOR_WHITE, -1)


def safe_addstr(win, y, x, text, attr=0):
    """addstr that swallows the 'wrote to bottom-right cell' curses error."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        win.addstr(y, x, text[: max(0, w - x - 1)], attr)
    except curses.error:
        pass


def draw_header(stdscr, title, subtitle=""):
    h, w = stdscr.getmaxyx()
    bar = f" {APP_NAME.upper()} v{__version__} ".center(w, "=")
    safe_addstr(stdscr, 0, 0, bar, curses.color_pair(COLOR_ACCENT) | curses.A_BOLD)
    safe_addstr(stdscr, 1, 2, title.upper(), curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
    if subtitle:
        safe_addstr(stdscr, 2, 2, subtitle, curses.color_pair(COLOR_DIM))
    safe_addstr(stdscr, 3, 0, "-" * w, curses.color_pair(COLOR_ACCENT))


def draw_footer(stdscr, hints):
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, h - 2, 0, "-" * w, curses.color_pair(COLOR_ACCENT))
    safe_addstr(stdscr, h - 1, 2, hints, curses.color_pair(COLOR_DIM) | curses.A_DIM)


def prompt_text(stdscr, label):
    """Bottom-line manual text entry. Returns a stripped string or ''."""
    h, w = stdscr.getmaxyx()
    curses.curs_set(1)
    curses.echo()
    safe_addstr(stdscr, h - 2, 0, " " * (w - 1))
    safe_addstr(stdscr, h - 2, 2, label, curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
    stdscr.refresh()
    win = curses.newwin(1, max(10, w - len(label) - 4), h - 2, len(label) + 3)
    win.keypad(True)
    try:
        text = win.getstr().decode("utf-8", errors="ignore").strip()
    except Exception:
        text = ""
    curses.noecho()
    curses.curs_set(0)
    return text


def message_box(stdscr, lines, attr=None):
    """Simple centered modal message; press any key to dismiss."""
    h, w = stdscr.getmaxyx()
    box_w = min(w - 4, max(len(l) for l in lines) + 6)
    box_h = len(lines) + 4
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.attrset(curses.color_pair(COLOR_ACCENT))
    win.box()
    for i, line in enumerate(lines):
        c = attr if attr else curses.color_pair(COLOR_NORMAL)
        safe_addstr(win, i + 1, 3, line, c)
    safe_addstr(win, box_h - 2, 3, "[ press any key ]", curses.color_pair(COLOR_DIM))
    win.refresh()
    win.getch()


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def browse_for_extraction(stdscr):
    """
    Returns (kind, Path) for the chosen extraction, or None if the user quit.
    kind is "folder" or "zip".
    """
    current_dir = Path.home()

    while True:
        entries = list_directory(current_dir)
        options = []
        if current_dir.parent != current_dir:
            options.append((".. (parent directory)", ("up", None)))
        for e in entries:
            tag = "[DIR]" if e["type"] == "dir" else "[ZIP]"
            options.append((f"{tag} {e['name']}", (e["type"], e["path"])))

        cursor = 0
        top = 0
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            draw_header(stdscr, "Select Extraction", f"Current: {current_dir}")
            list_top = 4
            list_h = h - list_top - 2

            if cursor < top:
                top = cursor
            if cursor >= top + list_h:
                top = cursor - list_h + 1

            if not options:
                safe_addstr(stdscr, list_top, 2, "(empty directory)", curses.color_pair(COLOR_DIM))
            for row, idx in enumerate(range(top, min(len(options), top + list_h))):
                label, _ = options[idx]
                y = list_top + row
                is_cur = (idx == cursor)
                attr = curses.color_pair(COLOR_SELECT) if is_cur else curses.color_pair(COLOR_NORMAL)
                marker = "> " if is_cur else "  "
                safe_addstr(stdscr, y, 2, marker + label, attr)

            draw_footer(stdscr,
                        "Up/Down move  Enter open  o select folder  p type path  d drives  q quit")
            stdscr.refresh()

            key = stdscr.getch()
            if key in (curses.KEY_UP, ord('k')) and options:
                cursor = (cursor - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord('j')) and options:
                cursor = (cursor + 1) % len(options)
            elif key in (curses.KEY_ENTER, 10, 13) and options:
                kind, val = options[cursor][1]
                if kind == "up":
                    current_dir = current_dir.parent
                    break
                elif kind == "dir":
                    current_dir = Path(val)
                    break
                elif kind == "zip":
                    try:
                        return inspect_path(val)
                    except ValueError as e:
                        message_box(stdscr, [str(e)], curses.color_pair(COLOR_ERR))
            elif key == ord('o'):
                try:
                    return inspect_path(str(current_dir))
                except ValueError as e:
                    message_box(stdscr, [str(e)], curses.color_pair(COLOR_ERR))
            elif key == ord('p'):
                raw = prompt_text(stdscr, "Path (folder or .zip): ")
                if raw:
                    try:
                        return inspect_path(raw)
                    except ValueError as e:
                        message_box(stdscr, [str(e)], curses.color_pair(COLOR_ERR))
            elif key == ord('d'):
                drives = list_drives()
                drive_options = [(d["name"], d["path"]) for d in drives]
                if drive_options:
                    chosen = run_menu(stdscr, "Drives / Mounts", "",
                                       drive_options,
                                       "Up/Down move  Enter select  q cancel")
                    if chosen:
                        current_dir = Path(chosen)
                        break
            elif key in (ord('q'), 27):
                return None


def choose_extraction_type(stdscr):
    options = [(v["label"], k) for k, v in DATABASE_PROFILES.items() if k != "common"]
    return run_menu(stdscr, "Extraction Type", "", options,
                     "Up/Down move  Enter select  q quit")


def choose_databases(stdscr, dtype):
    dbs = DATABASE_PROFILES[dtype]["databases"] + DATABASE_PROFILES["common"]["databases"]
    options = [(f"{d['name']:<28} {d['note']}", d) for d in dbs]
    checked = set(range(len(options)))  # default: everything selected
    result = run_checkbox_menu(
        stdscr,
        f"Select Databases ({DATABASE_PROFILES[dtype]['label']} + Third-Party)",
        "Space toggle  a toggle all  Enter confirm  q cancel",
        options, checked)
    if result is None:
        return None
    return [options[i][1] for i in sorted(result)]


def run_menu(stdscr, title, subtitle, options, footer_hints):
    cursor = 0
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_header(stdscr, title, subtitle)
        list_top = 4
        list_h = h - list_top - 2
        if cursor < top:
            top = cursor
        if cursor >= top + list_h:
            top = cursor - list_h + 1
        for row, idx in enumerate(range(top, min(len(options), top + list_h))):
            label, _ = options[idx]
            y = list_top + row
            is_cur = (idx == cursor)
            attr = curses.color_pair(COLOR_SELECT) if is_cur else curses.color_pair(COLOR_NORMAL)
            marker = "> " if is_cur else "  "
            safe_addstr(stdscr, y, 2, marker + label, attr)
        draw_footer(stdscr, footer_hints)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')) and options:
            cursor = (cursor - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord('j')) and options:
            cursor = (cursor + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13) and options:
            return options[cursor][1]
        elif key in (ord('q'), 27):
            return None


def run_checkbox_menu(stdscr, title, footer_hints, options, checked):
    cursor = 0
    top = 0
    checked = set(checked)
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_header(stdscr, title, f"{len(checked)}/{len(options)} selected")
        list_top = 4
        list_h = h - list_top - 2
        if cursor < top:
            top = cursor
        if cursor >= top + list_h:
            top = cursor - list_h + 1
        for row, idx in enumerate(range(top, min(len(options), top + list_h))):
            label, _ = options[idx]
            y = list_top + row
            is_cur = (idx == cursor)
            box = "[x] " if idx in checked else "[ ] "
            attr = curses.color_pair(COLOR_SELECT) if is_cur else curses.color_pair(COLOR_NORMAL)
            marker = "> " if is_cur else "  "
            safe_addstr(stdscr, y, 2, marker + box + label, attr)
        draw_footer(stdscr, footer_hints)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')) and options:
            cursor = (cursor - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord('j')) and options:
            cursor = (cursor + 1) % len(options)
        elif key == ord(' ') and options:
            if cursor in checked:
                checked.discard(cursor)
            else:
                checked.add(cursor)
        elif key == ord('a') and options:
            checked = set() if len(checked) == len(options) else set(range(len(options)))
        elif key in (curses.KEY_ENTER, 10, 13):
            if not checked:
                message_box(stdscr, ["Select at least one database first."],
                            curses.color_pair(COLOR_WARN))
                continue
            return checked
        elif key in (ord('q'), 27):
            return None


def confirm_screen(stdscr, kind, path, dtype, selected, dest_root):
    lines = [
        f"Extraction : {path}",
        f"Type       : {'folder' if kind == 'folder' else 'zip archive'} / {DATABASE_PROFILES[dtype]['label']}",
        f"Selected   : {len(selected)} database target(s)",
        f"Destination: {dest_root}",
        "",
        "Press ENTER to run the copy, q to cancel.",
    ]
    stdscr.erase()
    draw_header(stdscr, "Confirm", "")
    for i, line in enumerate(lines):
        safe_addstr(stdscr, 5 + i, 2, line, curses.color_pair(COLOR_NORMAL))
    draw_footer(stdscr, "Enter run  q cancel")
    stdscr.refresh()
    while True:
        key = stdscr.getch()
        if key in (curses.KEY_ENTER, 10, 13):
            return True
        if key in (ord('q'), 27):
            return False


STATUS_BOX = {
    "pending":         ("[ ]", COLOR_DIM),
    "located":         ("[~]", COLOR_ACCENT),
    "copying":         ("[>]", COLOR_HEADER),
    "copied":          ("[x]", COLOR_OK),
    "already_present": ("[x]", COLOR_WARN),
    "not_found":       ("[!]", COLOR_ERR),
    "error":           ("[!]", COLOR_ERR),
}

SPINNER_FRAMES = "|/-\\"


def _draw_bar(stdscr, y, x, width, fraction, indeterminate_offset=None):
    """Draw a simple text progress bar. If indeterminate_offset is given,
    draws a bouncing filled segment instead of a determinate fill."""
    width = max(10, width)
    if indeterminate_offset is None:
        filled = int(max(0.0, min(1.0, fraction)) * width)
        bar = "#" * filled + "-" * (width - filled)
    else:
        seg = max(3, width // 6)
        span = width - seg
        pos = indeterminate_offset % (span * 2)
        pos = pos if pos <= span else (span * 2 - pos)
        bar = "-" * pos + "#" * seg + "-" * (width - pos - seg)
    safe_addstr(stdscr, y, x, f"[{bar}]", curses.color_pair(COLOR_ACCENT))


def progress_screen(stdscr, tracker: "ProgressTracker", thread: threading.Thread):
    """Live-updating screen shown while the copy worker thread runs.
    Polls the tracker every ~120ms and redraws a checklist + progress bar
    until the thread finishes. Returns the final results list."""
    stdscr.nodelay(True)
    frame = 0
    try:
        while True:
            snap = tracker.snapshot()
            stdscr.erase()
            h, w = stdscr.getmaxyx()

            phase_label = {
                "scanning": "Scanning extraction for matches...",
                "copying": "Copying matched artifacts...",
                "done": "Done.",
            }.get(snap["phase"], "Working...")
            draw_header(stdscr, "Copying", phase_label)

            # --- summary / progress bar line ---
            targets = snap["targets"]
            total = len(targets)
            resolved = sum(1 for n in targets
                           if snap["status"].get(n) in
                           ("copied", "already_present", "not_found", "error"))

            y = 4
            if snap["phase"] == "scanning":
                located = sum(1 for n in targets if snap["match_count"].get(n, 0) > 0)
                safe_addstr(stdscr, y, 2,
                            f"Scanned {snap['scanned_dirs']} folders, {snap['scanned_files']} files"
                            f"   |   located {located}/{total} targets so far",
                            curses.color_pair(COLOR_NORMAL))
                _draw_bar(stdscr, y + 1, 2, w - 4, 0, indeterminate_offset=frame)
            else:
                pct = (resolved / total * 100) if total else 100
                safe_addstr(stdscr, y, 2,
                            f"Resolved {resolved}/{total} targets ({pct:0.0f}%)",
                            curses.color_pair(COLOR_NORMAL))
                _draw_bar(stdscr, y + 1, 2, w - 4, (resolved / total) if total else 1.0)

            current = snap["current_target"] or snap["current_path"]
            spin = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)] if snap["phase"] != "done" else "*"
            safe_addstr(stdscr, y + 3, 2, f"{spin} Currently: {current}"[: w - 4],
                        curses.color_pair(COLOR_ACCENT) | curses.A_BOLD)

            # --- live checklist ---
            list_top = y + 5
            list_h = h - list_top - 2
            # keep the active target roughly centered in the viewport
            active_idx = targets.index(snap["current_target"]) if snap["current_target"] in targets else 0
            top = max(0, min(active_idx - list_h // 2, max(0, total - list_h)))

            for row, idx in enumerate(range(top, min(total, top + list_h))):
                name = targets[idx]
                status = snap["status"].get(name, "pending")
                box, color = STATUS_BOX.get(status, ("[ ]", COLOR_NORMAL))
                count = snap["match_count"].get(name, 0)
                suffix = f"  ({count} found)" if status == "located" and count > 1 else ""
                line = f"{box} {name}{suffix}"
                attr = curses.color_pair(color)
                if status == "copying":
                    attr |= curses.A_BOLD
                safe_addstr(stdscr, list_top + row, 2, line, attr)

            draw_footer(stdscr, "Please wait -- this cannot be interrupted safely.")
            stdscr.refresh()

            if not thread.is_alive() and tracker.done:
                break
            stdscr.getch()  # non-blocking (nodelay); discards any keypress
            time.sleep(0.12)
            frame += 1
    finally:
        stdscr.nodelay(False)

    thread.join()
    return tracker.results


def results_screen(stdscr, results, dest_root):
    status_colors = {
        "copied": COLOR_OK,
        "already_present": COLOR_WARN,
        "not_found": COLOR_ERR,
        "error": COLOR_ERR,
    }
    cursor = 0
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_header(stdscr, "Results", f"Destination: {dest_root}")
        list_top = 4
        list_h = h - list_top - 2
        if cursor < top:
            top = cursor
        if cursor >= top + list_h:
            top = cursor - list_h + 1
        for row, idx in enumerate(range(top, min(len(results), top + list_h))):
            r = results[idx]
            y = list_top + row
            color = curses.color_pair(status_colors.get(r["status"], COLOR_NORMAL))
            is_cur = (idx == cursor)
            if is_cur:
                color = curses.color_pair(COLOR_SELECT)
            tag = r["status"].upper().ljust(16)
            safe_addstr(stdscr, y, 2, f"{tag} {r['name']}", color)
        draw_footer(stdscr, "Up/Down scroll  Enter detail  n new job  q quit")
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')) and results:
            cursor = (cursor - 1) % len(results)
        elif key in (curses.KEY_DOWN, ord('j')) and results:
            cursor = (cursor + 1) % len(results)
        elif key in (curses.KEY_ENTER, 10, 13) and results:
            r = results[cursor]
            message_box(stdscr, [f"{r['name']} [{r['status']}]", "", r["detail"]])
        elif key == ord('n'):
            return "new"
        elif key in (ord('q'), 27):
            return "quit"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_job(stdscr):
    picked = browse_for_extraction(stdscr)
    if picked is None:
        return "quit"
    kind, path = picked

    dtype = choose_extraction_type(stdscr)
    if dtype is None:
        return "new"

    selected = choose_databases(stdscr, dtype)
    if selected is None:
        return "new"

    if kind == "folder":
        dest_root = path / "Exported"
    else:
        dest_root = path.parent / "Exported"

    if not confirm_screen(stdscr, kind, path, dtype, selected, dest_root):
        return "new"

    dest_root.mkdir(parents=True, exist_ok=True)

    tracker = ProgressTracker()
    if kind == "folder":
        worker = threading.Thread(target=copy_from_folder,
                                   args=(path, selected, dest_root),
                                   kwargs={"progress": tracker}, daemon=True)
    else:
        worker = threading.Thread(target=copy_from_zip,
                                   args=(path, selected, dest_root),
                                   kwargs={"progress": tracker}, daemon=True)
    worker.start()
    results = progress_screen(stdscr, tracker, worker)

    return results_screen(stdscr, results, dest_root)


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    init_colors()
    while True:
        outcome = run_job(stdscr)
        if outcome == "quit":
            break
        # outcome == "new" -> loop back to a fresh browse/select/copy cycle


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="db_ripper_tui.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{ASCII_BANNER}\n{APP_NAME} v{__version__} -- terminal UI for "
                    f"locating mobile-extraction artifacts.")
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
        if config_message.startswith("!"):
            input("Press Enter to continue with built-in defaults (Ctrl+C to quit)... ")
        else:
            time.sleep(1.2)
 
    curses.wrapper(main)
