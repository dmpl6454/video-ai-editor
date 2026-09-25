"""Missing ("offline") media — QA-095 / QA-096.

A clip whose source file is gone used to be invisible as a problem: the bin
listed it as normal, the cached preview (keyed on the EDL hash alone) kept
playing the old render, one missing file blanked the whole preview once that
cache went, an export failed with "a clip's source file is missing" without
saying which, and Save wrote a project without it and said nothing.

This module is the one answer to "which media is missing, and what is it
called?", used by:

* the media library (`missing` flag on each row → offline badge + Relink),
* the preview path (`render_edl`: every missing source is swapped for a
  generated "Media offline" slate of the same length, so the other clips still
  preview and the cache key changes with the offline state — a stale render
  can never be served for a timeline whose media has gone),
* export (refused, naming the files, rather than silently exporting slates),
* save_project (a `missing` list in the answer and the manifest).

Relinking is `dispatch("relink_media")` — the one mutation path — so it is
undoable and logged like any other edit.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: Lanes whose clips play a media file (stickers are images with their own
#: renderer, which skips a missing one; they are reported but not slated).
_MEDIA_TRACK_TYPES = frozenset({"video", "audio", "music", "vo"})

#: Derived-render filename prefix → what the step did, for display names.
_DERIVED_TAGS = {
    "reframe": "reframed", "denoise": "denoised", "stable": "stabilized",
    "upscaled": "upscaled", "erased": "object removed", "bgr": "background removed",
    "smooth": "slow motion", "instrumental": "instrumental", "vocals": "vocals",
    "slate": "offline",
}


def _exists(src: str) -> bool:
    try:
        return Path(src).is_file()
    except (OSError, ValueError):
        return False


def derived_tag(path: str | Path) -> str | None:
    """`cache/reframe_ab12.mp4` → "reframed"; None for anything an AI step did
    not render (an upload named `stable_shot.mp4` is not "stabilized")."""
    p = Path(path)
    if "cache" not in p.parts or "_" not in p.stem:
        return None
    return _DERIVED_TAGS.get(p.stem.split("_", 1)[0].lower())


def display_name_for(session_dir: Path | None, src: str) -> str:
    """What the user calls `src`, without needing the file itself: the
    import's recorded display name (ingest.json beside it, or the library's
    name for a loose upload), following a derived render back to the upload
    it came from; the disk name with its uniquifying suffix removed last."""
    from .media_library import _display_from_disk, _load_state
    from .agent.media_origin import origin_of
    p = Path(src)
    origin = Path(origin_of(src))
    tag = derived_tag(p)
    base = origin if origin != p else p
    name: str | None = None
    ij = base.parent / "ingest.json"
    if ij.is_file():
        try:
            data = json.loads(ij.read_text(encoding="utf-8"))
            if Path(str(data.get("normalized", ""))).name == base.name or \
                    Path(str(data.get("src", ""))).name == base.name:
                name = data.get("display_name") or None
        except (OSError, ValueError):
            pass
    if name is None and session_dir is not None:
        try:
            names = _load_state(Path(session_dir)).get("names", {})
            name = names.get(str(base.resolve())) or names.get(str(base))
        except OSError:
            name = None
    if name is None:
        stem = base.name
        if stem.endswith(".normalized.mp4"):
            stem = stem[: -len(".normalized.mp4")] + ".mp4"
        name = _display_from_disk(stem)
    if tag and (origin != p):
        return f"{name} ({tag})"
    if tag and origin == p:
        return f"{tag.capitalize()} clip"
    return name


def missing_media(session_dir: Path | None, edl) -> list[dict[str, Any]]:
    """Every source the timeline references that is not on disk, once each,
    in timeline order: `{src, name, clip_ids, track_ids, kind}`."""
    out: dict[str, dict[str, Any]] = {}
    for track in getattr(edl, "tracks", []) or []:
        for clip in track.clips:
            src = getattr(clip, "src", None)
            if not isinstance(src, str) or not src or _exists(src):
                continue
            row = out.get(src)
            if row is None:
                kind = ("sticker" if track.type == "sticker" else
                        "audio" if track.type in ("music", "vo", "audio") else "video")
                row = out[src] = {"src": src, "name": display_name_for(session_dir, src),
                                  "clip_ids": [], "track_ids": [], "kind": kind}
            row["clip_ids"].append(clip.id)
            if track.id not in row["track_ids"]:
                row["track_ids"].append(track.id)
    return list(out.values())


def missing_message(rows: list[dict[str, Any]], verb: str = "export") -> str:
    names = [r["name"] for r in rows]
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    noun = "file is" if len(names) == 1 else "files are"
    return (f"Can't {verb} — {len(names)} media {noun} missing: {shown}. "
            f"Relink {'it' if len(names) == 1 else 'them'} from the Media panel, "
            f"or remove the clips that use {'it' if len(names) == 1 else 'them'}.")


def saved_missing_message(rows: list[dict[str, Any]]) -> str:
    names = [r["name"] for r in rows]
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    one = len(names) == 1
    return (f"Saved, but {len(names)} media file{'' if one else 's'} "
            f"{'was' if one else 'were'} missing and {'is' if one else 'are'} not in the "
            f"project file: {shown}. Relink {'it' if one else 'them'} and save again to "
            f"make the project self-contained.")


# ---------------------------------------------------------------- slates


def _slate_png(dst: Path, w: int, h: int, name: str) -> None:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (w, h), (38, 20, 26))
    d = ImageDraw.Draw(img)
    # Diagonal hatching reads as "not real footage" at any size.
    step = max(12, w // 24)
    for x in range(-h, w, step):
        d.line([(x, h), (x + h, 0)], fill=(58, 30, 38), width=max(2, step // 4))
    try:
        big = ImageFont.load_default(size=max(14, h // 9))
        small = ImageFont.load_default(size=max(10, h // 18))
    except TypeError:                       # Pillow < 10.1: no sized default
        big = small = ImageFont.load_default()
    for text, font, y in (("MEDIA OFFLINE", big, h * 0.40), (name[:60], small, h * 0.58)):
        box = d.textbbox((0, 0), text, font=font)
        d.text(((w - (box[2] - box[0])) / 2, y), text, fill=(236, 220, 224), font=font)
    img.save(dst)


def slate_for(session_dir: Path, src: str, seconds: float, canvas, name: str) -> Path:
    """A cached "Media offline" clip at least `seconds` long, shaped like the
    canvas (short side 360), with silent audio — valid on any lane."""
    from .edl import timebase as _tb
    from .ingest.still import normalize_still
    length = float(max(1, int(seconds + 1.999)))
    short = 360
    w, h = int(canvas.w), int(canvas.h)
    if w >= h:
        sw, sh = max(2, round(w * short / h / 2) * 2), short
    else:
        sw, sh = short, max(2, round(h * short / w / 2) * 2)
    fps_arg = _tb.ffmpeg_rate(canvas.fps)
    key = hashlib.sha1(f"{src}|{length}|{sw}x{sh}|{fps_arg}|{name}".encode("utf-8")).hexdigest()[:16]
    cache = Path(session_dir) / "cache" / "offline"
    dst = cache / f"slate_{key}.mp4"
    if dst.is_file() and dst.stat().st_size > 0:
        return dst
    cache.mkdir(parents=True, exist_ok=True)
    png = cache / f"slate_{key}.png"
    _slate_png(png, sw, sh, name)
    try:
        normalize_still(png, dst, fps_arg=fps_arg, short_side=None, source_seconds=length)
    finally:
        png.unlink(missing_ok=True)
    return dst


def render_edl(edl, session_dir: Path):
    """The EDL a PREVIEW should render: `edl` itself when every source is on
    disk, else a deep copy with each missing media clip pointed at a slate.
    The copy's hash differs from the real one, so a preview rendered while
    the media was present is never served for the offline timeline."""
    rows = [r for r in missing_media(session_dir, edl) if r["kind"] != "sticker"]
    if not rows:
        return edl
    view = edl.model_copy(deep=True)
    by_src = {r["src"]: r for r in rows}
    for track in view.tracks:
        if track.type not in _MEDIA_TRACK_TYPES:
            continue
        for clip in track.clips:
            row = by_src.get(getattr(clip, "src", None))
            if row is None:
                continue
            need = max(float(clip.out), float(getattr(clip, "in_", 0.0)) + 0.1)
            clip.src = str(slate_for(session_dir, row["src"], need, view.canvas, row["name"]))
    return view


__all__ = ["derived_tag", "display_name_for", "missing_media", "missing_message",
           "render_edl", "saved_missing_message", "slate_for"]
