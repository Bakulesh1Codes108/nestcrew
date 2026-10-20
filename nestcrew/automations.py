"""Automations: run jobs on a schedule or when new files land in an inbox folder.

Unattended runs can't ask you, so a tool marked `ask` is denied unless the
automation lists it under `allow:`. Blocklist + workspace sandbox still apply.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import threading
import time
from pathlib import Path
from typing import Callable

import yaml

from .config import ROOT, resolve
from .safety import Gate, audit

FILE = ROOT / "automations.yaml"
_lock = threading.Lock()        # one automation at a time -> protects RAM


def _state_path() -> Path:
    return resolve("./data/automation_state.json")


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")


def load() -> list[dict]:
    """automations.yaml entries + Canvas workflows that have a watch/schedule Start."""
    items = []
    if FILE.exists():
        items = (yaml.safe_load(FILE.read_text(encoding="utf-8")) or {}).get("automations") or []
    from . import workflows
    for w in workflows.list_all():
        try:
            wf = workflows.get(w["id"])
        except (KeyError, ValueError):
            continue
        if workflows.trigger_of(wf):           # only watch / daily / every workflows run by themselves
            items.append(workflows.as_automation(wf))
    return items


def get(name: str) -> dict:
    for a in load():
        if a["name"] == name:
            return a
    raise KeyError(f"No automation called '{name}'")


def is_enabled(a: dict, state: dict | None = None) -> bool:
    st = (state or _load_state()).get(a["name"], {})
    return st.get("enabled", a.get("enabled", False))


def set_enabled(name: str, on: bool) -> None:
    get(name)
    if name.startswith("wf:"):            # workflows keep their switch in their own file
        from . import workflows
        workflows.set_enabled(name[3:], on)
        return
    seed_state(get(name), on)


def seed_state(a: dict, on: bool) -> None:
    """Record the on/off switch; when switching on, existing files count as seen
    so only files added from now on are processed."""
    s = _load_state()
    entry = s.setdefault(a["name"], {})
    entry["enabled"] = on
    if on and "seen" not in entry and a.get("trigger", {}).get("watch"):
        entry["seen"] = [str(p) for p in _watch_files(a)]
    _save_state(s)


def status() -> list[dict]:
    s = _load_state()
    out = []
    for a in load():
        st = s.get(a["name"], {})
        trig = a.get("trigger", {})
        when = (f"new files in workspace/{trig['watch']}" if trig.get("watch")
                else f"daily at {trig['daily']}" if trig.get("daily")
                else f"every {trig['every_minutes']} min" if trig.get("every_minutes") else "manual")
        out.append({"name": a["name"], "enabled": is_enabled(a, s), "when": when,
                    "action": a.get("action", {}).get("type"), "allow": a.get("allow", []),
                    "last_run": st.get("last_run"), "last_result": st.get("last_result", "")})
    return out


# ------------------------------------------------------------- triggers ----
def _watch_dir(a: dict) -> Path:
    from .tools import _safe_path
    d = _safe_path(a["trigger"]["watch"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _watch_files(a: dict) -> list[Path]:
    pats = [p.strip() for p in str(a["trigger"].get("pattern", "*")).split(",")]
    files = [f for f in sorted(_watch_dir(a).iterdir())
             if f.is_file() and any(fnmatch.fnmatch(f.name.lower(), p.lower()) for p in pats)]
    # skip files still being copied (changed in the last 3 seconds)
    return [f for f in files if time.time() - f.stat().st_mtime > 3]


def due_jobs(a: dict, now: dt.datetime | None = None) -> list[Path | None]:
    """Files to process (watch) or [None] for a scheduled run; [] if nothing due."""
    now = now or dt.datetime.now()
    st = _load_state().get(a["name"], {})
    trig = a.get("trigger", {})
    if trig.get("watch"):
        seen = set(st.get("seen", []))
        return [f for f in _watch_files(a) if str(f) not in seen][:5]
    last = dt.datetime.fromisoformat(st["last_run"]) if st.get("last_run") else None
    if trig.get("daily"):
        hh, mm = map(int, str(trig["daily"]).split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return [None] if now >= target and (not last or last < target) else []
    if trig.get("every_minutes"):
        return [None] if not last or (now - last).total_seconds() >= 60 * trig["every_minutes"] else []
    return []


# --------------------------------------------------------------- actions ----
def _image_b64(path: Path) -> str:
    from .media import _jpeg_b64
    from PIL import Image
    with Image.open(path) as img:
        return _jpeg_b64(img)


def _append(rel: str, text: str) -> str:
    from .tools import _safe_path
    p = _safe_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(text)
    return f"workspace/{rel}"


def run_once(a: dict, file: Path | None = None, gate: Gate | None = None,
             on_event: Callable[[dict], None] | None = None) -> str:
    """Run one job. With no gate, only tools in `allow:` may run."""
    from .agent import Agent
    from .crew import load_crew, run_crew
    from .router import get_provider

    allow = set(a.get("allow") or [])
    gate = gate or Gate(ask_fn=lambda req: "task" if req["tool"] in allow else "deny",
                        run_id=f"auto:{a['name']}")
    if a.get("action", {}).get("type") == "workflow":
        from . import workflows
        res = workflows.run(workflows.get(a["action"]["id"]), gate=gate, on_event=on_event, file=file)
        _append("notes/automation_log.md",
                f"- {time.strftime('%Y-%m-%d %H:%M')} **{a['name']}**: {str(res)[:300]}\n")
        return str(res)
    act = a.get("action", {})
    kind = act.get("type")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    rel = None
    if file is not None:
        from .tools import workspace
        rel = str(file.relative_to(workspace())).replace("\\", "/")

    if kind == "describe_image":
        if file is None:
            return "Nothing to describe (this action needs a watch folder)."
        from .media import describe_image
        prov = get_provider(act.get("provider"), task=act.get("task", "image"))
        answer = describe_image(prov, _image_b64(file), act.get("prompt", "Describe this image."))
        where = _append(act.get("save_to", "notes/automation_notes.md"),
                        f"\n## {file.name}  ({stamp}, {prov.label()})\n{answer.strip()}\n")
        result = f"{file.name} -> {where}"
    elif kind == "video":
        if file is None:
            return "Nothing to analyse (this action needs a watch folder)."
        from .media import analyze_video, save_outputs
        from .tools import workspace
        prov = get_provider(act.get("provider"), task="video")
        res = analyze_video(prov, file, act.get("question", "What happens in this video?"), on_event)
        files = save_outputs(res, file.name, workspace() / "notes")
        _append("notes/videos.md", f"\n## {file.name}  ({stamp})\n{res['summary']}\n")
        result = f"{file.name} -> {', '.join(Path(f).name for f in files.values())}"
    elif kind == "agent":
        prov = get_provider(act.get("provider"), task="agent")
        prompt = act.get("prompt", "").replace("{file}", rel or "")
        result = Agent(role=a["name"], tools=act.get("tools")).run(
            prov, prompt, on_event=on_event, gate=gate)
    elif kind == "crew":
        inputs = {k: str(v).replace("{file}", rel or "") for k, v in (act.get("inputs") or {}).items()}
        result = run_crew(load_crew(act["crew"]), inputs, get_provider(act.get("provider"), task="agent"),
                          on_event=on_event, gate=gate)["final"]
    else:
        return f"Unknown action type '{kind}'"
    _append("notes/automation_log.md", f"- {stamp} **{a['name']}**: {str(result)[:300]}\n")
    return str(result)


def run_due(a: dict, gate: Gate | None = None, on_event=None, force: bool = False) -> list[str]:
    """Process everything due now (force=True runs a scheduled automation immediately)."""
    jobs = due_jobs(a) or ([None] if force and not a.get("trigger", {}).get("watch") else [])
    results = []
    with _lock:
        for job in jobs:
            try:
                r = run_once(a, job, gate=gate, on_event=on_event)
            except Exception as e:     # one bad file shouldn't stop the rest
                r = f"Error: {e}"
            results.append(r)
            s = _load_state()
            st = s.setdefault(a["name"], {})
            if job is not None:
                st.setdefault("seen", []).append(str(job))
            st["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
            st["last_result"] = r[:300]
            _save_state(s)
            audit(run=f"auto:{a['name']}", event="automation", file=str(job) if job else None,
                  result=r[:300])
    return results


class Runner(threading.Thread):
    """Background loop: checks enabled automations every `interval` seconds."""

    def __init__(self, interval: int = 30):
        super().__init__(daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(self.interval):
            self.tick()

    def tick(self) -> list[str]:
        out = []
        state = _load_state()
        for a in load():
            if is_enabled(a, state):
                try:
                    out += run_due(a)
                except Exception as e:
                    audit(run=f"auto:{a['name']}", event="automation_error", error=str(e))
        return out
