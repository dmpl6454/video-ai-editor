"""Final QA (0.8.0, round 1, render-audio): regressions for the export-truth,
engine and robustness sweeps' render/audio findings.

Each test here failed on tree 03e9ba4 before its fix:
  * the chunk cache deleted chunks the same render still needed (>200 clips);
  * disk-full / SIGKILL render failures blamed the user's clip;
  * a LUT path with an apostrophe, comma, semicolon or bracket broke the graph.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_ai_editor import platformutil as pu
from video_ai_editor.edl.schema import Clip
from video_ai_editor.main import _render_failure_message, _render_failure_error_code
from video_ai_editor.render import chunks as C


# --------------------------------------------------------------------------
# Chunk cache: a render's own chunks are never evicted before its concat.
# --------------------------------------------------------------------------

def test_render_with_more_than_200_distinct_chunks_keeps_every_chunk(tmp_path, monkeypatch):
    """engine finding: `get_or_build_chunks` ended with a count-based
    `evict_old_chunks(keep=200)`, run AFTER it built/touched this render's
    chunks and BEFORE the concat opened them — a 230-clip timeline lost 30
    of its own inputs and every preview and export failed with 'a clip's
    source file is missing'. The byte-budget LRU (cache_budget) owns
    eviction; nothing here may delete a path the caller is about to read."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x" * 64)

    def fake_render(c, *, dst, **_kw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"chunk" * 300)

    monkeypatch.setattr(C, "render_clip_to_chunk", fake_render)
    monkeypatch.setattr(C, "chunk_is_valid", lambda p: p.exists())
    monkeypatch.setenv("VAI_CHUNK_WORKERS", "4")
    n = 230
    clips = [Clip(src=str(src), in_=i / 30.0, out=i / 30.0 + 1 / 30.0, start=i / 30.0, id=f"c{i}")
             for i in range(n)]
    paths = C.get_or_build_chunks(
        clips, cache_dir=tmp_path / "chunks", canvas_w=320, canvas_h=180, fps=30,
        encoder_args=["-c:v", "libx264"], build_video_chain=lambda *a, **k: "",
        build_audio_chain=lambda *a, **k: "")
    assert len(set(paths)) == n
    missing = [p.name for p in paths if not p.exists()]
    assert missing == [], f"{len(missing)} of this render's own chunks were deleted"


# --------------------------------------------------------------------------
# Failure messages: a full disk or a killed encoder is not "corrupt media".
# --------------------------------------------------------------------------

_ENOSPC_TAIL = (
    "frame=  512 fps= 60 q=-1.0 Lsize=   98304kB time=00:00:17.03 bitrate=47000.0kbits/s\n"
    "[out#0/mp4 @ 0x600] Error writing trailer: No space left on device\n"
    "[out#0/mp4 @ 0x600] Error closing file: No space left on device\n"
    "Conversion failed!")


@pytest.mark.parametrize("kind", ["preview", "export"])
def test_disk_full_render_says_disk_full_not_corrupt_media(kind):
    msg = _render_failure_message(_ENOSPC_TAIL, f"ffmpeg render failed (rc=228):\n{_ENOSPC_TAIL}", kind=kind)
    assert "disk is full" in msg.lower()
    assert "your media is fine" in msg.lower()
    assert "corrupt" not in msg.lower() and "convert it to mp4" not in msg.lower()
    assert _render_failure_error_code(_ENOSPC_TAIL) == "disk_full"


def test_sigkilled_encoder_says_interrupted_not_corrupt_media():
    tail = ("frame=  30 fps=0.0 q=28.0 size=     256kB time=00:00:01.00 bitrate=2097.2kbits/s speed=1.9x")
    full = f"ffmpeg render failed (rc=-9):\n{tail}"
    msg = _render_failure_message(full[-400:], full, kind="export")
    assert "interrupted" in msg.lower()
    assert "nothing is wrong with your media" in msg.lower()
    assert "corrupt" not in msg.lower()
    assert _render_failure_error_code(full) == "render_failed"


def test_a_crashing_decoder_is_still_a_media_problem():
    """rc=-11 (SIGSEGV) is ffmpeg crashing on its input — not an interruption."""
    full = "ffmpeg render failed (rc=-11):\n[h264 @ 0x1] Invalid NAL unit size"
    msg = _render_failure_message(full, full, kind="export")
    assert "interrupted" not in msg.lower()


# --------------------------------------------------------------------------
# Filtergraph paths: both escaping levels (option AND graph).
# --------------------------------------------------------------------------

_ODD_NAMES = ["Tom's grade, v2.cube", "a[1];b=c:d.cube", "plain.cube"]


def test_filter_path_escapes_graph_and_option_specials():
    assert pu.ffmpeg_filter_path("/tmp/plain.cube") == "/tmp/plain.cube"
    # ':' keeps its historical double escape (level 1 then level 2).
    assert pu.ffmpeg_filter_path("/tmp/a:b.cube") == "/tmp/a\\\\:b.cube"
    got = pu.ffmpeg_filter_path("/x/Tom's grade, v2.cube")
    assert got == "/x/Tom\\\\\\'s grade\\, v2.cube"
    assert pu.ffmpeg_filter_path("/x/a[1];b.cube") == "/x/a\\[1\\]\\;b.cube"


@pytest.mark.parametrize("name", _ODD_NAMES)
def test_lut3d_and_movie_open_paths_with_graph_specials(tmp_path, name):
    """robustness finding: a LUT named "Tom's grade, v2.cube" was stored by
    apply_lut and then broke every preview and export ('corrupt frames')."""
    warm = Path(__file__).resolve().parents[1] / "src" / "video_ai_editor" / "presets" / "luts" / "warm.cube"
    if not warm.exists():
        warm = next((Path(__file__).resolve().parents[1]).rglob("presets/luts/warm.cube"))
    d = tmp_path / "lut dir"
    d.mkdir()
    lut = d / name
    lut.write_bytes(warm.read_bytes())
    img = d / (Path(name).stem + ".png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16:d=0.04",
                    "-frames:v", "1", str(img)], check=True, capture_output=True)
    esc_lut, esc_img = pu.ffmpeg_filter_path(lut), pu.ffmpeg_filter_path(img)
    graph = (f"color=c=gray:s=16x16:d=0.1,format=rgb24,lut3d={esc_lut}[a];"
             f"movie=filename={esc_img},scale=16:16,format=rgb24[b];"
             f"[a][b]overlay=0:0,format=yuv420p")
    proc = subprocess.run(["ffmpeg", "-v", "error", "-filter_complex", graph, "-frames:v", "1",
                           "-f", "null", "-"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Frame plans: no black frame / dropout from sub-frame rounding.
# --------------------------------------------------------------------------

import sys  # noqa: E402
from fractions import Fraction  # noqa: E402

import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Canvas  # noqa: E402
from video_ai_editor.render import compositor  # noqa: E402
from video_ai_editor.render.frame_map import planned_frames  # noqa: E402


@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> tuple[str, str]:
    d = tmp_path_factory.mktemp("fqbars")
    a = G.make_bar_source(d / "b30.mp4", G.SourceSpec(key="b30", sid=1, rate=Fraction(30), seconds=12.0))
    b = G.make_bar_source(d / "b25.mp4", G.SourceSpec(key="b25", sid=2, rate=Fraction(25), seconds=12.0))
    return str(a), str(b)


def _store(tmp: Path, fps=30) -> EDLStore:
    st = EDLStore(tmp)
    st.edl.canvas = Canvas(w=G.W, h=G.H, fps=fps)
    st.edl.canvas.loudness_lufs = None
    st.commit("init", {}, "init")
    return st


def _add(st, src, i, o, track="v1", start=None):
    return dispatch(st, "add_clip", {"track": track, "src": src, "in": i, "out": o,
                                     "start": st.edl.duration if start is None else start})["clip_id"]


def _retimed(tmp: Path, bars) -> EDLStore:
    """export-truth repro_trailing_black: five clips end to end, 1.5x on the
    second, the Montage curve on the last."""
    a, b = bars
    st = _store(tmp)
    ids = [_add(st, s, i, o) for s, i, o in ((a, 0.5, 2.0), (b, 1.02, 2.7), (a, 3.3, 4.9),
                                             (b, 2.0, 3.1), (a, 5.0, 6.6))]
    dispatch(st, "set_speed", {"clip_id": ids[1], "factor": 1.5})
    dispatch(st, "set_speed", {"clip_id": ids[4], "preset": "montage"})
    return st


def test_retimed_timeline_duration_is_its_frame_plan(tmp_path, bars):
    """A retimed clip ending a third of a frame past its rippled neighbour's
    start carried that sliver into EDL.duration (189.81 frames → 190) while
    the renderer packs whole frames (189): export and server preview ended on
    a black, silent frame — a black flash at every loop of a Reel."""
    e = _retimed(tmp_path / "s", bars).edl
    fps = e.canvas.fps
    plan = compositor._v1_frame_plan(compositor._video_clips(e), e.duration, fps)
    assert plan[-1][0] == "clip", plan[-3:]
    assert planned_frames(e) == tb.frame_of(e.duration, fps)


def _codes(path: Path) -> list[int]:
    return [int(c) for c in G._read_code(G.decode_gray(path), G.TOP_ROWS)]


def _pcm(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", "48000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _silent_windows(a: np.ndarray) -> list[float]:
    rms = np.sqrt(np.convolve(a.astype(np.float64) ** 2, np.ones(480) / 480, mode="valid"))
    q = np.flatnonzero(rms[:len(rms) - 4800] < 0.01)       # the last 100 ms may fade
    return sorted({round(float(x) / 48000, 2) for x in q})


def test_back_to_back_clips_plan_no_gap_at_any_export_rate(tmp_path, bars):
    """export-truth: a 30 fps project's clips at 0-2.5, 2.5-3.4333, 3.4333-5.4333
    exported at 25 planned [62, 23, gap 1, 50]: each clip's START and LENGTH
    were rounded separately at the export rate. The plan at any rate is now
    the project-grid plan resampled at its boundaries."""
    a, _b = bars
    st = _store(tmp_path / "s")
    for i, o in ((0.0, 2.5), (3.0, 3.9333), (5.0, 7.0)):
        _add(st, a, i, o)
    e = st.edl
    clips = compositor._video_clips(e)
    for R in tb.STANDARD_RATES:
        fps = G.fps_value(R)
        with compositor.v1_rate_scope(e, fps):
            plan = compositor._v1_frame_plan(clips, e.duration, fps)
        assert [k for k, _i, _n in plan] == ["clip"] * 3, (G.rate_name(R), plan)
        assert sum(n for _k, _i, n in plan) == planned_frames(e, fps) == tb.frame_of(e.duration, fps)


@pytest.mark.parametrize("fps", [25, 24, 60])
def test_back_to_back_export_at_another_rate_has_no_black_frame_or_dropout(tmp_path, bars, fps):
    a, _b = bars
    st = _store(tmp_path / "s")
    for i, o in ((0.0, 2.5), (3.0, 3.9333), (5.0, 7.0)):
        _add(st, a, i, o)
    e = st.edl
    sess = tmp_path / "sess"
    sess.mkdir()
    out = compositor.render_export(e, sess, fps=fps, height=G.H, crf=18).path
    codes = _codes(out)
    assert len(codes) == planned_frames(e, fps)
    assert [k for k, c in enumerate(codes) if c == 0] == []
    assert _silent_windows(_pcm(out)) == []


def test_many_clip_export_at_another_rate_goes_through_chunks_without_a_gap(tmp_path, bars, monkeypatch):
    """The chunked export (more clips than one pass holds) keys each chunk on
    its frame span at the export rate, so a resampled span is never served a
    chunk cut to the old length."""
    a, _b = bars
    st = _store(tmp_path / "s")
    for i, o in ((0.0, 2.5), (3.0, 3.9333), (5.0, 7.0)):
        _add(st, a, i, o)
    e = st.edl
    monkeypatch.setattr(compositor, "_EXPORT_SINGLE_PASS_MAX_CLIPS", 1)
    sess = tmp_path / "sess"
    sess.mkdir()
    out = compositor.render_export(e, sess, fps=25, height=G.H, crf=18).path
    assert (sess / "cache" / "chunks").exists(), "the chunk path ran"
    codes = _codes(out)
    assert len(codes) == planned_frames(e, 25)
    assert [k for k, c in enumerate(codes) if c == 0] == []


# --------------------------------------------------------------------------
# Text and stickers gate on the EXPORT rate's frames.
# --------------------------------------------------------------------------

def _text_frames(tmp: Path, bars, *, fps, export_fps, with_text: bool) -> np.ndarray:
    a, _b = bars
    st = _store(tmp, fps=fps)
    _add(st, a, 0.0, 2.0)
    _add(st, a, 5.0, 8.0)
    if with_text:
        t = dispatch(st, "add_text", {"text": "CUT", "start": 2.0, "end": 3.0})
        c = st.edl.get_clip(t["id"])[1]
        c.transform.y = 20
        c.style.size = 20
        st.commit("t", {}, "t")
    sess = tmp / "sess"
    sess.mkdir(parents=True)
    return G.decode_gray(compositor.render_export(st.edl, sess, fps=export_fps, height=G.H, crf=8).path)


@pytest.mark.parametrize("fps,export_fps", [(30, 60), (25, 50)])
def test_text_on_a_cut_is_drawn_from_its_first_export_frame(tmp_path, bars, fps, export_fps):
    """export-truth: at 60 fps from a 30 fps project the text was drawn one
    frame early (119..178, not 120..179) — over the last frame of the
    PREVIOUS shot — because its gate used half a PROJECT frame."""
    plain = _text_frames(tmp_path / "a", bars, fps=fps, export_fps=export_fps, with_text=False)
    text = _text_frames(tmp_path / "b", bars, fps=fps, export_fps=export_fps, with_text=True)
    diff = np.abs(plain[:, 0:40].astype(int) - text[:, 0:40].astype(int)).mean(axis=(1, 2))
    drawn = [k for k, x in enumerate(diff) if x > 0.5]
    assert drawn[0] == tb.frame_of(2.0, export_fps)
    assert drawn[-1] == tb.frame_of(3.0, export_fps) - 1


def test_looped_overlay_inputs_run_at_the_export_rate(tmp_path, bars):
    """Keyed/animated text and stickers loop their PNG at the render rate, so
    a 60 fps export of a 30 fps project animates them every frame."""
    from video_ai_editor.render.text_overlay import build_overlay_chain
    a, _b = bars
    st = _store(tmp_path / "s")
    _add(st, a, 0.0, 3.0)
    dispatch(st, "add_text", {"text": "HI", "start": 0.5, "end": 2.5, "anim_in": "fade", "anim_out": "fade"})
    chain, inputs, _lbl = build_overlay_chain(
        st.edl, tmp_path / "cache", source_label="[v]", out_label="[o]", first_input_index=1,
        out_w=G.W, out_h=G.H, fps=60)
    rates = [inputs[k + 1] for k, x in enumerate(inputs) if x == "-framerate"]
    assert rates and set(rates) == {"60"}, inputs
    assert "gte(t\\,0.491667)" in chain, chain


# --------------------------------------------------------------------------
# Loudness: the target holds on music the true-peak limiter bites into.
# --------------------------------------------------------------------------

def _loudness(p: Path) -> tuple[float, float]:
    import re as _re
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(p), "-map", "0:a:0",
                          "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    tail = err[err.rfind("Summary:"):]
    i = float(_re.search(r"I:\s+(-?[\d.]+) LUFS", tail).group(1))
    tp = float(_re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail).group(1))
    return i, tp


@pytest.fixture(scope="module")
def dynamic_bed(tmp_path_factory) -> dict[str, Path]:
    """Kick + snare + a quiet pad: about -17.5 LUFS with -1.8 dBTP peaks, a
    peak-to-loudness ratio near 16 dB like the bench's 100 bpm bed. Lifted to
    -14 its peaks sit 3-4 dB over the limiter."""
    d = tmp_path_factory.mktemp("dyn")
    bed = d / "bed.wav"
    expr = ("0.85*sin(2*PI*70*t)*exp(-20*mod(t\\,0.6))"
            "+0.8*(random(0)-0.5)*exp(-60*mod(t+0.3\\,0.6))+0.02*sin(2*PI*330*t)")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d=12:c=stereo",
                    "-c:a", "pcm_s24le", str(bed)], check=True, capture_output=True)
    pic = d / "pic.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:d=12:r=30",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "12", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(pic)],
                   check=True, capture_output=True)
    return {"bed": bed, "pic": pic}


@pytest.mark.parametrize("preset,volume_db", [("shorts", 0.0), ("reels", -12.0)])
def test_dynamic_music_lands_its_loudness_target(tmp_path, dynamic_bed, preset, volume_db):
    """export-truth: shorts (-14) measured -15.7 LUFS and reels (-16) -17.1
    with the bench bed: the static gain `target - I` then a true-peak limiter
    that removed 2-4 dB of peaks, and nothing made that loudness back."""
    from video_ai_editor.render import audio_mix
    st = EDLStore(tmp_path / "s")
    st.edl.canvas = Canvas(w=320, h=180, fps=30)
    st.commit("init", {}, "init")
    dispatch(st, "add_clip", {"track": "v1", "src": str(dynamic_bed["pic"]), "in": 0, "out": 12, "start": 0})
    dispatch(st, "add_music", {"src": str(dynamic_bed["bed"]), "start": 0, "in": 0, "out": 12,
                               "duck": False, "volume_db": volume_db})
    dispatch(st, "apply_export_preset", {"name": preset})
    target = st.edl.canvas.loudness_lufs
    wav = compositor.render_export(st.edl, st.dir, container="wav").path
    i, tp = _loudness(wav)
    assert i == pytest.approx(target, abs=0.5), (i, tp)
    assert tp <= audio_mix.EXPORT_TRUE_PEAK_DBTP, (i, tp)
    mp4 = compositor.render_export(st.edl, st.dir, height=144).path
    i, tp = _loudness(mp4)
    assert i == pytest.approx(target, abs=1.0), (i, tp)
    assert tp <= audio_mix.EXPORT_TRUE_PEAK_DBTP, (i, tp)
