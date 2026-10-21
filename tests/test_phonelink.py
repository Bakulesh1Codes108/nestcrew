"""Wi-Fi phone link and Bluetooth: security first, then transfers, tools and phrasing."""
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nestcrew import phonelink, rpa
from nestcrew.agent import Agent
from nestcrew.safety import Gate
from tests.test_core import FakeModel, offline  # noqa: F401

PHONE = ("192.168.1.23", 51000)          # a phone on the home Wi-Fi


@pytest.fixture
def pc(monkeypatch, tmp_path, tmp_path_factory):
    h = tmp_path_factory.mktemp("home")
    for d in ("Desktop/Reports", "Downloads", "Pictures"):
        (h / d).mkdir(parents=True)
    (h / "Desktop/Reports/q3.pdf").write_bytes(b"%PDF q3")
    (h / "Desktop/Reports/notes.txt").write_text("hi")
    monkeypatch.setattr(rpa, "_home", lambda: h)
    from nestcrew.config import load_config
    monkeypatch.setitem(load_config(), "rpa", {"folders": ["desktop", "downloads", "pictures"]})
    monkeypatch.setitem(load_config(), "phonelink", {"port": _free_port()})
    monkeypatch.setattr(rpa, "_undo_path", lambda: tmp_path / "undo.json")
    phonelink.STATE.update(token=None, received=[], downloads=0)
    yield h
    phonelink.stop()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def link(pc):
    phonelink.STATE.update(token="secret-code", started=time.time(), last=time.time())
    return TestClient(phonelink.link_app, base_url="http://192.168.1.10:8766", client=PHONE)


K = {"k": "secret-code"}


# ------------------------------------------------------------------ security --
def test_off_by_default(pc):
    c = TestClient(phonelink.link_app, client=PHONE)
    assert c.get("/", params=K).status_code == 403


def test_wrong_or_missing_code_is_refused(link):
    assert link.get("/").status_code == 403
    assert link.get("/", params={"k": "guess"}).status_code == 403
    assert link.get("/files", params={"k": "secret-codeX"}).status_code == 403


@pytest.mark.parametrize("ip", ["8.8.8.8", "203.0.113.9", "192.0.2.1", "100.64.0.5", "::ffff:8.8.8.8", "2001:db8::1", "testclient"])
def test_devices_outside_your_network_are_refused_even_with_the_code(pc, ip):
    phonelink.STATE.update(token="secret-code", started=time.time(), last=time.time())
    c = TestClient(phonelink.link_app, client=(ip, 5000))
    r = c.get("/", params=K)
    assert r.status_code == 403 and "own Wi-Fi" in r.json()["detail"]


def test_page_escapes_the_code(link):
    r = link.get("/", params={"k": "secret-code"})
    assert r.status_code == 200 and "Phone link" in r.text and "no-store" in r.headers["cache-control"]
    assert "frame" in r.headers["x-frame-options"].lower() or r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("bad,expected", [("../../Windows/evil.exe", "evil.exe"), ("C:\\x\\a.jpg", "a.jpg"),
                                          ('we<ird>:na"me?.jpg', "we_ird__na_me_.jpg"), ("", None)])
def test_uploads_can_only_land_in_the_inbox(link, pc, bad, expected):
    r = link.post("/upload", params={**K, "name": bad, "size": 3}, content=b"abc")
    assert r.status_code == 200
    saved = [p for p in (pc / "Downloads/From phone").rglob("*") if p.is_file()]
    assert len(saved) == 1 and saved[0].read_bytes() == b"abc"
    if expected:
        assert saved[0].name == expected
    assert not (pc / "Windows").exists()


def test_downloads_only_from_the_share_folder(link, pc):
    (pc / "Desktop/Send to phone").mkdir(parents=True)
    (pc / "Desktop/Send to phone/menu.pdf").write_bytes(b"menu")
    assert link.get("/get/menu.pdf", params=K).content == b"menu"
    for bad in ("../Reports/q3.pdf", "..%2FReports%2Fq3.pdf", "nope.pdf"):
        assert link.get("/get/" + bad, params=K).status_code == 404


# ----------------------------------------------------------------- transfers --
def test_upload_is_verified_and_never_overwrites(link, pc):
    for _ in range(2):
        r = link.post("/upload", params={**K, "name": "IMG_1.jpg", "size": 5}, content=b"12345")
        assert r.json()["verified"] is True
    day = next((pc / "Downloads/From phone").iterdir())
    assert sorted(p.name for p in day.iterdir()) == ["IMG_1 (2).jpg", "IMG_1.jpg"]
    s = link.get("/received", params=K).json()
    assert s["count"] == 2 and s["all_verified"] and s["bytes"] == 10


def test_already_sent_files_are_skipped_by_the_phone(link):
    assert link.get("/check", params={**K, "name": "IMG_9.jpg", "size": 4}).json()["have"] is False
    link.post("/upload", params={**K, "name": "IMG_9.jpg", "size": 4}, content=b"abcd")
    assert link.get("/check", params={**K, "name": "IMG_9.jpg", "size": 4}).json()["have"] is True
    assert link.get("/check", params={**K, "name": "IMG_9.jpg", "size": 5}).json()["have"] is False   # different file


def test_cut_off_upload_leaves_nothing_behind(link, pc):
    r = link.post("/upload", params={**K, "name": "VID.mp4", "size": 100}, content=b"only-part")
    assert r.status_code == 400 and "send it again" in r.json()["detail"]
    assert not [p for p in (pc / "Downloads/From phone").rglob("*") if p.is_file()]


def test_too_big_or_no_space_is_refused(link, monkeypatch):
    assert link.post("/upload", params={**K, "name": "huge.mp4", "size": 9 * (1 << 30)}, content=b"x").status_code == 413

    class DU:
        free = 1 << 29
    monkeypatch.setattr("shutil.disk_usage", lambda p: DU)
    assert link.post("/upload", params={**K, "name": "a.mp4", "size": 1 << 20}, content=b"x").status_code == 507


# --------------------------------------------------- real server start / stop --
def test_real_start_and_stop(pc, monkeypatch):
    monkeypatch.setattr(phonelink, "lan_ip", lambda: "127.0.0.1")
    s = phonelink.start()
    assert s["on"] and "?k=" in s["url"]
    port = phonelink.STATE["port"]
    token = phonelink.STATE["token"]
    r = httpx.get(f"http://127.0.0.1:{port}/", params={"k": token}, timeout=5)
    assert r.status_code == 200 and "Send to the PC" in r.text
    up = httpx.post(f"http://127.0.0.1:{port}/upload", params={"k": token, "name": "p.jpg", "size": 3},
                    content=b"xyz", timeout=5)
    assert up.json()["verified"] and phonelink.status()["received"] == 1
    phonelink.stop()
    assert not phonelink.status()["on"]
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{port}/", params={"k": token}, timeout=2)


def test_auto_stop_when_idle(pc, monkeypatch):
    monkeypatch.setattr(phonelink, "lan_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(phonelink, "WATCH_EVERY", 0.05)
    from nestcrew.config import load_config
    load_config()["phonelink"]["idle_minutes"] = 0
    phonelink.start()
    for _ in range(100):
        if not phonelink.STATE.get("token"):
            break
        time.sleep(0.05)
    assert not phonelink.status()["on"]


def test_no_wifi_gives_a_clear_message(pc, monkeypatch):
    monkeypatch.setattr(phonelink, "lan_ip", lambda: None)
    with pytest.raises(ValueError, match="same Wi-Fi"):
        phonelink.start()


def test_qr_code_is_made_when_on(pc, monkeypatch):
    monkeypatch.setattr(phonelink, "lan_ip", lambda: "127.0.0.1")
    s = phonelink.start()
    assert s["qr"].startswith("<svg")


# --------------------------------------------------------------- tools / agent --
def test_send_to_phone_copies_and_keeps_originals(pc):
    out = phonelink.send_to_phone("Desktop/Reports", "*.pdf")
    assert "1 file(s)" in out and (pc / "Desktop/Send to phone/q3.pdf").read_bytes() == b"%PDF q3"
    assert (pc / "Desktop/Reports/q3.pdf").exists()
    phonelink.send_to_phone("Desktop/Reports", "*.pdf")                   # again: identical, not duplicated
    assert sorted(p.name for p in (pc / "Desktop/Send to phone").iterdir()) == ["q3.pdf"]


def test_agent_send_reports_to_phone_asks_twice_then_link_is_on(pc, monkeypatch):
    monkeypatch.setattr(phonelink, "lan_ip", lambda: "127.0.0.1")
    asked = []
    out = Agent().run(FakeModel([]), "send Reports to my phone", gate=Gate(ask_fn=lambda r: asked.append(r) or "once"))
    assert [a["tool"] for a in asked] == ["send_to_phone", "phone_link_start"]
    assert "SHARE 2 file(s)" in asked[0]["preview"] and "only devices on your own network" in asked[1]["preview"].lower()
    assert phonelink.status()["on"] and "Phone link is on" in out


def test_agent_deny_keeps_link_off(pc):
    Agent().run(FakeModel([]), "connect my phone over wifi", gate=Gate(ask_fn=lambda r: "deny"))
    assert not phonelink.status()["on"]


def test_bluetooth_needs_windows_or_opens_the_wizard(pc, monkeypatch):
    if phonelink.os.name != "nt":
        with pytest.raises(ValueError, match="Windows"):
            phonelink.bluetooth_receive()
    opened = []
    monkeypatch.setattr(phonelink, "_fsquirt", lambda mode: opened.append(mode))
    assert "Bluetooth" in phonelink.bluetooth_receive() and opened == ["receive"]
    out = phonelink.bluetooth_send("Desktop/Reports", "*.pdf")
    assert opened == ["receive", "send"] and "q3.pdf" in out and (pc / "Desktop/Send to phone/q3.pdf").exists()


def test_main_app_endpoints(pc, monkeypatch, tmp_path):
    from nestcrew import memory, server
    monkeypatch.setattr(memory, "resolve", lambda p: tmp_path / p.replace("./", ""))
    monkeypatch.setattr(phonelink, "lan_ip", lambda: "127.0.0.1")
    c = TestClient(server.app, base_url="http://127.0.0.1")
    H = {"x-nestcrew-token": server.TOKEN}
    assert c.get("/api/phonelink", headers=H).json()["on"] is False
    assert c.post("/api/phonelink/start").status_code in (401, 403)                 # needs the app token
    assert c.post("/api/phonelink/start", headers=H, json={}).json()["on"] is True
    assert c.post("/api/phonelink/stop", headers=H, json={}).json()["on"] is False


@pytest.mark.parametrize("task,tools", [
    ("connect my phone over wifi", ["phone_link_start"]),
    ("share files between my phone and laptop", ["phone_link_start"]),
    ("receive files from my phone", ["phone_link_start"]),
    ("get photos from my phone over wifi", ["phone_link_start"]),
    ("send Reports to my phone", ["send_to_phone", "phone_link_start"]),
    ("send Desktop/Reports to my phone via bluetooth", ["bluetooth_send"]),
    ("receive photos from my phone via bluetooth", ["bluetooth_receive"]),
    ("send files to my phone by bluetooth", ["bluetooth_send"]),
    ("stop phone link", ["phone_link_stop"]),
    ("disconnect my phone", ["phone_link_stop"]),
    ("what did my phone send", ["phone_link_status"]),
    ("copy photos from my phone to Pictures/Phone", ["phone_list", "phone_copy"]),   # USB cable path unchanged
])
def test_phrases(pc, task, tools):
    assert [s["tool"] for s in rpa.quick_plan(task)] == tools


@pytest.mark.parametrize("ip", ["192.168.0.12", "10.1.2.3", "172.20.0.4", "127.0.0.1", "fe80::1%12", "::ffff:192.168.1.5"])
def test_home_network_addresses_are_allowed(ip):
    assert phonelink._is_local_network(ip)


def test_lan_ip_picks_a_home_address(monkeypatch):
    monkeypatch.setattr(phonelink.socket, "gethostbyname_ex", lambda h: ("pc", [], ["192.0.2.2", "169.254.3.4", "192.168.1.10"]))

    class S:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def connect(self, a): raise OSError
    monkeypatch.setattr(phonelink.socket, "socket", S)
    assert phonelink.lan_ip() == "192.168.1.10"
