# Using NestCrew

Type what you want in **Chat**, in your own words. NestCrew works out whether it's a question
(answered by the local model) or a job on your files (run as an **Agent** with tools). Anything that
*reads* happens straight away; anything that *changes* something shows an **approval card** with the
exact files first — **Allow once**, **Allow for this task** or **Deny**. **Stop** halts everything.

House rules, everywhere:
- Nothing is ever overwritten — a clash becomes `name (2).ext`; identical copies are skipped.
- "Delete" always means the **Windows Recycle Bin** (restorable). Nothing on a phone is ever deleted.
- Moves and sorts can be undone: `undo last move`.
- Only Desktop, Documents, Downloads, Pictures, Videos, Music, the workspace and other drives
  (D:, E:, USB, SD — never C:\Windows or C:\Program Files) can be touched. `.ssh`, `AppData`, `.git` are refused.
- Every action is written to `data/audit.jsonl`; every task is saved in **Brain**.

---

## 1. Files and folders

| You type | What happens |
|---|---|
| `copy all files from BIRAC to BIRAC2` | finds BIRAC (Desktop first, then inside your folders), lists it, asks, copies |
| `move the pdfs from Downloads to Documents/Invoices` | only `*.pdf`; undo with `undo last move` |
| `move all photos from Downloads and Desktop including subfolders to Pictures/Sorted` | several folders + subfolders, in batches, progress shown |
| `copy all videos from D drive to Pictures/Videos` | other drives: `D drive`, `drive E`, `E:/DCIM` |
| `create a folder called Reports 2026 on my desktop` | makes it |
| `copy all files from BIRAC to "Old folder"` | quotes keep words like *folder* in a name |

Kinds you can name: photos/images, videos, music, documents, pdfs, excel, csv, screenshots, screen recordings,
or a pattern like `*.heic`. If a name exists in two places, NestCrew asks which one.

## 2. Clean up and free space

| You type | What happens |
|---|---|
| `how much space is left on my drives` | free / used on every drive, warns above 90% |
| `scan Downloads` | size by type, largest files, duplicates and how much they'd free, junk, empty folders, old files |
| `make BIRAC2 folder clean` / `clean up Downloads` | junk → duplicates → empty folders, one card each; "Already clean" if nothing to do |
| `delete duplicates in Pictures` | byte-exact copies only; the oldest copy is kept |
| `organize Downloads by type` | Images / Videos / Audio / Documents / Archives / Apps / Other — undoable |
| `sort the files in Pictures by month` | 2026-01, 2026-02 … (or `by year`) |

## 3. Phone

### Over Wi-Fi (fast, no app, no cable)

1. Phone and PC on the same Wi-Fi (or the phone's hotspot).
2. Sidebar **📱 Phone** → **Start Wi-Fi link** (or type `connect my phone over wifi`).
3. Scan the QR code with the phone's camera. A page opens in the phone's browser.
4. **Choose photos & videos** → each upload shows ✓ when the PC has every byte. When all are ✓ the page says
   *"safe to delete from your Gallery"* — that's your signal to free up space on the phone.
5. Files the PC shares appear under **From the PC** — tap to download.

| You type | What happens |
|---|---|
| `send Reports to my phone` | copies them to `Desktop/Send to phone`, starts the link |
| `receive files from my phone` | starts the link; files land in `Downloads/From phone/<date>` |
| `what did my phone send` | list of received files |
| `stop phone link` | switches it off (it also stops after 30 idle minutes and when NestCrew closes) |

Then tidy what arrived: `delete duplicates in From phone`, `organize From phone by month`.

Safety: a second, separate server that only answers devices on your own network (192.168.x, 10.x, 172.16-31.x)
that have the secret code from the QR; it can only write into the inbox and only read from *Send to phone*.
First time, Windows Firewall may ask — allow on **Private** networks only.

### By Bluetooth (slow, ~1–2 MB/s, no Wi-Fi needed)

| You type | What happens |
|---|---|
| `receive photos from my phone via bluetooth` | opens Windows' Bluetooth *Receive* window → on the phone: Share → Bluetooth → this PC |
| `send Desktop/Reports to my phone via bluetooth` | copies to *Send to phone* and opens Windows' Bluetooth *Send* window |

Also on the **📱 Phone** page: *Receive from phone* / *Send to phone* buttons.

### By USB cable

Plug in, unlock, choose **File transfer** on the phone.

| You type | What happens |
|---|---|
| `show my phone` | its storage and folders |
| `backup my phone` | copies DCIM to `Pictures/Phone backup`, 50 per batch, skipping ones already copied |
| `copy my phone camera photos to D drive/Phone` | only `DCIM/Camera`, photos |

## 4. Excel and CSV

`add these rows to Documents/expenses.xlsx: …`, `read Documents/report.xlsx`, `make an Excel of the files in Downloads`.
Excel files are backed up to `workspace/.backups` before any change.

## 5. Repeat it: bots, flows and automations

After any task: **⚙ Save as RPA bot** (replays the exact steps, no AI) or **◇ Save as AI flow** (repeats the goal).
On the **Canvas**, give it a trigger — *watch a folder*, *every day at 9:00*, *every N minutes* — and pre-approve the
tools it may use unattended. **🕘 History** compares any saved version with now and restores it.

## 6. Models and System 1

**Models** page: one "brain" model for chat/agents (Qwen3-VL 2B instruct on an 8–16 GB laptop) and tiny "eyes"
models for screens, video and documents. **System 1** (Laya, MiniLM fallback) routes each message in milliseconds.
Everything runs offline; cloud models are optional (keys in `.env`).

## 7. When something's off

| You see | Do this |
|---|---|
| *NestCrew was updated — Restart now* | press it (closing only the window leaves the old one running) |
| *NestCrew isn't running* right after a restart | wait ~30 s; it reloads by itself |
| Phone can't open the link | same Wi-Fi? Windows Firewall → allow Python on Private networks; try `stop phone link` then start again |
| A task went to chat instead of doing it | press **Switch to Agent**, or pick **Agent** under the box |
| Something went wrong | 👎 under the answer opens an **Incident**; `undo last move`; Recycle Bin |
