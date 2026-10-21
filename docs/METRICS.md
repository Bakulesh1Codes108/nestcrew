# NestCrew in numbers

What it costs, how many tokens it uses, how fast it is — and how each number was measured, so you can
check it yourself. Re-run the benchmark on your own machine with:

```bash
python scripts/benchmark.py          # add --json for machine-readable output
```

It needs no model and no internet and works only inside a temporary folder.

## Cost

| | NestCrew | Typical cloud agent |
|---|---|---|
| Per-task API cost | **$0** — models run on your own CPU via Ollama | billed per input + output token |
| Plain file commands (copy, move, clean, sort, phone backup…) | **0 model calls, 0 tokens** — turned into exact steps by rules | every step is an LLM call |
| Open-ended tasks | local model: ~1.6k prompt tokens per step, output capped at 512 | same work, billed |
| Your files leave the PC | **never** (cloud models are opt-in, keys in `.env`) | sent to the provider |
| Voice | Whisper on the PC (~150 MB once), $0 per minute | per-minute speech API |

## Tokens

| Metric | Value | How measured |
|---|---|---|
| Agent system prompt (what one model step reads) | ~6,300 characters ≈ **1,580 tokens**, 34 tools | `scripts/benchmark.py` (chars ÷ 4) |
| Output cap per agent step | **512 tokens** (JSON steps), 1,024 (text answers) | `config.yaml` → `local.max_tokens*` |
| Context window | 4,096 tokens; 2,048 on laptops with < 12 GB RAM | `providers.py` |
| Plain file command | **0 tokens** | benchmark asserts the model is never called |
| Same command if a model planned it | ≈ 2–3 steps × 1.6–2k tokens ≈ **4–6k tokens** | estimate from the prompt size |

Why it matters: on a laptop CPU a small model reads and writes slowly, and in live testing a 2B "thinking"
model spent its **entire 512-token budget reasoning and returned an empty answer**. NestCrew now caps output,
retries with a bigger budget, recovers the answer from the model's reasoning, prefers the non-thinking
"instruct" build — and skips the model completely when a command is unambiguous.

## Speed

Benchmark machine: cloud VM, **2 vCPU** (Intel Xeon 2.1 GHz), Linux, Python 3.11, SSD. Laptop numbers: HP
laptop, Windows 11, 8–16 GB RAM, CPU only.

| Metric | Value | Where |
|---|---|---|
| Command → exact plan | **0.18 ms** median, 0.59 ms p95 | benchmark |
| Routing a message (chat vs agent vs file task) | **~54 ms** median | benchmark |
| Copy 2,000 photos × 200 KB (400 MB), batches + 4 parallel copies | **1.1 s** — ~1,850 files/s, ~370 MB/s | benchmark |
| Move 2,000 files on the same drive (10 batches, undo log) | **0.5 s** | benchmark |
| Byte-exact duplicate scan, 2,285 files / 457 MB | **0.5 s** (size → first 64 KB → full hash) | benchmark |
| Full folder report (types, largest, duplicates, junk, empty folders) | **0.5 s** for 2,285 files | benchmark |
| One agent step with Qwen3.5 0.8B (incl. loading the model) | **~13 s** | your laptop, live |
| "make BIRAC2 folder clean" — scan + 3 checks, nothing to remove | seconds, **no approval prompts, no model** | your laptop, live |
| App restart after an update | ~30–40 s (loads the System 1 model) | your laptop, live |
| Phone → PC over Wi-Fi, 3 files / 3 MB, each size-verified | completed, all verified | phone-sized browser, test |

## Footprint

| Component | Size |
|---|---|
| Vision models | 256M–2B parameters · 0.3–2.4 GB download · 0.6–3.5 GB RAM |
| Agent "brain" (default on 8–16 GB laptops) | Qwen3-VL 2B instruct · ~2 GB |
| System 1 router | MiniLM 46 MB (or Laya ~421M params) |
| Voice | Whisper base ~150 MB (small ~480 MB for Indian accents) |
| App itself | Python + one HTML file — no build step, no Electron |

## Quality and safety

| | |
|---|---|
| Automated tests | **312**, offline (scripted stand-in models), CI on **Windows + Linux**, Python 3.11 + 3.12 |
| Approval coverage | 100% of file changes show the exact files before anything happens |
| Deletes | Windows Recycle Bin only; nothing on a phone is ever deleted |
| Overwrites | never — clashes become `name (2).ext`, identical copies are skipped |
| Network | main app on 127.0.0.1 only; phone link: home-network addresses + QR secret, auto-off after 30 idle min |
| Audit | every action and decision in `data/audit.jsonl` |
| Bugs found by testing on a real laptop / Windows CI | empty answers from thinking models · wrong folder chosen for "BIRAC" · old server kept running after update · "folder" read as part of a name · Windows `AppData` temp path tripping the sandbox · Python's `is_private` accepting public test ranges · PyAV 19 breaking audio decoding |

## Ways to make it better (contributions welcome)

| Idea | Why | Effort |
|---|---|---|
| **Per-task token & time log** in Brain | replace the 4–6k estimate with real averages per task type | small |
| **More exact-command patterns** (Excel: "add totals.csv to report.xlsx", rename series, zip/unzip) | more tasks at 0 tokens | small |
| **GPU / NPU offload** (CUDA, DirectML, Intel NPU via OpenVINO) | 5–20× faster agent steps on laptops that have one | medium |
| **Streaming voice** (Whisper on 1-second chunks) + **spoken replies** (Piper TTS) | hands-free use | medium |
| **Signed updates** (verify a release signature before applying) | stronger supply-chain safety than "HTTPS from github.com" | medium |
| **Android companion** that deletes only files the PC has verified | one-tap "free up space" after a backup | medium |
| **Local RAG** over Documents (embeddings in SQLite) | answer questions about your own files offline | medium |
| **Fine-tune from Brain traces** (LoRA on 👍 runs) | a small model that knows your folders and phrasing | large |
| **Desktop click recording** as bot steps | RPA for apps without files/APIs | large |
| **Packaged installer** (single signed `.exe`) | no Python setup for non-technical users | medium |
| **Eval harness**: 100 real commands → success rate per model | pick models by measured accuracy, not size | small |
