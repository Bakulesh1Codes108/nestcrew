"""Small 'thinking' models and sloppy JSON: found while testing on a real laptop."""
import pytest

from nestcrew.agent import Agent
from nestcrew.providers import OllamaProvider, ProviderError
from tests.test_core import FakeModel, offline  # noqa: F401  (offline = temp memory db)


def _thinker(replies):
    """An OllamaProvider whose stream returns scripted (thinking, content, budget-seen) turns."""
    p = OllamaProvider({"model": "qwen3-vl:2b"})
    p._pick_model = lambda: "qwen3-vl:2b"
    calls = []

    def fake_stream(messages, json_mode, budget=None):
        thinking, content = replies[len(calls)]
        calls.append(budget)
        p.last_thinking = thinking
        if content:
            yield content
    p._stream = fake_stream
    return p, calls


def test_thinking_model_gets_bigger_budget():
    p, calls = _thinker([("<think>long...", ""), ("", '{"action": "final", "answer": "ok"}')])
    assert p.chat([{"role": "user", "content": "hi"}], json_mode=True) == '{"action": "final", "answer": "ok"}'
    assert calls[0] is None and calls[1] >= 2048
    assert "qwen3-vl:2b" in p.thinkers


def test_answer_taken_from_thinking_as_last_resort():
    think = 'I should list it. {"action": "list_folder", "input": {"path": "Desktop/BIRAC"}}'
    p, _ = _thinker([(think, ""), (think, "")])
    out = p.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert '"list_folder"' in out


def test_instruct_twin_preferred(monkeypatch):
    p = OllamaProvider({"model": "qwen3-vl:2b"})
    monkeypatch.setattr(p, "installed_models", lambda: ["qwen3-vl:2b", "qwen3-vl:2b-instruct"])
    assert p._pick_model() == "qwen3-vl:2b-instruct"
    monkeypatch.setattr(p, "installed_models", lambda: ["qwen3-vl:2b"])
    assert p._pick_model() == "qwen3-vl:2b"


def test_args_next_to_action_are_accepted():
    model = FakeModel(['{"action": "calculator", "expression": "2+3"}',
                       '{"action": "final", "answer": "5"}'])
    a = Agent(tools=["calculator"])
    assert a.run(model, "2+3?") == "5"
    step = next(e for e in a.events if e["type"] == "tool")
    assert step["input"] == {"expression": "2+3"} and step["result"].strip() == "5"


def test_empty_reply_is_an_error_not_a_blank_answer():
    a = Agent(tools=["calculator"])
    with pytest.raises(ProviderError):
        a.run(FakeModel(["", ""]), "anything")
