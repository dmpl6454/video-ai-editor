"""Final QA round 3 (prompt-assistant-backend): dispatch keeps what sits on a
picture WITH that picture, names times as the ruler shows them, and gives a
clip added by path the upload's normalisation.

* set_speed on a captioned main-track clip ripple-DELETED the tail of the
  old footprint: a 2x speed-up dropped every caption, title and sticker in
  the clip's second half (one collapsed stub kept per lane), and left the
  first half's captions at their old times while the speech played twice as
  fast. A slow-down or a speed curve left the cues where they were. Overlays
  over the clip now map through its time map; later overlays shift.
* After a transition, History printed a sticker/text at its layout time
  (06:15-09:15), not the ruler's (06:00-09:00).
* add_clip by path (MCP / agent) skipped the upload's normalisation: a JPEG
  became a one-frame clip that could not be lengthened, and HLG/PQ footage
  exported untonemapped and tagged HDR (re-tagging every SDR clip after it).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Sticker, TextClip, Track, Transform

W, H, FPS = 160, 90, 30


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> str:
    dst = tmp_path_factory.mktemp("media") / "talk.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=22:r={FPS}",
                    "-f", "lavfi", "-i", "sine=f=440:d=22",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
                   check=True, capture_output=True)
    return str(dst)


def _cue(i: int, a: float, b: float) -> TextClip:
    return TextClip(id=f"cue{i}", text=f"cue {i}", start=a, end=b, role="caption")


def _store(tmp: Path, src: str, *, cues=(), stickers=(), pip=None) -> EDLStore:
    clips = [Clip(id="a", src=src, in_=0.0, out=14.1, start=0.0),
             Clip(id="b", src=src, in_=14.1, out=20.0, start=14.1)]
    tracks = [Track(id="v1", type="video", clips=clips),
              Track(id="v2", type="video", z=1, clips=[pip] if pip else []),
              Track(id="captions", type="captions", z=4, clips=list(cues)),
              Track(id="st", type="sticker", z=5, clips=list(stickers)),
              Track(id="a1", type="audio", z=0)]
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=tracks)
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def _spans(store: EDLStore, track: str) -> dict[str, tuple[float, float]]:
    return {c.id: (round(c.start, 3), round(c.end, 3)) for c in store.edl.get_track(track).clips}


# ------------------------------------------------------------------ set_speed keeps the overlays

CUES = [(0.0, 1.5), (2.0, 4.0), (6.0, 6.74), (9.52, 11.52), (12.62, 14.03)]


def test_speed_up_keeps_every_cue_over_the_clip_at_half_its_offset(tmp_path, media):
    st = _store(tmp_path / "s", media,
                cues=[_cue(i, a, b) for i, (a, b) in enumerate(CUES)] + [_cue(9, 15.77, 17.91)])
    dispatch(st, "set_speed", {"clip_id": "a", "factor": 2})
    got = _spans(st, "captions")
    assert len(got) == 6, got
    for i, (a, b) in enumerate(CUES):
        assert got[f"cue{i}"] == pytest.approx((a / 2, b / 2), abs=1e-3), (i, got)
    # a cue over the NEXT clip follows that picture (final QA, K1): the same
    # 1.67 s into b, wherever b now starts (the removed 7.05 s, snapped to
    # the frame grid by the magnetic repack)
    b = st.edl.get_clip("b")[1]
    assert b.start == pytest.approx(7.05, abs=1 / FPS)
    assert got["cue9"] == pytest.approx((b.start + 1.67, b.start + 3.81), abs=1e-3)


def test_a_sticker_and_a_pip_in_the_second_half_follow_the_speed_up(tmp_path, media):
    stk = Sticker(id="s1", src="x.png", start=10.0, end=12.0, transform=Transform(x=10, y=10))
    pip = Clip(id="p", src=media, in_=0.0, out=2.0, start=8.0)
    st = _store(tmp_path / "s", media, stickers=[stk], pip=pip)
    dispatch(st, "set_speed", {"clip_id": "a", "factor": 2})
    assert _spans(st, "st") == {"s1": (5.0, 6.0)}
    assert st.edl.get_clip("p")[1].start == pytest.approx(4.0)


def test_slow_down_stretches_the_cues_over_the_clip(tmp_path, media):
    st = _store(tmp_path / "s", media,
                cues=[_cue(i, a, b) for i, (a, b) in enumerate(CUES)] + [_cue(9, 15.77, 17.91)])
    dispatch(st, "set_speed", {"clip_id": "a", "factor": 0.5})
    got = _spans(st, "captions")
    for i, (a, b) in enumerate(CUES):
        assert got[f"cue{i}"] == pytest.approx((a * 2, b * 2), abs=1e-3), (i, got)
    assert got["cue9"] == pytest.approx((15.77 + 14.1, 17.91 + 14.1), abs=1e-3)


def test_a_speed_curve_moves_each_cue_to_where_its_words_now_play(tmp_path, media):
    st = _store(tmp_path / "s", media, cues=[_cue(0, 15.77, 17.91), _cue(1, 18.15, 19.73)])
    dispatch(st, "set_speed", {"clip_id": "b", "preset": "hero"})
    b = st.edl.get_clip("b")[1]
    got = _spans(st, "captions")
    for cid, (a, e) in (("cue0", (15.77, 17.91)), ("cue1", (18.15, 19.73))):
        want = (b.start + b.timeline_offset_at(a - 14.1), b.start + b.timeline_offset_at(e - 14.1))
        assert got[cid] == pytest.approx(want, abs=2e-3), (cid, got[cid], want)
    # and they did move: the curve slows the middle of the clip
    assert got["cue1"][0] > 18.15 + 0.5


def test_speed_change_leaves_a_locked_caption_lane_alone(tmp_path, media):
    st = _store(tmp_path / "s", media, cues=[_cue(9, 15.77, 17.91)])
    st.edl.get_track("captions").locked = True
    st.commit("lock", {}, "lock")
    dispatch(st, "set_speed", {"clip_id": "a", "factor": 2})
    assert _spans(st, "captions") == {"cue9": (15.77, 17.91)}


# ------------------------------------------------------------------ History names the ruler's time

def _dissolved(tmp: Path, src: str) -> EDLStore:
    clips = [Clip(id="a", src=src, in_=0.0, out=5.0, start=0.0),
             Clip(id="b", src=src, in_=5.0, out=15.0, start=5.0)]
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None),
              tracks=[Track(id="v1", type="video", clips=clips),
                      Track(id="tx", type="text", z=3)])
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    st = EDLStore(tmp)
    dispatch(st, "add_transition", {"at": 5.0, "type": "dissolve", "duration": 0.5})
    return st


def test_sticker_text_and_timing_summaries_use_the_ruler_after_a_transition(tmp_path, media):
    st = _dissolved(tmp_path / "s", media)
    from video_ai_editor.render.clock import render_time
    assert render_time(st.edl, 6.5) == pytest.approx(6.0)
    png = tmp_path / "sun.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=yellow:s=32x32",
                    "-frames:v", "1", str(png)], check=True)
    s = dispatch(st, "add_sticker", {"src": str(png), "start": 6.5, "end": 9.5})["summary"]
    assert "6.00–9.00" in s, s
    t = dispatch(st, "add_text", {"text": "Hi", "start": 6.5, "end": 9.5})["summary"]
    assert "6.00–9.00" in t, t
    tid = next(c.id for t in st.edl.tracks for c in t.clips if isinstance(c, TextClip))
    m = dispatch(st, "set_clip_timing", {"clip_id": tid, "start": 7.5, "end": 8.5})["summary"]
    assert "7.00–8.00" in m, m


# ------------------------------------------------------------------ add_clip by path is normalised

def _probe(path: str, entries: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          f"stream={entries}", "-of", "json", path],
                         check=True, capture_output=True, text=True)
    return json.loads(out.stdout)["streams"][0]


def _empty(tmp: Path, w=W, h=H) -> EDLStore:
    edl = EDL(canvas=Canvas(w=w, h=h, fps=FPS, loudness_lufs=None),
              tracks=[Track(id="v1", type="video"), Track(id="a1", type="audio", z=0)])
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def test_a_jpeg_added_by_path_is_a_five_second_clip_that_can_be_lengthened(tmp_path):
    jpg = tmp_path / "photo.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240",
                    "-frames:v", "1", str(jpg)], check=True)
    st = _empty(tmp_path / "s")
    r = dispatch(st, "add_clip", {"track": "v1", "src": str(jpg), "in": 0, "out": 5, "start": 0})
    c = st.edl.get_clip(r["clip_id"])[1]
    assert c.effective_duration == pytest.approx(5.0), r["summary"]
    dispatch(st, "trim_clip", {"clip_id": c.id, "out": 10.0})
    assert st.edl.get_clip(c.id)[1].effective_duration == pytest.approx(10.0)


def _hlg(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:d=2:r=30",
                    "-c:v", "libx265", "-x265-params",
                    "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                    "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", str(path)], check=True)
    return path


def _sdr(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:d=2:r=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-colorspace", "bt709", str(path)], check=True)
    return path


def test_an_hlg_clip_added_by_path_is_tonemapped_and_the_export_is_tagged_bt709(tmp_path):
    hlg = _hlg(tmp_path / "hlg.mov")
    assert _probe(str(hlg), "color_transfer")["color_transfer"] == "arib-std-b67"
    sdr = _sdr(tmp_path / "hd709.mp4")
    st = _empty(tmp_path / "s")
    r = dispatch(st, "add_clip", {"track": "v1", "src": str(hlg), "in": 0, "out": 2, "start": 0})
    dispatch(st, "add_clip", {"track": "v1", "src": str(sdr), "in": 0, "out": 2, "start": 2})
    src = st.edl.get_clip(r["clip_id"])[1].src
    tags = _probe(str(src), "color_transfer,color_primaries,color_space")
    assert (tags["color_transfer"], tags["color_primaries"], tags["color_space"]) == ("bt709",) * 3
    from video_ai_editor.render import compositor
    out = compositor.render_export(st.edl, st.dir, fps=FPS, height=H, crf=23).path
    tags = _probe(str(out), "color_transfer,color_primaries,color_space")
    assert (tags.get("color_transfer"), tags.get("color_primaries")) == ("bt709", "bt709"), tags
