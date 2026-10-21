"""RPA toolkit: files, folders, Excel/CSV, and delegating to specialist agents.

Where agents may work
    The workspace plus the folders in config.yaml `rpa.folders` (default: your
    Desktop, Documents, Downloads, Pictures, Videos, Music). Paths can be written
    the way people say them: "Desktop/BIRAC", "Downloads", "Documents/invoices/a.xlsx".
    Hidden/credential folders (.ssh, AppData, .env ...) are always refused.

Safety
    - Looking (list, find, info, read Excel) never asks.
    - Every change (copy, move, rename, new folder, write/append Excel) is an "ask"
      tool: you see exactly which files are affected and approve it in chat.
    - Nothing is ever overwritten: clashes get " (2)" names, and Excel files are
      backed up before they change. There is NO delete tool.
    - Moves are logged so `undo_last_move` can put files back.
"""
from __future__ import annotations

import contextvars
import csv
import fnmatch
import json
import os
import re
import shutil
import time
from pathlib import Path

from .config import ROOT, load_config, resolve
from . import tools as _tools
from .tools import tool


def workspace() -> Path:
    return _tools.workspace()          # looked up each time (config/tests can change it)

ALIASES = ["desktop", "documents", "downloads", "pictures", "videos", "music"]
BLOCKED_PARTS = {".ssh", ".gnupg", ".aws", ".azure", "appdata", ".env", ".git", "ntuser.dat",
                 "windows", "program files", "program files (x86)", "programdata", "system32"}
MAX_FILES = 20000          # big jobs are done in batches (see cleanup.batches)
CHANGE_TOOLS = {"make_folder", "copy_files", "move_files", "rename_file", "write_excel",
                "append_excel_rows", "undo_last_move", "recycle_files", "organize_folder", "phone_copy",
                "phone_link_start", "send_to_phone", "bluetooth_send", "bluetooth_receive"}
READ_TOOLS = {"phone_link_status", "drive_space", "phone_list", "scan_folder", "find_duplicates", "find_folder", "list_folder", "find_files", "file_info", "read_text", "read_excel"}


# ------------------------------------------------------------------ paths ----
def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _known(name: str) -> Path:
    """Desktop/Documents may be redirected into OneDrive on Windows."""
    home = _home()
    title = name.capitalize()
    for cand in (home / title, home / "OneDrive" / title):
        if cand.exists():
            return cand
    return home / title


def drive_roots() -> list[Path]:
    """Other drives (D:, E:, USB sticks, SD cards) - never the Windows drive itself."""
    if os.name != "nt":
        return []
    import string
    system = (os.environ.get("SystemDrive") or "C:").upper().rstrip("\\") + "\\"
    out = []
    for letter in string.ascii_uppercase[2:]:
        root = Path(f"{letter}:\\")
        if str(root).upper() != system and root.exists():
            out.append(root)
    return out


def allowed_roots() -> list[Path]:
    cfg = load_config().get("rpa", {}) or {}
    roots = [workspace()]
    for f in cfg.get("folders") or ALIASES:
        if str(f).lower() == "drives":            # D:, E:, USB sticks, SD cards
            roots += drive_roots()
            continue
        p = _known(f) if f.lower() in ALIASES else Path(os.path.expanduser(f))
        roots.append(p)
    out = []
    for r in roots:
        try:
            out.append(r.resolve())
        except OSError:
            pass
    return out


def _norm(p: Path) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower() if os.name == "nt" else str(p).rstrip("/")


def resolve_path(text: str) -> Path:
    """Turn 'Desktop/BIRAC' or an absolute path into a real path inside an allowed folder."""
    raw = str(text or "").strip().strip('"').strip("'").replace("\\", "/")
    if not raw:
        raise ValueError("Empty path")
    raw = os.path.expanduser(raw)
    first, _, rest = raw.partition("/")
    if first.lower() in ALIASES:
        p = _known(first) / rest
    elif first.lower() == "workspace":
        p = workspace() / rest
    elif os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        p = Path(raw)
    else:                              # a bare name like "BIRAC": workspace, else Desktop/Documents/...
        p = workspace() / raw
        if not p.exists():
            found = locate(raw)
            if found:
                p = _known(found.split("/")[0]) / raw
    p = p.resolve()
    if p == ROOT.resolve() or (ROOT.resolve() in p.parents and workspace() not in [p, *p.parents]):
        raise ValueError("NestCrew's own program folder is off-limits (use the workspace)")
    roots = allowed_roots()
    np = _norm(p)
    inside = [r for r in roots if np == _norm(r) or np.startswith(_norm(r) + "/")]
    if not inside:
        names = ", ".join(r.name for r in roots)
        raise ValueError(f"'{text}' is outside the folders NestCrew may use ({names}). "
                         "Add it under rpa.folders in config.yaml if you want to allow it.")
    # Only the part below Desktop/Documents/... is checked, so it doesn't matter where Windows
    # keeps those folders - but Desktop/.ssh or Documents/AppData/... stay refused.
    root = max(inside, key=lambda r: len(_norm(r)))
    rel = np[len(_norm(root)):].strip("/")
    if {x.lower() for x in rel.split("/") if x} & BLOCKED_PARTS:
        raise ValueError(f"'{text}' is in a protected system or private folder")
    return p


def _search_roots() -> list[str]:
    cfg = load_config().get("rpa", {}) or {}
    return [a for a in ALIASES if a in [str(f).lower() for f in (cfg.get("folders") or ALIASES)]]


def search_folders(name: str, max_depth: int = 4, limit: int = 20) -> list[str]:
    """Look for a folder by name inside Desktop, Documents, Downloads... (subfolders too)."""
    want = name.strip().lower()
    hits, seen = [], 0
    for alias in _search_roots():
        base = _known(alias)
        if not base.is_dir():
            continue
        stack = [(base, 0)]
        while stack and len(hits) < limit and seen < 20000:
            folder, depth = stack.pop()
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                seen += 1
                if not e.is_dir(follow_symlinks=False) or e.name.startswith(".") or \
                        e.name.lower() in BLOCKED_PARTS:
                    continue
                if e.name.lower() == want:
                    hits.append(show(Path(e.path)))
                if depth + 1 < max_depth:
                    stack.append((Path(e.path), depth + 1))
    return hits


def locate(name: str) -> str | None:
    """Find a bare folder/file name in the user's folders: 'BIRAC' -> 'Desktop/BIRAC'.
    Looks at the top of Desktop, Documents... first, then searches their subfolders.
    Raises ValueError when the name is in several places (you're asked which one)."""
    clean = str(name).replace("\\", "/").strip("/")
    first, _, rest = clean.partition("/")
    if not first or first.lower() in ALIASES or first.lower() == "workspace" or \
            ".." in clean.split("/") or first.startswith(("~", ".")) or ":" in first:
        return None
    top = [f"{a.capitalize()}/{first}" for a in _search_roots() if (_known(a) / first).exists()]
    hits = top or search_folders(first)                # top of Desktop/Documents... wins over deeper
    if rest:
        hits = [h for h in hits if (resolve_path(h) / rest).exists()] or hits
    if len(hits) == 1:
        return hits[0] + (f"/{rest}" if rest else "")
    if len(hits) > 1:
        raise ValueError(f"I found '{first}' in {len(hits)} places: " + "; ".join(hits[:6]) +
                         f". Tell me which one, e.g. \"{hits[0]}\".")
    return None


@tool("find_folder", "Search Desktop, Documents, Downloads... (and their subfolders) for a folder by name.",
      '{"name": "BIRAC"}')
def find_folder(name: str) -> str:
    hits = search_folders(name)
    if not hits:
        return f"No folder named '{name}' found in {', '.join(a.capitalize() for a in _search_roots())}."
    return f"Found {len(hits)} folder(s) named '{name}':\n" + "\n".join(hits)


def show(p: Path) -> str:
    """Friendly display: Desktop/BIRAC/a.pdf instead of a long absolute path."""
    for r in sorted(allowed_roots(), key=lambda r: -len(str(r))):
        if _norm(p) == _norm(r) or _norm(p).startswith(_norm(r) + "/"):
            label = "workspace" if r == workspace().resolve() else r.name
            rel = p.relative_to(r) if str(p).lower() != str(r).lower() else Path("")
            return f"{label}/{rel.as_posix()}".rstrip("/") if str(rel) not in ("", ".") else label
    return str(p)


def _matches(source: Path, pattern: str, recursive: bool = False) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise ValueError(f"'{show(source)}' doesn't exist")
    pats = [x.strip() for x in (pattern or "*").split(",") if x.strip()] or ["*"]
    if recursive:                      # skip hidden/system folders (.git, AppData, $Recycle.Bin...)
        from .cleanup import _walk
        it = _walk(source, True)
    else:
        it = source.iterdir()
    out = [f for f in it if f.is_file() and not f.name.startswith(("~$", "."))
           and any(fnmatch.fnmatch(f.name.lower(), p.lower()) for p in pats)]
    return sorted(out)[:MAX_FILES]


def _free_name(dest_dir: Path, name: str, taken: set | None = None) -> Path:
    taken = taken or set()
    target = dest_dir / name
    if not target.exists() and target not in taken:
        return target
    stem, suf = Path(name).stem, Path(name).suffix
    i = 2
    while (dest_dir / f"{stem} ({i}){suf}").exists() or (dest_dir / f"{stem} ({i}){suf}") in taken:
        i += 1
    return dest_dir / f"{stem} ({i}){suf}"


def _flag(v) -> bool:
    """Models send true / "true" / "yes" / 1 for yes."""
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ----------------------------------------------------------------- looking ----
@tool("list_folder", "List what is inside a folder (Desktop, Documents, Downloads, workspace...).",
      '{"path": "Desktop/BIRAC"}')
def list_folder(path: str = "workspace") -> str:
    p = resolve_path(path)
    if not p.is_dir():
        raise ValueError(f"'{show(p)}' is not a folder")
    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [f"{'[folder] ' if x.is_dir() else ''}{x.name}" + ("" if x.is_dir() else f"  ({_size(x.stat().st_size)})")
             for x in items if not x.name.startswith((".", "~$"))][:200]
    return f"{show(p)} contains {len(lines)} item(s):\n" + ("\n".join(lines) or "(empty)")


@tool("find_files", "Find files by name pattern, e.g. *.pdf, in a folder (recursive=true includes subfolders).",
      '{"folder": "Downloads", "pattern": "*.pdf", "recursive": false}')
def find_files(folder: str, pattern: str = "*", recursive: bool = False) -> str:
    files = _matches(resolve_path(folder), pattern, bool(recursive))
    return f"{len(files)} file(s):\n" + "\n".join(show(f) for f in files[:100])


@tool("file_info", "Size and dates of a file or folder.", '{"path": "Desktop/report.xlsx"}')
def file_info(path: str) -> str:
    p = resolve_path(path)
    st = p.stat()
    kind = "folder" if p.is_dir() else "file"
    return (f"{show(p)}: {kind}, {_size(st.st_size)}, modified "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")


@tool("read_text", "Read a text, csv, md or json file from an allowed folder.", '{"path": "Documents/notes.txt"}')
def read_text(path: str) -> str:
    p = resolve_path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    return text[:8000] + ("\n...[truncated]" if len(text) > 8000 else "")


# ------------------------------------------------------------------ files ----
@tool("make_folder", "Create a new folder (asks you first).", '{"path": "Desktop/BIRAC2"}')
def make_folder(path: str) -> str:
    p = resolve_path(path)
    if p.exists():
        return f"{show(p)} already exists"
    p.mkdir(parents=True)
    return f"Created folder {show(p)}"


def split_sources(source) -> list[str]:
    """'Downloads; Desktop/Phone' or ['Downloads', 'Desktop'] -> list of folders."""
    if isinstance(source, (list, tuple)):
        return [str(x).strip() for x in source if str(x).strip()]
    return [x.strip() for x in re.split(r"\s*;\s*", str(source or "")) if x.strip()]


def gather(source, pattern: str, recursive: bool = False) -> tuple[list[Path], list[Path]]:
    """All matching files from one or more folders (subfolders too if recursive)."""
    srcs = [resolve_path(x) for x in split_sources(source)]
    if not srcs:
        raise ValueError("No source folder given")
    files, seen = [], set()
    for sp in srcs:
        for f in _matches(sp, pattern, recursive):
            if f not in seen:
                seen.add(f)
                files.append(f)
    return srcs, files


def _transfer(source, destination: str, pattern: str, move: bool, recursive: bool = False) -> str:
    srcs, files = gather(source, pattern, _flag(recursive))
    dst = resolve_path(destination)
    srcs_txt = ", ".join(show(x) for x in srcs)
    if not files:
        return f"No files matching '{pattern or '*'}' in {srcs_txt}"
    files = [f for f in files if dst not in f.parents]      # never re-take what's already in the target
    dst.mkdir(parents=True, exist_ok=True)
    from concurrent.futures import ThreadPoolExecutor
    from .cleanup import _progress, batches
    groups = batches(files)
    done, log, same = [], [], []
    t0 = time.time()
    try:
        for i, group in enumerate(groups, 1):
            if len(groups) > 1:
                _progress(f"Batch {i}/{len(groups)}: {len(group)} file(s)")   # Stop is checked here
            todo = []
            for f in group:                                # pick names first (no clashes), then work
                if not move and _same_file(f, dst / f.name):  # already there, identical: don't make a (2)
                    same.append(f.name)
                    continue
                todo.append((f, _free_name(dst, f.name, taken={t for _, t in todo})))
            if move:                                       # same drive: a move is an instant rename
                for f, target in todo:
                    shutil.move(str(f), str(target))
                    log.append([str(f), str(target)])
            else:                                          # copies run side by side
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda ft: shutil.copy2(str(ft[0]), str(ft[1])), todo))
            done += [f"{f.name} -> {show(t)}" for f, t in todo]
    finally:
        if move and log:
            _save_undo(log)                               # even if stopped half-way, undo works
    verb = "Moved" if move else "Copied"
    out = f"{verb} {len(done)} file(s) from {srcs_txt} to {show(dst)}"
    if time.time() - t0 > 5:
        out += f" in {time.time() - t0:.0f}s"
    if len(groups) > 1:
        out += f" in {len(groups)} batches"
    if same:
        out += f" ({len(same)} already there with the same content - skipped: {', '.join(same[:10])})"
    return out + ":\n" + "\n".join(done[:50]) + (f"\n... and {len(done) - 50} more" if len(done) > 50 else "")


def _same_file(a: Path, b: Path) -> bool:
    try:
        if not b.is_file() or a.stat().st_size != b.stat().st_size:
            return False
        import filecmp
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def similar_existing(p: Path) -> Path | None:
    """For a folder that doesn't exist yet: an existing sibling with nearly the same name."""
    if p.exists() or not p.parent.is_dir():
        return None
    import difflib
    want = re.sub(r"[\s_-]+(folder|directory|dir)$", "", p.name.lower()).strip()
    try:
        sibs = [e for e in p.parent.iterdir() if e.is_dir()]
    except OSError:
        return None
    for e in sibs:
        if e.name.lower() == want:
            return e
    best = difflib.get_close_matches(p.name.lower(), [e.name.lower() for e in sibs], n=1, cutoff=0.85)
    return next((e for e in sibs if best and e.name.lower() == best[0]), None)


@tool("copy_files", "Copy files from a folder (or one file, or several folders separated by ;) into another folder. "
      "pattern like *.pdf or *.jpg,*.png; recursive=true includes subfolders. Big sets go in batches. Asks you first.",
      '{"source": "Downloads", "destination": "Documents/Invoices", "pattern": "*.pdf"}')
def copy_files(source: str, destination: str, pattern: str = "*", recursive: bool = False) -> str:
    return _transfer(source, destination, pattern, move=False, recursive=recursive)


@tool("move_files", "Move files from a folder (or several separated by ;) into another folder. pattern like *.jpg; "
      "recursive=true includes subfolders. Big sets go in batches. Asks you first; can be undone.",
      '{"source": "Desktop/BIRAC", "destination": "Desktop/BIRAC2", "pattern": "*"}')
def move_files(source: str, destination: str, pattern: str = "*", recursive: bool = False) -> str:
    return _transfer(source, destination, pattern, move=True, recursive=recursive)


@tool("rename_file", "Rename one file or folder (asks you first).", '{"path": "Desktop/a.txt", "new_name": "b.txt"}')
def rename_file(path: str, new_name: str) -> str:
    p = resolve_path(path)
    if "/" in new_name or "\\" in new_name:
        raise ValueError("new_name is just a name, not a path")
    target = _free_name(p.parent, new_name)
    resolve_path(str(target))
    p.rename(target)
    _save_undo([[str(p), str(target)]])
    return f"Renamed {show(p)} -> {target.name}"


def _undo_path() -> Path:
    return resolve("./data/rpa_undo.json")


def _save_undo(pairs: list) -> None:
    p = _undo_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        hist = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = []
    hist.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "moves": pairs})
    p.write_text(json.dumps(hist[-20:], indent=1), encoding="utf-8")


@tool("undo_last_move", "Put back the files from the last move or rename (asks you first).", "{}")
def undo_last_move() -> str:
    p = _undo_path()
    try:
        hist = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = []
    if not hist:
        return "Nothing to undo"
    last = hist.pop()
    back = 0
    for src, dst in reversed(last["moves"]):
        if Path(dst).exists() and not Path(src).exists():
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dst, src)
            back += 1
    p.write_text(json.dumps(hist, indent=1), encoding="utf-8")
    return f"Put back {back} file(s) moved at {last['ts']}"


# ------------------------------------------------------------ Excel / CSV ----
def _rows_arg(rows) -> list[list]:
    if isinstance(rows, str):
        rows = json.loads(rows)
    if rows and isinstance(rows[0], dict):
        keys = list(rows[0])
        return [keys] + [[r.get(k, "") for k in keys] for r in rows]
    return [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]


def _backup(p: Path) -> str:
    if not p.exists():
        return ""
    bdir = workspace() / ".backups"
    bdir.mkdir(exist_ok=True)
    b = bdir / f"{p.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(p, b)
    return f" (old version backed up to workspace/.backups/{b.name})"


@tool("read_excel", "Read an Excel (.xlsx) or CSV file: shows the header and first rows.",
      '{"path": "Documents/sales.xlsx", "sheet": "", "max_rows": 30}')
def read_excel(path: str, sheet: str = "", max_rows: int = 30) -> str:
    p = resolve_path(path)
    max_rows = max(1, min(int(max_rows or 30), 200))
    if p.suffix.lower() in (".csv", ".tsv"):
        with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.reader(f, delimiter="\t" if p.suffix.lower() == ".tsv" else ","))
        sheets = ["(csv)"]
    else:
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True, data_only=True)
        sheets = wb.sheetnames
        ws = wb[sheet] if sheet and sheet in sheets else wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    total = len(rows)
    shown = ["\t".join("" if v is None else str(v) for v in r) for r in rows[:max_rows + 1]]
    return (f"{show(p)} - sheets: {', '.join(sheets)} - {max(0, total - 1)} data row(s)\n" + "\n".join(shown)
            + (f"\n... ({total - max_rows - 1} more rows)" if total > max_rows + 1 else ""))


@tool("write_excel", "Create or replace an Excel/CSV file with rows (list of lists, first row = headers). Asks you first; old file is backed up.",
      '{"path": "Desktop/report.xlsx", "rows": [["Name","Total"],["A",10]], "sheet": "Sheet1"}')
def write_excel(path: str, rows, sheet: str = "Sheet1") -> str:
    p = resolve_path(path)
    data = _rows_arg(rows)
    p.parent.mkdir(parents=True, exist_ok=True)
    note = _backup(p)
    if p.suffix.lower() == ".csv":
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows(data)
    else:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = (sheet or "Sheet1")[:31]
        for r in data:
            ws.append(r)
        if data:
            from openpyxl.styles import Font
            for c in ws[1]:
                c.font = Font(bold=True)
        wb.save(p)
    return f"Wrote {len(data)} row(s) to {show(p)}{note}"


@tool("append_excel_rows", "Add rows to the end of an existing Excel/CSV file (asks you first; backed up).",
      '{"path": "Documents/expenses.xlsx", "rows": [["2026-10-21","Taxi",250]], "sheet": ""}')
def append_excel_rows(path: str, rows, sheet: str = "") -> str:
    p = resolve_path(path)
    data = _rows_arg(rows)
    if not p.exists():
        return write_excel(path, data, sheet or "Sheet1")
    note = _backup(p)
    if p.suffix.lower() == ".csv":
        with open(p, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(data)
    else:
        from openpyxl import load_workbook
        wb = load_workbook(p)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        for r in data:
            ws.append(r)
        wb.save(p)
    return f"Added {len(data)} row(s) to {show(p)}{note}"


# --------------------------------------------------------------- previews ----
def preview(tool_name: str, args: dict) -> str:
    """What the permission card shows: the exact files that will be affected."""
    try:
        from . import cleanup, devices
        from . import phonelink
        extra_preview = (cleanup.preview(tool_name, args) or devices.preview(tool_name, args)
                         or phonelink.preview(tool_name, args))
        if extra_preview:
            return extra_preview
        if tool_name in ("copy_files", "move_files"):
            srcs, files = gather(args.get("source", ""), args.get("pattern") or "*", _flag(args.get("recursive", False)))
            dst = resolve_path(args.get("destination", ""))
            files = [f for f in files if dst not in f.parents]
            src_label = "; ".join(show(x) for x in srcs) + (" (and their subfolders)" if _flag(args.get("recursive", False)) else "")
            verb = "MOVE" if tool_name == "move_files" else "COPY"
            from .cleanup import batches
            total = sum(f.stat().st_size for f in files)
            nb = len(batches(files))
            many = len(srcs) > 1 or _flag(args.get("recursive", False))
            lines = "\n".join(f"  • {show(f) if many else f.name}" for f in files[:15]) + \
                (f"\n  … and {len(files) - 15} more" if len(files) > 15 else "")
            size_txt = f"  ({_size(total)}" + (f", in {nb} batches" if nb > 1 else "") + ")"
            extra = ""
            if not dst.exists():
                near = similar_existing(dst)
                extra = (f"\n⚠ NEW folder {show(dst)} will be created - did you mean the existing "
                         f"{show(near)}? Deny and say so if yes." if near else
                         f"\n(The folder {show(dst)} will be created.)")
            elif verb == "COPY":
                dup = [f for f in files if _same_file(f, dst / f.name)]
                if dup:
                    extra = f"\n({len(dup)} of them are already there with the same content - they'll be skipped.)"
            undo = "\nYou can undo this move afterwards." if verb == "MOVE" else ""
            return f"{verb} {len(files)} file(s){size_txt}\nfrom {src_label}\nto   {show(dst)}{extra}\n\n{lines}\n\nNothing is overwritten - name clashes get a (2).{undo}"
        if tool_name == "make_folder":
            return f"Create folder {show(resolve_path(args.get('path', '')))}"
        if tool_name == "rename_file":
            return f"Rename {show(resolve_path(args.get('path', '')))}\nto {args.get('new_name')}"
        if tool_name in ("write_excel", "append_excel_rows"):
            p = resolve_path(args.get("path", ""))
            data = _rows_arg(args.get("rows") or [])
            head = "\n".join("  " + " | ".join(str(v) for v in r) for r in data[:8])
            verb = "Add rows to" if tool_name == "append_excel_rows" else ("REPLACE" if p.exists() else "Create")
            return f"{verb} {show(p)} - {len(data)} row(s){' (backed up first)' if p.exists() else ''}\n\n{head}" + \
                (f"\n  … and {len(data) - 8} more" if len(data) > 8 else "")
        if tool_name == "undo_last_move":
            return "Put back the files from the most recent move/rename."
    except Exception as e:
        return f"{tool_name}: {json.dumps(args)[:300]}\n(Couldn't preview: {e})"
    return json.dumps(args, ensure_ascii=False)[:600]


# ------------------------------------------------------- specialist agents ----
# The main agent can hand a sub-task to a focused specialist with its own small
# tool set. Specialists use the same model and the same approval gate (so you
# still approve every risky step), and cannot delegate further.
SPECIALISTS = {
    "file_clerk": {"role": "File clerk", "goal": "Find, copy, move, rename and organise files and folders exactly as asked.",
                   "tools": ["find_folder", "list_folder", "find_files", "file_info", "make_folder", "copy_files", "move_files",
                             "rename_file", "undo_last_move"]},
    "cleaner": {"role": "Cleaner", "goal": "Free up space and tidy folders: scan first, then remove junk and "
                "duplicates to the Recycle Bin, sort files by type or month, and move big sets in batches.",
                "tools": ["scan_folder", "find_duplicates", "find_folder", "list_folder", "find_files",
                          "recycle_files", "organize_folder", "move_files", "copy_files", "make_folder",
                          "undo_last_move", "drive_space", "phone_list", "phone_copy", "phone_link_start",
                          "phone_link_status", "send_to_phone", "bluetooth_send", "bluetooth_receive"]},
    "excel_analyst": {"role": "Excel analyst", "goal": "Read spreadsheets, calculate totals, and write or append rows.",
                      "tools": ["list_folder", "find_files", "read_excel", "write_excel", "append_excel_rows",
                                "calculator"]},
    "researcher": {"role": "Researcher", "goal": "Find accurate facts (web when online, files otherwise).",
                   "tools": ["web_search", "web_fetch", "read_text", "find_files", "current_time"]},
    "writer": {"role": "Writer", "goal": "Write clear notes, reports and summaries and save them.",
               "tools": ["read_text", "read_file", "write_file", "list_files"]},
}

AGENT_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar("nestcrew_agent_ctx", default=None)


@tool("delegate", "Hand a sub-task to a specialist: file_clerk, cleaner, excel_analyst, researcher or writer. "
      "Give it a complete instruction; you get its result back.",
      '{"specialist": "file_clerk", "task": "Move all PDFs from Downloads to Documents/Invoices"}')
def delegate(specialist: str, task: str) -> str:
    from .agent import Agent
    ctx = AGENT_CTX.get()
    if not ctx:
        raise ValueError("delegate only works inside an agent run")
    if ctx.get("depth", 0) >= 1:
        raise ValueError("Specialists can't delegate further - do the step yourself")
    spec = SPECIALISTS.get(str(specialist).strip().lower().replace(" ", "_"))
    if not spec:
        raise ValueError(f"Unknown specialist. Choose one of: {', '.join(SPECIALISTS)}")
    emit = ctx.get("emit") or (lambda e: None)
    emit({"type": "delegate", "agent": ctx.get("role", "Agent"), "to": spec["role"], "task": task[:300]})
    sub = Agent(role=spec["role"], goal=spec["goal"], tools=spec["tools"], max_steps=6)
    sub._depth = ctx.get("depth", 0) + 1
    answer = sub.run(ctx["provider"], task, on_event=emit, gate=ctx.get("gate"))
    return f"{spec['role']} finished: {answer}"


# --------------------------------------------------------------- quick plans --
# Tiny models often misread plain file commands ("copy all files from A to B").
# These common shapes are turned into exact tool steps; approvals still apply.
_ROOT_WORDS = {"desktop": "Desktop", "documents": "Documents", "docs": "Documents",
               "downloads": "Downloads", "pictures": "Pictures", "photos": "Pictures",
               "videos": "Videos", "music": "Music", "workspace": "workspace"}
_KINDS = {"pdf": "*.pdf", "pdfs": "*.pdf", "excel": "*.xlsx", "excels": "*.xlsx", "xlsx": "*.xlsx",
          "csv": "*.csv", "csvs": "*.csv", "word": "*.docx", "docx": "*.docx", "txt": "*.txt",
          "text": "*.txt", "jpg": "*.jpg", "jpgs": "*.jpg", "png": "*.png", "pngs": "*.png",
          "mp4": "*.mp4", "zip": "*.zip", "pptx": "*.pptx", "powerpoint": "*.pptx"}

_TRANSFER = re.compile(
    r"^\s*(?:please\s+)?(?P<verb>copy|move|transfer)\s+"
    r"(?:(?:all|every)\s+)?(?:(?:the|my)\s+)?"
    r"(?:(?P<kind>\*\.\w+|\.?\w+)\s+)?(?:files?|documents?|items?|everything|contents?)?\s*"
    r"(?:from|in|inside)\s+(?:the\s+)?(?:folder\s+)?(?P<src>.+?)\s+"
    r"(?:to|into|in\s?to)\s+(?:(?:a|the)\s+)?(?:new\s+)?(?:folder\s+)?(?:called\s+|named\s+)?(?P<dst>.+?)"
    r"(?:\s+(?:on|in)\s+(?:my\s+|the\s+)?(?P<where>desktop|documents|downloads|pictures|videos|music|workspace))?"
    r"\s*[.!]?\s*$", re.I)
_MKDIR = re.compile(
    r"^\s*(?:please\s+)?(?:create|make)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called\s+|named\s+)?(?P<dst>.+?)"
    r"(?:\s+(?:on|in)\s+(?:my\s+|the\s+)?(?P<where>desktop|documents|downloads|pictures|videos|music|workspace))?"
    r"\s*[.!]?\s*$", re.I)


def _clean_path(text: str, where: str | None) -> str:
    quoted = text.strip()[:1] in "\"'`" and text.strip()[:1] != ""
    p = text.strip().strip("\"'`").strip().replace("\\", "/")
    if not quoted:                     # "BIRAC2 folder", "the BIRAC2 directory" -> BIRAC2
        p = re.sub(r"\s+(?:folder|directory|dir)$", "", p, flags=re.I)
    p = re.sub(r"^(?:the\s+|my\s+)?(?:folder\s+)?", "", p, flags=re.I).strip("\"' ")
    d = re.match(r"^(?:drive\s+([a-z])|([a-z])\s+drive|([a-z]):)(?=/|$|\s)", p, re.I)
    if d:                              # "D drive", "drive E", "E:" -> "D:/"
        letter = (d.group(1) or d.group(2) or d.group(3)).upper()
        return f"{letter}:/" + p[d.end():].strip().lstrip("/")
    first = p.split("/")[0].lower()
    if first in _ROOT_WORDS:
        return _ROOT_WORDS[first] + p[len(first):]
    if ":" in p[:3] or p.startswith("/") or p.startswith("~"):
        return p
    return f"{_ROOT_WORDS[where.lower()]}/{p}" if where else p


def _place(name: str, where: str | None) -> str:
    """'BIRAC2' -> 'Desktop/BIRAC2' (searched), 'my phone backup on desktop' -> 'Desktop/...'."""
    p = _clean_path(name, where)
    if not where and p.split("/")[0].lower() not in _ROOT_WORDS:
        try:
            p = locate(p) or p
        except ValueError:
            pass                               # ambiguous: the check before asking explains
    return p


_FOLDER = r"(?:(?:the|my)\s+)?(?:folder\s+)?(?P<path>.+?)(?:\s+(?:folder|directory|dir|drive))?"
_CLEAN = [re.compile(r"^\s*(?:please\s+)?make\s+" + _FOLDER + r"\s+(?:clean|tidy|neat)\s*(?:again)?\s*[.!]?\s*$", re.I),
          re.compile(r"^\s*(?:please\s+)?(?:clean|tidy|clear|declutter|free\s+(?:up\s+)?space\s+in)\s*(?:up)?\s+"
                     r"(?:in\s+)?" + _FOLDER + r"\s*[.!]?\s*$", re.I)]
_SORT = re.compile(r"^\s*(?:please\s+)?(?:organi[sz]e|sort|arrange|group)\s+(?:the\s+files\s+in\s+|files\s+in\s+)?"
                   + _FOLDER + r"(?:\s+(?:by|into)\s+(?P<by>type|kind|month|date|year)s?(?:\s+folders)?)?\s*[.!]?\s*$", re.I)
_DUPES = re.compile(r"^\s*(?:please\s+)?(?:(?P<act>delete|remove|recycle|clean|get\s+rid\s+of)\s+(?:the\s+|all\s+)?"
                    r"|(?:find|show|list|check)\s+(?:the\s+|all\s+|any\s+)?)duplicates?(?:\s+files)?\s+(?:in|from|on)\s+"
                    + _FOLDER + r"\s*[.!?]?\s*$", re.I)
_JUNK = re.compile(r"^\s*(?:please\s+)?(?:delete|remove|recycle|clean|get\s+rid\s+of)\s+(?:the\s+|all\s+)?"
                   r"(?P<what>junk|temp|temporary|empty\s+folders?)(?:\s+files)?\s+(?:in|from|on)\s+"
                   + _FOLDER + r"\s*[.!]?\s*$", re.I)
_SCAN = re.compile(r"^\s*(?:please\s+)?(?:scan|analy[sz]e|check|inspect|what(?:'s| is)\s+taking\s+(?:up\s+)?space\s+in"
                   r"|(?:find|show|list)\s+(?:the\s+)?(?:large|largest|big|biggest)\s+files\s+in)\s+" + _FOLDER +
                   r"\s*[.!?]?\s*$", re.I)


def _is_folder(p: str) -> bool:
    try:
        return resolve_path(p).is_dir()
    except ValueError as e:
        return "places" in str(e)              # same name in several places: we'll ask which


def _cleanup_plan(text: str, where: str | None) -> list[dict] | None:
    """Clean / sort / duplicates / junk / scan commands -> exact steps (each change still asks).
    Only for folders that exist - 'clean my room' is left to the model."""
    plan = _cleanup_steps(text, where)
    if plan and _is_folder(plan[0]["input"]["path"]):
        return plan
    return None


def _cleanup_steps(text: str, where: str | None) -> list[dict] | None:
    m = _DUPES.match(text)
    if m:
        p = _place(m.group("path"), where)
        if m.group("act"):
            return [{"tool": "find_duplicates", "input": {"path": p}},
                    {"tool": "recycle_files", "input": {"path": p, "what": "duplicates"}}]
        return [{"tool": "find_duplicates", "input": {"path": p}}]
    m = _JUNK.match(text)
    if m:
        what = "empty_folders" if "empty" in m.group("what").lower() else "junk"
        return [{"tool": "recycle_files", "input": {"path": _place(m.group("path"), where), "what": what}}]
    m = _SORT.match(text)
    if m:
        by = (m.group("by") or "type").lower()
        by = {"kind": "type", "date": "month"}.get(by, by)
        p = _place(m.group("path"), where)
        return [{"tool": "scan_folder", "input": {"path": p, "recursive": False}},
                {"tool": "organize_folder", "input": {"path": p, "by": by}}]
    m = _CLEAN[0].match(text) or _CLEAN[1].match(text)
    if m:
        p = _place(m.group("path"), where)
        return [{"tool": "scan_folder", "input": {"path": p}},
                {"tool": "recycle_files", "input": {"path": p, "what": "junk"}},
                {"tool": "recycle_files", "input": {"path": p, "what": "duplicates"}},
                {"tool": "recycle_files", "input": {"path": p, "what": "empty_folders"}}]
    m = _SCAN.match(text)
    if m:
        return [{"tool": "scan_folder", "input": {"path": _place(m.group("path"), where)}}]
    return None


_ACTION = re.compile(r"\b(clean|cleanup|tidy|declutter|organi[sz]e|sort|arrange|move|copy|transfer|back\s?up|"
                     r"delete|remove|recycle|rename|dedupe|duplicates?|free\s+up|scan|archive|zip|unzip|"
                     r"extract|empty|junk|batch)\b", re.I)
_OBJECT = re.compile(r"\b(files?|folders?|photos?|images?|pictures?|videos?|screenshots?|downloads|desktop|"
                     r"documents|drive|phone|dcim|pdfs?|excel|xlsx|csv|music|space|storage)\b", re.I)


def looks_like_file_action(text: str) -> bool:
    """'make BIRAC2 folder clean', 'sort my phone photos' -> True. A doing-word plus a file word
    or the name of one of your folders: chat can't do that, an agent can."""
    if not _ACTION.search(text):
        return False
    if _OBJECT.search(text):
        return True
    for word in re.findall(r"[\w][\w .-]{2,40}", text):       # a real folder name in the sentence?
        for w in word.split():
            if len(w) > 2 and any((_known(a) / w).is_dir() for a in _search_roots()):
                return True
    return False


_PHONE_WORDS = r"(?:my\s+|the\s+)?(?:phone|mobile|smartphone|smart\s+phone|android|iphone|samsung|redmi|xiaomi)(?:'s)?"
_PHONE_COPY = re.compile(
    r"^\s*(?:please\s+)?(?:copy|back\s?up|import|transfer|move|save|get|take|pull)\s+(?:all\s+)?(?:the\s+|my\s+)?"
    r"(?P<kind>photos?|pictures?|pics|images?|videos?|media|camera\s+roll|everything|files?)?\s*"
    r"(?:(?:and|&)\s+(?P<kind2>photos?|pictures?|videos?)\s*)?(?:files\s+)?"
    r"(?:from|off|of|on)?\s*" + _PHONE_WORDS +
    r"(?:\s+(?P<folder>camera|dcim|screenshots?))?"
    r"(?:\s+(?:to|into|onto|in)\s+(?P<dst>.+?))?\s*[.!]?\s*$", re.I)
_PHONE_COPY2 = re.compile(      # "copy my phone camera photos to ..."
    r"^\s*(?:please\s+)?(?:copy|back\s?up|import|transfer|move|save|get|take|pull)\s+(?:all\s+)?" + _PHONE_WORDS +
    r"\s+(?P<folder>camera|dcim|screenshots?)?\s*(?P<kind>photos?|pictures?|pics|images?|videos?|media|"
    r"camera\s+roll|files?)(?:\s+(?:and|&)\s+(?P<kind2>photos?|pictures?|videos?))?"
    r"(?:\s+(?:to|into|onto|in)\s+(?P<dst>.+?))?\s*[.!]?\s*$", re.I)
_PHONE_LIST = re.compile(r"^\s*(?:please\s+)?(?:show|list|open|check|what(?:'s| is)\s+(?:on|in)|connect\s+(?:to)?|"
                         r"find|see)\s+" + _PHONE_WORDS + r"(?:\s+(?:folders?|storage|files))?\s*[.!?]?\s*$", re.I)
_SPACE = re.compile(r"\b(?:how\s+much|free|left|check|show|full)\b.*\b(?:space|storage|disk)\b|"
                    r"\b(?:space|storage|disk)\b.*\b(?:left|free|full)\b|\bwhich\s+drives?\s+(?:is|are)\s+full\b",
                    re.I)


_PH = r"(?:my\s+|the\s+)?(?:phone|mobile|smartphone|android|iphone)"
_BT = re.compile(r"\bblue\s?tooth\b", re.I)
_WIFI = re.compile(r"\b(?:wi-?fi|wireless|wlan|hotspot|network|phone\s+link)\b", re.I)
_TO_PHONE = re.compile(r"^\s*(?:please\s+)?(?:send|share|give|put|transfer|copy)\s+(?P<src>.+?)\s+(?:to|with|onto|on)\s+"
                       + _PH + r"(?:\s+(?:via|over|using|by|through|with)\s+(?:the\s+)?(?:wi-?fi|wireless|bluetooth|link))?"
                       r"\s*[.!]?\s*$", re.I)
_FROM_PHONE = re.compile(r"\b(?:receive|get|accept|take|send|transfer|share|copy|move|backup|back\s+up|import)\b.*"
                         r"\b(?:from|off|of)\s+" + _PH + r"\b", re.I)
_LINK_ON = re.compile(r"\b(?:connect|link|pair|start|open|turn\s+on)\b.*\b(?:phone|mobile|android|iphone)\b|"
                      r"\b(?:share|transfer|send)\s+(?:files?\s+)?between\s+" + _PH, re.I)
_LINK_OFF = re.compile(r"\b(?:stop|close|end|turn\s+off|switch\s+off|disconnect)\b.*\b(?:phone\s+link|phone|link)\b", re.I)
_LINK_STATUS = re.compile(r"\bwhat\s+(?:did|has)\s+" + _PH + r"\s+sen[dt]\b|\bphone\s+link\s+status\b", re.I)


def _link_plan(text: str) -> list[dict] | None:
    """Wi-Fi / Bluetooth sharing with the phone."""
    if _LINK_STATUS.search(text):
        return [{"tool": "phone_link_status", "input": {}}]
    if _LINK_OFF.search(text) and (_WIFI.search(text) or "link" in text.lower() or "disconnect" in text.lower()):
        return [{"tool": "phone_link_stop", "input": {}}]
    m = _TO_PHONE.match(text)
    if m and not re.match(r"^(?:the\s+|my\s+|all\s+|some\s+)?(?:files?|photos?|pictures?|videos?|stuff|things)$",
                          m.group("src").strip(), re.I):
        src = _place(m.group("src"), None)
        if _BT.search(text):
            return [{"tool": "bluetooth_send", "input": {"source": src}}]
        return [{"tool": "send_to_phone", "input": {"source": src}}, {"tool": "phone_link_start", "input": {}}]
    if _BT.search(text) and re.search(r"\b(?:phone|mobile|android|iphone)\b", text, re.I):
        if _FROM_PHONE.search(text) or re.search(r"\b(?:receive|accept|get)\b", text, re.I):
            return [{"tool": "bluetooth_receive", "input": {}}]
        return [{"tool": "bluetooth_send", "input": {}}]
    if _WIFI.search(text) and re.search(r"\b(?:phone|mobile|android|iphone)\b", text, re.I) and \
            not _LINK_OFF.search(text):
        return [{"tool": "phone_link_start", "input": {}}]
    if _LINK_ON.search(text) and not re.search(r"\busb\b|\bcable\b", text, re.I):
        return [{"tool": "phone_link_start", "input": {}}]
    if re.search(r"\b(?:receive|accept)\b.*\bfrom\s+" + _PH, text, re.I):
        return [{"tool": "phone_link_start", "input": {}}]
    return None


def _device_plan(text: str) -> list[dict] | None:
    plan = _link_plan(text)
    if plan:
        return plan
    m = _PHONE_COPY.match(text) or _PHONE_COPY2.match(text)
    if m:
        kinds = {(m.group("kind") or "").lower(), (m.group("kind2") or "").lower()} - {""}
        has_v = any(k.startswith("video") for k in kinds)
        has_p = any(k.startswith(("photo", "picture", "pic", "image", "camera")) for k in kinds)
        kind = "media" if (has_v and has_p) or not (has_v or has_p) else ("videos" if has_v else "photos")
        folder = {"camera": "DCIM/Camera", "screenshot": "Pictures/Screenshots",
                  "screenshots": "Pictures/Screenshots"}.get((m.group("folder") or "").lower(), "DCIM")
        dst = m.group("dst")
        dst = _place(dst, None) if dst else "Pictures/Phone backup"
        return [{"tool": "phone_list", "input": {}},
                {"tool": "phone_copy", "input": {"destination": dst, "folder": folder, "kind": kind}}]
    if _PHONE_LIST.match(text):
        return [{"tool": "phone_list", "input": {}}]
    if _SPACE.search(text) and not re.search(r"\b(?:in|of)\s+(?!my\s+(?:pc|laptop|computer|drives?))\w", text, re.I):
        return [{"tool": "drive_space", "input": {}}]
    return None


def quick_plan(task: str) -> list[dict] | None:
    """Exact steps for common file commands, or None when the model should plan."""
    text = " ".join(task.split())
    plan = _device_plan(text)
    if plan:
        return plan
    # "... on my Desktop ..." anywhere in the sentence applies to both folders
    loc = re.search(r"\s+on\s+(?:my\s+|the\s+)?(desktop|documents|downloads|pictures|videos|music)\b",
                    text, re.I)
    if loc:
        text = (text[:loc.start()] + text[loc.end():]).strip()
    plan = _cleanup_plan(text, loc.group(1) if loc else None)
    if plan:
        return plan
    sub = re.search(r"\s*,?\s*\(?\b(?:including|incl\.?|with|and|plus)\s+(?:all\s+|their\s+|the\s+|its\s+)?"
                    r"sub-?\s?folders?\)?|\s+recursively\b|\s+from\s+all\s+sub-?\s?folders?", text, re.I)
    recursive = bool(sub)
    if sub:
        text = (text[:sub.start()] + text[sub.end():]).strip()
    m = _TRANSFER.match(text)
    if m:
        kind = (m.group("kind") or "").lower().lstrip(".")
        if kind in ("all", "every", "the", "my", "files", "file", "everything", "contents", "content",
                    "items", "item", "stuff", "things"):
            kind = ""
        from .cleanup import KIND_PATTERNS
        kinds = {**_KINDS, **KIND_PATTERNS, "image": KIND_PATTERNS["images"], "photo": KIND_PATTERNS["images"],
                 "video": KIND_PATTERNS["videos"], "document": KIND_PATTERNS["documents"],
                 "docs": KIND_PATTERNS["documents"]}
        pattern = m.group("kind") if (m.group("kind") or "").startswith("*.") else kinds.get(kind, "*")
        if kind and pattern == "*" and kind not in kinds:
            return None                               # an unknown word: let the model read it
        where = m.group("where") or (loc.group(1) if loc else None)
        src, dst = _clean_path(m.group("src"), where), _clean_path(m.group("dst"), where)
        parts = [x for x in re.split(r"\s*(?:,|;|\band\b|&)\s*", m.group("src")) if x.strip()]
        if len(parts) > 1:              # "from Downloads and Desktop" - only if each part is a real place
            places = [_place(x, where) for x in parts]
            if all(_is_folder(x) for x in places):
                step = {"source": "; ".join(places), "destination": _place(m.group("dst"), where)
                        if "/" not in dst and dst.split("/")[0].lower() not in _ROOT_WORDS else dst,
                        "pattern": pattern}
                if recursive:
                    step["recursive"] = True
                tool_name = "move_files" if m.group("verb").lower() == "move" else "copy_files"
                return [{"tool": "list_folder", "input": {"path": p}} for p in places] + \
                    [{"tool": tool_name, "input": step}]
        if not where:                  # "copy BIRAC to BIRAC2": find BIRAC, put BIRAC2 next to it
            try:
                found = locate(src)
            except ValueError:
                found = None                     # several matches: the check before asking explains
            if found:
                root = found.rsplit("/", 1)[0] if "/" in found else found   # new folders go next to it
                src = found
                try:
                    dst_found = locate(dst)
                except ValueError:
                    dst_found = None
                dst = dst_found or (dst if dst.split("/")[0].lower() in _ROOT_WORDS else f"{root}/{dst}")
        if not src or not dst or src == dst:
            return None
        tool_name = "move_files" if m.group("verb").lower() == "move" else "copy_files"
        step = {"source": src, "destination": dst, "pattern": pattern}
        if recursive:
            step["recursive"] = True
        return [{"tool": "list_folder", "input": {"path": src}}, {"tool": tool_name, "input": step}]
    m = _MKDIR.match(text)
    if m:
        where = m.group("where") or (loc.group(1) if loc else None)
        return [{"tool": "make_folder", "input": {"path": _clean_path(m.group("dst"), where)}}]
    return None


from . import cleanup  # noqa: E402,F401  (registers the clean-up tools)
from . import devices  # noqa: E402,F401  (drives and phones)
from . import phonelink  # noqa: E402,F401  (Wi-Fi / Bluetooth phone link)
