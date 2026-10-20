# NestCrew guide

Your own AI agents, running on this laptop. This guide covers starting the app, choosing the right small model for each job, and automating everyday tasks.

## 1. Start it

- **First time:** double-click `Install NestCrew.bat` in `Documents\NestCrew` (needs internet once).
- **One click:** double-click **Start NestCrew** on your Desktop. The first time it runs the installer (needs internet once); after that it opens the app. The installer replaces it with a proper **NestCrew** shortcut.
- A small console window opens (close it to quit) and the app opens in its own window.
- **Check setup:** in a terminal inside the folder, run `.venv\Scripts\nestcrew doctor`.

### The window

| Area | What's there |
|---|---|
| Left sidebar | New chat · Chat · Canvas · Brain · Incidents · Automations · Models · Guide · your recent chats and runs · status (online, model ready, System 1) |
| Middle | The conversation. Under the box: **Chat / Agent / Crew / Video** modes, Screenshots and Commands switches, Send / Stop |
| Hint bar | Appears as you type: what kind of task this looks like, which mode/model/switch to use, one-click buttons to apply them |
| Right panel | **Progress** (live steps: running, done, waiting for you, failed) · **Outputs** (files and notes created) · **Used** (model + tools) · **Similar past tasks** |

Under every agent answer: **👍 Good / 👎 Bad**, **◇ Capture as workflow**, **See all steps**.

## 2. The idea: one brain, several eyes

Tiny vision models are great at *looking* but too small to plan or use tools reliably. NestCrew splits the work:

| Role | What it does | Needs | Good picks |
|---|---|---|---|
| Brain (System 2) | Chat, agents, crews, deciding which tool to use | Tool use + follows instructions | Gemma 4 E4B (16 GB RAM), Qwen3 1.7B or Qwen3.5 0.8B (8 GB RAM) |
| Eyes | Reads your screen, video frames, documents, photos | Vision only | SmolVLM2 256M / 500M, LFM2-VL 450M, Moondream, Granite Vision, Qwen3-VL 2B |
| System 1 | Instant typed decisions: task routing, hints, Canvas branching, finding similar past tasks | Nothing generated — picks labels with a confidence | MiniLM embeddings (46 MB) |

When an agent uses the `look_at_screen` tool, the brain asks the **screen** model to look and gets a text answer back. Video analysis uses the **video** model frame by frame. Each job can use a different model.

## 3. Ultra-small VLM comparison

Approximate numbers for 8-bit/4-bit downloads on a CPU laptop. RAM is while the model is answering.

| Model | Parameters | Download | RAM (approx.) | Best suited for | Key feature | Tool use |
|---|---|---|---|---|---|---|
| SmolVLM2 256M | 256M | ~0.3 GB | ~0.6–1 GB | Quick screen captions, video frames, very old laptops | Smallest practical VLM; reads frame sequences | No |
| LFM2-VL 450M | 450M | ~0.5 GB | ~1 GB | Fast photo descriptions and tagging on CPU | Built for on-device speed | No |
| SmolVLM2 500M | 500M | ~0.6 GB | ~1–1.5 GB | Screenshots, UI reading, video summaries | Best quality per MB of the tiny models | No |
| nanoVLM | 222M | ~0.9 GB + PyTorch | ~2–2.5 GB | Learning how VLMs work, experiments | Pure PyTorch, easy to read and fine-tune | No |
| Moondream 2 | 1.8B | 1.7 GB | ~2.5–3 GB | "What's in this image?", counting objects | Robust visual Q&A for its size | No |
| Qwen3-VL 2B | 2B | ~2 GB | ~3 GB | Text in screenshots, OCR, other languages | Strongest small open VLM family | Yes |
| Granite 3.2 Vision | 2B | 2.4 GB | ~3–3.5 GB | Invoices, forms, tables, charts | Trained for document understanding | No |
| Gemma 4 E4B | ~4B effective | ~3–4 GB | ~4–6 GB | The agent brain: planning + tools + images | Vision and tool calling together | Yes |
| Qwen3 1.7B | 1.7B | ~1.4 GB | ~2 GB | Light brain for 8 GB laptops | Follows the JSON tool format well | Yes (text only) |
| Qwen3.5 0.8B | 0.8B | ~1 GB | ~1.5–2.5 GB | Smallest model that is brain *and* eyes | Vision + tools + thinking under 1B (2026) | Yes |

> Honest notes: nanoVLM is a teaching model from Hugging Face — its answers are loose and, because it runs through PyTorch, it uses *more* memory than SmolVLM2 256M. Use SmolVLM2 256M when you just want the smallest model that works. Moondream needs about 2.5–3 GB, not 4–5 GB.

## 4. Which model for which task

| Task | Recommended | Lighter option | Heavier / more accurate |
|---|---|---|---|
| Chat (with images) | Gemma 4 E4B | Qwen3 1.7B + an eyes model | Qwen3-VL 8B (16 GB+ with GPU) |
| Agents & crews | Gemma 4 E4B | Qwen3 1.7B | Claude / Gemini when online |
| Read my screen | SmolVLM2 500M | SmolVLM2 256M | Qwen3-VL 2B (small text, errors) |
| Video analysis | SmolVLM2 256M | — | SmolVLM2 500M or Moondream |
| Invoices, forms, tables | Granite 3.2 Vision | Qwen3-VL 2B | Claude when online |
| OCR / reading text | Qwen3-VL 2B | SmolVLM2 500M | Granite 3.2 Vision for layouts |
| Photo tagging | LFM2-VL 450M | SmolVLM2 256M | Moondream |
| Counting things in images | Moondream 2 | SmolVLM2 500M | Qwen3-VL 2B |

### Laptop presets

| Your laptop | Brain (chat + agent) | Screen | Video | Documents |
|---|---|---|---|---|
| 8 GB RAM | qwen3-1.7b or qwen3.5-0.8b | smolvlm2-256m | smolvlm2-256m | smolvlm2-500m |
| 16 GB RAM | gemma4-e4b | smolvlm2-500m | smolvlm2-256m | granite-vision-2b |
| 16 GB + GPU | gemma4-e4b | qwen3-vl-2b | smolvlm2-500m | granite-vision-2b |

## 5. Switching models

**In the app:** open **Models**.
1. Under *Which model does which job*, pick a model for each job. It saves instantly.
2. Click **Download** on any model you don't have yet (internet needed once).
3. **Use in chat** sends your next chat messages to that exact model — handy for comparing.
4. The model dropdown at the top lets you pick a model, *Auto*, or a cloud API for any message.

**In a terminal:**

```
nestcrew models                        # list models, sizes, what's downloaded, who does what
nestcrew models pull smolvlm2-500m     # download
nestcrew models use screen smolvlm2-256m
nestcrew models use agent qwen3-1.7b
nestcrew models remove moondream
```

**By file:** edit `tasks:` at the bottom of `models.yaml`.

Rules that keep you out of trouble:
- The **agent** job only accepts models with tool use (Gemma 4, Qwen3, Qwen3-VL). Tiny VLMs can't run agents — NestCrew will refuse the switch and tell you why.
- If a job's model isn't downloaded yet, NestCrew quietly uses your default model instead.
- Only one model sits in memory at a time when RAM is tight; switching jobs swaps models (a few seconds).

### Adding a new model

Copy any block in `models.yaml`, give it a new id, and set `tag:` to an Ollama name or a Hugging Face GGUF (`hf.co/<user>/<repo>:Q8_0`). It appears in the Models tab immediately.

If Ollama can't load a model's images, run it with llama.cpp instead and set `backend: llamacpp`:

```
llama-server -hf ggml-org/SmolVLM2-500M-Video-Instruct-GGUF:Q8_0 --port 8080
```

### nanoVLM (experimental)

```
git clone https://github.com/huggingface/nanoVLM
cd nanoVLM
python -m venv .venv && .venv\Scripts\activate
pip install torch torchvision transformers huggingface_hub safetensors pillow
python generate.py --image some.jpg --prompt "What is this?"
```

Then add to `config.yaml`:

```
nanovlm:
  repo: C:/Users/you/nanoVLM
  python: C:/Users/you/nanoVLM/.venv/Scripts/python.exe
```

and pick *nanoVLM 222M* for a job in the Models tab.

## 6. Files, folders and Excel (RPA)

Ask in plain words — in Chat or Agent mode. When Chat sees a request to *do* something (move files, fill a spreadsheet, calculate), it runs it as an **Agent** automatically.

| Ask | What happens |
|---|---|
| "Move all files from BIRAC to BIRAC2 on my Desktop" | Agent lists the folder (no question), then shows an approval card listing every file that will move |
| "Copy the PDFs from Downloads into Documents/Invoices" | Same, with `*.pdf` only; the folder is created if missing |
| "Read Documents/sales.xlsx and tell me the total of column C" | Reads the sheet automatically, uses the calculator |
| "Add a row 21-10-2026, Taxi, 250 to Desktop/expenses.xlsx" | Shows the rows first; backs the file up, then appends |
| "Make an Excel file listing every file in Downloads" | Lists the folder, then asks before writing the .xlsx |

Tools: `list_folder`, `find_files`, `file_info`, `read_text`, `read_excel` (automatic) · `make_folder`, `copy_files`, `move_files`, `rename_file`, `write_excel`, `append_excel_rows`, `undo_last_move` (ask you first).

Rules that keep your files safe:
- Only Desktop, Documents, Downloads, Pictures, Videos, Music and the workspace — change the list under `rpa: folders:` in `config.yaml`. Hidden/system folders (.ssh, AppData, Windows…) are always refused.
- **Nothing is deleted** (there is no delete tool) and **nothing is overwritten** — clashes become `name (2).ext`.
- Moves and renames can be undone: ask "undo the last move".
- Excel files are backed up to `workspace/.backups` before they change.

### Plain file commands run exactly

Commands like these are understood without the model guessing, and still show you an approval card:

| You type | What happens |
|---|---|
| `copy all files from BIRAC to BIRAC2` | finds BIRAC (Desktop first, then inside your folders), copies into BIRAC2 next to it |
| `move the pdfs from Downloads to Documents/Invoices` | moves only `*.pdf`; undo with "undo last move" |
| `copy everything from "NestCrew test/BIRAC" on my Desktop into a new folder "BIRAC copy"` | quotes and "on my Desktop" are fine |
| `create a folder called Reports 2026 on my desktop` | makes the folder |

If a name exists in two places (say Desktop/Reports and Documents/Reports), NestCrew asks which one instead of guessing.
For anything else (summarise, fill an Excel, sort by date) the agent plans with the model and can search with **find_folder**.

**Updates:** when NestCrew's files change, a bar says *NestCrew was updated* — press **Restart now**.
(Closing only the app window leaves NestCrew running in its black window; close that window to quit fully.)

### Specialist sub-agents

For bigger jobs the agent can **delegate** parts to a specialist: **file clerk** (files and folders), **Excel analyst** (sheets and totals), **researcher** (web when online, files otherwise), **writer** (notes and reports). You see "Agent handed a job to File clerk" in the chat and Progress panel, and you still approve every risky step the specialist takes. Specialists can't delegate further.

Small local models delegate less reliably than big ones — for simple jobs the agent just uses the tool itself.

## 7. Automating tasks

Automations run by themselves while NestCrew is open. They are listed in **Automations**, all **off** until you tick them. Each one has a trigger and an action, defined in `automations.yaml`.

### Ready-made automations

| Automation | How to use it | Model job | Output |
|---|---|---|---|
| Screenshot notes | Save screenshots into `workspace\inbox\screenshots` | screen | `notes\screenshots.md` |
| Invoice reader | Drop invoice photos into `workspace\inbox\invoices` | documents | `notes\invoices.md` (JSON per invoice) |
| Photo tagger | Drop photos into `workspace\inbox\photos` | image | `notes\photo_tags.md` |
| Video inbox | Drop videos into `workspace\inbox\videos` | video | summary + timeline CSV + chart in `notes\` |
| Morning report | Runs daily at 09:00 | agent | report from your notes |
| Inbox check | Runs every hour | agent | `notes\inbox_status.md` |

Tip: in Windows, set *Snipping Tool → Settings → Automatically save screenshots* to the `inbox\screenshots` folder and every screenshot becomes a note.

### Writing your own

```
- name: Receipt totals
  enabled: false
  trigger: { watch: inbox/receipts, pattern: "*.jpg,*.png" }
  action:
    type: describe_image
    task: documents
    prompt: "Return JSON: shop, date, total."
    save_to: notes/receipts.md

- name: Weekly tidy-up
  trigger: { every_minutes: 10080 }
  action:
    type: agent
    tools: [list_files, read_file, write_file]
    prompt: Summarise everything in notes/ into notes/weekly.md
  allow: [write_file]
```

Triggers: `watch` (+ `pattern`), `daily: "HH:MM"`, or `every_minutes`. Actions: `describe_image`, `video`, `agent`, `crew`. Inside prompts, `{file}` is the new file's path.

### Safety for unattended runs

- No one is there to click *Allow*, so any tool that normally asks you is **denied** unless that automation lists it under `allow:`.
- The command blocklist, workspace sandbox and backups still apply. `run_command` is never available to automations unless you add it to both `tools:` and `allow:` — think twice before doing that.
- **Run now** in the app runs the automation while you watch, so risky steps ask you in chat.
- Everything is recorded in `data\audit.jsonl` and `workspace\notes\automation_log.md`.
- Only one automation runs at a time, so your laptop isn't overloaded.

Without the app open: `nestcrew auto watch` runs automations from a terminal, and `nestcrew auto run "Invoice reader"` runs one once.

## 8. Hints and System 1

NestCrew has two kinds of thinking:

- **System 1 — fast decisions.** A 46 MB embedding model compares what you type with example tasks and returns a typed answer (one label + a confidence) in milliseconds. It never writes text. It powers the hint bar, the Decide step on the Canvas, and "similar past tasks".
- **System 2 — slow reasoning.** The brain model (Gemma 4, Qwen3…) plans, uses tools and writes answers.

This is the same idea as "System One" decision models such as TypeSafe AI's **Jev** — but NestCrew uses only **free, open-source** ones that run on this PC:

| Engine (Models page → System 1 engine) | What happens | Cost / privacy |
|---|---|---|
| **Laya 421M** ⭐ default | Open System One model (Apache-2.0): typed decisions with confidence, CPU, ~1 GB RAM. Installed by the one-click installer; NestCrew starts it automatically | Free · nothing leaves the PC |
| **Kev / OpenJev / Cribl** | Larger open System One models on your own server (see below) | Free · nothing leaves the PC |
| **MiniLM** | 46 MB embedding model; also the automatic fallback when the chosen server isn't running | Free · nothing leaves the PC |

If nothing else is available, System 1 falls back to keyword matching, so hints always work.

### Open System One models (Jev-compatible, run on this PC)

All of these speak the same `/v1/systemone` protocol as TypeSafe's Jev, so NestCrew talks to them the same way. Pick one in **Models → System 1 engine**; if its server isn't running, NestCrew falls back to the local engine.

| Model | Size | Runs on | Start it | NestCrew expects |
|---|---|---|---|---|
| **Laya** (convaiinnovations) | 421M (ModernBERT) | CPU, ~1 GB RAM | Automatic — installed by the installer (or **Install Laya** on the Models page) | `127.0.0.1:8000`, model `typed-decisions` |
| **Kev** (Jared Palmer) | 0.8B / 4B / 9B (Qwen3.5 + LoRA) | GPU or Apple Silicon; 0.8B can run on CPU slowly | `git clone https://github.com/jaredpalmer/kev` → `uv sync --extra serve` → `uv run --extra serve python -m kev.serve --run jaredpalmer/kev-0.8b --port 8009` | `127.0.0.1:8009` |
| **OpenJev-8B** | 8B (Qwen3-8B + LoRA) | GPU | `pip install "wev-ai[serve]"` → `wev serve --model alanhuangya/OpenJev-8B --port 8009` | `127.0.0.1:8009` |
| **cribl-decision-1.0** | Qwen3.5-4B + LoRA | GPU | Load with transformers + peft behind a `/v1/systemone` readout server (the model card has no ready server yet) | `127.0.0.1:8010` |

Change a URL, model name or add a key under `system1: servers:` in `config.yaml`, e.g.

```
system1:
  engine: laya
  servers:
    kev: { url: http://127.0.0.1:8009, model: kev-latest }
```

Honest notes: these projects are only weeks old. Laya's typed-decisions checkpoint scores about 0.77 on its own benchmark, the base checkpoints are close to chance without fine-tuning, and none of them look at images yet — image questions go to the free local vision models instead. For an 8 GB laptop, Laya is the practical choice; Kev 4B+, OpenJev and Cribl want a GPU.


**While you assign work**, the hint bar shows what the task looks like (e.g. *Invoice / form / table · 66%*) and buttons to apply the advice: switch mode, turn on Screenshots or Commands, attach an image, use the recommended model, or build it on the Canvas. If the guess is wrong, pick the right type in the hint's dropdown — System 1 remembers the correction.

**While you switch models** (Models page), each job shows ★ for the recommended model, greys out models that can't do the job (e.g. a tiny VLM for agents), and tells you whether a model is light, fine or heavy for this PC's RAM.

Without the embedding model, System 1 falls back to keyword matching — still useful, just less sharp.

## 9. Canvas — visual automations

The Canvas is a node editor: steps are boxes, arrows carry text from one step to the next as `{input}`.

| Step | Does | Hint |
|---|---|---|
| ▶ Trigger | Starts the workflow: manual, **watch** a workspace folder, **daily** at a time, or **every** N minutes | Every workflow has exactly one |
| 🖥 Look at screen | Screenshot + small screen model | Asks permission each run unless you allow it for automatic runs |
| 👁 Look at image | Reads the trigger's image with the job's model | `documents` for invoices, `image` for photos |
| 🎬 Analyse video | Frame-by-frame + CSV/chart | Use with a watch trigger |
| ◆ Decide (System 1) | Picks a label instantly (Laya) | Give a question and a few examples per label; set which label each outgoing arrow follows |
| ? If text contains | Plain text check, yes/no branch | Set an arrow to `no` for the other branch |
| ✦ Ask a model | One model call, no tools | Checking, rewriting, summarising |
| 🤖 Agent | Agent with tools | Tick tools; pre-approve risky ones for automatic runs |
| 👥 Crew | Runs a crew recipe | Inputs accept `{input}` and `{file}` |
| 📄 Save to note | Appends to a note in workspace | Template placeholders `{input} {date} {file}` |

How to use it:

1. Open **Canvas**. Pick a template (Invoice check, Screenshot notes, Daily brief, Ask about an image), a blank workflow, or one you captured.
2. Click **Add step** buttons; drag from the dot on a step's right edge onto another step to connect them.
3. Click a step to edit it (the panel on the right explains each field). Click an arrow to label or delete it.
4. **▶ Run now** — steps light up live (running → done / failed / skipped) and arrows animate. If a step needs approval, the card appears in the Progress panel.
5. To run by itself: set the Trigger to watch / daily / every, **Save**, then tick **Automatic**. It also shows up under Automations.

**Capture a finished task:** after any Agent, Crew or Video run, press **◇ Capture as workflow**. NestCrew turns it into Trigger → the same step(s) → Save result, with the tools you approved pre-approved — ready to repeat or schedule.

### Save a task as a flow or an RPA bot

Under every agent answer:

- **◇ Save as AI flow** — Trigger → Agent (same goal, same tools) → Save result. The model works it out again each time, so it adapts when things change.
- **⚙ Save as RPA bot** — every successful tool call becomes a fixed step (e.g. *list folder → copy files → append Excel rows*). It replays exactly, with **no AI**, so it's fast and predictable. Steps you approved when the task first ran are pre-approved for the bot. Edit any step's arguments on the Canvas (Use one tool → Arguments).

### Versions and compare

Every **Save** keeps the previous version. Click **🕘 History** on the Canvas:
- **Compare with now** — the canvas highlights **＋ new**, **✎ changed** and **− removed** steps (removed ones appear as red ghosts), and the side panel lists every changed field before → after.
- **Restore** — brings an old version back; the version you had is kept too, so you can always switch back.

## 10. Brain — the learning loop

Every agent, crew, video, workflow and automation run is recorded as a **trace**: the task, every tool call with its result, approvals, and the answer. The **Brain** page shows them, a map of your tasks by type (bubble size = runs, colour = success rate), and search.

The loop:

1. **Run** a task.
2. **Rate it** — 👍 Good or 👎 Bad (with a note).
3. **Immediately:** 👍 runs become lessons the agent reads before similar tasks, and appear under *Similar past tasks*. 👎 opens an incident and is kept out of training.
4. **Repeat** good runs: capture them as workflows.
5. **Train** (optional, later): **Export training data** writes `workspace\training\good_*.jsonl` (and `rejected_*.jsonl`) in chat format.

Fine-tuning your own small model from that file (needs a GPU — a free Google Colab GPU works):

1. Collect at least ~100 👍 runs of the kind of work you want improved.
2. Fine-tune a small model (e.g. Qwen3 1.7B or Qwen3.5 0.8B) with LoRA using **Unsloth** or **LLaMA-Factory** on the `good_*.jsonl` file.
3. Export the result as a GGUF file and import it into Ollama with a Modelfile (`FROM ./my-model.gguf`, then `ollama create my-agent -f Modelfile`).
4. Add it to `models.yaml` (`tag: my-agent`, `tools: true`) and pick it for the **agent** job.
5. Compare it against the original on the same tasks before switching for good.

## 11. Incidents

The **Incidents** page collects what went wrong, automatically:

| Kind | When |
|---|---|
| error | A run crashed |
| blocked | A dangerous command was refused by the blocklist |
| timeout | An approval wasn't answered within 5 minutes |
| step_limit | An agent ran out of steps |
| stopped | You pressed Stop |
| bad_result / reported | You pressed 👎 or *Report a problem* |

Open the run to see exactly which step failed, then **Resolve** with a note of what fixed it (a better model for that job, a clearer prompt, a pre-approved tool…). The red badge in the sidebar counts open incidents.

## 12. Everyday examples

| You want to… | Do this |
|---|---|
| Ask about an error on screen | Agent mode → tick *Screenshots* → "What does the error on my screen mean and how do I fix it?" |
| Repeat something that worked | Under the answer → **◇ Capture as workflow** → set a trigger → Automatic |
| Sort messages into "urgent / later" | Canvas: Trigger → Ask a model → **Decide** (urgent / later) → two Save to note steps |
| Turn a folder of receipts into a table | Turn on *Invoice reader*, drop photos in, then ask an agent: "Make a CSV of notes/invoices.md" |
| Count people in a CCTV clip | Video tab → attach clip → "How many people are visible?" → chart + CSV |
| Research and write a report | Crew tab → *Research & Write* → topic (web search works when online) |
| Compare models on the same image | Models → *Use in chat* on each model, ask the same question |
| Run a command safely | Agent tab → tick *Run commands* → you approve each one; dangerous ones are blocked |

## 13. Keep the laptop happy

- In `config.yaml`, `keep_alive: 5m` unloads a model after 5 idle minutes; `0` frees RAM immediately.
- Keep `num_ctx: 4096` unless you have spare RAM.
- On 8 GB laptops, set Windows environment variables `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1`.
- For long videos, increase `video_frame_every` rather than `video_max_frames`.
- On battery, use the 256M models.

## 14. Models to watch

Small models improve every few months. Ones worth checking:

- **Qwen3.5** — the 0.8B model is in the catalog; larger sizes in the same family add stronger vision and tools.
- **LFM2.5-VL** (Liquid AI) — newer versions of the 450M and 1.6B models.
- **DeepSeek-OCR** — strong document OCR (~3B total, small active part); not a chat model.
- **Molmo 1B** — can point at objects in images to ground its answers.
- **SmolVLM2 2.2B** — the bigger sibling when 500M isn't enough.
- **Qwen3-VL** — new sizes and quantizations appear regularly in the Ollama library.
- **Gemma 4 E2B** — a lighter brain with vision and tools.
- **FastVLM** (Apple) — very fast, but aimed at Apple devices.

To try one: add it to `models.yaml`, download it, assign it to a job, and compare answers on the same image.

## 15. Troubleshooting

| Problem | Fix |
|---|---|
| "Failed to fetch" / "NestCrew isn't running" | The window was left open after NestCrew closed. Close it and open **NestCrew** from the Desktop |
| "Ollama is not running" | Open the Ollama app, or run `ollama serve` |
| A model downloads but can't see images | Run it with llama.cpp and set `backend: llamacpp` |
| Agent loops or gives weird JSON | Use a bigger brain (Gemma 4 E4B) for the agent job |
| Agent says a folder is "outside the folders NestCrew may use" | Add it under `rpa: folders:` in config.yaml (e.g. `D:/Work`) |
| A move went wrong | Ask "undo the last move" — it puts the files back |
| Stop doesn't react | It now cuts the model off mid-answer; give it a second or two |
| Answers are slow | Use a smaller eyes model; close other heavy apps; lower `num_ctx` |
| Automation didn't run | Is it ticked on? Is NestCrew open? Files are picked up ~30 s after they finish copying |
| Permission card never appeared | Look in the Progress panel or Chat; unanswered requests auto-deny after 5 minutes |
| Hints feel random | Download *MiniLM embeddings (System 1)* on the Models page; correct wrong guesses in the hint dropdown |
| Workflow step "No image" | Vision/video steps need a **watch** trigger so they receive a file; for Run now, put a file in that folder first |
