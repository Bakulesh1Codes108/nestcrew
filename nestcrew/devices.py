"""Drives and phones.

drive_space: free/used space on this PC's drives (read-only).

Phones: a phone plugged in by USB in "File transfer" mode shows up in This PC but has no
drive letter (Windows talks to it over MTP), so normal file code can't see it. These tools
use the Windows Shell - the same thing File Explorer uses - through PowerShell:

  phone_list  - which phone is connected and its storage / DCIM folders (read-only)
  phone_copy  - copy photos / videos from the phone to a PC folder, in batches, skipping
                ones that are already there. It never deletes anything on the phone: check
                the copies, then free up space on the phone yourself.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .rpa import _size, resolve_path, show
from .tools import tool

PHONE_KINDS = {
    "photos": [".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".dng", ".gif"],
    "videos": [".mp4", ".mov", ".3gp", ".mkv", ".webm"],
}
PHONE_KINDS["media"] = PHONE_KINDS["photos"] + PHONE_KINDS["videos"]
PHONE_KINDS["images"] = PHONE_KINDS["photos"]
PHONE_BATCH = 50                       # files per PowerShell call: progress + Stop between batches
_CACHE: dict = {}                      # phone listing reused between the approval card and the copy


# ------------------------------------------------------------------- drives --
@tool("drive_space", "How much space is used and free on this PC's drives (read-only).", "{}")
def drive_space() -> str:
    from .rpa import drive_roots
    seen, rows = set(), []
    for root in [Path(Path.home().anchor or "/"), *drive_roots()]:
        key = str(root).upper()
        if key in seen:
            continue
        seen.add(key)
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        pct = 100 * u.used / u.total if u.total else 0
        warn = "  <- almost full" if pct > 90 else ""
        rows.append(f"  {str(root):<6} {_size(u.free)} free of {_size(u.total)} ({pct:.0f}% used){warn}")
    return "Drives:\n" + "\n".join(rows) if rows else "Couldn't read any drive."


# ------------------------------------------------------------------- phones --
def _ps(script: str, timeout: int = 600) -> str:
    """Run a PowerShell script (Windows only) and return its output."""
    if os.name != "nt":
        raise ValueError("Phone access works on Windows: plug the phone in by USB and choose "
                         "'File transfer' on the phone.")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", enc], capture_output=True, text=True, timeout=timeout,
                       creationflags=0x08000000)
    if r.returncode != 0 and not r.stdout.strip():
        raise ValueError(f"Windows couldn't read the phone: {r.stderr.strip()[:300]}")
    return r.stdout


def _q(text: str) -> str:
    """Quote for a PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


_PS_HELPERS = r"""
$ErrorActionPreference = 'Stop'
$sh = New-Object -ComObject Shell.Application
function Get-Phones { $sh.NameSpace(17).Items() | Where-Object { $_.IsFolder -and -not $_.IsFileSystem } }
function Get-Phone($name) {
  $all = @(Get-Phones)
  if ($name) { $all = @($all | Where-Object { $_.Name -like "*$name*" }) }
  if ($all.Count -eq 0) { throw 'NO_PHONE' }
  return $all[0]
}
function Find-Folder($root, $path) {
  $f = $root.GetFolder
  foreach ($part in ($path -split '/' | Where-Object { $_ })) {
    $next = $f.Items() | Where-Object { $_.IsFolder -and $_.Name -eq $part } | Select-Object -First 1
    if (-not $next) {   # 'DCIM' may sit one level down, under 'Internal shared storage'
      foreach ($s in @($f.Items() | Where-Object { $_.IsFolder })) {
        $next = $s.GetFolder.Items() | Where-Object { $_.IsFolder -and $_.Name -eq $part } | Select-Object -First 1
        if ($next) { break }
      }
    }
    if (-not $next) { throw "NO_FOLDER:$part" }
    $f = $next.GetFolder
  }
  return $f
}
"""


def _phone_error(e: Exception) -> ValueError:
    msg = str(e)
    if "NO_PHONE" in msg:
        return ValueError("No phone found. Plug it in by USB, unlock it, and choose 'File transfer' "
                          "(or 'Transfer files') in the USB notification on the phone.")
    if "NO_FOLDER:" in msg:
        return ValueError(f"The phone has no folder '{msg.split('NO_FOLDER:')[1].split()[0]}'. "
                          "Use phone_list to see its folders.")
    return ValueError(msg[:400])


@tool("phone_list", "Show the phone connected by USB (File transfer mode): its storage and photo folders "
      "(read-only).", "{}")
def phone_list() -> str:
    script = _PS_HELPERS + r"""
$out = @()
foreach ($p in @(Get-Phones)) {
  $stores = @()
  foreach ($s in @($p.GetFolder.Items() | Where-Object { $_.IsFolder })) {
    $subs = @($s.GetFolder.Items() | Where-Object { $_.IsFolder } | ForEach-Object { $_.Name })
    $stores += @{ name = $s.Name; folders = $subs }
  }
  $out += @{ name = $p.Name; storage = $stores }
}
ConvertTo-Json -Compress -Depth 5 @($out)
"""
    try:
        data = json.loads(_ps(script, 120) or "[]")
    except ValueError as e:
        raise _phone_error(e) from e
    if isinstance(data, dict):
        data = [data]
    if not data:
        raise _phone_error(ValueError("NO_PHONE"))
    lines = []
    for p in data:
        lines.append(f"Phone: {p['name']}")
        for s in p.get("storage") or []:
            folders = s.get("folders") or []
            if isinstance(folders, str):
                folders = [folders]
            lines.append(f"  {s['name']}: " + ", ".join(folders[:25]))
    lines.append("Photos are usually in DCIM/Camera; screenshots in Pictures/Screenshots or DCIM/Screenshots.")
    return "\n".join(lines)


def _phone_files(device: str, folder: str, kind: str) -> list[dict]:
    """Every matching file under the phone folder (subfolders too): rel path, name, size."""
    exts = PHONE_KINDS.get(kind.lower(), PHONE_KINDS["media"])
    key = (device, folder, kind)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    script = _PS_HELPERS + f"""
$exts = @({",".join(_q(e) for e in exts)})
$root = Find-Folder (Get-Phone {_q(device)}) {_q(folder)}
$out = New-Object System.Collections.ArrayList
function Walk($f, $rel, $depth) {{
  foreach ($i in @($f.Items())) {{
    if ($i.IsFolder) {{ if ($depth -lt 4 -and -not $i.Name.StartsWith('.')) {{ Walk $i.GetFolder ($rel + $i.Name + '/') ($depth + 1) }} }}
    elseif ($exts -contains ([IO.Path]::GetExtension($i.Name).ToLower())) {{
      [void]$out.Add(@{{ rel = $rel; name = $i.Name; size = [int64]$i.ExtendedProperty('System.Size') }})
    }}
  }}
}}
Walk $root '' 0
ConvertTo-Json -Compress -Depth 3 @($out)
"""
    try:
        data = json.loads(_ps(script, 900) or "[]")
    except ValueError as e:
        raise _phone_error(e) from e
    files = [data] if isinstance(data, dict) else list(data or [])
    _CACHE[key] = (time.time(), files)
    return files


def _already_there(dst: Path, f: dict) -> bool:
    p = dst / f["name"]
    try:
        return p.is_file() and (not f.get("size") or p.stat().st_size == int(f["size"]))
    except OSError:
        return False


def _copy_batch(device: str, folder: str, dst: Path, batch: list[dict]) -> None:
    items = ";".join(f"{f['rel']}|{f['name']}" for f in batch)
    script = _PS_HELPERS + f"""
$root = Find-Folder (Get-Phone {_q(device)}) {_q(folder)}
$dest = $sh.NameSpace({_q(str(dst))})
foreach ($entry in ({_q(items)} -split ';')) {{
  $rel, $name = $entry -split '\\|', 2
  $f = $root
  foreach ($part in ($rel -split '/' | Where-Object {{ $_ }})) {{
    $f = ($f.Items() | Where-Object {{ $_.IsFolder -and $_.Name -eq $part }} | Select-Object -First 1).GetFolder
  }}
  $item = $f.Items() | Where-Object {{ $_.Name -eq $name }} | Select-Object -First 1
  if ($item) {{
    $dest.CopyHere($item, 1564)   # no dialogs, rename on clash, yes to all
    $target = Join-Path {_q(str(dst))} $name
    $t0 = Get-Date
    while (-not (Test-Path -LiteralPath $target) -and ((Get-Date) - $t0).TotalSeconds -lt 120) {{ Start-Sleep -Milliseconds 200 }}
  }}
}}
'ok'
"""
    _ps(script, 60 + 120 * len(batch))


@tool("phone_copy", "Copy photos/videos from the phone (USB, File transfer mode) to a PC folder, in batches; "
      "skips files already copied; never deletes anything on the phone. kind = photos, videos or media. "
      "Asks you first.",
      '{"folder": "DCIM", "destination": "Pictures/Phone backup", "kind": "media"}')
def phone_copy(destination: str = "Pictures/Phone backup", folder: str = "DCIM", kind: str = "media",
               device: str = "") -> str:
    from .cleanup import _progress
    dst = resolve_path(destination)
    files = _phone_files(device, folder, kind)
    todo = [f for f in files if not _already_there(dst, f)]
    if not files:
        return f"No {kind} found in the phone's {folder}."
    if not todo:
        return f"All {len(files)} {kind} file(s) from the phone's {folder} are already in {show(dst)}."
    dst.mkdir(parents=True, exist_ok=True)
    t0, copied = time.time(), 0
    for i in range(0, len(todo), PHONE_BATCH):
        batch = todo[i:i + PHONE_BATCH]
        _progress(f"Phone batch {i // PHONE_BATCH + 1}/{(len(todo) - 1) // PHONE_BATCH + 1}: "
                  f"{copied}/{len(todo)} copied")                         # Stop is checked here
        _copy_batch(device, folder, dst, batch)
        copied += sum(1 for f in batch if (dst / f["name"]).exists())
    _CACHE.clear()
    total = sum(int(f.get("size") or 0) for f in todo)
    skipped = len(files) - len(todo)
    return (f"Copied {copied} of {len(todo)} file(s) ({_size(total)}) from the phone's {folder} to {show(dst)} "
            f"in {time.time() - t0:.0f}s" + (f"; {skipped} were already there" if skipped else "") +
            ". Nothing was deleted on the phone - check the copies, then free up space on the phone.")


def preview(tool_name: str, args: dict) -> str | None:
    if tool_name != "phone_copy":
        return None
    dst = resolve_path(args.get("destination") or "Pictures/Phone backup")
    folder, kind = args.get("folder") or "DCIM", args.get("kind") or "media"
    files = _phone_files(args.get("device", ""), folder, kind)
    todo = [f for f in files if not _already_there(dst, f)]
    size = sum(int(f.get("size") or 0) for f in todo)
    sample = "\n".join(f"  • {f['rel']}{f['name']}" for f in todo[:12]) + \
        (f"\n  … and {len(todo) - 12} more" if len(todo) > 12 else "")
    batches = (len(todo) - 1) // PHONE_BATCH + 1 if todo else 0
    return (f"COPY {len(todo)} {kind} file(s) from the phone ({folder}) - {_size(size)}, in {batches} batch(es)\n"
            f"to   {show(dst)}\n({len(files) - len(todo)} already copied - skipped)\n\n{sample}\n\n"
            "Nothing on the phone is deleted or changed.")
