"""One chat interface for every model backend.

Message format used everywhere in NestCrew:
    {"role": "system"|"user"|"assistant", "content": "text", "images": [base64, ...]}

Backends:
    OllamaProvider          - local model on your laptop (default, works offline)
    AnthropicProvider       - Claude API
    GeminiProvider          - Google Gemini API
    OpenAICompatProvider    - xAI Grok, OpenAI, or any OpenAI-compatible server
"""
from __future__ import annotations

import contextvars
import json
import threading
from typing import Iterator

import httpx

TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)


class ProviderError(RuntimeError):
    pass


# Set by the agent to its Stop switch, so a model call in progress can be cut off.
CANCEL: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("nestcrew_cancel", default=None)


class Cancelled(ProviderError):
    pass


def _ram_gb():
    try:
        from .hints import system_ram_gb
        return system_ram_gb()
    except Exception:
        return None


def mime_of(b64: str) -> str:
    if b64.startswith("iVBOR"):
        return "image/png"
    if b64.startswith("/9j/"):
        return "image/jpeg"
    if b64.startswith("R0lG"):
        return "image/gif"
    if b64.startswith("UklG"):
        return "image/webp"
    return "image/png"


def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    return system, rest


class Provider:
    name = "base"
    model = ""

    def chat(self, messages: list[dict], json_mode: bool = False) -> str:
        raise NotImplementedError

    def stream(self, messages: list[dict]) -> Iterator[str]:
        yield self.chat(messages)

    def label(self) -> str:
        return f"{self.name}:{self.model}"


# ----------------------------------------------------------------- local ----
class OllamaProvider(Provider):
    name = "local"

    def __init__(self, cfg: dict):
        self.base = cfg.get("base_url", "http://localhost:11434").rstrip("/")
        self.model = cfg.get("model", "gemma4:e4b")
        self.fallback_model = cfg.get("fallback_model")
        self.options = {
            "num_ctx": cfg.get("num_ctx", 4096),
            "temperature": cfg.get("temperature", 0.3),
        }
        self.keep_alive = cfg.get("keep_alive", "5m")
        self.json_ok = cfg.get("json_mode", True)
        # Small "thinking" models (qwen3...) can spend minutes thinking before answering.
        # think=false makes them answer directly; models without that switch are retried without it.
        self.think = cfg.get("think", False)
        # Cap reply length so a small model can't ramble for minutes (JSON steps are short).
        self.max_tokens = int(cfg.get("max_tokens", 1024))
        self.max_tokens_json = int(cfg.get("max_tokens_json", 512))
        self._no_think_param = set()
        ram = _ram_gb()
        if ram and ram < 12 and "num_ctx" not in cfg:     # low-memory laptop: smaller context
            self.options["num_ctx"] = 2048

    def _body(self, messages, stream, json_mode, model=None):
        msgs = []
        for m in messages:
            item = {"role": m["role"], "content": m.get("content", "")}
            if m.get("images"):
                item["images"] = m["images"]
            msgs.append(item)
        body = {
            "model": model or self.model,
            "messages": msgs,
            "stream": stream,
            "options": {**self.options,
                        "num_predict": self.max_tokens_json if json_mode else self.max_tokens},
            "keep_alive": self.keep_alive,
        }
        if json_mode and self.json_ok:
            body["format"] = "json"
        if self.think is not None and body["model"] not in self._no_think_param:
            body["think"] = bool(self.think)
        return body

    def _post(self, body):
        r = httpx.post(f"{self.base}/api/chat", json=body, timeout=TIMEOUT)
        if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
            self._no_think_param.add(body["model"])
            body.pop("think")
            r = httpx.post(f"{self.base}/api/chat", json=body, timeout=TIMEOUT)
        return r

    def installed_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.base}/api/tags", timeout=3)
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    def _pick_model(self) -> str:
        have = self.installed_models()
        if not have:
            return self.model
        if self.model in have or f"{self.model}:latest" in have:
            return self.model
        if self.fallback_model and self.fallback_model in have:
            return self.fallback_model
        return self.model

    def chat(self, messages, json_mode=False):
        """Streams internally so Stop can interrupt it; retries once on an empty reply."""
        for attempt in (1, 2):
            text = "".join(self._stream(messages, json_mode))
            if text.strip():
                return text
        return text

    def stream(self, messages):
        got = False
        for piece in self._stream(messages, False):
            got = got or bool(piece.strip())
            yield piece
        if not got:                                    # empty reply: try once more
            yield from self._stream(messages, False)

    def _stream(self, messages, json_mode):
        model = self._pick_model()
        body = self._body(messages, True, json_mode, model)
        cancel = CANCEL.get()
        for attempt in (1, 2):
            try:
                with httpx.stream("POST", f"{self.base}/api/chat", json=body, timeout=TIMEOUT) as r:
                    if r.status_code != 200:
                        text = r.read().decode("utf-8", "replace")
                        if attempt == 1 and r.status_code == 400 and "think" in text.lower() and "think" in body:
                            self._no_think_param.add(model)
                            body.pop("think")
                            continue
                        raise ProviderError(f"Ollama error {r.status_code}: {text[:300]}")
                    for line in r.iter_lines():
                        if cancel is not None and cancel.is_set():
                            raise Cancelled("Stopped by you.")     # closing the stream stops Ollama
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise ProviderError(f"Ollama: {chunk['error']}")
                        piece = chunk.get("message", {}).get("content", "")
                        if piece:
                            yield piece
                        if chunk.get("done"):
                            break
                    return
            except httpx.ConnectError as e:
                raise ProviderError("Ollama is not running. Start it with: ollama serve") from e
            except httpx.TimeoutException as e:
                raise ProviderError("The model took too long to answer - try a smaller model on the Models page.") from e


# ----------------------------------------------------------------- cloud ----
class AnthropicProvider(Provider):
    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, key: str, model: str):
        self.key, self.model = key, model

    def chat(self, messages, json_mode=False):
        system, rest = split_system(messages)
        if json_mode:
            system += "\n\nRespond with a single valid JSON object only."
        out = []
        for m in rest:
            blocks = [{"type": "image", "source": {"type": "base64", "media_type": mime_of(b),
                                                   "data": b}} for b in m.get("images") or []]
            blocks.append({"type": "text", "text": m.get("content") or " "})
            out.append({"role": m["role"], "content": blocks})
        body = {"model": self.model, "max_tokens": 2048, "messages": out}
        if system:
            body["system"] = system
        r = httpx.post(self.URL, json=body, timeout=TIMEOUT, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
        if r.status_code != 200:
            raise ProviderError(f"Claude API error {r.status_code}: {r.text[:300]}")
        return "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text")


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, key: str, model: str):
        self.key, self.model = key, model

    def chat(self, messages, json_mode=False):
        system, rest = split_system(messages)
        contents = []
        for m in rest:
            parts = [{"inline_data": {"mime_type": mime_of(b), "data": b}}
                     for b in m.get("images") or []]
            parts.append({"text": m.get("content") or " "})
            contents.append({"role": "model" if m["role"] == "assistant" else "user",
                             "parts": parts})
        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        r = httpx.post(url, json=body, timeout=TIMEOUT, headers={"x-goog-api-key": self.key})
        if r.status_code != 200:
            raise ProviderError(f"Gemini API error {r.status_code}: {r.text[:300]}")
        cands = r.json().get("candidates") or []
        if not cands:
            raise ProviderError("Gemini returned no answer")
        return "".join(p.get("text", "") for p in cands[0]["content"]["parts"])


class OpenAICompatProvider(Provider):
    """Works for xAI Grok, OpenAI, and any OpenAI-compatible endpoint."""

    def __init__(self, name: str, key: str, model: str, base_url: str):
        self.name, self.key, self.model, self.base = name, key, model, base_url.rstrip("/")

    def chat(self, messages, json_mode=False):
        out = []
        for m in messages:
            if m.get("images"):
                content = [{"type": "image_url",
                            "image_url": {"url": f"data:{mime_of(b)};base64,{b}"}}
                           for b in m["images"]]
                content.append({"type": "text", "text": m.get("content", "")})
            else:
                content = m.get("content", "")
            out.append({"role": m["role"], "content": content})
        body: dict = {"model": self.model, "messages": out}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {self.key}"})
        if r.status_code != 200:
            raise ProviderError(f"{self.name} API error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]


BASE_URLS = {"xai": "https://api.x.ai/v1", "openai": "https://api.openai.com/v1"}


def make_cloud(name: str, key: str, model: str) -> Provider:
    if name == "anthropic":
        return AnthropicProvider(key, model)
    if name == "gemini":
        return GeminiProvider(key, model)
    if name in BASE_URLS:
        return OpenAICompatProvider(name, key, model, BASE_URLS[name])
    raise ValueError(f"Unknown cloud provider: {name}")
