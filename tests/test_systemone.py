"""Open System One servers (Laya / Kev / OpenJev / Cribl) via /v1/systemone — mocked
in the exact response shape of laya 0.4.11 serve.py."""
import json

import httpx
import pytest

from nestcrew import hints, models, system1, workflows
from tests.test_core import FakeModel, offline  # noqa: F401


class Resp:
    def __init__(self, data, status=200):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


@pytest.fixture
def laya(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(models, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(workflows, "_dir", lambda: (tmp_path / "wf").mkdir(exist_ok=True) or tmp_path / "wf")
    monkeypatch.setattr(system1, "embed", lambda t: None)

    def post(url, json=None, timeout=None, headers=None):
        calls.append({"url": url, "body": json})
        (qid, q), = json["questions"].items()
        if q["type"] == "choice":
            labels = list(q["criteria"])
            pick = labels[-1]
            probs = {l: (0.8 if l == pick else 0.2 / (len(labels) - 1)) for l in labels}
            ans = {"type": "choice", "choice": pick, "confidence": 0.8, "probabilities": probs}
        elif q["type"] == "noul":
            ans = {"type": "noul", "noul": 0.741}
        else:
            ans = {"type": "score", "score": 1.49, "confidence": 0.6, "probabilities": {}, "legend": q["criteria"]}
        return Resp({"model": json["model"], "answers": {qid: ans}, "usage": {"input_tokens": 12, "output_tokens": 0}})
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: Resp({"status": "ok"}))
    return calls


def test_laya_choice_request_and_parse(laya):
    models.set_system1_engine("laya")
    r = system1.choice("billed twice", {"technical": {"examples": ["bug"]}, "billing": {"description": "payments"}},
                       group="dept", instructions="Which team?")
    assert r["label"] == "billing" and r["method"] == "laya" and r["probs"]["billing"] == 0.8
    b = laya[0]["body"]
    assert laya[0]["url"] == "http://127.0.0.1:8000/v1/systemone"
    assert b == {"state": "billed twice", "model": "typed-decisions",
                 "questions": {"dept": {"type": "choice", "instructions": "Which team?",
                                        "criteria": {"technical": "bug", "billing": "payments"}}}}


def test_laya_noul_and_score(laya):
    models.set_system1_engine("laya")
    assert system1.yes_no("refund or we cancel", ["y"], ["n"], question="Do they threaten to cancel?") == 0.741
    lv = [{"label": "not urgent"}, {"label": "soon"}, {"label": "blocking"}]
    assert system1.score("x", "low", "high", levels=lv) == round(1.49 / 2, 3)
    assert laya[-1]["body"]["questions"]["score"]["criteria"] == ["not urgent", "soon", "blocking"]


def test_hints_use_laya_locally(laya, monkeypatch):
    models.set_system1_engine("laya")
    monkeypatch.setattr(system1, "low_ram", lambda: False)
    h = hints.hint("please extract the invoice total")
    assert h["method"] == "laya" and laya                       # local server, nothing goes online


def test_low_ram_keeps_typing_hints_light(laya, monkeypatch):
    models.set_system1_engine("laya")
    monkeypatch.setattr(system1, "low_ram", lambda: True)
    hints.hint("please extract the invoice total")
    assert not laya                                             # no Laya call per keystroke
    system1.choice("x", {"a": {}, "b": {}})                     # Canvas decisions still use Laya
    assert len(laya) == 1


def test_kev_and_custom_url(laya, monkeypatch):
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "system1", {**load_config()["system1"],
                        "servers": {"kev": {"url": "http://127.0.0.1:9999"}}})
    models.set_system1_engine("kev")
    system1.choice("x", {"a": {}, "b": {}})
    assert laya[-1]["url"] == "http://127.0.0.1:9999/v1/systemone" and laya[-1]["body"]["model"] == "kev-latest"


def test_server_down_falls_back(laya, monkeypatch):
    models.set_system1_engine("laya")
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", boom)
    assert system1.choice("x", {"a": {"keywords": ["x"]}, "b": {}})["method"] == "keywords"


def test_canvas_decide_with_laya(laya):
    models.set_system1_engine("laya")
    wf = workflows.save({"name": "t", "nodes": [
        {"id": "t", "type": "trigger", "config": {"mode": "manual"}},
        {"id": "d", "type": "decide", "config": {"question": "Problem?", "options": {"fine": ["ok"], "problem": ["bad"]}}},
        {"id": "o", "type": "output", "config": {}}],
        "edges": [{"from": "t", "to": "d"}, {"from": "d", "to": "o", "when": "problem"}]})
    seen = []
    workflows.run(wf, on_event=seen.append)
    assert next(e for e in seen if e["type"] == "decision")["method"] == "laya"


def test_unknown_engine_rejected(laya):
    with pytest.raises(ValueError):
        models.set_system1_engine("jev-cloud")


def test_luna_and_paid_engines_removed(laya):
    with pytest.raises(ValueError):
        models.set_system1_engine("luna")
    import inspect
    assert "openai.com" not in inspect.getsource(system1)


def test_default_engine_is_free_laya(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_settings_path", lambda: tmp_path / "s.json")
    assert system1.engine() == "laya"
