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
MAX_FILES = 500
CHANGE_TOOLS = {"make_folder", "copy_files", "move_files", "rename_file", "write_excel",
                "append_excel_rows", "undo_last_move"}
READ_TOOLS = {"find_folder", "list_folder", "find_files", "file_info", "read_text", "read_excel"}


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


def allowed_roots() -> list[Path]:
    cfg = load_config().get("rpa", {}) or {}
    roots = [workspace()]
    for f in cfg.get("folders") or ALIASES:
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
    it = source.rglob("*") if recursive else source.iterdir()
    out = [f for f in it if f.is_file() and not f.name.startswith(("~$", "."))
           and any(fnmatch.fnmatch(f.name.lower(), p.lower()) for p in pats)]
    return sorted(out)[:MAX_FILES]


def _free_name(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    if not target.exists():
        return target
    stem, suf = Path(name).stem, Path(name).suffix
    i = 2
    while (dest_dir / f"{stem} ({i}){suf}").exists():
        i += 1
    return dest_dir / f"{stem} ({i}){suf}"


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


def _transfer(source: str, destination: str, pattern: str, move: bool) -> str:
    src, dst = resolve_path(source), resolve_path(destination)
    files = _matches(src, pattern)
    if not files:
        return f"No files matching '{pattern or '*'}' in {show(src)}"
    dst.mkdir(parents=True, exist_ok=True)
    done, log, same = [], [], []
    for f in files:
        if not move and _same_file(f, dst / f.name):      # already there, identical: don't make a (2)
            same.append(f.name)
            continue
        target = _free_name(dst, f.name)
        (shutil.move if move else shutil.copy2)(str(f), str(target))
        done.append(f"{f.name} -> {show(target)}")
        log.append([str(f), str(target)])
    if move:
        _save_undo(log)
    verb = "Moved" if move else "Copied"
    out = f"{verb} {len(done)} file(s) from {show(src)} to {show(dst)}"
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


@tool("copy_files", "Copy files from a folder (or one file) into another folder. pattern like *.pdf (asks you first).",
      '{"source": "Downloads", "destination": "Documents/Invoices", "pattern": "*.pdf"}')
def copy_files(source: str, destination: str, pattern: str = "*") -> str:
    return _transfer(source, destination, pattern, move=False)


@tool("move_files", "Move files from a folder (or one file) into another folder. pattern like *.jpg (asks you first; can be undone).",
      '{"source": "Desktop/BIRAC", "destination": "Desktop/BIRAC2", "pattern": "*"}')
def move_files(source: str, destination: str, pattern: str = "*") -> str:
    return _transfer(source, destination, pattern, move=True)


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
        if tool_name in ("copy_files", "move_files"):
            src, dst = resolve_path(args.get("source", "")), resolve_path(args.get("destination", ""))
            files = _matches(src, args.get("pattern") or "*")
            verb = "MOVE" if tool_name == "move_files" else "COPY"
            lines = "\n".join(f"  • {f.name}" for f in files[:15]) + (f"\n  … and {len(files) - 15} more" if len(files) > 15 else "")
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
            return f"{verb} {len(files)} file(s)\nfrom {show(src)}\nto   {show(dst)}{extra}\n\n{lines}\n\nNothing is overwritten - name clashes get a (2).{undo}"
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
    "excel_analyst": {"role": "Excel analyst", "goal": "Read spreadsheets, calculate totals, and write or append rows.",
                      "tools": ["list_folder", "find_files", "read_excel", "write_excel", "append_excel_rows",
                                "calculator"]},
    "researcher": {"role": "Researcher", "goal": "Find accurate facts (web when online, files otherwise).",
                   "tools": ["web_search", "web_fetch", "read_text", "find_files", "current_time"]},
    "writer": {"role": "Writer", "goal": "Write clear notes, reports and summaries and save them.",
               "tools": ["read_text", "read_file", "write_file", "list_files"]},
}

AGENT_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar("nestcrew_agent_ctx", default=None)


@tool("delegate", "Hand a sub-task to a specialist: file_clerk, excel_analyst, researcher or writer. "
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
    first = p.split("/")[0].lower()
    if first in _ROOT_WORDS:
        return _ROOT_WORDS[first] + p[len(first):]
    if ":" in p[:3] or p.startswith("/") or p.startswith("~"):
        return p
    return f"{_ROOT_WORDS[where.lower()]}/{p}" if where else p


def quick_plan(task: str) -> list[dict] | None:
    """Exact steps for common file commands, or None when the model should plan."""
    text = " ".join(task.split())
    # "... on my Desktop ..." anywhere in the sentence applies to both folders
    loc = re.search(r"\s+on\s+(?:my\s+|the\s+)?(desktop|documents|downloads|pictures|videos|music)\b",
                    text, re.I)
    if loc:
        text = (text[:loc.start()] + text[loc.end():]).strip()
    m = _TRANSFER.match(text)
    if m:
        kind = (m.group("kind") or "").lower().lstrip(".")
        if kind in ("all", "every", "the", "my", "files", "file", "everything", "contents", "content",
                    "items", "item", "stuff", "things"):
            kind = ""
        pattern = m.group("kind") if (m.group("kind") or "").startswith("*.") else _KINDS.get(kind, "*")
        if kind and pattern == "*" and kind not in _KINDS:
            return None                               # an unknown word: let the model read it
        where = m.group("where") or (loc.group(1) if loc else None)
        src, dst = _clean_path(m.group("src"), where), _clean_path(m.group("dst"), where)
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
        return [{"tool": "list_folder", "input": {"path": src}},
                {"tool": tool_name, "input": {"source": src, "destination": dst, "pattern": pattern}}]
    m = _MKDIR.match(text)
    if m:
        where = m.group("where") or (loc.group(1) if loc else None)
        return [{"tool": "make_folder", "input": {"path": _clean_path(m.group("dst"), where)}}]
    return None
