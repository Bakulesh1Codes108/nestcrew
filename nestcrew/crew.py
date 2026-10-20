"""A crew = several agents working through tasks in order (CrewAI-style),
defined in a YAML file under crews/."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from .agent import Agent
from .config import ROOT
from .providers import Provider
from .router import get_provider

CREWS_DIR = ROOT / "crews"


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def list_crews() -> list[dict]:
    out = []
    for p in sorted(CREWS_DIR.glob("*.yaml")):
        spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out.append({"file": p.stem, "name": spec.get("name", p.stem),
                    "description": spec.get("description", ""),
                    "inputs": spec.get("inputs", [])})
    return out


def load_crew(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.exists():
        p = CREWS_DIR / f"{name_or_path}.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def run_crew(spec: dict, inputs: dict | None = None, provider: Provider | None = None,
             on_event: Callable[[dict], None] | None = None, gate=None) -> dict:
    inputs = _Safe(inputs or {})
    agents = {}
    for key, a in (spec.get("agents") or {}).items():
        agents[key] = Agent(role=a.get("role", key), goal=a.get("goal", ""),
                            backstory=a.get("backstory", ""), tools=a.get("tools"),
                            max_steps=a.get("max_steps"))
    default_provider = provider or get_provider(task="agent")
    outputs, context = [], ""
    for i, t in enumerate(spec.get("tasks") or [], 1):
        agent = agents[t["agent"]]
        # a task may name a cloud provider or any model id from models.yaml
        prov = get_provider(t["provider"], task="agent") if t.get("provider") else default_provider
        desc = t["description"].format_map(inputs)
        if i == 1 and inputs.get("extra"):
            desc += f"\n\nExtra instructions from the user: {inputs['extra']}"
        if t.get("expected_output"):
            desc += f"\n\nExpected output: {t['expected_output'].format_map(inputs)}"
        if on_event:
            on_event({"type": "task", "n": i, "agent": agent.role, "description": desc})
        result = agent.run(prov, desc, context=context, on_event=on_event, gate=gate)
        outputs.append({"task": i, "agent": agent.role, "output": result})
        context += f"\n### {agent.role} (task {i})\n{result}\n"
    return {"crew": spec.get("name"), "outputs": outputs,
            "final": outputs[-1]["output"] if outputs else ""}
