"""Brand kit — per-project handle/hashtags/end-card/palette/font.

A brand kit creates two automatic overlays:
  - a persistent watermark (handle) on the watermark track
  - an end-card text overlay in the last 3 seconds with handle + hashtags
"""
from __future__ import annotations
import json
from pathlib import Path
from ..edl import EDL
from ..edl.schema import BrandKit, TextClip, Track, Transform


WATERMARK_TRACK_ID = "tx_watermark"
ENDCARD_TRACK_ID = "tx_endcard"


def ensure_track(edl: EDL, track_id: str, ttype: str = "text", z: int = 9) -> Track:
    t = edl.get_track(track_id)
    if t:
        return t
    t = Track(id=track_id, type=ttype, z=z, label=track_id.replace("tx_", "").title())
    edl.tracks.append(t)
    return t


def apply_brand_kit(edl: EDL, kit: BrandKit) -> dict:
    """Mutate EDL to attach watermark + end-card from a brand kit."""
    edl.brand_kit = kit
    canvas = edl.canvas
    edl.recompute_duration()
    duration = max(edl.duration, 1.0)

    # Strip prior auto overlays so the kit is idempotent.
    for tid in (WATERMARK_TRACK_ID, ENDCARD_TRACK_ID):
        t = edl.get_track(tid)
        if t:
            t.clips = []

    summary_parts = []
    if kit.handle:
        wm = ensure_track(edl, WATERMARK_TRACK_ID, "text", z=14)
        wm.clips.append(TextClip(
            id=f"t_wm_{kit.handle.replace('@', '').replace('.', '')[:6]}",
            text=kit.handle,
            start=0.0,
            end=duration,
            role="watermark",
            transform=Transform(x=canvas.w / 2, y=canvas.h - canvas.h * 0.04),
        ))
        summary_parts.append(f"watermark {kit.handle}")

    end_lines: list[str] = []
    if kit.handle:
        end_lines.append(kit.handle)
    if kit.hashtags:
        end_lines.append(" ".join(kit.hashtags))
    if end_lines:
        ec = ensure_track(edl, ENDCARD_TRACK_ID, "text", z=15)
        ec.clips.append(TextClip(
            id="t_endcard",
            text="\n".join(end_lines),
            start=max(0.0, duration - 3.0),
            end=duration,
            role="hook",
            transform=Transform(x=canvas.w / 2, y=canvas.h * 0.5),
        ))
        summary_parts.append("end-card")

    return {"applied": summary_parts}


def kit_path(presets_dir: Path, name: str) -> Path:
    return presets_dir / "brand_kits" / f"{name}.json"


def load_kit(presets_dir: Path, name: str) -> BrandKit | None:
    p = kit_path(presets_dir, name)
    if not p.exists():
        return None
    return BrandKit(**json.loads(p.read_text()))


def save_kit(presets_dir: Path, name: str, kit: BrandKit) -> Path:
    p = kit_path(presets_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kit.model_dump(), indent=2))
    return p
