"""Transitions must render regardless of what feeds the xfade node (RC-TB).

`xfade`'s config_output requires both inputs to share a timebase. In this
pipeline they routinely don't: a link out of `concat` or a `color=` gap filler is
normalised to 1/1000000, a raw per-clip chain keeps the demuxer's own tbn, and a
mask/alphamerge branch keeps another. Any mismatch aborts the ENTIRE graph with
"Input link parameters … do not match", which the UI surfaced as "corrupt frames
or an unusual codec".

The reported repro was "add a transition, then Split (Cmd-B)" — that works by
making the seam no longer the first, so its left input has been through `concat`.
But the trigger set is wider, and two of these cases fail on the FIRST seam with
no split involved at all:

  * mixed container timebases (mp4 + mkv)
  * a Mask on the left clip

Both are covered below precisely because the original bug report did not mention
them; fixing only the reported flow would have left them broken.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, Mask, empty_edl
from video_ai_editor.render import render_preview

CLIP_LEN = 3.0
TDUR = 0.5


def _mk(path: Path, duration: float = 4.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={duration}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def _duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


def _has_streams(path: Path) -> tuple[bool, bool]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    kinds = {s["codec_type"] for s in json.loads(proc.stdout)["streams"]}
    return "video" in kinds, "audio" in kinds


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("tb_src")
    return {
        "a": _mk(d / "a.mp4"),
        "b": _mk(d / "b.mp4"),
        # A different CONTAINER gives a different demuxer timebase, which is
        # enough to break a first-seam xfade on its own.
        "mkv": _mk(d / "c.mkv"),
    }


def _store(tmp_path: Path, srcs: list[Path], *, starts: list[float] | None = None,
           mask_first: bool = False) -> EDLStore:
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    v1 = edl.get_track("v1")
    starts = starts or [i * CLIP_LEN for i in range(len(srcs))]
    for src, start in zip(srcs, starts):
        c = Clip(src=str(src), in_=0.0, out=CLIP_LEN, start=start)
        if mask_first and not v1.clips:
            c.mask = Mask(type="circle", feather=10)
        v1.clips.append(c)
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def _assert_renders(store: EDLStore, tmp_path: Path, expected: float) -> None:
    res = render_preview(store.edl, tmp_path, height=180)
    has_v, has_a = _has_streams(res.path)
    assert has_v and has_a, "transition render lost a stream"
    got = _duration(res.path)
    assert abs(got - expected) < 0.25, f"expected ~{expected:.2f}s, got {got:.2f}s"


def test_transition_then_split_renders(tmp_path, sources):
    """THE REPORTED FLOW: add a transition, then split a clip."""
    store = _store(tmp_path, [sources["a"], sources["b"]])
    first_id = store.edl.get_track("v1").clips[0].id
    dispatch(store, "add_transition", {"at": CLIP_LEN, "type": "fade", "duration": TDUR})
    dispatch(store, "split_at", {"time": CLIP_LEN / 2, "clip_id": first_id})
    _assert_renders(store, tmp_path, 2 * CLIP_LEN - TDUR)


def test_transition_on_second_seam_renders(tmp_path, sources):
    """The general case: any seam that is not the first has a concat on its left."""
    store = _store(tmp_path, [sources["a"], sources["b"], sources["a"]])
    dispatch(store, "add_transition", {"at": 2 * CLIP_LEN, "type": "fade", "duration": TDUR})
    _assert_renders(store, tmp_path, 3 * CLIP_LEN - TDUR)


def test_transition_first_seam_mixed_container_renders(tmp_path, sources):
    """Not in any bug report: mp4 + mkv breaks the FIRST seam by itself."""
    store = _store(tmp_path, [sources["a"], sources["mkv"]])
    dispatch(store, "add_transition", {"at": CLIP_LEN, "type": "fade", "duration": TDUR})
    _assert_renders(store, tmp_path, 2 * CLIP_LEN - TDUR)


def test_transition_first_seam_masked_left_clip_renders(tmp_path, sources):
    """Not in any bug report: a Mask on the left clip breaks the FIRST seam."""
    store = _store(tmp_path, [sources["a"], sources["b"]], mask_first=True)
    dispatch(store, "add_transition", {"at": CLIP_LEN, "type": "fade", "duration": TDUR})
    _assert_renders(store, tmp_path, 2 * CLIP_LEN - TDUR)


def test_transition_with_leading_gap_renders(tmp_path, sources):
    """A leading offset injects a `color=` filler segment before the seam."""
    store = _store(tmp_path, [sources["a"], sources["b"]],
                   starts=[1.0, 1.0 + CLIP_LEN])
    dispatch(store, "add_transition", {"at": 1.0 + CLIP_LEN, "type": "fade", "duration": TDUR})
    _assert_renders(store, tmp_path, 1.0 + 2 * CLIP_LEN - TDUR)


# ---- controls: these already worked and must keep working ----

def test_single_transition_still_renders(tmp_path, sources):
    store = _store(tmp_path, [sources["a"], sources["b"]])
    dispatch(store, "add_transition", {"at": CLIP_LEN, "type": "fade", "duration": TDUR})
    _assert_renders(store, tmp_path, 2 * CLIP_LEN - TDUR)


def test_transitions_on_both_seams_still_render(tmp_path, sources):
    """xfade -> xfade already worked (xfade preserves its first input's tb)."""
    store = _store(tmp_path, [sources["a"], sources["b"], sources["a"]])
    dispatch(store, "add_transition", {"at": CLIP_LEN, "type": "fade", "duration": TDUR})
    dispatch(store, "add_transition", {"at": 2 * CLIP_LEN, "type": "fade", "duration": TDUR})
    _assert_renders(store, tmp_path, 3 * CLIP_LEN - 2 * TDUR)


def test_no_transition_plain_concat_unaffected(tmp_path, sources):
    """The settb nodes must be confined to the xfade branch."""
    store = _store(tmp_path, [sources["a"], sources["b"], sources["a"]])
    _assert_renders(store, tmp_path, 3 * CLIP_LEN)
