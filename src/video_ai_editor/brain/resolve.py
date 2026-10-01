"""`$brain:<kind>` — the one late binding a brain plan has (spec §5.5).

A sentinel step is an ordinary `Step` whose ONE sentinel arg names a
decision kind and whose `plan_ref` names the frozen EDP under
`<session>/brain/decisions/<did>.json`:

    {"tool": "cut_source_ranges", "args": {"track": "v1", "ranges": "$brain:cuts", "plan_ref": "d_5e2c9a17"}}

`live.resolve_live_args` hands such a step here immediately before its
dispatch; `resolve` loads the EDP (once per file), maps every relevant
decision's `ref` — `{src, t0, t1}` in that file's own seconds — through
`agent/timemap` against the LIVE tree at that moment, and returns one arg
dict or a fan-out LIST of arg dicts plus notices ("1 punch-in dropped: its
moment was cut away earlier in this plan"), one notice per reason. This is the contract the
executor already implements for `$fit_best` (executor.py `_dispatch_step`):
a list is guarded element by element and dispatched inside the one batch,
so the single-undo promise holds however many cuts, splits or keys fan out.

Rules pinned by tests/test_brain_resolve.py:
  * declared (tool, arg) pairs only — `BRAIN_SENTINELS`;
  * `plan_ref` matches `^d_[0-9a-f]{8}$`, exists, and the EDP names the
    session's CURRENT graph — a stale one is refused with a `replan`
    question (`StaleGraph`), never run;
  * the resolver never mutates the store; `plan_ref` and the sentinel are
    stripped from what reaches a handler;
  * fan-out per step ≤ `MAX_BRAIN_FANOUT`, and reaching the cap is SAID;
  * the footprint (which decision produced which entities) is written to
    `<store.dir>/brain/footprint.json` — the scratch store of a dry run
    gets its own; the card, feedback and `Decision.produced` read it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .. import config as _config
from ..agent.prompt.schema import PLAN_REF_PATTERN, NeedsInput
from ..agent.timemap import (_same_source, media_clips, source_range_to_timeline, source_to_timeline,
                             timeline_to_source)
from ..agent.tools import CAMERA_SWITCHES_MAX, CUT_RANGE_MAX_S, CUT_RANGES_MAX
from ..edl import timebase as _tb
from ..edl.schema import EDL, Clip
from ..edl.snapshot import EDLStore
from . import store as _store
from .schema import EDP, Decision

BRAIN_PREFIX = "$brain:"
PLAN_REF_ARG = "plan_ref"
PLAN_REF_RE = re.compile(PLAN_REF_PATTERN)
#: Dispatches one sentinel step may fan out into. A 60-minute podcast at
#: energy 8 is ≈ 450 cuts + 120 switches + 360 keys; the cap is said, never silent.
MAX_BRAIN_FANOUT = 4096
MAX_RANGES_PER_DISPATCH = CUT_RANGES_MAX
MAX_SWITCHES_PER_DISPATCH = CAMERA_SWITCHES_MAX
#: One resolved range is at most this long: `cut_source_ranges` refuses a
#: range over `tools.CUT_RANGE_MAX_S`, and the complement a `keep` window
#: leaves in a long recording is not bounded by anything (SC-02). A margin
#: under the tool's limit so `end − start` in floating point never crosses it.
MAX_RANGE_PIECE_S = CUT_RANGE_MAX_S - 0.5

#: kind → the ONE (tool, arg) pair that may carry it this wave (spec §5.5).
BRAIN_SENTINELS: dict[str, tuple[str, str]] = {
    "cuts": ("cut_source_ranges", "ranges"),
    "keep": ("cut_source_ranges", "ranges"),
    "story_splits": ("split_at", "time"),
    "story_order": ("reorder_clips", "order"),
    "camera": ("apply_camera_plan", "switches"),
    "punch_ins": ("add_keyframe", "clip_id"),
    "captions": ("add_caption_track", "cues"),
}
#: Args the resolver supplies for a sentinel step, so the validator does not
#: demand them of the plan (`add_keyframe` declares prop/time/value required).
SENTINEL_FILLS: dict[str, tuple[str, ...]] = {
    "cuts": ("ranges",), "keep": ("ranges",), "story_splits": ("time",), "story_order": ("order",),
    "camera": ("switches",), "punch_ins": ("prop", "props", "values", "value", "time", "interp"),
}
#: The order of stage 2 inside a brain plan (the validator's stage sort is
#: stable, so the compiler emits in this order and a test pins it).
STAGE2_ORDER: tuple[str, ...] = ("keep", "cuts", "story_splits", "story_order", "camera", "sync_dialogue_lane")
_KINDS_OF: dict[str, tuple[str, ...]] = {
    "cuts": ("cut_range",), "keep": ("keep_window",), "story_splits": ("open_on",), "story_order": ("open_on",),
    "camera": ("switch_angle",), "punch_ins": ("punch_in", "jump_cut_hide"),
}
_DEFAULT_PUNCH_SCALE = 1.10
_DEFAULT_PUNCH_INTERP = "ease-out"


class StaleGraph(ValueError):
    """The EDP was planned on a graph that is no longer the session's current one."""

    def __init__(self, did: str, planned: str, current: str | None):
        self.question = NeedsInput(
            key="replan", kind="confirm", required=True,
            question="The footage analysis changed since this plan was made. Read the footage again and re-plan?")
        super().__init__(f"decisions {did} were planned on graph {planned}; the session's current graph is "
                         f"{current or 'none'} — re-plan (replan)")


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------

def sentinel_kind(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(BRAIN_PREFIX):
        return value[len(BRAIN_PREFIX):] or None
    return None


def brain_args(args: dict[str, Any]) -> dict[str, str]:
    """`{arg: kind}` for every top-level `$brain:` value of a step."""
    return {k: kind for k, v in args.items() if (kind := sentinel_kind(v)) is not None}


def has_brain_value(value: Any) -> bool:
    """A `$brain:` string anywhere in `value`, nested included (the guard's last line)."""
    if isinstance(value, str):
        return value.startswith(BRAIN_PREFIX)
    if isinstance(value, dict):
        return any(has_brain_value(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_brain_value(v) for v in value)
    return False


def stage2_rank(step: Any) -> int:
    """Position inside stage 2 (`STAGE2_ORDER`); -1 for anything else."""
    if getattr(step, "tool", None) == "sync_dialogue_lane":
        return STAGE2_ORDER.index("sync_dialogue_lane")
    kinds = brain_args(dict(getattr(step, "args", {}) or {}))
    for kind in kinds.values():
        if kind in STAGE2_ORDER:
            return STAGE2_ORDER.index(kind)
    return -1


def brain_dir_for(store: EDLStore) -> Path:
    """`<session>/brain`. A preview's scratch store is named by the session
    id under `.prompt_preview/<run>/` and holds no brain/, so it reads the
    live session's (the EDP is frozen; both the dry run and Apply read the
    same file — spec §0.1)."""
    own = Path(store.dir) / "brain"
    if (own / "decisions").is_dir() or (own / "graph").is_dir():
        return own
    return Path(_config.WORKDIR) / Path(store.dir).name / "brain"


_EDP_CACHE: dict[tuple[str, int, int], EDP] = {}


def load_edp(bdir: Path, did: str) -> EDP | None:
    """The EDP, read once per (path, size, mtime) — 'once per run' for a
    frozen file, and never a stale copy after a re-plan."""
    if not PLAN_REF_RE.fullmatch(str(did)):
        raise ValueError(f"plan_ref {did!r} is not a decisions id")
    p = bdir / "decisions" / f"{did}.json"
    if not p.is_file():
        return None
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    edp = _EDP_CACHE.get(key)
    if edp is None:
        edp = _store.read_edp(bdir.parent, did)
        if len(_EDP_CACHE) >= 8:
            _EDP_CACHE.pop(next(iter(_EDP_CACHE)))
        _EDP_CACHE[key] = edp
    return edp


def source_paths(bdir: Path, gid: str) -> dict[str, str]:
    """source key → absolute path, from the graph's sources and angles.json."""
    out: dict[str, str] = {}
    try:
        graph = _store.read_graph(bdir.parent, gid)
    except (OSError, ValueError):
        graph = None
    if graph is not None:
        for s in graph.sources:
            if s.path:
                out[s.key.removeprefix("src_")] = s.path
    try:
        angles = _store.read_angles(bdir.parent)
    except (OSError, ValueError):
        angles = None
    if angles is not None:
        for m in angles.members:
            out.setdefault(m.src_key.removeprefix("src_"), m.path)
    return out


def angle_offsets(bdir: Path) -> dict[str, float]:
    """{file path: sync offset} for the angle group's members (angles.json). With no recorder the REFERENCE is a
    camera too and sits at offset 0 (its own clock); an `angles.json` written before the analysis listed it
    (members == [B] for cameras A and B) is completed from the graph, so camera A's moments still map (closer
    review: the captions of such a session were all dropped, "the moment is already removed")."""
    try:
        angles = _store.read_angles(bdir.parent)
    except (OSError, ValueError):
        return {}
    if angles is None:
        return {}
    out = {m.path: float(m.sync_offset_s) for m in angles.members}
    ref = _reference_camera_path(bdir, angles) if angles.members else None
    return {ref: 0.0, **out} if ref and ref not in out else out


def _reference_camera_path(bdir: Path, angles: Any) -> str | None:
    """The reference source's file when it is a camera (`role: angle`, with picture) of the current graph."""
    try:
        gid = _store.current_graph_id(bdir.parent)
        graph = _store.read_graph(bdir.parent, gid) if gid else None
    except (OSError, ValueError):
        return None
    if graph is None:
        return None
    bare = str(angles.reference).removeprefix("src_")
    for s in graph.sources:
        if s.key.removeprefix("src_") == bare and s.path and s.role == "angle" and s.has_video is not False:
            return str(s.path)
    return None


def _path_of(src: str, paths: dict[str, str]) -> str | None:
    hit = paths.get(src.removeprefix("src_"))
    if hit:
        return hit
    return src if os.path.isabs(src) else None


#: Decision kinds whose sources reach a FILE arg of a tool, per sentinel kind.
_FILE_KINDS: dict[str, tuple[str, ...]] = {"cuts": ("cut_range",), "keep": ("keep_window",), "camera": ("switch_angle",)}


def declared_paths(bdir: Path, edp: EDP, kind: str) -> list[str]:
    """Every file `$brain:<kind>` could hand to its tool: the decisions'
    `ref.src` and a camera decision's angle, mapped through the graph and
    angles.json exactly as the resolver will (SC-05: validate checks these
    against the offered files, so a tampered EDP or graph never validates).
    Keys that name no file resolve to nothing at run time and are not listed."""
    paths = source_paths(bdir, edp.graph.id)
    out: list[str] = []
    for d in edp.by_kind(*_FILE_KINDS.get(kind, ())):
        names = [d.ref.src] if d.ref is not None else []
        if kind == "camera":
            angle = d.params.get("angle") or d.params.get("angle_src")
            names += [str(angle)] if angle else []
        out += [p for n in names if (p := _path_of(str(n), paths)) is not None and p not in out]
    return out


# --------------------------------------------------------------------------
# a timeline the brain already edited (EX-02 / UX-04)
# --------------------------------------------------------------------------

#: Tools of a brain plan that must not run on a timeline whose camera plan
#: is already on the main lane.
RERUN_GUARDED_TOOLS = frozenset({"cut_source_ranges", "apply_camera_plan"})


class AlreadyEdited(ValueError):
    """The main lane already switches between the angle group's cameras, so a
    plan that cuts by SOURCE range would remove camera pieces its own
    decisions never name (EX-02: a second run cut 107 s of a 160 s episode)."""

    def __init__(self) -> None:
        self.question = NeedsInput(
            key="replan", kind="confirm", required=True,
            question="This timeline is already edited. Restore the version before that edit and re-edit, "
                     "or tighten what is here by hand?")
        super().__init__("this timeline already has camera changes from an earlier edit, so a second brain run "
                         "would cut footage its own decisions do not name. Nothing was changed. Restore the "
                         "version before that edit and run it once, or tighten what is here by hand")


def camera_plan_present(edl: EDL, member_paths: Iterable[str]) -> bool:
    """True when the live v1 layout alternates between the angle group's
    files — some camera plays two separate stretches with another camera
    between them. The layout is the truth: a fresh timeline holds each
    upload as one contiguous run (`[cam A][cam B]`), a camera plan leaves
    `A B A B …`. Other footage (B-roll) between two stretches of one camera
    does not count."""
    members = [str(m) for m in member_paths]
    if len(members) < 2:
        return False
    runs: list[int] = []
    for c in media_clips(edl, "v1"):
        if c.freeze is not None:
            continue
        k = next((i for i, m in enumerate(members) if _same_source(c.src, m)), None)
        if k is not None and (not runs or runs[-1] != k):
            runs.append(k)
    return len(runs) != len(set(runs))


def refuse_if_already_edited(store: EDLStore) -> None:
    """The last line for a re-run: raises `AlreadyEdited` when the session's
    angle group is already interleaved on v1. A session with no angles.json
    (one camera, brain never ran) is never refused here."""
    members = list(angle_offsets(brain_dir_for(store)))
    if camera_plan_present(store.edl, members):
        raise AlreadyEdited()


def _half_frame(edl: EDL) -> float:
    return _tb.frame_duration(edl.canvas.fps) / 2.0


def _r4(v: float) -> float:
    return round(float(v), 4)


# --------------------------------------------------------------------------
# per-kind resolution — each returns (arg dicts, footprint entries, dropped)
# --------------------------------------------------------------------------

class _Ctx:
    def __init__(self, store: EDLStore, edp: EDP, base: dict[str, Any], paths: dict[str, str],
                 offsets: dict[str, float] | None = None):
        self.edl: EDL = store.edl
        self.edp = edp
        self.base = base
        self.paths = paths
        self.offsets = dict(offsets or {})                  # {angle file: sync offset} (angles.json)
        self.entries: list[dict[str, Any]] = []
        self.dropped: list[dict[str, Any]] = []
        self.notices: list[str] = []

    def path(self, d: Decision) -> str | None:
        if d.ref is None:
            self.drop(d, "no source span")
            return None
        p = _path_of(d.ref.src, self.paths)
        if p is None:
            self.drop(d, f"source {d.ref.src} is not on this timeline")
        return p

    def same_moment(self, path: str, t: float) -> list[tuple[str, float]]:
        """`(file, second)` of the moment `t` of `path` in every angle of the
        group, `path` first: after a camera plan the piece that shows it may
        play another angle's file (`file_t = ref_t + offset[file]`)."""
        out = [(path, t)]
        own = self.offsets.get(path)
        if own is not None:
            out += [(p, _r4(t - own + off)) for p, off in self.offsets.items() if p != path]
        return out

    def drop(self, d: Decision, why: str) -> None:
        self.dropped.append({"decision": d.id, "kind": d.kind, "why": why})

    def entry(self, d: Decision, **more: Any) -> None:
        self.entries.append({"decision": d.id, "kind": d.kind, "code": d.reason.code, "text": d.reason.text, **more})


def _lineage(clip_id: str) -> list[str]:
    """A piece's id and its ancestors': a split/cut piece appends `_<hex>`
    per generation (`c_627c3ffb_6b1f58`), so the prefixes name the clips of
    the pre-run tree the piece came from."""
    parts = str(clip_id).split("_")
    return ["_".join(parts[:n]) for n in range(len(parts), 1, -1)]


def _clips_hit(cx: _Ctx, path: str, t0: float, t1: float) -> list[str]:
    """The v1 clips (and their ancestors by id) whose SOURCE span meets
    `[t0, t1]` of `path` in the live tree at resolution time — what ties a
    card line to its decision exactly (lane F's request)."""
    out: list[str] = []
    for c in media_clips(cx.edl, "v1", src=path):
        if c.freeze is None and c.in_ < t1 - 1e-6 and t0 < c.out - 1e-6:
            out.extend(i for i in _lineage(c.id) if i not in out)
    return out


GONE_WHY = "already removed, earlier in this plan or in an earlier edit"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _drop_notices(cx: _Ctx, noun: str, tail: str = "") -> None:
    """One notice per REASON among the dropped decisions (SC-16): each names
    its own decisions and its own reason, so a file that is not on the
    timeline is never reported as 'already removed'."""
    groups: dict[str, list[str]] = {}
    for x in cx.dropped:
        groups.setdefault(str(x["why"]), []).append(str(x["decision"]))
    for why, names in groups.items():
        cx.notices.append(f"{_plural(len(names), noun)} dropped ({', '.join(names)}): {why}{tail}")


def split_range(a: float, b: float) -> list[tuple[float, float]]:
    """`[a, b]` as ordered, abutting pieces of at most `MAX_RANGE_PIECE_S`;
    their union is `[a, b]` exactly (the first starts at `a`, the last ends
    at `b`, each ends where the next begins)."""
    if b - a <= MAX_RANGE_PIECE_S:
        return [(a, b)]
    out: list[tuple[float, float]] = []
    cur = a
    while b - cur > MAX_RANGE_PIECE_S:
        nxt = _r4(cur + MAX_RANGE_PIECE_S)
        out.append((cur, nxt))
        cur = nxt
    out.append((cur, b))
    return out


def _range_dispatches(cx: _Ctx, ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every range in pieces the tool accepts, then `MAX_RANGES_PER_DISPATCH`
    per fan-out entry (order and adjacency kept)."""
    rows = [{**r, "start": _r4(a), "end": _r4(z)} for r in ranges for a, z in split_range(float(r["start"]), float(r["end"]))]
    return [{**cx.base, "ranges": rows[i:i + MAX_RANGES_PER_DISPATCH]} for i in range(0, len(rows), MAX_RANGES_PER_DISPATCH)]


def _resolve_cuts(cx: _Ctx) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for d in cx.edp.by_kind("cut_range"):
        p = cx.path(d)
        if p is None:
            continue
        hits = source_range_to_timeline(cx.edl, "v1", d.ref.t0, d.ref.t1, src=p)
        if not hits:
            cx.drop(d, GONE_WHY)
            continue
        ranges.append({"src": p, "start": d.ref.t0, "end": d.ref.t1})
        cx.entry(d, src_range=[d.ref.t0, d.ref.t1], timeline=[[_r4(a), _r4(b)] for a, b in hits],
                 clip_ids=_clips_hit(cx, p, d.ref.t0, d.ref.t1))
    _drop_notices(cx, "cut")
    return _range_dispatches(cx, ranges)


def _resolve_keep(cx: _Ctx) -> list[dict[str, Any]]:
    windows: dict[str, list[tuple[float, float]]] = {}
    for d in cx.edp.by_kind("keep_window"):
        p = cx.path(d)
        if p is None:
            continue
        if not source_range_to_timeline(cx.edl, "v1", d.ref.t0, d.ref.t1, src=p):
            cx.drop(d, "the kept window is not on the timeline")
            continue
        windows.setdefault(p, []).append((d.ref.t0, d.ref.t1))
        cx.entry(d, kept=[d.ref.t0, d.ref.t1], clip_ids=_clips_hit(cx, p, d.ref.t0, d.ref.t1))
    ranges: list[dict[str, Any]] = []
    for p, wins in windows.items():
        merged = _merge(wins)
        for c in media_clips(cx.edl, "v1", src=p):
            if c.freeze is not None:
                continue
            for a, b in _subtract((c.in_, c.out), merged):
                rng = {"src": p, "start": _r4(a), "end": _r4(b)}
                if rng not in ranges:
                    ranges.append(rng)
    _drop_notices(cx, "kept window")
    return _range_dispatches(cx, ranges)


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(span: tuple[float, float], keep: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """`span` minus the union `keep`, as ordered non-empty pieces."""
    out: list[tuple[float, float]] = []
    cur = span[0]
    for a, b in keep:
        if b <= cur:
            continue
        if a >= span[1]:
            break
        if a > cur:
            out.append((cur, min(a, span[1])))
        cur = max(cur, b)
    if cur < span[1]:
        out.append((cur, span[1]))
    return [(a, b) for a, b in out if b - a > 1e-6]


def _hook_decisions(cx: _Ctx) -> list[tuple[Decision, str]]:
    out: list[tuple[Decision, str]] = []
    for d in cx.edp.by_kind("open_on"):
        p = cx.path(d)
        if p is None:
            continue
        if not source_range_to_timeline(cx.edl, "v1", d.ref.t0, d.ref.t1, src=p):
            why = "the opening moment is not on the timeline (cut away earlier in this plan or in an earlier edit)"
            cx.drop(d, why)
            cx.notices.append(f"{d.id}: {why}")
            continue
        out.append((d, p))
    return out


def _resolve_story_splits(cx: _Ctx) -> list[dict[str, Any]]:
    times: list[float] = []
    half = _half_frame(cx.edl)
    for d, p in _hook_decisions(cx):
        mine: list[float] = []
        for t_src in (d.ref.t0, d.ref.t1):
            tl = source_to_timeline(cx.edl, "v1", t_src, src=p)
            if tl is None:
                continue
            hit = timeline_to_source(cx.edl, "v1", tl)
            if hit is None:
                continue
            c, _ = hit
            if tl - c.start > half and (c.start + c.effective_duration) - tl > half:
                mine.append(_r4(tl))
        cx.entry(d, times=mine)
        times.extend(mine)
    times = sorted(set(times))
    if not times and not cx.dropped:
        cx.notices.append("the opening moment is already isolated — nothing to split")
    return [{**cx.base, "time": t} for t in times]


def _hook_clips(cx: _Ctx, d: Decision, p: str, one: float) -> list[Clip]:
    """The v1 pieces that ARE the opening moment, in source order: every
    piece of `p` inside the decision's span, the first opening at its start
    and the last closing at its end (a removal inside the moment leaves it
    in several pieces; they move together)."""
    inside = sorted((c for c in media_clips(cx.edl, "v1", src=p)
                     if c.freeze is None and c.in_ >= d.ref.t0 - one and c.out <= d.ref.t1 + one), key=lambda c: c.in_)
    if not inside or abs(inside[0].in_ - d.ref.t0) > one or abs(inside[-1].out - d.ref.t1) > one:
        return []
    return inside


def _resolve_story_order(cx: _Ctx) -> list[dict[str, Any]]:
    track = cx.edl.get_track("v1")
    if track is None:
        return []
    one = _tb.frame_duration(cx.edl.canvas.fps)
    first: list[str] = []
    for d, p in _hook_decisions(cx):
        hook = _hook_clips(cx, d, p, one)
        if not hook:
            cx.notices.append(f"{d.id}: the opening moment is not an isolated clip — order unchanged")
            continue
        first += [c.id for c in hook]
        cx.entry(d, clip_ids=[c.id for c in hook])
    if not first:
        return []
    rest = [c.id for c in track.clips if c.id not in first]
    return [{**cx.base, "order": [*first, *rest]}]


def _resolve_camera(cx: _Ctx) -> list[dict[str, Any]]:
    switches: list[dict[str, Any]] = []
    for d in cx.edp.by_kind("switch_angle"):
        p = cx.path(d)
        if p is None:
            continue
        angle = d.params.get("angle") or d.params.get("angle_src")
        angle_path = _path_of(str(angle), cx.paths) if angle else None
        if angle_path is None:
            cx.drop(d, "the angle names no file of this project")
            continue
        hits = source_range_to_timeline(cx.edl, "v1", d.ref.t0, d.ref.t1, src=p)
        if not hits:
            cx.drop(d, "its span is not on the timeline (cut away earlier in this plan or in an earlier edit)")
            continue
        switches.append({"src": p, "at_src": d.ref.t0, "until_src": d.ref.t1, "angle_src": angle_path})
        cx.entry(d, src_range=[d.ref.t0, d.ref.t1], angle=angle_path,
                 timeline=[[_r4(a), _r4(b)] for a, b in hits], clip_ids=_clips_hit(cx, p, d.ref.t0, d.ref.t1))
    _drop_notices(cx, "camera change")
    return [{**cx.base, "switches": switches[i:i + MAX_SWITCHES_PER_DISPATCH]}
            for i in range(0, len(switches), MAX_SWITCHES_PER_DISPATCH)]


def _punch_keys(d: Decision) -> list[dict[str, Any]]:
    keys = d.params.get("keys")
    if isinstance(keys, list) and keys:
        return [dict(k) for k in keys if isinstance(k, dict) and "t" in k]
    scale = float(d.params.get("scale", _DEFAULT_PUNCH_SCALE))
    interp = "step" if d.kind == "jump_cut_hide" else str(d.params.get("interp", _DEFAULT_PUNCH_INTERP))
    return [{"t": d.ref.t0 if d.ref else 0.0, "values": {"scale": scale}, "interp": interp}]


def _key_clip(cx: _Ctx, p: str, t: float, *, opens: bool) -> tuple[Clip, float] | None:
    """The v1 piece a key at source second `t` of `p` lands on, and its
    clip-local time. A key that OPENS a piece (a jump-cut hide sits exactly
    on the seam) belongs to the piece that starts there: its in-point is
    the frame-quantised seam, which may lie a hair after `t`."""
    one = _tb.frame_duration(cx.edl.canvas.fps)
    for path, at in cx.same_moment(p, t):
        if opens:
            starts = [c for c in media_clips(cx.edl, "v1", src=path) if c.freeze is None and abs(c.in_ - at) <= one + 1e-6]
            if starts:
                return min(starts, key=lambda c: abs(c.in_ - at)), 0.0
        tl = source_to_timeline(cx.edl, "v1", at, src=path)
        hit = timeline_to_source(cx.edl, "v1", tl) if tl is not None else None
        if hit is not None:
            return hit[0], max(0.0, _r4(tl - hit[0].start))
    return None


def _resolve_punch_ins(cx: _Ctx) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in cx.edp.by_kind("punch_in", "jump_cut_hide"):
        p = cx.path(d)
        if p is None:
            continue
        dispatches: list[dict[str, Any]] = []
        for i, key in enumerate(_punch_keys(d)):
            at = _key_clip(cx, p, float(key["t"]), opens=d.kind == "jump_cut_hide")
            if at is None:
                if i == 0:
                    dispatches = []
                    break
                continue
            c, local = at
            values = {k: float(v) for k, v in dict(key.get("values") or {}).items()}
            dispatches.append({**cx.base, "clip_id": c.id, "props": sorted(values), "values": values,
                               "time": local, "interp": str(key.get("interp", _DEFAULT_PUNCH_INTERP))})
        if not dispatches:
            cx.drop(d, "its moment is not on the timeline (cut away earlier in this plan or in an earlier edit)")
            continue
        cx.entry(d, clip_ids=sorted({x["clip_id"] for x in dispatches}),
                 keyframe_times=[x["time"] for x in dispatches])
        out.extend(dispatches)
    _drop_notices(cx, "punch-in")
    return out


def _resolve_captions(cx: _Ctx) -> list[dict[str, Any]]:
    from .caption_lane import resolve_captions      # cues on the reference clock → the live v1 layout, by source
    return resolve_captions(cx)


_RESOLVERS = {"cuts": _resolve_cuts, "keep": _resolve_keep, "story_splits": _resolve_story_splits,
              "story_order": _resolve_story_order, "camera": _resolve_camera, "punch_ins": _resolve_punch_ins,
              "captions": _resolve_captions}


# --------------------------------------------------------------------------
# the footprint
# --------------------------------------------------------------------------

def footprint_path(store: EDLStore) -> Path:
    return Path(store.dir) / "brain" / "footprint.json"


def _record_footprint(store: EDLStore, did: str, kind: str, cx: _Ctx) -> None:
    p = footprint_path(store)
    fp: dict[str, Any] = {"plan_ref": did, "steps": {}, "dropped": []}
    try:
        old = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        old = None
    if isinstance(old, dict) and old.get("plan_ref") == did:
        fp = {"plan_ref": did, "steps": dict(old.get("steps") or {}), "dropped": list(old.get("dropped") or [])}
    fp["steps"][kind] = cx.entries
    fp["dropped"] = [x for x in fp["dropped"] if x.get("step") != kind] + [{**x, "step": kind} for x in cx.dropped]
    _store.write_json(p, fp)


def read_footprint(store_dir: Path | str) -> dict[str, Any] | None:
    p = Path(store_dir) / "brain" / "footprint.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# the entry point (live.resolve_live_args → here)
# --------------------------------------------------------------------------

def resolve(store: EDLStore, tool: str,
            args: dict[str, Any]) -> tuple[dict[str, Any] | list[dict[str, Any]] | None, list[str]]:
    """Resolve the step's `$brain:` arg against the live tree. Returns
    `(args | [args, …] | None, notices)` — None when nothing is left to do."""
    kinds = brain_args(args)
    if not kinds:
        return {k: v for k, v in args.items() if k != PLAN_REF_ARG}, []
    if len(kinds) != 1:
        raise ValueError(f"{tool}: a step carries ONE brain sentinel, got {sorted(kinds)}")
    (arg, kind), = kinds.items()
    pair = BRAIN_SENTINELS.get(kind)
    if pair is None or pair != (tool, arg):
        raise ValueError(f"{tool}.{arg}: $brain:{kind} is not accepted here")
    did = str(args.get(PLAN_REF_ARG) or "")
    if not PLAN_REF_RE.fullmatch(did):
        raise ValueError(f"{tool}: a $brain step needs plan_ref d_xxxxxxxx, got {did!r}")
    bdir = brain_dir_for(store)
    edp = load_edp(bdir, did)
    if edp is None:
        raise ValueError(f"{tool}: decisions {did} are not in this session")
    current = _store.current_graph_id(bdir.parent)
    if current is None or edp.graph.id != current:
        raise StaleGraph(did, edp.graph.id, current)
    base = {k: v for k, v in args.items() if k not in (arg, PLAN_REF_ARG)}
    cx = _Ctx(store, edp, base, source_paths(bdir, edp.graph.id), angle_offsets(bdir))
    out = _RESOLVERS[kind](cx)
    if len(out) > MAX_BRAIN_FANOUT:
        left = len(out) - MAX_BRAIN_FANOUT
        out = out[:MAX_BRAIN_FANOUT]
        cx.notices.append(f"fan-out capped at {MAX_BRAIN_FANOUT} dispatches for {kind}: {left} left out")
    _record_footprint(store, did, kind, cx)
    if not out:
        return None, cx.notices
    if kind in ("story_order",) or (kind in ("cuts", "keep", "camera", "captions") and len(out) == 1):
        return out[0], cx.notices
    return out, cx.notices


__all__ = ["BRAIN_PREFIX", "PLAN_REF_ARG", "PLAN_REF_RE", "MAX_BRAIN_FANOUT", "BRAIN_SENTINELS", "SENTINEL_FILLS",
           "STAGE2_ORDER", "StaleGraph", "sentinel_kind", "brain_args", "has_brain_value", "stage2_rank",
           "brain_dir_for", "load_edp", "source_paths", "declared_paths", "split_range", "AlreadyEdited",
           "RERUN_GUARDED_TOOLS", "camera_plan_present", "refuse_if_already_edited", "footprint_path",
           "read_footprint", "resolve"]
