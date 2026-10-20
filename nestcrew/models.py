"""Model catalog, downloads and per-task model switching.

The idea: one "brain" model plans and uses tools (agent/chat), and tiny "eyes"
models do the looking (screen, video, documents, images). Each job can use a
different model; switch them in the Models tab, with `nestcrew models use`, or
in models.yaml.
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import httpx
import yaml

from .config import ROOT, load_config, resolve
from .providers import OllamaProvider, OpenAICompatProvider, Provider, ProviderError

TASKS = ["chat", "agent", "screen", "video", "documents", "image"]
TASK_HELP = {
    "chat": "Normal chat (with images)",
    "agent": "Agents & crews (needs tool use)",
    "screen": "Reading your screen",
    "video": "Video frame analysis",
    "documents": "Invoices, forms, tables, charts",
    "image": "Quick image captions / tagging",
}


def _settings_path() -> Path:
    return resolve("./data/settings.json")


def catalog() -> dict:
    p = ROOT / "models.yaml"
    if not p.exists():
        return {"models": {}, "tasks": {}}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"models": {}, "tasks": {}}


def _settings() -> dict:
    p = _settings_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def task_map() -> dict:
    m = dict(catalog().get("tasks") or {})
    m.update(_settings().get("tasks") or {})
    return m


def assign(task: str, model_id: str) -> None:
    if task not in TASKS:
        raise ValueError(f"Unknown task '{task}'. Tasks: {', '.join(TASKS)}")
    models = catalog()["models"]
    if model_id not in models:
        raise ValueError(f"Unknown model '{model_id}'. See `nestcrew models`.")
    from .hints import assign_hint
    h = assign_hint(task, {"id": model_id, **models[model_id]})
    if h["level"] == "no":
        raise ValueError(f"{models[model_id]['name']}: {h['text']}"
                         + (" Pick one with tool use (e.g. gemma4-e4b, qwen3-1.7b, qwen3.5-0.8b)."
                            if task == "agent" else ""))
    s = _settings()
    s.setdefault("tasks", {})[task] = model_id
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _ollama() -> OllamaProvider:
    return OllamaProvider(load_config().get("local", {}))


def _installed_tags() -> set[str]:
    return set(_ollama().installed_models())


def is_installed(entry: dict, have: set[str] | None = None) -> bool:
    backend = entry.get("backend", "ollama")
    if backend == "nanovlm":                       # set up only if its repo is configured
        repo = (load_config().get("nanovlm") or {}).get("repo")
        return bool(repo) and Path(repo).exists()
    if backend != "ollama":                        # Jan / LM Studio / llama.cpp: is its server up?
        try:
            httpx.get(server_url(backend).rstrip("/") + "/models", timeout=0.5)
            return True
        except (httpx.HTTPError, ValueError):
            return False
    have = _installed_tags() if have is None else have
    tag = entry["tag"]
    return tag in have or f"{tag}:latest" in have or \
        (":" not in tag and any(h.startswith(tag + ":") for h in have))


def status() -> list[dict]:
    from .hints import assign_hint, fit, system_ram_gb
    have = _installed_tags()
    tm = task_map()
    ram = system_ram_gb()
    out = []
    for mid, e in catalog()["models"].items():
        out.append({"id": mid, **e, "installed": is_installed(e, have),
                    "fit": fit(str(e.get("ram", "")), ram),
                    "job_hints": {t: assign_hint(t, {"id": mid, **e}) for t in TASKS},
                    "used_for": [t for t, m in tm.items() if m == mid]})
    return out


# Other free local apps that expose an OpenAI-compatible server.
LOCAL_SERVERS = {
    "jan": "http://127.0.0.1:1337/v1",        # Jan (jan.ai): Settings > Local API Server > Start
    "lmstudio": "http://127.0.0.1:1234/v1",   # LM Studio: Developer tab > Start server
    "llamacpp": "http://127.0.0.1:8080/v1",   # llama.cpp: llama-server -hf <repo> --port 8080
}


def server_url(backend: str) -> str:
    cfg = load_config().get("local_servers", {}) or {}
    return cfg.get(backend, LOCAL_SERVERS.get(backend, ""))


# ---------------------------------------------------------------- providers --
class NanoVLMProvider(Provider):
    """Runs Hugging Face's nanoVLM `generate.py` (experimental, educational model)."""
    name = "nanovlm"

    def __init__(self, tag: str):
        cfg = load_config().get("nanovlm", {}) or {}
        self.model = tag
        self.repo = cfg.get("repo")
        self.python = cfg.get("python", "python")

    def chat(self, messages, json_mode=False):
        if not self.repo or not Path(self.repo).exists():
            raise ProviderError("nanoVLM isn't set up. Clone https://github.com/huggingface/nanoVLM, "
                                "install PyTorch, then set nanovlm.repo in config.yaml (see GUIDE.md).")
        last = next((m for m in reversed(messages) if m["role"] == "user"), None)
        if not last or not last.get("images"):
            raise ProviderError("nanoVLM only answers questions about an image.")
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "img.jpg"
            img.write_bytes(base64.b64decode(last["images"][0]))
            r = subprocess.run([self.python, "generate.py", "--image", str(img),
                                "--prompt", last["content"][:500]],
                               cwd=self.repo, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise ProviderError(f"nanoVLM failed: {r.stderr[-300:]}")
        return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def provider_for(model_id: str) -> Provider:
    e = catalog()["models"].get(model_id)
    if not e:
        raise ProviderError(f"Unknown model '{model_id}'")
    backend = e.get("backend", "ollama")
    if backend == "ollama":
        local = dict(load_config().get("local", {}))
        local.update(model=e["tag"], fallback_model=None)
        p = OllamaProvider(local)
        p.name = "local"
        return p
    if backend in LOCAL_SERVERS:
        p = OpenAICompatProvider(backend, "none", e["tag"], server_url(backend))
        return p
    if backend == "nanovlm":
        return NanoVLMProvider(e["tag"])
    raise ProviderError(f"Unknown backend '{backend}' for {model_id}")


def provider_for_task(task: str) -> Provider:
    """The model assigned to this job; falls back to your default local model
    (config.yaml local.model) if the assigned one isn't downloaded yet."""
    mid = task_map().get(task)
    e = catalog()["models"].get(mid) if mid else None
    if e and is_installed(e):
        return provider_for(mid)
    return _ollama()


# ---------------------------------------------------------- pull / remove ----
def pull(model_id: str) -> Iterator[dict]:
    e = catalog()["models"].get(model_id)
    if not e:
        raise ProviderError(f"Unknown model '{model_id}'")
    if e.get("backend") != "ollama":
        yield {"status": f"{e['name']} isn't downloaded by Ollama - see GUIDE.md for its setup.",
               "done": True}
        return
    base = _ollama().base
    try:
        with httpx.stream("POST", f"{base}/api/pull", json={"model": e["tag"], "stream": True},
                          timeout=httpx.Timeout(10, read=None)) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise ProviderError(d["error"])
                pct = round(100 * d["completed"] / d["total"]) if d.get("total") and d.get("completed") else None
                yield {"status": d.get("status", ""), "percent": pct}
    except httpx.ConnectError as err:
        raise ProviderError("Ollama is not running. Start it with: ollama serve") from err
    yield {"status": "ready", "done": True}


def remove(model_id: str) -> None:
    e = catalog()["models"].get(model_id)
    if not e or e.get("backend") != "ollama":
        raise ProviderError("Only Ollama models can be removed here.")
    r = httpx.request("DELETE", f"{_ollama().base}/api/delete", json={"model": e["tag"]}, timeout=30)
    if r.status_code not in (200, 404):
        raise ProviderError(f"Couldn't remove: {r.text[:200]}")


def set_system1_engine(name: str) -> None:
    from .system1 import SERVERS
    if name not in ("local", *SERVERS):
        raise ValueError("Engine must be local, " + ", ".join(SERVERS))
    s = _settings()
    s["system1_engine"] = name
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")
