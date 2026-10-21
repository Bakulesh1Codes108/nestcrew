"""Voice commands: speech -> text on this PC with a small Whisper model (faster-whisper).

The chat bar's 🎤 records a few seconds of audio in the app window and sends it here; nothing
leaves the PC. Whisper understands ~100 languages; with "english" (the default) anything you
say in Hindi, Marathi, Tamil... comes back as an English sentence, so it works as a command.

First use downloads the model once (base ≈ 150 MB, small ≈ 480 MB) - "Enable offline voice"
in the app does it. Until then the app can fall back to the browser's own (online) speech
recognition.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time

from .config import load_config
from .safety import audit

MODELS = {"tiny": "~75 MB, fastest, rough", "base": "~150 MB, good on any laptop (default)",
          "small": "~480 MB, best for accents and Indian languages", "medium": "~1.5 GB, slow on CPU"}
STATE: dict = {"model": None, "name": None, "installing": False, "install_msg": "", "last_used": 0.0}
LOCK = threading.Lock()


def _cfg() -> dict:
    c = load_config().get("voice", {}) or {}
    return {"model": str(c.get("model", "base")), "output": str(c.get("output", "english")),
            "language": c.get("language") or None, "max_seconds": int(c.get("max_seconds", 60))}


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def model_ready(name: str | None = None) -> bool:
    """Is the Whisper model already on disk (no download needed)?"""
    if not available():
        return False
    try:
        from faster_whisper.utils import download_model
        download_model(name or _cfg()["model"], local_files_only=True)
        return True
    except Exception:
        return False


def status() -> dict:
    cfg = _cfg()
    ready = model_ready(cfg["model"])
    return {"engine": "whisper" if ready else "none", "library": available(), "model": cfg["model"],
            "model_info": MODELS.get(cfg["model"], ""), "ready": ready, "output": cfg["output"],
            "installing": STATE["installing"], "install_msg": STATE["install_msg"], "loaded": STATE["model"] is not None}


def _load():
    name = _cfg()["model"]
    with LOCK:
        if STATE["model"] is None or STATE["name"] != name:
            from faster_whisper import WhisperModel
            threads = max(1, min(8, (os.cpu_count() or 4) - 1))
            STATE["model"] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
            STATE["name"] = name
    return STATE["model"]


def transcribe(audio: bytes, suffix: str = ".webm", output: str | None = None) -> dict:
    """Audio bytes (webm/ogg/wav/m4a) -> {"text", "language", "seconds"}."""
    if not available():
        raise ValueError("Offline voice isn't set up yet - press 'Enable offline voice' (one download).")
    if len(audio) < 800:
        return {"text": "", "language": "", "seconds": 0.0, "took": 0.0}
    cfg = _cfg()
    out = (output or cfg["output"]).lower()
    t0 = time.time()
    model = _load()
    fd, path = tempfile.mkstemp(suffix=suffix if suffix.startswith(".") else "." + suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        segments, info = model.transcribe(
            path, task="translate" if out == "english" else "transcribe", language=cfg["language"],
            beam_size=1, vad_filter=True, condition_on_previous_text=False,
            initial_prompt="Commands for a file assistant: copy, move, clean, organize, Downloads, Desktop, "
                           "Documents, Pictures, phone, Excel, folder, BIRAC.")
        text = " ".join(s.text.strip() for s in segments).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    STATE["last_used"] = time.time()
    took = time.time() - t0
    audit(event="voice", language=info.language, seconds=round(info.duration, 1), took=round(took, 1),
          chars=len(text))
    return {"text": text, "language": info.language, "seconds": round(info.duration, 1), "took": round(took, 1)}


def install() -> None:
    """Background: pip install faster-whisper, then download the model once."""
    if STATE["installing"]:
        return

    def work():
        STATE.update(installing=True, install_msg="Installing the speech library…")
        try:
            if not available():
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "faster-whisper>=1.0", "av>=11,<19"],
                                   capture_output=True, text=True,
                                   creationflags=0x08000000 if sys.platform == "win32" else 0)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip()[-300:] or "pip failed")
                importlib.invalidate_caches()
            name = _cfg()["model"]
            STATE["install_msg"] = f"Downloading the '{name}' speech model ({MODELS.get(name, '')})…"
            from faster_whisper.utils import download_model
            download_model(name)
            STATE["install_msg"] = "Offline voice is ready."
            audit(event="voice_installed", model=name)
        except Exception as e:                                    # shown in the app
            STATE["install_msg"] = f"Couldn't set up offline voice: {e}"
            audit(event="voice_install_failed", error=str(e)[:300])
        finally:
            STATE["installing"] = False

    threading.Thread(target=work, daemon=True).start()
