# NestCrew 🪺

**A local-first AI agent platform for your own PC.** Small open models do the work offline;
agents automate files, folders and spreadsheets like an RPA bot, ask before every risky step,
and every finished task can be saved as a visual, versioned workflow.

![tests](https://github.com/Bakulesh1Codes108/nestcrew/actions/workflows/ci.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)
![offline](https://img.shields.io/badge/runs-100%25%20offline-2f7d4f)

---

## What it does

| | |
|---|---|
| 💬 **Chat, Agent, Crew, Video** | Chat with a local vision model; give an **agent** a task and watch each step; run multi-agent **crews**; analyse **videos** frame by frame into a CSV + chart. |
| 🗂 **RPA tools** | Copy / move / rename files, create folders, find files, read / write / append **Excel & CSV** in Desktop, Documents, Downloads… Moves are undoable, nothing is overwritten, there is no delete tool. |
| 🧑‍💼 **Specialist sub-agents** | The main agent can **delegate** to a file clerk, Excel analyst, researcher or writer — each with its own small tool set and the same approval rules. |
| ✋ **Asks before acting** | Approval cards show exactly what will change (which files, which rows). Allow once / for this task / deny. Stop interrupts even a model mid-answer. |
| ◇ **Canvas** | n8n-style node editor: triggers (manual, watch folder, daily, every N min), vision, Decide, If, model, tool, agent, crew, note steps. Live run highlighting. |
| ⚙ **Save as RPA bot** | Turn any finished task into a bot that replays the exact tool steps — no AI in the loop — or into an AI flow that repeats the goal flexibly. |
| 🕘 **Versions & compare** | Every save is kept. Compare any past version with now (added / changed / removed steps highlighted on the canvas) and restore it. |
| ⚡ **System 1** | Instant typed decisions (task routing, hints, branching) from **Laya**, an open-source "System One" model, with MiniLM / keyword fallback. |
| ◎ **Brain & learning loop** | Every run is a trace. 👍 runs guide similar tasks and export as a fine-tuning dataset; 👎 opens an incident. |
| 🧩 **Right model per job** | One "brain" model plans; tiny "eyes" models (SmolVLM2 256M/500M, LFM2-VL, Granite Vision…) read screens, videos and documents. Paid cloud APIs are optional. |

## Architecture

```mermaid
flowchart LR
    UI[App window<br/>chat · canvas · brain] --> S[FastAPI · 127.0.0.1<br/>per-launch token]
    S --> R[Router + System 1<br/>Laya]
    R --> A[Agent loop] --> G{Permission gate}
    A --> D[Specialist sub-agents] --> G
    G --> T[Tools: files · folders · Excel · web · screen]
    T --> FS[(Desktop · Documents · Downloads · workspace)]
    A --> O[Ollama: Qwen3-VL · Gemma 4 · SmolVLM2 …]
    S --> W[Workflow engine · automations] --> G
    A --> DB[(SQLite: chats · traces · incidents · lessons)]
    W --> V[(Versioned workflows)]
```

## How a task runs (real run on a Windows laptop)

![NestCrew moving files after approval](docs/screenshots/rpa-move-approved.png)

```mermaid
flowchart TD
    U["You type: move all files from BIRAC to BIRAC2"] --> H{"System 1 router<br/>rule match or Laya / MiniLM"}
    H -- "question" --> C["Chat with local model<br/>(Qwen3-VL 2B instruct)"]
    H -- "file / Excel task 99%" --> Q{"Quick plan?<br/>copy · move · make folder"}
    Q -- "yes: exact steps" --> F["Find folders<br/>Desktop → Documents → Downloads<br/>(subfolders too; asks if ambiguous)"]
    Q -- "no: open-ended" --> L["Agent loop<br/>model plans JSON tool calls<br/>(+ specialist sub-agents)"]
    L --> F
    F --> P["list_folder (read: automatic)"]
    P --> G{"Permission gate<br/>approval card lists every file"}
    G -- "Deny / no answer" --> X["Nothing changes"]
    G -- "Allow once / for task" --> T["copy_files / move_files<br/>no overwrite · identical skipped · undo log"]
    T --> R["Short answer + steps in Progress panel"]
    R --> B[("Brain: trace saved<br/>👍 → lessons · 👎 → incident")]
    B --> W["⚙ Save as RPA bot / ◇ AI flow<br/>→ Canvas: edit · version · compare · schedule"]
```

More diagrams (request sequence, safety layers, model routing, learning loop, code map): **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Quick start (Windows)

1. Download or clone this repo into `Documents\NestCrew`.
2. Double-click **`Install NestCrew.bat`** — installs Python packages, Ollama, a model sized to your RAM,
   the tiny vision models and Laya, then adds a **NestCrew** shortcut. (Internet needed once.)
3. Double-click **NestCrew** on the Desktop. It starts Ollama and Laya by itself and opens the app.

Then try, in Chat:

> Move all files from BIRAC to BIRAC2 on my Desktop

System 1 recognises it as a file task and runs it as an agent; you approve the move in the chat;
press **⚙ Save as RPA bot** to repeat it later in one click — or give the bot a trigger to run on its own.

macOS / Linux: `bash scripts/setup.sh`, then `nestcrew app`. Full manual: **[GUIDE.md](GUIDE.md)** (also in the app).

## Command line

```bash
nestcrew doctor                      # check Ollama, models, internet
nestcrew app                         # open the app window
nestcrew agent "copy the pdfs from Downloads to Documents/Invoices"
nestcrew crew research_writer --input topic="solar panels"
nestcrew video clip.mp4 "How many people are visible?"
nestcrew models use screen smolvlm2-256m
nestcrew auto watch                  # run automations from a terminal
```

## Safety model

- Listens on `127.0.0.1` only; API calls need a fresh secret token per launch.
- Tools are `allow` / `ask` / `deny`; anything that changes files asks you, with a preview.
- File tools only reach Desktop, Documents, Downloads, Pictures, Videos, Music and the workspace;
  `.ssh`, `AppData`, system folders and the app's own folder are refused.
- No delete tool; no overwrites; moves/renames undoable; Excel and text files backed up before edits.
- Dangerous shell commands are blocked even if approved; tool output is treated as data, not instructions.
- Unattended automations can only use tools pre-approved for that workflow.
- Every action and decision is written to `data/audit.jsonl`.

## Tech

Python 3.10+ · FastAPI + Server-Sent Events · SQLite · Ollama · Laya (`/v1/systemone`) ·
openpyxl · OpenCV · vanilla HTML/JS (no build step) · pytest (100+ offline tests with scripted models) ·
GitHub Actions on Windows + Linux.

## Tests

```bash
pip install -e ".[dev,media]"
pytest -q        # fully offline: models are replaced by scripted stand-ins
```

## Project layout

```
nestcrew/        server, agent, tools, rpa, safety, workflows, system1, models, traces …
nestcrew/static/ the whole UI (one HTML file)
workflows/       starter Canvas templates
crews/           multi-agent recipes (YAML)
docs/            architecture
tests/           unit + API tests
models.yaml      model catalog and per-job assignment
config.yaml      permissions, folders, safety, System 1 engine
```

## Roadmap

- Record mouse/keyboard steps in desktop apps as bot steps
- Local RAG over your documents
- Voice in/out (whisper.cpp + Piper)
- Packaged installer (single .exe)

## Author

Built by **Bakulesh Rane** ([@Bakulesh1Codes108](https://github.com/Bakulesh1Codes108)). MIT licensed.
