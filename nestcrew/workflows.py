"""Canvas workflows: visual node graphs you build (or capture from a finished
task) and run by hand or on a trigger.

Node types (config keys):
  trigger  mode: manual | watch | daily | every ; watch, pattern, daily, every_minutes
  screen   question, keep          - screenshot + small "screen" model (asks you first)
  vision   task (image|documents|screen), prompt   - looks at the trigger's file
  video    question                - frame-by-frame analysis of the trigger's file
  decide   options: {label: [examples]}  - System 1 picks a label; edges with
           `when: label` only fire on that label (branching, no LLM needed)
  condition contains, negate         - plain text check; arrows fire on "yes"
           (or `when: no` for the other branch)
  llm      job, prompt              - one model call, no tools (check, rewrite, summarise)
  tool     tool, args               - run one tool directly (goes through the same permission gate)
  output   -                        - marks the final answer of the workflow
  agent    prompt, tools, allow     - an agent with tools; {input} = previous output
  crew     crew, inputs
  note     save_to, template        - append text to a note in workspace/

Text flows along edges: each node gets its parents' outputs as {input}.
Placeholders anywhere: {input} {file} {date}.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from .config import resolve
from .safety import Gate

NODE_TYPES = {
    "trigger": {"label": "Trigger", "defaults": {"mode": "manual"}},
    "screen": {"label": "Look at screen", "defaults": {"question": "What is on the screen?", "keep": False}},
    "vision": {"label": "Look at image", "defaults": {"task": "image", "prompt": "Describe this image."}},
    "video": {"label": "Analyse video", "defaults": {"question": "What happens in this video?"}},
    "decide": {"label": "Decide (System 1)", "defaults": {"options": {
        "yes": ["this needs action", "there is an error or problem"],
        "no": ["nothing important", "everything looks fine"]}}},
    "condition": {"label": "If text contains", "defaults": {"contains": "PROBLEM", "negate": False}},
    "llm": {"label": "Ask a model", "defaults": {"job": "chat", "prompt": "Summarise:\n{input}"}},
    "tool": {"label": "Use one tool", "defaults": {"tool": "read_file", "args": {"path": "notes/automation_log.md"}}},
    "output": {"label": "Final answer", "defaults": {}},
    "agent": {"label": "Agent", "defaults": {"prompt": "{input}", "tools": ["read_file", "write_file"],
                                             "allow": []}},
    "crew": {"label": "Crew", "defaults": {"crew": "research_writer", "inputs": {"topic": "{input}"}}},
    "note": {"label": "Save to note", "defaults": {"save_to": "notes/workflow.md", "mode": "append",
                                                   "template": "## {date}\n{input}\n"}},
}


ALIASES = {"save": "note"}
TEMPLATES = Path(__file__).resolve().parent.parent / "workflows"


def normalize(wf: dict) -> dict:
    """Accept older/alternate field names (e.g. starter templates)."""
    for n in wf.get("nodes", []):
        n["type"] = ALIASES.get(n["type"], n["type"])
        c = n.setdefault("config", {})
        if "folder" in c and "watch" not in c:
            c["watch"] = c.pop("folder")
        if "job" in c and n["type"] == "vision":
            c.setdefault("task", c.pop("job"))
        if n["type"] == "note" and "path" in c:
            c.setdefault("save_to", c.pop("path"))
        if n["type"] == "trigger" and "time" in c:
            c.setdefault("daily", c.pop("time"))
        if n["type"] == "tool" and isinstance(c.get("args"), str):
            try:
                c["args"] = json.loads(c["args"] or "{}")
            except ValueError:
                c["args"] = {}
        if isinstance(c.get("negate"), str):
            c["negate"] = c["negate"].lower() == "true"
    if wf.get("allow"):
        for n in wf["nodes"]:
            if n["type"] == "agent":
                n["config"]["allow"] = sorted(set(n["config"].get("allow", [])) | set(wf["allow"]))
    return wf


def templates() -> list[dict]:
    out = []
    for p in sorted(TEMPLATES.glob("*.json")) if TEMPLATES.exists() else []:
        try:
            wf = normalize(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
        out.append({"id": p.stem, "name": wf.get("name", p.stem), "description": wf.get("description", ""),
                    "nodes": len(wf.get("nodes", []))})
    return out


def from_template(tid: str) -> dict:
    p = TEMPLATES / f"{_slug(tid)}.json"
    wf = normalize(json.loads(p.read_text(encoding="utf-8")))
    wf.pop("id", None)
    wf["enabled"] = False
    wf["source"] = f"template {tid}"
    return save(wf)


def _dir() -> Path:
    d = resolve("./data/workflows")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workflow"


def list_all() -> list[dict]:
    out = []
    for p in sorted(_dir().glob("*.json"), key=lambda p: -p.stat().st_mtime):
        try:
            wf = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        trig = next((n for n in wf.get("nodes", []) if n["type"] == "trigger"), {})
        out.append({"id": wf["id"], "name": wf["name"], "enabled": wf.get("enabled", False),
                    "nodes": len(wf.get("nodes", [])), "trigger": trig.get("config", {}),
                    "updated": p.stat().st_mtime, "source": wf.get("source", "")})
    return out


def get(wf_id: str) -> dict:
    p = _dir() / f"{_slug(wf_id)}.json"
    if not p.exists():
        raise KeyError(f"No workflow '{wf_id}'")
    return normalize(json.loads(p.read_text(encoding="utf-8")))


def save(wf: dict, note: str = "") -> dict:
    """Save; the previous version is kept in <id>.versions/ so you can compare or restore it."""
    wf.setdefault("id", f"{_slug(wf.get('name', 'workflow'))}-{uuid.uuid4().hex[:4]}")
    wf["id"] = _slug(wf["id"])
    normalize(wf)
    validate(wf)
    path = _dir() / f"{wf['id']}.json"
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if old and _content(old) != _content(wf):
            vdir = _dir() / f"{wf['id']}.versions"
            vdir.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            n = 1
            while (vdir / f"{stamp}-{n}.json").exists():
                n += 1
            (vdir / f"{stamp}-{n}.json").write_text(json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8")
            for extra in sorted(vdir.glob("*.json"))[:-40]:       # keep the last 40 versions
                extra.rename(extra.with_suffix(".old"))
    wf["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if note:
        wf["save_note"] = note
    path.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    return wf


def _content(wf: dict) -> str:
    """What counts as a real change (ignores save time, on/off switch, notes)."""
    keep = {k: v for k, v in wf.items() if k not in ("saved_at", "save_note", "enabled")}
    return json.dumps(keep, sort_keys=True)


def versions(wf_id: str) -> list[dict]:
    vdir = _dir() / f"{_slug(wf_id)}.versions"
    out = []
    for p in sorted(vdir.glob("*.json"), reverse=True) if vdir.exists() else []:
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append({"version": p.stem, "saved_at": v.get("saved_at") or p.stem, "name": v.get("name"),
                    "note": v.get("save_note", ""), "nodes": len(v.get("nodes", [])), "edges": len(v.get("edges", []))})
    return out


def get_version(wf_id: str, version: str) -> dict:
    p = _dir() / f"{_slug(wf_id)}.versions" / f"{_slug(version)}.json"
    if not p.exists():
        raise KeyError(f"No version {version}")
    return normalize(json.loads(p.read_text(encoding="utf-8")))


def restore(wf_id: str, version: str) -> dict:
    old = get_version(wf_id, version)
    old["id"] = _slug(wf_id)
    return save(old, note=f"restored version {version}")


def diff(old: dict, new: dict) -> dict:
    """Step-by-step comparison of two versions of a workflow."""
    a = {n["id"]: n for n in old.get("nodes", [])}
    b = {n["id"]: n for n in new.get("nodes", [])}
    changed = []
    for i in sorted(set(a) & set(b)):
        fields = [k for k in ("type", "title") if a[i].get(k) != b[i].get(k)]
        ca, cb = a[i].get("config") or {}, b[i].get("config") or {}
        fields += [f"config.{k}" for k in sorted(set(ca) | set(cb)) if ca.get(k) != cb.get(k)]
        moved = (a[i].get("x"), a[i].get("y")) != (b[i].get("x"), b[i].get("y"))
        if fields or moved:
            changed.append({"id": i, "fields": fields, "moved_only": not fields,
                            "before": {f: (ca.get(f[7:]) if f.startswith("config.") else a[i].get(f)) for f in fields},
                            "after": {f: (cb.get(f[7:]) if f.startswith("config.") else b[i].get(f)) for f in fields}})
    ek = lambda e: (e["from"], e["to"], e.get("when") or "")
    ea, eb = {ek(e) for e in old.get("edges", [])}, {ek(e) for e in new.get("edges", [])}
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)), "changed": changed,
            "edges_added": sorted(eb - ea), "edges_removed": sorted(ea - eb),
            "removed_nodes": [a[i] for i in sorted(set(a) - set(b))]}


def delete(wf_id: str) -> None:
    from .safety import audit
    p = _dir() / f"{_slug(wf_id)}.json"
    if p.exists():  # moved aside, not destroyed
        p.rename(p.with_suffix(f".deleted-{int(time.time())}"))
        audit(event="workflow_deleted", id=wf_id)


def validate(wf: dict) -> None:
    nodes = {n["id"]: n for n in wf.get("nodes", [])}
    for n in nodes.values():
        if n["type"] not in NODE_TYPES:
            raise ValueError(f"Unknown node type '{n['type']}'")
    trig = [n for n in nodes.values() if n["type"] == "trigger"]
    if len(trig) != 1:
        raise ValueError("A workflow needs exactly one Trigger node")
    for e in wf.get("edges", []):
        if e["from"] not in nodes or e["to"] not in nodes:
            raise ValueError("An arrow points to a missing node")
    _topo(wf)  # raises on loops


def _topo(wf: dict) -> list[str]:
    ids = [n["id"] for n in wf["nodes"]]
    indeg = {i: 0 for i in ids}
    for e in wf.get("edges", []):
        indeg[e["to"]] += 1
    order, q = [], [i for i in ids if indeg[i] == 0]
    while q:
        i = q.pop(0)
        order.append(i)
        for e in wf.get("edges", []):
            if e["from"] == i:
                indeg[e["to"]] -= 1
                if indeg[e["to"]] == 0:
                    q.append(e["to"])
    if len(order) != len(ids):
        raise ValueError("Workflows can't loop back on themselves - remove the loop arrow")
    return order


def _fill(text: str, ctx: dict) -> str:
    return (str(text).replace("{input}", ctx.get("input", "")).replace("{file}", ctx.get("file", ""))
            .replace("{date}", time.strftime("%Y-%m-%d %H:%M")))


def trigger_of(wf: dict) -> dict:
    t = next(n for n in wf["nodes"] if n["type"] == "trigger")["config"]
    mode = t.get("mode", "manual")
    if mode == "watch":
        return {"watch": t.get("watch", "inbox/new"), "pattern": t.get("pattern", "*")}
    if mode == "daily":
        return {"daily": t.get("daily", "09:00")}
    if mode == "every":
        return {"every_minutes": int(t.get("every_minutes", 60))}
    return {}


def allow_list(wf: dict) -> list[str]:
    allow = set(wf.get("allow") or [])
    for n in wf["nodes"]:
        c = n.get("config", {})
        allow |= set(c.get("allow", []))
        if n["type"] == "screen" and c.get("allow_unattended"):
            allow.add("look_at_screen")
    return sorted(allow)


# --------------------------------------------------------------------- run --
def run(wf: dict, gate: Gate | None = None, on_event: Callable[[dict], None] | None = None,
        file: Path | None = None) -> str:
    from .agent import Agent
    from .crew import load_crew, run_crew
    from .router import get_provider
    from . import system1

    allow = set(allow_list(wf))
    gate = gate or Gate(ask_fn=lambda req: "task" if req["tool"] in allow else "deny",
                        run_id=f"wf:{wf['id']}")
    emit = on_event or (lambda ev: None)
    nodes = {n["id"]: n for n in wf["nodes"]}
    edges = wf.get("edges", [])
    rel = ""
    if file is not None:
        from .tools import workspace
        rel = str(Path(file).resolve().relative_to(workspace())).replace("\\", "/")
    outputs: dict[str, str] = {}
    active: set[tuple[str, str]] = set()
    last = ""
    for nid in _topo(wf):
        n = nodes[nid]
        incoming = [e for e in edges if e["to"] == nid]
        if n["type"] != "trigger" and not any((e["from"], nid) in active for e in incoming):
            emit({"type": "node", "id": nid, "status": "skipped"})
            continue
        gate.check_stop()
        ctx = {"file": rel, "input": "\n\n".join(outputs[e["from"]] for e in incoming
                                                 if (e["from"], nid) in active and e["from"] in outputs)}
        emit({"type": "node", "id": nid, "status": "running", "label": n.get("title") or n["type"]})
        chosen = None
        try:
            out, chosen = _run_node(n, ctx, gate, emit, file, Agent, get_provider, load_crew,
                                    run_crew, system1)
        except Exception as e:
            from .safety import Stopped
            if isinstance(e, Stopped):
                raise
            emit({"type": "node", "id": nid, "status": "error", "output": str(e)[:500]})
            raise RuntimeError(f"Step '{n.get('title') or n['type']}' failed: {e}") from e
        outputs[nid] = out
        last = out or last
        for e in edges:
            if e["from"] != nid:
                continue
            if n["type"] == "condition":
                fire = (e.get("when") or "yes") == chosen
            else:
                fire = chosen is None or not e.get("when") or e["when"] == chosen
            if fire:
                active.add((nid, e["to"]))
        emit({"type": "node", "id": nid, "status": "done", "output": (out or "")[:800],
              "chosen": chosen})
    return last


def _run_node(n, ctx, gate, emit, file, Agent, get_provider, load_crew, run_crew, system1):
    c = {**NODE_TYPES[n["type"]]["defaults"], **(n.get("config") or {})}
    t = n["type"]
    if t == "trigger":
        return (f"File: {ctx['file']}" if ctx["file"] else "Started"), None
    if t == "screen":
        ok, why = gate.check("Workflow", "look_at_screen", {"question": c["question"], "keep": c.get("keep")})
        if not ok:
            raise RuntimeError(why)
        from .media import look_at_screen
        return look_at_screen(get_provider("local", task="screen"), _fill(c["question"], ctx),
                              keep=c.get("keep"))["answer"], None
    if t == "vision":
        if not file:
            raise RuntimeError("No image - use a 'watch' trigger so this step gets a file")
        from .automations import _image_b64
        from .media import describe_image
        prov = get_provider(c.get("provider"), task=c.get("task", "image"))
        return describe_image(prov, _image_b64(Path(file)), _fill(c["prompt"], ctx)), None
    if t == "video":
        if not file:
            raise RuntimeError("No video - use a 'watch' trigger so this step gets a file")
        from .media import analyze_video, save_outputs
        from .tools import workspace
        res = analyze_video(get_provider(c.get("provider"), task="video"), file,
                            _fill(c["question"], ctx), emit)
        save_outputs(res, Path(file).name, workspace() / "notes")
        return res["summary"], None
    if t == "decide":
        opts = {k: {"examples": v, "keywords": system1._kw(v)} for k, v in c["options"].items()}
        r = system1.choice(ctx["input"] or ctx["file"], opts, group=f"wf_{n['id']}",
                           instructions=c.get("question", ""))
        emit({"type": "decision", "id": n["id"], "label": r["label"], "confidence": r["confidence"],
              "method": r["method"]})
        return f"{r['label']} ({round(r['confidence'] * 100)}% sure · {r['method']})\n{ctx['input']}", r["label"]
    if t == "condition":
        hit = str(c.get("contains", "")).lower() in ctx["input"].lower()
        if c.get("negate"):
            hit = not hit
        return ctx["input"], ("yes" if hit else "no")
    if t == "llm":
        prov = get_provider(c.get("provider"), task=c.get("job", "chat"))
        return prov.chat([{"role": "user", "content": _fill(c["prompt"], ctx)}]).strip(), None
    if t == "agent":
        prov = get_provider(c.get("provider"), task="agent")
        return Agent(role=n.get("title") or "Workflow agent", tools=c.get("tools")).run(
            prov, _fill(c["prompt"], ctx), on_event=emit, gate=gate), None
    if t == "crew":
        inputs = {k: _fill(v, ctx) for k, v in (c.get("inputs") or {}).items()}
        return run_crew(load_crew(c["crew"]), inputs, get_provider(c.get("provider"), task="agent"),
                        on_event=emit, gate=gate)["final"], None
    if t == "note":
        text = _fill(c.get("template", "{input}"), ctx)
        if c.get("mode") == "overwrite":
            from .tools import write_file          # keeps a backup of the old version
            write_file(c["save_to"], text)
            where = f"workspace/{c['save_to']}"
        else:
            from .automations import _append
            where = _append(c["save_to"], text)
        return f"Saved to {where}\n{ctx['input']}", None
    if t == "tool":
        from .safety import redact
        from .tools import REGISTRY, run_tool
        name = c.get("tool", "")
        if name not in REGISTRY:
            raise RuntimeError(f"Unknown tool '{name}'")
        args = {k: _fill(v, ctx) if isinstance(v, str) else v for k, v in (c.get("args") or {}).items()}
        ok, why = gate.check("Workflow", name, args)
        if not ok:
            raise RuntimeError(why)
        return redact(run_tool(name, args)), None
    if t == "output":
        return ctx["input"], None
    raise RuntimeError(f"Unknown node type {t}")


# -------------------------------------------------------- capture a task ----
def from_trace(trace: dict, as_steps: bool = False) -> dict:
    """Turn a finished task into a reusable workflow (Capture -> Canvas).

    as_steps=False: Trigger -> Agent (same task, same tools) -> Save result. Flexible.
    as_steps=True : an RPA bot - every successful tool call becomes a fixed step that
                    replays exactly, with no AI in the loop. Fast and predictable.
    """
    from .safety import level_for
    ev = trace["events"]
    if as_steps:
        return _rpa_from_trace(trace)
    meta = next((e for e in ev if e.get("type") == "meta"), {})
    steps = [e for e in ev if e.get("type") == "tool"]
    used = sorted({s["tool"] for s in steps})
    approved = sorted({s["tool"] for s in steps if level_for(s["tool"]) == "ask"
                       and not str(s.get("result", "")).startswith("Error")})
    title = trace["title"] or "Captured task"
    nodes = [{"id": "t", "type": "trigger", "x": 60, "y": 160, "title": "Start", "config": {"mode": "manual"}}]
    kind = trace["kind"]
    if kind == "video":
        nodes[0]["config"] = {"mode": "watch", "watch": "inbox/videos", "pattern": "*.mp4,*.mov,*.mkv"}
        nodes.append({"id": "v", "type": "video", "x": 320, "y": 160, "title": "Analyse video",
                      "config": {"question": meta.get("question", title)}})
    elif kind == "crew" and meta.get("crew"):
        nodes.append({"id": "c", "type": "crew", "x": 320, "y": 160, "title": meta["crew"],
                      "config": {"crew": meta["crew"], "inputs": meta.get("inputs", {})}})
    else:
        nodes.append({"id": "a", "type": "agent", "x": 320, "y": 160, "title": "Agent",
                      "config": {"prompt": title, "tools": used or ["read_file"], "allow": approved}})
    nodes.append({"id": "n", "type": "note", "x": 580, "y": 160, "title": "Save result",
                  "config": {"save_to": f"notes/{_slug(title)[:30]}.md", "template": "## {date}\n{input}\n"}})
    edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]} for i in range(len(nodes) - 1)]
    return save({"name": title[:60], "enabled": False, "nodes": nodes, "edges": edges,
                 "source": f"captured from task {trace['id']}"})


def _rpa_from_trace(trace: dict) -> dict:
    from .safety import level_for
    steps = [e for e in trace["events"] if e.get("type") == "tool" and e.get("tool") != "delegate"
             and not str(e.get("result", "")).startswith("Error")]
    if not steps:
        raise ValueError("This task didn't use any tools successfully, so there are no steps to replay")
    title = trace["title"] or "Captured bot"
    nodes = [{"id": "start", "type": "trigger", "x": 60, "y": 80, "title": "Start", "config": {"mode": "manual"}}]
    for i, s_ in enumerate(steps, 1):
        col, row = i % 5, i // 5
        cfg = {"tool": s_["tool"], "args": s_.get("input") or {}}
        if level_for(s_["tool"]) == "ask":
            cfg["allow"] = [s_["tool"]]                    # you approved it when it first ran
        nodes.append({"id": f"s{i}", "type": "tool", "x": 60 + col * 250, "y": 80 + row * 170,
                      "title": f"{i}. {s_['tool'].replace('_', ' ')}", "config": cfg})
    nodes.append({"id": "done", "type": "output", "x": 60 + ((len(steps) + 1) % 5) * 250,
                  "y": 80 + ((len(steps) + 1) // 5) * 170, "title": "Done", "config": {}})
    edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]} for i in range(len(nodes) - 1)]
    return save({"name": f"Bot: {title[:52]}", "enabled": False, "nodes": nodes, "edges": edges,
                 "description": "Replays the exact steps of a finished task - no AI needed.",
                 "source": f"RPA steps captured from task {trace['id']}"}, note="captured")


# ------------------------------------------------------ scheduling glue -----
def as_automation(wf: dict) -> dict:
    return {"name": f"wf:{wf['id']}", "enabled": wf.get("enabled", False), "trigger": trigger_of(wf),
            "action": {"type": "workflow", "id": wf["id"]}, "allow": allow_list(wf)}


def set_enabled(wf_id: str, on: bool) -> None:
    from . import automations
    wf = get(wf_id)
    if on and not trigger_of(wf):
        raise ValueError("This workflow starts manually - give its Trigger a folder, time or interval first")
    wf["enabled"] = on
    save(wf)
    automations.seed_state(as_automation(wf), on)


def scheduled() -> list[dict]:
    out = []
    for w in list_all():
        if w["enabled"]:
            try:
                wf = get(w["id"])
            except KeyError:
                continue
            if trigger_of(wf):
                out.append(as_automation(wf))
    return out
