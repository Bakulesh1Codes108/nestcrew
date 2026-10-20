"""Tools agents can use. Add your own with the @tool decorator.

Safety: file tools are locked to the workspace folder. There is deliberately
NO shell/command tool - a small model running commands on your laptop is risky.
"""
from __future__ import annotations

import ast
import datetime as _dt
import html
import ipaddress
import operator
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .config import load_config, resolve
from .net import is_online


@dataclass
class Tool:
    name: str
    description: str
    args: str
    fn: Callable[..., str]
    needs_internet: bool = False
    opt_in: bool = False          # only given to an agent when explicitly listed


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, args: str, needs_internet: bool = False,
         opt_in: bool = False):
    def wrap(fn):
        REGISTRY[name] = Tool(name, description, args, fn, needs_internet, opt_in)
        return fn
    return wrap


def available(names: list[str] | None = None) -> list[Tool]:
    online = is_online()
    if names:
        picked = [REGISTRY[n] for n in names if n in REGISTRY]
    else:  # default set never includes opt-in tools like screen capture
        picked = [t for t in REGISTRY.values() if not t.opt_in]
    return [t for t in picked if online or not t.needs_internet]


def workspace() -> Path:
    ws = resolve(load_config().get("agent", {}).get("workspace", "./workspace"))
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _safe_path(rel: str) -> Path:
    ws = workspace()
    p = (ws / rel).resolve()
    if ws != p and ws not in p.parents:
        raise ValueError("Path is outside the workspace folder")
    return p


# ------------------------------------------------------------- built-ins ----
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / ** % // are allowed")


@tool("calculator", "Do exact math.", '{"expression": "12*(3+4)"}')
def calculator(expression: str) -> str:
    return str(_eval(ast.parse(expression, mode="eval").body))


@tool("current_time", "Get today's date and time.", "{}")
def current_time() -> str:
    return _dt.datetime.now().strftime("%A %d %B %Y, %H:%M")


@tool("list_files", "List files in the workspace folder.", '{"path": "."}')
def list_files(path: str = ".") -> str:
    p = _safe_path(path)
    items = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
    return "\n".join(items) or "(empty)"


@tool("read_file", "Read a text file from the workspace.", '{"path": "notes.txt"}')
def read_file(path: str) -> str:
    text = _safe_path(path).read_text(encoding="utf-8", errors="replace")
    return text[:8000] + ("\n...[truncated]" if len(text) > 8000 else "")


@tool("write_file", "Save text to a file in the workspace.",
      '{"path": "report.md", "content": "..."}')
def write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    if ".backups" in p.parts:
        raise ValueError("The .backups folder is read-only for agents")
    p.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if p.exists():  # never lose your work: keep the old version
        bdir = workspace() / ".backups"
        bdir.mkdir(exist_ok=True)
        backup = bdir / f"{p.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(p, backup)
        note = f" (previous version backed up to workspace/.backups/{backup.name})"
    p.write_text(content, encoding="utf-8")
    return f"Saved {len(content)} characters to workspace/{path}{note}"


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


@tool("web_search", "Search the web (only when online).", '{"query": "..."}',
      needs_internet=True)
def web_search(query: str) -> str:
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=15,
                   headers={"User-Agent": "Mozilla/5.0 NestCrew"}, follow_redirects=True)
    hits = re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', r.text)[:6]
    lines = []
    for href, title in hits:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        url = q.get("uddg", [href])[0]
        lines.append(f"- {_strip_html(title)} | {url}")
    return "\n".join(lines) or "No results."


@tool("web_fetch", "Read the text of a web page (only when online).", '{"url": "https://..."}',
      needs_internet=True)
def web_fetch(url: str) -> str:
    _check_public_url(url)
    r = httpx.get(url, timeout=20, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 NestCrew"})
    return _strip_html(r.text)[:4000]


def _check_public_url(url: str) -> None:
    """Block file://, localhost and private network addresses."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Only http(s) web addresses are allowed")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"Can't resolve {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Local / private network addresses are blocked")


@tool("run_command", "Run a shell command inside the workspace folder (the user approves each one). "
      "Keep commands simple and short.", '{"command": "dir"}', opt_in=True)
def run_command(command: str) -> str:
    from .safety import blocked_reason
    why = blocked_reason(command)
    if why:
        return f"Error: command {why}"
    timeout = load_config().get("safety", {}).get("command_timeout", 60)
    try:
        r = subprocess.run(command, shell=True, cwd=workspace(), capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return f"Error: command took longer than {timeout}s and was stopped"
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip() or "(no output)"
    return f"exit code {r.returncode}\n" + (out[:4000] + "\n...[truncated]" if len(out) > 4000 else out)


# The agent sets this so vision tools use the same model the agent runs on.
CONTEXT: dict = {"provider": None}


@tool("look_at_screen", "Take a screenshot of the user's screen and answer a question about it. "
      "Deleted right after unless keep is true.",
      '{"question": "What error is shown?", "keep": false}', opt_in=True)
def look_at_screen(question: str = "Describe what is on the screen.", keep: bool | None = None) -> str:
    from .media import look_at_screen as _look
    from .router import get_provider, is_local
    prov = CONTEXT.get("provider")
    if prov is None or is_local(prov):   # local brain -> use the small "screen" eyes model
        prov = get_provider("local", task="screen")
    res = _look(prov, question, keep)
    note = f"\n(screenshot kept at {res['saved_to']})" if res["saved_to"] else "\n(screenshot deleted)"
    return res["answer"] + note


def run_tool(name: str, args: dict) -> str:
    t = REGISTRY.get(name)
    if not t:
        return f"Error: unknown tool '{name}'. Available: {', '.join(REGISTRY)}"
    if t.needs_internet and not is_online():
        return "Error: this tool needs internet and you're offline."
    try:
        return t.fn(**(args or {}))
    except TypeError as e:
        return f"Error: wrong arguments for {name}. Expected {t.args}. ({e})"
    except Exception as e:  # report to the model so it can recover
        return f"Error: {e}"


# File/folder, Excel and specialist-agent tools live in rpa.py (registered on import).
from . import rpa  # noqa: E402,F401
