"""Tool dispatch — single mutation path for both Claude and the UI.

Every UI gesture and every Claude tool call lands here. Mutations call
store.commit() so we get free undo, ops log, and snapshot persistence.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Callable
from ..edl import EDLStore
from ..edl.schema import EDL, Clip, Track, Canvas, TextClip, BrandKit, CaptionsConfig, Transform
from ..render import render_preview as _render_preview
from ..show.brand_kit import apply_brand_kit as _apply_brand_kit, ensure_track
from ..show.audit import audit as _audit
from ..show.templates import (
    apply_template as _apply_template,
    list_template_names,
    save_show as _save_show,
    load_show as _load_show,
    list_shows as _list_shows,
    ShowSnapshot,
)
from .tools import list_tools as _list_tools
from .. import platformutil as _pu

DispatchFn = Callable[[EDLStore, dict], dict]


# ---------- helpers ----------

def _default_broll_dir() -> Path:
    """Default library folder for b-roll footage: ~/Videos/broll on Windows,
    ~/Movies/broll on macOS/Linux (Movies is the mac convention)."""
    root = "Videos" if _pu.IS_WINDOWS else "Movies"
    return Path.home() / root / "broll"


def _v_track(edl: EDL, track_id: str) -> Track:
    t = edl.get_track(track_id)
    if not t:
        raise ValueError(f"track '{track_id}' not found")
    return t


# Track types that can hold a media Clip (an uploaded video/audio file).
# Sticker/text/captions/effect tracks are for their own dedicated clip kinds
# only — the renderer's collect_text_clips/collect_stickers/build_pip_overlay
# etc. each only look at their own track type, so a media Clip parked on e.g.
# the captions track is not an error, just silently invisible in every render
# (issues 41/42/43, "anything can be placed anywhere").
_MEDIA_TRACK_TYPES = frozenset({"video", "audio", "music", "vo"})


def _v_track_for_media(edl: EDL, track_id: str) -> Track:
    """Like _v_track, but additionally rejects a track whose type can't hold
    a media Clip at all. Used only where a media Clip is actually being
    PLACED on the named track (add_clip's `track`, move_clip's `new_track`) —
    cut_range/split_at/remove_silences etc. resolve a track generically for
    time-based operations on whatever clips it already holds, and must not
    gain this restriction."""
    t = _v_track(edl, track_id)
    if t.type not in _MEDIA_TRACK_TYPES:
        raise ValueError(
            f"track '{track_id}' is a '{t.type}' lane and can't hold a media clip "
            f"(expected one of {sorted(_MEDIA_TRACK_TYPES)})"
        )
    return t


def _first_free_gap(
    track: Track, duration: float, preferred_start: float, ignore_clip_id: str | None = None
) -> float:
    """Find the first start time >= preferred_start on `track` where a clip of
    length `duration` fits without overlapping any existing media Clip
    (excluding `ignore_clip_id`, the clip actually being moved/placed).

    Used by move_clip's cross-track-drop guard: dropping a clip onto an
    occupied range used to silently stack it on top of whatever was already
    there (both clips survive in the EDL, so no data loss, but the canvas
    draws them with identical fill and no distinction — reading as "merged").
    Snapping to the nearest free gap keeps the drop useful instead of just
    rejecting it outright.

    Only considers `Clip` (media) siblings — text/sticker/caption tracks have
    their own independent overlap semantics (see add_super_text's role-based
    replace) and are never routed through this helper.
    """
    occupied = sorted(
        (c.start, c.start + c.duration)
        for c in track.clips
        if isinstance(c, Clip) and c.id != ignore_clip_id
    )
    candidate = max(0.0, preferred_start)

    def overlaps(start: float) -> bool:
        end = start + duration
        return any(start < o_end - 1e-9 and end > o_start + 1e-9 for o_start, o_end in occupied)

    # If the preferred slot is already free, use it as-is (no snapping needed
    # for the common non-overlapping case).
    if not overlaps(candidate):
        return candidate

    # Otherwise walk forward: for each existing clip whose range the
    # candidate would collide with, jump to that clip's end and re-check —
    # this converges because `occupied` is sorted and finite.
    for o_start, o_end in occupied:
        if candidate < o_end and candidate + duration > o_start:
            candidate = o_end
    # One more pass in case jumping past one clip landed inside another
    # (dense packing) — bounded by len(occupied) since each pass only moves
    # candidate forward to the next clip's end.
    for _ in range(len(occupied)):
        if not overlaps(candidate):
            break
        for o_start, o_end in occupied:
            if candidate < o_end and candidate + duration > o_start:
                candidate = o_end
    return candidate


def _ripple_close_gap(track: Track) -> None:
    """After a removal, slide subsequent clips left to close any gap."""
    track.clips.sort(key=lambda c: getattr(c, "start", 0))
    cursor = 0.0
    for c in track.clips:
        if not isinstance(c, Clip):
            continue
        c.start = cursor
        cursor = c.start + c.duration


_OVERLAY_TRACK_TYPES = ("text", "sticker", "captions")


def _ripple_overlays(edl: EDL, removed_start: float, removed_end: float) -> None:
    """Shift text/sticker/caption overlays to follow a ripple on the video
    track — the same left-shift `_ripple_close_gap` just applied to V1.

    Without this, cut_range/ripple_delete/trim_clip only re-time the video
    track they operate on: a Sticker/TextClip is pinned to absolute timeline
    seconds (schema.py), so shortening the footage (e.g. remove_silences
    looping cut_range) leaves every overlay at its OLD absolute time — which
    is now past the shortened content, or over unrelated footage. Reported
    as "emoji doesn't show up where I put it" / "emojis popped up at the end
    that were never added there" (issues 31/32/50).

    `removed_start`/`removed_end` is the [start, end) interval that was cut
    out of the video track's ORIGINAL timeline (before the ripple). Any
    overlay clip is remapped the same way ripple-delete remaps time:
      t <= removed_start        -> unchanged (before the cut, untouched)
      removed_start < t < removed_end -> collapsed to removed_start (the
                                          content it was pinned to no longer
                                          exists; snap to the cut point so it
                                          doesn't silently vanish)
      t >= removed_end          -> shifted left by (removed_end - removed_start)
    Applied to both `start` and `end` independently so a clip straddling the
    cut boundary shrinks rather than teleporting.
    """
    shift = removed_end - removed_start
    if shift <= 0:
        return

    def remap(t: float) -> float:
        if t <= removed_start:
            return t
        if t < removed_end:
            return removed_start
        return t - shift

    for track in edl.tracks:
        if track.type not in _OVERLAY_TRACK_TYPES:
            continue
        for c in track.clips:
            if not hasattr(c, "start") or not hasattr(c, "end"):
                continue
            new_start = remap(c.start)
            new_end = remap(c.end)
            if new_end <= new_start:
                new_end = new_start + 0.1  # keep a minimum visible span
            c.start = new_start
            c.end = new_end


def _rescale_transform_xy(transform, sx: float, sy: float) -> None:
    """Rescale a Transform's x/y in place by (sx, sy) — handles both a plain
    scalar and a keyframed value (list of [time, value] pairs)."""
    from ..edl.schema import Keyframe

    def _scale_one(value, factor: float):
        if isinstance(value, Keyframe):
            value.keyframes = [(t, v * factor) for t, v in value.keyframes]
            return value
        return value * factor

    transform.x = _scale_one(transform.x, sx)
    transform.y = _scale_one(transform.y, sy)


def _rescale_overlays_for_canvas_change(edl: EDL, old_w: int, old_h: int, new_w: int, new_h: int) -> None:
    """Proportionally rescale every clip/sticker/text transform's x/y when the
    canvas dimensions change.

    set_aspect_ratio/set_canvas used to only touch canvas.w/h — every
    Sticker/TextClip/Clip transform is positioned in ABSOLUTE canvas pixels
    (text_overlay.py reads tx.x/tx.y directly as pixel coordinates), so
    switching e.g. 9:16 (1080x1920) to 16:9 (1920x1080) left a sticker placed
    at y=1600 (fine in a 1920-tall canvas) sitting far below a now-1080-tall
    canvas — invisible, reported as "emojis vanished after changing aspect
    ratio" (issue 37). Rescaling proportionally keeps each overlay at the
    same RELATIVE position (e.g. "80% down the frame") across the change.
    """
    if old_w <= 0 or old_h <= 0 or (old_w == new_w and old_h == new_h):
        return
    sx, sy = new_w / old_w, new_h / old_h
    for track in edl.tracks:
        for c in track.clips:
            tx = getattr(c, "transform", None)
            if tx is not None:
                _rescale_transform_xy(tx, sx, sy)


def _current_v1_ingest_json(store: EDLStore) -> Path | None:
    """Return the ingest.json for the CURRENT v1 source clip, or None.

    ingest_upload() always writes `ingest.json` as a sibling of the clip's
    normalized media file (uploads/<stem>/{name.normalized.mp4, ingest.json}),
    so the correct file is derived directly from the active clip's `src` —
    never "the first ingest.json glob happens to find," which silently reads
    a stale transcript from an earlier/different upload in the same session
    (each upload gets its own uploads/<stem>/ subdirectory).
    """
    v1 = store.edl.get_track("v1")
    src_clip = next((c for c in (v1.clips if v1 else []) if isinstance(c, Clip)), None)
    if not src_clip:
        return None
    candidate = Path(src_clip.src).parent / "ingest.json"
    return candidate if candidate.exists() else None


# ---------- inspection ----------

def get_timeline(store: EDLStore, args: dict) -> dict:
    summary = args.get("summary", True)
    edl = store.edl
    if summary:
        # Include enough per-clip info that Claude can refer to specific clips
        # by id ("c_abcd1234") and source name without dumping the full EDL.
        track_summaries = []
        for t in edl.tracks:
            tinfo = {"id": t.id, "type": t.type, "label": t.label, "clips": []}
            for c in t.clips:
                if isinstance(c, Clip):
                    tinfo["clips"].append({
                        "id": c.id,
                        "src_name": str(c.src).split("/")[-1],
                        "in": c.in_,
                        "out": c.out,
                        "start": c.start,
                        "duration": c.duration,
                    })
                elif hasattr(c, "text"):  # text clip
                    tinfo["clips"].append({
                        "id": c.id,
                        "text": (getattr(c, "text", "") or "")[:60],
                        "start": getattr(c, "start", 0),
                        "end": getattr(c, "end", 0),
                        "role": getattr(c, "role", None),
                    })
                else:  # sticker
                    tinfo["clips"].append({
                        "id": c.id,
                        "label": getattr(c, "label", None),
                        "src": str(getattr(c, "src", "")).split("/")[-1],
                        "start": getattr(c, "start", 0),
                        "end": getattr(c, "end", 0),
                    })
            tinfo["clip_count"] = len(tinfo["clips"])
            track_summaries.append(tinfo)
        return {
            "duration": edl.duration,
            "canvas": edl.canvas.model_dump(),
            "tracks": track_summaries,
            "brand_kit": edl.brand_kit.model_dump() if edl.brand_kit else None,
            "edl_hash": edl.hash(),
            "ops": len(store.ops.ops),
        }
    return {"edl": edl.model_dump(by_alias=True), "edl_hash": edl.hash()}


def get_clip(store: EDLStore, args: dict) -> dict:
    cid = args["clip_id"]
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    track, c = res
    return {"track": track.id, "clip": c.model_dump(by_alias=True)}


def get_transcript(store: EDLStore, args: dict) -> dict:
    # Transcript lives in the session dir as ingest.json, one per uploaded
    # source. Resolve the file that belongs to the clip CURRENTLY on v1 —
    # not an arbitrary glob hit — so a stale transcript from a prior upload
    # in this session is never returned for the video actually on the timeline.
    import json
    ingest_json = _current_v1_ingest_json(store)
    if ingest_json is None:
        return {"segments": [], "language": None, "duration": 0.0}
    data = json.loads(ingest_json.read_text(encoding="utf-8"))
    return data.get("transcript") or {"segments": [], "language": None, "duration": 0.0}


# ---------- edits ----------

def _safe_src(p: str | Path) -> str:
    """Run a user-supplied filesystem path through the path-restriction
    allowlist. Returns the resolved absolute path as a string. Raises
    ValueError when restriction is on and the path escapes the allowlist;
    no-op (just resolves) when restriction is off."""
    from ..config import assert_path_allowed
    return str(assert_path_allowed(p))


def add_clip(store: EDLStore, args: dict) -> dict:
    track = _v_track_for_media(store.edl, args["track"])
    clip = Clip(src=_safe_src(args["src"]), in_=float(args["in"]),
                out=float(args["out"]), start=float(args["start"]))
    # Sensible default PiP placement for non-V1 video tracks. The PIP renderer
    # scales by 35% of the canvas long edge × `scale`. We can't know the
    # source aspect without probing, so we center the PIP and use a smaller
    # scale (0.6 → ~21% of canvas long edge) so it stays fully on-canvas
    # regardless of source orientation. User repositions via the Properties
    # panel sliders or by chat ("move the PIP to the top-right").
    if track.type == "video" and track.id != "v1":
        canvas = store.edl.canvas
        clip.transform.x = canvas.w * 0.5
        clip.transform.y = canvas.h * 0.5
        clip.transform.scale = 0.6
    track.clips.append(clip)
    track.clips.sort(key=lambda c: getattr(c, "start", 0))
    summary = f"Add clip {clip.id} to {track.id} ({clip.in_:.2f}–{clip.out:.2f})"
    store.commit("add_clip", args, summary)
    return {"clip_id": clip.id, "summary": summary}


def cut_range(store: EDLStore, args: dict) -> dict:
    track = _v_track(store.edl, args["track"])
    start = float(args["start"])
    end = float(args["end"])
    if end <= start:
        raise ValueError("end must be > start")
    if args.get("dry_run"):
        return {"would_cut": [c.id for c in track.clips
                              if isinstance(c, Clip)
                              and c.start < end and (c.start + c.duration) > start]}

    new_clips: list[Clip] = []
    for c in list(track.clips):
        if not isinstance(c, Clip):
            new_clips.append(c)
            continue
        c_start, c_end = c.start, c.start + c.duration
        if c_end <= start or c_start >= end:
            new_clips.append(c)
            continue
        # Trim or split
        if c_start < start and c_end > end:
            # Split into two: [c_start..start] and [end..c_end]
            left_dur = start - c_start
            right_dur = c_end - end
            left = c.model_copy(update={"out": c.in_ + left_dur, "start": c_start})
            right = c.model_copy(update={
                "id": f"c_{c.id[2:]}b",
                "in_": c.in_ + (end - c_start),
                "out": c.out,
                "start": start,  # will be ripple-adjusted
            })
            # Pydantic alias quirk: ensure model carries fresh `in`
            new_clips.append(left)
            new_clips.append(right)
        elif c_start < start:
            # Trim right side
            new_dur = start - c_start
            c.out = c.in_ + new_dur
            new_clips.append(c)
        elif c_end > end:
            # Trim left side
            shift = end - c_start
            c.in_ = c.in_ + shift
            new_clips.append(c)
        # else: fully inside → drop
    track.clips = new_clips
    _ripple_close_gap(track)
    if track.id == "v1":
        _ripple_overlays(store.edl, start, end)
    summary = f"Cut {start:.2f}–{end:.2f}s on {track.id} (ripple)"
    store.commit("cut_range", args, summary)
    return {"summary": summary, "clips_after": len(track.clips)}


def split_at(store: EDLStore, args: dict) -> dict:
    # `track` defaults to v1 — every other split convention does the same.
    track = _v_track(store.edl, args.get("track", "v1"))
    t = float(args["time"])
    new_clips: list = []
    split_count = 0
    for c in track.clips:
        if not isinstance(c, Clip):
            new_clips.append(c)
            continue
        c_start, c_end = c.start, c.start + c.duration
        if c_start < t < c_end:
            local = t - c_start
            left = c.model_copy(update={"out": c.in_ + local})
            right = c.model_copy(update={
                "id": f"c_{c.id[2:]}b",
                "in_": c.in_ + local,
                "start": t,
            })
            new_clips.append(left)
            new_clips.append(right)
            split_count += 1
        else:
            new_clips.append(c)
    track.clips = new_clips
    summary = f"Split at {t:.2f}s on {track.id} ({split_count} clip(s) split)"
    store.commit("split_at", args, summary)
    return {"summary": summary, "split": split_count}


def trim_clip(store: EDLStore, args: dict) -> dict:
    res = store.edl.get_clip(args["clip_id"])
    if not res:
        raise ValueError("clip not found")
    track, c = res
    if not isinstance(c, Clip):
        raise ValueError("trim_clip only supports media clips")
    old_start, old_in, old_duration = c.start, c.in_, c.duration
    if "in" in args:
        c.in_ = float(args["in"])
    if "out" in args:
        c.out = float(args["out"])
    new_duration = c.duration
    _ripple_close_gap(track)
    if track.id == "v1" and new_duration < old_duration - 1e-9:
        # The clip's OWN start doesn't move here (only _ripple_close_gap
        # repacks positions) — its old timeline footprint was
        # [old_start, old_start + old_duration). Trimming from the front
        # (in_ increased) removes the HEAD of that footprint, since the
        # surviving content still ends at the same `out` and now starts
        # later: removed = [old_start, old_start + delta_in). Trimming from
        # the back (out decreased, in_ unchanged) removes the TAIL instead,
        # since surviving content still starts at the same `in_`: removed =
        # [old_start + new_duration, old_start + old_duration). These are
        # genuinely different positions (not the same formula) — e.g. a
        # clip at start=5 with old_duration=10 trimmed to new_duration=6
        # removes [5,9) if trimmed from the front but [11,15) if trimmed
        # from the back.
        delta_in = c.in_ - old_in
        if delta_in > 1e-9:
            removed_start = old_start
        else:
            removed_start = old_start + new_duration
        removed_len = old_duration - new_duration
        _ripple_overlays(store.edl, removed_start, removed_start + removed_len)
    summary = f"Trim {c.id} → in={c.in_:.2f} out={c.out:.2f}"
    store.commit("trim_clip", args, summary)
    return {"summary": summary, "duration": c.duration}


def move_clip(store: EDLStore, args: dict) -> dict:
    res = store.edl.get_clip(args["clip_id"])
    if not res:
        raise ValueError("clip not found")
    track, c = res
    new_track_id = args.get("new_track")
    if new_track_id and new_track_id != track.id:
        # Only a media Clip is restricted to video/audio-family lanes — a
        # Sticker/TextClip crossing between two sticker-type or two
        # text-type tracks (e.g. moving a caption from tx_super to tx_hook)
        # is legitimate and must not go through the media-only check.
        new_t = _v_track_for_media(store.edl, new_track_id) if isinstance(c, Clip) \
            else _v_track(store.edl, new_track_id)
        track.clips.remove(c)
        new_t.clips.append(c)
        track = new_t
    if hasattr(c, "start"):
        # Clamp to >= 0; ffmpeg can't address negative timeline positions
        # and the timeline renderer would crash on the next preview.
        requested_start = max(0.0, float(args["new_start"]))
        if isinstance(c, Clip):
            # Cross-track (or same-track) drop onto an occupied range used to
            # silently stack two media clips at the same time — no data loss
            # (both survive in the EDL) but the canvas draws them with
            # identical fill and no z-order/outline, reading as "merged".
            # Snap to the first free gap at-or-after the requested position
            # instead of allowing the overlap; this is the backend
            # enforcement so Claude/MCP callers can't create it either (the
            # Timeline.tsx drop handler mirrors this client-side for instant
            # feedback, but this is the real guard).
            c.start = _first_free_gap(track, c.duration, requested_start, ignore_clip_id=c.id)
        else:
            c.start = requested_start
    track.clips.sort(key=lambda x: getattr(x, "start", 0))
    summary = f"Move {c.id} → {track.id} @ {c.start:.2f}s"
    store.commit("move_clip", args, summary)
    return {"summary": summary}


def reorder_clips(store: EDLStore, args: dict) -> dict:
    track = _v_track(store.edl, args["track"])
    order = list(args["order"])
    by_id = {c.id: c for c in track.clips}
    if set(order) != set(by_id.keys()):
        raise ValueError("order must contain exactly the current clip ids")
    track.clips = [by_id[i] for i in order]
    _ripple_close_gap(track)
    summary = f"Reorder {track.id}: {', '.join(order)}"
    store.commit("reorder_clips", args, summary)
    return {"summary": summary}


def ripple_delete(store: EDLStore, args: dict) -> dict:
    res = store.edl.get_clip(args["clip_id"])
    if not res:
        raise ValueError("clip not found")
    track, c = res
    # Capture the removed interval BEFORE the track repacks — only meaningful
    # (and only applied) when deleting a v1 media clip; deleting a sticker/
    # text overlay itself must not shift every OTHER overlay.
    removed_start, removed_end = (c.start, c.start + c.duration) if isinstance(c, Clip) else (None, None)
    track.clips.remove(c)
    _ripple_close_gap(track)
    if track.id == "v1" and removed_start is not None:
        _ripple_overlays(store.edl, removed_start, removed_end)
    summary = f"Delete {c.id} (ripple)"
    store.commit("ripple_delete", args, summary)
    return {"summary": summary}


def duplicate_clip(store: EDLStore, args: dict) -> dict:
    res = store.edl.get_clip(args["clip_id"])
    if not res:
        raise ValueError("clip not found")
    track, c = res
    if not isinstance(c, Clip):
        raise ValueError("duplicate_clip only supports media clips")
    dup = c.model_copy(update={"id": f"c_{c.id[2:]}d", "start": c.start + c.duration})
    track.clips.append(dup)
    _ripple_close_gap(track)
    summary = f"Duplicate {c.id} → {dup.id}"
    store.commit("duplicate_clip", args, summary)
    return {"summary": summary, "new_clip_id": dup.id}


# ---------- project ----------

def set_canvas(store: EDLStore, args: dict) -> dict:
    c = store.edl.canvas
    old_w, old_h = c.w, c.h
    if "w" in args:
        c.w = int(args["w"])
    if "h" in args:
        c.h = int(args["h"])
    if "fps" in args:
        c.fps = int(args["fps"])
    _rescale_overlays_for_canvas_change(store.edl, old_w, old_h, c.w, c.h)
    summary = f"Canvas → {c.w}×{c.h} @ {c.fps}fps"
    store.commit("set_canvas", args, summary)
    return {"summary": summary}


_RATIOS = {
    "9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350),
}


def set_aspect_ratio(store: EDLStore, args: dict) -> dict:
    old_w, old_h = store.edl.canvas.w, store.edl.canvas.h
    w, h = _RATIOS[args["ratio"]]
    store.edl.canvas.w = w
    store.edl.canvas.h = h
    _rescale_overlays_for_canvas_change(store.edl, old_w, old_h, w, h)
    summary = f"Aspect → {args['ratio']} ({w}×{h})"
    store.commit("set_aspect_ratio", args, summary)
    return {"summary": summary}


def undo_op(store: EDLStore, args: dict) -> dict:
    ok = store.undo()
    return {"ok": ok, "summary": "Undo" if ok else "Nothing to undo",
            "redo_available": store.redo_available}


def redo_op(store: EDLStore, args: dict) -> dict:
    ok = store.redo()
    return {"ok": ok, "summary": "Redo" if ok else "Nothing to redo",
            "redo_available": store.redo_available}


def render_preview_tool(store: EDLStore, args: dict) -> dict:
    res = _render_preview(store.edl, store.dir)
    return {"path": str(res.path), "cached": res.cached, "edl_hash": res.edl_hash}


def set_track_muted(store: EDLStore, args: dict) -> dict:
    """Mute or unmute a track. Muted tracks are skipped at render time."""
    track_id = str(args["track"])
    muted = bool(args.get("muted", True))
    track = store.edl.get_track(track_id)
    if not track:
        raise ValueError(f"track {track_id} not found")
    track.muted = muted
    summary = f"{'Mute' if muted else 'Unmute'} track {track_id}"
    store.commit("set_track_muted", args, summary)
    return {"summary": summary, "track": track_id, "muted": muted}


# ---------- text / captions / brand kit ----------

def add_super_text(store: EDLStore, args: dict) -> dict:
    text = str(args["text"])
    start = float(args["start"])
    end = float(args["end"])
    role = str(args.get("role", "super"))
    track_id = "tx_super" if role == "super" else "tx_hook" if role == "hook" else "tx_super"
    track = ensure_track(store.edl, track_id, "text", z=11 if role == "super" else 10)
    canvas = store.edl.canvas
    # Default position: bottom third for super, center for hook.
    from ..edl.schema import Transform
    y_default = canvas.h * (0.75 if role == "super" else 0.5)
    clip = TextClip(
        text=text, start=start, end=end, role=role,
        transform=Transform(x=canvas.w / 2, y=y_default),
    )
    # Idempotency: an identical re-run (same text/role/start/end on this track)
    # must not stack a second overlay — that produced the "double subtitle" bug.
    def _same(existing) -> bool:
        return (
            isinstance(existing, TextClip)
            and getattr(existing, "text", None) == clip.text
            and getattr(existing, "role", None) == clip.role
            and abs(existing.start - clip.start) < 1e-6
            and abs(existing.end - clip.end) < 1e-6
        )
    if any(_same(c) for c in track.clips):
        summary = f"Text already present: {clip.text!r} @ {clip.start:.2f}s"
        return {"summary": summary}  # no commit — nothing changed
    # Optional explicit replace: drop prior same-role overlapping clips.
    if bool(args.get("replace", False)):
        track.clips = [
            c for c in track.clips
            if not (isinstance(c, TextClip) and getattr(c, "role", None) == clip.role
                    and c.start < clip.end and c.end > clip.start)
        ]
    track.clips.append(clip)
    summary = f"Add {role} text “{text[:40]}” at {start:.2f}–{end:.2f}s"
    store.commit("add_super_text", args, summary)
    return {"clip_id": clip.id, "summary": summary}


def add_hook_overlay(store: EDLStore, args: dict) -> dict:
    text = str(args["text"])
    duration = float(args.get("duration", 3.0))
    return add_super_text(store, {"text": text, "start": 0.0, "end": duration, "role": "hook"})


# ---------- The 3-axis hook stack ---------------------------------------------
#
# Short-form viewers decide whether to keep watching in roughly 3 seconds.
# Three signals pull them in; missing any one cuts the chance ~in half:
#
#   👁 Visual  — bold, unexpected opening. Motion. Not a static talking head.
#   ✍ Text    — overlay in the first frames so silent-autoplay viewers get
#                the topic instantly without unmuting.
#   🎧 Audio   — strong opening line, trending sound, or audio cue that grabs
#                attention before they even look at the screen.
#
# `apply_hook_stack` lays down all three at once. `audit_aesthetic` reports
# which axes are present and blocks export when zero are.

def apply_hook_stack(store: EDLStore, args: dict) -> dict:
    """Bake all three hook axes onto the first ~3s of the project.

    Args:
        text      — hook overlay text. If omitted, calls generate_hook.
        duration  — seconds the hook holds (default 3.0).
        visual    — "punch_in" (default), "ken_burns", or "none". Adds keyframes
                    to V1's first clip so the opening has motion.
        audio     — "fade_boost" (default), "none". afade=in over 0.5s on V1
                    audio so the opening lands cleanly even on cold autoplay.

    Idempotent: tagged via metadata so re-running replaces rather than stacks.
    """
    duration = float(args.get("duration", 3.0))
    visual_kind = str(args.get("visual", "punch_in"))
    audio_kind = str(args.get("audio", "fade_boost"))

    # 1) TEXT axis ---------------------------------------------------------
    text = args.get("text")
    if not text:
        # Lean on the LLM/heuristic fallback to draft a hook line.
        try:
            r = generate_hook(store, {})
            cands = r.get("candidates") or []
            text = cands[0] if cands else "Watch this until the end."
        except Exception:
            text = "Watch this until the end."
    # Remove any prior hook to keep this idempotent. add_super_text routes
    # hooks to the `tx_hook` track; any role=="hook" overlay starting in the
    # first second is treated as a prior hook from this stack.
    for tid in ("tx_hook", "tx_super", "text"):
        prior = store.edl.get_track(tid)
        if prior:
            prior.clips = [
                c for c in prior.clips
                if not (isinstance(c, TextClip) and c.role == "hook" and c.start < 1.0)
            ]
    text_result = add_super_text(store, {
        "text": str(text), "start": 0.0, "end": duration, "role": "hook",
    })

    # 2) VISUAL axis -------------------------------------------------------
    v1 = store.edl.get_track("v1")
    first_clip = next(
        (c for c in (v1.clips if v1 else []) if isinstance(c, Clip)),
        None,
    )
    visual_applied = False
    if first_clip and visual_kind != "none":
        from ..edl.schema import Keyframe
        if visual_kind == "punch_in":
            # 1.0 → 1.06 over the hook window, ease-out → settles snappy.
            first_clip.transform.scale = Keyframe(
                keyframes=[[0.0, 1.0], [duration, 1.06]],
                interp="ease-out",
            )
            visual_applied = True
        elif visual_kind == "ken_burns":
            # Slow 1.0 → 1.10 zoom with a slight horizontal drift.
            first_clip.transform.scale = Keyframe(
                keyframes=[[0.0, 1.0], [duration, 1.10]],
                interp="linear",
            )
            first_clip.transform.x = Keyframe(
                keyframes=[[0.0, 0.0], [duration, 24.0]],
                interp="linear",
            )
            visual_applied = True

    # 3) AUDIO axis --------------------------------------------------------
    audio_applied = False
    if first_clip and audio_kind == "fade_boost":
        # Short fade-in so the audio lands cleanly without click/pop on
        # autoplay. Doesn't boost gain itself — that'd require LUFS-aware
        # measurement; we lean on loudnorm at export to hit the target.
        if first_clip.audio.fade_in < 0.05:
            first_clip.audio.fade_in = 0.5
            audio_applied = True
    # Music starting at 0 also counts as an audio hook even without fade.
    music = store.edl.get_track("music")
    has_zero_start_music = bool(music) and any(
        isinstance(c, Clip) and c.start <= 0.05 for c in (music.clips if music else [])
    )
    if has_zero_start_music:
        audio_applied = True

    summary = (
        f"Hook stack: "
        f"{'✓' if True else '·'} text, "
        f"{'✓' if visual_applied else '·'} visual({visual_kind}), "
        f"{'✓' if audio_applied else '·'} audio"
    )
    store.commit("apply_hook_stack", args, summary)
    return {
        "summary": summary,
        "text": text,
        "axes": {
            "visual": visual_applied,
            "text": True,
            "audio": audio_applied,
        },
        "text_clip_id": text_result.get("clip_id") or text_result.get("text_id"),
    }


def add_caption_track(store: EDLStore, args: dict) -> dict:
    style = args.get("style", "default")
    position = args.get("position", "bottom")
    cap = store.edl.get_track("captions")
    if not cap:
        cap = ensure_track(store.edl, "captions", "captions", z=13)
        from ..edl.schema import CaptionsConfig
        cap.config = CaptionsConfig()
    if cap.config is None:
        from ..edl.schema import CaptionsConfig
        cap.config = CaptionsConfig()
    cap.config.enabled = True
    cap.config.style = style  # type: ignore
    cap.config.position = position  # type: ignore

    import json
    ingest_json = _current_v1_ingest_json(store)
    cap.clips = []
    seg_count = 0
    if ingest_json is not None:
        data = json.loads(ingest_json.read_text(encoding="utf-8"))
        tx = data.get("transcript") or {}
        canvas = store.edl.canvas
        y_pos = canvas.h * (0.85 if position == "bottom" else 0.5 if position == "center" else 0.15)

        if style == "word_emphasis":
            # Group words into chunks of 1-3 (default 2) and emit one TextClip
            # per chunk, timed to the words' start..end. The TextLayer renders
            # them at hook-size to deliver the punchy IG/TikTok karaoke look.
            chunk_size = int(args.get("chunk_size", 2))
            for seg in tx.get("segments", []):
                words = seg.get("words") or []
                if not words:
                    # No word-level timing — fall back to single-segment caption
                    cap.clips.append(TextClip(
                        text=(seg.get("text") or "").strip(),
                        start=float(seg["start"]), end=float(seg["end"]),
                        role="caption",
                        transform=Transform(x=canvas.w / 2, y=canvas.h * 0.5),
                    ))
                    seg_count += 1
                    continue
                for i in range(0, len(words), chunk_size):
                    chunk = words[i:i + chunk_size]
                    text = " ".join((w.get("word") or "").strip() for w in chunk).strip()
                    if not text:
                        continue
                    cap.clips.append(TextClip(
                        text=text.upper(),
                        start=float(chunk[0]["start"]),
                        end=float(chunk[-1]["end"]),
                        role="hook",  # hook style = bold/centered/big — perfect for word_emphasis
                        transform=Transform(x=canvas.w / 2, y=canvas.h * 0.5),
                    ))
                    seg_count += 1
        else:
            # default / ig_chunky: one caption per segment
            for seg in tx.get("segments", []):
                cap.clips.append(TextClip(
                    text=(seg.get("text") or "").strip(),
                    start=float(seg["start"]),
                    end=float(seg["end"]),
                    role="caption",
                    transform=Transform(x=canvas.w / 2, y=y_pos),
                ))
                seg_count += 1
    summary = f"Add caption track ({style}, {position}) — {seg_count} caption(s)"
    store.commit("add_caption_track", args, summary)
    return {"summary": summary, "lines": seg_count}


def auto_caption(store: EDLStore, args: dict) -> dict:
    """One-call, best-quality auto captions for Hindi + English.

    Re-transcribes the V1 source with the large-v3 Whisper model (Metal-
    accelerated, anti-hallucination flags) — far better than the fast `small`
    model used at upload — then formats the words into broadcast-quality cues
    (≤2 lines, reading-speed limited) and lays down a caption track.

    Args:
      style    — "default" | "ig_chunky" | "word_emphasis" (default ig_chunky)
      position — "bottom" | "center" | "top"
      language — force a language (e.g. "hi", "en"); omit to auto-detect
                 (handles Hinglish code-switching).
      model    — override the model (default WHISPER_CAPTION_MODEL = large-v3).
      max_chars / max_cps — caption length + reading-speed knobs.
    """
    from ..config import WHISPER_CAPTION_MODEL
    from ..ingest.transcribe import transcribe
    from ..ingest.caption_format import cues_from_segments

    style = str(args.get("style", "ig_chunky"))
    position = str(args.get("position", "bottom"))
    language = args.get("language")  # None → auto-detect (Hinglish-friendly)
    model = str(args.get("model") or WHISPER_CAPTION_MODEL)

    v1 = store.edl.get_track("v1")
    src_clip = next((c for c in (v1.clips if v1 else []) if isinstance(c, Clip)), None)
    if not src_clip:
        raise ValueError("auto_caption: no clip on v1 to caption")
    src = Path(src_clip.src)
    if not src.exists():
        raise ValueError(f"auto_caption: source not found: {src}")

    # High-quality re-transcription. backend="auto" picks Metal whisper.cpp.
    tx = transcribe(src, language=language, model_size=model, backend="auto")
    tx_dict = tx.model_dump()

    # Persist so get_transcript / translate / export_srt see the upgraded text.
    # `src` is the v1 source clip we just re-transcribed — its ingest.json is
    # always the sibling file ingest_upload() wrote alongside the normalized
    # media (see _current_v1_ingest_json). Using that clip's own directory,
    # rather than an arbitrary glob hit, guarantees the upgraded transcript
    # lands on the file this clip's src actually reads from.
    import json as _json
    ingest_json = src.parent / "ingest.json"
    if not ingest_json.exists():
        ingest_json.parent.mkdir(parents=True, exist_ok=True)
        ingest_json.write_text(_json.dumps({"transcript": tx_dict}, ensure_ascii=False), encoding="utf-8")
    else:
        data = _json.loads(ingest_json.read_text(encoding="utf-8"))
        data["transcript"] = tx_dict
        ingest_json.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # Build readable cues from the word stream.
    cues = cues_from_segments(
        tx_dict.get("segments", []),
        max_chars=int(args.get("max_chars", 42)),
        max_cps=float(args.get("max_cps", 17.0)),
    )

    # Lay down the caption track from the cues.
    cap = store.edl.get_track("captions")
    if not cap:
        cap = ensure_track(store.edl, "captions", "captions", z=13)
    from ..edl.schema import CaptionsConfig
    if cap.config is None:
        cap.config = CaptionsConfig()
    cap.config.enabled = True
    cap.config.style = style  # type: ignore
    cap.config.position = position  # type: ignore
    cap.config.lang = tx.language

    canvas = store.edl.canvas
    y_pos = canvas.h * (0.85 if position == "bottom"
                        else 0.5 if position == "center" else 0.15)
    cap.clips = []
    if style == "word_emphasis":
        chunk = int(args.get("chunk_size", 2))
        words = [w for seg in tx_dict.get("segments", []) for w in (seg.get("words") or [])]
        for i in range(0, len(words), chunk):
            grp = words[i:i + chunk]
            text = " ".join((w.get("word") or "").strip() for w in grp).strip()
            if not text:
                continue
            cap.clips.append(TextClip(
                text=text.upper(), start=float(grp[0]["start"]), end=float(grp[-1]["end"]),
                role="hook", transform=Transform(x=canvas.w / 2, y=canvas.h * 0.5),
            ))
    else:
        for cue in cues:
            cap.clips.append(TextClip(
                text=cue.text, start=cue.start, end=cue.end, role="caption",
                transform=Transform(x=canvas.w / 2, y=y_pos),
            ))

    n = len(cap.clips)
    preview = " / ".join(c.text.replace("\n", " ") for c in cap.clips[:2])
    summary = (f"Auto-captioned ({model}, {tx.language}): {n} {style} cues. "
               f"e.g. “{preview[:60]}”")
    store.commit("auto_caption", args, summary)
    return {"summary": summary, "language": tx.language, "model": model,
            "cues": n, "style": style,
            "sample": [c.as_dict() for c in cues[:3]]}


def apply_brand_kit(store: EDLStore, args: dict) -> dict:
    kit = BrandKit(
        handle=args.get("handle"),
        hashtags=list(args.get("hashtags", [])),
        end_card=args.get("end_card"),
        palette=list(args.get("palette", [])),
        font=args.get("font"),
    )
    info = _apply_brand_kit(store.edl, kit)
    summary = f"Apply brand kit: {', '.join(info['applied']) or '(empty)'}"
    store.commit("apply_brand_kit", args, summary)
    return {"summary": summary, **info}


def audit_aesthetic(store: EDLStore, args: dict) -> dict:
    return _audit(store.edl)


# ---------- M3: audio (music, fades, volumes), auto-trim, beats, reframe ----------

def add_music(store: EDLStore, args: dict) -> dict:
    src = _safe_src(args["src"])
    start = float(args.get("start", 0.0))
    in_ = float(args.get("in", 0.0))
    out = float(args.get("out", 0.0))
    duck = bool(args.get("duck", True))
    volume_db = float(args.get("volume_db", -12.0))

    if out <= 0:
        # Probe source duration
        from ..ingest.probe import probe
        try:
            p = probe(src)
            out = p.duration
        except Exception:
            out = max(1.0, store.edl.duration)

    track = store.edl.get_track("music")
    if not track:
        from ..edl.schema import Track
        track = Track(id="music", type="music", z=0, label="Music")
        store.edl.tracks.append(track)
    if duck:
        from ..edl.schema import MusicDuck
        track.duck = MusicDuck(to_db=-18.0, track_ref="a1")
    else:
        track.duck = None

    from ..edl.schema import Clip, AudioProps
    clip = Clip(
        src=src, in_=in_, out=out, start=start,
        audio=AudioProps(gain_db=volume_db, fade_in=0.5, fade_out=1.0),
    )
    track.clips.append(clip)
    summary = f"Add music {src.split('/')[-1]} @ {start:.1f}s, {volume_db:.0f}dB{', ducked' if duck else ''}"
    store.commit("add_music", args, summary)
    return {"clip_id": clip.id, "summary": summary, "duck": duck}


def set_duck(store: EDLStore, args: dict) -> dict:
    """Toggle (or configure) sidechain ducking on the music track — ONLY the
    duck flag, no clip mutation.

    Before this tool existed, the only way to flip ducking was re-adding the
    music clip via add_music(duck=...) then ripple_delete-ing the original
    (MediaBin.tsx's "Duck under speech" checkbox) — which re-probed the full
    source and reset start/in/out to 0/0/full-duration, discarding any trim
    or repositioning the user had done, and the ripple_delete + re-add could
    land on a fresh clip id the panel hadn't captured, making a second toggle
    silently no-op (issue 39, "clicking again does nothing, stays expanded").
    """
    track_id = str(args.get("track", "music"))
    track = store.edl.get_track(track_id)
    if not track:
        raise ValueError(f"track '{track_id}' not found")
    enabled = bool(args.get("enabled", track.duck is None))
    if enabled:
        from ..edl.schema import MusicDuck
        to_db = float(args.get("to_db", track.duck.to_db if track.duck else -18.0))
        track_ref = str(args.get("track_ref", track.duck.track_ref if track.duck else "a1"))
        track.duck = MusicDuck(to_db=to_db, track_ref=track_ref)
    else:
        track.duck = None
    summary = f"Duck {'on' if enabled else 'off'} for {track_id}"
    store.commit("set_duck", args, summary)
    return {"summary": summary, "enabled": enabled}


def set_volume(store: EDLStore, args: dict) -> dict:
    """Set gain on a track (id like 'music', 'a1') or a clip (id like 'c_xxx')."""
    target = str(args["target"])
    db = float(args["db"])
    track = store.edl.get_track(target)
    if track:
        # Apply uniformly to all clips on that track via audio.gain_db
        from ..edl.schema import Clip
        for c in track.clips:
            if isinstance(c, Clip):
                c.audio.gain_db = db
        summary = f"Set {target} volume → {db:+.1f} dB"
    else:
        res = store.edl.get_clip(target)
        if not res:
            raise ValueError(f"target {target} not found (track or clip id)")
        _, c = res
        from ..edl.schema import Clip
        if isinstance(c, Clip):
            c.audio.gain_db = db
        summary = f"Set clip {c.id} volume → {db:+.1f} dB"
    store.commit("set_volume", args, summary)
    return {"summary": summary}


def add_fade(store: EDLStore, args: dict) -> dict:
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    from ..edl.schema import Clip
    if not isinstance(c, Clip):
        raise ValueError("add_fade only supports media clips")
    c.audio.fade_in = float(args.get("in_s", c.audio.fade_in))
    c.audio.fade_out = float(args.get("out_s", c.audio.fade_out))
    summary = f"Fade {cid}: in={c.audio.fade_in:.2f}s out={c.audio.fade_out:.2f}s"
    store.commit("add_fade", args, summary)
    return {"summary": summary}


def remove_silences(store: EDLStore, args: dict) -> dict:
    """Detect silences in the V1 audio + emit cut ops to remove them.

    Uses ffmpeg `silencedetect` then translates ranges to cut_range calls.
    """
    threshold_db = float(args.get("threshold_db", -30))
    min_dur = float(args.get("min_dur", 0.5))
    keep_pad = float(args.get("keep_pad", 0.1))  # leave a little air
    track_id = str(args.get("track", "v1"))

    track = store.edl.get_track(track_id)
    if not track or not track.clips:
        raise ValueError(f"no clips on track {track_id}")
    # Run silencedetect on each contributing source-clip slice in timeline order.
    import re, subprocess
    from ..edl.schema import Clip
    ranges_to_cut: list[tuple[float, float]] = []  # in TIMELINE coords
    for c in track.clips:
        if not isinstance(c, Clip):
            continue
        proc = subprocess.run(
            [_pu.FFMPEG, "-hide_banner", "-nostats",
             "-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src,
             "-af", f"silencedetect=noise={threshold_db}dB:d={min_dur}",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        starts = [float(m.group(1)) for m in re.finditer(r"silence_start: ([\d.]+)", proc.stderr)]
        ends = [float(m.group(1)) for m in re.finditer(r"silence_end: ([\d.]+)", proc.stderr)]
        # Pair them
        for i in range(min(len(starts), len(ends))):
            local_start = starts[i] + keep_pad
            local_end = ends[i] - keep_pad
            if local_end - local_start < min_dur:
                continue
            tl_start = c.start + local_start
            tl_end = c.start + local_end
            ranges_to_cut.append((tl_start, tl_end))

    if not ranges_to_cut:
        store.commit("remove_silences", args, "Remove silences: none found")
        return {"summary": "No silences detected", "cuts": 0}

    # Apply cuts back-to-front so timeline coords stay valid mid-cut
    ranges_to_cut.sort(reverse=True)
    n = 0
    for s, e in ranges_to_cut:
        try:
            cut_range(store, {"track": track_id, "start": s, "end": e})
            n += 1
        except ValueError:
            continue
    summary = f"Removed {n} silences (threshold {threshold_db}dB, min {min_dur}s)"
    # The individual cut_range commits already log; add a final summary op too.
    store.commit("remove_silences", args, summary)
    return {"summary": summary, "cuts": n}


def remove_fillers(store: EDLStore, args: dict) -> dict:
    """Find filler-word ranges in the transcript and cut them out."""
    fillers = [w.lower() for w in args.get("words", ["um", "uh", "like", "you know", "so basically"])]
    pad = float(args.get("pad", 0.05))
    track_id = str(args.get("track", "v1"))

    transcript = get_transcript(store, {})
    words = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            words.append(w)
    if not words:
        store.commit("remove_fillers", args, "Remove fillers: no transcript")
        return {"summary": "No transcript available", "cuts": 0}

    ranges: list[tuple[float, float]] = []
    for w in words:
        token = (w.get("word") or "").strip().lower().rstrip(",.!?")
        if token in fillers:
            ranges.append((max(0.0, w["start"] - pad), w["end"] + pad))

    if not ranges:
        store.commit("remove_fillers", args, "Remove fillers: none found")
        return {"summary": "No filler words found", "cuts": 0}

    ranges.sort(reverse=True)
    n = 0
    for s, e in ranges:
        try:
            cut_range(store, {"track": track_id, "start": s, "end": e})
            n += 1
        except ValueError:
            continue
    summary = f"Removed {n} filler words"
    store.commit("remove_fillers", args, summary)
    return {"summary": summary, "cuts": n}


def auto_cut_to_beats(store: EDLStore, args: dict) -> dict:
    """Detect beats in the music track and split V1 at each beat boundary."""
    subdivision = int(args.get("subdivision", 4))  # cut every Nth beat
    music = store.edl.get_track("music")
    from ..edl.schema import Clip
    music_clip = next((c for c in (music.clips if music else []) if isinstance(c, Clip)), None)
    if not music_clip:
        raise ValueError("no music clip on the music track — add_music first")

    try:
        import librosa
        import numpy as np
    except ImportError:
        raise RuntimeError("Beat detection needs librosa: `uv sync --extra ai`")

    y, sr = librosa.load(music_clip.src, sr=22050, mono=True,
                          offset=music_clip.in_, duration=music_clip.duration)
    _, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    # Project onto timeline (music_clip.start anchors them)
    beat_tl_times = [music_clip.start + t for t in beat_times if t >= 0]
    cuts = beat_tl_times[::max(1, subdivision)]
    n = 0
    for t in cuts:
        try:
            split_at(store, {"track": "v1", "time": t})
            n += 1
        except (ValueError, KeyError):
            continue
    summary = f"Cut V1 at {n} beats (every {subdivision})"
    store.commit("auto_cut_to_beats", args, summary)
    return {"summary": summary, "splits": n, "beats_total": len(beat_times)}


def tts_voiceover(store: EDLStore, args: dict) -> dict:
    """Generate a voiceover line via Piper TTS and place it on the vo track."""
    text = str(args["text"])
    voice = str(args.get("voice", "en_US-amy-medium"))
    start = float(args.get("start", 0.0))
    volume_db = float(args.get("volume_db", 0.0))

    from ..ai.tts import synthesize, cached_path
    cache_dir = store.dir / "cache" / "tts"
    out = cached_path(text, voice, cache_dir)
    synthesize(text, out, voice=voice)
    # Probe duration
    from ..ingest.probe import probe
    p = probe(out)
    track = ensure_track(store.edl, "vo", "vo", z=0)
    from ..edl.schema import Clip, AudioProps
    clip = Clip(src=str(out), in_=0.0, out=p.duration, start=start,
                audio=AudioProps(gain_db=volume_db, fade_in=0.05, fade_out=0.1))
    track.clips.append(clip)
    summary = f"VO ({voice}) {p.duration:.1f}s @ {start:.1f}s: \"{text[:40]}\""
    store.commit("tts_voiceover", args, summary)
    return {"clip_id": clip.id, "duration": p.duration, "summary": summary}


def find_moments(store: EDLStore, args: dict) -> dict:
    """Find moments by query: transcript-first, vision verify on top candidates."""
    query = str(args["query"])
    top_k = int(args.get("top_k", 3))
    transcript = get_transcript(store, {})
    # Use the first V1 source as the search target (multi-source coming later)
    v1 = store.edl.get_track("v1")
    src_clip = next((c for c in (v1.clips if v1 else []) if isinstance(c, Clip)), None)
    if not src_clip:
        return {"matches": [], "summary": "no clips on v1"}
    from ..ai.vision import find_moments as _find
    matches = _find(query, transcript, Path(src_clip.src),
                    cache_dir=store.dir / "cache", top_k=top_k)
    return {"matches": matches, "query": query}


def search_media(store: EDLStore, args: dict) -> dict:
    """Search the project's footage by content (palmier-pro style).

    scope:
      - "visual" — local CLIP model matches frames to the text query
                   ("a sunset over water"); no transcript needed.
      - "spoken" — searches the transcript for the phrase.
      - "both"   — merges visual + spoken (default).

    Returns {visual: [...], spoken: [...]} ranked by relevance.
    """
    query = str(args["query"]).strip()
    if not query:
        raise ValueError("search_media: query is empty")
    scope = str(args.get("scope", "both"))
    if scope not in ("visual", "spoken", "both"):
        raise ValueError("search_media: scope must be visual|spoken|both")
    limit = max(1, min(int(args.get("limit", 10)), 50))

    v1 = store.edl.get_track("v1")
    clips = [c for c in (v1.clips if v1 else []) if isinstance(c, Clip)]
    out: dict = {"query": query, "scope": scope}

    if scope in ("visual", "both"):
        from ..ai import clip_search
        if not clip_search.available():
            out["visual"] = {"status": "unavailable",
                             "message": "CLIP not installed (uv add open_clip_torch)"}
        elif not clips:
            out["visual"] = {"status": "empty", "results": []}
        else:
            payload = [{"id": c.id, "src": c.src, "in": c.in_, "out": c.out}
                       for c in clips]
            results = clip_search.search(query, payload,
                                         store.dir / "cache" / "clip_index", limit=limit)
            out["visual"] = {"status": "ok", "results": results}

    if scope in ("spoken", "both"):
        tx = get_transcript(store, {})
        q_low = query.lower()
        hits = []
        for seg in (tx.get("segments") or []):
            text = (seg.get("text") or "")
            if q_low in text.lower():
                hits.append({"start": round(float(seg.get("start", 0)), 2),
                             "end": round(float(seg.get("end", 0)), 2),
                             "text": text.strip()})
        out["spoken"] = {"status": "ok", "results": hits[:limit]}

    return out


def match_style(store: EDLStore, args: dict) -> dict:
    """Extract a style fingerprint from a reference video."""
    ref_path = Path(_safe_src(args["reference"]))
    if not ref_path.exists():
        raise ValueError(f"reference not found: {ref_path}")
    from ..ai.style_match import style_fingerprint
    fp = style_fingerprint(ref_path, store.dir / "cache")
    return {"reference": str(ref_path), "fingerprint": fp}


def add_effect(store: EDLStore, args: dict) -> dict:
    """Append a per-clip effect to a video clip."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("add_effect only supports media clips")
    from ..edl.schema import Effect
    eff = Effect(type=str(args["type"]), params=dict(args.get("params") or {}))
    c.effects.append(eff)
    summary = f"Add effect {eff.type} → {cid}"
    store.commit("add_effect", args, summary)
    return {"summary": summary, "index": len(c.effects) - 1}


def remove_effect(store: EDLStore, args: dict) -> dict:
    cid = str(args["clip_id"])
    # Accept both `index` (verbose) and `idx` (compact) — JSON-RPC convention
    # uses `idx`, Pydantic-flavoured callers tend to use `index`.
    idx = int(args.get("index", args.get("idx", 0)))
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip) or idx >= len(c.effects):
        raise ValueError("invalid clip or effect index")
    removed = c.effects.pop(idx)
    summary = f"Remove effect {removed.type} from {cid}"
    store.commit("remove_effect", args, summary)
    return {"summary": summary}


def color_grade(store: EDLStore, args: dict) -> dict:
    """Convenience: add a color effect with brightness/contrast/sat/temp/tint."""
    params = {k: float(v) for k, v in args.items()
              if k in ("brightness", "contrast", "saturation", "sat", "gamma", "temp", "tint")
              and v is not None}
    cid = args.get("clip_id")
    target_clips: list[Clip] = []
    if cid:
        res = store.edl.get_clip(str(cid))
        if not res:
            raise ValueError(f"clip {cid} not found")
        _, c = res
        if isinstance(c, Clip):
            target_clips.append(c)
    else:
        v1 = store.edl.get_track("v1")
        target_clips = [c for c in (v1.clips if v1 else []) if isinstance(c, Clip)]
    from ..edl.schema import Effect
    for c in target_clips:
        # Merge into the clip's existing "color" effect rather than appending
        # a new one: each slider release in Properties.tsx sends only the ONE
        # param the user just moved (e.g. {"brightness": 0.1}), so appending
        # would stack an independent eq=/colorbalance= filter pass per tweak —
        # 3 brightness adjustments in a row would compound 3x instead of
        # settling on the final value, and the effect list would grow
        # unbounded. A clip has exactly one logical color grade.
        existing = next((e for e in c.effects if e.type in ("color", "color_grade")), None)
        if existing is not None:
            existing.params.update(params)
        else:
            c.effects.append(Effect(type="color", params=params))
    summary = f"Color grade {len(target_clips)} clip(s): {params}"
    store.commit("color_grade", args, summary)
    return {"summary": summary, "applied_to": [c.id for c in target_clips]}


def apply_lut(store: EDLStore, args: dict) -> dict:
    # Accept both `src` and `lut_path` (the README/plan documents `lut_path`).
    src_arg = args.get("src") or args.get("lut_path")
    if not src_arg:
        raise ValueError("apply_lut needs `src` or `lut_path`")
    src = _safe_src(src_arg)
    intensity = float(args.get("intensity", 1.0))
    cid = args.get("clip_id")
    target_clips: list[Clip] = []
    if cid:
        res = store.edl.get_clip(str(cid))
        if not res:
            raise ValueError(f"clip {cid} not found")
        _, c = res
        if isinstance(c, Clip):
            target_clips.append(c)
    else:
        v1 = store.edl.get_track("v1")
        target_clips = [c for c in (v1.clips if v1 else []) if isinstance(c, Clip)]
    from ..edl.schema import Effect
    for c in target_clips:
        c.effects.append(Effect(type="lut", params={"src": src, "intensity": intensity}))
    summary = f"Apply LUT {Path(src).name} (×{intensity:.2f}) to {len(target_clips)} clip(s)"
    store.commit("apply_lut", args, summary)
    return {"summary": summary}


def add_transition(store: EDLStore, args: dict) -> dict:
    """Add a transition at a timeline boundary on V1.

    `at` is the timeline second the transition centers on (typically the boundary
    between two clips). Adjacent clips around `at` will be xfaded.
    """
    from ..edl.schema import Transition
    from ..render.transitions import is_valid, all_names, resolve_transition
    v1 = store.edl.get_track("v1")
    if not v1:
        raise ValueError("v1 track not found")
    ttype = str(args.get("type", "fade")).strip().lower()
    if not is_valid(ttype):
        raise ValueError(
            f"unknown transition {ttype!r}. {len(all_names())} available — "
            f"call list_transitions to see them. Common: fade, dissolve, "
            f"slideleft, zoomin, circleopen, radial, pixelize, glitch, whip, spin"
        )
    tr = Transition(at=float(args["at"]), type=ttype,
                    duration=float(args.get("duration", 0.5)))
    v1.transitions.append(tr)
    resolved, _ = resolve_transition(ttype)
    note = "" if resolved == ttype else f" → {resolved}"
    summary = f"Add {tr.type}{note} transition at {tr.at:.2f}s ({tr.duration:.2f}s)"
    store.commit("add_transition", args, summary)
    return {"summary": summary}


def add_mask(store: EDLStore, args: dict) -> dict:
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("add_mask only supports media clips")
    from ..edl.schema import Mask
    mtype = str(args.get("type", "circle"))
    feather = float(args.get("feather", 8.0))
    canvas = store.edl.canvas
    pos = args.get("position") or [canvas.w / 2, canvas.h / 2]
    c.mask = Mask(
        type=mtype,
        feather=feather,
        position=(float(pos[0]), float(pos[1])),
        invert=bool(args.get("invert", False)),
    )
    summary = f"Mask {cid}: {mtype} feather={feather:.0f}"
    store.commit("add_mask", args, summary)
    return {"summary": summary}


def chroma_key(store: EDLStore, args: dict) -> dict:
    """Set/clear chroma key on a clip. Args: clip_id, color (hex), similarity,
    smoothness, spill_suppress. Pass color=null to clear."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("chroma_key only supports media clips")
    if args.get("color") is None and "color" in args:
        c.chromakey = None
        summary = f"Chroma key cleared on {cid}"
    else:
        from ..edl.schema import ChromaKey
        c.chromakey = ChromaKey(
            color=str(args.get("color", "#00FF00")),
            similarity=float(args.get("similarity", 0.4)),
            smoothness=float(args.get("smoothness", 0.1)),
            spill_suppress=float(args.get("spill_suppress", 0.5)),
        )
        summary = (f"Chroma key {cid}: {c.chromakey.color} "
                   f"sim={c.chromakey.similarity:.2f} blend={c.chromakey.smoothness:.2f}")
    store.commit("chroma_key", args, summary)
    return {"summary": summary}


def set_track_muted(store: EDLStore, args: dict) -> dict:
    """Toggle/set muted on a track. Audio tracks (music/vo) are skipped from
    the audio mix; video track mute is informational for now."""
    track_id = str(args["track"])
    track = store.edl.get_track(track_id)
    if not track:
        raise ValueError(f"track {track_id!r} not found")
    track.muted = bool(args.get("muted", not track.muted))
    summary = f"{'Muted' if track.muted else 'Unmuted'} track {track.id}"
    store.commit("set_track_muted", args, summary)
    return {"summary": summary, "muted": track.muted}


def set_track_locked(store: EDLStore, args: dict) -> dict:
    """Lock/unlock a track (UI-only flag for now)."""
    track_id = str(args["track"])
    track = store.edl.get_track(track_id)
    if not track:
        raise ValueError(f"track {track_id!r} not found")
    track.locked = bool(args.get("locked", not track.locked))
    summary = f"{'Locked' if track.locked else 'Unlocked'} track {track.id}"
    store.commit("set_track_locked", args, summary)
    return {"summary": summary, "locked": track.locked}


def set_speed(store: EDLStore, args: dict) -> dict:
    """Set playback-speed factor on a clip (1.0 = normal). Saved to EDL; the
    renderer applies setpts/atempo when speed is non-default. M3 stores it as
    a scalar; full speed-curve support comes later."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("set_speed only supports media clips")
    factor = float(args["factor"])
    if factor <= 0:
        raise ValueError("speed factor must be > 0")
    c.speed = factor
    summary = f"Speed {cid} → {factor:.2f}×"
    store.commit("set_speed", args, summary)
    return {"summary": summary}


def set_clip_transform(store: EDLStore, args: dict) -> dict:
    """Adjust transform on a clip or sticker: rotation (deg), scale, x, y, opacity."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    # Media clips and stickers both carry a Transform; text clips don't.
    if not hasattr(c, "transform"):
        raise ValueError("set_clip_transform needs a media clip or sticker (it has no transform)")
    for k in ("x", "y", "scale", "rotation", "opacity"):
        if k in args and args[k] is not None:
            setattr(c.transform, k, float(args[k]))
    summary = (f"Transform {cid}: rot={c.transform.rotation} scale={c.transform.scale} "
               f"x={c.transform.x} y={c.transform.y} opacity={c.transform.opacity}")
    store.commit("set_clip_transform", args, summary)
    return {"summary": summary}


def bulk_delete(store: EDLStore, args: dict) -> dict:
    """Ripple-delete multiple clips in one op (avoids dispatching N times)."""
    ids = list(args.get("clip_ids", []))
    if not ids:
        raise ValueError("clip_ids must be a non-empty list")
    n = 0
    affected_tracks: set[str] = set()
    from .dispatch import _ripple_close_gap  # self-import safe
    for cid in ids:
        res = store.edl.get_clip(cid)
        if not res:
            continue
        track, c = res
        try:
            track.clips.remove(c)
            affected_tracks.add(track.id)
            n += 1
        except ValueError:
            pass
    for tid in affected_tracks:
        t = store.edl.get_track(tid)
        if t:
            _ripple_close_gap(t)
    summary = f"Bulk delete {n} clip(s)"
    store.commit("bulk_delete", args, summary)
    return {"summary": summary, "deleted": n}


def bulk_duplicate(store: EDLStore, args: dict) -> dict:
    ids = list(args.get("clip_ids", []))
    if not ids:
        raise ValueError("clip_ids must be a non-empty list")
    n = 0
    new_ids: list[str] = []
    affected_tracks: set[str] = set()
    from .dispatch import _ripple_close_gap
    for cid in ids:
        res = store.edl.get_clip(cid)
        if not res:
            continue
        track, c = res
        if not isinstance(c, Clip):
            continue
        dup = c.model_copy(update={
            "id": f"c_{c.id[2:]}d", "start": c.start + c.duration,
        })
        track.clips.append(dup)
        new_ids.append(dup.id)
        affected_tracks.add(track.id)
        n += 1
    for tid in affected_tracks:
        t = store.edl.get_track(tid)
        if t:
            _ripple_close_gap(t)
    summary = f"Bulk duplicate {n} clip(s)"
    store.commit("bulk_duplicate", args, summary)
    return {"summary": summary, "duplicated": n, "new_ids": new_ids}


def add_marker(store: EDLStore, args: dict) -> dict:
    from ..edl.schema import Marker
    m = Marker(
        time=float(args["time"]),
        label=str(args.get("label", "")),
        color=str(args.get("color", "#fbbf24")),  # amber — must differ from the playhead red (#ff4d6d)
    )
    store.edl.markers.append(m)
    summary = f"Marker @ {m.time:.2f}s{' — ' + m.label if m.label else ''}"
    store.commit("add_marker", args, summary)
    return {"summary": summary, "marker_id": m.id}


def remove_marker(store: EDLStore, args: dict) -> dict:
    mid = str(args["marker_id"])
    before = len(store.edl.markers)
    store.edl.markers = [m for m in store.edl.markers if m.id != mid]
    if len(store.edl.markers) == before:
        raise ValueError(f"marker {mid!r} not found")
    summary = f"Removed marker {mid}"
    store.commit("remove_marker", args, summary)
    return {"summary": summary}


def pyannote_status(store: EDLStore, args: dict) -> dict:
    """Tell the user what's missing for high-quality pyannote diarization, with
    no network or model load. Mirrors `ai.diarize.pyannote_status()`."""
    from ..ai.diarize import pyannote_status as _status
    return _status()


def diarize(store: EDLStore, args: dict) -> dict:
    """Speaker diarization on the V1 source.

    Uses pyannote.audio when HUGGINGFACE_TOKEN is set (best quality), else
    falls back to a librosa MFCC + KMeans heuristic that runs out of the box.
    Pass `fallback=False` to force the pyannote path.
    """
    v1 = store.edl.get_track("v1")
    src_clip = next((c for c in (v1.clips if v1 else []) if isinstance(c, Clip)), None)
    if not src_clip:
        raise ValueError("no clips on v1 to diarize")
    from ..ai.diarize import diarize as _diarize
    cache_dir = store.dir / "cache" / "diarize"
    turns = _diarize(Path(src_clip.src), cache_dir,
                     fallback=bool(args.get("fallback", True)),
                     num_speakers=int(args.get("num_speakers", 2)))
    speakers = sorted({t["speaker"] for t in turns})
    summary = f"Diarized {src_clip.id}: {len(turns)} turns, {len(speakers)} speaker(s)"
    return {"summary": summary, "turns": turns, "speakers": speakers}


def name_speakers(store: EDLStore, args: dict) -> dict:
    """Save a speaker→display-name mapping for use in lower-thirds. Currently
    informational only — future M-show passes will auto-place lower-thirds."""
    mapping = dict(args.get("mapping", {}))
    if not mapping:
        raise ValueError("name_speakers needs a mapping like {SPEAKER_00: 'Host'}")
    # Persist on the EDL via show_template field for now (M3.5 placeholder)
    import json
    p = store.dir / "speakers.json"
    p.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    summary = f"Named speakers: {', '.join(f'{k}={v}' for k, v in mapping.items())}"
    return {"summary": summary, "mapping": mapping, "path": str(p)}


def set_loudness_target(store: EDLStore, args: dict) -> dict:
    """Set the per-export speech loudness target. Default Reels/TikTok = -16
    LUFS, YouTube = -14, broadcast = -23. Pass `lufs=null` to skip the
    loudnorm pass (raw mix only)."""
    lufs = args.get("lufs", -16.0)
    store.edl.canvas.loudness_lufs = float(lufs) if lufs is not None else None
    summary = (f"Loudness target = {store.edl.canvas.loudness_lufs} LUFS"
               if store.edl.canvas.loudness_lufs is not None else
               "Loudness normalisation disabled")
    store.commit("set_loudness_target", args, summary)
    return {"summary": summary}


_EXPORT_PRESETS = {
    "reels":      {"w": 1080, "h": 1920, "fps": 30, "bitrate_kbps": 8000,  "lufs": -16.0},
    "shorts":     {"w": 1080, "h": 1920, "fps": 30, "bitrate_kbps": 8000,  "lufs": -14.0},
    "tiktok":     {"w": 1080, "h": 1920, "fps": 30, "bitrate_kbps": 8000,  "lufs": -16.0},
    "story":      {"w": 1080, "h": 1920, "fps": 30, "bitrate_kbps": 6000,  "lufs": -16.0},
    "ig_feed_1x1":{"w": 1080, "h": 1080, "fps": 30, "bitrate_kbps": 6000,  "lufs": -16.0},
    "ig_feed_4x5":{"w": 1080, "h": 1350, "fps": 30, "bitrate_kbps": 6000,  "lufs": -16.0},
    "youtube_16x9":{"w": 1920, "h": 1080,"fps": 30, "bitrate_kbps": 12000, "lufs": -14.0},
    "youtube_4k": {"w": 3840, "h": 2160, "fps": 30, "bitrate_kbps": 35000, "lufs": -14.0},
}


def apply_export_preset(store: EDLStore, args: dict) -> dict:
    """Set canvas + bitrate + loudness target from a named platform preset."""
    name = str(args.get("name", "reels")).lower()
    p = _EXPORT_PRESETS.get(name)
    if not p:
        raise ValueError(f"unknown export preset: {name}. options: {list(_EXPORT_PRESETS)}")
    canvas = store.edl.canvas
    canvas.w, canvas.h, canvas.fps = p["w"], p["h"], p["fps"]
    canvas.bitrate_kbps = p["bitrate_kbps"]
    canvas.loudness_lufs = p["lufs"]
    summary = (f"Export preset {name}: {canvas.w}×{canvas.h}@{canvas.fps}, "
               f"{canvas.bitrate_kbps} kbps, {canvas.loudness_lufs} LUFS")
    store.commit("apply_export_preset", args, summary)
    return {"summary": summary, "preset": name, "config": p}


def multicam(store: EDLStore, args: dict) -> dict:
    """Multi-cam switcher: take N synced inputs, plan the best take per window,
    and rewrite V1 as the resulting list of cuts.

    Args:
        srcs: list[str] paths to angles (first = sync reference).
        window_s: seconds per evaluation window (default 2).
        total: optional total project length (default = min(durations)).
        replace_v1: if True (default), wipe and rewrite V1; else just return the plan.
    """
    srcs = args.get("srcs") or []
    if len(srcs) < 1:
        raise ValueError("multicam: need at least one source path in `srcs`")
    from ..ai import multicam as mc
    plan = mc.plan_multicam(
        [Path(s) for s in srcs],
        window_s=float(args.get("window_s", 2.0)),
        total=args.get("total"),
    )
    cuts = plan["cuts"]
    if not bool(args.get("replace_v1", True)):
        summary = (f"Planned {len(cuts)} cuts across {len(srcs)} angles "
                   f"({plan['duration']:.1f}s total)")
        return {"summary": summary, "plan": plan}
    # Replace V1
    v1 = ensure_track(store.edl, "v1", "video", z=0)
    v1.clips.clear()
    for c in cuts:
        v1.clips.append(Clip(
            src=str(c["src"]),
            in_=float(c["start_in_src"]),
            out=float(c["end_in_src"]),
            start=float(c["start_on_timeline"]),
        ))
    store.edl.recompute_duration()
    summary = (f"Multi-cam: {len(cuts)} cuts across {len(srcs)} angles "
               f"({plan['duration']:.1f}s)")
    store.commit("multicam", args, summary)
    return {"summary": summary, "plan": plan, "cuts": len(cuts)}


_FFMPEG_HOSTILE_RE = None  # lazy compile


def repair_chunks(store: EDLStore, args: dict) -> dict:
    """Scan the chunk cache for corrupt files (e.g. left over from a killed
    render — present on disk but missing the trailing `moov` atom) and delete
    them so the next render rebuilds them. Idempotent."""
    from ..render.chunks import chunk_is_valid
    cache_dirs = [store.dir / "cache" / "chunks",
                  store.dir / "cache" / "videos",
                  store.dir / "previews"]
    removed: list[str] = []
    scanned = 0
    for cd in cache_dirs:
        if not cd.exists():
            continue
        for p in cd.glob("*.mp4"):
            scanned += 1
            if not chunk_is_valid(p):
                try:
                    p.unlink()
                    removed.append(str(p.relative_to(store.dir)))
                except FileNotFoundError:
                    pass
    summary = f"Scanned {scanned} cached files; removed {len(removed)} corrupt"
    return {"summary": summary, "removed": removed, "scanned": scanned}


def repair_media_paths(store: EDLStore, args: dict) -> dict:
    """Find clip srcs with ffmpeg-hostile chars, copy them to sanitized names,
    and rewrite the EDL references in place. Idempotent.

    Hostile chars: : ' [ ] , ; ` $ ( ) * ? & < > | \\ \" + and spaces.
    Files are copied (not moved) so an in-flight render referencing the old
    path still finishes. The original is left on disk for safety.
    """
    import re
    import shutil as _shutil
    rx = re.compile(r"[^A-Za-z0-9._/-]")
    repaired: list[dict] = []
    for t in store.edl.tracks:
        for c in t.clips:
            src_attr = getattr(c, "src", None)
            if not isinstance(src_attr, str) or not src_attr:
                continue
            stem = Path(src_attr).stem
            suffix = Path(src_attr).suffix.lower()
            # Only the leaf needs rewriting; the directory stays the same so we
            # don't break sibling assets like sidecar .srt's.
            new_stem = rx.sub("_", stem).strip("._") or "clip"
            if new_stem == stem and suffix == Path(src_attr).suffix:
                continue
            new_path = Path(src_attr).parent / f"{new_stem}{suffix}"
            if not Path(src_attr).exists():
                # Stale ref — rewrite anyway so future uploads with same name win.
                c.src = str(new_path)
                repaired.append({"id": c.id, "from": src_attr, "to": str(new_path),
                                 "copied": False, "reason": "source missing"})
                continue
            if not new_path.exists():
                _shutil.copy2(src_attr, new_path)
            c.src = str(new_path)
            repaired.append({"id": c.id, "from": src_attr, "to": str(new_path),
                             "copied": True})
    if repaired:
        summary = f"Repaired {len(repaired)} clip path(s) with hostile chars"
        store.commit("repair_media_paths", args, summary)
    else:
        summary = "No clip paths needed repair"
    return {"summary": summary, "repaired": repaired}


def find_broll(store: EDLStore, args: dict) -> dict:
    """Search a local b-roll folder for clips matching `query`. Returns ranked
    candidates the agent can `add_clip` from. The folder is set per-call via
    `bin` (env var `VAI_BROLL_BIN` is the default)."""
    bin_dir = Path(args.get("bin") or os.environ.get("VAI_BROLL_BIN") or
                   _default_broll_dir())
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("find_broll requires a 'query' argument")
    from ..ai import broll
    hits = broll.search_broll(bin_dir, query,
                              top_k=int(args.get("top_k", 8)),
                              max_duration=args.get("max_duration"))
    summary = (f"{len(hits)} b-roll candidate(s) for {query!r} in {bin_dir}"
               if hits else f"No b-roll matched {query!r} in {bin_dir}")
    return {"summary": summary, "candidates": hits, "bin": str(bin_dir)}


def import_srt_tool(store: EDLStore, args: dict) -> dict:
    """Replace the project transcript with one parsed from an external .srt /
    .vtt / .ass file. Useful when the user has a pre-edited subtitle file or
    a translation they want to caption with."""
    src = Path(args["path"])
    if not src.exists():
        raise ValueError(f"subtitle file not found: {src}")
    from ..ingest.srt_io import import_srt
    transcript = import_srt(src, language=str(args.get("language", "en")))
    # Persist as the project transcript so add_caption_track et al. pick it up.
    transcript_path = store.dir / "transcript.json"
    transcript_path.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
    summary = f"Imported {len(transcript.segments)} segments from {src.name}"
    store.commit("import_srt", args, summary)
    return {"summary": summary, "segments": len(transcript.segments)}


def export_srt_tool(store: EDLStore, args: dict) -> dict:
    """Write the current transcript out as a .srt file. Default destination
    is `<session>/captions.srt`."""
    from ..ingest.srt_io import export_srt
    transcript = _load_transcript(store)
    if transcript is None:
        raise RuntimeError("no transcript on this project")
    dst = Path(args.get("path") or (store.dir / "captions.srt"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(export_srt(transcript), encoding="utf-8")
    summary = f"Exported {len(transcript.segments)} segments → {dst.name}"
    return {"summary": summary, "path": str(dst)}


def export_vtt_tool(store: EDLStore, args: dict) -> dict:
    from ..ingest.srt_io import export_vtt
    transcript = _load_transcript(store)
    if transcript is None:
        raise RuntimeError("no transcript on this project")
    dst = Path(args.get("path") or (store.dir / "captions.vtt"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(export_vtt(transcript), encoding="utf-8")
    summary = f"Exported {len(transcript.segments)} segments → {dst.name}"
    return {"summary": summary, "path": str(dst)}


def export_ass_tool(store: EDLStore, args: dict) -> dict:
    from ..ingest.srt_io import export_ass
    transcript = _load_transcript(store)
    if transcript is None:
        raise RuntimeError("no transcript on this project")
    dst = Path(args.get("path") or (store.dir / "captions.ass"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(export_ass(transcript), encoding="utf-8")
    summary = f"Exported {len(transcript.segments)} segments → {dst.name}"
    return {"summary": summary, "path": str(dst)}


def _load_transcript(store: EDLStore):
    p = store.dir / "transcript.json"
    if not p.exists():
        return None
    from ..ingest.transcribe import Transcript
    return Transcript.model_validate_json(p.read_text(encoding="utf-8"))


def noise_reduce(store: EDLStore, args: dict) -> dict:
    """Spectrally denoise a clip's audio (good for hiss/fans/room tone).
    Replaces the clip's src with the cleaned mp4 (video stream copied)."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("noise_reduce only supports media clips")
    from ..ai import denoise
    if not denoise.available():
        raise RuntimeError("noisereduce not installed (uv add noisereduce soundfile)")
    cache_dir = store.dir / "cache" / "denoise"
    out = denoise.denoise_clip(Path(c.src), cache_dir,
                               strength=float(args.get("strength", 0.85)))
    c.src = str(out)
    summary = f"Denoised {cid} (strength={args.get('strength', 0.85)})"
    store.commit("noise_reduce", args, summary)
    return {"summary": summary, "out": str(out)}


def remove_background(store: EDLStore, args: dict) -> dict:
    """Strip the clip's background via rembg. Replaces the clip's src with the
    matted version. By default flattens onto green so a follow-up chroma_key
    keeps the workflow clean (you can pass bg_color=null for true alpha)."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("remove_background only supports media clips")
    from ..ai import bgremove
    if not bgremove.available():
        raise RuntimeError("rembg not installed (uv add rembg)")
    cache_dir = store.dir / "cache" / "bgremove"
    bg_color = args.get("bg_color", "#00FF00")
    out = bgremove.remove_background(Path(c.src), cache_dir,
                                     model=str(args.get("model", "u2net")),
                                     bg_color=bg_color)
    c.src = str(out)
    # Auto-key the green so the user gets transparent foreground by default.
    if bg_color == "#00FF00":
        from ..edl.schema import ChromaKey
        c.chromakey = ChromaKey(color="#00FF00", similarity=0.45,
                                smoothness=0.08, spill_suppress=0.4)
    summary = f"Removed background on {cid} → {Path(c.src).name}"
    store.commit("remove_background", args, summary)
    return {"summary": summary, "out": str(out)}


def motion_track(store: EDLStore, args: dict) -> dict:
    """Track a bounding box through `clip_id` and convert the path into x/y
    keyframes on `target_id` (a text/sticker overlay).

    Args:
        clip_id: source video clip whose pixels we track.
        target_id: text or sticker clip whose x/y will be overwritten.
        bbox: [x, y, w, h] normalised 0..1 in source frame.
        method: "mil" (default, robust) or "vit" (slower, scale-aware).
        sample_every: emit a keyframe every N frames (default 2 for smoothness).
    """
    cid = str(args["clip_id"])
    tid = str(args["target_id"])
    bbox = args.get("bbox") or [0.4, 0.4, 0.2, 0.2]
    if len(bbox) != 4:
        raise ValueError("bbox must be [x,y,w,h] normalised")
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("motion_track: source clip must be a media clip")

    target_obj = None
    target_kind = None
    target_start = 0.0
    for tr in store.edl.tracks:
        for it in tr.clips:
            if getattr(it, "id", None) == tid:
                target_obj = it
                target_start = float(getattr(it, "start", 0.0))
                target_kind = "text" if it.__class__.__name__ == "TextClip" else \
                              "sticker" if it.__class__.__name__ == "Sticker" else "clip"
                break
    if target_obj is None:
        raise ValueError(f"target {tid} not found")

    from ..ai import tracker
    canvas = store.edl.canvas
    track_data = tracker.track_object(
        Path(c.src), tuple(bbox),  # type: ignore
        canvas_w=canvas.w, canvas_h=canvas.h,
        method=str(args.get("method", "mil")),
        sample_every=int(args.get("sample_every", 2)),
    )
    track_dir = store.dir / "cache" / "tracks"
    track_id = f"trk_{tid}_{int(track_data['fps']*100)}"
    tracker.save_track(track_data, track_dir / f"{track_id}.json")

    # Convert track points to keyframes relative to target_obj.start.
    # Each track entry: [t, cx, cy, w, h] in canvas pixels.
    # Target's transform.x/y is canvas-pixel center.
    from ..edl.schema import Keyframe
    clip_start = float(c.start)
    x_kfs: list[list[float]] = []
    y_kfs: list[list[float]] = []
    end_t = float(getattr(target_obj, "end", target_obj.duration if hasattr(target_obj, "duration") else 9999))
    for t, cx, cy, _w, _h in track_data["track"]:
        # The tracker's t is "seconds from start of source clip". Translate to
        # timeline time, then to "seconds from target_obj.start".
        timeline_t = clip_start + float(t)
        local_t = timeline_t - target_start
        if local_t < 0:
            continue
        if local_t > (end_t - target_start):
            break
        x_kfs.append([round(local_t, 3), round(float(cx), 1)])
        y_kfs.append([round(local_t, 3), round(float(cy), 1)])

    if not x_kfs:
        raise RuntimeError("motion_track: no keyframes inside target's time range")

    target_obj.transform.x = Keyframe(keyframes=x_kfs, interp="linear")
    target_obj.transform.y = Keyframe(keyframes=y_kfs, interp="linear")
    # Only Clip carries track_to (the field is for source-clip tracking refs).
    if target_kind == "clip":
        target_obj.track_to = track_id  # type: ignore
    summary = (f"Motion-tracked {tid} along {len(x_kfs)} keyframes "
               f"({track_data['method']}) on {cid}")
    store.commit("motion_track", args, summary)
    return {"summary": summary, "track_id": track_id, "keyframes": len(x_kfs)}


def stabilize(store: EDLStore, args: dict) -> dict:
    """Two-pass libvidstab stabilization on a clip; replaces clip.src in place."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("stabilize only supports media clips")
    from ..ai.stabilize import stabilize as _stabilize
    new_src = _stabilize(Path(c.src), store.dir / "cache" / "stabilize")
    c.src = str(new_src)
    summary = f"Stabilize {cid} → {new_src.name}"
    store.commit("stabilize", args, summary)
    return {"summary": summary, "new_src": str(new_src)}


def smooth_slow_motion(store: EDLStore, args: dict) -> dict:
    """RIFE frame interpolation for `factor`× smooth slow-mo on a clip."""
    cid = str(args["clip_id"])
    factor = int(args.get("factor", 2))
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("smooth_slow_motion only supports media clips")
    from ..ai.rife import smooth_slow_motion as _slow
    new_src = _slow(Path(c.src), store.dir / "cache" / "rife", factor=factor)
    # The new file plays at original fps with `factor`× more frames → duration
    # becomes original * factor. Update clip out so the EDL reflects that.
    from ..ingest.probe import probe
    p = probe(new_src)
    c.src = str(new_src)
    c.in_ = 0.0
    c.out = p.duration
    summary = f"Smooth slow-mo ×{factor} on {cid} ({p.duration:.1f}s)"
    store.commit("smooth_slow_motion", args, summary)
    return {"summary": summary, "new_src": str(new_src), "duration": p.duration}


def object_erase(store: EDLStore, args: dict) -> dict:
    """LaMa inpaint: erase a bbox region across a time window on a clip."""
    cid = str(args["clip_id"])
    bbox = args["bbox"]  # [x, y, w, h] normalized 0..1
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox must be [x, y, w, h] in 0..1")
    t_start = float(args.get("t_start", 0.0))
    t_end = args.get("t_end")
    if t_end is not None:
        t_end = float(t_end)

    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("object_erase only supports media clips")
    from ..ai.lama import object_erase as _erase
    new_src = _erase(Path(c.src), store.dir / "cache" / "lama",
                     bbox=tuple(float(v) for v in bbox),
                     t_start=t_start, t_end=t_end)
    c.src = str(new_src)
    summary = f"Object erase {cid} bbox={bbox} t={t_start}..{t_end or 'end'}"
    store.commit("object_erase", args, summary)
    return {"summary": summary, "new_src": str(new_src)}


def translate_captions(store: EDLStore, args: dict) -> dict:
    """Translate the existing captions track to a target language via Argos
    Translate (local, no cloud). Replaces each caption clip's text in place.

    Args:
      target_lang: ISO code (e.g. 'hi', 'es', 'fr', 'en'). Default 'hi'.
      source_lang: source language code; defaults to the transcript's
                   detected language if available, else 'en'.
    """
    target = str(args.get("target_lang") or args.get("to") or "hi")
    cap = store.edl.get_track("captions")
    if not cap or not cap.clips:
        return {"summary": "no captions track to translate", "translated": 0}

    source = str(args.get("source_lang") or "")
    if not source:
        # Detect from transcript metadata
        tx = get_transcript(store, {})
        source = (tx.get("language") or "en").lower()
    if source == target:
        return {"summary": f"source and target both '{source}'; nothing to do",
                "translated": 0}

    try:
        from ..ai.translate import translate_text
    except ImportError as e:
        raise RuntimeError(f"argostranslate not installed: {e}")

    n = 0
    for c in cap.clips:
        if not hasattr(c, "text"):
            continue
        try:
            new_text = translate_text(str(c.text), from_code=source, to_code=target)
            c.text = new_text
            n += 1
        except Exception as e:
            # Stop the loop on the first failure so we don't waste time on a
            # bad install / missing language pack.
            raise RuntimeError(f"translate failed at clip {n}: {e}")

    if cap.config:
        cap.config.lang = target  # type: ignore
    summary = f"Translated {n} caption(s) {source}→{target}"
    store.commit("translate_captions", args, summary)
    return {"summary": summary, "translated": n,
            "from": source, "to": target}


def make_shorts(store: EDLStore, args: dict) -> dict:
    """Heuristically pick N highlight ranges from V1 and (optionally) save each
    as a new session containing just that range.

    Args:
      target_count: how many shorts to produce (default 3)
      max_dur: cap each short at this many seconds (default 60)
      min_dur: pad short shots up to at least this long (default 12)
      save_as_sessions: if true, create N new sessions named '<base> short 1..N'
                        and return their ids. Default false → returns ranges only.
    """
    target_count = int(args.get("target_count", 3))
    max_dur = float(args.get("max_dur", 60.0))
    min_dur = float(args.get("min_dur", 12.0))
    save_as_sessions = bool(args.get("save_as_sessions", False))

    v1 = store.edl.get_track("v1")
    src_clip = next((c for c in (v1.clips if v1 else []) if isinstance(c, Clip)), None)
    if not src_clip:
        raise ValueError("no V1 clip to extract shorts from")

    transcript = get_transcript(store, {})
    from ..ai.shorts import make_shorts as _make_shorts
    ranges = _make_shorts(
        Path(src_clip.src), transcript, store.dir / "cache" / "shorts",
        target_count=target_count, max_dur=max_dur, min_dur=min_dur,
    )

    new_sessions: list[str] = []
    if save_as_sessions and ranges:
        from ..storage import new_session_id, session_dir as _sd
        from ..edl.schema import empty_edl, Clip as _Clip
        base_name = (Path(src_clip.src).stem.replace(".normalized", ""))[:40]
        for i, r in enumerate(ranges, start=1):
            sid = new_session_id()
            sd = _sd(sid)
            new_edl = empty_edl()
            v1_new = new_edl.get_track("v1")
            assert v1_new is not None
            v1_new.clips.append(_Clip(
                src=src_clip.src,
                in_=src_clip.in_ + r["start"],
                out=src_clip.in_ + r["end"],
                start=0.0,
            ))
            new_edl.recompute_duration()
            (sd / "edl.json").write_text(new_edl.to_json(), encoding="utf-8")
            (sd / "meta.json").write_text(json.dumps({
                "name": f"{base_name} short {i}", "source": str(src_clip.src),
            }), encoding="utf-8")
            new_sessions.append(sid)

    summary = f"Made {len(ranges)} short(s)" + (
        f", saved as {len(new_sessions)} new session(s)" if new_sessions else ""
    )
    store.commit("make_shorts", args, summary)
    return {"summary": summary, "shorts": ranges, "new_sessions": new_sessions}


def add_keyframe(store: EDLStore, args: dict) -> dict:
    """Add (or update) a keyframe for a clip's transform property.

    `clip_id`  — clip to animate (Clip, TextClip, or Sticker)
    `prop`     — one of x / y / scale / rotation / opacity
    `time`     — clip-local seconds (0 = clip start)
    `value`    — scalar value at that time
    `interp`   — linear (default) | ease-in | ease-out | ease-in-out | step | back-out
    """
    cid = str(args["clip_id"])
    prop = str(args["prop"])
    if prop not in ("x", "y", "scale", "rotation", "opacity"):
        raise ValueError(f"prop must be x|y|scale|rotation|opacity, got {prop!r}")
    t = float(args["time"])
    val = float(args["value"])
    interp = str(args.get("interp", "linear"))

    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not hasattr(c, "transform"):
        raise ValueError(f"clip {cid} has no transform")
    cur = getattr(c.transform, prop)
    # Convert scalar → Keyframe spec; merge into existing list (replace at same time).
    if isinstance(cur, (int, float)):
        kfs = [(0.0, float(cur))]
    elif isinstance(cur, dict):
        kfs = [tuple(p) for p in (cur.get("keyframes") or [])]
        interp = cur.get("interp", interp)
    else:  # Keyframe instance
        kfs = [tuple(p) for p in cur.keyframes]
        interp = cur.interp
    kfs = [p for p in kfs if abs(p[0] - t) > 1e-3]
    kfs.append((t, val))
    kfs.sort(key=lambda p: p[0])
    from ..edl.schema import Keyframe
    setattr(c.transform, prop, Keyframe(keyframes=kfs, interp=interp))
    summary = f"Keyframe {cid}.{prop} @ {t:.2f}s = {val:.3f} (now {len(kfs)} keys)"
    store.commit("add_keyframe", args, summary)
    return {"summary": summary, "keys": len(kfs)}


def remove_keyframe(store: EDLStore, args: dict) -> dict:
    cid = str(args["clip_id"])
    prop = str(args["prop"])
    t = float(args["time"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    cur = getattr(c.transform, prop, None)
    if cur is None or isinstance(cur, (int, float)):
        return {"summary": f"{prop} on {cid} has no keyframes", "keys": 0}
    if isinstance(cur, dict):
        kfs = [tuple(p) for p in (cur.get("keyframes") or [])]
        interp = cur.get("interp", "linear")
    else:
        kfs = [tuple(p) for p in cur.keyframes]
        interp = cur.interp
    kfs = [p for p in kfs if abs(p[0] - t) > 1e-3]
    if not kfs:
        # Collapse back to scalar (use 0 as sensible default)
        setattr(c.transform, prop, 0.0)
    elif len(kfs) == 1:
        setattr(c.transform, prop, kfs[0][1])
    else:
        from ..edl.schema import Keyframe
        setattr(c.transform, prop, Keyframe(keyframes=kfs, interp=interp))
    summary = f"Removed keyframe {cid}.{prop} @ {t:.2f}s ({len(kfs)} left)"
    store.commit("remove_keyframe", args, summary)
    return {"summary": summary, "keys": len(kfs)}


def add_sticker(store: EDLStore, args: dict) -> dict:
    """Add a sticker overlay (PNG file or emoji character) to the stickers track."""
    src_arg = args.get("src")
    emoji_arg = args.get("emoji")
    if not src_arg and not emoji_arg:
        raise ValueError("add_sticker needs either 'src' (PNG path) or 'emoji' (character)")

    canvas = store.edl.canvas
    if emoji_arg and not src_arg:
        from ..ai.emoji import fetch_emoji_png
        png_path = fetch_emoji_png(str(emoji_arg))
        if not png_path:
            raise ValueError(f"could not fetch emoji PNG for {emoji_arg!r}")
        src_arg = str(png_path)
    else:
        # User-supplied PNG path goes through the allowlist guard.
        src_arg = _safe_src(src_arg)

    start = float(args.get("start", 0.0))
    end = float(args.get("end", start + 3.0))
    if end <= start + 0.1:
        # An explicit end that's <= start (or barely past it) collapses to a
        # near-invisible/zero-duration sticker — e.g. StickerPanel.tsx clamps
        # end to edl.duration, so inserting near the very end of the timeline
        # produced a ~0s window with nothing visibly placed (issue 31b). The
        # `args.get("end", start+3.0)` default above only covers a MISSING
        # end, not an explicitly-too-small one, so this is a second, always-
        # applied floor.
        end = start + 3.0
    pos = args.get("position") or [canvas.w / 2, canvas.h / 2]
    scale = float(args.get("scale", 1.0))
    rotation = float(args.get("rotation", 0.0))

    from ..edl.schema import Sticker, Transform, Track
    track = store.edl.get_track("stickers")
    if not track:
        track = Track(id="stickers", type="sticker", z=12, label="Stickers")
        store.edl.tracks.append(track)
    sticker = Sticker(
        src=str(src_arg),
        start=start, end=end,
        transform=Transform(x=float(pos[0]), y=float(pos[1]), scale=scale, rotation=rotation),
        label=str(emoji_arg) if emoji_arg else None,
    )
    track.clips.append(sticker)
    summary = (f"Sticker {sticker.label or Path(src_arg).name} "
               f"@ ({int(pos[0])},{int(pos[1])}) {start:.2f}–{end:.2f}s")
    store.commit("add_sticker", args, summary)
    # Ground truth for "how many stickers are there now" — without this the
    # only way to know is a separate get_timeline call, and an agent that
    # skips it falls back to counting from memory (reports 17 when 3 exist).
    return {"sticker_id": sticker.id, "summary": summary, "src": src_arg,
            "sticker_count": len(track.clips)}


def set_clip_timing(store: EDLStore, args: dict) -> dict:
    """Set start and/or end (seconds) of an OVERLAY clip — a sticker or text.

    Media clips have a computed `end`; they use trim_clip / move_clip instead.
    Drives the Properties panel's Start / Duration fields for stickers.
    """
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    track, c = res
    if isinstance(c, Clip):
        raise ValueError("set_clip_timing is for overlays (sticker/text); "
                         "use trim_clip / move_clip for media clips")
    if args.get("start") is not None:
        c.start = max(0.0, float(args["start"]))
    if args.get("end") is not None:
        c.end = float(args["end"])
    if c.end <= c.start:                      # never allow a zero/negative span
        c.end = c.start + 0.1
    track.clips.sort(key=lambda x: getattr(x, "start", 0))
    summary = f"Timing {cid}: {c.start:.2f}–{c.end:.2f}s"
    store.commit("set_clip_timing", args, summary)
    return {"summary": summary}


def vocal_isolate(store: EDLStore, args: dict) -> dict:
    """Run Demucs to extract a vocals-only WAV from a clip's source. Adds the
    resulting stem as a new clip on the `vo` track and mutes the original V1
    audio so they don't double up."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("vocal_isolate only supports media clips")
    from ..ai.separate import isolate_vocals
    cache_dir = store.dir / "cache" / "demucs"
    out_wav = isolate_vocals(Path(c.src), cache_dir)
    # Add as a clip on the vo track at the clip's start
    track = store.edl.get_track("vo") or ensure_track(store.edl, "vo", "vo", z=0)
    from ..ingest.probe import probe
    p = probe(out_wav)
    from ..edl.schema import Clip as _Clip, AudioProps
    new_clip = _Clip(src=str(out_wav), in_=c.in_, out=c.in_ + p.duration, start=c.start,
                     audio=AudioProps(gain_db=0))
    track.clips.append(new_clip)
    summary = f"Vocal isolate {cid} → vo clip ({p.duration:.1f}s)"
    store.commit("vocal_isolate", args, summary)
    return {"summary": summary, "vo_clip_id": new_clip.id, "src": str(out_wav)}


def instrumental_isolate(store: EDLStore, args: dict) -> dict:
    """Run Demucs to extract an instrumental (no-vocals) WAV from a clip and
    add it on the music track."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("instrumental_isolate only supports media clips")
    from ..ai.separate import isolate_instrumental
    cache_dir = store.dir / "cache" / "demucs"
    out_wav = isolate_instrumental(Path(c.src), cache_dir)
    track = store.edl.get_track("music") or ensure_track(store.edl, "music", "music", z=0)
    from ..ingest.probe import probe
    p = probe(out_wav)
    from ..edl.schema import Clip as _Clip, AudioProps
    new_clip = _Clip(src=str(out_wav), in_=c.in_, out=c.in_ + p.duration, start=c.start,
                     audio=AudioProps(gain_db=-6))
    track.clips.append(new_clip)
    summary = f"Instrumental isolate {cid} → music clip ({p.duration:.1f}s)"
    store.commit("instrumental_isolate", args, summary)
    return {"summary": summary, "music_clip_id": new_clip.id, "src": str(out_wav)}


def add_lower_third(store: EDLStore, args: dict) -> dict:
    """Drop a name + handle lower-third graphic. Speaker tag is informational
    until M5 brings real diarization."""
    name = str(args["name"])
    handle = str(args.get("handle", ""))
    start = float(args["start"])
    end = float(args.get("end", start + 4.0))
    speaker = args.get("speaker")
    track = ensure_track(store.edl, "tx_lt", "text", z=12)
    text = name
    if handle:
        text = f"{name}\n{handle}"
    canvas = store.edl.canvas
    clip = TextClip(
        text=text, start=start, end=end, role="lower_third",
        speaker=speaker,
        transform=Transform(x=canvas.w / 2, y=canvas.h * 0.80),
    )
    track.clips.append(clip)
    summary = f"Lower-third {name}{f' ({handle})' if handle else ''} {start:.1f}–{end:.1f}s"
    store.commit("add_lower_third", args, summary)
    return {"clip_id": clip.id, "summary": summary}


def apply_template(store: EDLStore, args: dict) -> dict:
    name = str(args["name"])
    inputs = dict(args.get("inputs") or {})
    info = _apply_template(store.edl, name, inputs=inputs)
    # Hook stack on every template by default — viewers decide in 3s, so
    # seeded EDLs should never ship without all three axes (visual + text +
    # audio) wired up. Caller can opt out with `with_hook_stack=False` when
    # they want to compose the hook themselves.
    if args.get("with_hook_stack", True):
        try:
            hook_text = inputs.get("hook") or inputs.get("text")
            hook_info = apply_hook_stack(store, {"text": hook_text} if hook_text else {})
            info.setdefault("applied", []).append(
                f"hook_stack({hook_info.get('axes', {})})"
            )
        except Exception as e:
            info.setdefault("applied", []).append(f"hook_stack:skipped({e})")
    summary = f"Apply template '{name}': {', '.join(info['applied'])}"
    store.commit("apply_template", args, summary)
    return {"summary": summary, **info}


def list_templates(store: EDLStore, args: dict) -> dict:
    return {"templates": list_template_names(), "shows": _list_shows()}


def set_property(store: EDLStore, args: dict) -> dict:
    """Generic dotted-path mutator. The most flexible CapCut-style 'tweak any
    field' operation: `set_property(clip_id, path, value)`.

    Supported `path` examples:
        transform.x, transform.y, transform.scale, transform.rotation,
        transform.opacity, audio.gain_db, audio.fade_in, audio.fade_out,
        audio.mute, speed, reverse, src, in, out, start
    """
    cid = str(args["clip_id"])
    path = str(args["path"])
    value = args["value"]
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    parts = path.split(".")
    obj = c
    for p in parts[:-1]:
        if p == "in":
            p = "in_"  # pydantic alias
        obj = getattr(obj, p, None)
        if obj is None:
            raise ValueError(f"path {path!r} does not resolve on {cid}")
    leaf = parts[-1]
    if leaf == "in":
        leaf = "in_"
    if not hasattr(obj, leaf):
        raise ValueError(f"unknown attr {leaf!r} on {type(obj).__name__}")
    setattr(obj, leaf, value)
    summary = f"Set {cid}.{path} = {value!r}"
    store.commit("set_property", args, summary)
    return {"summary": summary}


def add_text(store: EDLStore, args: dict) -> dict:
    """Full-control text overlay (vs add_super_text which uses canonical defaults).

    Args:
        text: string
        start, end: timeline seconds
        role: one of ROLE_STYLES (super, hook, lower_third, caption, label, watermark, default)
        x, y: canvas pixels (centre)
        scale, rotation, opacity: optional transforms
        anim_in, anim_out: pop, fade, slide_up, slide_down, etc.
    """
    from ..edl.schema import TextClip, Transform, TextStyle
    text = str(args["text"])
    role = args.get("role", "default")
    canvas = store.edl.canvas
    tx = Transform(
        x=float(args.get("x", canvas.w / 2)),
        y=float(args.get("y", canvas.h * 0.85)),
        scale=float(args.get("scale", 1.0)),
        rotation=float(args.get("rotation", 0.0)),
        opacity=float(args.get("opacity", 1.0)),
    )
    style = TextStyle(
        font=str(args.get("font", "Inter-Black")),
        size=float(args.get("size", 96)),
        color=str(args.get("color", "#FFFFFF")),
        stroke=str(args.get("stroke", "#000000")),
        stroke_w=float(args.get("stroke_w", 4)),
    )
    tc = TextClip(text=text, start=float(args["start"]), end=float(args["end"]),
                  role=role, transform=tx, style=style,
                  anim_in=args.get("anim_in"), anim_out=args.get("anim_out"))
    track = ensure_track(store.edl, "text", "text", z=10)
    # Idempotency: an identical re-run (same text/role/start/end on this track)
    # must not stack a second overlay — mirrors add_super_text's guard.
    def _same(existing) -> bool:
        return (
            isinstance(existing, TextClip)
            and getattr(existing, "text", None) == tc.text
            and getattr(existing, "role", None) == tc.role
            and abs(existing.start - tc.start) < 1e-6
            and abs(existing.end - tc.end) < 1e-6
        )
    if any(_same(c) for c in track.clips):
        summary = f"Text already present: {tc.text!r} @ {tc.start:.2f}s"
        return {"summary": summary}  # no commit — nothing changed
    # Optional explicit replace: drop prior same-role overlapping clips.
    if bool(args.get("replace", False)):
        track.clips = [
            c for c in track.clips
            if not (isinstance(c, TextClip) and getattr(c, "role", None) == tc.role
                    and c.start < tc.end and c.end > tc.start)
        ]
    track.clips.append(tc)
    summary = f"Added text {role!r} {text!r} ({tc.start:.2f}–{tc.end:.2f}s)"
    store.commit("add_text", args, summary)
    return {"summary": summary, "id": tc.id}


def apply_text_template(store: EDLStore, args: dict) -> dict:
    """Render a text overlay from a named preset bundle.

    Built-ins: hashtag_chunky, callout_arrow, big_question, end_card_handle,
    countdown_3_2_1, watermark_handle. Each fills the {handle}/{hashtag}/{text}
    slot from `fields`.
    """
    name = str(args.get("name", "")).lower()
    fields = args.get("fields") or {}
    text_default = str(fields.get("text", ""))
    handle = str(fields.get("handle", ""))
    hashtag = str(fields.get("hashtag", ""))
    canvas = store.edl.canvas
    start = float(args.get("start", 0.0))
    end = float(args.get("end", start + 3.0))

    presets: dict[str, dict] = {
        "hashtag_chunky": {"text": f"#{hashtag.lstrip('#')}" if hashtag else text_default,
                           "role": "caption",
                           "x": canvas.w/2, "y": canvas.h*0.86},
        "callout_arrow":  {"text": text_default or "→",
                           "role": "label",
                           "x": canvas.w*0.6, "y": canvas.h*0.4, "size": 96},
        "big_question":   {"text": text_default,
                           "role": "hook",
                           "x": canvas.w/2, "y": canvas.h*0.18},
        "end_card_handle":{"text": handle or text_default,
                           "role": "lower_third",
                           "x": canvas.w/2, "y": canvas.h*0.92, "size": 80},
        "countdown_3_2_1":{"text": "3 · 2 · 1",
                           "role": "hook",
                           "x": canvas.w/2, "y": canvas.h/2, "size": 220,
                           "anim_in": "pop", "anim_out": "fade"},
        "watermark_handle":{"text": handle or text_default,
                            "role": "watermark",
                            "x": canvas.w*0.85, "y": canvas.h*0.05},
    }
    if name not in presets:
        raise ValueError(f"unknown text template: {name}. options: {sorted(presets)}")
    p = presets[name]
    sub = {"start": start, "end": end, **p}
    return add_text(store, sub)


def record_voiceover(store: EDLStore, args: dict) -> dict:
    """Register a recorded voiceover clip on the `vo` track. The actual mic
    capture happens in the browser via MediaRecorder; this tool ingests the
    saved file path returned from the /vo_record endpoint.
    """
    src = Path(_safe_src(args["src"]))
    if not src.exists():
        raise ValueError(f"voiceover file not found: {src}")
    start = float(args.get("start", 0.0))
    duration = float(args.get("duration") or _probe_audio_duration(src))
    track = ensure_track(store.edl, "vo", "vo", z=20)
    c = Clip(src=str(src), in_=0.0, out=duration, start=start)
    track.clips.append(c)
    store.edl.recompute_duration()
    summary = f"Voiceover ({duration:.1f}s) added at {start:.2f}s"
    store.commit("record_voiceover", args, summary)
    return {"summary": summary, "id": c.id}


def _probe_audio_duration(p: Path) -> float:
    import subprocess as sp
    try:
        out = sp.run([_pu.FFPROBE, "-v", "error", "-show_entries",
                      "format=duration", "-of",
                      "default=nokey=1:noprint_wrappers=1", str(p)],
                     capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def list_filters(store: EDLStore, args: dict) -> dict:
    """All effect types `add_effect` understands."""
    from ..render.effects import EFFECT_BUILDERS
    return {"filters": sorted(EFFECT_BUILDERS.keys())}


def list_transitions(store: EDLStore, args: dict) -> dict:
    """The full transition catalog — categories, aliases, descriptions, count.

    `transitions` is the flat list (kept for backward compat with callers that
    just want names); `catalog` is the structured grouping for UI/chat.
    """
    from ..render.transitions import catalog, all_names
    cat = catalog()
    return {"transitions": all_names(), "catalog": cat, "count": cat["count"]}


def list_text_styles(store: EDLStore, args: dict) -> dict:
    """Built-in text/role presets + any user JSONs in presets/text_styles/."""
    from ..render.text_overlay import ROLE_STYLES
    from ..config import PRESETS_DIR
    extra: list[str] = []
    presets_dir = PRESETS_DIR / "text_styles"
    if presets_dir.exists():
        extra = sorted(p.stem for p in presets_dir.glob("*.json"))
    return {"roles": sorted(ROLE_STYLES.keys()), "presets": extra}


def list_shows(store: EDLStore, args: dict) -> dict:
    return {"shows": _list_shows()}


def list_luts(store: EDLStore, args: dict) -> dict:
    """All .cube LUTs in presets/luts/."""
    from ..config import PRESETS_DIR
    luts_dir = PRESETS_DIR / "luts"
    if not luts_dir.exists():
        return {"luts": []}
    return {"luts": sorted(p.name for p in luts_dir.glob("*.cube"))}


def save_show_template(store: EDLStore, args: dict) -> dict:
    name = str(args["name"])
    p = _save_show(name, store.edl)
    summary = f"Saved show template '{name}'"
    store.commit("save_show_template", args, summary)
    return {"summary": summary, "path": str(p)}


def apply_show_template(store: EDLStore, args: dict) -> dict:
    name = str(args["name"])
    snap = _load_show(name)
    applied = ShowSnapshot.apply_to_edl(store.edl, snap)
    summary = f"Apply show '{name}': {', '.join(applied) or '(empty snapshot)'}"
    store.commit("apply_show_template", args, summary)
    return {"summary": summary, "applied": applied}


def generate_hook(store: EDLStore, args: dict) -> dict:
    """Draft 3 candidate hook lines from the project's transcript via Claude.

    Pure suggestion — does NOT mutate the EDL. Caller (or Claude itself) picks
    one and calls add_hook_overlay.
    """
    from ..config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    if not ANTHROPIC_API_KEY:
        # Heuristic fallback: pull the first sentence of the transcript.
        tx = get_transcript(store, {})
        first = ""
        for seg in (tx.get("segments") or []):
            first = (seg.get("text") or "").strip()
            if first:
                break
        if not first:
            first = "WAIT FOR IT"
        candidates = [
            first[:60].upper(),
            "DON'T SCROLL — WATCH THIS",
            "EVERYONE'S MISSING THIS",
        ]
        return {"candidates": candidates, "source": "heuristic (no API key)"}

    from anthropic import Anthropic
    tx = get_transcript(store, {})
    text = " ".join((s.get("text") or "").strip() for s in (tx.get("segments") or []))[:3000]
    summary = get_timeline(store, {"summary": True})
    prompt = (
        f"You're scripting hooks for a short-form vertical video. Goal: stop the scroll in the first 3 seconds. "
        f"Read the transcript and propose 3 hook lines, each ≤7 words, in ALL CAPS where appropriate, that create a "
        f"curiosity gap. Avoid generic 'wait for it' filler. Reply ONLY as a JSON array of 3 strings.\n\n"
        f"Project: canvas {summary['canvas']['w']}×{summary['canvas']['h']}, duration {summary['duration']:.1f}s.\n"
        f"Transcript:\n{text or '(no transcript yet)'}"
    )
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    out_text = "".join(b.text for b in resp.content if b.type == "text").strip()
    import json as _json, re
    raw: list = []
    try:
        raw = _json.loads(out_text)
    except Exception:
        # Try to pull a JSON array out of code-block formatting
        m = re.search(r"\[[^\]]+\]", out_text, re.S)
        if m:
            try:
                raw = _json.loads(m.group(0))
            except Exception:
                raw = [out_text]
        else:
            raw = [out_text]
    # Trust-boundary guard: the LLM can return null, dicts, numbers, anything.
    # Coerce to non-empty stripped strings so downstream UI never gets null/dict
    # in a "list of hook strings" field.
    candidates: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    candidates.append(s)
            elif isinstance(item, (int, float, bool)):
                candidates.append(str(item))
            # Drop dicts, None, nested lists silently — they're never valid hooks.
    elif isinstance(raw, str) and raw.strip():
        candidates = [raw.strip()]
    return {"candidates": candidates[:3], "source": "claude"}


def auto_reframe(store: EDLStore, args: dict) -> dict:
    """Switch canvas to a target aspect with subject-tracked cropping per V1 clip.

    Pass `subject_track=False` to use a plain center-crop (faster, no MediaPipe).
    """
    ratio_arg = args.get("ratio") or args.get("aspect") or "9:16"
    ratios = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
    if ratio_arg not in ratios:
        raise ValueError(f"unsupported ratio {ratio_arg!r}; supported: {list(ratios)}")
    w, h = ratios[ratio_arg]
    old_w, old_h = store.edl.canvas.w, store.edl.canvas.h
    store.edl.canvas.w = w
    store.edl.canvas.h = h
    _rescale_overlays_for_canvas_change(store.edl, old_w, old_h, w, h)

    subject_track = bool(args.get("subject_track", True))
    reframed: list[str] = []
    if subject_track:
        from ..ai.reframe import reframe_clip
        v1 = store.edl.get_track("v1")
        for c in (v1.clips if v1 else []):
            if not isinstance(c, Clip):
                continue
            try:
                new_src = reframe_clip(Path(c.src), store.dir / "cache", target_w=w, target_h=h)
                # Preserve in/out as fractions of duration: the reframed file has
                # the same duration as the source so in/out remain valid.
                c.src = str(new_src)
                reframed.append(c.id)
            except Exception as e:
                # Don't bring the project down — leave that clip unmodified.
                reframed.append(f"{c.id}(skipped: {type(e).__name__})")

    summary = f"Auto-reframe → {ratio_arg} ({w}×{h}); subject-track {'on' if subject_track else 'off'}; reframed {len(reframed)} clip(s)"
    store.commit("auto_reframe", args, summary)
    return {"summary": summary, "w": w, "h": h, "reframed": reframed}


def vocal_isolate(store: EDLStore, args: dict) -> dict:
    """Replace a clip's audio with the vocal stem (Demucs)."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("vocal_isolate only supports media clips")
    from ..ai.separate import isolate_vocals
    stem = isolate_vocals(Path(c.src), store.dir / "cache" / "stems")
    # Drop the vocal stem onto the vo track; mute the original audio of the clip.
    c.audio.mute = True
    track = ensure_track(store.edl, "vo", "vo", z=0)
    from ..edl.schema import AudioProps
    track.clips.append(Clip(
        src=str(stem), in_=0.0, out=c.duration, start=c.start,
        audio=AudioProps(gain_db=0.0, fade_in=0.05, fade_out=0.1),
    ))
    summary = f"Isolate vocals from {cid} → vo track ({stem.name})"
    store.commit("vocal_isolate", args, summary)
    return {"summary": summary, "stem": str(stem)}


def instrumental_isolate(store: EDLStore, args: dict) -> dict:
    """Replace a clip's audio with the instrumental stem (no vocals)."""
    cid = str(args["clip_id"])
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("instrumental_isolate only supports media clips")
    from ..ai.separate import isolate_instrumental
    inst = isolate_instrumental(Path(c.src), store.dir / "cache" / "stems")
    c.audio.mute = True
    track = ensure_track(store.edl, "music", "music", z=0)
    from ..edl.schema import AudioProps
    track.clips.append(Clip(
        src=str(inst), in_=0.0, out=c.duration, start=c.start,
        audio=AudioProps(gain_db=-6.0, fade_in=0.05, fade_out=0.1),
    ))
    summary = f"Isolate instrumental from {cid} → music track"
    store.commit("instrumental_isolate", args, summary)
    return {"summary": summary, "stem": str(inst)}


def upscale(store: EDLStore, args: dict) -> dict:
    """Real-ESRGAN upscale a clip; replaces its src with the upscaled file."""
    cid = str(args["clip_id"])
    factor = int(args.get("factor", 2))
    res = store.edl.get_clip(cid)
    if not res:
        raise ValueError(f"clip {cid} not found")
    _, c = res
    if not isinstance(c, Clip):
        raise ValueError("upscale only supports media clips")
    from ..ai.upscale import upscale_clip, available
    if not available():
        raise RuntimeError("Real-ESRGAN binary missing; expected at models/realesrgan/")
    new_src = upscale_clip(Path(c.src), store.dir / "cache" / "upscale", factor=factor)
    c.src = str(new_src)
    summary = f"Upscale {cid} ×{factor} via Real-ESRGAN"
    store.commit("upscale", args, summary)
    return {"summary": summary, "new_src": str(new_src)}


# ---------- registry ----------

DISPATCH: dict[str, DispatchFn] = {
    # inspection
    "get_timeline": get_timeline,
    "get_clip": get_clip,
    "get_transcript": get_transcript,
    # edits
    "add_clip": add_clip,
    "cut_range": cut_range,
    "split_at": split_at,
    "trim_clip": trim_clip,
    "move_clip": move_clip,
    "reorder_clips": reorder_clips,
    "ripple_delete": ripple_delete,
    "duplicate_clip": duplicate_clip,
    # project
    "set_canvas": set_canvas,
    "set_aspect_ratio": set_aspect_ratio,
    "undo": undo_op,
    "redo": redo_op,
    "render_preview": render_preview_tool,
    "set_track_muted": set_track_muted,
    # text / brand / audit
    "add_super_text": add_super_text,
    "add_hook_overlay": add_hook_overlay,
    "apply_hook_stack": apply_hook_stack,
    "add_caption_track": add_caption_track,
    "auto_caption": auto_caption,
    "apply_brand_kit": apply_brand_kit,
    "audit_aesthetic": audit_aesthetic,
    # M3: audio + auto-trim + reframe
    "add_music": add_music,
    "set_duck": set_duck,
    "set_volume": set_volume,
    "add_fade": add_fade,
    "remove_silences": remove_silences,
    "remove_fillers": remove_fillers,
    "auto_cut_to_beats": auto_cut_to_beats,
    "auto_reframe": auto_reframe,
    # M3.5: templates, shows, lower-thirds, hook generator
    "add_lower_third": add_lower_third,
    "apply_template": apply_template,
    "list_templates": list_templates,
    "list_filters": list_filters,
    "list_transitions": list_transitions,
    "list_text_styles": list_text_styles,
    "list_shows": list_shows,
    "list_luts": list_luts,
    "set_property": set_property,
    "add_text": add_text,
    "apply_text_template": apply_text_template,
    "record_voiceover": record_voiceover,
    "save_show_template": save_show_template,
    "apply_show_template": apply_show_template,
    "generate_hook": generate_hook,
    "set_track_muted": set_track_muted,
    "set_track_locked": set_track_locked,
    "set_speed": set_speed,
    "set_clip_transform": set_clip_transform,
    "set_clip_timing": set_clip_timing,
    "bulk_delete": bulk_delete,
    "bulk_duplicate": bulk_duplicate,
    "add_marker": add_marker,
    "remove_marker": remove_marker,
    "add_sticker": add_sticker,
    "vocal_isolate": vocal_isolate,
    "instrumental_isolate": instrumental_isolate,
    # M4: effects, transitions, masks
    "add_effect": add_effect,
    "remove_effect": remove_effect,
    "color_grade": color_grade,
    "apply_lut": apply_lut,
    "add_transition": add_transition,
    "add_mask": add_mask,
    "chroma_key": chroma_key,
    # M5: vision-driven find + style match
    "find_moments": find_moments,
    "search_media": search_media,
    "match_style": match_style,
    # TTS voiceover (Piper)
    "tts_voiceover": tts_voiceover,
    # Heavy AI: vocal/instrumental separation, upscale
    "vocal_isolate": vocal_isolate,
    "instrumental_isolate": instrumental_isolate,
    "upscale": upscale,
    # Keyframes
    "add_keyframe": add_keyframe,
    "remove_keyframe": remove_keyframe,
    "make_shorts": make_shorts,
    "translate_captions": translate_captions,
    "remove_background": remove_background,
    "noise_reduce": noise_reduce,
    "find_broll": find_broll,
    "repair_media_paths": repair_media_paths,
    "repair_chunks": repair_chunks,
    "multicam": multicam,
    "set_loudness_target": set_loudness_target,
    "apply_export_preset": apply_export_preset,
    "import_srt": import_srt_tool,
    "export_srt": export_srt_tool,
    "export_vtt": export_vtt_tool,
    "export_ass": export_ass_tool,
    "motion_track": motion_track,
    "stabilize": stabilize,
    "smooth_slow_motion": smooth_slow_motion,
    "object_erase": object_erase,
    # M3 deferred: pyannote diarization (gated by HF token)
    "diarize": diarize,
    "pyannote_status": pyannote_status,
    "name_speakers": name_speakers,
}


def dispatch(store: EDLStore, tool: str, args: dict) -> dict:
    fn = DISPATCH.get(tool)
    if not fn:
        raise KeyError(f"unknown tool: {tool}")
    return fn(store, args)


def list_tools(categories: list[str] | None = None):
    return _list_tools(categories)
