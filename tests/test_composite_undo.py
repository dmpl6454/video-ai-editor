"""Composite tools must be ONE undo step.

Found while wiring the AI panel: `remove_silences` let every inner `cut_range`
commit its own snapshot and then added a "summary" commit whose snapshot was
identical to the previous one — so the first ⌘Z did nothing visible, and a
clip with N silences needed N+1 undos. `EDLStore.batch()` folds the inner
commits away; these tests pin that for the store itself and for each
composite that goes through it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import Clip, TextClip
from video_ai_editor.edl.snapshot import EDLStore


def _clip_with_gap(tmp_path: Path) -> Path:
    """6s clip: tone 0-2s, silence 2-4s, tone 4-6s (silencedetect finds one gap)."""
    src = tmp_path / "gap.mp4"
    expr = "0.6*sin(440*2*PI*t)*(between(t\\,0\\,2)+between(t\\,4\\,6))"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=6:r=30",
         "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True,
    )
    return src


def _store_with_clip(tmp_path: Path) -> EDLStore:
    store = EDLStore(tmp_path / "sess")
    src = _clip_with_gap(tmp_path)
    dispatch(store, "add_clip", {"track": "v1", "src": str(src),
                                 "in": 0, "out": 6, "start": 0})
    return store


def _clips(store: EDLStore) -> list[tuple[str, float, float, float]]:
    return [(t.id, round(c.start, 3), round(c.in_, 3), round(c.out, 3))
            for t in store.edl.tracks for c in t.clips if isinstance(c, Clip)]


def _snaps(store: EDLStore) -> int:
    return len(list(store.snapshots_dir.glob("*.json")))


def _ops(store: EDLStore) -> int:
    return len(store.ops.ops)


# ---------------------------------------------------------------- store level

def test_batch_folds_inner_commits_into_one_undo_step(tmp_path: Path):
    store = EDLStore(tmp_path / "sess")
    original_bg = store.edl.canvas.bg
    ops0, snaps0 = _ops(store), _snaps(store)

    with store.batch():
        store.edl.canvas.bg = "#101010"
        store.commit("inner_a", {}, "inner a")
        store.edl.canvas.bg = "#202020"
        store.commit("inner_b", {}, "inner b")
        # Nothing recorded — and nothing persisted — until the batch closes.
        assert (_ops(store), _snaps(store)) == (ops0, snaps0)
        assert not store.edl_path.exists()
    store.commit("composite", {}, "composite")

    assert (_ops(store), _snaps(store)) == (ops0 + 1, snaps0 + 1)
    assert store.ops.last().tool == "composite"
    assert store.undo() and store.edl.canvas.bg == original_bg
    assert store.redo() and store.edl.canvas.bg == "#202020"


def test_batch_is_reentrant(tmp_path: Path):
    store = EDLStore(tmp_path / "sess")
    ops0 = _ops(store)
    with store.batch():
        with store.batch():
            store.edl.canvas.bg = "#303030"
            store.commit("innermost", {}, "x")
        store.commit("middle", {}, "y")
        assert _ops(store) == ops0
    store.commit("outer", {}, "z")
    assert _ops(store) == ops0 + 1 and store.ops.last().tool == "outer"


def test_commit_outside_batch_is_unaffected(tmp_path: Path):
    store = EDLStore(tmp_path / "sess")
    ops0, snaps0 = _ops(store), _snaps(store)
    store.edl.canvas.bg = "#404040"
    store.commit("plain", {}, "plain")
    assert (_ops(store), _snaps(store)) == (ops0 + 1, snaps0 + 1)


def test_batch_rolls_back_memory_when_the_block_raises(tmp_path: Path):
    """The store is cached process-wide (main._STORES) and nothing reloads it
    on error, so a composite that dies halfway must not leave its partial
    edits in `store.edl` with no op behind them — the next unrelated commit
    would otherwise fold them in under its own name."""
    store = EDLStore(tmp_path / "sess")
    original_bg = store.edl.canvas.bg
    hash0, ops0, snaps0 = store.edl.hash(), _ops(store), _snaps(store)

    with pytest.raises(RuntimeError, match="boom"):
        with store.batch():
            store.edl.canvas.bg = "#505050"
            store.commit("inner", {}, "inner")          # folded — nothing on disk
            raise RuntimeError("boom")

    assert store.edl.canvas.bg == original_bg
    assert store.edl.hash() == hash0
    assert (_ops(store), _snaps(store)) == (ops0, snaps0)
    assert store._batch_depth == 0
    # A following plain edit records only itself, and one undo takes only it back.
    store.edl.canvas.bg = "#606060"
    store.commit("next", {}, "next")
    assert (_ops(store), _snaps(store)) == (ops0 + 1, snaps0 + 1)
    assert store.undo() and store.edl.canvas.bg == original_bg


def test_nested_batch_failure_rolls_back_only_its_own_block(tmp_path: Path):
    """The apply_template pattern: an inner composite fails, the outer one
    logs it and carries on. The inner block's edits must be gone, the outer
    block's own edits must survive."""
    store = EDLStore(tmp_path / "sess")
    ops0 = _ops(store)
    with store.batch():
        store.edl.canvas.bg = "#111111"
        try:
            with store.batch():
                store.edl.canvas.bg = "#222222"
                store.commit("inner", {}, "inner")
                raise RuntimeError("inner failed")
        except RuntimeError:
            pass
        assert store.edl.canvas.bg == "#111111"
        assert store._batch_depth == 1
    store.commit("outer", {}, "outer")
    assert _ops(store) == ops0 + 1 and store.edl.canvas.bg == "#111111"


# ------------------------------------------------------------- composites

def test_remove_silences_is_one_undo_step(tmp_path: Path):
    store = _store_with_clip(tmp_path)
    before, ops0, snaps0 = _clips(store), _ops(store), _snaps(store)

    result = dispatch(store, "remove_silences", {})
    assert result["cuts"] >= 1
    cut = _clips(store)
    assert len(cut) > len(before)
    # exactly one op and one snapshot for the whole pass
    assert (_ops(store), _snaps(store)) == (ops0 + 1, snaps0 + 1)
    assert store.ops.last().tool == "remove_silences"

    dispatch(store, "undo", {})
    assert _clips(store) == before, "one undo must restore the pre-pass timeline"
    dispatch(store, "redo", {})
    assert _clips(store) == cut


def test_remove_fillers_is_one_undo_step(tmp_path: Path):
    store = _store_with_clip(tmp_path)
    # get_transcript reads uploads/<stem>/ingest.json next to the V1 source
    # (see _current_v1_ingest_json), under a "transcript" key.
    src = Path(store.edl.get_track("v1").clips[0].src)
    (src.parent / "ingest.json").write_text(json.dumps({"transcript": {
        "language": "en", "duration": 6.0,
        "segments": [{"id": 0, "start": 0.0, "end": 6.0, "text": "um hi uh there",
                      "words": [{"word": "um", "start": 0.5, "end": 0.8},
                                {"word": "hi", "start": 0.9, "end": 1.1},
                                {"word": "uh", "start": 4.5, "end": 4.8},
                                {"word": "there", "start": 5.0, "end": 5.4}]}],
    }}))
    before, ops0 = _clips(store), _ops(store)

    result = dispatch(store, "remove_fillers", {})
    assert result["cuts"] == 2
    assert _ops(store) == ops0 + 1 and store.ops.last().tool == "remove_fillers"

    dispatch(store, "undo", {})
    assert _clips(store) == before


def test_apply_hook_stack_is_one_undo_step(tmp_path: Path):
    store = _store_with_clip(tmp_path)
    hash_before, ops0 = store.edl.hash(), _ops(store)

    dispatch(store, "apply_hook_stack", {"text": "watch this"})
    hooks = [c for t in store.edl.tracks for c in t.clips
             if isinstance(c, TextClip) and c.role == "hook"]
    assert len(hooks) == 1
    assert _ops(store) == ops0 + 1 and store.ops.last().tool == "apply_hook_stack"

    dispatch(store, "undo", {})
    assert store.edl.hash() == hash_before, "hook text + punch-in + fade all revert together"


def test_apply_template_is_one_undo_step(tmp_path: Path):
    """The only composite that NESTS a batch in production (apply_template →
    apply_hook_stack → add_super_text); the template's own edits, the hook
    stack's text/punch-in/fade and the commit all fold into one op."""
    store = _store_with_clip(tmp_path)
    hash_before, ops0, snaps0 = store.edl.hash(), _ops(store), _snaps(store)

    result = dispatch(store, "apply_template", {"name": "tech_tip", "inputs": {"hook": "x"}})
    assert any(a.startswith("hook_stack(") for a in result["applied"]), result
    hooks = [c for t in store.edl.tracks for c in t.clips
             if isinstance(c, TextClip) and c.role == "hook"]
    assert hooks, "the template's hook line must have landed"
    assert (_ops(store), _snaps(store)) == (ops0 + 1, snaps0 + 1)
    assert store.ops.last().tool == "apply_template"

    dispatch(store, "undo", {})
    assert store.edl.hash() == hash_before, "template + hook stack revert together"


def test_auto_cut_to_beats_is_one_undo_step(tmp_path: Path, monkeypatch):
    """Beats come from a stand-in `librosa` so the test runs — and means the
    same thing — with or without the optional dependency installed; the
    handler imports it lazily by name, so sys.modules is the seam."""
    import sys
    import types
    fake_librosa = types.SimpleNamespace(
        load=lambda path, sr, mono, offset, duration: ([], sr),
        beat=types.SimpleNamespace(beat_track=lambda y, sr: (120.0, [0, 1, 2])),
        frames_to_time=lambda frames, sr: types.SimpleNamespace(tolist=lambda: [1.0, 2.5, 4.0]),
    )
    monkeypatch.setitem(sys.modules, "librosa", fake_librosa)
    store = _store_with_clip(tmp_path)
    music = tmp_path / "music.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=f=200:duration=6", "-c:a", "pcm_s16le", str(music)],
                   check=True, capture_output=True)
    dispatch(store, "add_clip", {"track": "music", "src": str(music),
                                 "in": 0, "out": 6, "start": 0})
    before, ops0 = _clips(store), _ops(store)

    result = dispatch(store, "auto_cut_to_beats", {"subdivision": 1})
    assert result["splits"] == 3
    assert len(_clips(store)) == len(before) + 3
    assert _ops(store) == ops0 + 1 and store.ops.last().tool == "auto_cut_to_beats"

    dispatch(store, "undo", {})
    assert _clips(store) == before
