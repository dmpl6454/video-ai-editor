"""The two BLOCKING verifier checks of wave EB1 (spec §6.1), in the shape
`agent/prompt/verify.py` runs: `c_<name>(ctx, pc) -> CheckResult`.

`no_cut_mid_word(tol=0.02)` — every AUDIO cut seam created by the run
lies outside `[w.t0 + tol, w.t1 − tol]` of every kept word. The audio lane
is `a1` when it holds the dialogue (any media clip), else `v1`. Measured in
the SOURCE clock: `timemap` clips a word straddling a cut to its surviving
part, so on the timeline a mid-word cut always looks like a word that ends
exactly at the seam — only the source instant of each cut edge (`prev.out`,
`next.in_`) can say "mid-word". A seam where the source is CONTINUOUS (a
camera switch over a continuous a1, a bare split) is not a cut and is
exempt. Needs the transcript (`ctx.transcript()`, the side-effect snapshot
keeps it readable in-batch); without one the check cannot measure and
never blocks. Edges of pieces whose file is not the transcript's are not
measured by the transcript.

When the session has a CURRENT Content Graph the measuring stick is its
speech layer instead (EB1 integration): the words whisper timed are
repaired onto the sound there (whisper.cpp put “Um,” 0.2 s past its own
voiced island on fixture TH, so the raw transcript called a clean cut
mid-word), they are on the reference clock, and every source's offset is
known — so the edges of EVERY file of the angle group are measured.

`dialogue_in_sync(tol = half a frame)` — EDL only. When `a1` holds the
dialogue source: for every v1 media piece whose file is an angle member or
the dialogue file, the a1 clip covering `piece.start` starts within `tol`,
plays the same reference second (`in_ = piece.in_ + offsets[dialogue] −
offsets[piece.src]` — the sign `sync_dialogue_lane` lays and the one its
"positive = lags the reference" offsets imply; the EB1 brief's text has the
two terms the other way round, and the check follows the tool as built —
within `tol`) from the dialogue file; no a1 clip stands
over a v1 gap; every v1 angle piece is muted; no v1 transition. The
offsets and the dialogue file come from the plan's own `sync_dialogue_lane`
step (the a1 clips' common source when the plan has none). Without an a1
lane the check cannot measure.

`removal_within_plan` (EX-02, run by the executor's safety net for every
brain plan, blocking): the picture a run removed must not exceed what the
plan's OWN decisions name — the cut ranges, the complement of the kept
windows and the literal "angle leaves the main lane" ranges (which name
nothing once the camera plan is already on v1) — measured on the tree
BEFORE the run, by more than a frame per cut. It reads the frozen EDP, so
it cannot be talked round by a step's text.

`install()` registers both in `verify.CHECKS` (the executor calls it at
import, so the K3 net and `verify_plan` see them in every process).
"""
from __future__ import annotations

from typing import Any

from ..agent.prompt import live as _live
from ..agent.prompt import verify as _verify
from ..agent.prompt.schema import Postcondition
from ..agent.prompt.verify import CheckResult, VerifyCtx
from ..agent.timemap import _same_source, media_clips, source_range_to_timeline
from ..edl import timebase as _tb
from ..edl.schema import EDL, Clip

DEFAULT_MID_WORD_TOL_S = 0.02


def _ok(pc: Postcondition, passed: bool | None, measured: Any, expected: Any, detail: str = "") -> CheckResult:
    return CheckResult(check=pc.check, human=pc.human, passed=passed, measured=measured, expected=expected,
                       detail=detail, headline=pc.headline)


def _media(edl: EDL, track_id: str) -> list[Clip]:
    return media_clips(edl, track_id)


def audio_lane(edl: EDL) -> str:
    """`a1` when it holds the dialogue (any media clip), else `v1`."""
    return "a1" if _media(edl, "a1") else "v1"


def cut_seams(edl: EDL, track_id: str) -> list[tuple[float, Clip, Clip]]:
    """`(timeline_s, outgoing, incoming)` for every seam on `track_id`
    where the SOURCE is discontinuous — a cut, not a switch or a split."""
    clips = [c for c in _media(edl, track_id) if c.freeze is None]
    half = _tb.frame_duration(edl.canvas.fps) / 2.0
    out: list[tuple[float, Clip, Clip]] = []
    for prev, nxt in zip(clips, clips[1:]):
        boundary = prev.start + prev.effective_duration
        continuous = (_same_source(prev.src, nxt.src) and abs(nxt.in_ - prev.out) <= half
                      and not prev.reverse and not nxt.reverse)
        if not continuous:
            out.append((boundary, prev, nxt))
    return out


def _edge_instants(edl: EDL, lane: str, seam: tuple[float, Clip, Clip], tx_src: str | None) -> list[float]:
    """The source instants of a seam's two edges, in the transcript's clock:
    the lane's own clips when they play the transcript's file, else the v1
    pieces meeting at that instant that do."""
    boundary, prev, nxt = seam
    pairs: list[tuple[Clip, bool]] = [(prev, True), (nxt, False)]
    if lane != "v1" and not (_same_source(prev.src, tx_src) and _same_source(nxt.src, tx_src)):
        half = _tb.frame_duration(edl.canvas.fps) / 2.0
        pairs = []
        for c in _media(edl, "v1"):
            if abs(c.start + c.effective_duration - boundary) <= half:
                pairs.append((c, True))
            elif abs(c.start - boundary) <= half:
                pairs.append((c, False))
    return [(c.out if outgoing else c.in_) for c, outgoing in pairs if _same_source(c.src, tx_src)]


def _kept(edl: EDL, w: dict, tx_src: str | None) -> bool:
    return bool(source_range_to_timeline(edl, "v1", float(w["start"]), float(w["end"]), src=tx_src))


def _graph_clock(ctx: VerifyCtx) -> tuple[list[dict], dict[str, float]] | None:
    """The session's CURRENT Content Graph as a measuring stick: its speech
    layer's words (reference seconds, edges repaired onto the sound) and
    `{file path: sync offset}` for every source. None without a graph — the
    upload transcript is the stick then."""
    from . import resolve as _resolve
    from . import store as _store
    from .planner.graph_view import load_graph
    try:
        sdir = _resolve.brain_dir_for(ctx.store).parent
        gid = _store.current_graph_id(sdir)
        if gid is None:
            return None
        g = load_graph(sdir, gid)
    except Exception:  # noqa: BLE001 — an unreadable graph is "no graph", never a crash in a check
        return None
    offsets = {str(s["path"]): g.offset(s["key"]) for s in g.sources if s.get("path")}
    if not g.words or not offsets:
        return None
    return g.words, offsets


def _graph_offending(edl: EDL, lane: str, seams: list, words: list[dict], offsets: dict[str, float], tol: float) -> list[str]:
    """Every cut edge of `lane`, moved onto the reference clock by its own
    file's offset, against the graph's words. A piece that plays up to (or
    from) an instant strictly inside a word keeps part of that word."""
    out: list[str] = []
    fps = edl.canvas.fps
    for boundary, prev, nxt in seams:
        for c, x in ((prev, prev.out), (nxt, nxt.in_)):
            off = _offset(offsets, c.src)
            if off is None:
                continue
            r = float(x) - off
            hit = next((w for w in words if float(w["t0"]) + tol < r < float(w["t1"]) - tol), None)
            if hit is not None:
                out.append(f"“{str(hit.get('text', '')).strip()}” cut at source {float(x):.3f}s "
                           f"(seam {_live.smpte(boundary, fps)} on {lane})")
    return out


def c_no_cut_mid_word(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    tol = float(pc.args.get("tol", DEFAULT_MID_WORD_TOL_S) or DEFAULT_MID_WORD_TOL_S)
    expected = "cut edges outside every kept word"
    edl = ctx.edl
    lane = audio_lane(edl)
    seams = cut_seams(edl, lane)
    clock = _graph_clock(ctx)
    if clock is not None:
        offending = _graph_offending(edl, lane, seams, clock[0], clock[1], tol)
        measured = {"lane": lane, "seams": len(seams), "offending": offending, "words": "content graph"}
        return _ok(pc, not offending, measured, expected, "; ".join(offending[:6]))
    tx, tx_src = ctx.transcript()
    if tx is None or tx_src is None:
        return _ok(pc, None, None, expected, "no transcript to measure against")
    words = ctx.words_source()
    offending = []
    fps = edl.canvas.fps
    for seam in seams:
        for x in _edge_instants(edl, lane, seam, tx_src):
            for w in words:
                if float(w["start"]) + tol < x < float(w["end"]) - tol and _kept(edl, w, tx_src):
                    offending.append(f"“{str(w.get('word', '')).strip()}” cut at source {x:.3f}s "
                                     f"(seam {_live.smpte(seam[0], fps)} on {lane})")
                    break
    measured = {"lane": lane, "seams": len(seams), "offending": offending}
    if offending:
        return _ok(pc, False, measured, expected, "; ".join(offending[:6]))
    return _ok(pc, True, measured, expected)


# --------------------------------------------------------------------------
# dialogue_in_sync
# --------------------------------------------------------------------------

def _dialogue_step(ctx: VerifyCtx) -> dict[str, Any] | None:
    steps = [s for s in getattr(ctx.plan, "steps", []) or [] if getattr(s, "tool", None) == "sync_dialogue_lane"]
    return dict(steps[-1].args) if steps else None


def _offset(offsets: dict[str, float], src: str | None) -> float | None:
    for k, v in offsets.items():
        if _same_source(k, src):
            return float(v)
    return None


def _expected(p: Clip, d_src: str, offsets: dict[str, float], fps: Any) -> tuple[float, float]:
    """Where the lane's clip under v1 piece `p` starts and what it plays —
    the tool's own rule (dispatch._dialogue_piece): `in_ = p.in_ +
    off[dialogue] − off[piece]` ("positive offset = that file lags the
    reference": the piece shows its file's second `r + off[piece]`, the
    dialogue file plays `r` at `r + off[dialogue]`), starting where the
    piece starts; where the dialogue file has not begun yet (`in_ < 0`: the
    camera rolled before the recorder) the clip starts LATER, on the next
    frame boundary at which it has sound."""
    want_in = p.in_ + (_offset(offsets, d_src) or 0.0) - (_offset(offsets, p.src) or 0.0)
    if want_in >= 0.0 or p.reverse:
        return p.start, want_in
    start = _tb.ceil_to_frame(p.start + (0.0 - want_in) / p.speed_factor, fps)
    return start, want_in + (start - p.start) * p.speed_factor


def _check_piece(p: Clip, a1: list[Clip], d_src: str, offsets: dict[str, float], tol: float,
                 mute: bool, fps: Any) -> str | None:
    """One v1 piece against the lane; the first defect as text, else None."""
    is_member = _offset(offsets, p.src) is not None
    if mute and is_member and not p.audio.mute:
        return f"v1 piece at {_live.smpte(p.start, fps)} is not muted"
    start, want_in = _expected(p, d_src, offsets, fps)
    if start >= p.start + p.effective_duration - tol:
        return None                                    # the dialogue file has none of this piece: a gap is right
    a = next((c for c in a1 if c.start - tol <= start < c.start + c.effective_duration - tol), None)
    if a is None:
        return f"no a1 clip under the v1 piece at {_live.smpte(p.start, fps)}"
    if not _same_source(a.src, d_src):
        return f"a1 clip at {_live.smpte(a.start, fps)} plays another file"
    if abs(a.start - start) > tol:
        return f"a1 clip start {a.start:.4f} is off the v1 piece at {p.start:.4f}"
    if abs(a.in_ - want_in) > tol:
        return f"a1 in_ {a.in_:.4f} should be {want_in:.4f} under the piece at {_live.smpte(p.start, fps)}"
    return None


def c_dialogue_in_sync(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    edl = ctx.edl
    a1 = [c for c in _media(edl, "a1") if c.freeze is None]
    fps = edl.canvas.fps
    tol = float(pc.args.get("tol") or (_tb.frame_duration(fps) / 2.0))
    if not a1:
        return _ok(pc, None, None, "a1 plays the picture's reference seconds", "no dialogue lane")
    step = _dialogue_step(ctx) or {}
    offsets = {str(k): float(v) for k, v in dict(step.get("offsets") or {}).items()}
    d_src = str(step.get("src") or a1[0].src)
    mute = bool(step.get("mute_camera_mics", True))
    problems: list[str] = []
    v1 = [c for c in _media(edl, "v1") if c.freeze is None]
    pieces = [p for p in v1 if _same_source(p.src, d_src) or _offset(offsets, p.src) is not None]
    for p in pieces:
        why = _check_piece(p, a1, d_src, offsets, tol, mute, fps)
        if why:
            problems.append(why)
    starts = [p.start for p in v1] + [_expected(p, d_src, offsets, fps)[0] for p in pieces]
    for a in a1:
        if not any(abs(a.start - t) <= tol for t in starts):
            problems.append(f"a1 clip at {_live.smpte(a.start, fps)} stands over a v1 gap")
    track = edl.get_track("v1")
    if track is not None and track.transitions:
        problems.append(f"{len(track.transitions)} v1 transition(s) while the dialogue lane exists")
    measured = {"lane": "a1", "pieces": len(pieces), "a1_clips": len(a1), "tol": tol, "problems": problems}
    if problems:
        return _ok(pc, False, measured, "a1 plays the picture's reference seconds", "; ".join(problems[:6]))
    return _ok(pc, True, measured, "a1 plays the picture's reference seconds")


# --------------------------------------------------------------------------
# removal_within_plan
# --------------------------------------------------------------------------

def _named_rows(before: EDL, edp: Any, bdir: Any, steps: list[Any]) -> list[tuple[str, float, float]]:
    """`(src, start, end)` in SOURCE seconds: every range the plan's own
    decisions and literal steps name, resolved against `before`."""
    from types import SimpleNamespace

    from . import resolve as _r
    cx = _r._Ctx(SimpleNamespace(edl=before), edp, {}, _r.source_paths(bdir, edp.graph.id), _r.angle_offsets(bdir))
    kinds = {kind for s in steps for kind in _r.brain_args(dict(getattr(s, "args", {}) or {})).values()}
    rows: list[tuple[str, float, float]] = []
    for kind, resolver in (("keep", _r._resolve_keep), ("cuts", _r._resolve_cuts)):
        if kind in kinds:
            rows += [(str(x["src"]), float(x["start"]), float(x["end"])) for d in resolver(cx) for x in d["ranges"]]
    if not _r.camera_plan_present(before, list(cx.offsets)):
        for s in steps:
            lit = dict(getattr(s, "args", {}) or {}).get("ranges")
            if getattr(s, "tool", None) == "cut_source_ranges" and isinstance(lit, list):
                rows += [(str(x["src"]), float(x["start"]), float(x["end"])) for x in lit
                         if isinstance(x, dict) and _num_ok(x)]
    return rows


def _num_ok(r: dict) -> bool:
    return isinstance(r.get("src"), str) and all(
        isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool) for k in ("start", "end"))


def planned_removal(before: EDL, edp: Any, bdir: Any, steps: list[Any]) -> tuple[float, int]:
    """`(timeline seconds the plan names, number of cuts)`, overlaps counted once."""
    from . import resolve as _r
    per_src: dict[str, list[tuple[float, float]]] = {}
    for src, a, b in _named_rows(before, edp, bdir, steps):
        per_src.setdefault(src, []).append((a, b))
    total, cuts = 0.0, 0
    for src, spans in per_src.items():
        for a, b in _r._merge(spans):
            cuts += 1
            total += sum(t1 - t0 for t0, t1 in source_range_to_timeline(before, "v1", a, b, src=src))
    return total, cuts


def named_removals(store: Any, plan: Any, before: EDL) -> list[tuple[str, float, float]] | None:
    """`(file, start, end)` in SOURCE seconds — every stretch a brain plan's own decisions remove (the cut
    ranges, the complement of the kept windows), resolved against the tree before the run; None when the plan is
    not a brain plan or its decisions cannot be read. `speech_preserved` reads it: the words a reel drops on
    purpose are not lost speech (closer review: 6042 "lost" words on a correct 45-minute trim)."""
    from ..agent.prompt.executor import plan_decisions_id
    from . import resolve as _r
    steps = list(getattr(plan, "steps", []) or [])
    did = plan_decisions_id(steps)
    if did is None:
        return None
    try:
        bdir = _r.brain_dir_for(store)
        edp = _r.load_edp(bdir, did)
        return None if edp is None else _named_rows(before, edp, bdir, steps)
    except (OSError, ValueError):
        return None


def removal_overrun(store: Any, plan: Any, result: Any) -> list[dict[str, str]]:
    """Reasons (executor `safety_net` shape) when a brain plan removed more
    picture than its decisions name; [] when it did not, or when the plan is
    not a brain plan / its decisions cannot be read (never blocks blind)."""
    from ..agent.prompt.executor import plan_decisions_id
    from . import resolve as _r
    steps = list(getattr(plan, "steps", []) or [])
    did = plan_decisions_id(steps)
    if did is None:
        return []
    try:
        bdir = _r.brain_dir_for(store)
        edp = _r.load_edp(bdir, did)
    except (OSError, ValueError):
        return []
    if edp is None:
        return []
    before: EDL = result.edl_before
    named, cuts = planned_removal(before, edp, bdir, steps)
    removed = before.video_extent() - store.edl.video_extent()
    tol = _tb.frame_duration(before.canvas.fps) * (cuts + 1) + 1e-3
    if removed <= named + tol:
        return []
    return [{"kind": "check", "clause": "removal_within_plan",
             "message": f"the run removed {removed:.1f} s of the picture but the plan's own decisions name "
                        f"{named:.1f} s ({cuts} cut{'' if cuts == 1 else 's'}); nothing was kept"}]


# --------------------------------------------------------------------------
# one_moment_once (closer: two cameras and no recorder)
# --------------------------------------------------------------------------

def _group_offsets(bdir: Any, steps: list[Any]) -> dict[str, float]:
    """`{file: sync offset}` of the angle group: angles.json plus what the plan's own camera and dialogue
    steps carry (a graph analysed before the reference angle joined its group lists one camera fewer)."""
    from . import resolve as _r
    out: dict[str, float] = dict(_r.angle_offsets(bdir))
    for s in steps:
        if getattr(s, "tool", None) in ("apply_camera_plan", "sync_dialogue_lane"):
            offs = (getattr(s, "args", {}) or {}).get("offsets")
            if isinstance(offs, dict):
                out.update({str(k): float(v) for k, v in offs.items() if isinstance(v, (int, float))})
    return out


def moment_played_twice(store: Any, plan: Any, result: Any) -> list[dict[str, str]]:
    """Reasons (executor `safety_net` shape, blocking) when the main lane of a brain plan plays the same stretch
    of the recording twice from two different cameras — the second camera left on the main lane after the
    first ("the conversation plays twice"). Each v1 piece of an angle-group file covers a stretch of the
    reference clock (`[in_ − offset, out − offset]`); two pieces of DIFFERENT files that overlap by more than
    a frame are the fault. One camera, and cameras that only ever take turns, never overlap. [] for a plan
    that is not a brain plan or has fewer than two cameras (never blocks blind)."""
    from ..agent.prompt.executor import plan_decisions_id
    from . import resolve as _r
    steps = list(getattr(plan, "steps", []) or [])
    if plan_decisions_id(steps) is None:
        return []
    try:
        offsets = _group_offsets(_r.brain_dir_for(store), steps)
    except (OSError, ValueError):
        return []
    edl: EDL = store.edl
    files = {p: o for p, o in offsets.items()}
    if len(files) < 2:
        return []
    spans: list[tuple[float, float, str]] = []
    for c in _media(edl, "v1"):
        if c.freeze is not None:
            continue
        key = next((p for p in files if _same_source(c.src, p)), None)
        if key is not None:
            spans.append((c.in_ - files[key], c.out - files[key], key))
    spans.sort()
    tol = _tb.frame_duration(edl.canvas.fps)
    for i, (a0, a1, fa) in enumerate(spans):
        for b0, b1, fb in spans[i + 1:]:
            if b0 >= a1 - tol:
                break
            if fb != fa and min(a1, b1) - b0 > tol:
                return [{"kind": "check", "clause": "one_moment_once",
                         "message": "the second camera was left on the main lane after the first, so the recording "
                                    f"would play twice (the same {min(a1, b1) - b0:.1f} s from two cameras)"}]
    return []


CHECKS = {"no_cut_mid_word": c_no_cut_mid_word, "dialogue_in_sync": c_dialogue_in_sync}


def install() -> None:
    """Register the checks in `verify.CHECKS` (idempotent)."""
    for name, fn in CHECKS.items():
        _verify.CHECKS.setdefault(name, fn)


install()      # importing this module (or agent.prompt.verify, which imports it) registers the checks


__all__ = ["DEFAULT_MID_WORD_TOL_S", "audio_lane", "cut_seams", "c_no_cut_mid_word", "c_dialogue_in_sync",
           "planned_removal", "named_removals", "removal_overrun", "moment_played_twice", "CHECKS", "install"]
