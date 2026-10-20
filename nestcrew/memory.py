"""Chat history stored in a local SQLite file (never leaves your laptop)."""
from __future__ import annotations

import sqlite3
import time

from .config import load_config, resolve


def _db():
    path = resolve(load_config().get("memory", {}).get("db", "./data/memory.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, ts REAL)""")
    return con


def add(session: str, role: str, content: str) -> None:
    with _db() as con:
        con.execute("INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)",
                    (session, role, content, time.time()))


def history(session: str, limit: int = 8) -> list[dict]:
    """Last N turns (kept short so small models don't run out of context)."""
    with _db() as con:
        rows = con.execute("SELECT role, content FROM messages WHERE session=? "
                           "ORDER BY id DESC LIMIT ?", (session, limit)).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def sessions(limit: int = 40) -> list[dict]:
    """Conversation list for the sidebar: first message as the title."""
    with _db() as con:
        rows = con.execute("""SELECT session, MAX(ts),
            (SELECT content FROM messages m2 WHERE m2.session = m.session AND m2.role='user' ORDER BY id LIMIT 1)
            FROM messages m GROUP BY session ORDER BY MAX(ts) DESC LIMIT ?""", (limit,)).fetchall()
    return [{"id": r[0], "updated": r[1], "title": (r[2] or "New chat")[:60]} for r in rows]


def transcript(session: str, limit: int = 200) -> list[dict]:
    return history(session, limit)


def clear(session: str) -> None:
    with _db() as con:
        con.execute("DELETE FROM messages WHERE session=?", (session,))
    p = _image_path(session)
    if p.exists():
        p.unlink()


# --------------------------------------------- the photo a chat is about ----
# Follow-up questions ("what are they wearing?") need the photo again, so the
# last image of each chat is kept on this PC (data/chat_images) and re-sent.
def _image_path(session: str):
    import re
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session)[:64] or "default"
    return resolve("./data/chat_images") / f"{safe}.jpg"


def save_image(session: str, b64: str) -> None:
    import base64
    p = _image_path(session)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(b64))


def last_image(session: str) -> str | None:
    import base64
    p = _image_path(session)
    return base64.b64encode(p.read_bytes()).decode() if p.exists() else None


def has_image(session: str) -> bool:
    return _image_path(session).exists()


# ---------------------------------------------------- experience (lessons) ----
# Each successful agent run is stored as a "lesson": the task, which tools were
# used in which order, and the answer. Next time a similar task comes in, the
# best matches are shown to the agent as guidance. 👍 = trusted, 👎 = deleted.

def _lessons():
    con = _db()
    con.execute("""CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY, agent TEXT, task TEXT, approach TEXT, answer TEXT,
        status TEXT, uses INTEGER DEFAULT 0, ts REAL)""")
    return con


def _words(text: str) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def save_lesson(agent: str, task: str, events: list[dict], answer: str,
                status: str = "auto") -> int:
    steps = [e for e in events if e.get("type") == "tool"]
    approach = " -> ".join(f"{e['tool']}({', '.join(f'{k}' for k in e.get('input', {}))})"
                           for e in steps) or "answered directly"
    with _lessons() as con:
        cur = con.execute("INSERT INTO lessons(agent, task, approach, answer, status, ts) "
                          "VALUES (?,?,?,?,?,?)",
                          (agent, task[:1000], approach, answer[:1500], status, time.time()))
        return cur.lastrowid


def rate_lesson(lesson_id: int, good: bool) -> None:
    with _lessons() as con:
        if good:
            con.execute("UPDATE lessons SET status='good' WHERE id=?", (lesson_id,))
        else:
            con.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))


def recall(task: str, k: int = 2, agent: str | None = None) -> list[dict]:
    """Most similar past successes (keyword overlap, fully offline). 👍 ones rank higher."""
    want = _words(task)
    if not want:
        return []
    with _lessons() as con:
        rows = con.execute("SELECT id, agent, task, approach, answer, status FROM lessons "
                           "ORDER BY id DESC LIMIT 500").fetchall()
    scored = []
    for r in rows:
        if agent and r[1] != agent:
            continue
        have = _words(r[2])
        score = len(want & have) / (len(want | have) or 1)
        if r[5] == "good":
            score *= 1.5
        if score >= 0.2:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    out = [{"id": r[0], "task": r[2], "approach": r[3], "answer": r[4], "status": r[5]}
           for _, r in scored[:k]]
    if out:
        with _lessons() as con:
            con.executemany("UPDATE lessons SET uses = uses + 1 WHERE id=?",
                            [(o["id"],) for o in out])
    return out


def list_lessons(limit: int = 50) -> list[dict]:
    with _lessons() as con:
        rows = con.execute("SELECT id, agent, task, approach, status, uses FROM lessons "
                           "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(("id", "agent", "task", "approach", "status", "uses"), r)) for r in rows]
