# NestCrew — resume / portfolio notes

**NestCrew — local-first AI agent & RPA platform** · Python, FastAPI, SQLite, Ollama, JavaScript
github.com/Bakulesh1Codes108/nestcrew

Suggested bullets (pick 3–4):

- Built a local-first AI agent platform that runs open vision-language models (Qwen3-VL, Gemma 4, SmolVLM2) fully offline on a laptop, with optional cloud LLM fallback.
- Designed an RPA toolkit (file/folder operations, Excel/CSV read-write) with a sandboxed path resolver, human-in-the-loop approval previews, undo logs, automatic backups and no destructive operations.
- Implemented a multi-agent architecture: a JSON tool-calling agent loop that delegates sub-tasks to specialist agents (file clerk, Excel analyst, researcher, writer) under a shared permission gate.
- Built a visual workflow editor (DAG engine, branching on typed "System 1" decisions, folder-watch/schedule triggers) where finished agent runs can be captured as replayable RPA bots, with version history and visual diff/restore.
- Integrated an open-source "System One" decision model (Laya, `/v1/systemone` protocol) for millisecond task routing and hints, with embedding and keyword fallbacks.
- Added observability and a learning loop: every run stored as a trace; ratings feed in-context lessons and a chat-format fine-tuning dataset; failures and blocked actions logged as incidents.
- Added offline multilingual voice control: on-device Whisper (faster-whisper, int8 CPU) turns spoken Marathi, Hindi
  or English into English commands that drive the same agent and RPA pipeline, with spoken approvals ("allow"/"deny").
- Built a Wi-Fi phone link (QR pairing, LAN-only, size-verified uploads) plus USB/MTP and Bluetooth paths for
  backing up and cleaning a phone, and a Recycle-Bin-only clean-up toolkit (duplicates, junk, sorting, batches).
- Hardened the local server (loopback-only, per-launch token, CSP, prompt-injection wrapping, secret redaction, command blocklist, audit log) and covered it with 100+ offline tests in CI on Windows and Linux.

Talking points for interviews:
- Why small local models need a strict JSON protocol, capped output and a "brain + eyes" split.
- How the approval gate works for interactive vs unattended runs.
- AI flow vs RPA bot: flexibility vs determinism.
- How versioned workflows are diffed (node/edge/config level) and restored without losing history.

Note: parts of this project were built with an AI coding assistant; be ready to explain the design
decisions above in your own words.
