"""Screenshots, images and video -> frames -> timeline -> chart. All local.

Extra packages (installed by setup scripts):  pip install -e ".[media]"
    mss                     - screenshots
    Pillow                  - resizing
    opencv-python-headless  - reading video frames
"""
from __future__ import annotations

import base64
import csv
import io
import time
from pathlib import Path
from typing import Callable

from .agent import parse_json
from .config import load_config, resolve
from .providers import Provider


def _cfg() -> dict:
    return load_config().get("media", {})


def _need(pkg: str, pip_name: str):
    raise RuntimeError(f"'{pkg}' is not installed. Run: pip install {pip_name}  "
                       f"(or pip install -e \".[media]\")")


def _jpeg_b64(img, max_side: int | None = None) -> str:
    """PIL image -> shrunk JPEG base64."""
    max_side = max_side or _cfg().get("image_max_side", 1024)
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------ screenshots ----
def take_screenshot() -> Path:
    try:
        import mss
    except ImportError:
        _need("mss", "mss")
    try:
        from PIL import Image
    except ImportError:
        _need("Pillow", "Pillow")
    folder = resolve(_cfg().get("screenshots_dir", "./data/screenshots"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screen_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])            # primary monitor
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.thumbnail((_cfg().get("image_max_side", 1024),) * 2)
    img.save(path, "JPEG", quality=85)
    return path


def describe_image(provider: Provider, image_b64: str, question: str) -> str:
    return provider.chat([
        {"role": "system", "content": "You look at images carefully and answer precisely."},
        {"role": "user", "content": question, "images": [image_b64]},
    ])


def look_at_screen(provider: Provider, question: str, keep: bool | None = None) -> dict:
    """Capture -> analyse -> delete (unless keep). Returns answer + where it's kept."""
    keep = _cfg().get("keep_screenshots", False) if keep is None else keep
    path = take_screenshot()
    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
        answer = describe_image(provider, b64, question)
    finally:
        if not keep:
            path.unlink(missing_ok=True)
    return {"answer": answer, "saved_to": str(path) if keep else None}


# ------------------------------------------------------------------ video ----
def extract_frames(video_path: str | Path, every: float | None = None,
                   max_frames: int | None = None) -> list[tuple[float, str]]:
    """Return [(seconds, jpeg_b64), ...] sampled evenly; never more than max_frames."""
    try:
        import cv2
    except ImportError:
        _need("opencv", "opencv-python-headless")
    from PIL import Image

    every = every or _cfg().get("video_frame_every", 2)
    max_frames = max_frames or _cfg().get("video_max_frames", 12)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Can't open video: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0
    last = max(0.0, duration - 1 / fps)          # time of the final real frame
    times = [i * every for i in range(int(last // every) + 1)] if duration else [0]
    if len(times) > max_frames:  # spread the budget across the whole video
        step = (len(times) - 1) / (max_frames - 1)
        times = [times[round(i * step)] for i in range(max_frames)]
    frames = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(t * fps)))   # frame index seeks reliably
        ok, frame = cap.read()
        if not ok:
            continue
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        frames.append((round(t, 2), _jpeg_b64(img)))
    cap.release()
    return frames


FRAME_PROMPT = """This is one frame of a video at {t}s.
Question: {q}
Reply with JSON only: {{"description": "<one sentence about this frame>", "value": <a number answering the question for this frame, or null if not numeric>}}"""


def analyze_frames(provider: Provider, frames: list[tuple[float, str]], question: str,
                   on_event: Callable[[dict], None] | None = None) -> dict:
    """Frames are analysed ONE AT A TIME so only one image is in memory per call."""
    rows = []
    for i, (t, b64) in enumerate(frames, 1):
        raw = provider.chat([{"role": "user", "content": FRAME_PROMPT.format(t=t, q=question),
                              "images": [b64]}], json_mode=True)
        data = parse_json(raw) or {"description": raw.strip()[:200], "value": None}
        val = data.get("value")
        try:
            val = float(val) if val is not None and val != "" else None
        except (TypeError, ValueError):
            val = None
        row = {"time": t, "description": str(data.get("description", ""))[:300], "value": val}
        rows.append(row)
        if on_event:
            on_event({"type": "frame", "n": i, "of": len(frames), **row})
    timeline = "\n".join(f"{r['time']}s: {r['description']}"
                         + (f" (value={r['value']:g})" if r["value"] is not None else "")
                         for r in rows)
    summary = provider.chat([
        {"role": "system", "content": "You summarise video timelines clearly."},
        {"role": "user", "content": f"Question: {question}\n\nFrame-by-frame notes:\n{timeline}"
                                    "\n\nAnswer the question for the whole video. Mention key moments with timestamps."},
    ])
    return {"question": question, "rows": rows, "summary": summary.strip(),
            "has_values": any(r["value"] is not None for r in rows)}


def analyze_video(provider: Provider, video_path: str | Path, question: str,
                  on_event: Callable[[dict], None] | None = None) -> dict:
    if on_event:
        on_event({"type": "status", "text": "Slicing video into frames…"})
    frames = extract_frames(video_path)
    if on_event:
        on_event({"type": "status", "text": f"Analysing {len(frames)} frames one by one…"})
    return analyze_frames(provider, frames, question, on_event)


# ----------------------------------------------------------------- outputs ----
def save_outputs(result: dict, name: str, folder: Path) -> dict:
    """Write timeline CSV + an SVG chart (opens in any browser, offline)."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem or "video"
    csv_path = folder / f"{stem}_timeline.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time", "value", "description"])
        w.writeheader()
        w.writerows(result["rows"])
    out = {"csv": str(csv_path)}
    if result.get("has_values"):
        svg_path = folder / f"{stem}_chart.svg"
        svg_path.write_text(svg_chart(result["rows"], result["question"]), encoding="utf-8")
        out["chart"] = str(svg_path)
    return out


def svg_chart(rows: list[dict], title: str, w: int = 720, h: int = 320) -> str:
    pts = [(r["time"], r["value"]) for r in rows if r["value"] is not None]
    if not pts:
        return ""
    pad_l, pad_r, pad_t, pad_b = 48, 16, 36, 36
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(0, min(ys)), max(ys) or 1
    sx = lambda x: pad_l + (x - x0) / ((x1 - x0) or 1) * (w - pad_l - pad_r)
    sy = lambda y: h - pad_b - (y - y0) / ((y1 - y0) or 1) * (h - pad_t - pad_b)
    line = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in pts)
    dots = "".join(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3.5"><title>{x}s: {y:g}</title></circle>'
                   for x, y in pts)
    grid = "".join(
        f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" class="g"/>'
        f'<text x="{pad_l - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:g}</text>'
        for v in [y0 + (y1 - y0) * k / 4 for k in range(5)])
    esc = title.replace("&", "&amp;").replace("<", "&lt;")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="system-ui,sans-serif" font-size="11">
<style>.g{{stroke:#ddd}} text{{fill:#666}} polyline{{fill:none;stroke:#c4552d;stroke-width:2}} circle{{fill:#c4552d}}</style>
<rect width="100%" height="100%" fill="#fff"/>
<text x="{pad_l}" y="20" font-size="13" style="fill:#222;font-weight:600">{esc}</text>
{grid}<polyline points="{line}"/>{dots}
<text x="{pad_l}" y="{h - 10}">{x0:g}s</text><text x="{w - pad_r}" y="{h - 10}" text-anchor="end">{x1:g}s</text>
</svg>'''
