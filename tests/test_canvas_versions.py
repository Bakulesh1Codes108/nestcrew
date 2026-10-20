"""Canvas: RPA bots captured from runs, version history, compare, restore."""
import json

import pytest

from nestcrew import rpa, traces, workflows
from nestcrew.safety import Gate
from tests.test_core import FakeModel, offline  # noqa: F401
from tests.test_rpa import home  # noqa: F401


@pytest.fixture(autouse=True)
def wfdir(monkeypatch, tmp_path):
    monkeypatch.setattr(workflows, "_dir", lambda: (tmp_path / "wf").mkdir(exist_ok=True) or tmp_path / "wf")


def _wf(prompt="a"):
    return {"name": "Flow", "nodes": [{"id": "t", "type": "trigger", "x": 0, "y": 0, "config": {"mode": "manual"}},
                                      {"id": "a", "type": "llm", "x": 250, "y": 0, "config": {"prompt": prompt}}],
            "edges": [{"from": "t", "to": "a"}]}


def test_every_real_change_keeps_a_version():
    wf = workflows.save(_wf("v1"))
    workflows.save({**wf})                                      # no change -> no version
    assert workflows.versions(wf["id"]) == []
    wf["nodes"][1]["config"]["prompt"] = "v2"
    wf["nodes"].append({"id": "n", "type": "note", "x": 500, "y": 0, "config": {"save_to": "notes/x.md"}})
    wf["edges"].append({"from": "a", "to": "n"})
    workflows.save(wf)
    vs = workflows.versions(wf["id"])
    assert len(vs) == 1 and vs[0]["nodes"] == 2


def test_diff_and_restore():
    wf = workflows.save(_wf("v1"))
    wf["nodes"][1]["config"]["prompt"] = "v2"
    wf["nodes"].append({"id": "n", "type": "note", "x": 500, "y": 0, "config": {"save_to": "notes/x.md"}})
    wf["edges"].append({"from": "a", "to": "n"})
    workflows.save(wf)
    v = workflows.versions(wf["id"])[0]["version"]
    d = workflows.diff(workflows.get_version(wf["id"], v), workflows.get(wf["id"]))
    assert d["added"] == ["n"] and d["edges_added"] == [("a", "n", "")]
    assert d["changed"][0]["id"] == "a" and d["changed"][0]["before"]["config.prompt"] == "v1"
    restored = workflows.restore(wf["id"], v)
    assert [n["id"] for n in restored["nodes"]] == ["t", "a"]
    assert len(workflows.versions(wf["id"])) == 2                # the v2 we left is kept too


def test_capture_rpa_bot_and_replay_without_ai(home):
    rpa.copy_files("Desktop/BIRAC", "Documents/first", "*.pdf")    # pretend the agent did this
    ev = [{"type": "tool", "agent": "A", "tool": "list_folder", "input": {"path": "Desktop/BIRAC"}, "result": "3 items"},
          {"type": "tool", "agent": "A", "tool": "copy_files",
           "input": {"source": "Desktop/BIRAC", "destination": "Documents/Backup", "pattern": "*.pdf"}, "result": "Copied 2"},
          {"type": "tool", "agent": "A", "tool": "move_files", "input": {"source": "x", "destination": "y"},
           "result": "Error: 'x' doesn't exist"}]
    traces.save_trace("r1", "agent", "back up my pdfs", "m", "ok", ev, "done")
    bot = workflows.from_trace(traces.get_trace("r1"), as_steps=True)
    tools_used = [n["config"].get("tool") for n in bot["nodes"] if n["type"] == "tool"]
    assert tools_used == ["list_folder", "copy_files"]           # failed step left out
    assert workflows.allow_list(bot) == ["copy_files"]           # approved once -> may replay unattended
    workflows.run(bot)                                           # no model at all
    assert sorted(p.name for p in (home / "Documents" / "Backup").iterdir()) == ["a.pdf", "b.pdf"]


def test_bot_needs_tool_steps():
    traces.save_trace("r2", "agent", "chat only", "m", "ok", [], "hi")
    with pytest.raises(ValueError, match="didn't use any tools"):
        workflows.from_trace(traces.get_trace("r2"), as_steps=True)


def test_api_versions_compare_restore(monkeypatch):
    from fastapi.testclient import TestClient
    from nestcrew import server
    H = {"x-nestcrew-token": server.TOKEN}
    c = TestClient(server.app, base_url="http://127.0.0.1")
    wf = c.post("/api/workflows/save", headers=H, json={"workflow": _wf("v1")}).json()
    wf["nodes"][1]["config"]["prompt"] = "v2"
    c.post("/api/workflows/save", headers=H, json={"workflow": wf, "note": "better prompt"})
    vs = c.get(f"/api/workflows/{wf['id']}/versions", headers=H).json()
    r = c.post("/api/workflows/compare", headers=H, json={"id": wf["id"], "version": vs[0]["version"]}).json()
    assert r["diff"]["changed"][0]["after"]["config.prompt"] == "v2" and r["current"]["save_note"] == "better prompt"
    back = c.post("/api/workflows/restore", headers=H, json={"id": wf["id"], "version": vs[0]["version"]}).json()
    assert back["nodes"][1]["config"]["prompt"] == "v1"
