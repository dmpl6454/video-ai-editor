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
* a render whose output has no video stream fails loudly.
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


def test_a_render_with_no_picture_fails_instead_of_writing_sound_only(tmp_path, bars):
    """The raw single-keyframe stream placed directly in an EDL (a legacy
    project, or any path that skips add_clip): the seek lands past the only
    keyframe and the chain emits no frame."""
    st = _store(tmp_path / "s")
    st.edl.get_track("v1").clips = [Clip(src=bars["onekey"], in_=0.5, out=2.5, start=0.0, id="a")]
    st.edl.recompute_duration()
    with pytest.raises(RuntimeError, match="no picture"):
        compositor._render(st.edl, tmp_path / "o.mp4", height=G.H, fps=30, preview=False,
                           cache_dir=tmp_path / "cache", chunked=False)
