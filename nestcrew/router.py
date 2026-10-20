"""Decides which model answers: local first, cloud only when allowed + online."""
from __future__ import annotations

from .config import api_key, load_config
from .net import is_online
from .providers import Provider, ProviderError, make_cloud

CLOUD = ("anthropic", "gemini", "xai", "openai")


def cloud_provider(name: str | None = None, cfg: dict | None = None) -> Provider | None:
    cfg = cfg or load_config()
    name = name or cfg.get("cloud", {}).get("preferred", "anthropic")
    key = api_key(name)
    if not key:
        return None
    model = cfg.get("cloud", {}).get(name, {}).get("model")
    return make_cloud(name, key, model)


class FallbackProvider(Provider):
    """Try local; if it errors and we're online with a key, use cloud."""

    def __init__(self, local: Provider, cfg: dict):
        self.local, self.cfg = local, cfg
        self.name, self.model = local.name, local.model
        self.last_used = local.label()

    def _cloud(self):
        return cloud_provider(cfg=self.cfg) if is_online() else None

    def chat(self, messages, json_mode=False):
        try:
            out = self.local.chat(messages, json_mode)
            self.last_used = self.local.label()
            return out
        except ProviderError:
            cloud = self._cloud()
            if not cloud:
                raise
            self.last_used = cloud.label()
            return cloud.chat(messages, json_mode)

    def stream(self, messages):
        try:
            yield from self.local.stream(messages)
            self.last_used = self.local.label()
        except ProviderError:
            cloud = self._cloud()
            if not cloud:
                raise
            self.last_used = cloud.label()
            yield f"[local model unavailable - answered by {cloud.label()}]\n\n"
            yield from cloud.stream(messages)


def get_provider(choice: str | None = None, task: str = "chat") -> Provider:
    """choice: None/'auto' (follow config), 'local', a cloud name, or a model id
    from models.yaml. `task` picks the local model assigned to that job."""
    from . import models

    cfg = load_config()
    if choice in CLOUD:
        if not is_online():
            raise ProviderError(f"You're offline - {choice} needs internet. Use 'local'.")
        p = cloud_provider(choice, cfg)
        if not p:
            raise ProviderError(f"No API key for {choice}. Add it to your .env file.")
        return p
    if choice and choice not in ("auto", "local") and choice in models.catalog()["models"]:
        return models.provider_for(choice)
    local = models.provider_for_task(task)
    if choice == "local":
        return local
    mode = cfg.get("mode", "auto")
    if mode == "local":
        return local
    if mode == "cloud":
        p = cloud_provider(cfg=cfg) if is_online() else None
        return p or local
    return FallbackProvider(local, cfg)


def is_local(p: Provider) -> bool:
    return getattr(p, "name", "") in ("local", "llamacpp", "nanovlm", "jan", "lmstudio")
