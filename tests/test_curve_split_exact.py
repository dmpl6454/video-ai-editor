"""A split, a cut_range and a trim of a speed-CURVE clip are INVISIBLE (wave
D3, lane E1b): the exported frames of the edited timeline equal the whole
clip's, frame for frame.

Measured in milestone 2: splitting a Hero clip (20 s, 30 fps) at 10 s moved
103 of the 759 later frames one source frame late and held the cut frame 1
output frame instead of 2 — the chain rebased a curve at its first DECODED
frame, not at `in`, and truncated the curve's output into a time base in
which a project frame was not whole ticks. `edl/speed_curve.py` ("the v1
chain's clock") gives every piece of a clip the same clocks; the program map
(`render/frame_map.py`) models it with the same doubles.

* the program map, split at EVERY frame, cut and trimmed, over presets,
  project rates, source rates and time bases (fast: the model);
* real renders, decoded from bar-coded sources: split every 7th frame at
  once, and a cut + trims, against the whole clip — and against the model;
  constant-speed splits stay invisible too (their goldens are byte-identical,
  `tests/goldens/frame_map/{fuzz,structure,segments,transitions}.json`).
"""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl import speed_curve as SC  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402
from video_ai_editor.render.frame_map import SourceInfo, build_program_map  # noqa: E402

SRC = "bars.mp4"


def _store(tmp: Path, R: Fraction, preset, *, in_: float = 0.5, secs: float = 8.0,
           src: str = SRC) -> EDLStore:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=G.fps_value(R))
    st.edl.canvas.loudness_lufs = None
    st.edl.get_track("v1").clips = [Clip(src=src, in_=in_, out=in_ + secs, start=0.0, id="c_a")]
    st.edl.recompute_duration()
    st.commit("init", {}, "init")
    if isinstance(preset, str):
        dispatch(st, "set_speed", {"clip_id": "c_a", "preset": preset})
    elif preset is not None:
        dispatch(st, "set_speed", {"clip_id": "c_a", "factor": preset})
    return st


def _copy(st: EDLStore, tmp: Path) -> EDLStore:
    other = EDLStore(tmp)
    other.edl = st.edl.model_copy(deep=True)
    return other


def _frames(st: EDLStore, info: SourceInfo, src: str = SRC) -> list[int]:
    return build_program_map(st.edl, {src: info}).frame


# ---------------------------------------------------------------- the model

#: (project rate, source rate, source time base or None = the muxer's).
MODEL_RATES = [
    (Fraction(30), Fraction(30), None),
    (Fraction(25), Fraction(30), None),                 # 614.4 source ticks a frame
    (Fraction(30000, 1001), Fraction(30000, 1001), None),
    (Fraction(30000, 1001), Fraction(30), None),        # 512.512 ticks a frame
    (Fraction(24000, 1001), Fraction(25), Fraction(1, 90000)),
    (Fraction(60), Fraction(24), Fraction(1, 12288)),
    (Fraction(25), Fraction(60000, 1001), Fraction(1, 1000)),
]
PRESETS = ["hero", "montage", "bullet", "ramp_up", "ramp_down", "flash_in", "jump_cut"]


def _rate_id(r) -> str:
    R, S, tbase = r
    return f"p{G.rate_name(R)}_s{G.rate_name(S)}" + (f"_tb{tbase.denominator}" if tbase else "")


@pytest.mark.parametrize("rates", MODEL_RATES, ids=[_rate_id(r) for r in MODEL_RATES])
@pytest.mark.parametrize("preset", PRESETS)
def test_a_split_at_any_frame_is_invisible_in_the_program_map(tmp_path, rates, preset):
    R, S, tbase = rates
    info = SourceInfo.cfr(S, int(20 * S), time_base=tbase)
    st = _store(tmp_path / "w", R, preset, in_=0.4 + 1 / 7)   # an off-grid in
    whole = _frames(st, info)
    step = 1 if preset in ("hero", "bullet") else 3
    bad = []
    for k in range(1, len(whole), step):
        s2 = _copy(st, tmp_path / f"s{k}")
        r = dispatch(s2, "split_at", {"time": tb.time_of(k, R)})
        assert r["split"] == 1
        got = _frames(s2, info)
        if got != whole:
            bad.append((k, sum(a != b for a, b in zip(got, whole)) + abs(len(got) - len(whole))))
    assert bad == [], f"{len(bad)} splits changed frames: {bad[:6]}"


@pytest.mark.parametrize("rates", MODEL_RATES[:4], ids=[_rate_id(r) for r in MODEL_RATES[:4]])
@pytest.mark.parametrize("preset", ["hero", "montage", "ramp_down"])
def test_a_cut_range_through_a_curve_removes_exactly_its_frames(tmp_path, rates, preset):
    R, S, tbase = rates
    info = SourceInfo.cfr(S, int(20 * S), time_base=tbase)
    st = _store(tmp_path / "w", R, preset, in_=1.23)
    whole = _frames(st, info)
    n = len(whole)
    for i, (a, b) in enumerate([(5, 40), (n // 3, n // 3 + 1), (n // 2 - 17, n - 9), (1, n - 1)]):
        s2 = _copy(st, tmp_path / f"c{i}")
        dispatch(s2, "cut_range", {"track": "v1", "start": tb.time_of(a, R), "end": tb.time_of(b, R)})
        assert _frames(s2, info) == whole[:a] + whole[b:], (a, b)


@pytest.mark.parametrize("rates", MODEL_RATES[:4], ids=[_rate_id(r) for r in MODEL_RATES[:4]])
@pytest.mark.parametrize("preset", ["hero", "bullet", "flash_out"])
def test_a_trim_of_a_curve_keeps_the_frames_it_showed(tmp_path, rates, preset):
    """`trim_clip` takes SOURCE seconds (its contract): the source time the
    curve reaches at a frame boundary trims to exactly that frame."""
    R, S, tbase = rates
    info = SourceInfo.cfr(S, int(20 * S), time_base=tbase)
    st = _store(tmp_path / "w", R, preset, in_=0.9)
    whole = _frames(st, info)
    c = st.edl.get_clip("c_a")[1]
    n = len(whole)
    for i, (k0, k1) in enumerate([(7, n), (0, n - 11), (n // 4, n), (0, n // 2 + 3)]):
        s2 = _copy(st, tmp_path / f"t{i}")
        args = {"clip_id": "c_a"}
        if k0:
            args["in"] = c.in_ + c.source_offset_at(tb.time_of(k0, R))
        if k1 < n:
            args["out"] = c.in_ + c.source_offset_at(tb.time_of(k1, R))
        dispatch(s2, "trim_clip", args)
        t = s2.edl.get_clip("c_a")[1]
        assert t.speed_curve is not None and t.speed.get("name") == "custom"
        assert _frames(s2, info) == whole[k0:k1], (k0, k1)


def test_a_trim_that_extends_a_curve_clip_stretches_the_curve_as_before(tmp_path):
    info = SourceInfo.cfr(30, 900)
    st = _store(tmp_path / "w", Fraction(30), "hero", in_=2.0, secs=6.0)
    before = st.edl.get_clip("c_a")[1].speed
    dispatch(st, "trim_clip", {"clip_id": "c_a", "in": 1.0})
    c = st.edl.get_clip("c_a")[1]
    assert (c.in_, c.out) == (1.0, 8.0) and c.speed == before
    assert len(_frames(st, info)) == compositor.clip_frames(c, 30)


def test_a_cut_is_the_exact_integral_not_rounded_to_the_microsecond(tmp_path):
    st = _store(tmp_path / "w", Fraction(30), "hero", in_=0.4 + 1 / 7)
    c = st.edl.get_clip("c_a")[1]
    want = float(c.in_) + c.source_offset_at(tb.time_of(101, 30))
    r = dispatch(st, "split_at", {"time": tb.time_of(101, 30)})
    right = st.edl.get_clip(r["halves"]["c_a"])[1]
    assert right.in_ == want and st.edl.get_clip("c_a")[1].out == want
    assert round(want, 6) != want


# ---------------------------------------------------------------- the chain

def test_the_curve_chain_runs_on_the_file_clock_anchored_at_in():
    c = Clip(src=SRC, start=0.0, id="c", speed={"curve": SC.CURVE_PRESETS["hero"]})
    c.in_, c.out = 12.34, 20.0
    args = compositor.clip_input_args(c, 25)
    assert args[:2] == ["-ss", "11.800000"]          # the 1/5 s grid, >= 0.5 s before in
    chain = compositor._build_clip_video_chain(c, input_label="[0:v]", label_out="[v0]",
                                               canvas_w=320, canvas_h=180, fps=25)
    assert "setpts=PTS+round(11.8/TB)," in chain and "setpts=PTS-STARTPTS," not in chain.split("fps=")[0]
    assert "settb=intb*gcd(25\\,round(1/intb))/25" in chain
    assert "(PTS*TB-12.34)" in chain and ",fps=25:start_time=0," in chain
    # The curve's sound still decodes on the 1x half-frame pre-roll.
    assert compositor.sound_input_args(c, 25)[:2] == ["-ss", f"{12.34 - 0.02:.6f}"]
    # A clip without a curve keeps its chain: no settb, no anchored grid.
    for speed in (None, 2.0, 0.5):
        k = Clip(src=SRC, start=0.0, id="k", speed=speed)
        k.in_, k.out = 12.34, 20.0
        text = compositor._build_clip_video_chain(k, input_label="[0:v]", label_out="[v0]",
                                                  canvas_w=320, canvas_h=180, fps=25)
        assert "settb" not in text and "start_time" not in text
        assert text.startswith("[0:v]setpts=PTS-STARTPTS,")
        assert compositor.clip_input_args(k, 25) == compositor.sound_input_args(k, 25)


@pytest.mark.parametrize("in_,want", [(0.0, 0.0), (0.69, 0.0), (0.7, 0.0), (0.71, 0.2),
                                      (12.34, 11.8), (3600.5, 3600.0)])
def test_curve_seek_is_on_the_fifth_second_grid(in_, want):
    got = SC.curve_seek(in_)
    assert got == want
    assert got == 0 or (in_ - got >= SC.CURVE_PREROLL_S - 1e-12 and round(got * 5) == got * 5)


@pytest.mark.parametrize("tbase,rate,want", [
    (Fraction(1, 15360), 30, Fraction(1, 15360)), (Fraction(1, 15360), 25, Fraction(1, 76800)),
    (Fraction(1, 15360), 30000, Fraction(1, 1920000)), (Fraction(1, 90000), 60000, Fraction(1, 180000)),
    (Fraction(1, 1000), 30000, Fraction(1, 30000)), (Fraction(1, 12800), 24000, Fraction(1, 192000)),
])
def test_the_refined_time_base_makes_a_project_frame_whole_ticks(tbase, rate, want):
    got = SC.curve_time_base(tbase, rate)
    assert got == want
    assert (tbase / got).denominator == 1
    for R in (Fraction(rate), Fraction(rate, 1001)):
        assert ((1 / R) / got).denominator == 1


# ---------------------------------------------------------------- real renders

@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("bars")
    out = {}
    for key, sid, rate, ts in (("b30", 1, Fraction(30), None), ("b25", 2, Fraction(25), None),
                               ("b2997k", 3, Fraction(30000, 1001), 90000)):
        p = G.make_bar_source(d / f"{key}.mp4", G.SourceSpec(key=key, sid=sid, rate=rate,
                                                             seconds=12.0, timescale=ts))
        out[key] = (str(p), G.probe_source(p))
    return out


def _export(st: EDLStore, tmp: Path, name: str) -> list[int]:
    path = compositor._render(st.edl, tmp / f"{name}.mp4", height=G.H, fps=st.edl.canvas.fps,
                              preview=False, cache_dir=tmp / "cache", chunked=False)
    return [c & ((1 << G.FRAME_BITS) - 1) for c in G.measure(path)["top"]]


OFF_GRID = 0.4 + 1 / 7
RENDER_CASES = [
    ("hero", Fraction(30), "b30", 0, OFF_GRID),
    ("montage", Fraction(25), "b30", 3, OFF_GRID),
    ("bullet", Fraction(30000, 1001), "b2997k", 1, OFF_GRID),
    ("ramp_up", Fraction(24000, 1001), "b25", 5, OFF_GRID),
    # Constant speeds whose splits were invisible before stay so (their
    # chain is untouched). NOT every constant split is: a source rate other
    # than the project's, or a speed like 1.5x / 0.5x, re-rounds at each
    # snapped cut — measured identically before and after this lane
    # (1x 30->25p: 55/200 frames, 1.5x: 97/133, 0.5x: 136/480 + 1 frame).
    (None, Fraction(30), "b30", 2, OFF_GRID),
    (2.0, Fraction(30), "b30", 4, 0.4),
]


@pytest.mark.parametrize("preset,R,key,phase,in_", RENDER_CASES,
                         ids=[f"{p}_p{G.rate_name(R)}_{k}" for p, R, k, _, _i in RENDER_CASES])
def test_a_split_every_seventh_frame_exports_the_whole_clips_frames(bars, tmp_path, preset, R, key,
                                                                    phase, in_):
    src, info = bars[key]
    st = _store(tmp_path / "s", R, preset, in_=in_, secs=8.0, src=src)
    whole_model = _frames(st, info, src)
    whole = _export(st, tmp_path, "whole")
    assert whole == whole_model
    for k in range(7 + phase, len(whole) - 1, 7):
        dispatch(st, "split_at", {"time": tb.time_of(k, R)})
    pieces = st.edl.get_track("v1").clips
    assert len(pieces) >= len(whole) // 7 - 1
    split = _export(st, tmp_path, "split")
    diff = [i for i, (a, b) in enumerate(zip(split, whole)) if a != b]
    assert len(split) == len(whole) and diff == [], f"{len(diff)} frames differ, first {diff[:8]}"
    assert split == _frames(st, info, src)


def test_a_cut_and_trims_of_a_curve_export_exactly_the_kept_frames(bars, tmp_path):
    src, info = bars["b30"]
    R = Fraction(25)
    st = _store(tmp_path / "s", R, "hero", in_=1.37, secs=8.0, src=src)
    whole = _export(st, tmp_path, "whole")
    n = len(whole)
    c = st.edl.get_clip("c_a")[1]
    dispatch(st, "trim_clip", {"clip_id": "c_a", "in": c.in_ + c.source_offset_at(tb.time_of(9, R)),
                               "out": c.in_ + c.source_offset_at(tb.time_of(n - 6, R))})
    kept = whole[9:n - 6]
    a, b = 60, 60 + len(kept) // 3
    dispatch(st, "cut_range", {"track": "v1", "start": tb.time_of(a, R), "end": tb.time_of(b, R)})
    want = kept[:a] + kept[b:]
    got = _export(st, tmp_path, "edited")
    assert got == want
    assert got == _frames(st, info, src)
