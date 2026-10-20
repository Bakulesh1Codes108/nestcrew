"""Per-task model switching and automations (no Ollama needed)."""
import datetime as dt
import json
import os
import time

import pytest

from nestcrew import automations, models
from tests.test_core import FakeModel, offline  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(automations, "_state_path", lambda: tmp_path / "auto_state.json")
    monkeypatch.setattr(models, "_installed_tags",
                        lambda: {"gemma4:e4b", "hf.co/ggml-org/SmolVLM2-256M-Video-Instruct-GGUF:Q8_0"})


def test_catalog_has_ultra_small_models():
    ids = set(models.catalog()["models"])
    assert {"smolvlm2-256m", "smolvlm2-500m", "nanovlm-222m", "moondream", "lfm2-vl-450m"} <= ids
    for m in models.catalog()["models"].values():
        assert {"params", "download", "ram", "best_for", "backend", "tag"} <= set(m)


def test_assign_and_route_per_task():
    models.assign("screen", "smolvlm2-256m")
    assert models.task_map()["screen"] == "smolvlm2-256m"
    p = models.provider_for_task("screen")
    assert p.model.endswith("SmolVLM2-256M-Video-Instruct-GGUF:Q8_0")


def test_agent_job_rejects_models_without_tools():
    with pytest.raises(ValueError, match="(?i)can't use tools"):
        models.assign("agent", "smolvlm2-256m")
    models.assign("agent", "qwen3-1.7b")


def test_missing_model_falls_back_to_default():
    models.assign("documents", "granite-vision-2b")          # not "downloaded"
    p = models.provider_for_task("documents")
    assert p.model == "gemma4:e4b"                            # config local.model


def test_router_accepts_model_id():
    from nestcrew.router import get_provider
    assert get_provider("moondream").model == "moondream:1.8b"


def test_status_marks_installed():
    st = {m["id"]: m for m in models.status()}
    assert st["smolvlm2-256m"]["installed"] and not st["moondream"]["installed"]


# ------------------------------------------------------------ automations
def _auto(**kw):
    a = {"name": "T", "trigger": {"watch": "inbox/t", "pattern": "*.png"},
         "action": {"type": "describe_image", "task": "screen", "prompt": "what?", "save_to": "notes/t.md"}}
    a.update(kw)
    return a


def _png(path):
    from PIL import Image
    Image.new("RGB", (32, 32), "blue").save(path)
    old = time.time() - 10
    os.utime(path, (old, old))


def test_watch_only_new_files_after_enable(monkeypatch, tmp_path):
    a = _auto()
    monkeypatch.setattr(automations, "load", lambda: [a])
    (tmp_path / "inbox/t").mkdir(parents=True)
    _png(tmp_path / "inbox/t/old.png")
    automations.set_enabled("T", True)                       # existing file is ignored
    assert automations.due_jobs(a) == []
    _png(tmp_path / "inbox/t/new.png")
    (tmp_path / "inbox/t/skip.txt").write_text("x")
    old = time.time() - 10
    os.utime(tmp_path / "inbox/t/skip.txt", (old, old))
    assert [p.name for p in automations.due_jobs(a)] == ["new.png"]


def test_describe_image_automation_writes_notes(monkeypatch, tmp_path):
    a = _auto()
    monkeypatch.setattr(automations, "load", lambda: [a])
    seen_task = {}

    def fake_provider(choice=None, task="chat"):
        seen_task["task"] = task
        return FakeModel(["A blue square."])
    monkeypatch.setattr("nestcrew.router.get_provider", fake_provider)
    automations.set_enabled("T", True)
    _png(tmp_path / "inbox/t/shot.png")
    out = automations.Runner().tick()
    assert out and "notes/t.md" in out[0] and seen_task["task"] == "screen"
    assert "A blue square." in (tmp_path / "notes/t.md").read_text()
    assert automations.Runner().tick() == []                 # not processed twice
    assert automations.status()[0]["last_result"]


def test_unattended_agent_cannot_use_unlisted_risky_tools(monkeypatch, tmp_path):
    write = '{"action":"write_file","input":{"path":"x.txt","content":"hi"}}'
    done = '{"action":"final","answer":"ok"}'
    a = {"name": "W", "trigger": {"every_minutes": 1},
         "action": {"type": "agent", "tools": ["write_file"], "prompt": "write"}}
    monkeypatch.setattr("nestcrew.router.get_provider", lambda c=None, task="chat": FakeModel([write, done]))
    automations.run_once(a)
    assert not (tmp_path / "x.txt").exists()                 # denied: not in allow
    a["allow"] = ["write_file"]
    monkeypatch.setattr("nestcrew.router.get_provider", lambda c=None, task="chat": FakeModel([write, done]))
    automations.run_once(a)
    assert (tmp_path / "x.txt").exists()


def test_daily_and_interval_triggers(monkeypatch):
    d = {"name": "D", "trigger": {"daily": "09:00"}, "action": {}}
    monkeypatch.setattr(automations, "load", lambda: [d])
    assert automations.due_jobs(d, dt.datetime(2026, 10, 19, 8, 59)) == []
    assert automations.due_jobs(d, dt.datetime(2026, 10, 19, 9, 1)) == [None]
    e = {"name": "E", "trigger": {"every_minutes": 60}, "action": {}}
    assert automations.due_jobs(e) == [None]


def test_shipped_automations_are_off_and_valid():
    for a in automations.load():
        assert a.get("enabled") is False
        assert a["action"]["type"] in ("describe_image", "video", "agent", "crew")
        assert "run_command" not in (a.get("allow") or [])
