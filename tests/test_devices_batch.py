"""Batches from several folders (subfolders too), other drives, and phone import.

The phone is reached through Windows PowerShell; here that call is replaced by a fake phone
so the batching, skipping and safety logic is checked on any OS."""
import json

import pytest

from nestcrew import cleanup, devices, rpa
from nestcrew.agent import Agent
from nestcrew.safety import Gate, Stopped
from tests.test_core import FakeModel, offline  # noqa: F401


@pytest.fixture
def pc(monkeypatch, tmp_path, tmp_path_factory):
    h = tmp_path_factory.mktemp("home")
    for d in ("Desktop/Phone dump/2024", "Desktop/Phone dump/.thumbnails", "Desktop/proj/.git",
              "Downloads/AppData", "Downloads/trip", "Pictures", "Documents"):
        (h / d).mkdir(parents=True)
    (h / "Desktop/Phone dump/IMG_1.jpg").write_bytes(b"1" * 100)
    (h / "Desktop/Phone dump/2024/IMG_2.jpg").write_bytes(b"2" * 100)
    (h / "Desktop/Phone dump/2024/VID_1.mp4").write_bytes(b"v" * 300)
    (h / "Desktop/Phone dump/.thumbnails/t1.jpg").write_bytes(b"t")        # hidden: skipped
    (h / "Desktop/proj/.git/logo.png").write_bytes(b"g")                    # system: skipped
    (h / "Downloads/IMG_1.jpg").write_bytes(b"other content")              # same name, different file
    (h / "Downloads/trip/beach.png").write_bytes(b"b" * 50)
    (h / "Downloads/AppData/secret.png").write_bytes(b"s")                  # private: skipped
    (h / "Downloads/report.pdf").write_bytes(b"p")
    monkeypatch.setattr(rpa, "_home", lambda: h)
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "rpa", {"folders": ["desktop", "documents", "downloads", "pictures"]})
    monkeypatch.setattr(rpa, "_undo_path", lambda: tmp_path / "undo.json")
    return h


def names(p):
    return sorted(x.name for x in p.iterdir()) if p.exists() else None


def run(task, decision="once", events=None):
    asked = []
    a = Agent()
    out = a.run(FakeModel([]), task, gate=Gate(ask_fn=lambda r: asked.append(r) or decision),
                on_event=(events.append if events is not None else None))
    return out, asked, a


# ------------------------------------------------- several folders, subfolders --
def test_photos_from_two_folders_including_subfolders(pc):
    out, asked, _ = run("move all photos from Desktop/Phone dump and Downloads including subfolders to Pictures/Sorted")
    assert len(asked) == 1 and asked[0]["tool"] == "move_files"
    card = asked[0]["preview"]
    assert "MOVE 4 file(s)" in card and "(and their subfolders)" in card
    got = names(pc / "Pictures/Sorted")
    assert got == ["IMG_1 (2).jpg", "IMG_1.jpg", "IMG_2.jpg", "beach.png"]      # name clash -> (2), nothing lost
    assert (pc / "Desktop/Phone dump/2024/VID_1.mp4").exists()                  # not a photo: left alone
    assert (pc / "Desktop/Phone dump/.thumbnails/t1.jpg").exists()             # hidden folder skipped
    assert (pc / "Downloads/AppData/secret.png").exists()                       # private folder skipped
    assert (pc / "Downloads/report.pdf").exists()
    assert "Put back 4" in rpa.undo_last_move()
    assert (pc / "Desktop/Phone dump/2024/IMG_2.jpg").exists() and (pc / "Downloads/IMG_1.jpg").read_bytes() == b"other content"


def test_folder_names_with_and_are_not_split(pc):
    (pc / "Documents/Finance and Tax").mkdir()
    plan = rpa.quick_plan("copy all files from Finance and Tax to Finance backup")
    assert plan[-1]["input"]["source"] == "Documents/Finance and Tax"


def test_target_inside_source_is_not_copied_again(pc):
    out, asked, _ = run("copy images from Downloads including subfolders to Downloads/trip")
    assert "beach (2).png" not in names(pc / "Downloads/trip")


def test_many_files_parallel_copy_in_batches_with_progress(pc, monkeypatch):
    src = pc / "Downloads/bulk"
    src.mkdir()
    for i in range(450):
        (src / f"IMG_{i:04}.jpg").write_bytes(bytes([i % 256]) * 64)
    events = []
    out, asked, _ = run("copy all photos from Downloads/bulk to Pictures/Bulk", events=events)
    assert "in 3 batches" in asked[0]["preview"]
    assert len(names(pc / "Pictures/Bulk")) == 450
    assert all((pc / "Pictures/Bulk" / f.name).read_bytes() == f.read_bytes() for f in src.iterdir())
    assert [e["text"] for e in events if e["type"] == "status"][:3] == [
        "Batch 1/3: 200 file(s)", "Batch 2/3: 200 file(s)", "Batch 3/3: 50 file(s)"]


def test_stop_mid_batch_job_keeps_undo(pc, monkeypatch):
    src = pc / "Downloads/bulk"
    src.mkdir()
    for i in range(450):
        (src / f"IMG_{i:04}.jpg").write_bytes(b"x" * (i + 1))
    gate = Gate(ask_fn=lambda r: "once")
    calls = {"n": 0}
    real = cleanup._progress

    def progress(text):
        calls["n"] += 1
        if calls["n"] == 2:
            gate.cancel.set()                         # you press Stop before batch 2
        real(text)
    monkeypatch.setattr(cleanup, "_progress", progress)
    with pytest.raises(Stopped):
        Agent().run(FakeModel([]), "move all photos from Downloads/bulk to Pictures/Bulk", gate=gate)
    assert len(names(pc / "Pictures/Bulk")) == 200 and len(names(src)) == 250
    assert "Put back 200" in rpa.undo_last_move() and len(names(src)) == 450


# ------------------------------------------------------------------ drives --
@pytest.mark.parametrize("text,expected", [("D drive", "D:/"), ("drive e", "E:/"), ("E:", "E:/"),
                                           ("D drive/Photos", "D:/Photos"), ("d:/Backup 2026", "D:/Backup 2026")])
def test_drive_words(text, expected):
    assert rpa._clean_path(text, None) == expected


def test_other_drives_are_allowed_when_enabled(pc, monkeypatch, tmp_path_factory):
    usb = tmp_path_factory.mktemp("usb_drive")
    (usb / "DCIM").mkdir()
    (usb / "DCIM/a.jpg").write_bytes(b"a")
    from nestcrew.config import load_config
    with pytest.raises(ValueError, match="outside"):
        rpa.resolve_path(str(usb / "DCIM"))
    monkeypatch.setattr(rpa, "drive_roots", lambda: [usb])
    monkeypatch.setitem(load_config(), "rpa", {"folders": ["desktop", "pictures", "drives"]})
    assert rpa.resolve_path(str(usb / "DCIM")) == (usb / "DCIM").resolve()
    out, asked, _ = run(f"copy photos from {usb.as_posix()}/DCIM to Pictures/From USB")
    assert names(pc / "Pictures/From USB") == ["a.jpg"]
    (usb / ".ssh").mkdir()
    with pytest.raises(ValueError, match="protected"):
        rpa.resolve_path(str(usb / ".ssh"))


def test_drive_space_report():
    out = devices.drive_space()
    assert out.startswith("Drives:") and "free of" in out


def test_system_drive_never_listed_as_extra(monkeypatch):
    if rpa.os.name != "nt":
        assert rpa.drive_roots() == []


# ------------------------------------------------------------------ phone --
class FakePhone:
    """Stands in for PowerShell + the Windows Shell talking to a phone."""

    def __init__(self, files):
        self.files, self.calls, self.copied_to = files, [], None

    def __call__(self, script, timeout=600):
        self.calls.append(script)
        if "Walk $root" in script:
            exts = [e.strip("'") for e in script.split("$exts = @(")[1].split(")")[0].split(",")]
            return json.dumps([f for f in self.files if any(f["name"].lower().endswith(e) for e in exts)])
        if "CopyHere" in script:
            dest = script.split("$dest = $sh.NameSpace(")[1].split(")")[0].strip("'")
            entries = script.split("foreach ($entry in (")[1].split(" -split")[0].strip("'")
            for e in entries.split(";"):
                rel, name = e.split("|", 1)
                f = next(x for x in self.files if x["rel"] == rel and x["name"] == name)
                from pathlib import Path
                (Path(dest) / name).write_bytes(b"x" * f["size"])
            return "ok"
        if "ConvertTo-Json" in script and "storage = $stores" in script:
            return json.dumps([{"name": "Galaxy A54", "storage": [
                {"name": "Internal storage", "folders": ["DCIM", "Pictures", "WhatsApp"]}]}])
        raise AssertionError("unexpected script")


@pytest.fixture
def phone(monkeypatch):
    files = [{"rel": "Camera/", "name": f"IMG_{i:03}.jpg", "size": 10 + i} for i in range(120)] + \
            [{"rel": "Camera/", "name": "VID_001.mp4", "size": 500},
             {"rel": "Screenshots/", "name": "Screenshot_1.png", "size": 7}]
    fake = FakePhone(files)
    monkeypatch.setattr(devices, "_ps", fake)
    devices._CACHE.clear()
    return fake


def test_phone_list(phone):
    out = devices.phone_list()
    assert "Galaxy A54" in out and "DCIM" in out


def test_phone_copy_in_batches_skips_existing_and_asks_first(pc, phone):
    (pc / "Pictures/Phone backup").mkdir(parents=True)
    (pc / "Pictures/Phone backup/IMG_000.jpg").write_bytes(b"x" * 10)        # already copied before
    events = []
    out, asked, _ = run("backup my phone photos", events=events)
    assert [a["tool"] for a in asked] == ["phone_copy"]
    card = asked[0]["preview"]
    assert "COPY 120 photos file(s)" in card and "(1 already copied - skipped)" in card
    assert "in 3 batch(es)" in card and "Nothing on the phone is deleted" in card
    got = names(pc / "Pictures/Phone backup")
    assert len(got) == 121 and "VID_001.mp4" not in got
    assert sum("CopyHere" in c for c in phone.calls) == 3                     # 50 + 50 + 20
    assert "Nothing was deleted on the phone" in out
    assert any(e["type"] == "status" and e["text"].startswith("Phone batch 3/3") for e in events)


def test_phone_copy_again_copies_nothing(pc, phone):
    run("copy photos and videos from my phone to Pictures/Phone")
    devices._CACHE.clear()
    out, asked, _ = run("copy photos and videos from my phone to Pictures/Phone")
    assert "already in Pictures/Phone" in out or "already" in out


def test_phone_deny_copies_nothing(pc, phone):
    run("backup my phone", decision="deny")
    assert not (pc / "Pictures/Phone backup").exists()
    assert not any("CopyHere" in c for c in phone.calls)


def test_no_phone_message_is_helpful(monkeypatch):
    def nophone(script, timeout=600):
        raise ValueError("NO_PHONE")
    monkeypatch.setattr(devices, "_ps", nophone)
    with pytest.raises(ValueError, match="File transfer"):
        devices.phone_list()


def test_phone_needs_windows():
    if devices.os.name != "nt":
        with pytest.raises(ValueError, match="Windows"):
            devices._ps("Get-Date")


def test_phone_names_are_quoted_safely():
    assert devices._q("Bob's phone") == "'Bob''s phone'"


@pytest.mark.parametrize("task,tool,kind", [
    ("copy photos from my phone to Pictures/Phone", "phone_copy", "photos"),
    ("backup my phone", "phone_copy", "media"),
    ("import photos and videos from my phone", "phone_copy", "media"),
    ("copy my phone camera photos", "phone_copy", "photos"),
    ("move videos off my samsung to Desktop", "phone_copy", "videos"),
    ("what's on my phone?", "phone_list", None),
    ("how much space is left on my drives", "drive_space", None),
    ("which drive is full", "drive_space", None)])
def test_device_phrases(pc, task, tool, kind):
    plan = rpa.quick_plan(task)
    assert plan[-1]["tool"] == tool
    if kind:
        assert plan[-1]["input"]["kind"] == kind


def test_moving_from_phone_still_only_copies(pc, phone):
    out, asked, _ = run("move videos off my phone to Desktop/Phone videos")
    assert asked[0]["tool"] == "phone_copy" and "Nothing on the phone is deleted" in asked[0]["preview"]
