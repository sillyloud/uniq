import json
import os
import random
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="VideoUniq")

# Serve the frontend
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
@app.head("/")
async def index_head():
    return HTMLResponse(content="", status_code=200)

@app.post("/process")
async def process_video(
    video: UploadFile = File(...),
    options: str = Form(default="{}"),
):
    opts = json.loads(options)
    strength = int(opts.get("strength", 3))  # 1–5

    # ── Validate file ──
    if not video.content_type or not video.content_type.startswith("video/"):
        # also accept common extensions even if content_type is wrong
        ext = Path(video.filename or "").suffix.lower()
        if ext not in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".ts", ".m4v"}:
            raise HTTPException(400, "Unsupported file type")

    # ── Save upload to temp file ──
    suffix = Path(video.filename or "video.mp4").suffix or ".mp4"
       suffix = Path(video.filename or "video.mp4").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_in:
        while True:
            chunk = await video.read(1024 * 1024)
            if not chunk:
                break
            tmp_in.write(chunk)
        input_path = tmp_in.name

    output_path = input_path.replace(suffix, f"_uniq{suffix}")

    try:
        filters = _build_filters(opts, strength)
        cmd = _build_ffmpeg_cmd(input_path, output_path, filters, opts, strength)
        _run_ffmpeg(cmd)
    except Exception as exc:
        _cleanup(input_path, output_path)
        raise HTTPException(500, f"ffmpeg error: {exc}") from exc

    if not Path(output_path).exists():
        _cleanup(input_path)
        raise HTTPException(500, "Output file was not created")

    filename = f"uniq_{uuid.uuid4().hex[:8]}{suffix}"

    response = FileResponse(
        output_path,
        media_type="video/mp4",
        filename=filename,
        background=None,
    )

    # Clean up input; output will be cleaned after response is sent
    # FastAPI's FileResponse streams the file before it can be deleted,
    # so we register a shutdown cleanup instead.
    _cleanup(input_path)
    # Register output for deferred cleanup (best-effort)
    _PENDING_CLEANUP.add(output_path)

    return response


# ── Deferred cleanup set ──
_PENDING_CLEANUP: set[str] = set()


@app.on_event("shutdown")
async def _on_shutdown():
    for p in list(_PENDING_CLEANUP):
        _cleanup(p)


# ─────────────────────────────────────────────
# Filter / command builders
# ─────────────────────────────────────────────

def _build_filters(opts: dict, strength: int) -> list[str]:
    """Return a list of ffmpeg video filter strings."""
    vf = []

    # Visual filter: subtle brightness / contrast / saturation tweak
    if opts.get("visual", True):
        # strength 1→0.3%  5→1.5%  (barely visible even at max)
        delta = 0.003 * strength          # brightness offset
        contrast = 1.0 + 0.002 * strength
        saturation = 1.0 + 0.003 * strength
        vf.append(
            f"eq=brightness={delta:.4f}:contrast={contrast:.4f}:saturation={saturation:.4f}"
        )

    # Horizontal flip
    if opts.get("flip", False):
        vf.append("hflip")

    # Zoom / crop: scale up slightly then crop back to original size
    if opts.get("crop", False):
        zoom_pct = 1 + 0.005 * strength   # 0.5%–2.5%
        vf.append(
            f"scale=iw*{zoom_pct:.4f}:ih*{zoom_pct:.4f},"
            "crop=iw/{{zoom_pct:.4f}}:ih/{{zoom_pct:.4f}}".replace(
                "{zoom_pct:.4f}", f"{zoom_pct:.4f}"
            )
        )
        # Simpler alternative that works reliably:
        vf[-1] = (
            f"scale='iw*{zoom_pct:.4f}':'ih*{zoom_pct:.4f}',"
            f"crop=iw/{zoom_pct:.4f}:ih/{zoom_pct:.4f}"
        )

    return vf


def _build_ffmpeg_cmd(
    input_path: str,
    output_path: str,
    video_filters: list[str],
    opts: dict,
    strength: int,
) -> list[str]:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
    ]

    # ── Video codec ──
    cmd += ["-c:v", "libx264"]

    # Quality: strength 1 = CRF 23 (high quality), strength 5 = CRF 28
    crf = 22 + strength
    cmd += ["-crf", str(crf), "-preset", "fast"]

    # Apply video filters if any
    if video_filters:
        cmd += ["-vf", ",".join(video_filters)]

    # ── Audio codec ──
    if opts.get("audio", True):
        # Pitch shift: ±1–2 cents, inaudible but changes fingerprint
        cents = random.choice([-1, 1]) * (0.5 + 0.3 * strength)
        # tempo compensation keeps duration identical
        rate_factor = 2 ** (cents / 1200)
        cmd += [
            "-c:a", "aac",
            "-af", f"asetrate=44100*{rate_factor:.6f},aresample=44100",
            "-b:a", "192k",
        ]
    else:
        cmd += ["-c:a", "copy"]

    # ── Metadata strip ──
    if opts.get("metadata", True):
        cmd += [
            "-map_metadata", "-1",
            "-fflags", "+bitexact",
        ]

    # ── Re-encode flag forces new hash even without filters ──
    if opts.get("reencode", True):
        # Already re-encoding via libx264; add a random private metadata
        # field (won't appear in normal viewers) to make the hash unique
        tag_val = uuid.uuid4().hex[:16]
        cmd += ["-metadata", f"comment={tag_val}"]

    cmd += ["-movflags", "+faststart", output_path]
    return cmd


def _run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        # Surface the last 20 lines of stderr for debugging
        stderr_tail = "\n".join(result.stderr.splitlines()[-20:])
        raise RuntimeError(stderr_tail)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass
