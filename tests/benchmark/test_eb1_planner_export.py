"""EB1 fix wave, lane FX-C1 (planner, picture side): what a viewer SEES and HEARS in the real export.

    VAI_BRAIN=recipes uv run pytest -m "benchmark and eb1" tests/benchmark/test_eb1_planner_export.py

The planner's unit tests (tests/test_brain_{emphasis,camera,seams}.py) decode the plan; this module decodes the
render, through the same route the slice takes (upload -> transcribe -> analyse -> the Prompt bar), on lane A's
fixtures, with no cloud key:

  * EX-01 / EX-04 / UX-11: every scale step of the reel — the hide keys AND the seams the story pass made — is
    measured from the bar-code cells of the exported frames (the cell width is the zoom) just before and just after
    the seam. The export shows exactly what the browser preview would (`lib/overlay.ts` `sampleKF`), and every
    `jump_cut_hide` the plan claims is a step of at least 6 % in the picture.
  * EX-08: the podcast at 20 / 25 / 29.97 / 30 fps project rates: every recorder click meets its picture flash
    within half an output frame, no key is dropped ("cut away earlier in this plan"), and the hides are in the timeline.
  * UX-10 / EX-07: every whole turn of the podcast that is not a backchannel plays on its speaker's close.

Skips, naming what is missing, when whisper.cpp or a fixture is not on the machine.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from . import eb1_slice_lib as L  # noqa: E402
from . import measure as M  # noqa: E402
from .harness import open_bench, requirement_missing  # noqa: E402

pytestmark = [pytest.mark.benchmark, pytest.mark.eb1, pytest.mark.slow]

_BLOCKED = ("librosa", "torch", "mlx", "mlx_lm")
HIDE_STEP_MIN = 0.06
SCALE_RESOLUTION = 0.0025          # what the bar-cell geometry can tell apart
CW = 1080
K = CW / 320.0


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    why = requirement_missing("whisper_small") or L.whisper_cpp_missing()
    if why:
        pytest.skip(why)
    mp = pytest.MonkeyPatch()
    mp.setenv("VAI_BRAIN_ENABLED", "1")
    mp.setenv("VAI_PROMPT_CONFIRM", "1")
    for name in _BLOCKED:
        mp.setitem(sys.modules, name, None)
    try:
        with open_bench(tmp_path_factory.mktemp("eb1_fx_c1"), brain="recipes") as bench:
            inner = pytest.MonkeyPatch()
            inner.setenv("WHISPER_BACKEND", "whisper_cpp")
            try:
                yield bench
            finally:
                inner.undo()
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def fixtures():
    from brain_fixtures import build_brain_fixtures
    return build_brain_fixtures()


def _prompt(env: Any, sid: str, text: str, *, fps: float | None = None) -> dict[str, Any]:
    """Optionally set the project rate, then run the Prompt bar; one op each. Returns what the run left."""
    n0 = len(M.load_ops(env.session_dir(sid)))
    if fps is not None:
        env.dispatch(sid, "set_canvas", {"fps": fps})
    run = env.run_prompt(sid, text, answers={"apply": "yes", "go": "yes"})
    assert not run.errors, run.errors
    ops = M.load_ops(env.session_dir(sid))
    from video_ai_editor.brain import store as B
    did = L.decisions_id_of(ops[-1])
    edp = B.read_edp(env.session_dir(sid), did)
    import json
    return {"run": run, "edl": M.load_edl(env.session_dir(sid)), "added": len(ops) - n0,
            "edp": json.loads(edp.model_dump_json()), "text": run.first_text}


def _undo(env: Any, sid: str, n: int) -> None:
    for _ in range(n):
        env.dispatch(sid, "undo", {})


# --------------------------------------------------------------------------
# decoding the scale of the reel's frames from the bar-code cells
# --------------------------------------------------------------------------

def _rows(render: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(render), "-map", "0:v:0", "-vf",
                          "scale=1080:1920:flags=neighbor,crop=1080:8:0:572,format=gray", "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 8, CW).astype(np.float32).mean(axis=1)


def _scale_of(row: np.ndarray) -> float | None:
    """The zoom about the picture's centre, from where the lit bar-code cells are (a cell is 10 px of 320 at 1.0)."""
    lit = row > 125.5
    d = np.diff(lit.astype(np.int8))
    ups, downs = np.where(d == 1)[0] + 1, np.where(d == -1)[0] + 1
    runs = [(u, dn) for u in ups for dn in downs[downs > u][:1]]
    runs = [(u, dn) for u, dn in runs if u > 3 and dn < CW - 3 and 6 * K <= dn - u <= 14 * K]
    if not runs:
        return None
    centres = np.array([(u + dn) / 2 for u, dn in runs])
    width = float(np.mean([dn - u for u, dn in runs])) / (10 * K)
    best = None
    for s in np.arange(0.98, 1.16, SCALE_RESOLUTION):
        b = ((centres - 540) / (s * K) + 160 - 10) / 20
        cost = float(np.abs(b - np.round(b)).max()) + abs(width - s) * 0.5
        if best is None or cost < best[0]:
            best = (cost, float(s))
    return round(best[1], 4)


def _click_offsets_ms(render: Path) -> list[float]:
    """For every picture flash, (its click - the flash) in ms: the clicks by a matched filter for the fixture's 4 ms
    1 kHz burst (the export is loudness-normalised, so a click is no longer full scale — an amplitude threshold
    finds speech peaks instead), each onset refined to the first sample past a quarter of its peak."""
    from timing_fixtures import audio_samples, flash_onsets
    rate = 48000
    a = np.asarray(audio_samples(render, rate=rate), dtype=np.float32)
    n = int(0.004 * rate)
    k = np.sin(2 * np.pi * 1000 * np.arange(n) / rate).astype(np.float32)
    k /= np.sqrt((k ** 2).sum())
    amp = np.abs(np.correlate(a, k, mode="valid"))
    burst = np.abs(amp) / np.maximum(np.sqrt(np.convolve(a.astype(np.float64) ** 2, np.ones(n), mode="valid")), 1e-4)
    idx = np.where((amp > 0.35 * amp.max()) & (burst > 0.9))[0]
    clicks, last = [], -10 ** 9
    for i in idx:
        if i - last > 0.05 * rate:
            clicks.append(i + int(np.argmax(amp[i:i + n])))
        last = i
    out = []
    for f in flash_onsets(render):
        near = [c for c in clicks if abs(c / rate - f) < 0.4]
        if near:
            c = min(near, key=lambda x: abs(x / rate - f))
            i0, i1 = max(0, c - int(0.004 * rate)), c + int(0.006 * rate)
            seg = np.abs(a[i0:i1])
            out.append((i0 + int(np.argmax(seg > 0.25 * seg.max()))) / rate * 1000.0 - f * 1000.0)
    return out


def _preview_scale(clip: Any, local: float) -> float:
    """What the browser preview shows: `lib/overlay.ts` sampleKF — any key counts, a step holds its value."""
    from video_ai_editor.edl.keyframes import sample
    return float(sample(clip.transform.scale, local))


# --------------------------------------------------------------------------
# 1. the reel: what the export draws at every seam
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def reel(env, fixtures):
    sid = env.new_session("FX-C1 reel")
    env.upload_video(sid, Path(fixtures.th.video_9x16))
    L.transcribe(env, sid)
    L.analyse(env, sid)
    got = _prompt(env, sid, L.TH_PROMPT)
    from video_ai_editor.render.compositor import render_export
    res = render_export(got["edl"], env.session_dir(sid))
    path = Path(res.path if hasattr(res, "path") else res)
    scales = [_scale_of(r) for r in _rows(path)]
    return {**got, "sid": sid, "render": path, "scales": scales}


def _seam_sides(reel: dict[str, Any]) -> list[dict[str, Any]]:
    from video_ai_editor.edl.timebase import fps_float
    edl, scales = reel["edl"], reel["scales"]
    fps = fps_float(edl.canvas.fps)
    v1 = L.v1(edl)
    out = []
    for a, b in zip(v1, v1[1:]):
        i = int(round(float(b.start) * fps))
        before = [s for s in scales[max(0, i - 4):i] if s]
        after = [s for s in scales[i:i + 4] if s]
        if not before or not after:
            continue
        out.append({"t": float(b.start), "gap": float(b.in_) - float(a.out),
                    "export": (before[-1], after[0]),
                    "preview": (_preview_scale(a, float(a.effective_duration)), _preview_scale(b, 0.0))})
    return out


def test_the_export_draws_the_scale_the_preview_shows_at_every_seam(reel):
    """EX-01: a one-key hide showed 1.08 in the preview and 1.0 in the export. Two keys, and they agree."""
    sides = _seam_sides(reel)
    assert len(sides) >= 6, sides
    off = [(round(s["t"], 3), s["export"], s["preview"]) for s in sides
           if any(abs(e - p) > 0.012 for e, p in zip(s["export"], s["preview"]))]
    assert not off, f"seams where the export and the preview differ (t, export before/after, preview before/after): {off}"


def test_every_hide_the_plan_claims_is_a_step_of_six_percent_in_the_picture(reel):
    """EX-04 + UX-11: measured on the render, a claimed `jump_cut_hide` is a real step, and no seam of the reel
    that skipped >= 0.4 s of the source is left at the same scale on both sides when the plan says it hid it."""
    edp, sides = reel["edp"], _seam_sides(reel)
    hides = [d for d in edp["decisions"] if d["kind"] == "jump_cut_hide"]
    assert len(hides) >= 3, [d["params"] for d in hides]
    for d in hides:
        assert abs(d["params"]["scale"] - d["params"]["from_scale"]) >= HIDE_STEP_MIN - 1e-6, d["params"]
        assert len(d["params"]["keys"]) == 2
    from video_ai_editor.edl.schema import Clip
    clips = [c for c in L.v1(reel["edl"]) if isinstance(c, Clip)]

    def opening(d: dict) -> Clip | None:            # the piece that opens at the hide's seam (the tool put its in-point on the grid)
        c = min(clips, key=lambda c: abs(float(c.in_) - d["params"]["piece"][0]))
        return c if abs(float(c.in_) - d["params"]["piece"][0]) <= L.frame_s(reel["edl"]) + 1e-6 else None
    for d in hides:
        piece = opening(d)
        assert piece is not None, ("a hide with no piece opening at its seam", d["params"]["piece"])
        s = next(x for x in sides if abs(x["t"] - float(piece.start)) < 1e-3)
        step = abs(s["export"][1] - s["export"][0])
        assert step >= HIDE_STEP_MIN - 2 * SCALE_RESOLUTION, f"hide at timeline {s['t']:.3f}: measured step {step:.4f}, plan {d['params']['step']}"
    steps = [round(abs(s["export"][1] - s["export"][0]), 3) for s in sides if s["gap"] >= 0.4 - 1e-6]
    unhidden = [s for s in sides if s["gap"] >= 0.4 - 1e-6 and abs(s["export"][1] - s["export"][0]) < HIDE_STEP_MIN - 2 * SCALE_RESOLUTION]
    # the seams the plan could not hide (a punch-in holds the next piece) are NOT claimed: no hide decision opens them
    claimed = {round(float(opening(d).start), 3) for d in hides}
    assert not [s for s in unhidden if round(s["t"], 3) in claimed], (steps, unhidden)
    print(f"FX-C1 reel: {len(hides)} hides, measured steps at every >=0.4 s seam: {steps}; unhidden and unclaimed: "
          f"{[round(s['t'], 2) for s in unhidden]}")


def _speech_holes(reel: dict[str, Any], truth: Any) -> list[tuple[float, float, str, str]]:
    """(timeline second, hole s, sentence, next sentence): the air between two consecutive whole sentences of the reel's
    programme, measured on the fixture's own voiced spans (not on whisper's word times)."""
    edl = reel["edl"]
    pieces = [(float(c.start), float(c.in_), float(c.out)) for c in L.v1(edl)]

    def at(src: float) -> float | None:
        return next((st + src - i for st, i, o in pieces if i - 1e-6 <= src <= o + 1e-6), None)
    rows = []
    for s in truth.sentences:
        a, b = at(s.t0), at(s.t1)
        if a is not None and b is not None and abs((b - a) - (s.t1 - s.t0)) < 0.05:
            rows.append((a, b, s.id))
    rows.sort()
    return [(round(b0, 3), round(a1 - b0, 3), i0, i1) for (_a0, b0, i0), (a1, _b1, i1) in zip(rows, rows[1:])]


def test_the_reel_meets_its_length_without_a_hole_between_sentences(reel, fixtures):
    """UX-11: the asked length used to be met with 0.45 s given back at the two longest pauses — ~1 s of silence
    between two sentences (measured 1.00 and 1.15 s on this reel). The air is spread and no pause is left as long as a
    silence; the length stays within the second the whole-sentence fit allows."""
    edl = reel["edl"]
    assert abs(float(edl.duration) - 45.0) <= 1.0, edl.duration
    holes = _speech_holes(reel, fixtures.th.truth)
    assert len(holes) >= 6, holes
    limit = 0.6 + 0.3                      # a reel's dead-air line, plus what the recording's own tails add to a gap measured on the script
    long = [h for h in holes if h[1] > limit]
    assert not long, f"holes longer than {limit} s between two sentences: {long} (all: {[h[1] for h in holes]})"
    print(f"FX-C1 reel: {float(edl.duration):.2f} s of 45; longest hole between sentences {max(h[1] for h in holes):.3f} s of {len(holes)}")


# --------------------------------------------------------------------------
# 2. the podcast: rates, clicks and flashes, whole turns
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def podcast(env, fixtures):
    if fixtures.p2 is None:
        pytest.skip(fixtures.p2_skip_reason or "P2 fixture unavailable")
    p2 = fixtures.p2
    sid = env.new_session("FX-C1 podcast")
    env.upload_video(sid, Path(p2.cam_a))
    env.upload_video(sid, Path(p2.cam_b))
    env.upload_audio(sid, Path(p2.recorder_wav), add_to_music=True)
    L.transcribe(env, sid)
    L.analyse(env, sid)
    return sid


@pytest.mark.parametrize("fps", [20.0, 25.0, 29.97, 30.0])
def test_the_podcast_clicks_meet_their_flashes_at_every_project_rate(env, fixtures, podcast, fps):
    """EX-08: at any project rate every click meets its flash within half an output frame (plus the 0.5 ms the
    EDL's 4-decimal times can move a seam), no key is dropped, and every hide is in the timeline."""
    from video_ai_editor.edl.timebase import fps_float
    from video_ai_editor.render.compositor import render_export
    sid = podcast
    got = _prompt(env, sid, L.P2_PROMPT, fps=fps if fps != 20.0 else None)
    try:
        assert "dropped" not in got["text"], got["text"]
        edl, edp = got["edl"], got["edp"]
        hides = [d for d in edp["decisions"] if d["kind"] == "jump_cut_hide"]
        keyed = [c for c in L.v1(edl) if getattr(c.transform.scale, "keyframes", None)]
        assert len(keyed) >= len(hides) >= 1, (len(keyed), len(hides))
        res = render_export(edl, env.session_dir(sid), height=360)
        render = Path(res.path if hasattr(res, "path") else res)
        offs = _click_offsets_ms(render)
        rate = fps_float(edl.canvas.fps)
        half = 500.0 / rate
        assert len(offs) >= 30, len(offs)
        worst = max(abs(o) for o in offs)
        assert worst <= half + 0.5, f"{fps} fps: worst click - flash {worst:.2f} ms against half a frame {half:.2f} ms: {sorted(offs)[:4]}"
        print(f"FX-C1 podcast {fps} fps: {len(offs)} pairs, worst {worst:.2f} ms of {half:.2f} ms; {len(hides)} hides, {len(keyed)} keyed pieces")
    finally:
        _undo(env, sid, got["added"])


def test_every_whole_turn_of_the_podcast_is_on_its_speakers_close(env, fixtures, podcast):
    """UX-10 / EX-07: no turn that is not a backchannel plays on the other camera — but for the seconds the other voice
    still overlaps it and the head of a turn a tighten removal took."""
    truth = fixtures.p2.truth
    sid = podcast
    got = _prompt(env, sid, L.P2_PROMPT)
    try:
        edl = got["edl"]
        srcs = sorted({str(c.src) for t in edl.tracks for c in t.clips if getattr(c, "src", None)})
        cam = {name: next(x for x in srcs if Path(x).name.startswith(Path(getattr(fixtures.p2, name)).stem)) for name in ("cam_a", "cam_b")}
        rec = next(x for x in srcs if Path(x).name.startswith(Path(fixtures.p2.recorder_wav).stem))
        angles = {cam["cam_a"]: "cam_a", cam["cam_b"]: "cam_b"}
        spoken = [(t.t0, t.t1, t.expected_angle) for t in truth.turns if t.expected_angle and t.id not in truth.backchannels]
        share = L.angle_share(edl, rec, spoken, angles)
        never = []
        for t in truth.turns:
            if not t.expected_angle or t.id in truth.backchannels:
                continue
            one = L.angle_share(edl, rec, [(t.t0, t.t1, t.expected_angle)], angles)
            if one is not None and one < 0.05:
                never.append((t.id, t.speaker, round(t.t0, 2), round(t.t1, 2)))
        assert not never, f"whole turns never on their speaker's close: {never}"
        assert share >= 0.95, f"{share:.1%} of talking time on the speaker's close"
        print(f"FX-C1 podcast: {share:.1%} of talking time on the speaker's own close, no turn wholly on the wrong camera; "
              f"{got['edp']['summary']['camera']['switches']} angle changes")
    finally:
        _undo(env, sid, got["added"])


#: what whisper dropped on the podcast fixture (lane D's report): "Why the sensor first?", "The lens, the heat,", "Yeah."
P2_UNHEARD = ((33.70, 35.10), (38.14, 39.46), (71.05, 71.51))


def test_the_podcast_never_cuts_the_speech_whisper_dropped(env, fixtures, podcast):
    """Lane D's guard: a voiced run under no word is speech. On the real analysis of the podcast the three stretches
    (33.70-35.10, 38.14-39.46, 71.05-71.51 s of the recorder) are in no cut_range and all still play on the dialogue lane."""
    sid = podcast
    got = _prompt(env, sid, L.P2_PROMPT)
    try:
        edl, edp = got["edl"], got["edp"]
        srcs = sorted({str(c.src) for t in edl.tracks for c in t.clips if getattr(c, "src", None)})
        rec = next(x for x in srcs if Path(x).name.startswith(Path(fixtures.p2.recorder_wav).stem))
        for a, b in P2_UNHEARD:
            played = L.played_fraction(edl, rec, a, b, track="a1")
            assert played >= 0.98, f"{a}-{b} s of the recording plays {played:.0%} on the dialogue lane"
        print("FX-C1 podcast: the three stretches whisper dropped all play "
              f"({[round(L.played_fraction(edl, rec, a, b, track='a1'), 3) for a, b in P2_UNHEARD]})")
    finally:
        _undo(env, sid, got["added"])
