"""The "brain": every run is recorded as a trace (task -> steps -> result).

  - Rate a trace 👍/👎. Good traces guide future agents (lessons) and become
    training data; bad ones open an incident and are kept out of training.
  - Incidents collect what went wrong: errors, blocked commands, denials,
    stops, and answers you report as wrong.
  - export_training() writes a JSONL dataset (chat format) you can use to
    fine-tune a small model later (see GUIDE.md -> Training loop).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import load_config, resolve


def _db():
    path = resolve(load_config().get("memory", {}).get("db", "./data/memory.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS traces(
        id TEXT PRIMARY KEY, kind TEXT, title TEXT, task_type TEXT, model TEXT, status TEXT,
        rating INTEGER DEFAULT 0, started REAL, duration REAL, events TEXT, result TEXT,
        session TEXT, lesson_id INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS incidents(
        id INTEGER PRIMARY KEY, ts REAL, trace_id TEXT, kind TEXT, severity TEXT,
        title TEXT, detail TEXT, status TEXT DEFAULT 'open', note TEXT DEFAULT '')""")
    return con


# ------------------------------------------------------------------ traces --
def save_trace(trace_id: str, kind: str, title: str, model: str, status: str,
               events: list[dict], result: str = "", started: float | None = None,
               session: str = "", lesson_id: int | None = None) -> None:
    from .hints import classify
    started = started or time.time()
    try:
        task_type = classify(title)["label"] if title else kind
    except Exception:
        task_type = kind
    slim = [{k: (v[:1500] if isinstance(v, str) else v) for k, v in e.items()} for e in events][:400]
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trace_id, kind, title[:300], task_type, model, status, 0, started,
                     round(time.time() - started, 2), json.dumps(slim, ensure_ascii=False),
                     str(result)[:4000], session, lesson_id))
    _auto_incidents(trace_id, title, status, slim)


_COLS = ("id", "kind", "title", "task_type", "model", "status", "rating", "started",
         "duration", "events", "result", "session", "lesson_id")


def _row(r) -> dict:
    d = dict(zip(_COLS, r))
    d["events"] = json.loads(d["events"] or "[]")
    return d


def get_trace(trace_id: str) -> dict | None:
    with _db() as con:
        r = con.execute(f"SELECT {','.join(_COLS)} FROM traces WHERE id=?", (trace_id,)).fetchone()
    return _row(r) if r else None


def list_traces(q: str = "", limit: int = 50) -> list[dict]:
    with _db() as con:
        if q:
            rows = con.execute(f"SELECT {','.join(_COLS)} FROM traces WHERE title LIKE ? "
                               "ORDER BY started DESC LIMIT ?", (f"%{q}%", limit)).fetchall()
        else:
            rows = con.execute(f"SELECT {','.join(_COLS)} FROM traces ORDER BY started DESC LIMIT ?",
                               (limit,)).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d["steps"] = sum(1 for e in d.pop("events") if e.get("type") == "tool")
        out.append(d)
    return out


def rate(trace_id: str, good: bool, note: str = "") -> None:
    from . import memory
    t = get_trace(trace_id)
    if not t:
        raise KeyError(trace_id)
    with _db() as con:
        con.execute("UPDATE traces SET rating=? WHERE id=?", (1 if good else -1, trace_id))
    if t.get("lesson_id"):
        memory.rate_lesson(t["lesson_id"], good)
    if not good:
        report_incident(trace_id, note or "Marked as a bad result", kind="bad_result")


def similar_traces(text: str, k: int = 3) -> list[dict]:
    from .system1 import similar
    ts = [t for t in list_traces(limit=200) if t["rating"] >= 0 and t["status"] == "ok"]
    return [{**ts[i], "similarity": round(s, 2)} for i, s in similar(text, [t["title"] for t in ts], k)]


def stats() -> dict:
    with _db() as con:
        rows = con.execute("SELECT task_type, status, rating FROM traces").fetchall()
        open_inc = con.execute("SELECT COUNT(*) FROM incidents WHERE status='open'").fetchone()[0]
    by: dict[str, dict] = {}
    for tt, st, rt in rows:
        b = by.setdefault(tt or "other", {"runs": 0, "ok": 0, "good": 0, "bad": 0})
        b["runs"] += 1
        b["ok"] += st == "ok"
        b["good"] += rt == 1
        b["bad"] += rt == -1
    return {"total": len(rows), "open_incidents": open_inc, "by_type": by,
            "trainable": sum(1 for _, st, rt in rows if st == "ok" and rt == 1)}


# --------------------------------------------------------------- incidents --
def _auto_incidents(trace_id: str, title: str, status: str, events: list[dict]) -> None:
    if status == "error":
        err = next((e.get("text", "") for e in reversed(events) if e.get("type") == "error"), "")
        add_incident(trace_id, "error", "high", f"Task failed: {title[:80]}", err)
    elif status == "stopped":
        add_incident(trace_id, "stopped", "low", f"Stopped by you: {title[:80]}", "")
    for e in events:
        res = str(e.get("result", ""))
        if e.get("type") == "tool" and "blocked by safety rule" in res:
            add_incident(trace_id, "blocked", "high", f"Blocked {e.get('tool')}: dangerous action",
                         json.dumps(e.get("input", {}))[:500])
        elif e.get("type") == "denied" and e.get("why") == "no answer in time":
            add_incident(trace_id, "timeout", "low", f"Approval for {e.get('tool')} timed out", "")
        elif e.get("type") == "limit":
            add_incident(trace_id, "step_limit", "medium", f"Ran out of steps: {title[:80]}", "")


def add_incident(trace_id: str | None, kind: str, severity: str, title: str, detail: str = "") -> int:
    with _db() as con:
        cur = con.execute("INSERT INTO incidents(ts, trace_id, kind, severity, title, detail) "
                          "VALUES (?,?,?,?,?,?)", (time.time(), trace_id, kind, severity,
                                                   title[:200], detail[:2000]))
        return cur.lastrowid


def report_incident(trace_id: str | None, note: str, kind: str = "reported") -> int:
    t = get_trace(trace_id) if trace_id else None
    return add_incident(trace_id, kind, "medium",
                        f"Reported: {(t or {}).get('title', 'task')[:80]}", note)


def list_incidents(status: str | None = None) -> list[dict]:
    cols = ("id", "ts", "trace_id", "kind", "severity", "title", "detail", "status", "note")
    with _db() as con:
        if status:
            rows = con.execute(f"SELECT {','.join(cols)} FROM incidents WHERE status=? ORDER BY ts DESC",
                               (status,)).fetchall()
        else:
            rows = con.execute(f"SELECT {','.join(cols)} FROM incidents ORDER BY ts DESC LIMIT 200").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def resolve_incident(inc_id: int, note: str = "") -> None:
    with _db() as con:
        con.execute("UPDATE incidents SET status='resolved', note=? WHERE id=?", (note, inc_id))


# ---------------------------------------------------------------- training --
def _to_messages(t: dict) -> list[dict] | None:
    from .agent import Agent
    steps = [e for e in t["events"] if e.get("type") == "tool"]
    final = t["result"]
    if not final or t["kind"] not in ("agent", "crew", "automation", "workflow"):
        return None
    msgs = [{"role": "system", "content": Agent().system_prompt()},
            {"role": "user", "content": t["title"]}]
    for s in steps:
        msgs.append({"role": "assistant", "content": json.dumps(
            {"thought": s.get("thought", ""), "action": s["tool"], "input": s.get("input", {})})})
        msgs.append({"role": "user", "content": f"Tool result:\n{s.get('result', '')}\n\nContinue."})
    msgs.append({"role": "assistant", "content": json.dumps({"action": "final", "answer": final})})
    return msgs


def export_training(include_unrated: bool = False) -> dict:
    from .tools import workspace
    out_dir = workspace() / "training"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    good_p, bad_p = out_dir / f"good_{stamp}.jsonl", out_dir / f"rejected_{stamp}.jsonl"
    n_good = n_bad = 0
    with _db() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM traces WHERE status='ok'").fetchall()]
    with open(good_p, "w", encoding="utf-8") as g, open(bad_p, "w", encoding="utf-8") as b:
        for tid in ids:
            t = get_trace(tid)
            msgs = _to_messages(t)
            if not msgs:
                continue
            if t["rating"] == 1 or (include_unrated and t["rating"] == 0):
                g.write(json.dumps({"messages": msgs}, ensure_ascii=False) + "\n")
                n_good += 1
            elif t["rating"] == -1:
                b.write(json.dumps({"messages": msgs}, ensure_ascii=False) + "\n")
                n_bad += 1
    return {"good": n_good, "rejected": n_bad,
            "good_file": f"workspace/training/{good_p.name}",
            "rejected_file": f"workspace/training/{bad_p.name}"}
