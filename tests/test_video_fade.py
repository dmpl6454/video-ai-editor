"""Video fade (issue: no VISUAL fade existed — Properties' "Fade in/out"
fields only ever set clip.audio.fade_in/out).

Three layers under test:

1. `set_video_fade` dispatch semantics — sets/preserves/clamps the top-level
   Clip.video_fade_in/out fields; rejects text/sticker clips, audio-lane
   clips (music/vo/a1 — no video to fade), and v2/PIP clips (the PIP overlay
   chain can't take the fade fragment; rejecting loudly beats a dead field).

2. The renderer actually fades: a bright white lavfi clip with 0.8s in/out
   fades renders near-black at both edges and bright in the middle,
   pixel-verified on extracted frames (this also exercises the chunk render
   path, which reuses compositor._build_clip_video_chain).

3. chunks.fingerprint_clip is sensitive to video_fade_in/out — without this
   a cached chunk would silently render without the fade (the exact staleness
   bug class the audio-props fingerprint comment documents).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (
    EDL, Canvas, Clip, Sticker, TextClip, Track,
)
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render.chunks import fingerprint_clip

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# fixtures

def _store(tmp_path: Path) -> EDLStore:
    """Session with one v1 media clip, one v2/PIP clip, one music clip, one
    text clip, one sticker. Dispatch semantics never touch the media files,
    so fake src paths keep this fixture ffmpeg-free."""
    edl = EDL(
        canvas=Canvas(w=320, h=180, fps=30),
        tracks=[
            Track(id="v1", type="video", clips=[
                Clip(id="c1", src="/fake/src.mp4", in_=0.0, out=4.0, start=0.0),
            ]),
            Track(id="v2", type="video", z=1, clips=[
                Clip(id="c2", src="/fake/pip.mp4", in_=0.0, out=2.0, start=0.5),
            ]),
            Track(id="music", type="music", clips=[
                Clip(id="m1", src="/fake/music.mp3", in_=0.0, out=4.0, start=0.0),
            ]),
            Track(id="tx", type="text", clips=[
                TextClip(id="t1", text="hello", start=0.0, end=1.0),
            ]),
            Track(id="stickers", type="sticker", clips=[
                Sticker(id="s1", src="/fake/sticker.png", start=0.0, end=1.0),
            ]),
        ],
    )
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


# ---------------------------------------------------------------------------
# 1) dispatch semantics

def test_set_video_fade_sets_both_sides(tmp_path):
    store = _store(tmp_path)
    res = dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.8, "out_s": 0.5})
    _, c = store.edl.get_clip("c1")
    assert c.video_fade_in == pytest.approx(0.8)
    assert c.video_fade_out == pytest.approx(0.5)
    assert "0.80" in res["summary"] and "0.50" in res["summary"]
    # audio fade untouched — this is the VIDEO fade, not add_fade
    assert c.audio.fade_in == 0.0 and c.audio.fade_out == 0.0


def test_set_video_fade_omitted_side_preserved(tmp_path):
    """add_fade's single-key-update convention: an omitted side keeps its
    current value."""
    store = _store(tmp_path)
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.8, "out_s": 0.5})

    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 1.2})
    _, c = store.edl.get_clip("c1")
    assert c.video_fade_in == pytest.approx(1.2)
    assert c.video_fade_out == pytest.approx(0.5)

    dispatch(store, "set_video_fade", {"clip_id": "c1", "out_s": 0.9})
    _, c = store.edl.get_clip("c1")
    assert c.video_fade_in == pytest.approx(1.2)
    assert c.video_fade_out == pytest.approx(0.9)


def test_set_video_fade_clamps_negatives_to_zero(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.8, "out_s": 0.5})
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": -3.0, "out_s": -0.1})
    _, c = store.edl.get_clip("c1")
    assert c.video_fade_in == 0.0
    assert c.video_fade_out == 0.0


def test_set_video_fade_survives_json_roundtrip(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.7, "out_s": 0.3})
    store2 = EDLStore(tmp_path)
    _, c = store2.edl.get_clip("c1")
    assert c.video_fade_in == pytest.approx(0.7)
    assert c.video_fade_out == pytest.approx(0.3)


def test_set_video_fade_rejects_text_clip(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="media clips"):
        dispatch(store, "set_video_fade", {"clip_id": "t1", "in_s": 0.5})


def test_set_video_fade_rejects_sticker(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="media clips"):
        dispatch(store, "set_video_fade", {"clip_id": "s1", "in_s": 0.5})


def test_set_video_fade_rejects_audio_lane_clip(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="audio lane"):
        dispatch(store, "set_video_fade", {"clip_id": "m1", "in_s": 0.5})


def test_set_video_fade_rejects_pip_clip(tmp_path):
    """v2/PIP clips are rejected loudly: the PIP overlay chain has no setpts
    shift and needs an alpha (to-transparent) fade — shipping the field there
    would be a silent no-op."""
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="v1 only"):
        dispatch(store, "set_video_fade", {"clip_id": "c2", "in_s": 0.5})


def test_set_video_fade_unknown_clip(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        dispatch(store, "set_video_fade", {"clip_id": "nope", "in_s": 0.5})


# ---------------------------------------------------------------------------
# 2) render oracle: the fade actually reaches the pixels

@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
def test_video_fade_renders_black_edges(tmp_path):
    src = tmp_path / "white.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=white:s=320x180:d=2:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-shortest", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    edl = EDL(
        canvas=Canvas(w=320, h=180, fps=30),
        tracks=[Track(id="v1", type="video", clips=[
            Clip(id="c1", src=str(src), in_=0.0, out=2.0, start=0.0),
        ])],
    )
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.8, "out_s": 0.8})

    from video_ai_editor.render import render_preview
    pv = render_preview(store.edl, tmp_path)

    def mean_luma(at_s: float) -> float:
        frame = tmp_path / f"fade_frame_{at_s:.2f}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(at_s), "-i", str(pv.path),
             "-frames:v", "1", str(frame)],
            check=True, capture_output=True,
        )
        with Image.open(frame) as im:
            return ImageStat.Stat(im.convert("L")).mean[0]

    early = mean_luma(0.05)   # fade-in factor ~0.06 → near black
    mid = mean_luma(1.00)     # between fades → full white
    late = mean_luma(1.90)    # fade-out (st=1.2) factor ~0.13 → near black
    assert mid > 180, f"mid-clip should be bright white, got luma {mid:.1f}"
    assert early < 80, f"t=0.05 should be near-black (fade-in), got luma {early:.1f}"
    assert late < 80, f"t=1.90 should be near-black (fade-out), got luma {late:.1f}"
    assert early < mid and late < mid


# ---------------------------------------------------------------------------
# 3) chunk-cache fingerprint sensitivity

def test_fingerprint_clip_sensitive_to_video_fade():
    kw = dict(canvas_w=320, canvas_h=180, fps=30, encoder_args=["-c:v", "libx264"])
    c = Clip(id="c1", src="/fake.mp4", in_=0.0, out=2.0, start=0.0)
    base = fingerprint_clip(c, **kw)

    faded_in = c.model_copy(deep=True)
    faded_in.video_fade_in = 0.8
    assert fingerprint_clip(faded_in, **kw) != base

    faded_out = c.model_copy(deep=True)
    faded_out.video_fade_out = 0.8
    assert fingerprint_clip(faded_out, **kw) != base

    # And distinct from each other (in vs out are separate identity inputs).
    assert fingerprint_clip(faded_in, **kw) != fingerprint_clip(faded_out, **kw)


# ---------------------------------------------------------------------------
# 4) fades partition across a split/cut seam
#
# model_copy carries every field to both fragments, so without an explicit
# partition each half of a split inherits BOTH fades — giving the first half
# a fade-to-black tail and the second half a fade-from-black head in the
# middle of one continuous shot (pixel-proven black flash at the cut, and
# remove_silences strobing at every removed silence).

def test_split_at_partitions_video_fades(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.6, "out_s": 0.6})
    dispatch(store, "split_at", {"track": "v1", "time": 2.0})

    v1 = next(t for t in store.edl.tracks if t.id == "v1")
    assert len(v1.clips) == 2
    left, right = v1.clips[0], v1.clips[1]
    # Outer edges keep their fade; the interior seam has none.
    assert left.video_fade_in == pytest.approx(0.6)
    assert left.video_fade_out == 0.0
    assert right.video_fade_in == 0.0
    assert right.video_fade_out == pytest.approx(0.6)


def test_cut_range_middle_partitions_video_fades(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_video_fade", {"clip_id": "c1", "in_s": 0.5, "out_s": 0.5})
    # Remove an interior span, splitting the 4s clip into two fragments.
    dispatch(store, "cut_range", {"track": "v1", "start": 1.5, "end": 2.5})

    v1 = next(t for t in store.edl.tracks if t.id == "v1")
    assert len(v1.clips) == 2
    left, right = v1.clips[0], v1.clips[1]
    assert left.video_fade_in == pytest.approx(0.5)
    assert left.video_fade_out == 0.0
    assert right.video_fade_in == 0.0
    assert right.video_fade_out == pytest.approx(0.5)


def test_split_of_unfaded_clip_stays_unfaded(tmp_path):
    """The partition must not INTRODUCE a fade on a clip that had none."""
    store = _store(tmp_path)
    dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    v1 = next(t for t in store.edl.tracks if t.id == "v1")
    assert len(v1.clips) == 2
    for c in v1.clips:
        assert c.video_fade_in == 0.0
        assert c.video_fade_out == 0.0
