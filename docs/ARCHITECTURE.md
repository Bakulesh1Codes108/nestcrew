# NestCrew architecture

NestCrew is a local-first agent platform: a FastAPI server on `127.0.0.1` drives small
open models through Ollama, gates every risky action behind a human approval, records
every run, and lets you turn runs into visual, versioned workflows (AI flows or RPA bots).

## 1. System overview

```mermaid
flowchart LR
    subgraph UI["Desktop app window (HTML/JS, no framework)"]
        Chat[Chat / Agent / Crew / Video]
        Canvas[Canvas editor<br/>versions + compare]
        Brain[Brain · Incidents · Models · Automations]
    end

    subgraph Server["FastAPI server · 127.0.0.1:8765 · per-launch token"]
        Router[Router<br/>right model per job]
        Hints[System 1 hints<br/>task type → mode/model]
        Agent[Agent loop<br/>think → tool → observe]
        Delegate[Specialists<br/>file clerk · excel analyst<br/>researcher · writer]
        Gate[Permission gate<br/>allow · ask · deny]
        Tools[Tools<br/>files · folders · Excel · web · screen]
        WF[Workflow engine<br/>DAG runner]
        Auto[Automation runner<br/>watch · daily · every]
        Traces[(SQLite<br/>chats · traces · incidents · lessons)]
    end

    subgraph Local["This PC"]
        Ollama[Ollama<br/>Qwen3-VL · Gemma 4 · SmolVLM2 …]
        Laya[Laya 421M<br/>/v1/systemone]
        FS[(Desktop · Documents · Downloads<br/>workspace/)]
    end

    Cloud[(Optional paid APIs<br/>Claude · Gemini · Grok · OpenAI)]

    Chat --> Router --> Agent
    Chat --> Hints --> Laya
    Agent --> Gate --> Tools --> FS
    Agent --> Delegate --> Gate
    Agent --> Ollama
    Canvas --> WF --> Gate
    Auto --> WF
    Agent --> Traces
    WF --> Traces
    Router -. only with your key .-> Cloud
```

## 2. One agent task, end to end

```mermaid
sequenceDiagram
    participant U as You
    participant UI as App window
    participant S1 as System 1 (Laya)
    participant A as Agent (small LLM)
    participant G as Permission gate
    participant T as Tool (move_files)
    participant B as Brain (SQLite)

    U->>UI: "Move all files from BIRAC to BIRAC2 on Desktop"
    UI->>S1: what kind of task is this?
    S1-->>UI: files · 98% → run as Agent
    UI->>A: task + tools (list_folder, move_files, …)
    A->>T: list_folder(Desktop/BIRAC)  — allowed automatically
    T-->>A: 3 files
    A->>G: move_files(Desktop/BIRAC → Desktop/BIRAC2)
    G->>UI: approval card: exactly which files move
    U->>UI: Allow once
    UI->>G: once
    G->>T: run
    T-->>A: Moved 3 files (undo logged)
    A-->>UI: final answer
    UI->>B: trace saved (steps, approvals, result)
    U->>UI: 👍 / ⚙ Save as RPA bot
```

## 3. The agent loop and safety layers

```mermaid
flowchart TD
    Start([Task]) --> Recall[Recall similar 👍 runs as lessons]
    Recall --> Think[Model returns one JSON action<br/>capped length · Stop can cut it off]
    Think -->|final| Done([Answer + trace])
    Think -->|tool| Allowed{Tool allowed<br/>for this agent?}
    Allowed -->|no| Think
    Allowed -->|yes| Block{Blocklist / sandbox<br/>violation?}
    Block -->|yes| Incident[Refuse + open incident] --> Think
    Block -->|no| Level{Permission level}
    Level -->|allow| Run[Run tool]
    Level -->|ask| Card[Approval card in chat<br/>once · this task · deny · 5-min timeout]
    Level -->|deny| Think
    Card -->|approved| Run
    Card -->|denied| Think
    Run --> Wrap[Redact secrets · mark output as untrusted data] --> Audit[(audit.jsonl)] --> Think
```

| Layer | What it guarantees |
|---|---|
| Bind + token | Server listens on 127.0.0.1 only; every API call needs a fresh per-launch token (blocks other websites / DNS rebinding) |
| Permission gate | Every tool is `allow`, `ask` or `deny`; unattended runs can only use tools pre-approved for that workflow |
| Sandbox | File tools resolve paths inside Desktop/Documents/Downloads/… + workspace; `.ssh`, `AppData`, system folders, NestCrew's own folder are refused |
| No destructive tools | There is no delete tool; copies/moves never overwrite (`name (2).ext`); moves and renames are undoable; Excel/text files are backed up before changes |
| Command blocklist | `rm -rf`, `format`, `reg delete`, `shutdown`, piping downloads into shells … are refused even if approved |
| Prompt-injection guard | Tool output (web pages, files) is wrapped and labelled as data, never instructions |
| Audit | Every action and decision is appended to `data/audit.jsonl`; incidents collect failures and blocks |

## 4. Models: one brain, several eyes, a System 1

```mermaid
flowchart LR
    Task[Your task] --> S1{System 1<br/>Laya → MiniLM → keywords}
    S1 -->|chat / agent / crew| Brain[Brain model<br/>Qwen3-VL 2B · Gemma 4 E4B · Qwen3.5 0.8B]
    S1 -->|screen| Eyes1[SmolVLM2 500M]
    S1 -->|video frames| Eyes2[SmolVLM2 256M]
    S1 -->|invoices / tables| Eyes3[Granite Vision 2B]
    S1 -->|photo tags| Eyes4[LFM2-VL 450M]
    Brain -. optional .-> Cloud[Claude / Gemini / Grok]
```

Each job (chat, agent, screen, video, documents, image) has its own model in `models.yaml`, switchable
in the Models page; a job falls back to the default model until its own is downloaded.

## 5. From a one-off task to a reusable, versioned flow

```mermaid
flowchart LR
    Run[Finished task<br/>trace] -->|◇ Save as AI flow| Flow[Trigger → Agent → Save note<br/>flexible, uses a model]
    Run -->|⚙ Save as RPA bot| Bot[Trigger → tool → tool → … → Done<br/>exact replay, no model]
    Flow --> Canvas[Canvas: edit steps, branch with Decide / If]
    Bot --> Canvas
    Canvas -->|Save| V[(versions/<br/>every change kept)]
    V -->|Compare| Diff[added · changed · removed<br/>highlighted on the canvas]
    V -->|Restore| Canvas
    Canvas -->|Automatic| Trig[watch folder · daily · every N min]
```

Workflow steps: Trigger, Look at screen, Look at image, Analyse video, Decide (System 1), If text contains,
Ask a model, Use one tool, Agent, Crew, Save to note, Final answer. Workflows are DAGs (loops are rejected),
text flows along arrows as `{input}`, and Decide/If steps choose which arrows fire.

## 6. Learning loop

```mermaid
flowchart LR
    R[Run] --> T[(Trace)]
    T --> Rate{You rate}
    Rate -->|👍| L[Lesson: recalled for similar tasks]
    Rate -->|👍| D[Training set<br/>workspace/training/good_*.jsonl]
    Rate -->|👎| I[Incident + rejected set]
    D --> FT[LoRA fine-tune a small model<br/>Unsloth / LLaMA-Factory] --> O[Import to Ollama] --> R
```

## 7. Code map

| Module | Responsibility |
|---|---|
| `server.py` | FastAPI app, SSE streaming, approval round-trips, Stop, token guard, starts Ollama/Laya |
| `agent.py` | JSON tool-calling loop, lessons, cancellation, sub-agent context |
| `rpa.py` | Files/folders/Excel tools, sandboxed paths, previews, undo, specialist delegation |
| `tools.py` | Tool registry, workspace tools, web tools, screen tool, command tool |
| `safety.py` | Permission gate, blocklist, redaction, untrusted wrapping, audit log |
| `providers.py` | Ollama (streaming, capped, interruptible) + Claude/Gemini/OpenAI-compatible clients |
| `router.py`, `models.py` | Per-job model routing, catalog, downloads, fit-for-this-PC hints |
| `system1.py`, `hints.py` | Typed decisions via Laya/Kev/OpenJev/Cribl (`/v1/systemone`) or MiniLM; task routing |
| `workflows.py` | DAG engine, templates, capture (AI flow / RPA bot), versions, diff, restore |
| `automations.py` | Folder-watch and schedule runner for recipes and workflows |
| `traces.py`, `memory.py` | Run traces, incidents, training export, chats, lessons |
| `media.py` | Screenshots, video frame sampling, CSV/SVG charts |
| `static/index.html` | The whole UI: chat, progress panel, canvas editor, brain, models, guide |
