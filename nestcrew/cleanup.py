"""Clean-up toolkit: scan a folder, find duplicates and junk, sort by type or month,
move big sets of files in batches, and "delete" to the Recycle Bin.

Same rules as every RPA tool: looking is automatic, every change shows you the exact
files first and waits for your approval, nothing is overwritten, moves can be undone,
and "delete" means Recycle Bin (restorable) - never a permanent delete.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path

from .rpa import (AGENT_CTX, _free_name, _save_undo, _size, resolve_path, show)
from .tools import tool

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff",
               ".raw", ".cr2", ".nef", ".arw", ".dng", ".svg"},
    "Videos": {".mp4", ".mov", ".mkv", ".avi", ".3gp", ".m4v", ".webm", ".wmv", ".flv", ".mts"},
    "Audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".amr", ".wma"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".md",
                  ".odt", ".ods", ".rtf", ".epub", ".json", ".xml"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "Apps": {".apk", ".exe", ".msi", ".appx", ".dmg"},
}
KIND_PATTERNS = {k.lower(): ",".join("*" + e for e in sorted(v)) for k, v in CATEGORIES.items()}
KIND_PATTERNS.update({"photos": KIND_PATTERNS["images"], "pictures": KIND_PATTERNS["images"],
                      "pics": KIND_PATTERNS["images"], "movies": KIND_PATTERNS["videos"],
                      "music": KIND_PATTERNS["audio"], "songs": KIND_PATTERNS["audio"],
                      "screenshots": "screenshot*,*screenshot*",
                      "screen recordings": "screenrecord*,screen_record*,*screen recording*"})
JUNK = ("*.tmp", "*.temp", "~$*", "thumbs.db", ".ds_store", "*.crdownload", "*.part", "*.partial",
        "*.download", "._*", "*.bak~")
SKIP_DIRS = {".git", "node_modules", "$recycle.bin", "system volume information", "appdata",
             "nestcrew recycle"}
BATCH_FILES, BATCH_BYTES = 200, 1 << 30          # auto batches: at most 200 files or 1 GB each
MAX_SCAN = 50_000


def category(p: Path) -> str:
    ext = p.suffix.lower()
    return next((k for k, v in CATEGORIES.items() if ext in v), "Other")


def _walk(root: Path, recursive: bool = True):
    """Files under root (hidden/system folders skipped), capped so a whole drive can't hang us."""
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if recursive and e.name.lower() not in SKIP_DIRS and not e.name.startswith("."):
                    stack.append(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                n += 1
                if n > MAX_SCAN:
                    return
                yield Path(e.path)


def _is_junk(p: Path) -> bool:
    import fnmatch
    name = p.name.lower()
    try:
        empty = p.stat().st_size == 0 and p.suffix.lower() not in (".gitkeep", ".keep")
    except OSError:
        empty = False
    return empty or any(fnmatch.fnmatch(name, j) for j in JUNK)


def _hash(p: Path, first_only: bool = False) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(p, "rb") as f:
        if first_only:
            h.update(f.read(64 * 1024))
        else:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def duplicate_groups(root: Path, recursive: bool = True) -> list[list[Path]]:
    """Exact duplicates: same size, then same first 64 KB, then same full content."""
    by_size = defaultdict(list)
    for f in _walk(root, recursive):
        try:
            s = f.stat().st_size
        except OSError:
            continue
        if s > 0:
            by_size[s].append(f)
    groups = []
    for same in by_size.values():
        if len(same) < 2:
            continue
        for stage in (True, False):
            regrouped = defaultdict(list)
            for f in same:
                try:
                    regrouped[_hash(f, stage)].append(f)
                except OSError:
                    pass
            same = [f for g in regrouped.values() if len(g) > 1 for f in g]
            if stage:
                continue
            for g in regrouped.values():
                if len(g) > 1:     # keep the oldest copy (then the shortest path); the rest are extra
                    groups.append(sorted(g, key=lambda x: (x.stat().st_mtime, len(str(x)), str(x))))
    return sorted(groups, key=lambda g: -g[0].stat().st_size * (len(g) - 1))


def _empty_dirs(root: Path) -> list[Path]:
    """Folders with no files anywhere inside (a folder holding only empty folders counts too).
    Only the top-most ones are returned - recycling a folder takes its empty subfolders with it."""
    empty: set[Path] = set()
    for d, subdirs, files in os.walk(root, topdown=False):
        p = Path(d)
        if p == root or p.name.lower() in SKIP_DIRS:
            continue
        try:
            entries = list(p.iterdir())
        except OSError:
            continue
        if all(e.is_dir() and e in empty for e in entries):
            empty.add(p)
    return sorted(p for p in empty if p.parent not in empty)


# ------------------------------------------------------------------ reading --
@tool("scan_folder", "Report on a folder: size by file type, largest files, duplicates, junk and empty folders "
      "(nothing is changed). Use this first when asked to clean, sort or free up space.",
      '{"path": "Desktop/BIRAC2"}')
def scan_folder(path: str, recursive: bool = True) -> str:
    root = resolve_path(path)
    if not root.is_dir():
        raise ValueError(f"'{show(root)}' isn't a folder")
    t0 = time.time()
    by_cat, files = defaultdict(lambda: [0, 0]), []
    old = time.time() - 365 * 86400
    old_n = junk_n = 0
    for f in _walk(root, recursive):
        try:
            st = f.stat()
        except OSError:
            continue
        files.append((st.st_size, f))
        c = by_cat[category(f)]
        c[0] += 1
        c[1] += st.st_size
        old_n += st.st_mtime < old
        junk_n += _is_junk(f)
    total = sum(s for s, _ in files)
    dups = duplicate_groups(root, recursive)
    extra = sum(len(g) - 1 for g in dups)
    reclaim = sum(g[0].stat().st_size * (len(g) - 1) for g in dups)
    empties = _empty_dirs(root) if recursive else []
    lines = [f"{show(root)}: {len(files)} file(s), {_size(total)}" + (" (stopped counting at 50,000)"
                                                                      if len(files) >= MAX_SCAN else "")]
    for k, (n, s) in sorted(by_cat.items(), key=lambda kv: -kv[1][1]):
        lines.append(f"  {k}: {n} file(s), {_size(s)}")
    if files:
        lines.append("Largest:")
        lines += [f"  {_size(s)}  {show(f)}" for s, f in sorted(files, key=lambda x: -x[0])[:8]]
    lines.append(f"Duplicates: {extra} extra copie(s) in {len(dups)} group(s) - {_size(reclaim)} could be freed")
    lines.append(f"Junk (temp / partial downloads / empty files): {junk_n}")
    lines.append(f"Empty folders: {len(empties)}")
    lines.append(f"Not changed for over a year: {old_n} file(s)")
    lines.append(f"(scanned in {time.time() - t0:.1f}s)")
    return "\n".join(lines)


@tool("find_duplicates", "List files that are exact copies of each other inside a folder (nothing is changed).",
      '{"path": "Pictures"}')
def find_duplicates(path: str, recursive: bool = True) -> str:
    root = resolve_path(path)
    groups = duplicate_groups(root, recursive)
    if not groups:
        return f"No duplicate files in {show(root)}."
    out = [f"{sum(len(g) - 1 for g in groups)} extra copie(s) in {len(groups)} group(s) in {show(root)} "
           "(the first of each group is kept):"]
    for g in groups[:30]:
        out.append(f"  keep {show(g[0])}  ({_size(g[0].stat().st_size)})")
        out += [f"     copy {show(x)}" for x in g[1:4]] + ([f"     ... +{len(g) - 4} more"] if len(g) > 4 else [])
    return "\n".join(out)


# ------------------------------------------------------------------ changing --
def _trash(p: Path) -> str:
    """Send to the Recycle Bin (restorable). Without send2trash: a 'NestCrew Recycle' folder."""
    try:
        from send2trash import send2trash
        send2trash(str(p))
        return "Recycle Bin"
    except ImportError:
        pass
    bin_dir = Path(p.anchor) / "NestCrew Recycle" if os.name == "nt" else Path.home() / "NestCrew Recycle"
    try:
        dest_dir = bin_dir / time.strftime("%Y-%m-%d")
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        dest_dir = resolve_path("workspace") / "NestCrew Recycle"
        dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(_free_name(dest_dir, p.name)))
    return str(dest_dir)


def _recycle_targets(root: Path, what: str, pattern: str, recursive: bool) -> list[Path]:
    what = (what or "").lower().strip()
    if what == "duplicates":
        return [x for g in duplicate_groups(root, recursive) for x in g[1:]]
    if what == "junk":
        return [f for f in _walk(root, recursive) if _is_junk(f)]
    if what in ("empty_folders", "empty folders", "empty"):
        return _empty_dirs(root)
    if pattern:
        from .rpa import _matches
        return _matches(root, pattern, recursive)
    raise ValueError("Say what to recycle: what = duplicates, junk or empty_folders, or a pattern like *.tmp")


@tool("recycle_files", "Send files to the Recycle Bin (restorable, asks you first): what = duplicates "
      "(keeps one copy), junk (temp/partial/empty files), empty_folders, or a pattern like *.tmp.",
      '{"path": "Desktop/BIRAC2", "what": "duplicates"}')
def recycle_files(path: str, what: str = "", pattern: str = "", recursive: bool = True) -> str:
    root = resolve_path(path)
    targets = _recycle_targets(root, what, pattern, recursive)
    if not targets:
        return f"Nothing to recycle in {show(root)} ({what or pattern})."
    done, where = [], ""
    for p in targets:
        try:
            where = _trash(p)
            done.append(show(p))
        except OSError as e:
            done.append(f"{show(p)} - skipped ({e.strerror or e})")
        _progress(f"Recycled {len(done)}/{len(targets)}")
    freed = ""
    return (f"Sent {len(done)} item(s) from {show(root)} to the {where or 'Recycle Bin'}{freed} - "
            "restore them from there if needed:\n" + "\n".join(done[:40]) +
            (f"\n... and {len(done) - 40} more" if len(done) > 40 else ""))


def _organize_plan(root: Path, by: str) -> list[tuple[Path, Path]]:
    by = (by or "type").lower()
    plan = []
    for f in sorted(p for p in root.iterdir() if p.is_file() and not p.name.startswith((".", "~$"))
                    and p.name.lower() != "desktop.ini"):
        if by == "type":
            sub = category(f)
        elif by in ("month", "date"):
            sub = time.strftime("%Y-%m", time.localtime(f.stat().st_mtime))
        elif by == "year":
            sub = time.strftime("%Y", time.localtime(f.stat().st_mtime))
        else:
            raise ValueError("by must be type, month or year")
        plan.append((f, root / sub))
    return plan


@tool("organize_folder", "Sort the files directly inside a folder into subfolders by type (Images, Videos, "
      "Documents...), month (2026-01...) or year. Asks you first; can be undone.",
      '{"path": "Downloads", "by": "type"}')
def organize_folder(path: str, by: str = "type") -> str:
    root = resolve_path(path)
    plan = _organize_plan(root, by)
    if not plan:
        return f"Nothing to sort in {show(root)}."
    log, counts = [], defaultdict(int)
    for f, dest in plan:
        dest.mkdir(exist_ok=True)
        target = _free_name(dest, f.name)
        shutil.move(str(f), str(target))
        log.append([str(f), str(target)])
        counts[dest.name] += 1
    _save_undo(log)
    return f"Sorted {len(log)} file(s) in {show(root)} by {by}: " + \
        ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ". Say 'undo last move' to put them back."


# ----------------------------------------------------------------- batches --
def batches(files: list[Path]) -> list[list[Path]]:
    """Group files so each batch stays under ~200 files or ~1 GB: big videos go a few at a
    time, thousands of small photos go 200 at a time. Stop is checked between batches."""
    out, cur, cur_bytes = [], [], 0
    for f in files:
        try:
            s = f.stat().st_size
        except OSError:
            s = 0
        if cur and (len(cur) >= BATCH_FILES or cur_bytes + s > BATCH_BYTES):
            out.append(cur)
            cur, cur_bytes = [], 0
        cur.append(f)
        cur_bytes += s
    if cur:
        out.append(cur)
    return out


def _progress(text: str) -> None:
    ctx = AGENT_CTX.get()
    if not ctx:
        return
    gate = ctx.get("gate")
    if gate is not None:
        gate.check_stop()                       # Stop works between files/batches
    emit = ctx.get("emit")
    if emit:
        emit({"type": "status", "text": text})


# ---------------------------------------------------------- preview / checks --
def preview(tool_name: str, args: dict) -> str | None:
    if tool_name == "recycle_files":
        root = resolve_path(args.get("path", ""))
        targets = _recycle_targets(root, args.get("what", ""), args.get("pattern", ""),
                                   args.get("recursive", True))
        size = sum(p.stat().st_size for p in targets if p.is_file())
        lines = "\n".join(f"  • {show(p)}" for p in targets[:15]) + \
            (f"\n  … and {len(targets) - 15} more" if len(targets) > 15 else "")
        label = {"duplicates": "duplicate copies (one copy of each is kept)", "junk": "junk files",
                 "empty_folders": "empty folders"}.get(args.get("what", ""), f"files matching {args.get('pattern')}")
        return (f"RECYCLE {len(targets)} {label}\nfrom {show(root)}  ({_size(size)})\n\n{lines}\n\n"
                "They go to the Recycle Bin - you can restore them from there.")
    if tool_name == "organize_folder":
        root = resolve_path(args.get("path", ""))
        plan = _organize_plan(root, args.get("by", "type"))
        counts = defaultdict(list)
        for f, d in plan:
            counts[d.name].append(f.name)
        body = "\n".join(f"  {k}/  ← {len(v)} file(s): {', '.join(v[:4])}{' …' if len(v) > 4 else ''}"
                         for k, v in sorted(counts.items()))
        return f"SORT {len(plan)} file(s) in {show(root)} by {args.get('by', 'type')}\n\n{body}\n\nYou can undo this afterwards."
    return None


def nothing_to_do(tool_name: str, args: dict) -> str | None:
    """Skip the approval card when a step would change nothing."""
    try:
        if tool_name == "recycle_files":
            root = resolve_path(args.get("path", ""))
            if not _recycle_targets(root, args.get("what", ""), args.get("pattern", ""), args.get("recursive", True)):
                label = (args.get("what") or args.get("pattern") or "").replace("_", " ")
                return f"No {label} in {show(root)} - nothing to recycle."
        if tool_name == "organize_folder":
            if not _organize_plan(resolve_path(args.get("path", "")), args.get("by", "type")):
                return "Nothing to sort."
    except (ValueError, OSError):
        return None
    return None
