import tempfile
from pathlib import Path
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip


def test_commit_creates_op_and_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        store = EDLStore(Path(tmp))
        store.edl.tracks[0].clips.append(Clip(src="x.mp4", in_=0.0, out=5.0, start=0.0))
        store.commit("add_clip", {"src": "x.mp4"}, "Added clip x.mp4")
        assert len(store.ops.ops) == 1
        assert store.ops.last().tool == "add_clip"
        assert (Path(tmp) / "edl.json").exists()


def test_undo_restores_previous_state():
    with tempfile.TemporaryDirectory() as tmp:
        store = EDLStore(Path(tmp))
        # initial commit so we have something to undo *to*
        store.commit("init", {}, "Initial")
        store.edl.tracks[0].clips.append(Clip(src="x.mp4", in_=0.0, out=5.0, start=0.0))
        store.commit("add_clip", {}, "Added x.mp4")
        assert len(store.edl.tracks[0].clips) == 1
        ok = store.undo()
        assert ok
        assert len(store.edl.tracks[0].clips) == 0
        assert len(store.ops.ops) == 1
