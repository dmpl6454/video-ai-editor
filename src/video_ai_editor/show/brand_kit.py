"""Brand kit — per-project handle/hashtags/end-card/palette/font.

A brand kit creates automatic overlays:
  - a persistent watermark (handle) on the watermark track
  - an end-card text overlay in the last 3 seconds with handle + hashtags
  - when `end_card` (an image path) is set, a full-canvas image sticker
    behind that end-card text for the same window

`palette` and `font` are MATERIALISED into the created clips' TextStyle at
apply time (end-card text gets palette[0] as its color; watermark + end-card
get the brand font) — the render layer then honors clip styles with no
brand-kit knowledge of its own. These three fields used to be accepted and
persisted but read by nothing.
"""
from __future__ import annotations
import json
from pathlib import Path
from ..edl import EDL
from ..edl.schema import BrandKit, Sticker, TextClip, TextStyle, Track, Transform


WATERMARK_TRACK_ID = "tx_watermark"
ENDCARD_TRACK_ID = "tx_endcard"
ENDCARD_STICKER_ID = "st_brand_endcard"

# cache_sticker_pngs sizes a sticker to 22% of the canvas long edge × scale,
# so 1/0.22 makes the image span the full long edge (aspect preserved).
_FULL_CANVAS_STICKER_SCALE = 1 / 0.22


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
    st = edl.get_track("stickers")
    if st:
        st.clips = [c for c in st.clips if c.id != ENDCARD_STICKER_ID]

    # Brand style, materialised into the clips this kit creates. TextStyle's
    # defaults act as "use the role style" sentinels in the renderer, so only
    # explicitly-set brand values are carried.
    def _brand_style(color: str | None = None) -> TextStyle:
        kwargs: dict = {}
        if kit.font:
            kwargs["font"] = kit.font
        if color:
            kwargs["color"] = color
        return TextStyle(**kwargs)

    summary_parts = []
    if kit.handle:
        wm = ensure_track(edl, WATERMARK_TRACK_ID, "text", z=14)
        wm.clips.append(TextClip(
            id=f"t_wm_{kit.handle.replace('@', '').replace('.', '')[:6]}",
            text=kit.handle,
            start=0.0,
            end=duration,
            role="watermark",
            style=_brand_style(),
            transform=Transform(x=canvas.w / 2, y=canvas.h - canvas.h * 0.04),
        ))
        summary_parts.append(f"watermark {kit.handle}")

    endcard_start = max(0.0, duration - 3.0)

    # End-card image: a full-canvas sticker UNDER the end-card text (the
    # stickers track's z sits below the end-card text track's z, and the
    # overlay compositor sorts by z).
    if kit.end_card:
        stickers = ensure_track(edl, "stickers", "sticker", z=12)
        stickers.clips.append(Sticker(
            id=ENDCARD_STICKER_ID,
            src=kit.end_card,
            start=endcard_start,
            end=duration,
            transform=Transform(x=canvas.w / 2, y=canvas.h / 2,
                                scale=_FULL_CANVAS_STICKER_SCALE),
        ))
        summary_parts.append("end-card image")

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
            start=endcard_start,
            end=duration,
            role="hook",
            style=_brand_style(color=kit.palette[0] if kit.palette else None),
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
    return BrandKit(**json.loads(p.read_text(encoding="utf-8")))


def save_kit(presets_dir: Path, name: str, kit: BrandKit) -> Path:
    p = kit_path(presets_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kit.model_dump(), indent=2), encoding="utf-8")
    return p
