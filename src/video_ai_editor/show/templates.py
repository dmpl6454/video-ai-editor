"""Built-in templates + recurring 'show' template save/apply.

Templates (`outfit_breakdown`, `tech_tip`, `explainer`) are composable functions
that mutate an existing EDL — they assume the user has already added a clip on
V1 and they layer on hooks, supers, captions, brand kit defaults appropriate
for that style.

Show templates are user-saved snapshots of brand kit + canvas + caption style
+ music gain so a recurring weekly segment (Style Spotlight, Tech Tip on
quicksolutions.in, etc.) can be re-applied to next week's raw footage in one
call.
"""
from __future__ import annotations
import json
from pathlib import Path
from ..config import PRESETS_DIR
from ..edl import EDL
from ..edl.schema import BrandKit, Canvas, TextClip, Transform, Track
from .brand_kit import apply_brand_kit, ensure_track


# ---------- Built-in templates (composable EDL setups) ----------

def template_outfit_breakdown(edl: EDL, *, guest: str | None = None,
                              hook: str | None = None) -> dict:
    """Numbered outfit-detail labels keyed to wardrobe items + a hook."""
    edl.recompute_duration()
    duration = max(edl.duration, 1.0)
    edl.canvas = Canvas(w=1080, h=1920, fps=30)

    hook_text = hook or (f"WHY EVERYONE'S COPYING {guest.upper()}'S LOOK"
                         if guest else "THIS OUTFIT HITS DIFFERENT")
    super_track = ensure_track(edl, "tx_hook", "text", z=10)
    super_track.clips.append(TextClip(
        text=hook_text, start=0.0, end=min(3.0, duration),
        role="hook",
        transform=Transform(x=edl.canvas.w / 2, y=edl.canvas.h / 2),
    ))

    # Sprinkle 3 detail labels across the timeline
    labels = ["THE LOOK", "TOP DETAILS", "ACCESSORIES"]
    label_track = ensure_track(edl, "tx_super", "text", z=11)
    if duration >= 6.0:
        for i, label in enumerate(labels):
            t = 3.0 + (duration - 4.0) * (i + 0.5) / len(labels)
            label_track.clips.append(TextClip(
                text=label, start=t, end=min(t + 2.0, duration - 0.5),
                role="super",
                transform=Transform(x=edl.canvas.w / 2, y=edl.canvas.h * 0.78),
            ))
    return {"applied": ["outfit_breakdown", f"hook='{hook_text}'", f"{len(labels)} item labels"]}


def template_tech_tip(edl: EDL, *, hook: str | None = None) -> dict:
    """Bold opening hook + ig_chunky captions + brand-kit-friendly setup."""
    edl.recompute_duration()
    duration = max(edl.duration, 1.0)
    edl.canvas = Canvas(w=1080, h=1920, fps=30)

    hook_text = hook or "DO THIS NOW"
    hook_track = ensure_track(edl, "tx_hook", "text", z=10)
    hook_track.clips.append(TextClip(
        text=hook_text, start=0.0, end=min(2.5, duration),
        role="hook",
        transform=Transform(x=edl.canvas.w / 2, y=edl.canvas.h * 0.40),
    ))

    # Captions track gets enabled (real lines fill in when add_caption_track runs)
    cap = ensure_track(edl, "captions", "captions", z=13)
    if cap.config is None:
        from ..edl.schema import CaptionsConfig
        cap.config = CaptionsConfig()
    cap.config.enabled = True
    cap.config.style = "ig_chunky"
    cap.config.position = "bottom"
    return {"applied": ["tech_tip", f"hook='{hook_text}'", "ig_chunky captions enabled"]}


def template_explainer(edl: EDL, *, hook: str | None = None) -> dict:
    """Narrator + word-emphasis captions + clickbait hook."""
    edl.recompute_duration()
    duration = max(edl.duration, 1.0)
    edl.canvas = Canvas(w=1080, h=1920, fps=30)

    hook_text = hook or "YOU WON'T BELIEVE THIS"
    hook_track = ensure_track(edl, "tx_hook", "text", z=10)
    hook_track.clips.append(TextClip(
        text=hook_text, start=0.0, end=min(3.0, duration),
        role="hook",
        transform=Transform(x=edl.canvas.w / 2, y=edl.canvas.h * 0.45),
    ))
    cap = ensure_track(edl, "captions", "captions", z=13)
    if cap.config is None:
        from ..edl.schema import CaptionsConfig
        cap.config = CaptionsConfig()
    cap.config.enabled = True
    cap.config.style = "word_emphasis"
    cap.config.position = "center"
    return {"applied": ["explainer", f"hook='{hook_text}'", "word_emphasis captions enabled"]}


TEMPLATES = {
    "outfit_breakdown": template_outfit_breakdown,
    "tech_tip": template_tech_tip,
    "explainer": template_explainer,
}


def apply_template(edl: EDL, name: str, *, inputs: dict | None = None) -> dict:
    fn = TEMPLATES.get(name)
    if fn is None:
        raise ValueError(f"unknown template {name!r}; known: {sorted(TEMPLATES)}")
    return fn(edl, **(inputs or {}))


def list_template_names() -> list[str]:
    return sorted(TEMPLATES.keys())


# ---------- Show templates (per-user recurring shows) ----------

def shows_dir() -> Path:
    p = PRESETS_DIR / "shows"
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_shows() -> list[str]:
    return sorted(p.stem for p in shows_dir().glob("*.json"))


class ShowSnapshot:
    """A bundle of reusable, content-agnostic project settings."""

    @staticmethod
    def from_edl(edl: EDL) -> dict:
        # We snapshot only the parts that are reusable across episodes — never
        # any specific media clips or guest-specific text.
        cap = edl.get_track("captions")
        cap_cfg = cap.config.model_dump() if cap and cap.config else None
        music_track = edl.get_track("music")
        # We capture music *settings* (gain, duck) and the source path; the
        # source can be re-used if the user wants the same bg track each week.
        music_seed = None
        if music_track and music_track.clips:
            from ..edl.schema import Clip
            mc = music_track.clips[0]
            if isinstance(mc, Clip):
                music_seed = {
                    "src": mc.src, "volume_db": mc.audio.gain_db,
                    "fade_in": mc.audio.fade_in, "fade_out": mc.audio.fade_out,
                    "duck": bool(music_track.duck),
                }
        return {
            "canvas": edl.canvas.model_dump(),
            "brand_kit": edl.brand_kit.model_dump() if edl.brand_kit else None,
            "captions": cap_cfg,
            "music_seed": music_seed,
        }

    @staticmethod
    def apply_to_edl(edl: EDL, snap: dict) -> list[str]:
        applied: list[str] = []
        if snap.get("canvas"):
            edl.canvas = Canvas(**snap["canvas"])
            applied.append(f"canvas → {edl.canvas.w}×{edl.canvas.h}")
        if snap.get("brand_kit"):
            kit = BrandKit(**snap["brand_kit"])
            apply_brand_kit(edl, kit)
            applied.append(f"brand kit ({kit.handle or 'unnamed'})")
        if snap.get("captions"):
            cap = ensure_track(edl, "captions", "captions", z=13)
            from ..edl.schema import CaptionsConfig
            cap.config = CaptionsConfig(**snap["captions"])
            applied.append(f"captions ({cap.config.style})")
        if snap.get("music_seed"):
            ms = snap["music_seed"]
            from ..edl.schema import Track, Clip, AudioProps, MusicDuck
            mt = ensure_track(edl, "music", "music", z=0)
            mt.clips = []
            if ms.get("duck"):
                mt.duck = MusicDuck(to_db=-18.0, track_ref="a1")
            edl.recompute_duration()
            dur = max(edl.duration, 1.0)
            mt.clips.append(Clip(
                src=ms["src"], in_=0.0, out=dur, start=0.0,
                audio=AudioProps(
                    gain_db=ms.get("volume_db", -12),
                    fade_in=ms.get("fade_in", 0.5),
                    fade_out=ms.get("fade_out", 1.0),
                ),
            ))
            applied.append("music seed")
        return applied


def save_show(name: str, edl: EDL) -> Path:
    snap = ShowSnapshot.from_edl(edl)
    p = shows_dir() / f"{name}.json"
    p.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    return p


def load_show(name: str) -> dict:
    p = shows_dir() / f"{name}.json"
    if not p.exists():
        raise ValueError(f"show template {name!r} not found in {shows_dir()}")
    return json.loads(p.read_text(encoding="utf-8"))
