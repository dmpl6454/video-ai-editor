"""The benchmark cases (spec §6.2) — THE benchmark case format (§0.2: K owns
it; P's grammar tests consume `CASES[*].prompt` / `.intents`).

A `Case` is data plus two callables: `setup(ctx)` puts the cloned session
into the state the prompt is about (a prior prompt, a direct dispatch, a
deleted transcript) and `checks(ctx)` returns `Assertion`s computed by
`measure.py` from the persisted EDL / transcript / ffprobe / a 360p render —
never from the app's `verify` event, which the test asserts AGREES.

Twenty-six cases: the 24 of §6.2 (18 is the Basic-category transition case,
`crossdissolve`) plus two more transition cases — 25 (Zoom) and 26
(Glitch/Stylised) — so "all the transitions from CapCut" is measured across
three categories through the prompt, with the ops and a render whose
duration reflects the overlaps. Tiers: fast = 1–5, 9–18, 21, 22, 25, 26;
slow = 6, 7, 8, 19, 20, 23, 24.

Ground truth: `narration.py` (planted fillers/pauses at exact source
offsets), `media.py` (scene cuts; librosa's own beats on each bed). Whisper
text is never the oracle for a cut — its TIMING is what `captions_sync`
measures, and that is the product's own claim.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from video_ai_editor.agent.prompt.recipes import FILLERS_STRICT
from video_ai_editor.render.transitions import CATEGORIES, canonical

from . import measure as M
from .harness import BenchEnv, PromptRun, requirement_missing
from .measure import Assertion, check
from .narration import Narration

Tier = str          # "fast" | "slow"

#: Transition families the three transition cases assert on, from the
#: render catalog so a rename there is caught here.
BASIC_FAMILY = frozenset({"fade", "dissolve", "crossdissolve", "crossfade", "fadefast", "fadeslow"})
ZOOM_FAMILY = frozenset(CATEGORIES["zoom"])
GLITCH_FAMILY = frozenset(CATEGORIES["stylized"]) | frozenset(CATEGORIES["texture"])

#: ±1 frame at 30 fps (spec: beat splits land within a frame of a beat).
FRAME_S = 1 / 30

#: The render-clock probes of the transition cases (18/25/26): a full-frame
#: sticker plus a 1 kHz tone on the VO lane, one pair `PROBE_LEAD_S` before
#: the first seam and one pair `PROBE_LEAD_S` after it, each `PROBE_S` long.
#: 3 s clears any catalog transition's window on either side of a seam (the
#: longest is well under 2 s) while staying inside the neighbouring shot;
#: 0.5 s is 15 frames — long enough to find, short enough not to hide a
#: seam. Layout time is what the EDL stores; the render is asked where the
#: probe actually appears.
PROBE_LEAD_S = 3.0
PROBE_S = 0.5
PROBE_TONE_HZ = 1000.0
#: The tone must land within this of its expected onset (the tone's own
#: bandpass rise is ~20 ms; a transition's overlap is ≥ 100 ms).
PROBE_AUDIO_TOL_S = 0.05
#: Probe times snap OUTWARD to this grid (see `_probe_grid`).
PROBE_GRID_S = 0.1
#: Loudness tolerance, and the wider one docs/BENCHMARK.md explains.
LUFS_TOL = 1.0
LUFS_TOL_SHORT_CONTENT = 1.5


@dataclass
class CaseCtx:
    """What `setup`/`checks` see: the app, the case's own session, the
    ground truth, the pre-prompt snapshot and whatever setup stashed."""
    env: BenchEnv
    sid: str
    narration: Narration | None
    before: M.Snapshot
    run: PromptRun | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def session_dir(self) -> Path:
        return self.env.session_dir(self.sid)

    def edl(self):
        return M.load_edl(self.session_dir)

    def snapshot(self) -> M.Snapshot:
        return M.Snapshot.take(self.session_dir)

    def render(self) -> Path | None:
        return M.render(self.session_dir)


Setup = Callable[[CaseCtx], None]
Checks = Callable[[CaseCtx], list[Assertion]]


@dataclass(frozen=True)
class Case:
    id: int
    name: str
    prompt: str
    fixture: str
    tier: Tier
    parity: str                       # the CapCut feature this stands in for
    intents: tuple[str, ...]          # recipes the grammar must detect (P's fixture)
    checks: Checks
    setup: Setup | None = None
    marks: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()    # harness.requirement_missing keys
    allow_downloads: bool = False
    answers: dict[str, Any] | None = None
    expects_op: bool = True           # exactly one `op` event for a mutating case
    wall_max_s: float | None = None

    @property
    def slug(self) -> str:
        return f"{self.id:02d}_{self.name}"


# --------------------------------------------------------------------------
# shared assertion builders
# --------------------------------------------------------------------------

def _silences_removed(ctx: CaseCtx, edl, *, min_share: float = 0.7) -> list[Assertion]:
    assert ctx.narration is not None
    removed = ctx.before.duration - float(edl.duration)
    target = ctx.narration.planted_pause_seconds * min_share
    out = [check("duration_shrank_by_pauses", removed >= target, round(removed, 2),
                 f"≥ {target:.1f}", "seconds removed vs planted pause time")]
    path = ctx.render()
    if path is None:
        out.append(check("render_silence_leq", None, detail="timeline too long to render"))
    else:
        sil = M.render_silence(path)
        out.append(check("render_silence_leq", sil <= 1.0, sil, "≤ 1.0", "silencedetect on the 360p render"))
    return out


def _fillers_removed(ctx: CaseCtx, edl, *, min_gone: int = 8) -> list[Assertion]:
    assert ctx.narration is not None
    gone, total, rows = M.fillers_gone(edl, ctx.narration)
    like = M.content_like_survival(edl, ctx.narration)
    kept = M.sentences_survival(edl, ctx.narration)
    return [check("fillers_gone", gone >= min_gone, f"{gone}/{total}", f"≥ {min_gone}/{total}", "; ".join(rows)),
            check("content_like_survives", like >= 0.9, round(like, 2), "≥ 0.9",
                  "'I like this part' is a content word"),
            check("speech_preserved", kept >= 0.97, round(kept, 3), "≥ 0.97",
                  "mean survival of the content sentences' voiced spans")]


def _captions_present(ctx: CaseCtx, edl, *, style: str | None = "ig_chunky") -> list[Assertion]:
    assert ctx.narration is not None
    cues = M.caption_clips(edl)
    cover = M.caption_cover(edl, ctx.narration)
    ok_ext, last, extent = M.captions_within_extent(edl)
    out = [check("captions_nonempty", len(cues) >= 1, len(cues), "≥ 1"),
           check("captions_cover", cover >= 0.9, round(cover, 3), "≥ 0.9", "share of ground-truth speech (timeline) under a cue"),
           check("captions_within_extent", ok_ext, round(last, 2), f"≤ {extent:.2f}")]
    if style:
        out.append(check("captions_style", M.caption_style(edl) == style, M.caption_style(edl), style))
    return out


def _canvas(edl, w: int, h: int) -> Assertion:
    return check("canvas_size", (edl.canvas.w, edl.canvas.h) == (w, h), f"{edl.canvas.w}x{edl.canvas.h}", f"{w}x{h}")


def _no_letterbox(edl) -> Assertion:
    from video_ai_editor.agent.prompt.presets import aspect_of
    from video_ai_editor.ingest.probe import probe
    canvas = aspect_of(edl.canvas.w, edl.canvas.h)
    bad: list[str] = []
    for c in M.v1_clips(edl):
        if c.fit == "cover":
            continue
        v = probe(Path(c.src)).video
        if v is not None and v.width and v.height and aspect_of(v.width, v.height) != canvas:
            bad.append(f"{c.id}: {v.width}x{v.height} contain on {canvas}")
    return check("no_letterbox", not bad, len(bad), 0, "; ".join(bad))


def _safe_zone(edl) -> Assertion:
    bad = M.overlay_zone_violations(edl)
    return check("overlays_inside_safe_zone", not bad, len(bad), 0, "; ".join(bad[:6]))


def _hook(edl, *, max_start: float = 0.5) -> list[Assertion]:
    h = M.hook_clip(edl)
    return [check("hook_present", h is not None, None if h is None else h.text[:40], "a role=hook overlay"),
            check("hook_starts_early", h is not None and float(h.start) <= max_start,
                  None if h is None else round(float(h.start), 2), f"≤ {max_start}")]


def _music(edl, *, min_cover: float = 0.95) -> list[Assertion]:
    """Kept in step with the app's own `verify.c_music_covers` /
    `c_music_within_video_extent`: cover is the bed's span CLIPPED to the
    video extent (an overhanging bed must not score > 1 — that was exactly
    baseline gap 1, a bed fitted at add time and never re-fitted after cuts),
    and the last bed must end at the extent."""
    beds = M.music_clips(edl)
    extent = float(edl.video_extent())
    spans = M.merge_spans((float(c.start), min(extent, float(c.start) + float(c.effective_duration))) for c in beds)
    cover = (M.span_total(spans) / extent) if extent else 0.0
    last_end = max((float(c.start) + float(c.effective_duration) for c in beds), default=0.0)
    duck = M.music_duck_db(edl)
    return [check("music_present", len(beds) >= 1, len(beds), "≥ 1"),
            check("music_ducked", duck is not None and duck <= -12, duck, "≤ -12 dB"),
            check("music_covers", cover >= min_cover, round(cover, 3), f"≥ {min_cover}",
                  "bed span clipped to the video extent"),
            check("music_within_extent", bool(beds) and last_end <= extent + 0.05, round(last_end, 3),
                  f"≤ {extent:.2f}", "the bed must not outlast the video (baseline gap 1)")]


def _loudness(ctx: CaseCtx, edl, target: float, *, tol: float = LUFS_TOL) -> list[Assertion]:
    out = [check("loudness_target_set", edl.canvas.loudness_lufs == target, edl.canvas.loudness_lufs, target)]
    path = ctx.render()
    if path is None:
        return out + [check("render_loudness_within", None, detail="timeline too long to render")]
    lufs = M.render_loudness(path)
    ok = lufs is not None and abs(lufs - target) <= tol
    wide = lufs is not None and abs(lufs - target) <= LUFS_TOL_SHORT_CONTENT
    return out + [check("render_loudness_within", ok or wide, lufs, f"{target} ± {tol}",
                        "" if ok else ("within the ±1.5 short-content tolerance (docs/BENCHMARK.md)" if wide else ""))]


def _audit_ok(edl) -> Assertion:
    from video_ai_editor.show.audit import audit
    a = audit(edl)
    hook = int(a.get("hook", {}).get("hook_score", 0))
    errors = [i for i in a.get("issues", []) if i.get("level") == "error"]
    return check("audit_ok", bool(a.get("ok")) and not errors and hook == 3,
                 f"ok={a.get('ok')} errors={len(errors)} hook={hook} score={a.get('score')}",
                 "ok, no errors, hook_score 3", "the numeric score is reported, not gated")


def _one_op(ctx: CaseCtx) -> Assertion:
    after = ctx.snapshot()
    return check("one_op", after.ops == ctx.before.ops + 1, after.ops - ctx.before.ops, 1, "ops.json entries added")


def _transition_family(edl, family: frozenset[str], *, min_count: int, label: str) -> list[Assertion]:
    trs = M.transitions(edl)
    kinds = sorted({canonical(t.type) for t in trs})
    outside = [t.type for t in trs if canonical(t.type) not in family]
    return [check("transitions_count_geq", len(trs) >= min_count, len(trs), f"≥ {min_count}"),
            check(f"transitions_in_{label}_family", bool(trs) and not outside, kinds, sorted(family)[:8],
                  f"outside: {outside}" if outside else "")]


def _render_reflects_overlaps(ctx: CaseCtx, edl) -> list[Assertion]:
    """The compositor really overlaps the clips: the AUDIO stream (sample-
    exact) of the transitioned render is shorter than the un-transitioned
    render of the same clone by ≥ 80% of the EDL overlap, and lands within a
    frame (+20 ms) of extent − overlap. The container duration is frame-
    quantised (case 26: 84.800 vs audio 84.734), so with a 0.2 s tolerance a
    0.3 s glitch left 0.13 s of margin and a ≤ 0.2 s transition was invisible."""
    expected, overlap = M.expected_render_duration(edl)
    path = ctx.render()
    if path is None:
        return [check("render_duration_reflects_overlaps", None, detail="timeline too long to render")]
    got = M.audio_duration(path)
    fps = float(edl.canvas.fps or 30)
    tol = 1.0 / fps + 0.02
    # Every applied transition must cost at least the catalog's shortest
    # duration (0.1 s); a single 0.3 s glitch is a real overlap too.
    floor = 0.1 * max(1, len(M.transitions(edl)))
    out = [check("transitions_overlap_positive", overlap >= floor, round(overlap, 3), f"≥ {floor:.1f} s",
                 "Σ min(duration, neighbours) over the seams"),
           check("render_duration_reflects_overlaps", abs(got - expected) <= tol, round(got, 3),
                 f"{expected:.3f} ± {tol:.3f}", "audio-stream duration of the 360p render vs extent − overlap"),
           check("edl_duration_agrees", abs(float(edl.duration) - expected) <= 0.05, float(edl.duration),
                 round(expected, 3))]
    before = ctx.extra.get("render_before")
    if before is None:
        out.append(check("render_shorter_than_untransitioned", None, detail="no pre-transition render in setup"))
    else:
        shrink = M.audio_duration(before) - got
        out.append(check("render_shorter_than_untransitioned", shrink >= 0.8 * overlap, round(shrink, 3),
                         f"≥ {0.8 * overlap:.3f}", "the transitioned render is shorter than the same clone rendered before"))
    return out


def _window_ok(got: tuple[float, float] | None, expected: tuple[float, float], tol: float) -> bool:
    return got is not None and abs(got[0] - expected[0]) <= tol and abs(got[1] - expected[1]) <= tol


def _seam_table_agrees(edl) -> Assertion:
    """The benchmark's restated seam rule (`M.seam_overlaps`) and the
    product's `EDL.v1_seam_table()` — which `render/clock.py` walks — name
    the same seams with the same cost. Every render-clock expectation below
    is computed from the benchmark's copy, so a disagreement here means the
    expectations are suspect, not the renderer; it is reported by name
    rather than left to surface as a mysterious probe lag."""
    ours = [(round(a, 3), round(b, 3)) for a, b in M.seam_overlaps(edl)]
    theirs = [(round(a, 3), round(b, 3)) for a, b in edl.v1_seam_table()]
    return check("seam_table_agrees", ours == theirs, theirs, ours,
                 "EDL.v1_seam_table() vs the benchmark's own seam rule (seam, seconds consumed)")


def _fmt_window(w: tuple[float, float] | None) -> str:
    return "not found" if w is None else f"[{w[0]:.3f}, {w[1]:.3f})"


def _probe_windows(path: Path, edl, *, t: float, dur: float, fps: float,
                   levels: list[tuple[float, float]]) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """(sticker window, tone window) found in the render for the probe pair
    laid at layout `t`. The search spans from the render-clock position to
    the layout position (plus half a second each side), so a probe that
    plays at LAYOUT time is still found and reported with its real lag
    rather than as 'not found'."""
    lo = min(M.render_time(edl, t), t) - 0.5
    hi = max(M.render_time(edl, t), t) + dur + 0.5
    return (M.overlay_window(path, lo=lo, hi=hi, fps=fps),
            M.tone_window(levels, lo=lo, hi=hi))


def _render_clock(ctx: CaseCtx, edl) -> list[Assertion]:
    """The transition cases' RENDER-level sync check. `captions_relaid_
    without_drift` and `render_duration_reflects_overlaps` compare layout
    coordinates with layout coordinates and were blind to the renderer
    playing every overlay lane late: the v1 lane is pulled left by each
    cross-fade's overlap (clip B starts `d` early), while text, stickers,
    PiP and the VO/music beds were positioned at raw layout time. Two probe
    pairs planted in setup (`_plant_render_clock_probes`) answer it from the
    pixels and the samples: the pair AFTER the first seam must appear at
    `render_time(t) = t − overlap` (within a frame / 0.05 s), the pair BEFORE
    it must not move at all, and — so a probe that never renders cannot
    pass vacuously — both must have been visible at layout time in the
    untransitioned render setup kept."""
    probes = ctx.extra.get("render_clock_probes")
    names = ("overlay_follows_render_clock", "audio_follows_render_clock",
             "probes_before_first_seam_unmoved", "probes_visible_before_transitions")
    table = _seam_table_agrees(edl)
    if not probes:
        return [table] + [check(n, None, detail="no render-clock probes planted in setup") for n in names]
    path = ctx.render()
    if path is None:
        return [table] + [check(n, None, detail="timeline too long to render") for n in names]
    fps = float(edl.canvas.fps or 30)
    # One frame (+5 ms of float slack). It also absorbs what the renderer
    # itself does at a window's edges: `enable=between(t,a,b)` is inclusive
    # at `b`, so a sticker's last frame is the one AT `end` (measured
    # [2.0, 2.533) for a box laid on [2.0, 2.5)), and a render time that
    # falls between frames starts on the next one.
    frame_tol = 1.0 / fps + 0.005
    pre, post, dur, seam = probes["pre"], probes["post"], probes["dur"], probes["seam"]
    exp_post = (M.render_time(edl, post), M.render_time(edl, post + dur))
    exp_pre = (M.render_time(edl, pre), M.render_time(edl, pre + dur))
    consumed = post - exp_post[0]
    levels = M.band_levels(path, freq_hz=PROBE_TONE_HZ)
    post_v, post_a = _probe_windows(path, edl, t=post, dur=dur, fps=fps, levels=levels)
    pre_v, pre_a = _probe_windows(path, edl, t=pre, dur=dur, fps=fps, levels=levels)

    def _lag(got: tuple[float, float] | None, exp: tuple[float, float]) -> str:
        return "" if got is None else f"lag {got[0] - exp[0]:+.3f} s vs the picture"

    where = f"layout {post:.2f} s; {consumed:.3f} s consumed by the seam at {seam:.2f} s"
    out = [table,
           check("overlay_follows_render_clock", _window_ok(post_v, exp_post, frame_tol), _fmt_window(post_v),
                 f"{_fmt_window(exp_post)} ± {frame_tol:.3f}", f"sticker {where}; {_lag(post_v, exp_post)}"),
           check("audio_follows_render_clock", _window_ok(post_a, exp_post, PROBE_AUDIO_TOL_S), _fmt_window(post_a),
                 f"{_fmt_window(exp_post)} ± {PROBE_AUDIO_TOL_S:.2f}", f"1 kHz tone on the VO lane {where}; {_lag(post_a, exp_post)}"),
           check("probes_before_first_seam_unmoved",
                 _window_ok(pre_v, exp_pre, frame_tol) and _window_ok(pre_a, exp_pre, PROBE_AUDIO_TOL_S),
                 f"sticker {_fmt_window(pre_v)}, tone {_fmt_window(pre_a)}", f"both {_fmt_window(exp_pre)}",
                 f"layout {pre:.2f} s lies before every seam, so its render time is its layout time")]
    before = ctx.extra.get("render_before")
    if before is None:
        out.append(check(names[3], None, detail="no pre-transition render in setup"))
        return out
    # No transitions yet when `before` was rendered: layout time IS render
    # time, so both windows must sit at the layout position exactly.
    lv_before = M.band_levels(before, freq_hz=PROBE_TONE_HZ)
    v0 = M.overlay_window(before, lo=post - 0.5, hi=post + dur + 0.5, fps=fps)
    a0 = M.tone_window(lv_before, lo=post - 0.5, hi=post + dur + 0.5)
    layout = (post, post + dur)
    out.append(check(names[3], _window_ok(v0, layout, frame_tol) and _window_ok(a0, layout, PROBE_AUDIO_TOL_S),
                     f"sticker {_fmt_window(v0)}, tone {_fmt_window(a0)}", f"both {_fmt_window(layout)}",
                     "the same probes at layout time in the untransitioned render (guards the checks above against a probe that never renders)"))
    return out


# --------------------------------------------------------------------------
# setups
# --------------------------------------------------------------------------

def _prior_prompt(prompt: str, **kw: Any) -> Setup:
    def _run(ctx: CaseCtx) -> None:
        run = ctx.env.run_prompt(ctx.sid, prompt, **kw)
        if run.errors:
            raise RuntimeError(f"setup prompt {prompt!r} failed: {run.errors}")
        ctx.extra.setdefault("setup_runs", []).append(run)
    return _run


def _prior_dispatch(tool: str, **args: Any) -> Setup:
    def _run(ctx: CaseCtx) -> None:
        ctx.env.dispatch(ctx.sid, tool, args)
    return _run


def _chain(*steps: Setup) -> Setup:
    def _run(ctx: CaseCtx) -> None:
        for s in steps:
            s(ctx)
    return _run


def _delete_transcript(ctx: CaseCtx) -> None:
    ctx.env.delete_transcript(ctx.sid)


def _pending_transcript(ctx: CaseCtx) -> None:
    ctx.extra["writer"] = ctx.env.simulate_upload_transcript(ctx.sid, delay_s=3.0)


def _remember_render(ctx: CaseCtx) -> None:
    """Case 10: the unsplit render and the pre-split seams, for the frame
    diff and the "new boundaries" delta."""
    path = ctx.render()
    ctx.extra["render_before"] = path
    ctx.extra["boundaries_before"] = M.v1_boundaries(ctx.edl())


def _plant_render_clock_probes(ctx: CaseCtx) -> None:
    """Cases 18/25/26: two probe pairs through the real dispatch route — a
    canvas-covering magenta sticker (the `stickers` lane bakes through the
    same `overlay=enable=between(t,…)` window as text and captions; a
    TextClip has no opaque background box, so a full frame of one colour is
    what a 1×1 area-scaled frame can recognise) and a 1 kHz tone as a clip on
    the VO lane (the same `adelay=start` path as a voiceover or a bed) —
    `PROBE_LEAD_S` before and after the FIRST seam. Must run before
    `_remember_render` so the untransitioned render carries them too.

    The two stickers sit 1 px apart on purpose: `add_sticker` cascades an
    EXACT position collision 3 % of the canvas down-right (so stacked
    stickers stay selectable), which would leave a scene-coloured strip along
    two edges of the second probe; 1 px of offset is below its 1 px collision
    threshold and invisible in a frame mean."""
    edl = ctx.edl()
    seams = M.v1_boundaries(edl)
    if len(seams) < 2:
        raise RuntimeError(f"render-clock probes need at least two v1 seams, found {seams}")
    seam, nxt = seams[0], seams[1]
    pre, post = _probe_grid(seam - PROBE_LEAD_S, up=False), _probe_grid(seam + PROBE_LEAD_S, up=True)
    if pre < 0.5 or post + PROBE_S + PROBE_LEAD_S > nxt:
        raise RuntimeError(f"seams {seam:.2f}/{nxt:.2f} s leave no room for {PROBE_LEAD_S} s probes either side")
    canvas = edl.canvas
    png = M.write_probe_sticker(ctx.session_dir / "uploads" / "stickers" / "render_clock_probe.png", canvas.w, canvas.h)
    wav = M.write_probe_tone(ctx.session_dir / "uploads" / "render_clock_probe_1khz.wav",
                             freq_hz=PROBE_TONE_HZ, seconds=PROBE_S)
    for i, t in enumerate((pre, post)):
        ctx.env.dispatch(ctx.sid, "add_sticker", {"src": str(png), "start": t, "end": t + PROBE_S,
                                                  "position": [canvas.w / 2 + i, canvas.h / 2],
                                                  "scale": M.PROBE_STICKER_SCALE})
        ctx.env.dispatch(ctx.sid, "add_clip", {"track": "vo", "src": str(wav), "in": 0.0, "out": PROBE_S, "start": t})
    ctx.extra["render_clock_probes"] = {"pre": pre, "post": post, "dur": PROBE_S, "seam": seam}


def _probe_grid(t: float, *, up: bool) -> float:
    """`t` snapped away from the seam to the `PROBE_GRID_S` grid (down for
    the pre-seam probe, up for the post-seam one). The renderer writes
    overlay windows as `enable='between(t,{start:.3f},…)'` and audio
    offsets as whole milliseconds; the fixture's first seam is 11.0667 s, so
    a probe at seam − 3 = 8.0667 s would be written as 8.067 — PAST the
    8.0667 frame — and its first visible frame would be the next one: a
    one-frame lag the renderer did not cause (measured: sticker at 8.100 for
    a layout start of 8.067). A multiple of 0.1 s is exact in three decimals
    and lands on a frame at the fixture's 30 fps (3 frames), so a probe's
    layout window IS a frame window and the ±1-frame tolerance is spent on
    the renderer, not on formatting."""
    steps = t / PROBE_GRID_S
    snapped = math.ceil(steps - 1e-6) if up else math.floor(steps + 1e-6)   # float noise never crosses a step
    return round(snapped * PROBE_GRID_S, 3)


def _remember_hash(ctx: CaseCtx) -> None:
    ctx.extra["hash_before_setup"] = M.edl_hash(ctx.session_dir)
    ctx.extra["ops_before_setup"] = len(M.load_ops(ctx.session_dir))


def _remember_caption_deltas(ctx: CaseCtx) -> None:
    """Case 18: the per-seam cue-vs-word offsets BEFORE the transitions, so
    the case measures drift (what the re-lay changed), not whisper's
    segmentation (which it cannot change)."""
    ctx.extra["caption_deltas_before"] = M.caption_seam_deltas(ctx.session_dir, ctx.edl())


# --------------------------------------------------------------------------
# checks per case
# --------------------------------------------------------------------------

def c01(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    run = ctx.run
    assert run is not None
    return _captions_present(ctx, edl) + [
        check("no_auto_caption_step", not run.ran("auto_caption") and not run.tool_uses("auto_caption"),
              [s["tool"] for s in run.steps("ok")], "add_caption_track from the persisted transcript"),
        check("wall_under_10s", run.wall_s < 10.0, round(run.wall_s, 1), "< 10 s")]


def c02(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    text = M.captions_text(edl)
    latin = M.script_ratio(text, block=M.LATIN)
    return [check("captions_nonempty", len(M.caption_clips(edl)) >= 1, len(M.caption_clips(edl)), "≥ 1"),
            check("captions_latin_ratio", latin >= 0.9, round(latin, 3), "≥ 0.9", "hinglish = romanised"),
            check("captions_style", M.caption_style(edl) == "ig_chunky", M.caption_style(edl), "ig_chunky")]


def c03(ctx: CaseCtx) -> list[Assertion]:
    return _silences_removed(ctx, ctx.edl())


def c04(ctx: CaseCtx) -> list[Assertion]:
    return _fillers_removed(ctx, ctx.edl())


def c05(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    run = ctx.run
    assert run is not None
    transcribes = [s for s in run.steps("ok") if s["tool"] == "transcribe"]
    return (_silences_removed(ctx, edl) + _fillers_removed(ctx, edl) + _captions_present(ctx, edl, style=None) +
            [check("one_transcription_step", len(transcribes) == 1, len(transcribes), 1, "the prerequisite `transcribe`"),
             check("no_auto_caption_step", not run.tool_uses("auto_caption"), len(run.tool_uses("auto_caption")), 0)])


def c06(ctx: CaseCtx) -> list[Assertion]:
    run = ctx.run
    assert run is not None
    results = [e.get("result") or {} for e in run.of_type("tool_result") if e.get("name") == "make_shorts"]
    sessions = [s for r in results for s in (r.get("new_sessions") or [])]
    out = [check("shorts_created", len(sessions) == 3, len(sessions), 3), _one_op(ctx)]
    for sid in sessions:
        d = ctx.env.session_dir(sid)
        if not (d / "edl.json").exists():
            out.append(check(f"child_{sid}_exists", False, "missing", "edl.json"))
            continue
        child = M.load_edl(d)
        dur = float(child.duration)
        out += [check(f"child_{sid}_duration", 11.5 <= dur <= 30.5, round(dur, 2), "[11.5, 30.5]"),
                check(f"child_{sid}_vertical", (child.canvas.w, child.canvas.h) == (1080, 1920),
                      f"{child.canvas.w}x{child.canvas.h}", "1080x1920"),
                check(f"child_{sid}_captions", len(M.caption_clips(child)) >= 1, len(M.caption_clips(child)), "≥ 1"),
                check(f"child_{sid}_hook", M.hook_clip(child) is not None, M.hook_clip(child) is not None, True)]
    return out


def c07(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    return [_canvas(edl, 1080, 1920), _no_letterbox(edl), _safe_zone(edl),
            check("captions_kept", len(M.caption_clips(edl)) >= 1, len(M.caption_clips(edl)), "≥ 1",
                  "laid before the reframe by the case setup")]


def c08(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    return [_canvas(edl, 1080, 1920), _no_letterbox(edl),
            check("bitrate_kbps", edl.canvas.bitrate_kbps == 8000, edl.canvas.bitrate_kbps, 8000),
            check("loudness_target_set", edl.canvas.loudness_lufs == -16, edl.canvas.loudness_lufs, -16)]


def c09(ctx: CaseCtx) -> list[Assertion]:
    return _music(ctx.edl())


def c10(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    before = set(ctx.extra.get("boundaries_before") or ())
    now = M.v1_boundaries(edl)
    new = [b for b in now if all(abs(b - o) > 0.02 for o in before)]
    beats, period = M.bed_beats_on_timeline(edl)
    frac, off = M.beat_alignment(new, beats, period, tol=FRAME_S)
    pulses = M.pulses_on_beats(edl, beats)
    bad_words = M.split_inside_word(edl, ctx.session_dir, new)
    out = [check("new_boundaries", len(new) >= 8, len(new), "≥ 8"),
           check("boundaries_on_beats", frac >= 0.8, round(frac, 2), "≥ 0.8", f"off-beat: {[round(b, 3) for b in off[:6]]}"),
           check("pulses_on_fragments", pulses >= 8, pulses, "≥ 8", "scale keyframes starting on a detected beat"),
           check("min_shot", M.min_shot(edl) >= 0.8, round(M.min_shot(edl), 3), "≥ 0.8"),
           check("no_split_inside_word", not bad_words, len(bad_words), 0, "; ".join(bad_words[:4]))]
    before_path, after_path = ctx.extra.get("render_before"), ctx.render()
    if before_path is None or after_path is None or len(new) < 2:
        out.append(check("frames_differ_at_beats", None, detail="renders unavailable or fewer than two new seams"))
    else:
        differing = sum(1 for b in new[:2] if M.frame_hash(before_path, b + 0.2) != M.frame_hash(after_path, b + 0.2))
        out.append(check("frames_differ_at_beats", differing == 2, differing, 2,
                         "the punch-in changes real pixels 0.2 s after two new seams"))
    return out


#: The pre-0.7.0 canned rotation (baseline finding 3) — a hook equal to one of
#: these is content-blind whatever the reply claims.
_CANNED_HOOKS = ("THIS MISTAKE COSTS MOST PEOPLE EVERYTHING", "WATCH THIS BEFORE YOU SCROLL", "WAIT FOR IT",
                 "THEY LIED TO YOU", "DON'T SCROLL", "WATCH THIS UNTIL THE END")
_HOOK_STOP = frozenset({"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "is", "are", "was",
                        "were", "it", "its", "this", "that", "you", "your", "with", "from", "but", "so"})


def c11(ctx: CaseCtx) -> list[Assertion]:
    run = ctx.run
    assert run is not None
    assert ctx.narration is not None
    uses = run.tool_uses("apply_hook_stack")
    texts = [str((u.get("args") or {}).get("text") or "").strip() for u in uses]
    explicit = bool(uses) and all(texts)
    # From the transcript, not from a can: ≥ 2 content tokens of the hook
    # occur in the ground-truth first two sentences, and the line is not one
    # of the canned templates. A regression to the old rotation passed every
    # earlier assertion of this case (hook present, early, text explicit).
    head = " ".join(s.text for s in ctx.narration.sentences[:2]).lower()
    head_tokens = {t.strip(".,!?;:'\"") for t in head.split()}
    hook = texts[-1] if texts else ""
    content = [t.strip(".,!?;:'\"").lower() for t in hook.split()]
    content = [t for t in content if len(t) >= 3 and t not in _HOOK_STOP and t not in FILLERS_STRICT]
    hits = [t for t in content if t in head_tokens]
    reply = (run.reply or "").lower()
    names_source = any(k in reply for k in ("heuristic", "hook text", "text by", "yours", "written by"))
    return _hook(ctx.edl()) + [
        check("hook_text_explicit", explicit, texts, "apply_hook_stack received `text`", "the handler never calls generate_hook"),
        check("hook_from_transcript", len(hits) >= 2 and hook.upper() not in _CANNED_HOOKS, hits,
              "≥ 2 content words from the first two sentences", f"hook: {hook!r}"),
        check("reply_names_hook_source", names_source, run.reply[:120], "the reply says who wrote the hook (spec §2.6)")]


def c12(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    clips = M.v1_clips(edl)
    rows: list[str] = []
    for c in clips:
        luts = [p for p in M.lut_effects(c) if str(p.get("src", "")).endswith("teal_orange.cube")
                and 0.6 <= float(p.get("intensity", 0)) <= 1.0]
        if not luts:
            rows.append(f"{c.id}: {M.lut_effects(c) or 'no lut'}")
    return [check("fragments_exist", len(clips) >= 2, len(clips), "≥ 2", "silence cut first so fragments exist"),
            check("lut_on_every_clip", not rows, len(clips) - len(rows), len(clips), "; ".join(rows[:4]))]


def c13(ctx: CaseCtx) -> list[Assertion]:
    run = ctx.run
    assert run is not None
    edl = ctx.edl()
    if run.ran("noise_reduce"):
        state = "ok"
    elif any(s["tool"] == "noise_reduce" and (s.get("error") or s.get("summary")) for s in run.steps("skipped")):
        state = "skipped_with_reason"
    elif "noise_reduce" not in {s["tool"] for s in run.steps()}:
        state = "unavailable_not_planned"
    else:
        state = "skipped_silently"
    return _loudness(ctx, edl, -14.0) + [check("noise_reduce_honest", state != "skipped_silently", state,
                                                 "ok | skipped with a reason | feature-gated out of the plan")]


def c14(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    clips = M.v1_clips(edl)
    factors = sorted({round(c.speed_factor, 3) for c in clips})
    expected = ctx.before.duration / 1.5
    return [check("speed_on_every_clip", factors == [1.5], factors, [1.5]),
            check("duration_scaled", abs(float(edl.duration) - expected) <= 0.05 * expected,
                  round(float(edl.duration), 2), f"{expected:.2f} ± 5%")]


def c15(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    expected = ctx.before.duration - 5.0
    first = M.first_kept_source_time(edl)
    return [check("duration_trimmed", abs(float(edl.duration) - expected) <= 0.1, round(float(edl.duration), 3),
                  f"{expected:.2f} ± 0.1"),
            check("first_kept_source_time", first is not None and first >= 5.0, first, "≥ 5.0")]


def c16(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    lower = [c for c in M.text_clips(edl, "lower_third")]
    named = [c for c in lower if "Priya Sharma" in c.text]
    handle = any("@priya.codes" in c.text for c in M.text_clips(edl))
    return [check("lower_third_named", bool(named), [c.text for c in lower][:2], "contains 'Priya Sharma'"),
            check("lower_third_starts_at_start", bool(named) and float(named[0].start) <= 1.0,
                  None if not named else round(float(named[0].start), 2), "≤ 1.0"),
            check("handle_present", handle, handle, True), _safe_zone(edl)]


def c17(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    handle = "@quicksolutions.in"
    kit = edl.brand_kit.handle if edl.brand_kit else None
    watermarks = M.text_clips(edl, "watermark")
    tail = [c for c in M.text_clips(edl) if c.role != "watermark" and handle in c.text
            and float(c.start) >= float(edl.duration) - 3.5]
    return [check("brand_kit_handle", kit == handle, kit, handle),
            check("watermark_present", bool(watermarks), len(watermarks), "≥ 1"),
            check("exactly_one_end_card", len(tail) == 1, len(tail), 1, "handle text in the last 3.5 s")]


def c18(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    ok_ext, last, extent = M.captions_within_extent(edl)
    before = ctx.extra.get("caption_deltas_before") or []
    after = M.caption_seam_deltas(ctx.session_dir, edl)
    pairs = [(b, a) for b, a in zip(before, after) if b is not None and a is not None]
    drift = max((abs(a - b) for b, a in pairs), default=0.0)
    rows = [f"seam {i}: {b:+.3f} → {a:+.3f}" for i, (b, a) in enumerate(pairs) if abs(a - b) > 0.1]
    return (_transition_family(edl, BASIC_FAMILY, min_count=5, label="basic") +
            [check("captions_within_extent", ok_ext, round(last, 2), f"≤ {extent:.2f}", "captions re-laid after the seams moved"),
             check("captions_relaid_without_drift", bool(pairs) and drift <= 0.1, round(drift, 3), "≤ 0.1 s drift per seam",
                   "; ".join(rows) or f"{len(pairs)} seams compared: cue-vs-word offset unchanged by the re-lay")] +
            _render_reflects_overlaps(ctx, edl) + _render_clock(ctx, edl))


def c19(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    return ([_canvas(edl, 1920, 1080)] + _captions_present(ctx, edl, style=None) +
            _loudness(ctx, edl, -14.0) + _hook(edl) + [_audit_ok(edl)])


def c20(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    run = ctx.run
    assert run is not None
    out = ([_canvas(edl, 1080, 1920), _no_letterbox(edl)] + _music(edl) + _hook(edl) +
           [check("loudness_target_set", edl.canvas.loudness_lufs == -16, edl.canvas.loudness_lufs, -16),
            _audit_ok(edl), _one_op(ctx), _safe_zone(edl)])
    text = M.captions_text(edl)
    if requirement_missing("madlad") is None:
        # The translation model is cached, so no download question arose and
        # the captions must really be Hindi (the spec's second variant).
        dev = M.script_ratio(text, block=M.DEVANAGARI)
        out.append(check("captions_devanagari", dev >= 0.7, round(dev, 3), "≥ 0.7", "MADLAD cached on this machine"))
    else:
        reply = run.reply.lower()
        out += [check("captions_as_spoken", M.script_ratio(text, block=M.LATIN) >= 0.9 and bool(text),
                      round(M.script_ratio(text, block=M.LATIN), 3), "≥ 0.9 Latin (English as spoken)"),
                check("reply_says_fallback", any(k in reply for k in ("download", "as spoken", "as-spoken", "skipped", "english")),
                      run.reply[:160], "the reply says the translation was skipped")]
    h = ctx.snapshot().hash
    ctx.env.dispatch(ctx.sid, "undo", {})
    restored = M.edl_hash(ctx.session_dir) == ctx.before.hash
    out.append(check("undo_restores_hash", restored, M.edl_hash(ctx.session_dir)[:12], ctx.before.hash[:12],
                     f"one undo step from {h[:12]}"))
    return out


def c21(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    vos = M.vo_clips(edl)
    start = float(vos[-1].start) if vos else None
    wav = M.probe_duration(Path(vos[-1].src)) if vos and Path(vos[-1].src).exists() else 0.0
    return [check("vo_present", bool(vos), len(vos), "≥ 1"),
            check("vo_at_end", start is not None and start >= float(edl.duration) - 4.0, start, f"≥ {float(edl.duration) - 4:.1f}"),
            check("vo_audio_real", wav > 0.5, round(wav, 2), "> 0.5 s")]


def c22(ctx: CaseCtx) -> list[Assertion]:
    run = ctx.run
    assert run is not None
    h = M.edl_hash(ctx.session_dir)
    ops = len(M.load_ops(ctx.session_dir))
    return [check("hash_equals_pre_prompt", h == ctx.extra["hash_before_setup"], h[:12], ctx.extra["hash_before_setup"][:12],
                  "the caption op from the setup prompt is undone"),
            check("ops_rolled_back", ops == ctx.extra["ops_before_setup"], ops, ctx.extra["ops_before_setup"]),
            check("plan_intent_undo", (run.plan or {}).get("intent") == "undo", (run.plan or {}).get("intent"), "undo")]


def c23(ctx: CaseCtx) -> list[Assertion]:
    run = ctx.run
    assert run is not None
    first = run.turns[0] if run.turns else []
    clarify_idx = next((i for i, e in enumerate(first) if e.get("type") == "clarify"), None)
    step_idx = next((i for i, e in enumerate(first) if e.get("type") == "step"), None)
    go = any(q.get("key") == "go" for c in run.clarifies for q in c.get("questions", []))
    est = float((run.plans[0] if run.plans else {}).get("estimated_seconds") or 0)
    return [check("go_question_asked", go, go, True),
            check("estimated_over_90s", est > 90, round(est, 1), "> 90"),
            check("asked_before_any_step", clarify_idx is not None and (step_idx is None or clarify_idx < step_idx),
                  (clarify_idx, step_idx), "clarify precedes step"),
            check("edl_unchanged_after_no", M.edl_hash(ctx.session_dir) == ctx.before.hash, M.edl_hash(ctx.session_dir)[:12],
                  ctx.before.hash[:12])]


def c24(ctx: CaseCtx) -> list[Assertion]:
    run = ctx.run
    assert run is not None
    writer = ctx.extra.get("writer")
    if writer is not None:
        writer.join(timeout=10)
    edl = ctx.edl()
    waited = any(s.get("tool") == "transcribe" and "wait" in str(s.get("summary", "")).lower()
                 for s in run.of_type("step"))
    return _fillers_removed(ctx, edl) + [
        check("waited_for_upload_transcript", waited or not run.ran("transcribe"), waited,
              "waited (or dropped the step once the upload transcript landed)"),
        check("no_caption_track", not M.caption_clips(edl), len(M.caption_clips(edl)), 0)]


def c25(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    return (_transition_family(edl, ZOOM_FAMILY, min_count=5, label="zoom") + _render_reflects_overlaps(ctx, edl) +
            _render_clock(ctx, edl) + [_one_op(ctx)])


def c26(ctx: CaseCtx) -> list[Assertion]:
    edl = ctx.edl()
    trs = M.transitions(edl)
    first_seam = ctx.before.v1_boundaries[0] if ctx.before.v1_boundaries else None
    at_hook = first_seam is not None and any(abs(t.at - first_seam) < 0.05 for t in trs)
    return (_transition_family(edl, GLITCH_FAMILY, min_count=1, label="glitch") +
            [check("transition_at_first_seam", at_hook, [round(t.at, 2) for t in trs], first_seam,
                   "'at the hook' = the opening seam")] + _render_reflects_overlaps(ctx, edl) +
            _render_clock(ctx, edl) + [_one_op(ctx)])


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

CASES: tuple[Case, ...] = (
    Case(1, "captions", "add captions", "en_16x9", "fast", "Auto captions", ("captions",), c01, wall_max_s=10.0),
    Case(2, "captions_hinglish", "add chunky captions in hinglish", "hi_16x9", "fast", "Captions + translate",
         ("captions",), c02, marks=("tts",), requires=("hindi_voice", "madlad")),
    Case(3, "remove_silences", "remove the silences", "en_16x9", "fast", "Remove silence", ("remove_silences",), c03),
    Case(4, "remove_fillers", "cut out the ums", "en_16x9", "fast", "Filler removal (parity+)", ("remove_fillers",), c04),
    Case(5, "tighten_captions", "tighten it up and add captions", "en_16x9", "fast", "Smart cut",
         ("tighten", "captions"), c05, setup=_delete_transcript),
    Case(6, "shorts", "make 3 shorts under 30 seconds for tiktok", "en_16x9", "slow", "Auto shorts", ("shorts",), c06,
         marks=("slow",)),
    Case(7, "reframe_reels", "make it vertical for reels", "en_16x9", "slow", "Auto reframe", ("reframe",), c07,
         setup=_prior_prompt("add captions"), marks=("slow",)),
    Case(8, "tiktok_preset", "turn this into a tiktok", "en_16x9", "slow", "Export preset (+implied reframe)",
         ("export_preset",), c08, marks=("slow",)),
    Case(9, "music_duck", "add chill background music and duck it under my voice", "en_16x9", "fast",
         "Music + ducking", ("music", "duck"), c09),
    Case(10, "beat_sync", "cut to the beat of the music", "en_16x9", "fast", "Beat sync", ("beat_sync",), c10,
         setup=_chain(_prior_prompt("add chill background music and duck it under my voice"), _remember_render)),
    Case(11, "hook", "add a hook in the first 3 seconds", "en_16x9", "fast", "Hook", ("hook",), c11),
    Case(12, "cinematic_look", "give it a cinematic look", "en_16x9", "fast", "LUT", ("color_look",), c12,
         setup=_prior_dispatch("remove_silences", track="v1")),
    Case(13, "clean_audio_lufs", "clean up the audio and normalize to -14 LUFS", "en_16x9", "fast", "Audio enhance",
         ("clean_audio", "loudness"), c13),
    Case(14, "speed", "speed it up 1.5x", "en_16x9", "fast", "Speed", ("speed",), c14),
    Case(15, "trim", "cut the first 5 seconds", "en_16x9", "fast", "Trim", ("trim",), c15),
    Case(16, "lower_third", "add a lower third for Priya Sharma @priya.codes at the start", "en_16x9", "fast",
         "Lower thirds", ("title",), c16),
    Case(17, "brand_kit", "apply my brand kit @quicksolutions.in with #techtips and add an end card", "en_16x9",
         "fast", "Brand", ("brand", "end_card"), c17),
    Case(18, "transitions_smooth", "add smooth transitions between the clips", "presplit_16x9", "fast", "Transitions (Basic)",
         ("transitions",), c18, setup=_chain(_prior_dispatch("add_caption_track", style="ig_chunky", position="bottom"),
                                             _remember_caption_deltas, _plant_render_clock_probes, _remember_render)),
    Case(19, "youtube_auto_edit", "make it good for youtube", "en_16x9", "slow", "Auto edit", ("auto_edit",), c19,
         marks=("slow",)),
    Case(20, "reels_full_pipeline", "complete the video for instagram reels with hindi captions and upbeat music",
         "en_16x9", "slow", "Full pipeline", ("auto_edit",), c20, marks=("slow",)),
    Case(21, "voiceover", "add a voiceover saying 'Thanks for watching' at the end", "en_16x9", "fast", "TTS (parity+)",
         ("voiceover",), c21),
    # WHY expects_op=True: an undo MOVES the EDL, and the desktop/phone only
    # refresh the timeline on an `op` frame — so the turn must stream exactly
    # one, like any mutating run. `expects_op=False` here was what let the
    # step-less no-op (read-only branch, store untouched) pass the harness
    # while c22's hash/ops checks failed.
    Case(22, "undo", "undo that", "en_16x9", "fast", "Undo", ("undo",), c22,
         setup=_chain(_remember_hash, _prior_prompt("add captions")), expects_op=True),
    Case(23, "runtime_gate", "make it pop", "long_12min", "slow", "Run-time gate", ("auto_edit",), c23,
         marks=("slow",), answers={"go": "no"}, expects_op=False),
    Case(24, "pending_transcript", "remove the ums", "en_16x9", "slow", "Prerequisite honesty", ("remove_fillers",), c24,
         setup=_pending_transcript, marks=("slow",)),
    Case(25, "transitions_zoom", "smooth zoom between every clip", "presplit_16x9", "fast", "Transitions (Zoom)",
         ("transitions",), c25, setup=_chain(_plant_render_clock_probes, _remember_render)),
    Case(26, "transitions_glitch", "add a glitch transition at the hook", "presplit_16x9", "fast",
         "Transitions (Glitch/Stylised)", ("transitions",), c26, setup=_chain(_plant_render_clock_probes, _remember_render)),
)

CASE_BY_ID: dict[int, Case] = {c.id: c for c in CASES}
FAST_TIER: tuple[int, ...] = tuple(c.id for c in CASES if c.tier == "fast")
SLOW_TIER: tuple[int, ...] = tuple(c.id for c in CASES if c.tier == "slow")
TRANSITION_CASES: tuple[int, ...] = (18, 25, 26)


def prompts() -> list[str]:
    """Every benchmark prompt — P's grammar fixture (`≥ 0.75` confidence)."""
    return [c.prompt for c in CASES]


__all__ = ["Case", "CaseCtx", "CASES", "CASE_BY_ID", "FAST_TIER", "SLOW_TIER", "TRANSITION_CASES",
           "BASIC_FAMILY", "ZOOM_FAMILY", "GLITCH_FAMILY", "FRAME_S", "LUFS_TOL", "LUFS_TOL_SHORT_CONTENT",
           "PROBE_LEAD_S", "PROBE_S", "PROBE_TONE_HZ", "PROBE_AUDIO_TOL_S", "PROBE_GRID_S", "prompts"]
