"""Raw MPEG-TS / PS sources added by path (the MCP / agent add_clip route)
export the frames the timeline asks for (review RD3).

Uploads are normalised on import; `add_clip` took any readable path as is.
For an UN-INDEXED container (.ts, .m2ts, .mpg, .vob, raw ES) ffmpeg's input
`-ss` lands after the GOP's keyframe, and the 1x chain rebases at the first
decoded frame: a TS (g=60) clip at in 3.3 s started at source frame 120, not
99 (60 of 60 frames wrong); a single-keyframe file exported NO picture at all
and the render still "succeeded" with an audio-only mp4.

* add_clip now normalises such a source once (CFR H.264, the upload
  recipe, no transcription) and points the clip at that copy;
* a render whose output has no video stream fails loudly;
* a single-keyframe raw stream placed directly in an EDL renders the frames
  the program map plans (the anchored seek, wave E) — it no longer fails.
"""
from __future__ import annotations

import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402
from video_ai_editor.render.frame_map import build_program_map  # noqa: E402

FB = (1 << G.FRAME_BITS) - 1


@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> dict[str, str]:
    d = tmp_path_factory.mktemp("rawts")
    src = G.make_bar_source(d / "b30.mp4", G.SourceSpec(key="b30", sid=1, rate=Fraction(30), seconds=12.0))
    out = {"mp4": str(src)}
    recipes = {
        "ts": ["-c:v", "libx264", "-crf", "8", "-g", "60", "-bf", "2", "-c:a", "aac", "-f", "mpegts"],
        "m2ts": ["-c:v", "libx264", "-crf", "8", "-g", "30", "-bf", "2", "-c:a", "ac3", "-f", "mpegts",
                 "-mpegts_m2ts_mode", "1"],
        "mpg": ["-c:v", "mpeg2video", "-q:v", "2", "-g", "15", "-bf", "2", "-c:a", "mp2", "-f", "dvd"],
    }
    for ext, args in recipes.items():
        p = d / f"bars.{ext}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *args, str(p)], check=True)
        out[ext] = str(p)
    one_key = d / "onekey.m2ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", "3", "-c:v", "libx264",
                    "-x264-params", "keyint=1000:min-keyint=1000:scenecut=0", "-c:a", "aac",
                    "-f", "mpegts", "-mpegts_m2ts_mode", "1", str(one_key)], check=True)
    out["onekey"] = str(one_key)
    return out


def _store(tmp: Path) -> EDLStore:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=30)
    st.edl.canvas.loudness_lufs = None
    st.commit("c", {}, "c")
    return st


def _frames(st: EDLStore, tmp: Path, name: str) -> list[int]:
    p = compositor._render(st.edl, tmp / f"{name}.mp4", height=G.H, fps=30, preview=False,
                           cache_dir=tmp / "cache", chunked=False)
    return [c & FB for c in G.measure(p)["top"]]


@pytest.mark.parametrize("ext", ["ts", "m2ts", "mpg"])
def test_a_raw_stream_added_by_path_exports_the_frames_it_asks_for(tmp_path, bars, ext):
    st = _store(tmp_path / "s")
    cid = dispatch(st, "add_clip", {"track": "v1", "src": bars[ext], "in": 3.3, "out": 5.3,
                                    "start": 0})["clip_id"]
    c = st.edl.get_clip(cid)[1]
    assert Path(c.src).suffix == ".mp4", "the clip plays the normalised copy"
    got = _frames(st, tmp_path, ext)
    # The normalised copy keeps the container's A/V clock (a TS whose video
    # starts 21 ms after its sound shows frame 98 at 3.3 s, as an UPLOAD of
    # the same file does); what matters is a contiguous run from there — it
    # was 120.. (the next keyframe) — and the preview engine's model of the
    # same file agreeing with it.
    assert abs(got[0] - 99) <= 1 and got == [got[0] + k for k in range(60)], got[:5]
    model = build_program_map(st.edl, lambda _s: G.probe_source(Path(c.src))).frame
    own = [x & FB for x in G.measure(Path(c.src))["top"]]     # the copy's frame i shows code own[i]
    assert got == [own[i] for i in model]


def test_an_indexed_source_is_used_as_is(tmp_path, bars):
    st = _store(tmp_path / "s")
    cid = dispatch(st, "add_clip", {"track": "v1", "src": bars["mp4"], "in": 3.3, "out": 5.3,
                                    "start": 0})["clip_id"]
    assert st.edl.get_clip(cid)[1].src == str(Path(bars["mp4"]).resolve())


def test_the_same_raw_stream_is_normalised_once(tmp_path, bars):
    st = _store(tmp_path / "s")
    a = dispatch(st, "add_clip", {"track": "v1", "src": bars["ts"], "in": 0, "out": 1, "start": 0})
    b = dispatch(st, "add_clip", {"track": "v2", "src": bars["ts"], "in": 1, "out": 2, "start": 0})
    assert st.edl.get_clip(a["clip_id"])[1].src == st.edl.get_clip(b["clip_id"])[1].src


def test_a_one_keyframe_raw_stream_placed_directly_renders_the_planned_frames(tmp_path, bars):
    """The raw single-keyframe stream placed directly in an EDL (a legacy
    project, or any path that skips add_clip). The input seek used to land
    past the only keyframe and the chain emitted NO frame; the anchored seek
    (wave E) decodes from the stream's start, so the render now shows exactly
    the frames the program map plans — every one of the 60, in order. This
    used to assert the render FAILED; that was the right answer only while
    the renderer could not read the file, so it is now held to the frame map
    like every other source."""
    st = _store(tmp_path / "s")
    st.edl.get_track("v1").clips = [Clip(src=bars["onekey"], in_=0.5, out=2.5, start=0.0, id="a")]
    st.edl.recompute_duration()
    got = _frames(st, tmp_path, "onekey")
    model = build_program_map(st.edl, lambda _s: G.probe_source(Path(bars["onekey"]))).frame
    own = [x & FB for x in G.measure(Path(bars["onekey"]))["top"]]   # source frame i shows own[i]
    assert len(model) == 60 and len(got) == 60, (len(model), len(got))
    assert got == [own[i] for i in model], (got[:5], [own[i] for i in model][:5])
    assert got == [got[0] + k for k in range(60)], got[:5]          # contiguous, no held frame


def _sound_only(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3",
                    "-c:a", "aac", str(path)], check=True)
    return path


def test_a_render_with_no_picture_fails_loudly(tmp_path):
    """A clip on v1 whose file has no video stream at all: the render must
    raise, never hand back a sound-only mp4 as a successful export."""
    st = _store(tmp_path / "s")
    aud = _sound_only(tmp_path / "tone.m4a")
    st.edl.get_track("v1").clips = [Clip(src=str(aud), in_=0.5, out=2.5, start=0.0, id="a")]
    st.edl.recompute_duration()
    dst = tmp_path / "o.mp4"
    # Either loud failure is right: ffmpeg refusing the graph (no video stream
    # to map) or the post-render picture check. Named, so an unrelated
    # RuntimeError (a Python bug) cannot pass for "failed loudly".
    with pytest.raises(RuntimeError, match=r"ffmpeg render failed|no picture"):
        compositor._render(st.edl, dst, height=G.H, fps=30, preview=False,
                           cache_dir=tmp_path / "cache", chunked=False)
    assert not dst.exists(), "a failed render must not leave a file to be served"


def test_the_picture_check_rejects_a_sound_only_output(tmp_path):
    """The guard itself (review RD3): ffmpeg can exit 0 having written only
    sound. `_check_picture` must refuse that file and delete it, whatever
    input produced it."""
    st = _store(tmp_path / "s")
    st.edl.get_track("v1").clips = [Clip(src="x.mp4", in_=0.0, out=2.0, start=0.0, id="a")]
    st.edl.recompute_duration()
    out = _sound_only(tmp_path / "o.mp4")
    with pytest.raises(RuntimeError, match="no picture"):
        compositor._check_picture(st.edl, out, 30)
    assert not out.exists(), "a sound-only render is never kept to be served later"
