"""Loads config.yaml and .env (API keys)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # optional
    pass

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_cache: dict | None = None


def load_config(path: str | Path | None = None) -> dict:
    global _cache
    if _cache is not None and path is None:
        return _cache
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Allow env overrides for quick switching
    if os.getenv("NESTCREW_MODE"):
        cfg["mode"] = os.environ["NESTCREW_MODE"]
    if os.getenv("NESTCREW_LOCAL_MODEL"):
        cfg.setdefault("local", {})["model"] = os.environ["NESTCREW_LOCAL_MODEL"]
    if path is None:
        _cache = cfg
    return cfg


def api_key(provider: str) -> str | None:
    env = KEY_ENV.get(provider)
    return os.getenv(env) if env else None


def resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT / p).resolve()
