"""RPA toolkit: files/folders/Excel with approvals, undo, sandboxing, and specialist delegation."""
import json

import pytest

from nestcrew import rpa, tools
from nestcrew.agent import Agent
from nestcrew.safety import Gate, preview
from tests.test_core import FakeModel, offline  # noqa: F401


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    (h / "Desktop" / "BIRAC").mkdir(parents=True)
    (h / "Documents").mkdir()
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_rsa").write_text("secret")
    for n in ("a.pdf", "b.pdf", "notes.txt"):
        (h / "Desktop" / "BIRAC" / n).write_text(n)
    monkeypatch.setattr(rpa, "_home", lambda: h)
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "rpa", {"folders": ["desktop", "documents", "downloads"]})
    monkeypatch.setattr(rpa, "_undo_path", lambda: tmp_path / "undo.json")
    return h


def J(**kw):
    return json.dumps(kw)


def test_move_birac_to_birac2_with_approval_and_undo(home):
    asked = []
    m = FakeModel([J(action="list_folder", input={"path": "Desktop/BIRAC"}),
                   J(action="move_files", input={"source": "Desktop/BIRAC", "destination": "Desktop/BIRAC2"}),
                   J(action="final", answer="Moved 3 files")])
    a = Agent()
    a.run(m, "MOVE ALL FILE FROM BIRAC TO BIRAC2 ON DESKTOP", gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert len(asked) == 1 and asked[0]["tool"] == "move_files"          # listing didn't ask
    assert "MOVE 3 file(s)" in asked[0]["preview"] and "a.pdf" in asked[0]["preview"]
    assert sorted(p.name for p in (home / "Desktop" / "BIRAC2").iterdir()) == ["a.pdf", "b.pdf", "notes.txt"]
    assert not list((home / "Desktop" / "BIRAC").iterdir())
    assert "Put back 3" in rpa.undo_last_move()
    assert len(list((home / "Desktop" / "BIRAC").iterdir())) == 3


def test_denied_move_changes_nothing(home):
    m = FakeModel([J(action="move_files", input={"source": "Desktop/BIRAC", "destination": "Desktop/X"}),
                   J(action="final", answer="ok")])
    Agent().run(m, "move", gate=Gate(ask_fn=lambda r: "deny"))
    assert len(list((home / "Desktop" / "BIRAC").iterdir())) == 3 and not (home / "Desktop" / "X").exists()


def test_unattended_cannot_move(home):
    m = FakeModel([J(action="move_files", input={"source": "Desktop/BIRAC", "destination": "Desktop/X"}),
                   J(action="final", answer="ok")])
    Agent().run(m, "move")                                                   # no human -> denied
    assert not (home / "Desktop" / "X").exists()


def test_copy_never_overwrites(home):
    rpa.copy_files("Desktop/BIRAC", "Documents", "*.pdf")
    rpa.copy_files("Desktop/BIRAC", "Documents", "*.pdf")
    assert sorted(p.name for p in (home / "Documents").iterdir()) == ["a (2).pdf", "a.pdf", "b (2).pdf", "b.pdf"]


@pytest.mark.parametrize("bad", ["~/.ssh/id_rsa", "C:/Windows/System32", "/etc/passwd", "../../outside", "Desktop/../.ssh"])
def test_sandbox_refuses_outside_paths(home, bad):
    with pytest.raises(ValueError):
        rpa.resolve_path(bad)


def test_no_delete_tool():
    assert not any("delete" in n or "remove" in n for n in tools.REGISTRY)


def test_excel_write_read_append_with_backup(home, tmp_path):
    pytest.importorskip("openpyxl")
    rpa.write_excel("Desktop/report.xlsx", [["Name", "Total"], ["A", 10]])
    rpa.append_excel_rows("Desktop/report.xlsx", json.dumps([["B", 32]]))
    out = rpa.read_excel("Desktop/report.xlsx")
    assert "2 data row(s)" in out and "B\t32" in out
    assert list((tmp_path / ".backups").iterdir())                            # appended file was backed up
    rpa.write_excel("Desktop/list.csv", [{"x": 1, "y": 2}])
    assert "x\ty" in rpa.read_excel("Desktop/list.csv")


def test_previews_show_affected_files(home):
    p = preview("copy_files", {"source": "Desktop/BIRAC", "destination": "Documents/New", "pattern": "*.pdf"})
    assert "COPY 2 file(s)" in p and "will be created" in p and "notes.txt" not in p
    p = preview("write_excel", {"path": "Desktop/r.xlsx", "rows": [["a", "b"], [1, 2]]})
    assert p.startswith("Create Desktop/r.xlsx - 2 row(s)")


def test_delegate_to_file_clerk_shares_approvals(home):
    asked, seen = [], []
    m = FakeModel([
        J(action="delegate", input={"specialist": "file_clerk", "task": "sort the pdfs in Desktop/BIRAC"}),
        J(action="copy_files", input={"source": "Desktop/BIRAC", "destination": "Documents", "pattern": "*.pdf"}),
        J(action="final", answer="copied 2"),
        J(action="final", answer="All done")])
    out = Agent().run(m, "organise my pdfs", on_event=seen.append, gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert out == "All done" and len(list((home / "Documents").iterdir())) == 2
    assert asked[0]["agent"] == "File clerk"                                  # you approve the specialist's step
    assert any(e["type"] == "delegate" and e["to"] == "File clerk" for e in seen)
    sub_prompt = m.seen[1][0]["content"]
    assert "File clerk" in sub_prompt and "delegate" not in sub_prompt.split("Tools:")[1]


def test_delegated_plain_file_command_uses_quick_plan(home):
    m = FakeModel([J(action="delegate", input={"specialist": "file_clerk",
                                               "task": "copy pdfs from Desktop/BIRAC to Documents"}),
                   J(action="final", answer="All done")])
    asked = []
    assert Agent().run(m, "organise my pdfs", gate=Gate(ask_fn=lambda r: asked.append(r) or "once")) == "All done"
    assert len(m.seen) == 2 and asked[0]["agent"] == "File clerk"            # specialist needed no model call
    assert len(list((home / "Documents").iterdir())) == 2


def test_specialist_cannot_delegate_again(home):
    m = FakeModel([J(action="delegate", input={"specialist": "writer", "task": "x"}),
                   J(action="delegate", input={"specialist": "writer", "task": "y"}),
                   J(action="final", answer="w"), J(action="final", answer="done")])
    a = Agent(tools=["delegate"])
    a.run(m, "t", gate=Gate(ask_fn=lambda r: "once"))
    assert "not allowed" in m.seen[2][-1]["content"]                          # writer has no delegate tool


@pytest.mark.parametrize("task,tool_name,src,dst,pattern", [
    ("MOVE ALL FILE FROM BIRAC TO BIRAC2 ON DESKTOP", "move_files", "Desktop/BIRAC", "Desktop/BIRAC2", "*"),
    ('Copy all files from "NestCrew test/BIRAC" on my Desktop into a new folder "NestCrew test/BIRAC copy"',
     "copy_files", "Desktop/NestCrew test/BIRAC", "Desktop/NestCrew test/BIRAC copy", "*"),
    ("move the pdfs from Downloads to Documents/Invoices", "move_files", "Downloads", "Documents/Invoices", "*.pdf"),
    ("copy *.xlsx from Downloads into Documents/Reports", "copy_files", "Downloads", "Documents/Reports", "*.xlsx"),
])
def test_quick_plan_reads_common_file_commands(task, tool_name, src, dst, pattern):
    plan = rpa.quick_plan(task)
    assert plan[0] == {"tool": "list_folder", "input": {"path": src}}
    assert plan[1] == {"tool": tool_name, "input": {"source": src, "destination": dst, "pattern": pattern}}


@pytest.mark.parametrize("task", ["Summarise the pdf in Downloads", "copy the totals from report into a new sheet",
                                  "move files from A to A", "write an excel of my expenses"])
def test_quick_plan_leaves_other_tasks_to_the_model(task):
    assert rpa.quick_plan(task) is None


def test_quick_plan_runs_without_the_model_and_still_asks(home):
    asked = []
    model = FakeModel([])                                  # would fail if the model were called
    a = Agent()
    out = a.run(model, "copy the pdfs from BIRAC to BIRAC copy on my desktop",
                gate=Gate(ask_fn=lambda r: asked.append(r["tool"]) or "once"))
    assert asked == ["copy_files"] and "Copied 2 file(s)" in out
    assert sorted(p.name for p in (home / "Desktop" / "BIRAC copy").iterdir()) == ["a.pdf", "b.pdf"]
    assert [e["tool"] for e in a.events if e["type"] == "tool"] == ["list_folder", "copy_files"]


def test_quick_plan_stops_when_folder_is_missing(home):
    out = Agent().run(FakeModel([]), "move all files from NOPE to BIRAC2 on desktop",
                      gate=Gate(ask_fn=lambda r: "once"))
    assert out.startswith("I stopped") and not (home / "Desktop" / "BIRAC2").exists()


def test_bare_folder_names_are_found_on_desktop(home):
    (home / "Desktop" / "BIRAC2").mkdir()
    assert rpa.locate("BIRAC") == "Desktop/BIRAC"
    assert rpa.resolve_path("BIRAC") == (home / "Desktop" / "BIRAC").resolve()
    plan = rpa.quick_plan("copy all files from BIRAC to BIRAC2")
    assert plan[1]["input"] == {"source": "Desktop/BIRAC", "destination": "Desktop/BIRAC2", "pattern": "*"}
    plan = rpa.quick_plan("copy all files from BIRAC to Reports new")      # new folder goes next to it
    assert plan[1]["input"]["destination"] == "Desktop/Reports new"
