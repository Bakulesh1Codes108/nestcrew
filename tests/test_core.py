"""Offline tests using a scripted fake model - no Ollama or internet needed."""
import json

import pytest

from nestcrew import tools
from nestcrew.agent import Agent, parse_json
from nestcrew.crew import run_crew
from nestcrew.providers import Provider, mime_of


class FakeModel(Provider):
    name, model = "fake", "scripted"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, json_mode=False):
        self.seen.append(messages)
        return self.replies.pop(0)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    from nestcrew.config import load_config
    cfg = load_config()
    monkeypatch.setitem(cfg, "memory", {"db": str(tmp_path / "mem.db")})
    monkeypatch.setitem(cfg, "safety", {**cfg.get("safety", {}),
                                        "audit_log": str(tmp_path / "audit.jsonl")})
    monkeypatch.setitem(cfg, "media", {**cfg.get("media", {}),
                                       "screenshots_dir": str(tmp_path / "shots")})
    monkeypatch.setattr("nestcrew.tools.is_online", lambda: False)
    monkeypatch.setattr("nestcrew.tools.workspace", lambda: tmp_path)


def test_calculator_safe():
    assert tools.calculator("12*(3+4)") == "84"
    with pytest.raises(ValueError):
        tools.calculator("__import__('os').system('echo hi')")


def test_agent_uses_tool_then_answers():
    m = FakeModel([
        json.dumps({"thought": "math", "action": "calculator", "input": {"expression": "4250*0.18"}}),
        json.dumps({"action": "final", "answer": "GST is 765.0"}),
    ])
    a = Agent(tools=["calculator"])
    assert a.run(m, "18% of 4250?") == "GST is 765.0"
    assert a.events[0]["tool"] == "calculator" and a.events[0]["result"] == "765.0"


def test_agent_accepts_plain_text_from_small_model():
    assert Agent().run(FakeModel(["Just a plain answer."]), "hi") == "Just a plain answer."


def test_offline_hides_internet_tools():
    names = [t.name for t in tools.available()]
    assert "web_search" not in names and "calculator" in names
    assert "offline" in tools.run_tool("web_search", {"query": "x"})


def test_file_tools_stay_in_workspace():
    tools.write_file("a.txt", "hello")
    assert tools.read_file("a.txt") == "hello"
    assert "outside" in tools.run_tool("read_file", {"path": "../../etc/passwd"})


def test_crew_passes_context():
    spec = {"name": "t", "agents": {"a": {"role": "A"}, "b": {"role": "B"}},
            "tasks": [{"agent": "a", "description": "research {topic}"},
                      {"agent": "b", "description": "write"}]}
    m = FakeModel(['{"action":"final","answer":"FACTS"}', '{"action":"final","answer":"REPORT"}'])
    res = run_crew(spec, {"topic": "tea"}, m)
    assert res["final"] == "REPORT"
    assert "research tea" in m.seen[0][1]["content"]
    assert "FACTS" in m.seen[1][1]["content"]


def test_helpers():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('sure! {"action":"final","answer":"x"} ok') == {"action": "final", "answer": "x"}
    assert mime_of("/9j/abc") == "image/jpeg"


# ------------------------------------------------ screenshots, lessons, video
def test_screen_tool_is_opt_in():
    assert "look_at_screen" not in [t.name for t in tools.available()]
    m = FakeModel(['{"action":"look_at_screen","input":{}}', '{"action":"final","answer":"no"}'])
    a = Agent()
    a.run(m, "what is on my screen?")
    assert "not allowed" in a.events[0]["result"]


def _fake_shot(tmp_path):
    from PIL import Image
    def shot():
        p = tmp_path / "shot.jpg"
        Image.new("RGB", (64, 64), "red").save(p)
        return p
    return shot


def test_screenshot_deleted_by_default(monkeypatch, tmp_path):
    from nestcrew import media
    monkeypatch.setattr(media, "take_screenshot", _fake_shot(tmp_path))
    m = FakeModel(['{"action":"look_at_screen","input":{"question":"colour?"}}',
                   "It is red.", '{"action":"final","answer":"Red screen"}'])
    from nestcrew.safety import Gate
    a = Agent(tools=["look_at_screen"])
    assert a.run(m, "what colour is my screen?", gate=Gate(ask_fn=lambda r: "once")) == "Red screen"
    assert "screenshot deleted" in a.events[0]["result"]
    assert not (tmp_path / "shot.jpg").exists()
    assert m.seen[1][1]["images"]  # the vision call really got the image


def test_screenshot_kept_when_asked(monkeypatch, tmp_path):
    from nestcrew import media
    monkeypatch.setattr(media, "take_screenshot", _fake_shot(tmp_path))
    res = media.look_at_screen(FakeModel(["red"]), "colour?", keep=True)
    assert res["saved_to"] and (tmp_path / "shot.jpg").exists()


def test_lessons_saved_recalled_and_rated():
    from nestcrew import memory
    m = FakeModel([
        '{"action":"calculator","input":{"expression":"4250*0.18"}}',
        '{"action":"final","answer":"765"}'])
    a = Agent(role="Accountant", tools=["calculator"])
    a.run(m, "calculate 18% GST on invoice amount 4250")
    assert a.last_lesson_id
    # a similar task later gets the past approach in its system prompt
    m2 = FakeModel(['{"action":"final","answer":"ok"}'])
    Agent(role="Accountant", tools=["calculator"]).run(m2, "calculate 18% GST on invoice amount 9900")
    assert "calculator(expression)" in m2.seen[0][0]["content"]
    memory.rate_lesson(a.last_lesson_id, True)
    assert memory.list_lessons()[-1]["status"] == "good"


def test_failed_runs_are_not_learned():
    m = FakeModel(['{"action":"read_file","input":{"path":"missing.txt"}}',
                   '{"action":"final","answer":"could not"}'])
    a = Agent(tools=["read_file"])
    a.run(m, "read missing file")
    assert a.last_lesson_id is None


def test_video_slices_analyses_and_charts(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from nestcrew import media
    path = tmp_path / "clip.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for i in range(100):  # 10 seconds
        vw.write(np.full((48, 64, 3), i * 2, dtype=np.uint8))
    vw.release()
    frames = media.extract_frames(path, every=2, max_frames=4)
    assert len(frames) == 4 and frames[0][0] == 0
    replies = [f'{{"description":"frame {i}","value":{i}}}' for i in range(4)] + ["Brightness rises."]
    res = media.analyze_frames(FakeModel(replies), frames, "how bright?")
    assert res["has_values"] and res["summary"] == "Brightness rises."
    out = media.save_outputs(res, "clip.mp4", tmp_path)
    assert open(out["csv"]).read().count("\n") == 5
    assert "<polyline" in open(out["chart"]).read()
