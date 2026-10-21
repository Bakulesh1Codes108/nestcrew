"""Updater: new program files in, your settings / chats / workflows untouched, nothing outside the app."""
import io
import zipfile

import pytest

from nestcrew import updater


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr("nestcrew-main/" + name, data)
    return buf.getvalue()


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "NestCrew"
    for rel, data in {"nestcrew/server.py": "old server", "nestcrew/rpa.py": "same", "config.yaml": "model: mine",
                      ".env": "KEY=secret", "data/memory.db": "chats", "workspace/a.txt": "mine",
                      "workflows/my-bot.json": "my bot", "workflows/daily-brief.json": "edited by me"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)
    return root


def test_apply_updates_code_and_keeps_your_things(install):
    z = make_zip({"nestcrew/server.py": "new server", "nestcrew/rpa.py": "same", "nestcrew/voice.py": "voice",
                  "config.yaml": "model: default", ".env": "KEY=", "data/memory.db": "empty",
                  "workflows/daily-brief.json": "template", "workflows/new-template.json": "new",
                  "Update NestCrew.bat": "bat"})
    r = updater.apply(z, install)
    assert r["changed"] == ["nestcrew/server.py"]
    assert set(r["added"]) == {"nestcrew/voice.py", "workflows/new-template.json", "Update NestCrew.bat"}
    assert (install / "nestcrew/server.py").read_text() == "new server"
    assert (install / "config.yaml").read_text() == "model: mine"
    assert (install / ".env").read_text() == "KEY=secret"
    assert (install / "data/memory.db").read_text() == "chats"
    assert (install / "workflows/daily-brief.json").read_text() == "edited by me"
    assert (install / "workflows/my-bot.json").read_text() == "my bot"


def test_refuses_paths_outside_the_app(install, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nestcrew-main/nestcrew/server.py", "x")
        z.writestr("nestcrew-main/../../evil.txt", "x")
    updater.apply(buf.getvalue(), install)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()


def test_refuses_something_that_isnt_nestcrew(install):
    with pytest.raises(ValueError, match="doesn't look like NestCrew"):
        updater.apply(make_zip({"README.md": "hi"}), install)


@pytest.mark.parametrize("repo", ["evil.com/x", "a/b/c", "owner/na me", "../x"])
def test_repo_setting_is_validated(monkeypatch, repo):
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "update", {"repo": repo})
    with pytest.raises(ValueError):
        updater._repo()


def test_download_only_from_github(monkeypatch):
    import httpx

    class R:
        def __init__(self, host):
            self.url = httpx.URL(f"https://{host}/x.zip")
            self.content = b"zip"
        def raise_for_status(self):
            pass

    class C:
        host = "codeload.github.com"
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return R(C.host)
    monkeypatch.setattr(httpx, "Client", C)
    assert updater.download("Bakulesh1Codes108/nestcrew") == b"zip"
    C.host = "evil.example.com"
    with pytest.raises(ValueError, match="unexpected host"):
        updater.download("Bakulesh1Codes108/nestcrew")
