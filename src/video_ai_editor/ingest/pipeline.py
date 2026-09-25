"""Ingest orchestrator: probe → normalize → transcribe → scenes (lazy options)."""
from __future__ import annotations
import json
import threading
from pathlib import Path
from typing import Callable
from pydantic import BaseModel
from .probe import probe, ProbeResult
from .normalize import normalize
from .srt_io import detect_sidecar, import_srt
from .transcribe import Transcript


class IngestResult(BaseModel):
    src: str           # original upload path
    normalized: str    # CFR-normalized path
    probe: ProbeResult
    transcript: Transcript | None = None
    sidecar_used: str | None = None
    # The name the user's file had before sanitising (QA-001). The on-disk name
    # is ASCII-only and uniquified; this is what a human would call the clip.
    display_name: str | None = None
    # ffmpeg rate string the file was normalised to ("25", "30000/1001").
    fps: str | None = None
    # "sdr" | "tonemapped" | "approximate" (HDR retagged without a tone map).
    color: str | None = None
    notices: list[str] = []
    # QA-090: a photo, normalised into a `STILL_SOURCE_SECONDS` source and
    # placed at `STILL_DEFAULT_SECONDS` (see ingest/still.py).
    still: bool = False
    # QA-089: the source's displayed (w, h) when normalisation CLAMPED it (a
    # 4K file becomes a 1080p editing proxy). Export renders a full-quality
    # master from `src` on demand instead of upscaling the proxy — see
    # ingest/masters.py.
    clamped_from: list[int] | None = None


def ingest_upload(
    src: Path,
    out_dir: Path,
    *,
    fps: float | int | None = None,
    proxy_height: int | None = None,
    transcribe_audio: bool = True,
    clamp_height: int | None = 1080,
    display_name: str | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: "threading.Event | None" = None,
    still_fps: float | None = None,
) -> IngestResult:
    """Run the full ingest pipeline on a freshly uploaded video.

    - normalizes to CFR H.264 + AAC at `fps`, which defaults to the SOURCE's
      own standard rate (QA-009; see `edl.timebase.source_rate`)
    - clamps sources whose SHORT side exceeds `clamp_height` (4K) so
      editing/preview stays snappy. The short side, not the height: testing the
      height shrank every 1080x1920 portrait clip to 608x1080 (QA-008). Set
      clamp_height=None to preserve source resolution.
    - if a sidecar `.srt`/`.vtt`/`.ass` is present, uses it instead of whisper
    - else runs faster-whisper if `transcribe_audio` is True
    Heavy steps (scene detect, vision, beats) are deferred to first-use to
    keep upload latency low.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = out_dir / f"{src.stem}.normalized.mp4"

    from .still import is_still_image, normalize_still
    if is_still_image(src):
        # QA-090: a photo has no frame rate of its own; it takes the
        # project's (`still_fps`), so its frames sit on the edit grid.
        from ..edl import timebase as _tb
        fps_arg = _tb.ffmpeg_rate(float(still_fps or fps or 30))
        p = normalize_still(src, normalized, fps_arg=fps_arg,
                            short_side=proxy_height or clamp_height,
                            on_progress=on_progress, cancel_event=cancel_event)
        result = IngestResult(src=str(src), normalized=str(normalized), probe=p,
                              display_name=display_name, fps=fps_arg, color="sdr",
                              still=True)
        (out_dir / "ingest.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    # Decide target short side: respect explicit proxy_height; else clamp
    # sources whose short side is above clamp_height.
    target = proxy_height
    clamped_from: list[int] | None = None
    if target is None and clamp_height is not None:
        from .probe import probe as _probe
        info = _probe(src)
        v = info.video
        if v and v.width and v.height and min(v.width, v.height) > clamp_height:
            target = clamp_height
            clamped_from = [int(v.width), int(v.height)]
    report: dict = {}
    p = normalize(src, normalized, fps=fps, height=target,
                  on_progress=on_progress, cancel_event=cancel_event, report=report)

    notices: list[str] = []
    if report.get("color") == "approximate":
        notices.append(
            "This clip is HDR and could only be converted approximately — "
            "colours may look flat or washed out.")

    transcript: Transcript | None = None
    sidecar_used: str | None = None
    sidecar = detect_sidecar(src)
    if sidecar:
        transcript = import_srt(sidecar)
        sidecar_used = str(sidecar)
    elif transcribe_audio:
        from .transcribe import transcribe
        transcript = transcribe(normalized)

    result = IngestResult(
        src=str(src),
        normalized=str(normalized),
        probe=p,
        transcript=transcript,
        sidecar_used=sidecar_used,
        display_name=display_name,
        fps=report.get("fps"),
        color=report.get("color"),
        notices=notices,
        clamped_from=clamped_from,
    )
    (out_dir / "ingest.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
