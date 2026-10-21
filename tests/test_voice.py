"""Voice commands: recording upload -> local Whisper -> text, setup, and safety limits.
The Whisper model itself is replaced by a stand-in (no 150 MB download in tests); the
audio decoding that runs before it is real."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nestcrew import memory, server, voice
from tests.test_core import offline  # noqa: F401

H = {"x-nestcrew-token": server.TOKEN}
TONE = Path(__file__).parent / "data" / "tone.webm"        # 2 s Opus/WebM, like the app window records


class Seg:
    def __init__(self, text):
        self.text = text


class Info:
    language, duration = "hi", 2.0


class FakeWhisper:
    def __init__(self, text=" Move all photos from Downloads to Pictures. "):
        self.text, self.calls = text, []

    def transcribe(self, path, **kw):
        self.calls.append((Path(path).suffix, Path(path).read_bytes()[:4], kw))
        return iter([Seg(self.text)]), Info()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "resolve", lambda p: tmp_path / p.replace("./", ""))
    return TestClient(server.app, base_url="http://127.0.0.1")


@pytest.fixture
def whisper(monkeypatch):
    fake = FakeWhisper()
    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice, "_load", lambda: fake)
    return fake


def test_transcribe_endpoint_returns_english_text(client, whisper):
    r = client.post("/api/voice/transcribe", headers=H, files={"audio": ("voice.webm", TONE.read_bytes(), "audio/webm")},
                    data={"output": "english"})
    assert r.status_code == 200
    assert r.json()["text"] == "Move all photos from Downloads to Pictures." and r.json()["language"] == "hi"
    suffix, head, kw = whisper.calls[0]
    assert suffix == ".webm" and head == TONE.read_bytes()[:4]          # the real recording reached Whisper
    assert kw["task"] == "translate" and kw["vad_filter"] is True        # any language -> English command


def test_keep_language_option(client, whisper):
    client.post("/api/voice/transcribe", headers=H, files={"audio": ("voice.webm", TONE.read_bytes())},
                data={"output": "same"})
    assert whisper.calls[0][2]["task"] == "transcribe"


def test_temp_recording_is_removed(client, whisper, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    client.post("/api/voice/transcribe", headers=H, files={"audio": ("voice.webm", TONE.read_bytes())})
    assert not list(tmp_path.glob("*.webm"))


def test_needs_the_app_token(client, whisper):
    assert client.post("/api/voice/transcribe", files={"audio": ("v.webm", TONE.read_bytes())}).status_code in (401, 403)


def test_too_long_recording_is_refused(client, whisper):
    r = client.post("/api/voice/transcribe", headers=H, files={"audio": ("v.webm", b"x" * (25 * 1024 * 1024 + 5))})
    assert r.status_code == 413


def test_silence_returns_empty_text(client, whisper):
    r = client.post("/api/voice/transcribe", headers=H, files={"audio": ("v.webm", b"tiny")})
    assert r.json()["text"] == "" and not whisper.calls


def test_not_set_up_gives_a_clear_message(client, monkeypatch):
    monkeypatch.setattr(voice, "available", lambda: False)
    r = client.post("/api/voice/transcribe", headers=H, files={"audio": ("v.webm", TONE.read_bytes())})
    assert r.status_code == 400 and "Enable offline voice" in r.json()["detail"]
    s = client.get("/api/voice", headers=H).json()
    assert s["engine"] == "none" and s["ready"] is False and s["model"] == "base"


def test_broken_audio_is_a_friendly_error(client, monkeypatch):
    class Boom:
        def transcribe(self, *a, **k):
            raise RuntimeError("Invalid data found when processing input")
    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice, "_load", lambda: Boom())
    r = client.post("/api/voice/transcribe", headers=H, files={"audio": ("v.webm", b"\x1aE\xdf\xa3" + b"0" * 2000)})
    assert r.status_code == 400 and "Try again" in r.json()["detail"]


def test_install_runs_in_background_and_reports(monkeypatch, client):
    steps = []
    monkeypatch.setattr(voice, "available", lambda: True)

    class FakeUtils:
        @staticmethod
        def download_model(name, local_files_only=False):
            steps.append((name, local_files_only))
            if local_files_only and len(steps) < 3:
                raise FileNotFoundError
            return "/models/" + name
    import sys
    import types
    fw = types.ModuleType("faster_whisper")
    fw.utils = FakeUtils
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", FakeUtils)
    assert client.get("/api/voice", headers=H).json()["ready"] is False
    client.post("/api/voice/install", headers=H, json={})
    for _ in range(50):
        if not voice.STATE["installing"]:
            break
        import time
        time.sleep(0.05)
    assert ("base", False) in steps and voice.STATE["install_msg"] == "Offline voice is ready."
    assert client.get("/api/voice", headers=H).json()["ready"] is True


def test_real_audio_decoding_of_an_app_recording():
    fw = pytest.importorskip("faster_whisper")
    audio = fw.decode_audio(str(TONE))
    assert 1.8 * 16000 < len(audio) < 2.2 * 16000          # 2 s at 16 kHz: what Whisper gets
