"""Models page when Ollama is slow/stopped, 'Download all for this PC', and the background queue."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nestcrew import memory, models, server
from tests.test_core import offline  # noqa: F401

H = {"x-nestcrew-token": server.TOKEN}


class Resp:
    def __init__(self, tags):
        self.tags = tags

    def json(self):
        return {"models": [{"name": t} for t in self.tags]}


@pytest.fixture
def ollama(monkeypatch, tmp_path):
    state = {"up": True, "tags": ["qwen3.5:0.8b", "all-minilm:22m"]}

    def get(url, timeout=None, **kw):
        if not state["up"]:
            raise httpx.ConnectError("refused")
        return Resp(state["tags"])
    monkeypatch.setattr(models.httpx, "get", get)
    monkeypatch.setattr(models, "_tags_cache", lambda: tmp_path / "ollama_models.json")
    monkeypatch.setattr(memory, "resolve", lambda p: tmp_path / p.replace("./", ""))
    models.QUEUE.clear()
    return state


def by_id(rows):
    return {r["id"]: r for r in rows}


def test_models_dont_vanish_when_ollama_stops(ollama):
    assert by_id(models.status())["qwen3.5-0.8b"]["installed"] is True
    ollama["up"] = False                                       # Ollama busy / stopped
    rows = by_id(models.status())
    assert rows["qwen3.5-0.8b"]["installed"] is True and rows["all-minilm"]["installed"] is True
    assert models.OLLAMA["up"] is False


def test_models_page_reports_ollama_down(ollama):
    ollama["up"] = False
    c = TestClient(server.app, base_url="http://127.0.0.1")
    d = c.get("/api/models", headers=H).json()
    assert d["ollama_up"] is False and d["recommended"] == []


def test_recommended_skips_heavy_installed_and_non_ollama(ollama, monkeypatch):
    from nestcrew import hints
    monkeypatch.setattr(hints, "system_ram_gb", lambda: 8.0)
    rec = models.recommended_for_pc()
    assert "qwen3.5-0.8b" not in rec and "all-minilm" not in rec           # already there
    assert "jan-local" not in rec and "nanovlm-222m" not in rec             # not Ollama models
    assert "gemma4-e4b" not in rec                                         # heavy for 8 GB
    assert rec[0] == "smolvlm2-256m"                                       # smallest first


def test_queue_downloads_one_by_one_and_survives_errors(ollama, monkeypatch):
    order = []

    def pull(mid):
        order.append(mid)
        if mid == "moondream":
            from nestcrew.providers import ProviderError
            raise ProviderError("pull model manifest: file does not exist")
        yield {"status": "pulling", "percent": 50}
        yield {"status": "ready", "done": True}
    monkeypatch.setattr(models, "pull", pull)
    monkeypatch.setattr(models, "ensure_ollama", lambda wait=0: True)
    models.queue_pull(["smolvlm2-256m", "moondream", "lfm2-vl-450m"])
    for _ in range(100):
        if all(v["state"] in ("done", "error") for v in models.queue_state().values()):
            break
        time.sleep(0.02)
    q = models.queue_state()
    assert order == ["smolvlm2-256m", "moondream", "lfm2-vl-450m"]
    assert q["smolvlm2-256m"]["state"] == "done" and q["lfm2-vl-450m"]["state"] == "done"
    assert q["moondream"]["state"] == "error" and "does not exist" in q["moondream"]["msg"]


def test_queue_waits_for_ollama(ollama, monkeypatch):
    monkeypatch.setattr(models, "ensure_ollama", lambda wait=0: False)
    models.queue_pull(["smolvlm2-256m"])
    for _ in range(100):
        if models.queue_state()["smolvlm2-256m"]["state"] == "error":
            break
        time.sleep(0.02)
    assert "Start Ollama" in models.queue_state()["smolvlm2-256m"]["msg"]


def test_pull_all_endpoint(ollama, monkeypatch):
    got = []
    monkeypatch.setattr(models, "queue_pull", lambda ids: got.extend(ids) or {})
    c = TestClient(server.app, base_url="http://127.0.0.1")
    assert c.post("/api/models/pull_all").status_code in (401, 403)
    r = c.post("/api/models/pull_all", headers=H, json={})
    assert r.json()["queued"] == got and "smolvlm2-256m" in got


def test_start_ollama_endpoint(ollama, monkeypatch):
    monkeypatch.setattr(models, "ensure_ollama", lambda wait=0: False)
    c = TestClient(server.app, base_url="http://127.0.0.1")
    r = c.post("/api/ollama/start", headers=H, json={}).json()
    assert r["up"] is False and "ollama.com" in r["message"]
