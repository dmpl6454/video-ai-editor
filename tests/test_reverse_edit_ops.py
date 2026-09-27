"""Edit ops on a REVERSED clip keep its picture (wave D3 fixer, review RD3).

A reversed clip plays its source range `[in, out)` from `out` down to `in`,
so timeline offset `t` shows source `out - source_offset_at(t)`. `split_at`,
`cut_range` and `freeze_frame` used the forward mapping: the left piece got
`[in, cut]` and the right `[cut, out]`, both still reversed, and the edited
clip played its source out of order (measured: a split at 40 % changed 180 of
180 frames at 1x; a cut_range 1-2 s kept frames [61, 60, 239, ...] where
[211, 210, 179, ...] were due). A reversed piece that plays FIRST holds the
source's END: left `[cut, out]`, right `[in, cut]`. The speed curve lives on
the OUTPUT clock, so each piece keeps the same part of it as a forward split.

* the program map (the model the export goldens pin) for v1, and the per-clip
  frame list for an overlay (PiP) lane, at every frame: fast;
* real renders decoded from bar-coded sources: whole == split on v1 and v2.
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
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, Transform  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402
from video_ai_editor.render.frame_map import SourceInfo, build_program_map, clip_frame_list  # noqa: E402

SRC = "bars.mp4"
R30 = Fraction(30)


def _sid(sp) -> str:
    return "1x" if sp is None else (sp if isinstance(sp, str) else f"{sp:g}x")


def _store(tmp: Path, sp, *, track: str = "v1", src: str = SRC, R=R30,
           in_: float = 2.0, out: float = 8.0) -> EDLStore:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=G.fps_value(R))
    st.edl.canvas.loudness_lufs = None
    st.commit("init", {}, "init")
    if track != "v1":
        dispatch(st, "add_clip", {"track": "v1", "src": src, "in": 0.0, "out": 10.0, "start": 0.0})
    cid = dispatch(st, "add_clip", {"track": track, "src": src, "in": in_, "out": out,
                                    "start": 0.5 if track != "v1" else 0.0})["clip_id"]
    if track != "v1":
        st.edl.get_clip(cid)[1].transform = Transform(x=G.W / 2, y=G.H / 2, scale=2.86)
        st.commit("t", {}, "t")
    dispatch(st, "set_clip_reverse", {"clip_id": cid, "reverse": True})
    if isinstance(sp, str):
        dispatch(st, "set_speed", {"clip_id": cid, "preset": sp})
    elif sp is not None:
        dispatch(st, "set_speed", {"clip_id": cid, "factor": sp})
    st.cid = cid   # type: ignore[attr-defined]
    return st


def _copy(st: EDLStore, tmp: Path) -> EDLStore:
    other = EDLStore(tmp)
    other.edl = st.edl.model_copy(deep=True)
    other.cid = st.cid   # type: ignore[attr-defined]
    return other


def _v1(st: EDLStore, info: SourceInfo, src: str = SRC) -> list[int]:
    return build_program_map(st.edl, lambda _s: info).frame


def _lane(st: EDLStore, info: SourceInfo, track: str = "v2") -> list[int]:
    """An overlay lane's frames, clip after clip (each piece's own list, as
    render/pip.py picks them), checked contiguous on the frame grid."""
    fps = st.edl.canvas.fps
    out: list[int] = []
    cursor = None
    for c in sorted(st.edl.get_track(track).clips, key=lambda c: c.start):
        k0 = tb.frame_of(c.start, fps)
        assert cursor is None or k0 == cursor, "overlay pieces must stay contiguous"
        n = compositor.clip_frames(c, fps)
        out += clip_frame_list(c, info, fps)[:n]
        cursor = k0 + n
    return out


@pytest.fixture(scope="module")
def info() -> SourceInfo:
    return SourceInfo.cfr(30, 900)


# ------------------------------------------------------------------ the model

def _on_grid_split(sp, k: int) -> bool:
    """A constant speed whose source cut `k · speed` frames is not whole
    frames re-rounds at the snapped cut (QA-002): a FORWARD 0.5x / 1.5x
    split moves frames too (E1b measured 136/480 and 97/133) — not a
    reverse matter. Those splits are compared where the cut is whole frames."""
    if sp == 0.5:
        return k % 2 == 0
    if sp == 1.5:
        return k % 2 == 0
    return True


def _split_errors(st: EDLStore, info: SourceInfo, tmp: Path, sp, track: str = "v1",
                  step: int = 1) -> tuple[list, int]:
    """(splits that changed frames as (k, frames differing, worst source-frame
    error), worst error over all), splitting a copy at every `step`-th frame."""
    lane = (lambda s: _v1(s, info)) if track == "v1" else (lambda s: _lane(s, info, track))
    whole = lane(st)
    assert whole[0] > whole[-1], "the fixture plays backwards"
    c = st.edl.get_clip(st.cid)[1]
    k0 = tb.frame_of(c.start, R30)
    bad, worst = [], 0
    for k in range(k0 + 1, k0 + len(whole), step):
        if not _on_grid_split(sp, k - k0):
            continue
        s2 = _copy(st, tmp / f"s{k}")
        if dispatch(s2, "split_at", {"time": tb.time_of(k, R30), "track": track})["split"] != 1:
            continue
        assert all(p.reverse for p in s2.edl.get_track(track).clips)
        got = lane(s2)
        if got != whole:
            err = max([abs(a - b) for a, b in zip(got, whole)] + [0])
            if len(got) != len(whole):
                err = max(err, 10**6)
            bad.append((k, sum(a != b for a, b in zip(got, whole)), err))
            worst = max(worst, err)
    return bad, worst


EXACT = [None, 2.0, 0.5, 1.5]
CURVES = ["hero", "montage"]


@pytest.mark.parametrize("sp", EXACT, ids=_sid)
def test_split_of_a_reversed_v1_clip_at_every_frame_is_invisible(tmp_path, info, sp):
    bad, _ = _split_errors(_store(tmp_path / "w", sp), info, tmp_path, sp)
    assert bad == [], f"{len(bad)} splits changed frames: {bad[:6]}"


@pytest.mark.parametrize("sp", CURVES, ids=_sid)
def test_split_of_a_reversed_curve_clip_keeps_the_order_within_a_frame(tmp_path, info, sp):
    """A reversed CURVE splits at an off-grid source point (the exact
    integral); its right piece's intermediate is then built on a grid a
    fraction of a frame away from the whole's, so a frame can land one
    source frame off (the order and the footprint are right). Before this
    fix the pieces played in the wrong order (errors of 100+ frames)."""
    bad, worst = _split_errors(_store(tmp_path / "w", sp), info, tmp_path, sp, step=3)
    assert worst <= 1, f"worst source-frame error {worst}: {bad[:6]}"


@pytest.mark.xfail(strict=True, reason="reversed-curve splits are within one source frame, not "
                   "exact: the reversed intermediate is anchored at the piece's own range "
                   "(d2-followups: absolute-grid reversed intermediates)")
def test_split_of_a_reversed_hero_clip_is_frame_exact(tmp_path, info):
    bad, _ = _split_errors(_store(tmp_path / "w", "hero"), info, tmp_path, "hero", step=3)
    assert bad == []


@pytest.mark.parametrize("sp", EXACT + CURVES, ids=_sid)
def test_split_of_a_reversed_overlay_clip_keeps_its_frames(tmp_path, info, sp):
    bad, worst = _split_errors(_store(tmp_path / "w", sp, track="v2"), info, tmp_path, sp,
                               track="v2", step=5)
    if sp in CURVES:
        assert worst <= 1, f"worst source-frame error {worst}: {bad[:6]}"
    else:
        assert bad == [], f"{len(bad)} overlay splits changed frames: {bad[:6]}"


@pytest.mark.parametrize("sp", [None, 2.0, "hero"], ids=_sid)
def test_cut_range_through_a_reversed_clip_removes_exactly_its_frames(tmp_path, info, sp):
    st = _store(tmp_path / "w", sp)
    whole = _v1(st, info)
    n = len(whole)
    for i, (a, b) in enumerate([(30, 60), (5, n - 7), (n // 3, n // 3 + 1), (0, 40), (n - 25, n)]):
        s2 = _copy(st, tmp_path / f"c{i}")
        dispatch(s2, "cut_range", {"track": "v1", "start": tb.time_of(a, R30),
                                   "end": tb.time_of(b, R30)})
        got, want = _v1(s2, info), whole[:a] + whole[b:]
        assert len(got) == len(want), (a, b)
        if sp in CURVES:   # the reversed-curve bound (see the split test above)
            assert max(abs(x - y) for x, y in zip(got, want)) <= 1, (a, b)
        else:
            assert got == want, (a, b)


@pytest.fixture(scope="module")
def bar(tmp_path_factory):
    d = tmp_path_factory.mktemp("rbars")
    p = G.make_bar_source(d / "b30.mp4", G.SourceSpec(key="b30", sid=1, rate=R30, seconds=12.0))
    return str(p), G.probe_source(p)


@pytest.mark.parametrize("sp", [None, 2.0, "hero"], ids=_sid)
def test_freeze_on_a_reversed_clip_keeps_the_frames_around_it(tmp_path, bar, sp):
    src, binfo = bar
    st = _store(tmp_path / "w", sp, src=src)
    whole = _v1(st, binfo, src)
    k = 77 if len(whole) > 100 else len(whole) // 2 + 1
    hold = 15
    dispatch(st, "freeze_frame", {"time": tb.time_of(k, R30), "duration": tb.time_of(hold, R30)})
    got = _v1(st, binfo, src)
    assert len(got) == len(whole) + hold
    assert got[:k] == whole[:k], "every frame before the freeze is unchanged"
    assert got[k:k + hold] == [whole[k]] * hold, "the freeze holds the frame at the playhead"
    if sp in CURVES:   # the reversed-curve bound (see the split test above)
        assert max(abs(x - y) for x, y in zip(got[k + hold:], whole[k:])) <= 1
    else:
        assert got[k + hold:] == whole[k:], "and the picture resumes where it stopped"


def test_freeze_on_a_reversed_clip_without_a_frame_table_holds_the_mirrored_frame(tmp_path, info):
    """The fallback (an unprobeable source): the split point, mirrored."""
    st = _store(tmp_path / "w", None)
    whole = _v1(st, info)
    dispatch(st, "freeze_frame", {"time": tb.time_of(40, R30), "duration": 0.5})
    got = _v1(st, info)
    assert got[40:55] == [whole[40]] * 15 and got[:40] == whole[:40] and got[55:] == whole[40:]


# ------------------------------------------------------------ real renders

def _export(st: EDLStore, tmp: Path, name: str) -> list[int]:
    path = compositor._render(st.edl, tmp / f"{name}.mp4", height=G.H, fps=st.edl.canvas.fps,
                              preview=False, cache_dir=tmp / "cache", chunked=False)
    return G.measure(path)["top"]


@pytest.mark.parametrize("track,sp", [("v1", None), ("v1", "hero"), ("v2", 2.0)],
                         ids=["v1_1x", "v1_hero", "v2_2x"])
def test_a_split_reversed_clip_exports_the_whole_clips_frames(tmp_path, bar, track, sp):
    src, _ = bar
    st = _store(tmp_path / "s", sp, track=track, src=src)
    whole = _export(st, tmp_path, "whole")
    if track == "v1":
        assert [x & ((1 << G.FRAME_BITS) - 1) for x in whole] == _v1(st, _, src)
    c = st.edl.get_clip(st.cid)[1]
    at = tb.time_of(tb.frame_of(c.start + c.effective_duration * 0.4, R30), R30)
    assert dispatch(st, "split_at", {"time": at, "track": track})["split"] == 1
    split = _export(st, tmp_path, "split")
    assert len(split) == len(whole)
    diff = [i for i, (a, b) in enumerate(zip(split, whole)) if a != b]
    if sp in CURVES:
        # The reversed-curve bound (see the model test): within one source
        # frame, and exactly what the program map predicts for the pieces.
        fbits = (1 << G.FRAME_BITS) - 1
        assert max(abs((a & fbits) - (b & fbits)) for a, b in zip(split, whole)) <= 1
        if track == "v1":
            assert [x & fbits for x in split] == _v1(st, _, src)
    else:
        assert diff == [], f"{len(diff)} frames differ, first {diff[:5]}"


# ------------------------------------------- the reversed view's footprint

@pytest.mark.parametrize("R", [Fraction(24000, 1001), Fraction(25), Fraction(30000, 1001),
                               Fraction(30), Fraction(60000, 1001)], ids=G.rate_name)
@pytest.mark.parametrize("speed", [2.0, 4.0, 0.5, 1.5, 3.0])
def test_the_reversed_view_keeps_the_clips_footprint(R, speed):
    """A reversed clip plays its intermediate through a VIEW (in 0); the view
    must occupy the frames the EDL lays the clip out on. Measured before the
    fix: 653-693 of 3000 grid-aligned reversed 2x clips (293-370 at 4x) were
    a frame longer or shorter through the view (a .5-frame tie rounded two
    ways), so the export and the program map drifted from the timeline."""
    from video_ai_editor.render import frame_map, reverse
    import random
    rng = random.Random(7)
    bad = []
    for _ in range(600):
        a, n = rng.randrange(0, 900), rng.randrange(1, 400)
        c = Clip(src=SRC, in_=tb.time_of(a, R), out=tb.time_of(a + n, R), start=0.0, id="c",
                 speed=speed, reverse=True)
        want = compositor.clip_frames(c, R)
        for view in (frame_map._reversed_view(c, R),
                     c.model_copy(update={"in_": 0.0, "out": reverse.view_out(c), "reverse": False})):
            if compositor.clip_frames(view, R) != want:
                bad.append((a, n))
    assert bad == [], f"{len(bad)} reversed views change the footprint: {bad[:5]}"


def _six_reversed_2x(tmp: Path, src: str) -> EDLStore:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=30)
    st.edl.canvas.loudness_lufs = None
    st.commit("init", {}, "init")
    for i in range(6):
        cid = dispatch(st, "add_clip", {"track": "v1", "src": src, "in": 2.0, "out": 2.0 + 63 / 30,
                                        "start": st.edl.duration})["clip_id"]
        dispatch(st, "set_speed", {"clip_id": cid, "factor": 2.0})
        dispatch(st, "set_clip_reverse", {"clip_id": cid, "reverse": True})
    return st


def test_six_reversed_2x_clips_of_63_frames_stay_on_the_timeline(tmp_path, info):
    """s11 of the RD3 review: timeline starts [0, 31, 63, 94, 126, 158] and
    189 frames, but the export and the program map laid [0, 32, 64, ...] and
    192 frames (every later v1 clip, text and PiP slid)."""
    st = _six_reversed_2x(tmp_path / "s", SRC)
    starts = [tb.frame_of(c.start, R30) for c in st.edl.get_track("v1").clips]
    pm = build_program_map(st.edl, lambda _s: info)
    assert pm.clip_start == starts
    assert pm.total == tb.frame_of(st.edl.duration, R30) == 189


def test_six_reversed_2x_clips_export_the_timelines_length(tmp_path, bar):
    src, _ = bar
    st = _six_reversed_2x(tmp_path / "s", src)
    frames = _export(st, tmp_path, "six")
    assert len(frames) == tb.frame_of(st.edl.duration, R30) == 189
