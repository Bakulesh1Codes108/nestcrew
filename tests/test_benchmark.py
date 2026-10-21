"""The published numbers come from scripts/benchmark.py - keep it runnable (tiny sizes here)."""
import importlib.util
from pathlib import Path


def test_benchmark_runs_without_any_model(capsys):
    spec = importlib.util.spec_from_file_location("bench", Path(__file__).parent.parent / "scripts" / "benchmark.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    r = bench.main(files=60, size_kb=5)
    assert r["model_calls_for_file_commands"] == 0
    assert r["copy"]["files"] == 60 and r["duplicates"]["found"] == 60 // 7
    assert r["plan_ms_median"] < 50 and r["agent_prompt_tokens_est"] > 500
    assert "Command -> plan" in capsys.readouterr().out
