"""'Ollama is not running' while Ollama was running: NestCrew must reach it directly (no proxy,
no IPv6 surprise), recover when it drops mid-task, and keep only one model in memory."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from nestcrew import models, providers
from nestcrew.providers import OLLAMA_DROPPED, OLLAMA_GONE, OllamaProvider, ProviderError, local_url


def test_localhost_becomes_127():
    assert local_url("http://localhost:11434") == "http://127.0.0.1:11434"
    assert local_url("http://localhost") == "http://127.0.0.1"
    assert local_url("http://localhost.example.com:1") == "http://localhost.example.com:1"
    assert OllamaProvider({"base_url": "http://localhost:11434"}).base == "http://127.0.0.1:11434"


class FakeOllama(BaseHTTPRequestHandler):
    """A tiny stand-in for Ollama's /api/chat, /api/tags and /api/generate."""
    log = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send(200, {"models": [{"name": "m"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        FakeOllama.log.append((self.path, body.get("model"), body.get("keep_alive")))
        if self.path == "/api/chat":
            self.send_response(200)
            self.end_headers()
            for part in ("Hello", " there"):
                self.wfile.write((json.dumps({"message": {"content": part}}) + "\n").encode())
            self.wfile.write(b'{"done": true}\n')
        else:
            self._send(200, {})

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ollama(monkeypatch):
    port = _free_port()
    srv = {"s": None}

    def up():
        srv["s"] = ThreadingHTTPServer(("127.0.0.1", port), FakeOllama)
        threading.Thread(target=srv["s"].serve_forever, daemon=True).start()

    def down():
        srv["s"].shutdown()
        srv["s"].server_close()
    FakeOllama.log = []
    providers._LOADED["model"] = None
    up()
    yield {"port": port, "up": up, "down": down}
    try:
        down()
    except Exception:
        pass


def prov(port, model="m"):
    p = OllamaProvider({"base_url": f"http://localhost:{port}", "model": model})
    p._pick_model = lambda: p.model
    return p


def test_real_server_answers(ollama):
    assert prov(ollama["port"]).chat([{"role": "user", "content": "hi"}]) == "Hello there"


def test_ollama_that_stopped_is_started_again_and_the_task_continues(ollama, monkeypatch):
    ollama["down"]()                                      # Ollama crashed / is restarting
    started = []
    monkeypatch.setattr(providers, "_recover_ollama", lambda: started.append(1) or ollama["up"]() or True)
    assert prov(ollama["port"]).chat([{"role": "user", "content": "hi"}]) == "Hello there"
    assert started == [1]


def test_ollama_gone_for_good_gives_clear_steps(ollama, monkeypatch):
    ollama["down"]()
    monkeypatch.setattr(providers, "_recover_ollama", lambda: False)
    with pytest.raises(ProviderError) as e:
        prov(ollama["port"]).chat([{"role": "user", "content": "hi"}])
    assert str(e.value) == OLLAMA_GONE and "ollama serve" not in str(e.value)


def test_drop_after_part_of_the_answer_says_retry(monkeypatch):
    class R:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_lines(self):
            yield json.dumps({"message": {"content": "Half an ans"}})
            raise httpx.RemoteProtocolError("peer closed connection")
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: R())
    monkeypatch.setattr(providers, "_recover_ollama", lambda: True)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: None)
    p = OllamaProvider({"model": "m"})
    p._pick_model = lambda: "m"
    with pytest.raises(ProviderError) as e:
        list(p.stream([{"role": "user", "content": "x"}]))
    assert str(e.value) == OLLAMA_DROPPED


def test_proxy_settings_are_ignored_for_ollama(monkeypatch):
    seen = []

    def stream(*a, **k):
        seen.append(k.get("trust_env"))
        raise httpx.ReadTimeout("x")
    monkeypatch.setattr(httpx, "stream", stream)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: seen.append(("post", k.get("trust_env"))))
    p = OllamaProvider({"model": "m"})
    p._pick_model = lambda: "m"
    with pytest.raises(ProviderError):
        p.chat([{"role": "user", "content": "x"}])
    assert seen and all(x is False or x == ("post", False) for x in seen)


def test_vpn_proxy_env_does_not_break_local_calls(ollama, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")         # a dead proxy, like a closed VPN app
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    assert prov(ollama["port"]).chat([{"role": "user", "content": "hi"}]) == "Hello there"


def test_only_one_model_kept_in_memory(ollama):
    prov(ollama["port"], "big-vision").chat([{"role": "user", "content": "a"}])
    prov(ollama["port"], "small-text").chat([{"role": "user", "content": "b"}])
    unloads = [x for x in FakeOllama.log if x[0] == "/api/generate"]
    assert unloads == [("/api/generate", "big-vision", 0)]


def test_running_but_busy_ollama_is_waited_for_not_started_twice(monkeypatch):
    state = {"n": 0}

    def tags(timeout=6.0):
        state["n"] += 1
        return (state["n"] >= 3, set())
    started = []
    monkeypatch.setattr(models, "ollama_tags", tags)
    monkeypatch.setattr(models, "_port_open", lambda: True)          # port held = Ollama is there
    monkeypatch.setattr(models.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert models.ensure_ollama(wait=5) is True and started == []


def test_frozen_ollama_is_reported_for_restart(monkeypatch):
    monkeypatch.setattr(models, "ollama_tags", lambda timeout=6.0: (False, set()))
    monkeypatch.setattr(models, "_port_open", lambda: True)
    monkeypatch.setattr(models.time if hasattr(models, "time") else __import__("time"), "sleep", lambda s: None)
    assert models.ensure_ollama(wait=0) is False and models.OLLAMA["stuck"] is True


def test_stopped_ollama_is_started(monkeypatch):
    calls = {"n": 0}

    def tags(timeout=6.0):
        calls["n"] += 1
        return (calls["n"] > 2, set())
    started = []
    monkeypatch.setattr(models, "ollama_tags", tags)
    monkeypatch.setattr(models, "_port_open", lambda: False)
    monkeypatch.setattr(models, "_ollama_exe", lambda: "ollama")
    monkeypatch.setattr(models.subprocess, "Popen", lambda *a, **k: started.append(a[0]))
    assert models.ensure_ollama(wait=5) is True and started == [["ollama", "serve"]]
