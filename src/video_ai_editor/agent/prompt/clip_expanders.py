"""The CapCut clip edits a key-free prompt names by clip (wave D3, lane E3).

"delete the second clip", "duplicate this clip", "move the last clip to the
start", "zoom in on the second clip", "rotate the first clip 90 degrees",
"make it brighter", "slow down the last 3 seconds". Before this module the
recipes brain read "speed up the second clip 2x" as EVERY clip at 2x and
"zoom in on the last clip" as crossfades on every seam — both committed, both
wrong (the E3 key-free sweep, `tests/test_prompt_capcut_sweep.py`).

Two rules keep a wrong edit from ever being committed:

  * A clip is BOUND to one real id (or a live sentinel the executor resolves)
    at plan time, from `TimelineFacts` (`bind_clip`). The grammar's internal
    references — `$v1_nth:2`, `$v1_at:5` — never reach `validate_plan`.
  * When the prompt does not say which clip (and nothing is selected), or
    names one that is not there, the expansion is a QUESTION in `notes` and
    has no steps: the plan is read-only and the reply asks.

Pure functions `(Intent, TimelineFacts, Context) -> Expansion`, like every
expander in `expanders.py` (which registers these in `EXPANDERS`).
"""
from __future__ import annotations

from typing import Any

from .facts import TimelineFacts
from .grammar import AT_REF, NTH_REF
from .recipes import Context, Expansion, Intent, pc, step
from .schema import STAGE_CUTS, STAGE_LOOK, Step

#: Seam / edge tolerance, the same half-frame-ish slack `_check_refs` uses.
_TOL_S = 0.05
#: Ken Burns: a slow push from 100 % to 115 % (or back) over the whole clip.
KEN_BURNS_SCALE = 1.15
#: A punch-in with no level named: CapCut's usual "zoom to 120 %".
PUNCH_IN_SCALE = 1.2
ZOOM_SCALE_RANGE = (0.2, 5.0)
#: At most this many clips get their own Ken Burns steps (two steps each).
MAX_KEN_BURNS_CLIPS = 10
#: The Adjust sliders a relative request moves to (eq= values, render/effects._color).
ADJUST_UP = {"brightness": 0.1, "contrast": 1.2, "saturation": 1.3}
ADJUST_DOWN = {"brightness": -0.1, "contrast": 0.85, "saturation": 0.7}


# --------------------------------------------------------------------------
# 1. Which clip
# --------------------------------------------------------------------------

def v1_spans(f: TimelineFacts) -> list[tuple[str, float, float]] | None:
    """(id, start, end) of every v1 clip in timeline order, from the seams —
    None when v1 has a gap (the seams then do not partition the timeline)."""
    ids = list(f.v1_clip_ids)
    if not ids:
        return None
    bounds = [0.0, *f.v1_boundaries, float(f.duration)]
    if len(bounds) != len(ids) + 1:
        return None
    return [(cid, round(bounds[i], 3), round(bounds[i + 1], 3)) for i, cid in enumerate(ids)]


def _span_of(ref: str, f: TimelineFacts) -> tuple[float, float] | None:
    spans = v1_spans(f) or []
    if ref == "$v1_first" and spans:
        return spans[0][1], spans[0][2]
    if ref == "$v1_last" and spans:
        return spans[-1][1], spans[-1][2]
    return next(((a, b) for cid, a, b in spans if cid == ref), None)


def _count(n: int) -> str:
    return "there is only one clip on the main track" if n == 1 else f"there are only {n} clips on the main track"


def bind_clip(ref: Any, f: TimelineFacts) -> tuple[str | None, str | None]:
    """(a clip id or a live sentinel, None) — or (None, the question to ask)."""
    if ref is None:
        return None, None
    ref = str(ref)
    ids = list(f.v1_clip_ids)
    if ref == "$selected":
        if f.selection and f.selection in set(f.clip_ids) | set(ids):
            return f.selection, None
        return None, "Which clip? Nothing is selected — select it on the timeline, or name it like 'the second clip'."
    if ref == f"{NTH_REF}mid":
        if len(ids) % 2 == 1:
            return ids[len(ids) // 2], None
        return None, (f"Which clip is the middle one? There are {len(ids)} clips — name it like "
                      f"'clip {len(ids) // 2}' or 'clip {len(ids) // 2 + 1}'.")
    if ref.startswith(NTH_REF):
        n = int(float(ref[len(NTH_REF):]))
        idx = n - 1 if n > 0 else len(ids) + n
        if 0 <= idx < len(ids):
            return ids[idx], None
        return None, f"Which clip did you mean? {_count(len(ids)).capitalize()}."
    if ref.startswith(AT_REF):
        t = float(ref[len(AT_REF):])
        hit = next((cid for cid, a, b in (v1_spans(f) or []) if a - _TOL_S <= t < b), None)
        if hit:
            return hit, None
        return None, f"Which clip? There is no clip on the main track at {t:g}s (the video is {f.duration:.1f}s)."
    if ref in ("$v1_first", "$v1_last", "$v1_all") and not ids:
        return None, "There is no clip on the main track yet — add one first."
    if ref == "$playhead" and f.playhead is None:
        return None, "Which clip? The playhead position is not known — name it like 'the second clip'."
    return ref, None


def one_clip(it: Intent, f: TimelineFacts, verb: str) -> tuple[str | None, str | None]:
    """The ONE clip an edit acts on: the named one, else the selection, else
    a question. `$v1_all` is returned as is (the caller decides)."""
    ref = it.get("clip_ref")
    if ref is None:
        if f.selection and f.selection in set(f.clip_ids) | set(f.v1_clip_ids):
            return f.selection, None
        return None, (f"Which clip should I {verb}? Name it like 'the second clip' or 'the last clip', "
                      f"or select it and say 'this clip'.")
    return bind_clip(ref, f)


def _label(ref: str, f: TimelineFacts) -> str:
    ids = list(f.v1_clip_ids)
    if ref == "$v1_first" or (ids and ref == ids[0]):
        return "the first clip"
    if ref == "$v1_last" or (ids and ref == ids[-1]):
        return "the last clip"
    if ref in ids:
        return f"clip {ids.index(ref) + 1}"
    if ref == "$v1_all":
        return "every clip"
    return f"clip {ref}"


def _ask(question: str) -> Expansion:
    return Expansion(notes=(question,))


def other_cuts(ctx: Context, me: str) -> bool:
    """True when ANOTHER recipe in the plan re-times v1 (so plan-time clip
    lengths and ids are stale); `ctx.has_cut_steps` counts this recipe too."""
    from .planner import CUT_RECIPES
    return bool((ctx.recipes - {me}) & CUT_RECIPES)


# --------------------------------------------------------------------------
# 2. Delete / duplicate / move
# --------------------------------------------------------------------------

def x_delete_clip(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut Delete on the main track: the clip goes and the gap closes
    (`ripple_delete`). The check is the timeline's new length."""
    ref, q = one_clip(it, f, "delete")
    if q:
        return _ask(q)
    if ref == "$v1_all":
        return _ask("Delete every clip? That empties the timeline — select the clips and press Delete, "
                    "or name one clip like 'the second clip'.")
    if len(f.v1_clip_ids) == 1 and ref in (*f.v1_clip_ids, "$v1_first", "$v1_last"):
        # "remove the first part" on a one-clip video is a trim, not an empty timeline
        return _ask("That is the only clip — which part should I cut? Say like 'the first 5 seconds', "
                    "or select the clip and press Delete to empty the timeline.")
    span = _span_of(ref, f)
    pcs = [pc("tool_ok", "the clip was deleted", tool="ripple_delete")]
    if span and not other_cuts(ctx, "delete_clip"):
        pcs.insert(0, pc("duration_between", "the timeline closed the gap",
                         target=round(max(0.0, f.duration - (span[1] - span[0])), 3), tol=0.1))
    what = _label(ref, f)
    return Expansion(steps=(step("ripple_delete", STAGE_CUTS, f"delete {what} and close the gap", clip_id=ref),),
                     postconditions=tuple(pcs),
                     notes=(f"deleted {what}" + (f" ({span[1] - span[0]:.1f}s)" if span else "") + " and closed the gap",))


def x_duplicate(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    ref, q = one_clip(it, f, "duplicate")
    if q:
        return _ask(q)
    if ref == "$v1_all":
        return _ask("Duplicate every clip? Name one clip, like 'duplicate the second clip'.")
    span = _span_of(ref, f)
    pcs = [pc("tool_ok", "the clip was duplicated", tool="duplicate_clip")]
    if span and not other_cuts(ctx, "duplicate"):
        pcs.insert(0, pc("duration_between", "the copy sits right after the original",
                         target=round(f.duration + (span[1] - span[0]), 3), tol=0.1))
    what = _label(ref, f)
    return Expansion(steps=(step("duplicate_clip", STAGE_CUTS, f"duplicate {what} right after itself", clip_id=ref),),
                     postconditions=tuple(pcs), notes=(f"duplicated {what}",))


def _concrete(ref: Any, f: TimelineFacts) -> tuple[str | None, str | None]:
    """A v1 clip id (a reorder names ids, not sentinels)."""
    cid, q = bind_clip(ref, f)
    if q or cid is None:
        return None, q
    ids = list(f.v1_clip_ids)
    cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
    if cid not in ids:
        return None, "Which clip on the main track? Only main-track clips can be reordered."
    return cid, None


def x_move_clip(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """Reorder the main track (`reorder_clips`, which closes the gaps): move
    one clip to the start / end / before or after another, swap two, or
    reverse the order."""
    ids = list(f.v1_clip_ids)
    if len(ids) < 2:
        return _ask("There is only one clip on the main track — nothing to reorder.")
    if other_cuts(ctx, "move_clip"):
        return _ask("Reorder in its own prompt: the cuts in this one change which clip is which.")
    to = it.get("to") or "end"
    order = list(ids)
    if to == "reverse":
        order.reverse()
    else:
        cid, q = (_concrete(it.get("clip_ref"), f) if it.get("clip_ref") is not None
                  else ((f.selection, None) if f.selection in ids else (None, None)))
        if q:
            return _ask(q)
        if cid is None:
            return _ask("Which clip should I move? Name it like 'the second clip', or select it and say 'this clip'.")
        if to in ("before", "after", "swap"):
            other, q2 = _concrete(it.get("anchor"), f) if it.get("anchor") is not None else (None, None)
            if q2:
                return _ask(q2)
            if other is None or other == cid:
                return _ask("Move it next to which clip? Say like 'after the third clip'.")
            if to == "swap":
                i, j = order.index(cid), order.index(other)
                order[i], order[j] = order[j], order[i]
            else:
                order.remove(cid)
                k = order.index(other)
                order.insert(k if to == "before" else k + 1, cid)
        else:
            order.remove(cid)
            order.insert(0 if to == "start" else len(order), cid)
    if order == ids:
        return Expansion(notes=("the clips are already in that order",))
    human = ", ".join(str(ids.index(c) + 1) for c in order)
    return Expansion(steps=(step("reorder_clips", STAGE_CUTS, f"new clip order {human}", track="v1", order=order),),
                     postconditions=(pc("tool_ok", "the clips were reordered", tool="reorder_clips"),),
                     notes=(f"clip order is now {human}",))


# --------------------------------------------------------------------------
# 3. Zoom / rotate / adjust
# --------------------------------------------------------------------------

def _zoom_targets(it: Intent, f: TimelineFacts) -> tuple[list[str], str | None]:
    ref, q = one_clip(it, f, "zoom")
    if q:
        return [], q
    if ref == "$v1_all":
        return list(f.v1_clip_ids), None
    ids = list(f.v1_clip_ids)
    ref = {"$v1_first": ids[0] if ids else ref, "$v1_last": ids[-1] if ids else ref}.get(ref, ref)
    return [ref], None


def x_zoom(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """A slow push in / out over the whole clip (Ken Burns: two scale
    keyframes, clip start → clip end, on the timeline-local clock the render
    and the UI share since RENDER_BEHAVIOR_VERSION 18) or a fixed zoom level
    (`set_clip_transform(scale)`)."""
    targets, q = _zoom_targets(it, f)
    if q:
        return _ask(q)
    direction = it.get("direction") or "in"
    scale = it.get("scale")
    static = it.get("style") == "static" or (scale is not None and it.get("style") != "slow")
    if static:
        level = float(scale if scale is not None else (PUNCH_IN_SCALE if direction == "in" else 1 / PUNCH_IN_SCALE))
        level = round(min(ZOOM_SCALE_RANGE[1], max(ZOOM_SCALE_RANGE[0], level)), 3)
        # the way the user said it goes, unless they named the level ("to 80%")
        check_dir = ("in" if level > 1 else "out") if it.get("absolute") else direction
        steps = [step("set_clip_transform", STAGE_LOOK, f"zoom {_label(c, f)} to {level * 100:g}%",
                      clip_id=c, scale=level) for c in targets]
        return Expansion(steps=tuple(steps),
                         postconditions=(pc("tool_ok", "the zoom is set", tool="set_clip_transform"),
                                         *(pc("clip_zoomed", f"the picture zooms {check_dir}", clip_id=c,
                                              direction=check_dir) for c in targets)),
                         notes=(f"{_label(targets[0], f) if len(targets) == 1 else 'every clip'} zoomed to {level * 100:g}%",))
    if ctx.has_cut_steps:
        return _ask("Zoom in its own prompt: the cuts in this one change every clip's length.")
    if len(targets) > MAX_KEN_BURNS_CLIPS:
        return _ask(f"That is {len(targets)} clips — select the ones to zoom, or name one clip.")
    a, b = (1.0, KEN_BURNS_SCALE) if direction == "in" else (KEN_BURNS_SCALE, 1.0)
    steps: list[Step] = []
    for c in targets:
        span = _span_of(c, f)
        if span is None:
            return _ask("Which clip? That one's length on the timeline is not known here.")
        dur = round(span[1] - span[0], 3)
        what = _label(c, f)
        steps.append(step("add_keyframe", STAGE_LOOK, f"{what}: scale {a:g} at its start", clip_id=c,
                          prop="scale", time=0.0, value=a, interp="ease-in-out"))
        steps.append(step("add_keyframe", STAGE_LOOK, f"{what}: scale {b:g} at its end ({dur:g}s)", clip_id=c,
                          prop="scale", time=dur, value=b, interp="ease-in-out"))
    who = _label(targets[0], f) if len(targets) == 1 else f"{len(targets)} clips"
    return Expansion(steps=tuple(steps),
                     postconditions=(pc("tool_ok", "the zoom is keyframed", tool="add_keyframe"),
                                     *(pc("clip_zoomed", f"the picture zooms {direction}", clip_id=c,
                                          direction=direction) for c in targets)),
                     notes=(f"slow zoom {direction} on {who} ({a * 100:g}% → {b * 100:g}%)",))


def x_rotate(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    targets, q = _zoom_targets(it, f)
    if q:
        return _ask(q.replace("zoom", "rotate"))
    deg = it.get("degrees")
    if deg is None:
        return _ask("Rotate by how much? Say like 'rotate it 90 degrees'.")
    deg = float(deg) % 360.0
    deg = deg - 360.0 if deg > 180.0 else deg
    steps = [step("set_clip_transform", STAGE_LOOK, f"rotate {_label(c, f)} {deg:g}°", clip_id=c, rotation=deg)
             for c in targets]
    return Expansion(steps=tuple(steps),
                     postconditions=(pc("tool_ok", "the rotation is set", tool="set_clip_transform"),),
                     notes=(f"rotated {_label(targets[0], f) if len(targets) == 1 else 'every clip'} {deg:g}°",))


def x_adjust(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut Adjust: brightness / contrast / saturation through
    `color_grade` (one merged `color` effect per clip)."""
    prop = it.get("property") or "brightness"
    change = it.get("change") or "up"
    amount = it.get("amount")
    if amount is not None:
        a = min(1.0, max(0.0, float(amount)))
        value = (a if change == "up" else -a) if prop == "brightness" else (1.0 + a if change == "up" else 1.0 - a)
    else:
        value = (ADJUST_UP if change == "up" else ADJUST_DOWN)[prop]
    value = round(value, 3)
    ref = it.get("clip_ref") or "$v1_all"
    cid, q = bind_clip(ref, f)
    if q:
        return _ask(q)
    word = {"brightness": ("brighter", "darker"), "contrast": ("more contrast", "less contrast"),
            "saturation": ("more saturated", "less saturated")}[prop][0 if change == "up" else 1]
    return Expansion(
        steps=(step("color_grade", STAGE_LOOK, f"{_label(cid, f)}: {prop} {value:g} ({word})",
                    clip_id=cid, **{prop: value}),),
        postconditions=(pc("effect_present", f"the {prop} change is applied", type="color", track="v1",
                           all=cid == "$v1_all"),),
        notes=(f"{_label(cid, f)} {word} ({prop} {value:g})",))


# --------------------------------------------------------------------------
# 4. An edit over "the last / first N seconds" (speed, mute)
# --------------------------------------------------------------------------

def range_targets(rng: Any, f: TimelineFacts, verb: str) -> tuple[list[Step], list[str], str | None]:
    """(split steps, the clip ids / sentinels the edit then acts on, question).

    "the last N seconds": a split at `duration - N` when that falls inside
    the LAST clip, then the edit on `$v1_last`. "the first N seconds": a
    split at `N` (the left half keeps its id, so every clip up to the split
    is a known id). A range that starts on a seam needs no split. A "last N"
    range that starts inside an earlier clip would need the right half of
    that split, whose id is made at run time — that one asks instead."""
    spans = v1_spans(f)
    if not spans:
        return [], [], f"Which clip should I {verb}? The main track's clips are not known here."
    start, end = rng.resolve(f.duration)
    if end - start <= _TOL_S:
        return [], [], f"That range is empty on this {f.duration:.1f}s timeline — which part should I {verb}?"
    if rng.kind == "last":
        inside = next(((cid, a, b) for cid, a, b in spans if a + _TOL_S < start < b - _TOL_S), None)
        if inside is None:
            return [], [cid for cid, a, _b in spans if a >= start - _TOL_S], None
        if inside[0] != spans[-1][0]:
            last = spans[-1]
            n = [cid for cid, _a, _b in spans].index(inside[0]) + 1
            return [], [], (f"The last {end - start:g}s start inside clip {n} at {start:g}s, across a cut. "
                            f"Should I {verb} just the last clip ({last[2] - last[1]:.1f}s)? "
                            f"Say '{verb} the last clip', or split at {start:g}s first.")
        return [step("split_at", STAGE_CUTS, f"split at {start:g}s where the last {end - start:g}s begin",
                     track="v1", time=round(start, 3))], ["$v1_last"], None
    if rng.kind == "first":
        inside = next(((cid, a, b) for cid, a, b in spans if a + _TOL_S < end < b - _TOL_S), None)
        before = [cid for cid, _a, b in spans if b <= end + _TOL_S]
        if inside is None:
            return [], before, None
        return ([step("split_at", STAGE_CUTS, f"split at {end:g}s where the first {end:g}s end", track="v1",
                      time=round(end, 3))], before + [inside[0]], None)
    # an absolute range: only when it starts on a clip's start (the left half
    # of a split keeps its id; a right half's id is made at run time)
    head = next(((cid, a, b) for cid, a, b in spans if abs(a - start) <= _TOL_S), None)
    if head is None:
        return [], [], (f"Should I {verb} from {start:g}s to {end:g}s? That starts inside a clip — split at {start:g}s "
                        f"first, then say '{verb} the clip at {start:g}s'.")
    whole = [cid for cid, a, b in spans if a >= start - _TOL_S and b <= end + _TOL_S]
    inside = next(((cid, a, b) for cid, a, b in spans if a + _TOL_S < end < b - _TOL_S), None)
    if inside is None:
        return [], whole, None
    return ([step("split_at", STAGE_CUTS, f"split at {end:g}s", track="v1", time=round(end, 3))],
            whole + [inside[0]], None)


__all__ = ["v1_spans", "bind_clip", "one_clip", "range_targets", "x_delete_clip", "x_duplicate", "x_move_clip",
           "x_zoom", "x_rotate", "x_adjust", "KEN_BURNS_SCALE", "PUNCH_IN_SCALE", "ADJUST_UP", "ADJUST_DOWN"]
