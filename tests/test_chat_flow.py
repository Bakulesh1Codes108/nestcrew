"""The 'second chat doesn't work' fixes: follow-ups keep the photo, messages are saved
even when the model fails, and thinking-models get think:false (with a retry)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from nestcrew import memory, server
from nestcrew.providers import OllamaProvider, Provider, ProviderError
from tests.test_core import offline  # noqa: F401

H = {"x-nestcrew-token": server.TOKEN}


class Vision(Provider):
    name, model = "local", "vision"

    def __init__(self, fail=False):
        self.seen, self.fail = [], fail

    def stream(self, messages):
        self.seen.append(messages)
        if self.fail:
            raise ProviderError("model crashed")
        yield "A photo " if messages[-1].get("images") else "No photo "
        yield "answer."


def _events(t):
    return [json.loads(l[6:]) for l in t.split("\n\n") if l.startswith("data: ")]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "resolve", lambda p: tmp_path / p.replace("./", ""))
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_second_message_and_follow_up_keep_the_photo(client, monkeypatch):
    v = Vision()
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: v)
    e1 = _events(client.post("/api/chat", headers=H, json={"message": "identify this", "images": ["/9j/AAAA"],
                                                          "session": "s1"}).text)
    e2 = _events(client.post("/api/chat", headers=H, json={"message": "what are they wearing?", "session": "s1"}).text)
    assert e1[-1]["type"] == "done" and e2[-1]["type"] == "done"
    assert v.seen[1][-1]["images"] == ["/9j/AAAA"]                  # follow-up re-sent the photo
    assert [m["role"] for m in v.seen[1]][-3:] == ["user", "assistant", "user"]   # history included
    got = client.get("/api/session/s1", headers=H).json()
    assert len(got["messages"]) == 4 and got["image"] == "/9j/AAAA"


def test_new_chat_does_not_reuse_old_photo(client, monkeypatch):
    v = Vision()
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: v)
    client.post("/api/chat", headers=H, json={"message": "x", "images": ["/9j/AAAA"], "session": "a"})
    client.post("/api/chat", headers=H, json={"message": "hello", "session": "b"})
    assert not v.seen[1][-1]["images"]


def test_failure_still_saves_question_and_reports_error(client, monkeypatch):
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: Vision(fail=True))
    ev = _events(client.post("/api/chat", headers=H, json={"message": "second question", "session": "s2"}).text)
    assert ev[0]["type"] == "status" and ev[-1] == {"type": "error", "text": "model crashed"}
    assert client.get("/api/session/s2", headers=H).json()["messages"][0]["content"] == "second question"


def test_last_chats_listed_and_deletable(client, monkeypatch):
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: Vision())
    for i in range(6):
        client.post("/api/chat", headers=H, json={"message": f"chat {i}", "session": f"c{i}"})
    chats = [x for x in client.get("/api/sessions", headers=H).json() if x["kind"] == "chat"]
    assert [c["title"] for c in chats[:5]] == ["chat 5", "chat 4", "chat 3", "chat 2", "chat 1"]
    client.post("/api/session/c5/delete", headers=H)
    assert "c5" not in [x["id"] for x in client.get("/api/sessions", headers=H).json()]


class _R:
    def __init__(self, code, lines=(), text=""):
        self.status_code, self._lines, self.text = code, lines, text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.text.encode()

    def iter_lines(self):
        return iter(self._lines)


def test_think_false_sent_and_retried_without_it(monkeypatch):
    bodies = []

    def fake_stream(method, url, json=None, timeout=None, **kw):
        bodies.append(dict(json))
        if "think" in json:
            return _R(400, text='{"error":"model does not support thinking"}')
        return _R(200, ['{"message":{"content":"hi"}}', '{"done":true}'])
    monkeypatch.setattr(httpx, "stream", fake_stream)
    p = OllamaProvider({"model": "m"})
    monkeypatch.setattr(p, "_pick_model", lambda: "m")
    assert "".join(p.stream([{"role": "user", "content": "x"}])) == "hi"
    assert bodies[0]["think"] is False and "think" not in bodies[1]
    assert "".join(p.stream([{"role": "user", "content": "x"}])) == "hi" and "think" not in bodies[2]


def test_timeout_becomes_friendly_error(monkeypatch):
    def slow(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(httpx, "stream", slow)
    p = OllamaProvider({"model": "m"})
    monkeypatch.setattr(p, "_pick_model", lambda: "m")
    with pytest.raises(ProviderError, match="too long"):
        list(p.stream([{"role": "user", "content": "x"}]))
