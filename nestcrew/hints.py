"""Hints while you assign work: what kind of task is this, which mode, model and
switches fit it. Decided by System 1 (fast, no text generation)."""
from __future__ import annotations

from . import system1

TASK_TYPES: dict[str, dict] = {
    "chat": {
        "label": "Question / chat",
        "examples": ["what is the capital of france", "explain how solar panels work",
                     "give me ideas for a birthday", "translate this sentence to hindi"],
        "keywords": ["what", "why", "how", "explain", "idea", "ideas", "translate", "meaning"],
        "mode": "chat", "model_task": "chat",
        "tip": "A plain chat is enough - no tools needed.",
    },
    "screen": {
        "label": "Look at my screen",
        "examples": ["what does the error on my screen mean", "look at my screen and tell me what to click",
                     "read this window", "what app is open right now"],
        "keywords": ["screen", "window", "error", "click", "popup", "dialog", "showing"],
        "mode": "agent", "toggles": ["screen"], "model_task": "screen",
        "tip": "Agent mode with Screenshots on. The small screen model looks; the screenshot is deleted after.",
    },
    "documents": {
        "label": "Invoice / form / table",
        "examples": ["extract the total from this invoice", "read this receipt", "turn this table into csv",
                     "what is the gst on this bill", "fill details from this form"],
        "keywords": ["invoice", "receipt", "bill", "form", "table", "gst", "total", "statement", "pdf", "chart"],
        "mode": "chat", "attach": True, "model_task": "documents",
        "tip": "Attach the image; the documents model (Granite Vision) is best at layouts and tables.",
    },
    "ocr": {
        "label": "Read text from an image",
        "examples": ["read the text in this screenshot", "copy the words from this photo", "ocr this image"],
        "keywords": ["text", "ocr", "read", "words", "copy", "transcribe", "handwriting"],
        "mode": "chat", "attach": True, "model": "qwen3-vl-2b",
        "tip": "Qwen3-VL 2B reads small text best. SmolVLM2 500M is faster but misses fine print.",
    },
    "image": {
        "label": "Describe / tag a photo",
        "examples": ["describe this photo", "what is in this picture", "give tags for this image",
                     "how many people are in this photo"],
        "keywords": ["photo", "picture", "image", "describe", "tags", "caption", "count"],
        "mode": "chat", "attach": True, "model_task": "image",
        "tip": "Tiny models are fine here. For counting objects, Moondream is the steadiest.",
    },
    "video": {
        "label": "Analyse a video",
        "examples": ["what happens in this video", "count people in this cctv clip",
                     "summarise this recording", "chart cars per second in the footage"],
        "keywords": ["video", "clip", "footage", "cctv", "recording", "mp4", "frames"],
        "mode": "video", "model_task": "video",
        "tip": "Use the Video mode: frames are read one by one and you get a chart + CSV.",
    },
    "research": {
        "label": "Research & write",
        "examples": ["research electric scooters and write a report", "write a blog post about tea",
                     "compare three laptops and summarise", "find information about"],
        "keywords": ["research", "report", "article", "blog", "compare", "summarise", "summarize", "write"],
        "mode": "crew", "crew": "research_writer", "model_task": "agent",
        "tip": "The Research & Write crew splits the work between a researcher and a writer.",
    },
    "files": {
        "label": "Copy / move / organise files",
        "examples": ["move all files from BIRAC to BIRAC2 on desktop", "copy the pdfs from downloads into documents/invoices",
                     "organise the files in my workspace", "make a folder called reports on the desktop",
                     "rename the photos by date", "list the files in my downloads folder"],
        "keywords": ["file", "files", "folder", "folders", "move", "copy", "desktop", "downloads", "documents",
                     "organise", "organize", "rename", "pdfs", "photos", "list"],
        "mode": "agent", "model_task": "agent", "weight": 1.3,
        "tip": "Agent mode with file tools. Looking is automatic; every copy, move or new folder asks you first, and nothing is ever deleted.",
    },
    "excel": {
        "label": "Excel / CSV",
        "examples": ["read sales.xlsx and total column C", "write these rows into an excel file",
                     "add a row to my expenses spreadsheet", "make a csv of the invoices",
                     "combine all excel files in the folder into one sheet"],
        "keywords": ["excel", "xlsx", "csv", "spreadsheet", "sheet", "rows", "column", "columns", "table"],
        "mode": "agent", "model_task": "agent", "weight": 1.3,
        "tip": "Agent mode with Excel tools: it reads sheets automatically and asks before writing or adding rows.",
    },
    "command": {
        "label": "Run a program / command",
        "examples": ["run the python script", "install this package", "check git status",
                     "convert this file with ffmpeg"],
        "keywords": ["run", "install", "pip", "git", "python", "script", "command", "terminal", "ffmpeg"],
        "mode": "agent", "toggles": ["commands"], "model_task": "agent",
        "tip": "Agent mode with Run commands on. You approve each command; dangerous ones are blocked.",
    },
    "automation": {
        "label": "Do this automatically",
        "examples": ["every morning summarise my notes", "whenever a screenshot is saved describe it",
                     "each time an invoice arrives extract the total", "do this daily at 9",
                     "every hour check the inbox folder", "automate this for me"],
        "keywords": ["every", "daily", "whenever", "automatically", "automate", "schedule", "each time",
                     "always", "weekly", "hourly", "morning", "evening", "night", "monday", "recurring"],
        "weight": 2.0,     # scheduling words decide it even if the rest sounds like another task
        "page": "canvas", "model_task": "agent",
        "tip": "Build it on the Canvas (or turn a finished task into a workflow) and give it a trigger.",
    },
    "math": {
        "label": "Calculate",
        "examples": ["what is 18% gst on 4250", "calculate emi for 5 lakh", "add these numbers"],
        "keywords": ["calculate", "%", "percent", "sum", "emi", "gst", "total", "multiply"],
        "mode": "agent", "model_task": "agent",
        "tip": "Agent mode uses the calculator tool, so the maths is exact.",
    },
}

GROUP = "task_type"


def classify(text: str) -> dict:
    opts = {k: {"examples": v["examples"], "keywords": v["keywords"], "weight": v.get("weight", 1.0)}
            for k, v in TASK_TYPES.items()}
    return system1.choice(text, opts, group=GROUP, quick=True)


def hint(text: str, has_image: bool = False) -> dict:
    """Suggestion for the work you're about to assign."""
    from . import models
    if len(text.strip()) < 4:
        return {}
    r = classify(text)
    kind = r["label"]
    if has_image and kind in ("chat", "files"):
        kind = "image"
    t = TASK_TYPES[kind]
    model_id = t.get("model") or models.task_map().get(t.get("model_task", "chat"))
    entry = models.catalog()["models"].get(model_id, {})
    return {
        "type": kind, "label": t["label"], "confidence": r["confidence"], "method": r["method"],
        "tip": t["tip"], "mode": t.get("mode"), "toggles": t.get("toggles", []),
        "attach": t.get("attach", False), "crew": t.get("crew"), "page": t.get("page"),
        "model": model_id, "model_name": entry.get("name", model_id),
        "model_ready": bool(entry) and models.is_installed(entry),
        "alternatives": [{"type": k, "label": TASK_TYPES[k]["label"], "p": p}
                         for k, p in list(r["probs"].items())[1:3]],
    }


def correct(text: str, true_type: str) -> None:
    """You picked a different task type -> System 1 learns from it."""
    if true_type in TASK_TYPES:
        system1.teach(GROUP, text, true_type)


# ----------------------------------------------------------- model fit hints --
def system_ram_gb() -> float | None:
    """Total RAM of this PC in GB (None if unknown)."""
    import os
    import sys
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.total / 1024 ** 3, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
    except (OSError, ValueError, AttributeError):
        return None


def fit(ram_text: str, ram_gb: float | None) -> str:
    """'easy' | 'ok' | 'heavy' | 'unknown' - does this model's RAM need suit this PC?"""
    import re
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", ram_text)]
    if not nums or not ram_gb:
        return "unknown"
    need = max(nums)
    if need <= ram_gb * 0.2:
        return "easy"
    if need <= ram_gb * 0.45:
        return "ok"
    return "heavy"


JOB_NEEDS = {
    "chat": "Any model; vision models can also read attached images.",
    "agent": "Needs tool use (the app won't allow others). Bigger = more reliable plans.",
    "screen": "Needs vision. Small text on screens: Qwen3-VL 2B; speed: SmolVLM2.",
    "video": "Needs vision. Runs once per frame, so the smallest models are best.",
    "documents": "Needs vision. Layout-trained Granite Vision handles tables and forms best.",
    "image": "Needs vision. Tiny models are fine for captions and tags.",
}


def assign_hint(job: str, model: dict) -> dict:
    """Is this model a good choice for this job? Shown while you switch models."""
    if model.get("kind") == "embedding":
        return {"level": "no", "text": "Embedding model - powers System 1 decisions, not answers."}
    if job == "agent" and not model.get("tools"):
        return {"level": "no", "text": "Can't use tools, so it can't run agents."}
    if job in ("screen", "video", "documents", "image") and not model.get("vision"):
        return {"level": "no", "text": "Text-only model - it can't see images."}
    rec = {"chat": ["gemma4-e4b", "qwen3.5-0.8b", "qwen3-1.7b"],
           "agent": ["gemma4-e4b", "qwen3-1.7b", "qwen3.5-0.8b"],
           "screen": ["smolvlm2-500m", "qwen3-vl-2b", "smolvlm2-256m"],
           "video": ["smolvlm2-256m", "smolvlm2-500m", "lfm2-vl-450m"],
           "documents": ["granite-vision-2b", "qwen3-vl-2b"],
           "image": ["lfm2-vl-450m", "smolvlm2-256m", "moondream"]}.get(job, [])
    if model.get("id") in rec[:1]:
        return {"level": "best", "text": "Recommended for this job."}
    if model.get("id") in rec:
        return {"level": "good", "text": "Good fit for this job."}
    return {"level": "ok", "text": "Works, but not the usual pick for this job."}
