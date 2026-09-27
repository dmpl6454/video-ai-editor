"""A transition after a RETIMED clip is applied (review RD3).

`set_speed` ripples the next v1 clip to the frame the retimed footprint
rounds to. When that rounds UP, the retimed clip's exact end sits a fraction
of a frame before its neighbour (1.5x on 91 frames: 5.02222 s vs 5.03333 s),
and `seam_table_for` called that sliver a GAP (> 1 ms) — a hard cut. The
transition the user added there was stored and reported, then never applied:
no xfade in the export, no blend frames in the program map or the engine, no
change to the transport length. The frame plan inserts no filler for less
than a frame, so adjacency is now judged on the frame grid, in Python and in
the TS port alike (the golden `xfade_retimed_p30/p25` pins the render).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, Transition, seam_table_for  # noqa: E402
from video_ai_editor.render.frame_map import KIND_BLEND, SourceInfo, build_program_map  # noqa: E402

CASES = [("1.5x", {"factor": 1.5}, 91), ("hero", {"preset": "hero"}, 150),
         ("2x_odd", {"factor": 2.0}, 93), ("0.75x", {"factor": 0.75}, 91),
         ("montage", {"preset": "montage"}, 150), ("2x_even", {"factor": 2.0}, 92)]


@pytest.mark.parametrize("name,sp,nfr", CASES, ids=[c[0] for c in CASES])
def test_a_transition_after_a_retimed_clip_is_applied(tmp_path, name, sp, nfr):
    st = EDLStore(tmp_path)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=30)
    st.edl.canvas.loudness_lufs = None
    st.commit("c", {}, "c")
    dispatch(st, "add_clip", {"track": "v1", "src": "bars.mp4", "in": 0, "out": 3, "start": 0})
    b = dispatch(st, "add_clip", {"track": "v1", "src": "bars.mp4", "in": 5, "out": 5 + nfr / 30,
                                  "start": 3})["clip_id"]
    c = dispatch(st, "add_clip", {"track": "v1", "src": "bars.mp4", "in": 20, "out": 23,
                                  "start": 3 + nfr / 30})["clip_id"]
    dispatch(st, "set_speed", {"clip_id": b, **sp})
    cc = st.edl.get_clip(c)[1]
    before = st.edl.duration
    dispatch(st, "add_transition", {"at": cc.start, "type": "fade", "duration": 0.5})
    seams = st.edl.v1_seam_table()
    assert len(seams) == 1 and seams[0][1] == pytest.approx(0.5)
    assert st.edl.duration == pytest.approx(before - 0.5)
    pm = build_program_map(st.edl, lambda _s: SourceInfo.cfr(30, 1200))
    assert sum(1 for k in pm.kind if k == KIND_BLEND) == 15


def test_a_whole_frame_gap_is_still_a_hard_cut():
    a = Clip(src="s", in_=0.0, out=1.0, start=0.0, id="a")
    b = Clip(src="s", in_=0.0, out=1.0, start=1.0 + 1 / 30, id="b")
    tr = [Transition(at=1.0, type="fade", duration=0.5)]
    assert seam_table_for([a, b], tr, fps=30) == []
    b.start = 1.0 + 0.4 / 30          # a sub-frame gap: one seam on the grid
    assert seam_table_for([a, b], tr, fps=30) == [(1.0, 0.5)]
    # Without a project rate the historical 1 ms rule is unchanged.
    assert seam_table_for([a, b], tr) == []
