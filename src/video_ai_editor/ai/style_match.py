"""Extract a 'style fingerprint' from a reference video.

What we capture:
  - cut tempo (cuts per minute) from scene-change detection
  - average / median shot length
  - dominant BPM (audio) via librosa
  - dominant color palette from a sampled keyframe

This is enough for Claude to seed a new EDL that mimics the rhythm. Subjective
features (text style, color grade) are deferred to a later pass.
"""
from __future__ import annotations
import statistics
from pathlib import Path
from PIL import Image
from collections import Counter

from ..ingest.scenes import detect_shots
from ..ingest.probe import probe


def _quantize(c: tuple[int, int, int], step: int = 32) -> tuple[int, int, int]:
    return tuple((v // step) * step for v in c)  # type: ignore[return-value]


def dominant_palette(image_path: Path, top_n: int = 5) -> list[str]:
    img = Image.open(image_path).convert("RGB").resize((128, 128))
    counter: Counter[tuple[int, int, int]] = Counter()
    for px in img.getdata():
        counter[_quantize(px)] += 1
    return ["#%02X%02X%02X" % rgb for rgb, _ in counter.most_common(top_n)]


def estimate_bpm(src: Path) -> float | None:
    try:
        import librosa
    except ImportError:
        return None
    try:
        y, sr = librosa.load(str(src), sr=22050, mono=True, duration=60.0)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        return float(tempo) if tempo else None
    except Exception:
        return None


def style_fingerprint(src: Path, cache_dir: Path) -> dict:
    info = probe(src)
    duration = info.duration
    shots = detect_shots(src, threshold=0.3)
    # Drop synthetic 0-duration shots at the boundaries.
    shot_durs = [s.end - s.start for s in shots if s.end - s.start > 0.05]
    cuts_per_min = (len(shots) - 1) / max(0.001, duration / 60.0) if duration else 0
    median_shot = statistics.median(shot_durs) if shot_durs else None
    avg_shot = statistics.mean(shot_durs) if shot_durs else None
    bpm = estimate_bpm(src)
    # Sample a frame mid-clip for the palette
    cache_dir.mkdir(parents=True, exist_ok=True)
    from .vision import extract_keyframe
    frame = cache_dir / f"style_frame_{abs(hash(str(src))) & 0xFFFFFF:x}.jpg"
    extract_keyframe(src, duration / 2, frame)
    palette: list[str] = []
    if frame.exists():
        try:
            palette = dominant_palette(frame, top_n=5)
        except Exception:
            pass
    return {
        "duration": duration,
        "n_shots": len(shots),
        "cuts_per_min": round(cuts_per_min, 2),
        "median_shot_s": round(median_shot, 3) if median_shot else None,
        "avg_shot_s": round(avg_shot, 3) if avg_shot else None,
        "bpm": round(bpm, 1) if bpm else None,
        "palette": palette,
    }
