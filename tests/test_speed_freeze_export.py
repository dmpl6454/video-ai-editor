"""The EDITS of lane S2 render as the program map says, in a real export.

Every CapCut curve preset and a freeze are applied through `dispatch` (the
path the Inspector, the timeline toolbar and the agent all take) to clips of
REAL bar-coded sources (each frame carries its index); the timeline is then
exported by the compositor's single pass and decoded frame by frame. The
decoded frames must equal `frame_map.build_program_map` of the edited EDL —
the model the preview draws from — and the freeze must hold the frame the
export showed at the playhead BEFORE the edit.
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
from video_ai_editor.edl import speed_presets as SP  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402

FPS = 30


@pytest.fixture(scope="module")
def bars(tmp_path_factory):
    d = tmp_path_factory.mktemp("bars")
    out = {}
    for key, sid, rate in (("s30", 1, Fraction(30)), ("s25", 2, Fraction(25))):
        p = G.make_bar_source(d / f"{key}.mp4", G.SourceSpec(key=key, sid=sid, rate=rate, seconds=4.0))
        out[key] = (str(p), sid, G.probe_source(p))
    return out


def _export(store: EDLStore, tmp: Path, name: str) -> dict:
    path = compositor._render(store.edl, tmp / f"{name}.mp4", height=G.H, fps=FPS, preview=False,
                              cache_dir=tmp / "cache", chunked=False)
    return G.measure(path)


@pytest.mark.parametrize("key", ["s30", "s25"])
def test_every_preset_and_a_freeze_export_exactly_as_the_program_map(bars, tmp_path, key):
    src, sid, info = bars[key]
    store = EDLStore(tmp_path / "sess")
    store.edl.canvas = Canvas(w=G.W, h=G.H, fps=FPS)
    store.edl.canvas.loudness_lufs = None
    menu = [p for p in SP.PRESETS if p.menu]
    store.edl.get_track("v1").clips = [
        Clip(src=src, in_=0.2 + 0.1 * i, out=2.6 + 0.1 * i, start=2.4 * i, id=f"c_{p.id}")
        for i, p in enumerate(menu)]
    store.edl.recompute_duration()
    store.commit("init", {}, "init")
    for p in menu:
        dispatch(store, "set_speed", {"clip_id": f"c_{p.id}", "preset": p.label})
    sources, sids = {src: info}, {src: sid}
    before = G.expected_frames(store.edl, sources, sids)
    # Freeze inside the Hero clip, 0.4 s into it.
    hero = store.edl.get_clip("c_hero")[1]
    t = tb.quantize(hero.start + 0.4, FPS)
    k = tb.frame_of(t, FPS)
    dispatch(store, "freeze_frame", {"time": t, "duration": 1.0})
    model = G.expected_frames(store.edl, sources, sids)
    measured = _export(store, tmp_path, key)
    errs = G.compare(measured, model)
    assert errs == [], "export vs program map:\n" + "\n".join(errs[:12])
    n = tb.frame_of(1.0, FPS)
    assert measured["top"][k:k + n] == [before["top"][k]] * n
    assert measured["top"][:k] == before["top"][:k]
    assert len(measured["top"]) == len(before["top"]) + n
