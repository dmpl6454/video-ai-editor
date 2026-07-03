"""Ingest orchestrator: probe → normalize → transcribe → scenes (lazy options)."""
from __future__ import annotations
import json
from pathlib import Path
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


def ingest_upload(
    src: Path,
    out_dir: Path,
    *,
    fps: int = 30,
    proxy_height: int | None = None,
    transcribe_audio: bool = True,
    clamp_height: int = 1080,
) -> IngestResult:
    """Run the full ingest pipeline on a freshly uploaded video.

    - normalizes to CFR H.264 + AAC at `fps`
    - clamps very large sources (e.g. 4K) to `clamp_height` so editing/preview
      stays snappy. Set clamp_height=None to preserve source resolution.
    - if a sidecar `.srt`/`.vtt`/`.ass` is present, uses it instead of whisper
    - else runs faster-whisper if `transcribe_audio` is True
    Heavy steps (scene detect, vision, beats) are deferred to first-use to
    keep upload latency low.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = out_dir / f"{src.stem}.normalized.mp4"

    # Decide target height: respect explicit proxy_height; else clamp tall sources.
    target_h = proxy_height
    if target_h is None and clamp_height is not None:
        from .probe import probe as _probe
        info = _probe(src)
        if info.video and info.video.height and info.video.height > clamp_height:
            target_h = clamp_height
    p = normalize(src, normalized, fps=fps, height=target_h)

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
    )
    (out_dir / "ingest.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
