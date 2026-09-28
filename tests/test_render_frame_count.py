"""Every render emits exactly the frames its plan says (wave E, item 24).

`compositor._check_picture` failed only a render with NO picture; a frame
count that differed from the plan was logged. It compared against
`frame_of(edl.duration)`, which is NOT the plan: on the 500 dispatch-made
EDLs of tests/goldens/frame_plan_cases.json it differs from the program map's
total on 33 (a transition's overlap). The plan is `frame_map.planned_frames`
(the v1 frame plan less every xfade seam: `build_program_map(...).total`
without a source). This file proves the renderer meets it on every path —
single-pass export, chunked (stream-copy and re-encode assembly), the
server preview at three sizes, and export at the nine standard rates — and
that a mismatch is now fatal.
"""
from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import EDL, Canvas, Transform  # noqa: E402
from video_ai_editor.render import compositor, render_preview  # noqa: E402
from video_ai_editor.render.frame_map import SourceInfo, build_program_map, planned_frames  # noqa: E402

GOLDENS = Path(__file__).resolve().parent / "goldens"


def _count(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0].rstrip(","))


# ------------------------------------------------------------------ the model

def test_the_plan_is_the_program_maps_total_on_the_corpus():
    doc = json.loads((GOLDENS / "frame_plan_cases.json").read_text())
    infos = {k: SourceInfo.from_json(v) for k, v in doc["sources"].items()}
    naive = 0
    for c in doc["cases"]:
        e = EDL.model_validate(c["edl"])
        pf = planned_frames(e)
        assert pf == build_program_map(e, lambda s: infos[s]).total, c.get("seq")
        naive += pf != tb.frame_of(e.duration, e.canvas.fps)
    assert naive > 0, "the corpus no longer shows why frame_of(duration) is not the plan"


def test_the_plan_is_what_every_golden_render_decoded():
    """138 cases rendered through render_preview AND the export, decoded."""
    n = 0
    for f in sorted((GOLDENS / "frame_map").glob("*.json")):
        for c in json.loads(f.read_text())["cases"]:
            e = EDL.model_validate(c["edl"])
            assert planned_frames(e) == len(c["measured"]["top"]), c["name"]
            n += 1
    assert n >= 100


# ------------------------------------------------------------- real renders

@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> tuple[str, str]:
    d = tmp_path_factory.mktemp("fcbars")
    a = G.make_bar_source(d / "b30.mp4", G.SourceSpec(key="b30", sid=1, rate=Fraction(30), seconds=12.0))
    b = G.make_bar_source(d / "b25.mp4", G.SourceSpec(key="b25", sid=2, rate=Fraction(25), seconds=12.0))
    return str(a), str(b)


def _edl(tmp: Path, shape: str, bars: tuple[str, str], fps=30) -> EDL:
    a, b = bars
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=fps)
    st.edl.canvas.loudness_lufs = None
    st.commit("init", {}, "init")

    def add(track, src, i, o, start=None):
        return dispatch(st, "add_clip", {"track": track, "src": src, "in": i, "out": o,
                                         "start": st.edl.duration if start is None else start})["clip_id"]
    if shape == "mixed":          # seams, then the retimed clips, one store
        _build(st, "seams", add, a, b, fps)
        shape = "retimed"
    _build(st, shape, add, a, b, fps)
    return st.edl


def _build(st: EDLStore, shape: str, add, a: str, b: str, fps) -> None:
    if shape == "cuts":
        add("v1", a, 0.5, 2.2)
        c2 = add("v1", b, 1.013, 3.4)
        add("v1", a, 4.0, 5.37)
        dispatch(st, "set_speed", {"clip_id": c2, "factor": 1.5})
    elif shape == "tail":
        add("v1", a, 0.0, 2.0)
        add("v1", b, 3.0, 4.1)
        pip = add("v2", a, 5.0, 8.3, start=1.2)          # runs past v1: a trailing gap
        st.edl.get_clip(pip)[1].transform = Transform(x=G.W / 2, y=G.H / 2, scale=0.5)
        st.commit("t", {}, "t")
    elif shape == "retimed":
        c1 = add("v1", a, 1.0, 3.1)
        dispatch(st, "set_clip_reverse", {"clip_id": c1, "reverse": True})
        dispatch(st, "set_speed", {"clip_id": c1, "factor": 2.0})
        c2 = add("v1", b, 2.0137, 6.4111)
        dispatch(st, "set_speed", {"clip_id": c2, "preset": "hero"})
        c3 = add("v1", a, 6.0, 8.0)
        dispatch(st, "set_clip_reverse", {"clip_id": c3, "reverse": True})
        dispatch(st, "set_speed", {"clip_id": c3, "preset": "montage"})
        k0 = tb.frame_of(st.edl.get_clip(c1)[1].start, fps)
        dispatch(st, "freeze_frame", {"time": tb.time_of(k0 + 20, fps), "duration": 0.7})
    elif shape == "seams":
        add("v1", a, 0.0, 2.3)
        c2 = add("v1", b, 2.0, 4.6)
        add("v1", a, 5.0, 7.2)
        dispatch(st, "set_speed", {"clip_id": c2, "factor": 0.8})
        v1 = [c for c in st.edl.get_track("v1").clips]
        for c in v1[:-1]:
            dispatch(st, "add_transition", {"at": c.start + c.effective_duration, "type": "fade",
                                            "duration": 0.37, "track": "v1"})
    else:
        raise AssertionError(shape)


SHAPES = ["cuts", "tail", "retimed", "seams"]


@pytest.mark.parametrize("shape", SHAPES)
def test_single_pass_export_emits_the_plan(tmp_path, bars, shape):
    e = _edl(tmp_path / "s", shape, bars)
    out = compositor._render(e, tmp_path / "x.mp4", height=G.H, fps=e.canvas.fps, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _count(out) == planned_frames(e)


@pytest.mark.parametrize("shape", ["cuts", "tail", "retimed"])
def test_chunked_render_emits_the_plan(tmp_path, bars, shape):
    """Per-clip chunks, joined by packet copy (no gap) or re-encoded (a gap)."""
    e = _edl(tmp_path / "s", shape, bars)
    out = compositor._render(e, tmp_path / "x.mp4", height=G.H, fps=e.canvas.fps, preview=False,
                             cache_dir=tmp_path / "cache", chunked=True)
    assert (tmp_path / "cache" / "chunks").exists(), "the chunk path ran"
    assert _count(out) == planned_frames(e)


@pytest.mark.parametrize("height", [120, 360, 540])
@pytest.mark.parametrize("shape", ["tail", "seams"])
def test_server_preview_emits_the_plan_at_any_size(tmp_path, bars, shape, height):
    e = _edl(tmp_path / "s", shape, bars)
    sess = tmp_path / "sess"
    sess.mkdir()
    out = render_preview(e, sess, height=height).path
    assert _count(out) == planned_frames(e)


@pytest.mark.parametrize("R", list(tb.STANDARD_RATES), ids=G.rate_name)
def test_a_project_at_every_standard_rate_exports_the_plan(tmp_path, bars, R):
    """A project AT each standard rate (seams, reversed, curves, a freeze)."""
    fps = G.fps_value(R)
    e = _edl(tmp_path / "s", "mixed", bars, fps=fps)
    out = compositor._render(e, tmp_path / "x.mp4", height=G.H, fps=fps, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _count(out) == planned_frames(e)


@pytest.mark.parametrize("R", list(tb.STANDARD_RATES), ids=G.rate_name)
def test_a_30fps_project_exported_at_every_standard_rate_emits_the_plan(tmp_path, bars, R):
    """Export at another rate: the seam costs stay on the project grid (the
    render clock) and each xfade removes its duration in the export's frames
    (at 24 fps this project is 374 frames where frame_of(duration) says 375
    and the rate's own seam table 375)."""
    e = _edl(tmp_path / "s", "mixed", bars)
    fps = G.fps_value(R)
    out = compositor._render(e, tmp_path / "x.mp4", height=G.H, fps=fps, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    assert _count(out) == planned_frames(e, fps)


# ------------------------------------------------------------- now fatal

def test_a_render_that_misses_its_plan_fails(tmp_path, bars, monkeypatch):
    e = _edl(tmp_path / "s", "cuts", bars)
    real = planned_frames(e)
    from video_ai_editor.render import frame_map
    monkeypatch.setattr(frame_map, "planned_frames", lambda _e, _f=None: real + 1)
    with pytest.raises(RuntimeError, match="frames"):
        compositor._render(e, tmp_path / "x.mp4", height=G.H, fps=e.canvas.fps, preview=False,
                           cache_dir=tmp_path / "cache", chunked=False)
    assert not (tmp_path / "x.mp4").exists(), "a rejected render is never served from a cache"


def test_an_overlay_that_outlives_the_timeline_does_not_lengthen_it(tmp_path):
    """A keyframed text is a looped PNG input; at `-t` its window + 0.5 s the
    export's `overlay` kept emitting frozen frames past the end (measured:
    135 frames for a 120-frame timeline). The picture stops at the plan
    (`trim=end_frame` in the graph since gate RX; it was `-frames:v`,
    which cut the sound's tail), and since gate X2 the input lasts exactly its window
    (tests/test_overlay_input_length.py), so the render is the timeline."""
    from overlay_render_helpers import base_edl
    from video_ai_editor.edl.schema import Keyframe, TextClip, TextStyle
    from video_ai_editor.render import render_export
    edl = base_edl(tmp_path, 320, 180, 4.0)
    edl.get_track("tx").clips.append(TextClip(
        id="t1", text="MOVING", start=0.0, end=4.0, role=None, style=TextStyle(color="#FF0000", size=40),
        transform=Transform(x=Keyframe(keyframes=[(0.0, 60.0), (3.5, 260.0)]), y=90)))
    edl.recompute_duration()
    out = render_export(edl, tmp_path, height=180).path
    assert _count(out) == planned_frames(edl) == 120
