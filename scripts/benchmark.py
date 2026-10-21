"""Reproducible NestCrew benchmarks - no models, no internet, nothing outside a temp folder.

    python scripts/benchmark.py            # prints a table; --json for machine-readable output

Measures the parts that don't depend on an LLM: command -> plan, task routing, batch copy/move,
duplicate detection, folder scan, and the agent prompt size (tokens a model step would read).
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(as_json: bool = False, files: int = 2000, size_kb: int = 200) -> dict:
    """Runs in a throw-away folder and puts every setting it touches back afterwards."""
    import copy
    from nestcrew import rpa
    from nestcrew.config import load_config
    cfg = load_config()
    saved_cfg = copy.deepcopy(cfg)
    saved = (rpa._home, rpa._undo_path)
    try:
        return _run(as_json, files, size_kb)
    finally:
        cfg.clear()
        cfg.update(saved_cfg)
        rpa._home, rpa._undo_path = saved


def _run(as_json: bool, files: int, size_kb: int) -> dict:
    from nestcrew import cleanup, hints, rpa, tools
    from nestcrew.agent import Agent
    from nestcrew.config import load_config
    from nestcrew.providers import Provider
    from nestcrew.safety import Gate

    class NoModel(Provider):            # proves the file commands need no model at all
        name, model = "none", "none"
        calls = 0

        def chat(self, messages, json_mode=False):
            NoModel.calls += 1
            raise AssertionError("a model was called")

    home = Path(tempfile.mkdtemp(prefix="nestcrew-bench-"))
    for d in ("Desktop/BIRAC", "Desktop/BIRAC2", "Downloads/bulk", "Pictures"):
        (home / d).mkdir(parents=True)
    rpa._home = lambda: home
    cfg = load_config()
    cfg["rpa"] = {"folders": ["desktop", "downloads", "pictures"]}
    cfg["memory"] = {"db": str(home / "bench.db")}
    cfg.setdefault("agent", {})["learn"] = False
    rpa._undo_path = lambda: home / "undo.json"
    cfg.setdefault("safety", {})["audit_log"] = str(home / "audit.jsonl")     # never touch your real log
    r: dict = {"machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPU(s), Python {platform.python_version()}"}

    prompt = Agent().system_prompt()
    r["agent_prompt_tokens_est"] = len(prompt) // 4
    r["tools"] = len(tools.available())

    cmds = ["copy all files from BIRAC to BIRAC2", "move all photos from Downloads and Desktop including "
            "subfolders to Pictures/Sorted", "make BIRAC2 folder clean", "backup my phone",
            "how much space is left on my drives"]
    t = []
    for _ in range(100):
        for c in cmds:
            t0 = time.perf_counter()
            rpa.quick_plan(c)
            t.append((time.perf_counter() - t0) * 1000)
    r["plan_ms_median"], r["plan_ms_p95"] = round(statistics.median(t), 2), round(sorted(t)[int(len(t) * .95)], 2)
    t = []
    for _ in range(20):
        for c in cmds:
            t0 = time.perf_counter()
            hints.hint(c)
            t.append((time.perf_counter() - t0) * 1000)
    r["route_ms_median"] = round(statistics.median(t), 1)

    src = home / "Downloads/bulk"
    for i in range(files):
        (src / f"IMG_{i:05}.jpg").write_bytes(os.urandom(size_kb * 1000))
    mb = files * size_kb / 1000
    gate = Gate(ask_fn=lambda req: "once")
    t0 = time.perf_counter()
    Agent().run(NoModel(), "copy all photos from Downloads/bulk to Pictures/Bulk", gate=gate)
    dt = time.perf_counter() - t0
    r["copy"] = {"files": files, "MB": mb, "seconds": round(dt, 2), "files_per_s": round(files / dt), "MB_per_s": round(mb / dt)}
    t0 = time.perf_counter()
    Agent().run(NoModel(), "move all photos from Pictures/Bulk to Desktop/Moved", gate=gate)
    r["move_same_drive_s"] = round(time.perf_counter() - t0, 2)
    moved = home / "Desktop/Moved"
    dups = files // 7
    for i in range(dups):
        (moved / f"dup_{i}.jpg").write_bytes((moved / f"IMG_{i:05}.jpg").read_bytes())
    t0 = time.perf_counter()
    groups = cleanup.duplicate_groups(moved)
    r["duplicates"] = {"files": files + dups, "MB": round((files + dups) * size_kb / 1000), "found": len(groups),
                       "seconds": round(time.perf_counter() - t0, 2)}
    t0 = time.perf_counter()
    cleanup.scan_folder("Desktop/Moved")
    r["scan_s"] = round(time.perf_counter() - t0, 2)
    r["model_calls_for_file_commands"] = NoModel.calls

    import shutil
    shutil.rmtree(home, ignore_errors=True)
    if as_json:
        print(json.dumps(r, indent=2))
    else:
        c, d = r["copy"], r["duplicates"]
        print(f"Machine: {r['machine']}")
        print(f"Agent prompt (what one model step reads): ~{r['agent_prompt_tokens_est']:,} tokens, {r['tools']} tools")
        print(f"Command -> plan: {r['plan_ms_median']} ms median, {r['plan_ms_p95']} ms p95 "
              f"(model calls: {r['model_calls_for_file_commands']}, tokens: 0)")
        print(f"Task routing: {r['route_ms_median']} ms median")
        print(f"Copy {c['files']:,} files / {c['MB']:.0f} MB: {c['seconds']} s ({c['files_per_s']:,} files/s, {c['MB_per_s']} MB/s)")
        print(f"Move {c['files']:,} files on the same drive: {r['move_same_drive_s']} s")
        print(f"Duplicate scan {d['files']:,} files / {d['MB']} MB: {d['seconds']} s ({d['found']} duplicate groups)")
        print(f"Folder scan report: {r['scan_s']} s")
    return r


if __name__ == "__main__":
    main(as_json="--json" in sys.argv)
