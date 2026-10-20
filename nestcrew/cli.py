"""Command line:  nestcrew doctor | chat | agent | crew | serve"""
from __future__ import annotations

import argparse
import base64
import sys
import time
import uuid
from pathlib import Path

from . import memory
from .agent import Agent
from .config import api_key, load_config
from .crew import list_crews, load_crew, run_crew
from .net import is_online
from .providers import OllamaProvider, ProviderError
from .router import CLOUD, get_provider


def _img(path: str | None) -> list[str]:
    return [base64.b64encode(Path(path).read_bytes()).decode()] if path else []


def _terminal_ask(req: dict) -> str:
    print("\n" + "-" * 60)
    print(f"[{req['agent']}] wants to use {req['tool']}:")
    print(req["preview"])
    print("-" * 60)
    while True:
        ans = input("Allow? [y] once  [a] for this task  [n] deny > ").strip().lower()
        if ans in ("y", "yes"):
            return "once"
        if ans == "a":
            return "task"
        if ans in ("n", "no", ""):
            return "deny"


def _gate():
    from .safety import Gate
    return Gate(ask_fn=_terminal_ask)


def _print_event(ev: dict):
    if ev["type"] == "task":
        print(f"\n== Task {ev['n']} -> {ev['agent']}")
    elif ev["type"] == "tool":
        print(f"  [{ev['agent']}] {ev['tool']} {ev['input']}")
        print("   ->", ev["result"][:200].replace("\n", " "))


def doctor(_):
    cfg = load_config()
    local = OllamaProvider(cfg.get("local", {}))
    models = local.installed_models()
    print(f"Mode:          {cfg.get('mode')}")
    print(f"Internet:      {'online' if is_online() else 'offline'}")
    print(f"Ollama:        {'running' if models else 'NOT running (start: ollama serve)'}")
    print(f"Local model:   {local.model} "
          f"{'(installed)' if any(m.startswith(local.model) for m in models) else '(missing: ollama pull ' + local.model + ')'}")
    if models:
        print(f"Installed:     {', '.join(models)}")
    print("Cloud keys:    " + ", ".join(f"{c}={'yes' if api_key(c) else 'no'}" for c in CLOUD))
    print("Crews:         " + ", ".join(c["file"] for c in list_crews()))


def chat(a):
    prov = get_provider(a.provider)
    session = a.session or uuid.uuid4().hex[:8]
    print(f"NestCrew chat ({prov.label()}). Type /exit to quit, /img <path> to attach an image.")
    pending: list[str] = []
    while True:
        try:
            text = input("\nyou > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("/exit", "/quit"):
            break
        if text.startswith("/img "):
            pending = _img(text[5:].strip())
            print("(image attached to next message)")
            continue
        if not text:
            continue
        msgs = [{"role": "system", "content": "You are NestCrew, a concise helpful assistant."}]
        msgs += memory.history(session)
        msgs.append({"role": "user", "content": text, "images": pending})
        pending = []
        print("ai  > ", end="", flush=True)
        out = []
        try:
            for piece in prov.stream(msgs):
                out.append(piece)
                print(piece, end="", flush=True)
        except ProviderError as e:
            print(f"\n[error] {e}")
            continue
        print()
        memory.add(session, "user", text)
        memory.add(session, "assistant", "".join(out))


def agent(a):
    prov = get_provider(a.provider, task="agent")
    from .tools import available
    tools = a.tools.split(",") if a.tools else None
    if a.allow_screen or a.allow_commands:
        tools = tools or [t.name for t in available()]
        tools += ["look_at_screen"] if a.allow_screen else []
        tools += ["run_command"] if a.allow_commands else []
    ans = Agent(tools=tools).run(
        prov, a.task, _img(a.image), on_event=_print_event, gate=_gate())
    print("\n" + ans)


def crew(a):
    inputs = dict(kv.split("=", 1) for kv in a.input or [])
    res = run_crew(load_crew(a.crew), inputs, get_provider(a.provider, task="agent"), on_event=_print_event,
                   gate=_gate())
    print("\n==== FINAL ====\n" + res["final"])


def screen(a):
    from .media import look_at_screen
    res = look_at_screen(get_provider(a.provider, task="screen"), a.question, keep=a.keep)
    print(res["answer"])
    print(f"\n(screenshot kept at {res['saved_to']})" if res["saved_to"] else "\n(screenshot deleted)")


def video(a):
    from .media import analyze_video, save_outputs
    from .tools import workspace

    def ev(e):
        if e["type"] == "status":
            print(e["text"])
        elif e["type"] == "frame":
            val = f"  value={e['value']:g}" if e["value"] is not None else ""
            print(f"  {e['time']:>6}s{val}  {e['description'][:90]}")

    if a.every or a.max_frames:
        import nestcrew.media as m
        m_cfg = m._cfg()
        if a.every:
            m_cfg["video_frame_every"] = a.every
        if a.max_frames:
            m_cfg["video_max_frames"] = a.max_frames
    res = analyze_video(get_provider(a.provider, task="video"), a.path, a.question, on_event=ev)
    files = save_outputs(res, Path(a.path).name, workspace())
    print("\n==== SUMMARY ====\n" + res["summary"])
    print("\nSaved: " + ", ".join(files.values()))


def lessons(a):
    if a.forget:
        memory.rate_lesson(a.forget, False)
        print(f"Forgot lesson {a.forget}")
        return
    if a.good:
        memory.rate_lesson(a.good, True)
        print(f"Marked lesson {a.good} as trusted")
        return
    rows = memory.list_lessons()
    if not rows:
        print("No lessons yet - successful agent runs are saved automatically.")
    for r in rows:
        mark = "👍" if r["status"] == "good" else "  "
        print(f"{mark} #{r['id']:<4} used {r['uses']}x  [{r['agent']}] {r['task'][:60]}")
        print(f"         approach: {r['approach']}")


def models_cmd(a):
    from . import models
    if a.action == "pull":
        for ev in models.pull(a.args[0]):
            pct = f" {ev['percent']}%" if ev.get("percent") is not None else ""
            print(f"\r{ev['status']}{pct}          ", end="", flush=True)
        print()
        return
    if a.action == "remove":
        models.remove(a.args[0]); print("Removed."); return
    if a.action == "use":
        models.assign(a.args[0], a.args[1]); print(f"{a.args[0]} -> {a.args[1]}"); return
    print(f"{'id':<20}{'params':<12}{'download':<14}{'ram':<14}{'ready':<7}best for")
    for m in models.status():
        print(f"{m['id']:<20}{m['params']:<12}{m['download']:<14}{m['ram']:<14}"
              f"{'yes' if m['installed'] else '-':<7}{m['best_for']}")
    print("\nJobs:", ", ".join(f"{t}={m}" for t, m in models.task_map().items()))
    print("Switch: nestcrew models use screen smolvlm2-256m   |   Download: nestcrew models pull moondream")


def auto_cmd(a):
    from . import automations
    if a.action == "on" or a.action == "off":
        automations.set_enabled(a.name, a.action == "on"); print(f"{a.name}: {a.action}"); return
    if a.action == "run":
        for r in automations.run_due(automations.get(a.name), gate=_gate(),
                                     on_event=_print_event, force=True) or ["Nothing new."]:
            print(r)
        return
    if a.action == "watch":
        print("Watching automations (Ctrl+C to stop)...")
        r = automations.Runner()
        while True:
            for res in r.tick():
                print(time.strftime("%H:%M"), res)
            time.sleep(30)
    for s in automations.status():
        print(f"[{'on ' if s['enabled'] else 'off'}] {s['name']:<20} {s['when']:<40} {s['action']}")


def serve(_):
    from .server import main
    main()


def app(_):
    from .server import main
    main(open_window=True)


def main(argv=None):
    p = argparse.ArgumentParser(prog="nestcrew", description="Offline-first AI agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check your setup").set_defaults(fn=doctor)
    c = sub.add_parser("chat", help="chat in the terminal")
    c.add_argument("--provider", help="local | anthropic | gemini | xai | openai")
    c.add_argument("--session")
    c.set_defaults(fn=chat)
    g = sub.add_parser("agent", help="run one agent on a task")
    g.add_argument("task")
    g.add_argument("--image")
    g.add_argument("--tools", help="comma list, e.g. calculator,read_file")
    g.add_argument("--provider")
    g.add_argument("--allow-screen", action="store_true", help="let the agent take screenshots")
    g.add_argument("--allow-commands", action="store_true", help="let the agent run commands (asks each time)")
    g.set_defaults(fn=agent)
    w = sub.add_parser("crew", help="run a crew from crews/*.yaml")
    w.add_argument("crew", help="name (e.g. research_writer) or path")
    w.add_argument("--input", action="append", help="key=value, repeatable")
    w.add_argument("--provider")
    w.set_defaults(fn=crew)
    sc = sub.add_parser("screen", help="screenshot your screen and ask about it")
    sc.add_argument("question", nargs="?", default="Describe what is on the screen.")
    sc.add_argument("--keep", action="store_true", help="keep the screenshot instead of deleting")
    sc.add_argument("--provider")
    sc.set_defaults(fn=screen)
    v = sub.add_parser("video", help="slice a video into frames, analyse, chart")
    v.add_argument("path")
    v.add_argument("question")
    v.add_argument("--every", type=float, help="seconds between frames")
    v.add_argument("--max-frames", type=int)
    v.add_argument("--provider")
    v.set_defaults(fn=video)
    ls = sub.add_parser("lessons", help="see / rate what the agents have learned")
    ls.add_argument("--good", type=int, help="mark lesson id as trusted")
    ls.add_argument("--forget", type=int, help="delete lesson id")
    ls.set_defaults(fn=lessons)
    mo = sub.add_parser("models", help="list / pull / remove models, or switch the model for a job")
    mo.add_argument("action", nargs="?", default="list", choices=["list", "pull", "remove", "use"])
    mo.add_argument("args", nargs="*")
    mo.set_defaults(fn=models_cmd)
    au = sub.add_parser("auto", help="automations: list | on NAME | off NAME | run NAME | watch")
    au.add_argument("action", nargs="?", default="list", choices=["list", "on", "off", "run", "watch"])
    au.add_argument("name", nargs="?")
    au.set_defaults(fn=auto_cmd)
    sub.add_parser("serve", help="start the web app").set_defaults(fn=serve)
    sub.add_parser("app", help="start and open NestCrew in its own window").set_defaults(fn=app)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (ProviderError, RuntimeError, ValueError, KeyError) as e:
        sys.exit(f"[error] {e}")
    except KeyboardInterrupt:
        sys.exit("\nStopped.")


if __name__ == "__main__":
    main()
