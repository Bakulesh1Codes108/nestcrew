"""Clean-up toolkit on real (temporary) folders: scan, duplicates, junk, empty folders,
sort by type/month, batches with Stop, Recycle Bin only, and routing to an agent."""
import os
import time

import pytest

from nestcrew import cleanup, rpa
from nestcrew.agent import Agent
from nestcrew.safety import Gate, Stopped
from tests.test_core import FakeModel, offline  # noqa: F401


BIN = [None]          # the stand-in Recycle Bin of the current test


@pytest.fixture
def pc(monkeypatch, tmp_path, tmp_path_factory):
    h = tmp_path_factory.mktemp("home")
    b2 = h / "Desktop" / "BIRAC2"
    (b2 / "old" / "empty1").mkdir(parents=True)
    (b2 / "empty2").mkdir()
    (b2 / "clip.mp4").write_bytes(b"V" * 5000)
    (b2 / "clip (2).mp4").write_bytes(b"V" * 5000)            # exact duplicate
    (b2 / "old" / "clip copy.mp4").write_bytes(b"V" * 5000)   # another duplicate, in a subfolder
    (b2 / "other.mp4").write_bytes(b"W" * 5000)               # same size, different content
    (b2 / "notes.txt").write_text("keep me")
    (b2 / "~$report.docx").write_text("lock")                 # junk
    (b2 / "setup.tmp").write_text("t")                        # junk
    (b2 / "movie.mp4.crdownload").write_bytes(b"x" * 10)      # junk (half download)
    old = time.time() - 400 * 86400
    os.utime(b2 / "clip.mp4", (old, old))                     # the oldest copy is the one kept
    for d in ("Documents", "Downloads", "Pictures"):
        (h / d).mkdir()
    monkeypatch.setattr(rpa, "_home", lambda: h)
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "rpa", {"folders": ["desktop", "documents", "downloads", "pictures"]})
    monkeypatch.setattr(rpa, "_undo_path", lambda: tmp_path / "undo.json")
    trash = tmp_path / "RecycleBin"
    trash.mkdir()

    def fake_send2trash(p):                                   # stands in for the Windows Recycle Bin
        import shutil
        shutil.move(p, str(trash / f"{len(list(trash.iterdir()))}_{os.path.basename(p)}"))
    import send2trash
    monkeypatch.setattr(send2trash, "send2trash", fake_send2trash)
    BIN[0] = trash
    return h


def names(p):
    return sorted(str(x.relative_to(p)).replace("\\", "/") for x in p.rglob("*")) if p.exists() else None


def run(task, decide="once", asked=None):
    asked = [] if asked is None else asked
    a = Agent()
    out = a.run(FakeModel([]), task, gate=Gate(ask_fn=lambda r: asked.append(r) or decide))
    return out, asked, a


def test_scan_reports_everything(pc):
    r = cleanup.scan_folder("Desktop/BIRAC2")
    assert "8 file(s)" in r and "Videos: 4 file(s)" in r
    assert "Duplicates: 2 extra copie(s) in 1 group(s)" in r and "could be freed" in r
    assert "Junk (temp / partial downloads / empty files): 3" in r and "Empty folders: 2" in r
    assert "Not changed for over a year: 1" in r


def test_duplicates_are_exact_and_keep_the_oldest(pc):
    groups = cleanup.duplicate_groups(rpa.resolve_path("Desktop/BIRAC2"))
    assert len(groups) == 1 and groups[0][0].name == "clip.mp4"
    assert sorted(p.name for p in groups[0][1:]) == ["clip (2).mp4", "clip copy.mp4"]   # other.mp4 isn't a copy


def test_make_folder_clean_end_to_end(pc):
    out, asked, a = run("make BIRAC2 folder clean")
    assert [r["tool"] for r in asked] == ["recycle_files"] * 3            # junk, duplicates, empty folders
    assert "RECYCLE 3 junk files" in asked[0]["preview"] and "Recycle Bin" in asked[0]["preview"]
    assert "RECYCLE 2 duplicate copies (one copy of each is kept)" in asked[1]["preview"]
    left = names(pc / "Desktop" / "BIRAC2")
    assert left == ["clip.mp4", "notes.txt", "old", "other.mp4"]           # originals and unique files stay
    assert len(list(BIN[0].iterdir())) == 3 + 2 + 3 - 1                  # 'old' became empty only after its copy left
    assert "Sent 3 item(s)" in out and "Sent 2 item(s)" in out
    assert [e["tool"] for e in a.events if e["type"] == "tool"][0] == "scan_folder"


def test_deny_cleans_nothing(pc):
    before = names(pc / "Desktop" / "BIRAC2")
    out, asked, _ = run("clean BIRAC2", decide="deny")
    assert names(pc / "Desktop" / "BIRAC2") == before and not list(BIN[0].iterdir())
    assert out.startswith("I stopped") and len(asked) == 1


def test_clean_folder_asks_nothing_when_already_clean(pc):
    tidy = pc / "Documents" / "Tidy"
    tidy.mkdir()
    (tidy / "a.txt").write_text("a")
    out, asked, _ = run("clean Tidy")
    assert asked == [] and out.startswith("Already clean - no junk, duplicates or empty folders")


def test_find_and_delete_duplicates_only(pc):
    out, asked, _ = run("delete duplicates in BIRAC2")
    assert len(asked) == 1 and (pc / "Desktop/BIRAC2/setup.tmp").exists()   # junk untouched
    assert not (pc / "Desktop/BIRAC2/clip (2).mp4").exists() and (pc / "Desktop/BIRAC2/clip.mp4").exists()
    assert "keep" in run("find duplicates in Pictures")[0] or "No duplicate" in run("find duplicates in Pictures")[0]


def test_sort_by_type_and_undo(pc):
    d = pc / "Downloads"
    for n in ("a.jpg", "b.PNG", "c.pdf", "d.mp4", "e.zip", "f.xyz"):
        (d / n).write_text(n)
    out, asked, _ = run("organize Downloads by type")
    assert "SORT 6 file(s)" in asked[0]["preview"] and "Images/" in asked[0]["preview"]
    assert names(d) == ["Archives", "Archives/e.zip", "Documents", "Documents/c.pdf", "Images", "Images/a.jpg",
                        "Images/b.PNG", "Other", "Other/f.xyz", "Videos", "Videos/d.mp4"]
    assert "Put back 6" in rpa.undo_last_move() and (d / "a.jpg").exists()


def test_sort_by_month(pc):
    d = pc / "Pictures"
    (d / "x.jpg").write_text("x")
    t = time.mktime((2025, 3, 15, 12, 0, 0, 0, 0, -1))
    os.utime(d / "x.jpg", (t, t))
    run("sort the files in Pictures by month")
    assert (d / "2025-03" / "x.jpg").exists()


def test_move_only_photos(pc):
    d = pc / "Downloads"
    for n in ("IMG_1.jpg", "IMG_2.HEIC", "doc.pdf", "vid.mp4"):
        (d / n).write_text(n)
    run("move all photos from Downloads to Pictures/Phone")
    assert names(pc / "Pictures" / "Phone") == ["IMG_1.jpg", "IMG_2.HEIC"] and (d / "doc.pdf").exists()


def test_big_job_runs_in_batches_with_progress(pc, monkeypatch):
    monkeypatch.setattr(cleanup, "BATCH_FILES", 100)
    src = pc / "Downloads" / "phone"
    src.mkdir()
    for i in range(1000):
        (src / f"IMG_{i:04}.jpg").write_bytes(b"p" * 50)
    seen = []
    a = Agent()
    out = a.run(FakeModel([]), "move all photos from Downloads/phone to Pictures/Phone backup",
                on_event=seen.append, gate=Gate(ask_fn=lambda r: "once"))
    assert len(list((pc / "Pictures" / "Phone backup").iterdir())) == 1000 and "in 10 batches" in out
    assert sum(1 for e in seen if e.get("type") == "status" and e["text"].startswith("Batch")) == 10


def test_batches_split_by_size_too(pc, monkeypatch):
    monkeypatch.setattr(cleanup, "BATCH_BYTES", 10_000)
    d = pc / "Videos_src"
    d.mkdir()
    files = []
    for i in range(5):
        (d / f"v{i}.mp4").write_bytes(b"v" * 6000)
        files.append(d / f"v{i}.mp4")
    assert [len(b) for b in cleanup.batches(files)] == [1, 1, 1, 1, 1]       # big files go one at a time


def test_stop_between_batches_keeps_undo(pc, monkeypatch):
    monkeypatch.setattr(cleanup, "BATCH_FILES", 10)
    src = pc / "Downloads" / "many"
    src.mkdir()
    for i in range(50):
        (src / f"f{i:02}.jpg").write_text(str(i))
    gate = Gate(ask_fn=lambda r: "once")
    calls = {"n": 0}
    real = cleanup._progress

    def stop_after_two(text):
        calls["n"] += 1
        if calls["n"] == 3:
            gate.cancel.set()
        real(text)
    monkeypatch.setattr(cleanup, "_progress", stop_after_two)
    with pytest.raises(Stopped):
        Agent().run(FakeModel([]), "move all files from Downloads/many to Documents/many", gate=gate)
    moved = len(list((pc / "Documents" / "many").iterdir()))
    assert moved == 20 and len(list(src.iterdir())) == 30                      # stopped cleanly after 2 batches
    assert "Put back 20" in rpa.undo_last_move() and len(list(src.iterdir())) == 50


def test_never_touches_private_or_outside_folders(pc):
    (pc / "Desktop" / ".ssh").mkdir()
    with pytest.raises(ValueError):
        cleanup.recycle_files("Desktop/.ssh", what="junk")
    with pytest.raises(ValueError):
        cleanup.scan_folder("C:/Windows")


@pytest.mark.parametrize("task", ["make BIRAC2 folder clean", "sort my phone photos", "free up space on my drive",
                                  "delete the junk in Downloads", "clean BIRAC2", "tidy the BIRAC2 folder"])
def test_file_actions_route_to_agent_without_the_model(pc, task, monkeypatch):
    from nestcrew import hints
    monkeypatch.setattr(hints, "classify", lambda t: (_ for _ in ()).throw(AssertionError("model used")))
    assert hints.hint(task)["type"] == "files"


@pytest.mark.parametrize("task", ["what is the capital of France", "how do I clean my laptop screen",
                                  "explain duplicates in statistics", "clean my room please"])
def test_normal_questions_are_not_file_actions(pc, task):
    assert not rpa.looks_like_file_action(task) or rpa.quick_plan(task) is None
    assert rpa.quick_plan(task) is None


def test_chat_never_claims_it_cannot_reach_files():
    import inspect
    from nestcrew import server
    src = inspect.getsource(server.chat)
    assert "never say you can't access them" in src and "Switch to Agent" in src


def test_cleaner_specialist_has_the_tools():
    spec = rpa.SPECIALISTS["cleaner"]
    for t in ("scan_folder", "find_duplicates", "recycle_files", "organize_folder", "move_files"):
        assert t in spec["tools"]
    assert "Recycle Bin" in spec["goal"]


def test_already_clean_folder_gets_one_clear_answer(pc):
    import shutil as _sh
    clean = pc / "Desktop" / "Tidy"
    clean.mkdir()
    (clean / "a.mp4").write_bytes(b"a" * 10)
    (clean / "b.mp4").write_bytes(b"b" * 20)
    asked = []
    out = Agent().run(FakeModel([]), "make Tidy folder clean", gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert not asked
    assert out == "Already clean - no junk, duplicates or empty folders. (Desktop/Tidy: 2 file(s), 30 B)"
    _sh.rmtree(clean)
