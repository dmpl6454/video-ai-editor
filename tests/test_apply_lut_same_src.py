"""Final QA: "apply the warm filter to every clip" on a project whose last
clip already had Warm stacked a SECOND warm.cube on it (the grade applied
twice, two 'LUT · Warm' chips) while the other clips got one. The same LUT
again now updates the existing one's intensity; a different LUT still stacks
unless `replace` is asked for."""
from __future__ import annotations

import tempfile
from pathlib import Path

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track


def _store() -> EDLStore:
    tmp = tempfile.mkdtemp()
    src = str(Path(tmp) / "missing" / "x.mp4")
    edl = EDL(canvas=Canvas(w=1920, h=1080, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id=f"c{i}", src=src, in_=0, out=2, start=2 * i)
                                            for i in range(4)])])
    edl.recompute_duration()
    (Path(tmp) / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(Path(tmp))


def _luts(store: EDLStore, cid: str) -> list[tuple[str, float]]:
    c = store.edl.get_clip(cid)[1]
    return [(Path(e.params["src"]).name, e.params["intensity"]) for e in c.effects if e.type == "lut"]


def test_the_same_lut_on_every_clip_does_not_stack_on_a_clip_that_has_it():
    store = _store()
    dispatch(store, "apply_lut", {"clip_id": "c3", "src": "warm.cube"})
    dispatch(store, "apply_lut", {"src": "warm.cube", "intensity": 0.8})
    for cid in ("c0", "c1", "c2", "c3"):
        assert _luts(store, cid) == [("warm.cube", 0.8)], cid


def test_a_different_lut_still_stacks_without_replace():
    store = _store()
    dispatch(store, "apply_lut", {"clip_id": "c0", "src": "warm.cube"})
    dispatch(store, "apply_lut", {"clip_id": "c0", "src": "cool.cube"})
    assert [n for n, _ in _luts(store, "c0")] == ["warm.cube", "cool.cube"]
