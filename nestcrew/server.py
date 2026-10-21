"""Local web app: http://127.0.0.1:8765 (never exposed beyond your laptop)."""
from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import memory, traces
from .agent import Agent
from .config import api_key, load_config, resolve
from .crew import list_crews, load_crew, run_crew
from .net import is_online
from .providers import OllamaProvider, ProviderError
from .router import CLOUD, get_provider
from .safety import Gate, Stopped, audit
from .tools import available, workspace

app = FastAPI(title="NestCrew", docs_url=None, redoc_url=None, openapi_url=None)
STATIC = Path(__file__).parent / "static"

# A fresh secret each launch. The page gets it; other websites open in your
# browser don't, so they can't drive NestCrew or approve actions.
TOKEN = secrets.token_urlsafe(24)
RUNS: dict[str, dict] = {}


def _code_sig() -> tuple:
    """Fingerprint of the app's own code: changes when NestCrew is updated on disk."""
    files = sorted(Path(__file__).parent.glob("*.py")) + [STATIC / "index.html"]
    return tuple((f.name, f.stat().st_size, int(f.stat().st_mtime)) for f in files if f.exists())


START_SIG = _code_sig()


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):          # blocks DNS-rebinding tricks
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.url.path.startswith("/api/") and \
            not secrets.compare_digest(request.headers.get("x-nestcrew-token", ""), TOKEN):
        return JSONResponse({"detail": "missing or wrong token"}, status_code=403)
    return await call_next(request)


class ChatIn(BaseModel):
    message: str
    images: list[str] = []
    provider: str | None = None
    session: str = "default"
    allow_screen: bool = False
    allow_commands: bool = False


class DecisionIn(BaseModel):
    run: str
    id: str
    decision: str          # once | task | deny


class StopIn(BaseModel):
    run: str


class AssignIn(BaseModel):
    task: str
    model: str


class ModelIn(BaseModel):
    model: str


class AutoIn(BaseModel):
    name: str
    enabled: bool = True


class FeedbackIn(BaseModel):
    lesson_id: int
    good: bool


class CrewIn(BaseModel):
    crew: str
    inputs: dict = {}
    provider: str | None = None


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__NESTCREW_TOKEN__", TOKEN),
                        headers={"Cache-Control": "no-store",
                                 "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                 "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'none'"})


@app.get("/api/status")
def status():
    cfg = load_config()
    local = OllamaProvider(cfg.get("local", {}))
    return {
        "online": is_online(),
        "mode": cfg.get("mode", "auto"),
        "local_model": local.model,
        "installed_models": local.installed_models(),
        "cloud_keys": {c: bool(api_key(c)) for c in CLOUD},
        "tools": [t.name for t in available()],
        "crews": list_crews(),
        "update_ready": _code_sig() != START_SIG,      # new code on disk -> offer Restart
    }


@app.post("/api/restart")
def restart():
    """Start a fresh NestCrew (new code) and close this one. The window reconnects by itself."""
    if RUNS:
        raise HTTPException(409, "A task is still running - stop it or wait, then restart.")
    import os
    import subprocess
    import sys
    audit(event="restart")
    flags = 0x00000010 if sys.platform == "win32" else 0          # CREATE_NEW_CONSOLE
    cwd = str(Path(__file__).resolve().parent.parent)
    code = ("import traceback\ntry:\n    from nestcrew.server import main\n    main(wait_port=True)\n"
            "except SystemExit:\n    raise\nexcept BaseException:\n    traceback.print_exc()\n"
            "    input('NestCrew stopped with a problem (details above). Press Enter to close.')")
    subprocess.Popen([sys.executable, "-c", code],
                     cwd=cwd, creationflags=flags, start_new_session=sys.platform != "win32")
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"ok": True}


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev)}\n\n"


@app.post("/api/chat")
def chat(body: ChatIn):
    try:
        prov = get_provider(body.provider, task="chat")
    except ProviderError as e:
        raise HTTPException(400, str(e))
    msgs = [{"role": "system", "content": "You are NestCrew, a helpful assistant running "
             "on the user's own laptop. Be concise. If an image is attached, answer about that image. "
             "NestCrew CAN work on the user's files and folders, but only in Agent mode: if they ask you to "
             "move, copy, clean, sort or delete files, never say you can't access them - say "
             "\"Press Switch to Agent (or pick Agent) and I'll do it, asking you before each change.\""}]
    msgs += memory.history(body.session)
    images = list(body.images)
    if images:
        memory.save_image(body.session, images[0])           # remember the photo for follow-ups
    elif memory.has_image(body.session):
        images = [memory.last_image(body.session)]           # follow-up about the same photo
    msgs.append({"role": "user", "content": body.message, "images": images})
    memory.add(body.session, "user", body.message + (" [image]" if body.images else ""))  # saved even if the reply fails

    def gen():
        full = []
        yield _sse({"type": "status", "text": "Loading the model..." if images else "Thinking..."})
        try:
            for piece in prov.stream(msgs):
                full.append(piece)
                yield _sse({"type": "token", "text": piece})
            answer = "".join(full).strip()
            if not answer:
                raise ProviderError("The model returned an empty answer - try again, or pick another model.")
            memory.add(body.session, "assistant", answer)
            yield _sse({"type": "done", "model": getattr(prov, "last_used", prov.label())})
        except Exception as e:                                # never leave the chat hanging
            msg = str(e) if isinstance(e, ProviderError) else f"Something went wrong: {e}"
            if full:
                memory.add(body.session, "assistant", "".join(full) + " [cut off]")
            audit(event="chat_error", session=body.session, error=msg[:300])
            yield _sse({"type": "error", "text": msg})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/session/{sid}/delete")
def session_delete(sid: str):
    memory.clear(sid)
    return {"ok": True}


def _run_in_thread(work, kind: str = "agent", title: str = "", session: str = "",
                   model: str = "", meta: dict | None = None) -> StreamingResponse:
    """Run agent/crew/video/workflow work in a thread, stream its events live,
    pause for your approval whenever a tool needs it, and record the whole run
    as a trace (Brain) - incidents are opened automatically for failures."""
    q: queue.Queue = queue.Queue()
    run_id = uuid.uuid4().hex[:10]
    run: dict = {"pending": {}}
    events: list[dict] = []
    started = time.time()
    timeout = load_config().get("safety", {}).get("permission_timeout", 300)

    def push(ev: dict):
        if ev.get("type") not in ("token", "permission_done"):
            events.append({k: v for k, v in ev.items() if k != "preview"} if ev.get("type") == "permission" else ev)
        q.put(ev)

    def ask(req: dict) -> str:
        pid = uuid.uuid4().hex[:8]
        ev = threading.Event()
        run["pending"][pid] = {"ev": ev, "decision": "deny"}
        push({"type": "permission", "run": run_id, "id": pid, **req})
        end = time.time() + timeout
        while not ev.wait(0.5):                # wake up for Stop or timeout
            if gate.cancel.is_set() or time.time() > end:
                break
        decision = run["pending"].pop(pid)["decision"] if ev.is_set() else "deny"
        if not ev.is_set():
            run["pending"].pop(pid, None)
            if not gate.cancel.is_set():
                push({"type": "denied", "agent": req.get("agent", ""), "tool": req.get("tool", ""),
                      "why": "no answer in time"})
        q.put({"type": "permission_done", "id": pid, "decision": decision})
        return decision

    gate = Gate(ask_fn=ask, run_id=run_id)
    run["gate"] = gate
    RUNS[run_id] = run

    def emit(ev):
        push(ev)
        gate.check_stop()

    def target():
        status, result = "ok", ""
        try:
            q.put({"type": "run", "id": run_id, "kind": kind})
            if meta:
                events.append({"type": "meta", **meta})
            out = work(emit, gate)
            result = out
            push({"type": "result", "result": out})
        except Stopped:
            status = "stopped"
            audit(run=run_id, event="stopped")
            push({"type": "stopped", "text": "Stopped. Nothing else will run."})
        except Exception as e:
            status = "error"
            push({"type": "error", "text": str(e)})
        finally:
            RUNS.pop(run_id, None)
            try:
                text = result
                lesson = None
                if isinstance(result, dict):
                    lesson = result.get("lesson_id")
                    text = result.get("answer") or result.get("final") or result.get("summary") or ""
                traces.save_trace(run_id, kind, title or kind, model, status, events, str(text or ""),
                                  started=started, session=session, lesson_id=lesson)
                q.put({"type": "trace", "id": run_id, "status": status})
            except Exception as e:      # recording must never break the run
                audit(run=run_id, event="trace_save_failed", error=str(e))
            q.put(None)

    threading.Thread(target=target, daemon=True).start()

    def gen():
        while (ev := q.get()) is not None:
            yield _sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/permission")
def permission(body: DecisionIn):
    run = RUNS.get(body.run)
    p = run and run["pending"].get(body.id)
    if not p:
        raise HTTPException(404, "That request already expired.")
    p["decision"] = body.decision if body.decision in ("once", "task", "deny") else "deny"
    p["ev"].set()
    return {"ok": True}


@app.post("/api/stop")
def stop(body: StopIn):
    run = RUNS.get(body.run)
    if run:
        run["gate"].cancel.set()
    return {"ok": bool(run)}


@app.post("/api/agent")
def agent(body: ChatIn):
    try:
        prov = get_provider(body.provider, task="agent")
    except ProviderError as e:
        raise HTTPException(400, str(e))
    tools = None
    if body.allow_screen or body.allow_commands:   # opt-in extras on top of the defaults
        tools = [t.name for t in available()]
        tools += ["look_at_screen"] if body.allow_screen else []
        tools += ["run_command"] if body.allow_commands else []
    a = Agent(tools=tools)

    def work(emit, gate):
        answer = a.run(prov, body.message, body.images, on_event=emit, gate=gate)
        return {"answer": answer, "lesson_id": a.last_lesson_id}

    return _run_in_thread(work, "agent", body.message, body.session, prov.label())


@app.post("/api/feedback")
def feedback(body: FeedbackIn):
    memory.rate_lesson(body.lesson_id, body.good)
    return {"ok": True}


@app.get("/api/lessons")
def lessons():
    return memory.list_lessons()


@app.post("/api/video")
def video(file: UploadFile = File(...), question: str = Form(...),
          provider: str | None = Form(None)):
    from .media import analyze_video, save_outputs, svg_chart

    try:
        prov = get_provider(provider or None, task="video")
    except ProviderError as e:
        raise HTTPException(400, str(e))
    cfg = load_config().get("media", {})
    up_dir = resolve("./data/uploads")
    up_dir.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename or "video.mp4").name
    path = up_dir / f"{int(time.time())}_{safe}"
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    def work(emit, gate):
        try:
            res = analyze_video(prov, path, question, on_event=emit)
            res["files"] = save_outputs(res, safe, workspace())
            res["svg"] = svg_chart(res["rows"], question) if res["has_values"] else ""
            return res
        finally:
            if not cfg.get("keep_videos", False):
                path.unlink(missing_ok=True)

    return _run_in_thread(work, "video", f"{question} ({safe})", "", prov.label(),
                          meta={"question": question, "file": safe})


@app.post("/api/crew")
def crew(body: CrewIn):
    try:
        prov = get_provider(body.provider, task="agent")
        spec = load_crew(body.crew)
    except (ProviderError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    title = f"{spec.get('name', body.crew)}: " + ", ".join(f"{v}" for v in body.inputs.values() if v)
    return _run_in_thread(lambda emit, gate: run_crew(spec, body.inputs, prov, on_event=emit, gate=gate),
                          "crew", title, "", prov.label(), meta={"crew": body.crew, "inputs": body.inputs})


# ------------------------------------------------------------------ models --
@app.get("/api/models")
def models_list():
    from . import models
    st = models.status()
    return {"models": st, "tasks": models.task_map(), "task_help": models.TASK_HELP,
            "ollama_up": bool(models.OLLAMA["up"]), "downloads": models.queue_state(),
            "recommended": models.recommended_for_pc() if models.OLLAMA["up"] else []}


@app.post("/api/models/pull_all")
def models_pull_all(body: dict | None = None):
    """'Download all for this PC' (or a given list) - runs in the background, one at a time."""
    from . import models
    ids = (body or {}).get("models") or models.recommended_for_pc()
    audit(event="model_pull_all", models=ids)
    return {"queued": ids, "downloads": models.queue_pull(ids)}


@app.get("/api/models/downloads")
def models_downloads():
    from . import models
    return models.queue_state()


@app.post("/api/ollama/start")
def ollama_start():
    from . import models
    up = models.ensure_ollama(wait=20)
    return {"up": up, "message": "Ollama is running." if up else
            "Couldn't start Ollama. Open the Ollama app from the Start menu, or reinstall it from ollama.com."}


@app.post("/api/models/assign")
def models_assign(body: AssignIn):
    from . import models
    try:
        models.assign(body.task, body.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/models/pull")
def models_pull(body: ModelIn):
    """Download a model (you clicked Download, so no extra approval is asked)."""
    from . import models

    def gen():
        try:
            for ev in models.pull(body.model):
                yield _sse(ev)
            audit(event="model_pull", model=body.model)
        except ProviderError as e:
            yield _sse({"status": str(e), "error": True, "done": True})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/models/remove")
def models_remove(body: ModelIn):
    from . import models
    try:
        models.remove(body.model)
    except ProviderError as e:
        raise HTTPException(400, str(e))
    audit(event="model_remove", model=body.model)
    return {"ok": True}


# ------------------------------------------------------------- automations --
@app.get("/api/automations")
def autos_list():
    from . import automations
    return automations.status()


@app.post("/api/automations/toggle")
def autos_toggle(body: AutoIn):
    from . import automations
    try:
        automations.set_enabled(body.name, body.enabled)
    except KeyError as e:
        raise HTTPException(404, str(e))
    audit(event="automation_toggle", name=body.name, enabled=body.enabled)
    return {"ok": True}


@app.post("/api/automations/run")
def autos_run(body: AutoIn):
    """Run now. You're watching, so risky tools ask you in chat as usual."""
    from . import automations
    try:
        a = automations.get(body.name)
    except KeyError as e:
        raise HTTPException(404, str(e))

    def work(emit, gate):
        res = automations.run_due(a, gate=gate, on_event=emit, force=True)
        return {"answer": "\n".join(res) if res else "Nothing new to process."}

    return _run_in_thread(work, "automation", f"Automation: {body.name}")


# --------------------------------------------- sessions, brain, incidents --
class RateIn(BaseModel):
    id: str
    good: bool
    note: str = ""


class ReportIn(BaseModel):
    trace_id: str | None = None
    note: str


class ResolveIn(BaseModel):
    id: int
    note: str = ""


class HintIn(BaseModel):
    text: str
    has_image: bool = False


class CorrectIn(BaseModel):
    text: str
    type: str


@app.get("/api/sessions")
def sessions():
    chats = [{**c, "kind": "chat"} for c in memory.sessions(30)]
    runs = [{"id": t["id"], "title": t["title"], "updated": t["started"], "kind": t["kind"],
             "status": t["status"], "rating": t["rating"]} for t in traces.list_traces(limit=30)]
    return sorted(chats + runs, key=lambda x: -(x["updated"] or 0))[:50]


@app.get("/api/session/{sid}")
def session(sid: str):
    return {"messages": memory.history(sid, 200), "image": memory.last_image(sid)}


@app.get("/api/traces")
def traces_list(q: str = ""):
    return traces.list_traces(q)


@app.get("/api/traces/{tid}")
def trace_get(tid: str):
    t = traces.get_trace(tid)
    if not t:
        raise HTTPException(404, "No such run")
    return t


@app.post("/api/traces/rate")
def trace_rate(body: RateIn):
    try:
        traces.rate(body.id, body.good, body.note)
    except KeyError:
        raise HTTPException(404, "No such run")
    return {"ok": True}


@app.get("/api/brain")
def brain():
    from . import system1
    eng = system1.engine()
    return {**traces.stats(), "system1": system1.available(), "engine": eng,
            "system1_model": system1.SERVERS[eng]["label"] if eng in system1.SERVERS else system1.model_name(),
            "engine_up": system1.server_up(eng) if eng in system1.SERVERS else None}


class EngineIn(BaseModel):
    engine: str


@app.post("/api/system1/engine")
def system1_engine(body: EngineIn):
    from . import models, system1
    try:
        models.set_system1_engine(body.engine)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(event="system1_engine", engine=body.engine)
    started = system1.start_laya() if body.engine == "laya" else None
    return {"ok": True, "laya": started}


@app.get("/api/system1/servers")
def system1_servers():
    from . import system1
    return {"engine": system1.engine(), "laya_installed": system1.laya_installed(),
            "servers": {k: {**system1.server_conf(k), "up": system1.server_up(k)} for k in system1.SERVERS}}


@app.post("/api/system1/install_laya")
def system1_install_laya():
    """You clicked Install: pip-install Laya (Apache-2.0, ~421M model) into NestCrew's own environment."""
    import subprocess
    import sys

    def gen():
        audit(event="laya_install_started")
        cmd = [sys.executable, "-m", "pip", "install", "--progress-bar", "off", "laya[serve]"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             creationflags=0x08000000 if sys.platform == "win32" else 0)
        for line in p.stdout:
            line = line.strip()
            if line:
                yield _sse({"status": line[:160]})
        ok = p.wait() == 0
        audit(event="laya_install_done", ok=ok)
        if ok:
            from . import system1
            system1.start_laya()
        yield _sse({"status": "Laya installed - starting it (first start downloads the model, ~1-2 min)"
                    if ok else "Install failed - see the lines above", "done": True, "error": not ok})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/system1/start_laya")
def system1_start_laya():
    from . import system1
    return {"status": system1.start_laya()}


@app.get("/api/brain/similar")
def brain_similar(text: str):
    return traces.similar_traces(text)


@app.post("/api/training/export")
def training_export(include_unrated: bool = False):
    res = traces.export_training(include_unrated)
    audit(event="training_export", **res)
    return res


@app.get("/api/incidents")
def incidents(status: str | None = None):
    return traces.list_incidents(status)


@app.post("/api/incidents/report")
def incident_report(body: ReportIn):
    return {"id": traces.report_incident(body.trace_id, body.note)}


@app.post("/api/incidents/resolve")
def incident_resolve(body: ResolveIn):
    traces.resolve_incident(body.id, body.note)
    return {"ok": True}


@app.post("/api/hint")
def hint(body: HintIn):
    from . import hints
    h = hints.hint(body.text, body.has_image)
    if h:
        h["similar"] = [{"id": t["id"], "title": t["title"], "rating": t["rating"]}
                        for t in traces.similar_traces(body.text, 2)]
    return h


@app.post("/api/hint/correct")
def hint_correct(body: CorrectIn):
    from . import hints
    hints.correct(body.text, body.type)
    return {"ok": True}


@app.get("/api/hint/types")
def hint_types():
    from . import hints
    return {k: v["label"] for k, v in hints.TASK_TYPES.items()}


# ----------------------------------------------------------------- canvas --
class WorkflowIn(BaseModel):
    workflow: dict
    note: str = ""


class WfIdIn(BaseModel):
    id: str
    enabled: bool = True


@app.get("/api/workflows")
def wf_list():
    from . import workflows
    return {"workflows": workflows.list_all(), "templates": workflows.templates(),
            "node_types": workflows.NODE_TYPES, "crews": [c["file"] for c in list_crews()]}


@app.get("/api/workflows/{wid}")
def wf_get(wid: str):
    from . import workflows
    try:
        return workflows.get(wid)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/workflows/save")
def wf_save(body: WorkflowIn):
    from . import workflows
    try:
        wf = workflows.save(body.workflow, note=body.note)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    audit(event="workflow_saved", id=wf["id"])
    return wf


@app.post("/api/workflows/delete")
def wf_delete(body: WfIdIn):
    from . import workflows
    workflows.delete(body.id)
    return {"ok": True}


@app.post("/api/workflows/toggle")
def wf_toggle(body: WfIdIn):
    from . import workflows
    try:
        workflows.set_enabled(body.id, body.enabled)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    audit(event="workflow_toggle", id=body.id, enabled=body.enabled)
    return {"ok": True}


class CaptureIn(BaseModel):
    id: str
    steps: bool = False


class VersionIn(BaseModel):
    id: str
    version: str


@app.post("/api/workflows/from_trace")
def wf_from_trace(body: CaptureIn):
    from . import workflows
    t = traces.get_trace(body.id)
    if not t:
        raise HTTPException(404, "No such run")
    try:
        return workflows.from_trace(t, as_steps=body.steps)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/workflows/{wid}/versions")
def wf_versions(wid: str):
    from . import workflows
    return workflows.versions(wid)


@app.post("/api/workflows/compare")
def wf_compare(body: VersionIn):
    """Compare a past version with the current one (or two versions: 'v1..v2')."""
    from . import workflows
    try:
        cur = workflows.get(body.id)
        old = workflows.get_version(body.id, body.version)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"old": old, "current": cur, "diff": workflows.diff(old, cur)}


@app.post("/api/workflows/restore")
def wf_restore(body: VersionIn):
    from . import workflows
    try:
        wf = workflows.restore(body.id, body.version)
    except KeyError as e:
        raise HTTPException(404, str(e))
    audit(event="workflow_restored", id=body.id, version=body.version)
    return wf


@app.post("/api/workflows/from_template")
def wf_from_template(body: WfIdIn):
    from . import workflows
    try:
        return workflows.from_template(body.id)
    except (OSError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/workflows/run")
def wf_run(body: WfIdIn):
    """Run now while you watch: nodes light up live; risky steps ask you in chat."""
    from . import workflows
    try:
        wf = workflows.get(body.id)
    except KeyError as e:
        raise HTTPException(404, str(e))

    def work(emit, gate):
        file = None
        trig = workflows.trigger_of(wf)
        if trig.get("watch"):                       # test with the newest file in the folder
            from .automations import _watch_files
            files = _watch_files({"trigger": trig})
            file = files[-1] if files else None
            if file is None:
                raise RuntimeError(f"Put a file in workspace/{trig['watch']} to test this workflow")
        return {"answer": workflows.run(wf, gate=gate, on_event=emit, file=file)}

    return _run_in_thread(work, "workflow", f"Workflow: {wf['name']}")


@app.get("/api/guide")
def guide():
    from .config import ROOT
    p = ROOT / "GUIDE.md"
    return {"markdown": p.read_text(encoding="utf-8") if p.exists() else "# Guide missing"}


_runner = None


@app.on_event("startup")
def _start_automations():
    global _runner
    from .automations import Runner
    from . import system1
    _runner = Runner()
    _runner.start()
    threading.Thread(target=_ensure_ollama, daemon=True).start()
    threading.Thread(target=_ensure_python_deps, daemon=True).start()
    if system1.engine() == "laya":
        threading.Thread(target=system1.start_laya, daemon=True).start()


def _ensure_python_deps():
    """New features may need a small package the first install didn't have (e.g. openpyxl for Excel)."""
    import importlib.util
    import subprocess
    import sys
    missing = [pkg for mod, pkg in (("openpyxl", "openpyxl>=3.1"), ("send2trash", "send2trash>=1.8"), ("qrcode", "qrcode>=7.4")) if importlib.util.find_spec(mod) is None]
    if missing:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing], capture_output=True,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        audit(event="deps_installed", packages=missing, ok=r.returncode == 0)


def _ensure_ollama():
    """Start Ollama in the background if it isn't running."""
    from . import models
    models.ensure_ollama()


@app.on_event("shutdown")
def _stop_helpers():
    from . import phonelink, system1
    system1.stop_laya()
    phonelink.stop()                     # the Wi-Fi link never outlives NestCrew


# ------------------------------------------------------------- phone link --
@app.get("/api/phonelink")
def phonelink_status():
    from . import phonelink
    return phonelink.status()


@app.post("/api/phonelink/start")
def phonelink_start():
    """Pressing Start on the Phone page is your approval."""
    from . import phonelink
    try:
        return phonelink.start()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/phonelink/stop")
def phonelink_stop():
    from . import phonelink
    phonelink.stop()
    return phonelink.status()


@app.post("/api/phonelink/bluetooth")
def phonelink_bluetooth(body: dict):
    from . import phonelink
    mode = "send" if body.get("mode") == "send" else "receive"
    try:
        msg = phonelink.bluetooth_send() if mode == "send" else phonelink.bluetooth_receive()
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(event="bluetooth_" + mode)
    return {"ok": True, "text": msg}


# ------------------------------------------------------------------ update --
@app.post("/api/update")
async def update_app():
    """Guide → 'Update from GitHub': new program files; settings, chats and workflows are kept."""
    from . import updater
    if RUNS:
        raise HTTPException(409, "A task is still running - wait for it or press Stop, then update.")
    try:
        res = await run_in_threadpool(updater.update)
    except Exception as e:
        raise HTTPException(400, f"Update failed: {e}")
    return {"repo": res["repo"], "changed": res["changed"][:60], "added": res["added"][:60],
            "count": len(res["changed"]) + len(res["added"]), "deps": res["deps"]}


# ------------------------------------------------------------------- voice --
@app.get("/api/voice")
def voice_status():
    from . import voice
    return voice.status()


@app.post("/api/voice/install")
def voice_install():
    from . import voice
    voice.install()
    return voice.status()


@app.post("/api/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...), output: str = Form("")):
    from . import voice
    data = await audio.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "That recording is too long - keep voice commands under a minute.")
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    try:
        return await run_in_threadpool(voice.transcribe, data, suffix, output or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                       # bad/empty audio: say so instead of a 500
        raise HTTPException(400, f"Couldn't understand the recording ({type(e).__name__}). Try again.")


@app.post("/api/clear")
def clear(session: str = "default"):
    memory.clear(session)
    return {"ok": True}


def main(open_window: bool = False, wait_port: bool = False):
    import uvicorn

    cfg = load_config().get("server", {})
    host, port = "127.0.0.1", cfg.get("port", 8765)     # never listen on the network
    import socket

    def port_busy() -> bool:
        with socket.socket() as sock:                  # an older copy still running?
            sock.settimeout(1)
            return sock.connect_ex((host, port)) == 0
    busy = port_busy()
    for _ in range(30 if wait_port else 0):            # restarting: wait for the old copy to exit
        if not busy:
            break
        time.sleep(0.5)
        busy = port_busy()
    if busy:
        print(f"Port {port} is already in use - another NestCrew may still be running.\n"
              "Close it (or restart the PC) and start NestCrew again. Opening the existing one...")
        if open_window:
            _open_app_window(f"http://{host}:{port}")
        raise SystemExit(3)
    print(f"NestCrew running at http://{host}:{port}  (Ctrl+C to quit)")
    if open_window:
        threading.Timer(1.5, _open_app_window, args=(f"http://{host}:{port}",)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _open_app_window(url: str):
    """Open as a standalone app window (Edge/Chrome --app), else default browser."""
    import os
    import shutil as sh
    import subprocess
    import webbrowser

    candidates = [sh.which("msedge"), sh.which("chrome"), sh.which("google-chrome"),
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
    for exe in candidates:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1100,800"])
            return
    webbrowser.open(url)
