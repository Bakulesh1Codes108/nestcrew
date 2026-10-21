"""Update NestCrew from its GitHub repository - keeps your settings, chats and workflows.

    python -m nestcrew.updater          (or double-click "Update NestCrew.bat", or Guide → Update)

Downloads the latest code of the configured repository (update.repo in config.yaml, by default
the official one) over HTTPS from github.com only, then copies the program files over this
install. Never touched: config.yaml, .env, data/ (chats, Brain, audit), workspace/, your saved
workflows (new templates are added, yours are kept), .venv. Afterwards press "Restart now".
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

DEFAULT_REPO = "Bakulesh1Codes108/nestcrew"
ROOT = Path(__file__).resolve().parent.parent
KEEP = {"config.yaml", ".env", "data", "workspace", ".venv", "venv", ".git"}
ADD_ONLY = {"workflows", "crews"}             # add new templates, never replace yours


def _repo() -> str:
    try:
        from .config import load_config
        repo = (load_config().get("update") or {}).get("repo") or DEFAULT_REPO
    except Exception:
        repo = DEFAULT_REPO
    import re
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}", repo):
        raise ValueError(f"update.repo must look like owner/name, not {repo!r}")
    return repo


def download(repo: str | None = None, branch: str = "main") -> bytes:
    import httpx
    repo = repo or _repo()
    url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
    with httpx.Client(follow_redirects=True, timeout=120) as c:
        r = c.get(url)
        host = r.url.host or ""
        if not (host == "github.com" or host.endswith(".github.com") or host == "codeload.github.com"):
            raise ValueError(f"Refusing update from unexpected host {host}")
        r.raise_for_status()
        return r.content


def apply(zip_bytes: bytes, root: Path = ROOT) -> dict:
    """Copy program files from the downloaded zip over the install. Returns what changed."""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names or not any(n.split("/", 1)[-1] == "nestcrew/server.py" for n in names):
        raise ValueError("That download doesn't look like NestCrew.")
    changed, added, kept = [], [], []
    for n in names:
        rel = n.split("/", 1)[1] if "/" in n else ""
        if not rel or ".." in Path(rel).parts or Path(rel).is_absolute():
            continue
        top = rel.split("/", 1)[0]
        target = root / rel
        if top in KEEP:
            kept.append(rel)
            continue
        if top in ADD_ONLY and target.exists():
            kept.append(rel)
            continue
        data = zf.read(n)
        if target.exists() and target.read_bytes() == data:
            continue
        (changed if target.exists() else added).append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        tmp.write_bytes(data)
        tmp.replace(target)
    return {"changed": changed, "added": added, "kept": len(kept)}


def _deps(root: Path = ROOT) -> str:
    """Install any new Python packages the update needs."""
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(root)], capture_output=True,
                       text=True, creationflags=0x08000000 if sys.platform == "win32" else 0)
    return "ok" if r.returncode == 0 else (r.stderr.strip()[-300:] or "pip failed")


def update(install_deps: bool = True) -> dict:
    repo = _repo()
    res = apply(download(repo))
    res["repo"] = repo
    res["deps"] = _deps() if install_deps and (res["changed"] or res["added"]) else "unchanged"
    try:
        from .safety import audit
        audit(event="update", repo=repo, changed=len(res["changed"]), added=len(res["added"]))
    except Exception:
        pass
    return res


def main() -> None:
    print(f"Updating NestCrew from github.com/{_repo()} …")
    try:
        r = update()
    except Exception as e:
        print(f"Update failed: {e}")
        sys.exit(1)
    n = len(r["changed"]) + len(r["added"])
    print(json.dumps({k: v for k, v in r.items() if k not in ("changed", "added")}))
    for f in r["changed"] + r["added"]:
        print("  updated", f)
    print("Already up to date." if not n else f"Updated {n} file(s). In NestCrew press 'Restart now' (or reopen it).")


if __name__ == "__main__":
    main()
