"""System 1, hints, traces/incidents/training, and Canvas workflows (offline)."""
import json
import os
import time

import pytest

from nestcrew import hints, system1, traces, workflows
from nestcrew.safety import Gate
from tests.test_core import FakeModel, offline  # noqa: F401


@pytest.fixture(autouse=True)
def iso(monkeypatch, tmp_path):
    monkeypatch.setattr(system1, "embed", lambda texts: None)        # keyword mode (no Ollama)
    monkeypatch.setattr(system1, "_examples_path", lambda: tmp_path / "s1.json")
    monkeypatch.setattr(workflows, "_dir", lambda: (tmp_path / "wf").mkdir(exist_ok=True) or tmp_path / "wf")
    import nestcrew.automations as au
    monkeypatch.setattr(au, "_state_path", lambda: tmp_path / "auto.json")


# ------------------------------------------------------------ System 1
def test_choice_returns_typed_label_with_probabilities():
    r = system1.choice("my laptop shows an error popup", {
        "problem": {"examples": ["there is an error"], "keywords": ["error", "crash", "popup"]},
        "fine": {"examples": ["all good"], "keywords": ["fine", "ok", "good"]}})
    assert r["label"] == "problem" and 0.5 < r["confidence"] <= 1 and set(r["probs"]) == {"problem", "fine"}


def test_hints_route_common_tasks():
    assert hints.hint("extract the total from this invoice")["type"] == "documents"
    assert hints.hint("every morning summarise my notes")["type"] == "automation"
    assert hints.hint("count people in this cctv clip")["mode"] == "video"
    h = hints.hint("what does the error on my screen mean")
    assert h["type"] == "screen" and "screen" in h["toggles"]


def test_system1_learns_from_corrections():
    text = "tidy my downloads thing"
    before = hints.classify(text)["label"]
    hints.correct(text, "files")
    hints.correct("sort my downloads thing out", "files")
    assert hints.classify(text)["label"] == "files" or before == "files"


def test_assign_hints():
    assert hints.assign_hint("agent", {"id": "smolvlm2-256m", "tools": False})["level"] == "no"
    assert hints.assign_hint("video", {"id": "smolvlm2-256m", "vision": True})["level"] == "best"
    assert hints.assign_hint("screen", {"id": "qwen3-1.7b", "vision": False})["level"] == "no"
    assert hints.fit("~1 GB", 16) == "easy" and hints.fit("~4-6 GB", 8) == "heavy"


# ------------------------------------------------------------ traces / incidents / training
def _trace(tid="t1", status="ok", events=None, kind="agent"):
    ev = events or [{"type": "tool", "agent": "A", "tool": "calculator", "thought": "math",
                     "input": {"expression": "2+2"}, "result": "4"}]
    traces.save_trace(tid, kind, "what is 2+2", "fake", status, ev, "4")


def test_trace_rate_and_export(tmp_path):
    _trace()
    traces.rate("t1", True)
    st = traces.stats()
    assert st["total"] == 1 and st["trainable"] == 1
    out = traces.export_training()
    line = json.loads((tmp_path / out["good_file"].replace("workspace/", "")).read_text().splitlines()[0])
    roles = [m["role"] for m in line["messages"]]
    assert roles[:2] == ["system", "user"] and '"final"' in line["messages"][-1]["content"]


def test_auto_incidents():
    _trace("e1", status="error", events=[{"type": "error", "text": "boom"}])
    _trace("b1", events=[{"type": "tool", "agent": "A", "tool": "run_command", "input": {"command": "rm -rf /"},
                          "result": "Error: Command blocked by safety rule (x). It will not run."}])
    _trace("s1", status="stopped", events=[])
    kinds = {i["kind"] for i in traces.list_incidents()}
    assert {"error", "blocked", "stopped"} <= kinds


def test_bad_rating_opens_incident_and_is_excluded():
    _trace("x1")
    traces.rate("x1", False, "wrong answer")
    assert any(i["trace_id"] == "x1" for i in traces.list_incidents("open"))
    assert traces.export_training()["good"] == 0
    iid = traces.list_incidents("open")[0]["id"]
    traces.resolve_incident(iid, "used a bigger model")
    assert not traces.list_incidents("open")


# ------------------------------------------------------------ workflows
def _wf(nodes, edges):
    return workflows.save({"name": "t", "nodes": nodes, "edges": edges})


def test_workflow_branches_with_decide_and_writes_note(monkeypatch, tmp_path):
    monkeypatch.setattr("nestcrew.router.get_provider",
                        lambda c=None, task="chat": FakeModel(["There is an ERROR in the log"]))
    wf = _wf([{"id": "t", "type": "trigger", "config": {"mode": "manual"}},
              {"id": "l", "type": "llm", "config": {"prompt": "check"}},
              {"id": "d", "type": "decide", "config": {"options": {
                  "problem": ["there is an error", "it crashed"], "fine": ["all good", "no issues"]}}},
              {"id": "bad", "type": "note", "config": {"save_to": "notes/bad.md", "template": "{input}"}},
              {"id": "good", "type": "note", "config": {"save_to": "notes/good.md", "template": "{input}"}}],
             [{"from": "t", "to": "l"}, {"from": "l", "to": "d"},
              {"from": "d", "to": "bad", "when": "problem"}, {"from": "d", "to": "good", "when": "fine"}])
    seen = []
    workflows.run(wf, gate=Gate(ask_fn=lambda r: "once"), on_event=seen.append)
    assert (tmp_path / "notes/bad.md").exists() and not (tmp_path / "notes/good.md").exists()
    assert {"id": "good", "status": "skipped", "type": "node"} in seen


def test_condition_node_and_template_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr("nestcrew.router.get_provider",
                        lambda c=None, task="chat": FakeModel(["PROBLEM: tax missing"]))
    wf = workflows.from_template("invoice-check")                     # uses save/llm/condition aliases
    assert {n["type"] for n in wf["nodes"]} >= {"trigger", "vision", "note", "llm", "condition"}
    assert workflows.trigger_of(wf) == {"watch": "inbox/invoices", "pattern": "*.png,*.jpg,*.jpeg"}


def test_loops_rejected_and_one_trigger_required():
    with pytest.raises(ValueError, match="loop"):
        _wf([{"id": "t", "type": "trigger", "config": {}}, {"id": "a", "type": "llm", "config": {}},
             {"id": "b", "type": "llm", "config": {}}],
            [{"from": "t", "to": "a"}, {"from": "a", "to": "b"}, {"from": "b", "to": "a"}])
    with pytest.raises(ValueError, match="Trigger"):
        _wf([{"id": "a", "type": "llm", "config": {}}], [])


def test_unattended_workflow_denies_unapproved_tools(monkeypatch, tmp_path):
    write = '{"action":"write_file","input":{"path":"w.txt","content":"x"}}'
    done = '{"action":"final","answer":"ok"}'
    monkeypatch.setattr("nestcrew.router.get_provider", lambda c=None, task="chat": FakeModel([write, done]))
    wf = _wf([{"id": "t", "type": "trigger", "config": {"mode": "every", "every_minutes": 5}},
              {"id": "a", "type": "agent", "config": {"prompt": "go", "tools": ["write_file"], "allow": []}}],
             [{"from": "t", "to": "a"}])
    workflows.run(wf)                                     # no human -> denied
    assert not (tmp_path / "w.txt").exists()
    wf["nodes"][1]["config"]["allow"] = ["write_file"]
    monkeypatch.setattr("nestcrew.router.get_provider", lambda c=None, task="chat": FakeModel([write, done]))
    workflows.run(workflows.save(wf))
    assert (tmp_path / "w.txt").exists()


def test_capture_trace_to_workflow_and_schedule(monkeypatch):
    _trace("c1", events=[{"type": "tool", "agent": "A", "tool": "write_file", "input": {"path": "a"},
                          "result": "Saved 1 characters"}])
    wf = workflows.from_trace(traces.get_trace("c1"))
    agent = next(n for n in wf["nodes"] if n["type"] == "agent")
    assert agent["config"]["allow"] == ["write_file"] and wf["enabled"] is False
    with pytest.raises(ValueError, match="manually"):
        workflows.set_enabled(wf["id"], True)
    wf["nodes"][0]["config"] = {"mode": "every", "every_minutes": 1}
    workflows.save(wf)
    workflows.set_enabled(wf["id"], True)
    from nestcrew import automations
    assert any(a["name"] == f"wf:{wf['id']}" and automations.is_enabled(a) for a in automations.load())


def test_daily_brief_template_runs_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/automation_log.md").write_text("- invoice reader failed once\n")
    monkeypatch.setattr("nestcrew.router.get_provider",
                        lambda c=None, task="chat": FakeModel(["1. Fix the invoice reader"]))
    wf = workflows.from_template("daily-brief")
    out = workflows.run(wf)                                  # unattended: write_file pre-approved by template
    assert "Fix the invoice reader" in out
    assert "Fix the invoice reader" in (tmp_path / "notes/daily_brief.md").read_text()
