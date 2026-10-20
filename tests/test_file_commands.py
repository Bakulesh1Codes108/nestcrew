"""Rigorous checks for plain file commands, end to end on real (temporary) folders.

Found on a real laptop: "copy all files from BIRAC to BIRAC2" was misread by a 0.8B
model, which looked in the empty workspace. These tests pin the behaviour down:
the right files land in the right folder, approvals are always asked, and a denial,
a missing folder or Stop changes nothing.
"""
import json

import pytest
from fastapi.testclient import TestClient

from nestcrew import memory, rpa, server
from nestcrew.agent import Agent
from nestcrew.safety import Gate, Stopped
from tests.test_core import FakeModel, offline  # noqa: F401

H = {"x-nestcrew-token": server.TOKEN}


@pytest.fixture
def pc(monkeypatch, tmp_path, tmp_path_factory):
    """A fake user home: Desktop/BIRAC (3 files), empty Desktop/BIRAC2, Downloads with mixed files."""
    h = tmp_path_factory.mktemp("home")                 # outside the workspace, like a real PC
    birac = h / "Desktop" / "BIRAC"
    birac.mkdir(parents=True)
    (h / "Desktop" / "BIRAC2").mkdir()
    (h / "Desktop" / "NestCrew test" / "BIRAC").mkdir(parents=True)
    for n in ("invoice_A101.txt", "invoice_A102.txt", "totals.csv"):
        (birac / n).write_text(n)
        (h / "Desktop" / "NestCrew test" / "BIRAC" / n).write_text(n)
    dl = h / "Downloads"
    dl.mkdir()
    for n in ("a.pdf", "b.pdf", "sheet.xlsx", "photo.jpg"):
        (dl / n).write_text(n)
    (h / "Documents").mkdir()
    monkeypatch.setattr(rpa, "_home", lambda: h)
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "rpa", {"folders": ["desktop", "documents", "downloads"]})
    monkeypatch.setattr(rpa, "_undo_path", lambda: tmp_path / "undo.json")
    return h


def names(p):
    return sorted(x.name for x in p.iterdir()) if p.exists() else None


ALL3 = ["invoice_A101.txt", "invoice_A102.txt", "totals.csv"]

# (what the user types, folder that must receive files, expected names there, "copy"/"move")
CASES = [
    ("copy all files from BIRAC to BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("Copy all files from BIRAC to BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("COPY ALL FILES FROM BIRAC TO BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("copy all file from BIRAC to BIRAC2.", "Desktop/BIRAC2", ALL3, "copy"),
    ("please copy everything from BIRAC into BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("copy files from BIRAC to BIRAC2 on my desktop", "Desktop/BIRAC2", ALL3, "copy"),
    ("copy all files from Desktop/BIRAC to Desktop/BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("copy all files from desktop\\BIRAC to desktop\\BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("copy all files from the folder BIRAC to the folder BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("copy all files from BIRAC to a new folder called BIRAC backup", "Desktop/BIRAC backup", ALL3, "copy"),
    ('Copy all files from "NestCrew test/BIRAC" on my Desktop into a new folder "NestCrew test/BIRAC copy"',
     "Desktop/NestCrew test/BIRAC copy", ALL3, "copy"),
    ("copy the csv files from BIRAC to BIRAC2", "Desktop/BIRAC2", ["totals.csv"], "copy"),
    ("copy *.txt from BIRAC to BIRAC2", "Desktop/BIRAC2", ["invoice_A101.txt", "invoice_A102.txt"], "copy"),
    ("move all files from BIRAC to BIRAC2", "Desktop/BIRAC2", ALL3, "move"),
    ("MOVE ALL FILE FROM BIRAC TO BIRAC2 ON DESKTOP", "Desktop/BIRAC2", ALL3, "move"),
    ("transfer all files from BIRAC to BIRAC2", "Desktop/BIRAC2", ALL3, "copy"),
    ("move the pdfs from Downloads to Documents/Invoices", "Documents/Invoices", ["a.pdf", "b.pdf"], "move"),
    ("copy excel files from Downloads into Documents", "Documents", ["sheet.xlsx"], "copy"),
    ("copy all jpg files from downloads to pictures", None, None, "outside"),   # Pictures not allowed here
]


@pytest.mark.parametrize("task,dest,expected,kind", CASES, ids=[c[0][:48] for c in CASES])
def test_file_command_puts_the_right_files_in_the_right_place(pc, task, dest, expected, kind):
    asked = []
    model = FakeModel([])                       # the model must not be needed for these
    out = Agent().run(model, task, gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    if kind == "outside":
        assert out.startswith("I stopped") and "outside" in out and not asked   # refused before asking
        return
    assert len(asked) == 1 and asked[0]["tool"] == ("move_files" if kind == "move" else "copy_files")
    got = names(pc / dest)
    assert got is not None and all(n in got for n in expected), (out, got)
    src = pc / ("Downloads" if "ownloads" in task else
                "Desktop/NestCrew test/BIRAC" if "NestCrew test" in task else "Desktop/BIRAC")
    left = names(src)
    for n in expected:
        assert (n in left) == (kind == "copy")   # copy keeps the originals, move takes them


def test_preview_lists_every_file_before_anything_changes(pc):
    seen = []

    def ask(req):
        seen.append(req["preview"])
        assert names(pc / "Desktop" / "BIRAC2") == []             # nothing copied yet
        return "once"
    Agent().run(FakeModel([]), "copy all files from BIRAC to BIRAC2", gate=Gate(ask_fn=ask))
    assert "COPY 3 file(s)" in seen[0] and all(n in seen[0] for n in ALL3)
    assert "Desktop/BIRAC2" in seen[0]


def test_deny_changes_nothing(pc):
    out = Agent().run(FakeModel([]), "move all files from BIRAC to BIRAC2", gate=Gate(ask_fn=lambda r: "deny"))
    assert names(pc / "Desktop" / "BIRAC") == ALL3 and names(pc / "Desktop" / "BIRAC2") == []
    assert out.startswith("I stopped")


def test_no_human_means_nothing_changes(pc):
    Agent().run(FakeModel([]), "move all files from BIRAC to BIRAC2")       # unattended: 'ask' -> denied
    assert names(pc / "Desktop" / "BIRAC") == ALL3


def test_stop_while_waiting_for_approval(pc):
    gate = Gate(ask_fn=lambda r: (gate.cancel.set(), "deny")[1])
    with pytest.raises(Stopped):
        Agent().run(FakeModel([]), "copy all files from BIRAC to BIRAC2", gate=gate)
    assert names(pc / "Desktop" / "BIRAC2") == []


def test_missing_source_folder_is_reported(pc):
    asked = []
    out = Agent().run(FakeModel([]), "copy all files from BIRAK to BIRAC2",
                      gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert out.startswith("I stopped") and "BIRAK" in out and not asked


def test_copy_twice_never_overwrites(pc):
    for _ in range(2):
        Agent().run(FakeModel([]), "copy all files from BIRAC to BIRAC2", gate=Gate(ask_fn=lambda r: "once"))
    got = names(pc / "Desktop" / "BIRAC2")
    assert "totals.csv" in got and "totals (2).csv" in got and len(got) == 6


def test_move_then_undo_puts_everything_back(pc):
    Agent().run(FakeModel([]), "move all files from BIRAC to BIRAC2", gate=Gate(ask_fn=lambda r: "once"))
    assert names(pc / "Desktop" / "BIRAC") == []
    assert "Put back 3" in rpa.undo_last_move()
    assert names(pc / "Desktop" / "BIRAC") == ALL3 and names(pc / "Desktop" / "BIRAC2") == []


def test_steps_are_recorded_for_save_as_bot(pc):
    from nestcrew import workflows
    a = Agent()
    a.run(FakeModel([]), "copy all files from BIRAC to BIRAC2", gate=Gate(ask_fn=lambda r: "once"))
    steps = [e for e in a.events if e["type"] == "tool"]
    assert [s["tool"] for s in steps] == ["list_folder", "copy_files"]
    wf = workflows.from_trace({"id": "t1", "title": "copy BIRAC", "events": a.events}, as_steps=True)
    tools = [n["config"].get("tool") for n in wf["nodes"] if n["type"] == "tool"]
    assert tools == ["list_folder", "copy_files"]


@pytest.mark.parametrize("task", [
    "what files are in BIRAC?", "summarise totals.csv", "copy the totals from report into a new sheet",
    "write an excel of my expenses", "move files from A to A", "copy this text to the clipboard",
    "tell me how to copy files from BIRAC to BIRAC2 in Windows"])
def test_other_requests_are_left_to_the_model(task):
    assert rpa.quick_plan(task) is None


# ------------------------------------------------- through the real HTTP API --
@pytest.fixture
def client(monkeypatch, tmp_path, pc):
    monkeypatch.setattr(memory, "resolve", lambda p: tmp_path / p.replace("./", ""))
    from nestcrew import router
    monkeypatch.setattr(router, "get_provider", lambda *a, **k: FakeModel([]))
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: FakeModel([]))
    return TestClient(server.app, base_url="http://127.0.0.1")


def _run(client, task, decision):
    events = []
    with client.stream("POST", "/api/agent", headers=H, json={"message": task, "session": "t"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            events.append(ev)
            if ev["type"] == "permission":
                ok = client.post("/api/permission", headers=H,
                                 json={"run": ev["run"], "id": ev["id"], "decision": decision})
                assert ok.status_code == 200
    return events


def test_api_copy_with_approval_click(client, pc):
    ev = _run(client, "copy all files from BIRAC to BIRAC2", "once")
    kinds = [e["type"] for e in ev]
    assert "permission" in kinds and kinds[-1] == "trace"
    assert names(pc / "Desktop" / "BIRAC2") == ALL3 and names(pc / "Desktop" / "BIRAC") == ALL3
    result = next(e for e in ev if e["type"] == "result")["result"]
    text = result if isinstance(result, str) else json.dumps(result)
    assert "Copied 3 file(s)" in text


def test_api_deny_click(client, pc):
    _run(client, "move all files from BIRAC to BIRAC2", "deny")
    assert names(pc / "Desktop" / "BIRAC") == ALL3 and names(pc / "Desktop" / "BIRAC2") == []


def test_restart_needs_token_and_reports_update(client):
    assert client.post("/api/restart").status_code in (401, 403)
    s = client.get("/api/status", headers=H).json()
    assert s["update_ready"] is False


@pytest.mark.parametrize("task", [c[0] for c in CASES])
def test_file_commands_always_route_to_an_agent(task, monkeypatch):
    from nestcrew import hints
    monkeypatch.setattr(hints, "classify", lambda t: (_ for _ in ()).throw(AssertionError("model used")))
    h = hints.hint(task)
    assert h["type"] == "files" and h["confidence"] >= 0.9


# ------------------------------------------------ folders not at the top level --
def test_folder_inside_subfolders_is_found(pc):
    deep = pc / "Documents" / "Projects" / "2026" / "Grants"
    deep.mkdir(parents=True)
    (deep / "a.pdf").write_text("a")
    assert rpa.locate("Grants") == "Documents/Projects/2026/Grants"
    asked = []
    out = Agent().run(FakeModel([]), "copy all files from Grants to Grants backup",
                      gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert "Copied 1 file(s)" in out and names(pc / "Documents/Projects/2026/Grants backup") == ["a.pdf"]
    assert asked and "Documents/Projects/2026/Grants" in asked[0]["preview"]


def test_same_name_in_two_places_asks_which_one(pc):
    (pc / "Documents" / "Old" / "BIRAC").mkdir(parents=True)
    (pc / "Desktop" / "Reports").mkdir()
    (pc / "Documents" / "Reports").mkdir()
    asked = []
    out = Agent().run(FakeModel([]), "copy all files from Reports to Reports2",
                      gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert out.startswith("I stopped") and "2 places" in out and "Tell me which one" in out and not asked
    # a top-level folder wins over deeper ones with the same name - no question needed
    assert rpa.locate("BIRAC") == "Desktop/BIRAC"
    (pc / "Downloads" / "x" / "Deep").mkdir(parents=True)
    (pc / "Documents" / "Deep").mkdir()
    assert rpa.locate("Deep") == "Documents/Deep"


def test_find_folder_tool_and_permission(pc):
    (pc / "Desktop" / "Work" / "Invoices").mkdir(parents=True)
    from nestcrew.safety import Gate as G
    ok, _ = G().check("x", "find_folder", {"name": "Invoices"})          # reading needs no approval
    assert ok and "Desktop/Work/Invoices" in rpa.find_folder("Invoices")
    assert "No folder" in rpa.find_folder("Nope123")


def test_hidden_and_private_folders_are_not_searched(pc):
    (pc / "Documents" / ".secret" / "Keys").mkdir(parents=True)
    (pc / "Documents" / "AppData" / "Keys").mkdir(parents=True)
    assert rpa.search_folders("Keys") == []
