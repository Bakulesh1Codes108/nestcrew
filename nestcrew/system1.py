"""System 1: fast, typed decisions - no text generation.

Inspired by "System One" decision models like TypeSafe's Jev (proprietary,
hosted-only). This is an offline, open-source take on the same idea using a tiny
local embedding model (all-minilm, 46 MB) through Ollama:

    choice(text, options)  -> one label from a set you define + probabilities
    yes_no(text, question) -> probability that the answer is yes
    score(text, low, high) -> 0..1 on a scale you describe
    similar(text, items)   -> most similar past items (for recall)

Every answer carries a confidence, so callers can act automatically when it is
high and fall back to the LLM ("System 2") or ask you when it is low.
If the embedding model isn't downloaded, it falls back to keyword matching.
You can teach it: corrections are stored and used as extra examples.
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path

import httpx

from .config import load_config, resolve

_cache: dict[str, list[float]] = {}
_lock = threading.Lock()
_status = {"ok": None, "checked": 0.0}


def _cfg() -> dict:
    return load_config().get("system1", {}) or {}


def model_name() -> str:
    return _cfg().get("embed_model", "all-minilm:22m")


def _examples_path() -> Path:
    return resolve("./data/system1_examples.json")


def taught() -> dict:
    try:
        return json.loads(_examples_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def teach(group: str, text: str, label: str) -> None:
    """Remember that `text` belongs to `label` in decision `group`."""
    data = taught()
    data.setdefault(group, {}).setdefault(label, [])
    if text not in data[group][label]:
        data[group][label].append(text[:500])
    p = _examples_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- embedding --
def embed(texts: list[str]) -> list[list[float]] | None:
    """Embeddings from Ollama, cached. None if unavailable (-> keyword fallback)."""
    import time
    if _status["ok"] is False and time.time() - _status["checked"] < 60:
        return None
    todo = [t for t in dict.fromkeys(texts) if t not in _cache]
    if todo:
        from .providers import local_url
        base = local_url(load_config().get("local", {}).get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        try:
            r = httpx.post(f"{base}/api/embed", trust_env=False, json={"model": model_name(), "input": todo,
                                                      "keep_alive": "30m"}, timeout=20)
            vecs = r.json().get("embeddings") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            vecs = None
        if not vecs or len(vecs) != len(todo):
            _status.update(ok=False, checked=time.time())
            return None
        with _lock:
            for t, v in zip(todo, vecs):
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                _cache[t] = [x / n for x in v]
    _status.update(ok=True, checked=time.time())
    return [_cache[t] for t in texts]


def available() -> bool:
    if engine() in LOCAL_ENGINES and server_up(engine()):
        return True
    return embed(["ping"]) is not None


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _softmax(scores: dict[str, float], temp: float) -> dict[str, float]:
    m = max(scores.values())
    ex = {k: math.exp((v - m) / temp) for k, v in scores.items()}
    tot = sum(ex.values())
    return {k: v / tot for k, v in ex.items()}


_WORD = re.compile(r"[a-z0-9%]+")


def _words(t: str) -> set[str]:
    return set(_WORD.findall(t.lower()))


# ------------------------------------- open "System One" servers (/v1/systemone) --
# Laya, Kev, OpenJev (wev serve) and cribl-decision servers all speak the same
# protocol as TypeSafe's Jev: {"state", "model", "questions": {name: {type,
# instructions, criteria}}} -> {"answers": {name: {...}}}. They run on this PC.
SERVERS = {
    "laya": {"url": "http://127.0.0.1:8000", "model": "typed-decisions",
             "label": "Laya 421M (CPU, ~1 GB RAM)"},
    "kev": {"url": "http://127.0.0.1:8009", "model": "kev-latest",
            "label": "Kev 0.8B / 4B / 9B (Qwen3.5)"},
    "openjev": {"url": "http://127.0.0.1:8009", "model": "alanhuangya/OpenJev-8B",
                "label": "OpenJev 8B (GPU)"},
    "cribl": {"url": "http://127.0.0.1:8010", "model": "cribl-decision-1.0",
              "label": "cribl-decision-1.0 (Qwen3.5-4B, GPU)"},
}
LOCAL_ENGINES = set(SERVERS)


def server_conf(name: str) -> dict:
    conf = dict(SERVERS.get(name, {}))
    conf.update((_cfg().get("servers") or {}).get(name) or {})
    return conf


def server_up(name: str) -> bool:
    c = server_conf(name)
    if not c.get("url"):
        return False
    try:
        r = httpx.get(c["url"].rstrip("/") + "/health", timeout=1.0, trust_env=False)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def _systemone(name: str, text: str, qname: str, question: dict) -> dict | None:
    from .safety import audit
    c = server_conf(name)
    body = {"state": text, "model": c.get("model", ""), "questions": {qname: question}}
    headers = {"Authorization": f"Bearer {c['api_key']}"} if c.get("api_key") else {}
    try:
        r = httpx.post(c["url"].rstrip("/") + "/v1/systemone", json=body, timeout=30, headers=headers, trust_env=False)
        if r.status_code != 200:
            audit(event="systemone_error", engine=name, status=r.status_code, detail=r.text[:200])
            return None
        answers = r.json().get("answers") or {}
        ans = answers.get(qname) if isinstance(answers, dict) else (answers[0] if answers else None)
        return ans if isinstance(ans, dict) else None
    except (httpx.HTTPError, ValueError) as e:
        audit(event="systemone_error", engine=name, detail=str(e)[:200])
        return None


def _probs_from(ans: dict, labels: list[str]) -> dict:
    p = ans.get("probabilities") or ans.get("probs") or ans.get("distribution") or {}
    if isinstance(p, list):
        p = {str(x.get("value", x.get("label"))): float(x.get("probability", 0)) for x in p if isinstance(x, dict)}
    out = {k: round(float(v), 3) for k, v in p.items() if k in labels}
    return out


def engine() -> str:
    """'local' (MiniLM) | 'laya' | 'kev' | 'openjev' | 'cribl' - all free and on this PC."""
    try:
        from .models import _settings
        e = _settings().get("system1_engine")
    except Exception:
        e = None
    e = e or _cfg().get("engine", "laya")
    return e if e in ("local", *SERVERS) else "local"


# ---------------------------------------------------------------- primitives --
def low_ram() -> bool:
    """Under 12 GB: keep the big helpers out of the typing path so the laptop stays responsive."""
    try:
        from .hints import system_ram_gb
        r = system_ram_gb()
        return bool(r) and r < 12
    except Exception:
        return False


def choice(text: str, options: dict[str, dict], group: str | None = None,
           images: list[str] | None = None, instructions: str = "", prefer_online: bool = False,
           quick: bool = False) -> dict:
    """options: {label: {"examples": [...], "keywords": [...], "description": "..."}}.
    Returns {"label", "confidence", "probs", "method"}.
    Everything runs on this PC; `images` / `prefer_online` are accepted for compatibility and ignored."""
    eng = engine()
    if eng in LOCAL_ENGINES and not (quick and low_ram()):   # open System One model on this PC
        q = {"type": "choice", "instructions": instructions or "Which option best fits the input?",
             "criteria": {lab: (o.get("description") or "; ".join(o.get("examples", [])[:4]) or lab)[:400]
                          for lab, o in options.items()}}
        ans = _systemone(eng, text, group or "decision", q)
        if ans and ans.get("choice") in options:
            probs = _probs_from(ans, list(options)) or {ans["choice"]: float(ans.get("confidence", 1))}
            return {"label": ans["choice"], "confidence": round(float(ans.get("confidence", probs.get(ans["choice"], 0))), 3),
                    "probs": dict(sorted(probs.items(), key=lambda x: -x[1])), "method": eng}
    extra = taught().get(group, {}) if group else {}
    ex = {lab: list(o.get("examples", [])) + extra.get(lab, []) for lab, o in options.items()}
    flat = [e for lab in ex for e in ex[lab]]
    vecs = embed([text] + flat) if flat else None
    if vecs:
        q, rest, scores, i = vecs[0], vecs[1:], {}, 0
        for lab, exs in ex.items():
            sims = [_cos(q, rest[i + j]) for j in range(len(exs))]
            i += len(exs)
            top = sorted(sims, reverse=True)[:2] or [0.0]
            scores[lab] = sum(top) / len(top) + 0.02 * (float(options[lab].get("weight", 1.0)) - 1)
        probs = _softmax(scores, temp=float(_cfg().get("temperature", 0.04)))
        method = "embedding"
    else:
        w = _words(text)
        low = text.lower()
        scores = {}
        for lab, o in options.items():
            kws = o.get("keywords", [])
            hits = sum(1 for k in kws if (k in w if " " not in k else k in low))
            hits += sum(0.5 for t in extra.get(lab, []) if len(_words(t) & w) >= 2)
            scores[lab] = hits * float(o.get("weight", 1.0))
        if max(scores.values()) == 0:
            probs = {k: 1 / len(scores) for k in scores}
        else:
            probs = _softmax(scores, temp=0.6)
        method = "keywords"
    label = max(probs, key=probs.get)
    return {"label": label, "confidence": round(probs[label], 3),
            "probs": {k: round(v, 3) for k, v in sorted(probs.items(), key=lambda x: -x[1])},
            "method": method}


def yes_no(text: str, yes_examples: list[str], no_examples: list[str], question: str = "",
           images: list[str] | None = None, prefer_online: bool = False) -> float:
    """Probability the answer is yes."""
    if question and engine() in LOCAL_ENGINES:
        ans = _systemone(engine(), text, "yes_no", {"type": "noul", "instructions": question})
        if ans and "noul" in ans:
            return round(float(ans["noul"]), 3)
    r = choice(text, {"yes": {"examples": yes_examples, "keywords": _kw(yes_examples)},
                      "no": {"examples": no_examples, "keywords": _kw(no_examples)}})
    return r["probs"]["yes"]


def score(text: str, low: str, high: str, levels: list[dict] | None = None,
          instructions: str = "", prefer_online: bool = False) -> float:
    """Where `text` sits between two described ends of a scale (0..1)."""
    if levels and engine() in LOCAL_ENGINES:
        ans = _systemone(engine(), text, "score", {"type": "score", "instructions": instructions or "Rate the input.",
                                                   "criteria": [lv.get("description") or lv.get("label") for lv in levels]})
        if ans and "score" in ans:
            return round(float(ans["score"]) / max(1, len(levels) - 1), 3)
    v = embed([text, low, high])
    if not v:
        return 0.5
    a, b = _cos(v[0], v[1]), _cos(v[0], v[2])
    return round(_softmax({"low": a, "high": b}, 0.05)["high"], 3)


def similar(text: str, items: list[str], k: int = 3, min_sim: float = 0.55) -> list[tuple[int, float]]:
    """Indices of the most similar items (embedding cosine); keyword Jaccard fallback."""
    if not items:
        return []
    v = embed([text] + items)
    if v:
        sims = [(i, _cos(v[0], e)) for i, e in enumerate(v[1:])]
    else:
        w = _words(text)
        sims = [(i, len(w & _words(t)) / (len(w | _words(t)) or 1)) for i, t in enumerate(items)]
        min_sim = 0.2
    sims = [s for s in sims if s[1] >= min_sim]
    return sorted(sims, key=lambda s: -s[1])[:k]


def _kw(examples: list[str]) -> list[str]:
    stop = {"the", "a", "an", "is", "my", "this", "that", "to", "of", "and", "in", "on", "for", "it", "me"}
    return sorted({w for e in examples for w in _words(e) if w not in stop and len(w) > 2})


# ------------------------------------------------------------ Laya on this PC --
def _scripts_dir():
    import sys
    from pathlib import Path
    return Path(sys.executable).parent


def laya_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("laya") is not None


_laya_proc = {"p": None}


def start_laya() -> str:
    """Start laya-serve bound to 127.0.0.1 (never the network), typed-decisions only."""
    import os
    import shutil
    import subprocess
    if server_up("laya"):
        return "already running"
    if not laya_installed():
        return "not installed"
    exe = shutil.which("laya-serve", path=str(_scripts_dir())) or shutil.which("laya-serve")
    if not exe:
        return "laya-serve not found"
    from urllib.parse import urlparse
    port = urlparse(server_conf("laya")["url"]).port or 8000
    env = {**os.environ, "LAYA_HOST": "127.0.0.1", "LAYA_PORT": str(port),
           "LAYA_MODELS": _cfg().get("laya_models", "typed-decisions"), "LAYA_DEVICE": "cpu",
           "LAYA_MAX_LOADED": "1", "LAYA_MAX_CONCURRENT": "2"}
    if low_ram():                                   # be gentle on 8 GB laptops
        env.update(LAYA_THREADS="2", LAYA_IDLE_UNLOAD_SECONDS="300", LAYA_PRELOAD="0")
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
    _laya_proc["p"] = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=flags)
    return "starting"


def stop_laya() -> None:
    p = _laya_proc.get("p")
    if p and p.poll() is None:
        p.terminate()
