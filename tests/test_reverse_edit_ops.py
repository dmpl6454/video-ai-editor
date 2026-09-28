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
def test_split_of_a_reversed_curve_clip_at_every_frame_is_invisible(tmp_path, info, sp):
    """A reversed CURVE splits at an off-grid source point (the exact
    integral). Its pieces' intermediates are built on the source's ABSOLUTE
    grid and opened at a fractional `in` (render/reverse.py
    `intermediate_span`/`view_range`, wave E item 21), so every piece reads
    the whole clip's file clock: exact at every frame. Before: built on the
    piece's own range, Hero at 30 fps changed frames at 25 of 76 split
    points (one source frame each); before RD3 the pieces played out of
    order (errors of 100+ frames)."""
    bad, _ = _split_errors(_store(tmp_path / "w", sp), info, tmp_path, sp)
    assert bad == [], f"{len(bad)} splits changed frames: {bad[:6]}"


@pytest.mark.parametrize("sp", CURVES, ids=_sid)
@pytest.mark.parametrize("R", [Fraction(24000, 1001), Fraction(25), Fraction(60000, 1001)],
                         ids=G.rate_name)
def test_split_of_a_reversed_curve_clip_is_exact_at_other_rates(tmp_path, sp, R):
    """The same at 23.976, 25 and 59.94 with an off-grid in and out."""
    info = SourceInfo.cfr(R, 900)
    st = _store(tmp_path / "w", sp, R=R, in_=2.0137, out=8.4111)
    whole = build_program_map(st.edl, lambda _s: info).frame
    c = st.edl.get_clip(st.cid)[1]
    k0 = tb.frame_of(c.start, R)
    bad = []
    for k in range(k0 + 1, k0 + len(whole), 4):
        s2 = _copy(st, tmp_path / f"s{k}")
        if dispatch(s2, "split_at", {"time": tb.time_of(k, R), "track": "v1"})["split"] != 1:
            continue
        if build_program_map(s2.edl, lambda _s: info).frame != whole:
            bad.append(k)
    assert bad == [], f"{len(bad)} splits changed frames: {bad[:6]}"


def test_a_reversed_curve_intermediate_sits_on_the_source_grid():
    """The recipe itself: a curve's intermediate spans [floor(in), ceil(out))
    on the project grid and its view opens it where `out` sits, keeping the
    clip's span bit for bit; a constant-speed reversal is unchanged."""
    from video_ai_editor.render import reverse
    c = Clip(src=SRC, in_=2.0137, out=8.4111, start=0.0, id="c", reverse=True,
             speed={"name": "custom", "curve": [[0, 1], [0.5, 0.2], [1, 1]]})
    t0, m = reverse.intermediate_span(c, R30)
    assert (t0, m) == (tb.time_of(60, R30), 253 - 60)        # floor(60.41), ceil(252.33)
    v_in, v_out = reverse.view_range(c, R30)
    assert abs(v_in - (253 / 30 - 8.4111)) < 1e-12
    assert v_out - v_in == c.out - c.in_
    flat = c.model_copy(update={"speed": 2.0})
    assert reverse.intermediate_span(flat, R30) == (2.0137, tb.frame_of(8.4111 - 2.0137, R30))
    assert reverse.view_range(flat, R30) == (0.0, reverse.view_out(flat))


@pytest.mark.parametrize("sp", EXACT + CURVES, ids=_sid)
def test_split_of_a_reversed_overlay_clip_keeps_its_frames(tmp_path, info, sp):
    bad, _ = _split_errors(_store(tmp_path / "w", sp, track="v2"), info, tmp_path, sp,
                           track="v2", step=5)
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


@pytest.mark.parametrize("track,sp", [("v1", None), ("v1", "hero"), ("v2", 2.0), ("v2", "montage")],
                         ids=["v1_1x", "v1_hero", "v2_2x", "v2_montage"])
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
    assert diff == [], f"{len(diff)} frames differ, first {diff[:5]}"
    if track == "v1":
        fbits = (1 << G.FRAME_BITS) - 1
        assert [x & fbits for x in split] == _v1(st, _, src), "the program map predicts it"


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


# ------------------------------------- the browser port reads the same pieces

PIECES_GOLDEN = Path(__file__).resolve().parent / "goldens" / "reversed_curve_pieces.json"


def _pieces_corpus(tmp: Path) -> dict:
    """Whole reversed curve clips and their split / cut / trimmed / frozen
    edits, with the program map's frames: frameMap.ts must read the same
    (frontend/src/lib/preview/timeline/reversedPieces.test.ts)."""
    import json
    cases = []
    for R in (Fraction(30), Fraction(24000, 1001), Fraction(25)):
        info = SourceInfo.cfr(R, 900)
        for sp in CURVES:
            st = _store(tmp / f"{G.rate_name(R)}-{sp}", sp, R=R, in_=2.0137, out=8.4111)
            c = st.edl.get_clip(st.cid)[1]
            k0, n = tb.frame_of(c.start, R), compositor.clip_frames(c, R)
            edits = [("whole", None)]
            edits += [(f"split@{k}", ("split_at", {"time": tb.time_of(k0 + k, R), "track": "v1"}))
                      for k in (n // 5, n // 2, n - 3)]
            edits += [("cut", ("cut_range", {"track": "v1", "start": tb.time_of(n // 4, R),
                                             "end": tb.time_of(n // 3, R)}))]
            for name, op in edits:
                s2 = _copy(st, tmp / f"{G.rate_name(R)}-{sp}-{name}")
                if op is not None:
                    dispatch(s2, op[0], op[1])
                pm = build_program_map(s2.edl, lambda _s: info)
                cases.append({"name": f"{G.rate_name(R)}/{sp}/{name}",
                              "edl": json.loads(s2.edl.model_dump_json(by_alias=True)),
                              "source": info.to_json(), "frames": pm.frame})
    return {"_": "tests/test_reverse_edit_ops.py (VAI_REGEN_GOLDENS=1 rewrites it)", "cases": cases}


def test_the_reversed_curve_pieces_golden_is_current(tmp_path):
    import json
    import os
    doc = _pieces_corpus(tmp_path)
    if os.environ.get("VAI_REGEN_GOLDENS") == "1" or not PIECES_GOLDEN.exists():
        PIECES_GOLDEN.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    stored = json.loads(PIECES_GOLDEN.read_text())
    assert [c["frames"] for c in stored["cases"]] == [c["frames"] for c in doc["cases"]], \
        "stale: VAI_REGEN_GOLDENS=1 pytest tests/test_reverse_edit_ops.py -k pieces_golden"
    whole = {c["name"].rsplit("/", 1)[0]: c["frames"] for c in doc["cases"] if c["name"].endswith("/whole")}
    for c in doc["cases"]:
        base, edit = c["name"].rsplit("/", 1)
        if edit.startswith("split"):
            assert c["frames"] == whole[base], c["name"]


# ------------------------------------------------- trims of a reversed curve

@pytest.mark.parametrize("sp", CURVES, ids=_sid)
@pytest.mark.parametrize("track", ["v1", "v2"])
def test_trim_of_a_reversed_curve_clip_keeps_the_frames_it_shows(tmp_path, info, sp, track):
    """`trim_clip` on a reversed CURVE clip (wave E, item 22): its source
    `out` is played FIRST, so lowering `out` trims the HEAD of the footprint
    and raising `in` trims the TAIL. The kept frames play exactly as before
    (the curve's reversed integral, `dispatch._trim_curve`). Before: the
    source points were mapped forwards, so a tail trim cut the head's curve
    and the frames that stayed changed."""
    st = _store(tmp_path / "w", sp, track=track)
    lane = (lambda s: _v1(s, info)) if track == "v1" else (lambda s: _lane(s, info, track))
    whole = lane(st)
    c = st.edl.get_clip(st.cid)[1]
    D = c.effective_duration
    for i, (new_in, new_out) in enumerate([(c.in_ + 1.3, c.out), (c.in_, c.out - 1.7),
                                           (c.in_ + 0.77, c.out - 2.21)]):
        s2 = _copy(st, tmp_path / f"t{i}")
        args = {"clip_id": st.cid, "in": new_in, "out": new_out}
        if track != "v1":
            args["move_start"] = True
        dispatch(s2, "trim_clip", args)
        t0 = tb.quantize(c.timeline_offset_at(c.out - new_out), R30) if new_out < c.out else 0.0
        t1 = tb.quantize(c.timeline_offset_at(c.out - new_in), R30) if new_in > c.in_ else D
        k0, k1 = tb.frame_of(t0, R30), tb.frame_of(t1, R30)
        got = lane(s2)
        trimmed = s2.edl.get_clip(st.cid)[1]
        assert trimmed.reverse and trimmed.in_ >= c.in_ and trimmed.out <= c.out
        if track == "v1":
            assert got == whole[k0:k1], (new_in, new_out)
        else:
            assert tb.frame_of(trimmed.start, R30) == tb.frame_of(c.start, R30) + k0
            assert got == whole[k0:k1], (new_in, new_out)
