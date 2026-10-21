"""Phone link: move files between this PC and a phone over Wi-Fi or Bluetooth.

Wi-Fi (fast, any Android/iPhone browser, no app to install)
  You switch it on (it asks you first). NestCrew then opens a *second*, separate little server on
  your Wi-Fi and shows a QR code. Scan it with the phone: a page opens where you can
  - send photos, videos or any file to the PC (they land in Downloads/From phone/<date>),
  - see each file verified (bytes received = size on the phone) and, once a backup is verified,
    a clear "safe to delete these from your Gallery" message - the clean-up path for a phone,
  - download files the PC shared with you (Desktop/Send to phone).
  Safety: only devices on your own network (private addresses) with the secret code in the QR
  can use it; it only writes into the inbox and only reads from the share folder; nothing is
  ever deleted or overwritten; every transfer is in the audit log; it switches itself off after
  30 idle minutes (or 3 hours) and whenever NestCrew closes. The main NestCrew app is still only
  reachable from this PC.

Bluetooth (slow, ~1-2 MB/s, but no Wi-Fi needed)
  NestCrew opens Windows' own Bluetooth File Transfer (fsquirt.exe) on its Send or Receive screen;
  you pick the phone there.
"""
from __future__ import annotations

import html
import ipaddress
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import load_config
from .rpa import _free_name, _size, resolve_path, show
from .safety import audit
from .tools import tool

STATE: dict = {"server": None, "thread": None, "token": None, "url": None, "port": None,
               "started": 0.0, "last": 0.0, "received": [], "downloads": 0}
LOCK = threading.Lock()


def _cfg() -> dict:
    c = load_config().get("phonelink", {}) or {}
    return {"port": int(c.get("port", 8766)), "inbox": c.get("inbox", "Downloads/From phone"),
            "share": c.get("share", "Desktop/Send to phone"), "idle_minutes": int(c.get("idle_minutes", 30)),
            "max_hours": float(c.get("max_hours", 3)), "max_file_gb": float(c.get("max_file_gb", 8))}


def inbox() -> Path:
    return resolve_path(_cfg()["inbox"])


def share() -> Path:
    return resolve_path(_cfg()["share"])


# ------------------------------------------------------------------ network --
def lan_ip() -> str | None:
    """This PC's address on the home Wi-Fi (a private 192.168.x / 10.x / 172.16-31.x address)."""
    cands = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))           # no packet is sent; just picks the route
            cands.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    for ip in cands:
        a = ipaddress.ip_address(ip)
        if _is_local_network(ip) and not a.is_loopback and not a.is_link_local:
            return ip
    return None


# Home/LAN ranges only (Python's is_private also says yes to some public test ranges - not used).
LAN_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                                               "127.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")]


def _is_local_network(host: str | None) -> bool:
    try:
        a = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in LAN_NETS if n.version == a.version)


def _safe_name(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name                  # never a path
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or f"file-{int(time.time())}"


def _qr_svg(text: str) -> str:
    try:
        import io

        import qrcode
        import qrcode.image.svg
        buf = io.BytesIO()
        qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2).save(buf)
        svg = buf.getvalue().decode()
        return svg[svg.index("<svg"):]
    except Exception:                                              # no qrcode package: address only
        return ""


# --------------------------------------------------------------- the server --
link_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@link_app.middleware("http")
async def _guard(request: Request, call_next):
    host = request.client.host if request.client else None
    if not _is_local_network(host):
        audit(event="phonelink_refused", client=str(host), why="not on your network")
        return JSONResponse({"detail": "Only devices on your own Wi-Fi can use this link."}, status_code=403)
    token = STATE.get("token")
    given = request.query_params.get("k") or request.headers.get("x-link-key") or ""
    if not token or not secrets.compare_digest(given, token):
        return JSONResponse({"detail": "This link is off or the code is wrong - scan the QR code again."},
                            status_code=403)
    STATE["last"] = time.time()
    resp = await call_next(request)
    resp.headers.update({"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"})
    return resp


@link_app.get("/")
def _page(k: str = ""):
    return HTMLResponse(PHONE_PAGE.replace("__KEY__", html.escape(k)).replace("__PC__", html.escape(socket.gethostname())))


@link_app.get("/check")
def _check(name: str, size: int = -1):
    """Already on the PC (same name and size)? Then the phone skips sending it again."""
    n = _safe_name(name)
    root = inbox()
    if root.exists():
        for p in root.rglob(n):
            try:
                if p.is_file() and (size < 0 or p.stat().st_size == size):
                    return {"have": True, "where": show(p)}
            except OSError:
                pass
    return {"have": False}


@link_app.post("/upload")
async def _upload(request: Request, name: str, size: int = -1):
    cfg = _cfg()
    if size > cfg["max_file_gb"] * (1 << 30):
        raise HTTPException(413, f"Files over {cfg['max_file_gb']:g} GB aren't accepted.")
    day = inbox() / time.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    free = __import__("shutil").disk_usage(day).free
    if size > 0 and free - size < (1 << 30):
        raise HTTPException(507, f"Not enough space on the PC ({_size(free)} free).")
    final = _free_name(day, _safe_name(name))
    part = final.with_name(final.name + ".part")
    got = 0
    try:
        with open(part, "wb") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > cfg["max_file_gb"] * (1 << 30):
                    raise HTTPException(413, "File too big.")
                f.write(chunk)
        if size >= 0 and got != size:
            raise HTTPException(400, f"Only {got} of {size} bytes arrived - send it again.")
        os.replace(part, final)
    finally:
        if part.exists():
            part.unlink()
    rec = {"name": final.name, "size": got, "where": show(final), "verified": size == got, "at": time.time()}
    with LOCK:
        STATE["received"].append(rec)
    audit(event="phonelink_received", file=show(final), bytes=got, verified=size == got)
    return rec


@link_app.get("/files")
def _files():
    root = share()
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(root.iterdir()):
        if p.is_file() and not p.name.startswith((".", "~$")) and p.name.lower() != "desktop.ini":
            out.append({"name": p.name, "size": p.stat().st_size, "human": _size(p.stat().st_size)})
    return out


@link_app.get("/get/{name}")
def _get(name: str):
    root = share().resolve()
    p = (root / _safe_name(name)).resolve()
    if p.parent != root or not p.is_file():
        raise HTTPException(404, "Not in the 'Send to phone' folder.")
    STATE["downloads"] += 1
    audit(event="phonelink_sent", file=show(p), bytes=p.stat().st_size)
    return FileResponse(p, filename=p.name)


@link_app.get("/received")
def _received():
    with LOCK:
        recs = list(STATE["received"])
    return {"count": len(recs), "bytes": sum(r["size"] for r in recs), "human": _size(sum(r["size"] for r in recs)),
            "all_verified": all(r["verified"] for r in recs), "files": recs[-200:]}


# ------------------------------------------------------------- start / stop --
def status() -> dict:
    on = bool(STATE.get("token"))
    with LOCK:
        recs = list(STATE["received"])
    out = {"on": on, "url": STATE.get("url") if on else None, "share": show(share()), "inbox": show(inbox()),
           "received": len(recs), "received_bytes": _size(sum(r["size"] for r in recs)),
           "recent": [r["where"] for r in recs[-8:]], "downloads": STATE.get("downloads", 0)}
    if on:
        cfg = _cfg()
        out["stops_in_min"] = max(0, round(min(STATE["last"] + cfg["idle_minutes"] * 60,
                                               STATE["started"] + cfg["max_hours"] * 3600) - time.time()) // 60)
        out["qr"] = _qr_svg(STATE["url"])
    return out


WATCH_EVERY = 5.0


def _watchdog():
    cfg = _cfg()
    token = STATE.get("token")
    while STATE.get("token") == token and token:
        time.sleep(WATCH_EVERY)
        now = time.time()
        if now - STATE["last"] > cfg["idle_minutes"] * 60 or now - STATE["started"] > cfg["max_hours"] * 3600:
            audit(event="phonelink_auto_stop")
            stop()
            return


def start(host: str | None = None) -> dict:
    if STATE.get("token"):
        return status()
    import uvicorn
    cfg = _cfg()
    ip = host or lan_ip()
    if not ip:
        raise ValueError("This PC isn't on a Wi-Fi/home network right now - connect the PC and the phone "
                         "to the same Wi-Fi (or the phone's hotspot) and try again.")
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", cfg["port"])) == 0:
            raise ValueError(f"Port {cfg['port']} is busy - set phonelink.port in config.yaml to another number.")
    inbox().mkdir(parents=True, exist_ok=True)
    share().mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    server = uvicorn.Server(uvicorn.Config(link_app, host="0.0.0.0", port=cfg["port"], log_level="warning",
                                           access_log=False))
    t = threading.Thread(target=server.run, daemon=True, name="phonelink")
    STATE.update(server=server, thread=t, token=token, port=cfg["port"], url=f"http://{ip}:{cfg['port']}/?k={token}",
                 started=time.time(), last=time.time(), received=[], downloads=0)
    t.start()
    for _ in range(50):                                  # wait until it's listening
        if server.started:
            break
        time.sleep(0.1)
    threading.Thread(target=_watchdog, daemon=True).start()
    audit(event="phonelink_start", address=f"{ip}:{cfg['port']}")
    return status()


def stop() -> None:
    server = STATE.get("server")
    STATE["token"] = None                                # codes stop working at once
    if server is not None:
        server.should_exit = True
        t = STATE.get("thread")
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
    STATE.update(server=None, thread=None, url=None)
    audit(event="phonelink_stop")


# -------------------------------------------------------------------- tools --
@tool("phone_link_start", "Start the Wi-Fi phone link so a phone on the same Wi-Fi can send files to this PC "
      "and download shared files (QR code on the Phone page). Asks you first.", "{}")
def phone_link_start() -> str:
    s = start()
    return (f"Phone link is on. On the phone, open the camera and scan the QR code on NestCrew's Phone page "
            f"(sidebar 📱), or type this address in the phone's browser:\n{s['url']}\n"
            f"Files from the phone go to {s['inbox']}; files in {s['share']} can be downloaded on the phone. "
            f"It switches off after {_cfg()['idle_minutes']} idle minutes. If the phone can't open it, allow "
            "NestCrew/Python on *private* networks when Windows Firewall asks.")


@tool("phone_link_stop", "Switch the Wi-Fi phone link off.", "{}")
def phone_link_stop() -> str:
    was = bool(STATE.get("token"))
    stop()
    return "Phone link is off." if was else "Phone link was already off."


@tool("phone_link_status", "Is the phone link on, and what has the phone sent so far (read-only).", "{}")
def phone_link_status() -> str:
    s = status()
    lines = [f"Phone link: {'ON - ' + s['url'] if s['on'] else 'off'}",
             f"Received from the phone: {s['received']} file(s), {s['received_bytes']} (in {s['inbox']})"]
    lines += [f"  {x}" for x in s["recent"]]
    lines.append(f"Shared for the phone: {len(_files())} file(s) in {s['share']}; downloaded {s['downloads']} time(s)")
    return "\n".join(lines)


def _stage(source, pattern: str, recursive: bool) -> list[str]:
    from .rpa import _flag, _same_file, gather
    import shutil
    _, files = gather(source, pattern, _flag(recursive))
    dst = share()
    dst.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        if dst in f.parents:
            continue
        if _same_file(f, dst / f.name):
            out.append(f.name)
            continue
        target = _free_name(dst, f.name)
        shutil.copy2(f, target)
        out.append(target.name)
    return out


@tool("send_to_phone", "Put copies of files into the 'Send to phone' folder so the phone can download them "
      "over the Wi-Fi link (or Bluetooth). pattern like *.pdf; asks you first.",
      '{"source": "Desktop/Reports", "pattern": "*.pdf"}')
def send_to_phone(source: str, pattern: str = "*", recursive: bool = False) -> str:
    names = _stage(source, pattern, recursive)
    if not names:
        return f"No files matching '{pattern}' to share."
    on = bool(STATE.get("token"))
    return (f"Ready for the phone: {len(names)} file(s) in {show(share())}: {', '.join(names[:10])}"
            + ("" if len(names) <= 10 else f" … +{len(names) - 10}") +
            (". Open the Phone link page on the phone and tap them." if on else
             ". Start the phone link (or Bluetooth) to send them."))


def _fsquirt(mode: str) -> None:
    if os.name != "nt":
        raise ValueError("Bluetooth file transfer uses Windows' built-in Bluetooth wizard (Windows only).")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "fsquirt.exe"
    if not exe.exists():
        raise ValueError("This PC has no Bluetooth file transfer (fsquirt.exe) - is Bluetooth available?")
    subprocess.Popen([str(exe), f"-{mode}"], close_fds=True)


@tool("bluetooth_send", "Send files to the phone by Bluetooth: copies them to 'Send to phone' and opens "
      "Windows' Bluetooth Send screen. Slow (~1-2 MB/s) - prefer the Wi-Fi link for many photos. Asks you first.",
      '{"source": "Desktop/report.pdf"}')
def bluetooth_send(source: str = "", pattern: str = "*") -> str:
    names = _stage(source, pattern, False) if source else []
    _fsquirt("send")
    return ("Windows' Bluetooth window is open: turn on Bluetooth on the phone, pick it in the list, then "
            f"Browse → {share()} and select " + (f"{len(names)} file(s): {', '.join(names[:6])}" if names else "the files") +
            ". Accept on the phone when it asks.")


@tool("bluetooth_receive", "Receive files from the phone by Bluetooth: opens Windows' Bluetooth Receive screen. "
      "Asks you first.", "{}")
def bluetooth_receive() -> str:
    _fsquirt("receive")
    return ("Windows is waiting for files by Bluetooth. On the phone: select the photos/files → Share → Bluetooth → "
            f"pick this PC ({socket.gethostname()}). When it finishes, Windows asks where to save - choose "
            f"{inbox()} so NestCrew can sort and clean them.")


def preview(tool_name: str, args: dict) -> str | None:
    if tool_name == "phone_link_start":
        ip = lan_ip() or "your Wi-Fi address"
        cfg = _cfg()
        return (f"Open a private file link on your Wi-Fi at http://{ip}:{cfg['port']}\n\n"
                "• Only devices on your own network that scan the QR code (secret code) can use it\n"
                f"• Phone → PC files go to {cfg['inbox']}; PC → phone files come only from {cfg['share']}\n"
                "• Nothing is deleted or overwritten; every transfer is logged\n"
                f"• It switches off after {cfg['idle_minutes']} idle minutes, or when you say 'stop phone link'")
    if tool_name in ("send_to_phone", "bluetooth_send") and args.get("source"):
        from .rpa import _flag, gather
        _, files = gather(args["source"], args.get("pattern") or "*", _flag(args.get("recursive", False)))
        lines = "\n".join(f"  • {f.name}  ({_size(f.stat().st_size)})" for f in files[:15])
        how = "by Bluetooth (Windows' Send window opens)" if tool_name == "bluetooth_send" else "over the Wi-Fi link"
        return (f"SHARE {len(files)} file(s) with the phone {how}\ncopies go to {show(share())} - the originals stay\n\n{lines}")
    if tool_name == "bluetooth_receive":
        return "Open Windows' Bluetooth Receive window, so the phone can send files to this PC."
    if tool_name == "bluetooth_send":
        return "Open Windows' Bluetooth Send window."
    return None


# ------------------------------------------------------------- phone page --
PHONE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NestCrew · Phone link</title>
<style>
:root{--bg:#f6f4ef;--card:#fff;--ink:#1f1d1a;--mut:#6b675f;--acc:#c4532d;--ok:#2f7d4f;--line:#e4e0d6}
@media(prefers-color-scheme:dark){:root{--bg:#1b1a18;--card:#262421;--ink:#ece9e2;--mut:#a7a299;--line:#3a3733}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 2px}h2{font-size:17px;margin:0 0 8px}
.sub{color:var(--mut);font-size:14px;margin:0 0 14px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0}
.big{display:block;width:100%;padding:14px;border:0;border-radius:12px;background:var(--acc);color:#fff;font-size:17px;font-weight:600;text-align:center}
.alt{background:transparent;color:var(--ink);border:1px solid var(--line);margin-top:8px}
input[type=file]{display:none}.row{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-top:1px solid var(--line);font-size:14px}
.row:first-child{border-top:0}.nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.st{color:var(--mut);white-space:nowrap}
.ok{color:var(--ok)}.err{color:#c0392b}.bar{height:6px;background:var(--line);border-radius:4px;overflow:hidden;margin:10px 0 4px}
.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .2s}
.safe{display:none;background:#e6f2ea;color:#1f5c38;border-radius:12px;padding:12px;margin-top:10px;font-size:15px}
@media(prefers-color-scheme:dark){.safe{background:#1f3a2a;color:#bfe5cc}}a{color:var(--acc)}
</style></head><body><main>
<h1>📲 NestCrew Phone link</h1><p class="sub">Connected to <b>__PC__</b> over your Wi-Fi. Keep this page open while files move.</p>

<section class="card"><h2>Send to the PC</h2>
<p class="sub">Photos, videos or any file. Already-sent files are skipped, so you can send the same album again safely.</p>
<label class="big" for="media">Choose photos &amp; videos</label><input id="media" type="file" accept="image/*,video/*" multiple>
<label class="big alt" for="any">Choose any files</label><input id="any" type="file" multiple>
<div class="bar"><i id="bar"></i></div><div class="st" id="sum">Nothing sent yet.</div>
<div class="safe" id="safe"></div><div id="list"></div></section>

<section class="card"><h2>From the PC</h2><p class="sub">Files put in "Send to phone" on the PC. Tap to download.</p>
<div id="files" class="st">Loading…</div></section>
<p class="sub">Free up phone space: after the green ✓ "safe to delete" message, delete those items in your Gallery app — NestCrew never deletes anything on the phone.</p>
</main><script>
const K="__KEY__", q=(p)=>p+(p.includes("?")?"&":"?")+"k="+encodeURIComponent(K);
const $=s=>document.querySelector(s); let total=0, done=0, okCount=0, okBytes=0, failed=0, running=false; const queue=[];
function human(n){const u=["B","KB","MB","GB"];let i=0;while(n>=1024&&i<3){n/=1024;i++}return (i?n.toFixed(1):n)+" "+u[i]}
function row(name){const d=document.createElement("div");d.className="row";d.innerHTML='<span class="nm"></span><span class="st">waiting</span>';d.firstChild.textContent=name;$("#list").prepend(d);return d.lastChild}
function add(files){for(const f of files){queue.push([f,row(f.name)]);total++} upd(); if(!running) next()}
function upd(){$("#bar").style.width=(total?Math.round(100*done/total):0)+"%";$("#sum").textContent=total?`${done} of ${total} done · ${okCount} verified (${human(okBytes)})`+(failed?` · ${failed} failed`:""):"Nothing sent yet.";
  const s=$("#safe"); if(total&&done===total){ s.style.display="block";
    s.textContent=failed?`${failed} file(s) failed — choose them again to resend; don't delete those yet.`
      :`✓ All ${okCount} file(s) are safely on the PC and verified (sizes match). You can delete them from your phone's Gallery now.`; }
  else s.style.display="none"}
async function next(){const it=queue.shift(); if(!it){running=false;return} running=true; const [f,st]=it;
  try{const c=await fetch(q("/check?name="+encodeURIComponent(f.name)+"&size="+f.size)).then(r=>r.json());
    if(c.have){st.textContent="already on PC ✓";st.className="st ok";okCount++;okBytes+=f.size}
    else{await new Promise((res,rej)=>{const x=new XMLHttpRequest();x.open("POST",q("/upload?name="+encodeURIComponent(f.name)+"&size="+f.size));
      x.upload.onprogress=e=>{if(e.lengthComputable)st.textContent=Math.round(100*e.loaded/e.total)+"%"};
      x.onload=()=>{if(x.status===200){const r=JSON.parse(x.responseText); if(r.verified){st.textContent="✓ "+human(r.size);st.className="st ok";okCount++;okBytes+=r.size} else {st.textContent="sent (unverified)"} res()} else {rej(new Error((JSON.parse(x.responseText||"{}").detail)||x.status))}};
      x.onerror=()=>rej(new Error("connection lost")); x.send(f)})}
  }catch(e){st.textContent="✗ "+e.message;st.className="st err";failed++}
  done++; upd(); next()}
$("#media").onchange=e=>{add(e.target.files);e.target.value=""}; $("#any").onchange=e=>{add(e.target.files);e.target.value=""};
async function files(){try{const l=await fetch(q("/files")).then(r=>r.json());
  $("#files").innerHTML=l.length?"":"Nothing shared yet."; for(const f of l){const d=document.createElement("div");d.className="row";
  const a=document.createElement("a");a.href=q("/get/"+encodeURIComponent(f.name));a.textContent=f.name;a.className="nm";a.setAttribute("download",f.name);
  const s=document.createElement("span");s.className="st";s.textContent=f.human;d.append(a,s);$("#files").append(d)}}
  catch(e){$("#files").textContent="The link is off — scan the QR code on the PC again."}}
files(); setInterval(files,15000);
</script></body></html>"""
