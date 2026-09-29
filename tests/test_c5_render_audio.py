"""Wave C, lane C5 (render + audio): every claim measured on a real render.

  * QA-120 — no latency anywhere: with a music bed, across an aligned cut and
    across a 30 fps + 25 fps join, every click lands on its flash in the
    EXPORT (default −16 LUFS target), the very first one included;
  * QA-121 — every export preset lands its loudness target (±1 LU) under a
    −1 dBTP true-peak ceiling, measured with ebur128 on the delivered AAC, on
    a mixed timeline whose content has inter-sample peaks;
  * QA-122 — a left-only camera recording exports one-sided unless its
    channel mode says otherwise; the mode fills both sides on every lane, and
    the waveform reports each side;
  * QA-131 — adding a marker does not re-render the preview;
  * QA-132 — a cached waveform is served without spawning ffprobe;
  * QA-086 (audio speed) — speed on a music lane retimes it like v1;
  * QA-100 (audio-only) — Export → M4A / WAV: the sound alone, mastered;
  * QA-039 (keep pitch) — the render binary has no rubberband, so keep-pitch
    timing is atempo's (documented limitation), measured here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from timing_fixtures import av_offsets_ms, click_times, make_clap  # noqa: E402

from video_ai_editor import platformutil as _pu  # noqa: E402
from video_ai_editor.agent.dispatch import _EXPORT_PRESETS, dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl.schema import Canvas, empty_edl  # noqa: E402
from video_ai_editor.render import audio_mix, compositor, render_export, waveform  # noqa: E402

SR = 48000


def _ff(args: list[str]) -> None:
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", *args], check=True, capture_output=True)


def _store(sd: Path, *, fps=30, lufs: float | None = -16.0, w=320, h=180) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=w, h=h, fps=fps)
    e.canvas.loudness_lufs = lufs
    (sd / "edl.json").write_text(e.model_dump_json())
    return EDLStore(sd)


def _loudness(p: Path) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) of `p`'s audio — ebur128 peak=true."""
    err = subprocess.run([_pu.FFMPEG, "-hide_banner", "-nostats", "-i", str(p), "-map", "0:a:0",
                          "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    tail = err[err.rfind("Summary:"):]
    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", tail).group(1))
    tp = float(re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail).group(1))
    return i, tp


def _ffmpeg_version() -> str:
    """The render binary's version token ("8.1.1", "6.1.1-3ubuntu5"), for skip reasons."""
    out = subprocess.run([_pu.FFMPEG, "-version"], capture_output=True, text=True).stdout
    m = re.search(r"ffmpeg version (\S+)", out)
    return m.group(1) if m else "unknown"


def _skip_unless_encode_overshoots(tp_enc: float) -> None:
    """Whether the AAC encode of a take re-adds inter-sample peak over the
    ceiling is a property of the encoder build, not of the product: on some
    encoders (the CI runners' 6.1 / 9.x builds) the delivered take peaks at or
    under the ceiling and there is nothing for the fix-up to dip."""
    ceiling = audio_mix.EXPORT_TRUE_PEAK_DBTP
    if tp_enc <= ceiling:
        pytest.skip(f"this ffmpeg {_ffmpeg_version()} AAC encode of the take peaks at {tp_enc} dBTP, "
                    f"not over the {ceiling} ceiling: nothing to dip")


def _probe(p: Path) -> dict:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                          str(p)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _channels(p: Path) -> np.ndarray:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(p), "-map", "0:a:0", "-ac", "2",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).astype(np.float64)


def _rms_db(x: np.ndarray) -> float:
    ms = float(np.mean(x * x)) if len(x) else 0.0
    return 10 * np.log10(ms) if ms > 1e-24 else -240.0


# ---------------------------------------------------------------- QA-120

def test_export_has_no_latency_with_a_bed_across_a_cut_and_a_mixed_fps_join(tmp_path):
    """The export's default −16 LUFS target, a −40 dB bed, a cut and a 25 fps
    clip after a 30 fps one, from AAC sources (what every import is): every
    flash has its click within 1 ms — each clip's FIRST one too. `-ss 0`
    garbled the first 21 ms of an AAC input (`audio_mix.input_seek`), so the
    click on a clip's first frame read 17 ms late or vanished; the master is
    a latency-compensated true-peak limiter (no alimiter delay)."""
    clap30 = make_clap(tmp_path / "clap30.mp4", seconds=8, fps=30)
    clap25 = make_clap(tmp_path / "clap25.mp4", seconds=6, fps=25)
    bed = tmp_path / "bed.wav"
    _ff(["-f", "lavfi", "-i", "anoisesrc=a=0.01:c=pink:d=16:r=48000", "-ac", "2", str(bed)])
    s = _store(tmp_path / "s", fps=30)
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap30), "in": 0, "out": 8, "start": 0})
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap25), "in": 0, "out": 6, "start": 8})
    dispatch(s, "cut_range", {"track": "v1", "start": 3.0, "end": 4.0})     # an aligned cut
    dispatch(s, "add_music", {"src": str(bed), "start": 0, "in": 0, "out": 13, "duck": False,
                              "volume_db": 0})
    assert s.edl.canvas.loudness_lufs == -16.0
    offs = av_offsets_ms(render_export(s.edl, s.dir).path)
    # 8 s clip minus the cut = 7 flashes, then 6 from the 25 fps clip
    assert len(offs) == 13, [round(o, 2) for o in offs]
    assert max(abs(o) for o in offs) <= 1.0, [round(o, 2) for o in offs]


# ---------------------------------------------------------------- QA-121

@pytest.fixture(scope="module")
def mixed_media(tmp_path_factory) -> dict[str, Path]:
    """A heavy mixed timeline's sources. The loud clip is a 12 kHz tone at a
    45° phase: its SAMPLES peak at 0.64 while the waveform between them
    reaches 0.9 — exactly the inter-sample peak a sample-peak limiter never
    sees (the case that exported at −0.3 / −0.6 dBTP)."""
    d = tmp_path_factory.mktemp("mix")
    quiet = d / "quiet.mov"
    _ff(["-f", "lavfi", "-i", "color=c=gray:s=320x180:d=4:r=30",
         "-f", "lavfi", "-i", "anoisesrc=a=0.05:c=pink:d=4:r=48000,volume='0.5+0.5*sin(2*PI*3*t)':eval=frame",
         "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "pcm_s16le", "-ac", "2", str(quiet)])
    loud = d / "loud.mov"
    _ff(["-f", "lavfi", "-i", "color=c=white:s=320x180:d=4:r=30",
         "-f", "lavfi", "-i", "aevalsrc='0.9*sin(2*PI*12000*t+PI/4)*lt(mod(t\\,0.5)\\,0.25)'"
                              ":s=48000:d=4:c=stereo",
         "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "pcm_s24le", str(loud)])
    bed = d / "bed.wav"
    _ff(["-f", "lavfi", "-i", "anoisesrc=a=0.25:c=pink:d=8:r=48000", "-ac", "2", str(bed)])
    vo = d / "vo.wav"
    _ff(["-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*300*t)*sin(2*PI*2*t)':s=48000:d=6:c=stereo", str(vo)])
    return {"quiet": quiet, "loud": loud, "bed": bed, "vo": vo}


def _mixed_store(sd: Path, m: dict[str, Path]) -> EDLStore:
    s = _store(sd)
    dispatch(s, "add_clip", {"track": "v1", "src": str(m["quiet"]), "in": 0, "out": 4, "start": 0})
    dispatch(s, "add_clip", {"track": "v1", "src": str(m["loud"]), "in": 0, "out": 4, "start": 4})
    dispatch(s, "add_music", {"src": str(m["bed"]), "start": 0, "in": 0, "out": 8, "duck": True,
                              "volume_db": 0})
    dispatch(s, "add_clip", {"track": "vo", "src": str(m["vo"]), "in": 0, "out": 6, "start": 1})
    return s


@pytest.mark.parametrize("preset", sorted(_EXPORT_PRESETS))
def test_every_preset_lands_its_target_under_minus_one_dbtp(tmp_path, mixed_media, preset):
    s = _mixed_store(tmp_path / preset, mixed_media)
    dispatch(s, "apply_export_preset", {"name": preset})
    target = s.edl.canvas.loudness_lufs
    assert target == _EXPORT_PRESETS[preset]["lufs"]
    i, tp = _loudness(render_export(s.edl, s.dir, height=144).path)
    assert i == pytest.approx(target, abs=1.0), (preset, i, tp)
    assert tp <= audio_mix.EXPORT_TRUE_PEAK_DBTP, (preset, i, tp)


def test_broadcast_target_and_no_target_both_hold_the_ceiling(tmp_path, mixed_media):
    s = _mixed_store(tmp_path / "ebu", mixed_media)
    dispatch(s, "set_loudness_target", {"lufs": -23.0})
    i, tp = _loudness(render_export(s.edl, s.dir).path)
    assert i == pytest.approx(-23.0, abs=1.0) and tp <= -1.0, (i, tp)
    # "Off · keep the mix as it is": no normalisation, but a mixed timeline
    # still never ships an inter-sample over — here a mix at about −5.6 LUFS,
    # far hotter than any real master. (Past ~−5 LUFS the AAC encoder itself
    # can re-add up to 2 dB; the WAV export holds the ceiling at any level —
    # CLAUDE.md, audio rules.)
    s2 = _mixed_store(tmp_path / "off", mixed_media)
    dispatch(s2, "set_loudness_target", {"lufs": None})
    assert s2.edl.canvas.loudness_lufs is None
    dispatch(s2, "set_volume", {"target": s2.edl.get_track("music").clips[0].id, "db": -9.0})
    dispatch(s2, "set_volume", {"target": s2.edl.get_track("v1").clips[1].id, "db": -3.0})
    i, tp = _loudness(render_export(s2.edl, s2.dir).path)
    assert i > -8.0 and tp <= -1.0, (i, tp)
    i, tp = _loudness(render_export(s2.edl, s2.dir, container="wav").path)
    assert tp <= -1.0, (i, tp)


def test_the_mastering_gain_is_measured_from_the_mix(tmp_path, mixed_media):
    """Pass 1 reads the raw mix's integrated loudness — the number pass 2's
    static gain is computed from — and it IS the mix: the same timeline
    rendered with no target (a quiet mix, so the limiter is idle) measures
    the same."""
    s = _store(tmp_path / "m")
    dispatch(s, "add_clip", {"track": "v1", "src": str(mixed_media["quiet"]), "in": 0, "out": 4, "start": 0})
    dispatch(s, "add_clip", {"track": "vo", "src": str(mixed_media["vo"]), "in": 0, "out": 3, "start": 0.5})
    measured = compositor.measure_mix_loudness(s.edl, fps=30, cache_dir=s.dir / "cache")
    assert measured is not None and -40.0 < measured < -10.0, measured
    assert audio_mix.export_gain_for(-16.0, measured) == pytest.approx(-16.0 - measured)
    dispatch(s, "set_loudness_target", {"lufs": None})
    raw = render_export(s.edl, s.dir, container="wav").path
    assert _loudness(raw)[0] == pytest.approx(measured, abs=0.3)
    # A silent programme: 0 dB, never a normalisation of silence.
    assert audio_mix.export_gain_for(-16.0, -70.0) == 0.0


@pytest.fixture(scope="module")
def dense_bed(tmp_path_factory) -> dict[str, Path]:
    """Dense, transient-heavy "music": noise hats on every 8th over a low
    bass. At −14 LUFS its peaks sit on the limiter, and its HF bursts are
    what the AAC encoder re-adds inter-sample peak to — up to +1.6 dB over a
    −1.5 dBTP PCM master (the bench's 100 bpm bed: −0.2 dBTP in the mp4)."""
    d = tmp_path_factory.mktemp("dense")
    beds = {
        "hats": "0.8*(random(0)-0.5)*2*exp(-30*mod(t\\,0.25))+0.2*sin(2*PI*80*t)",
        "clicks": "0.25*(2*mod(220*t\\,1)-1)+0.25*(2*mod(331*t\\,1)-1)"
                  "+0.9*(random(0)-0.5)*2*exp(-80*mod(t\\,0.3))",
    }
    out: dict[str, Path] = {}
    for name, expr in beds.items():
        out[name] = d / f"{name}.wav"
        _ff(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d=12:c=stereo",
             "-c:a", "pcm_s24le", str(out[name])])
    # The picture's room noise sits ~40 dB under the bed, yet it decides
    # WHICH AAC frame overshoots: unseeded (anoisesrc's default), this test
    # failed ~1 run in 8 on the fix-up's loudness. Seeds 8 and 3 are takes
    # that put one +0.6 dBTP spot in the hats encode and a -0.8 one in the
    # clicks encode (C2 sweep, 30 seeds x 2 beds x 2 targets) — pinned, so
    # the hard case runs every time instead of by chance.
    for key, seed in (("pic", 8), ("pic3", 3)):
        out[key] = d / f"{key}.mp4"
        _ff(["-f", "lavfi", "-i", "color=c=gray:s=320x180:d=12:r=30",
             "-f", "lavfi", "-i", f"anoisesrc=a=0.02:c=pink:d=12:r=48000:seed={seed}", "-shortest",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-ac", "2", str(out[key])])
    return out


@pytest.mark.parametrize("bed", ["hats", "clicks"])
def test_dense_undocked_music_holds_the_ceiling_after_the_aac_encode(tmp_path, dense_bed, bed):
    """QA-121 remainder: the 4x-oversampled limiter held the PCM master at
    −1.5 dBTP, but 192 kbps AAC re-adds 0.8-1.6 dB of inter-sample peak on
    dense limited music, and the export allowed for 0.6. The delivered mp4
    AND m4a are measured after the encode and brought under the ceiling."""
    s = _store(tmp_path / "s", w=640, h=360)
    dispatch(s, "add_clip", {"track": "v1", "src": str(dense_bed["pic"]), "in": 0, "out": 12, "start": 0})
    dispatch(s, "add_music", {"src": str(dense_bed[bed]), "start": 0, "in": 0, "out": 12,
                              "duck": False, "volume_db": 0})
    dispatch(s, "apply_export_preset", {"name": "youtube_16x9"})
    target = s.edl.canvas.loudness_lufs
    mp4 = render_export(s.edl, s.dir, height=144).path
    m4a = render_export(s.edl, s.dir, container="m4a").path
    for p in (mp4, m4a):
        i, tp = _loudness(p)
        assert tp <= audio_mix.EXPORT_TRUE_PEAK_DBTP, (p.name, i, tp)
        assert i == pytest.approx(target, abs=1.0), (p.name, i, tp)
    # The picture is untouched by the audio fix-up (stream copy, same frames).
    pr = _probe(mp4)
    v = next(st for st in pr["streams"] if st["codec_type"] == "video")
    assert (v["width"], v["height"]) == (256, 144)
    assert abs(float(pr["format"]["duration"]) - 12.0) < 0.1


@pytest.mark.parametrize("bed,pic", [("hats", "pic"), ("clicks", "pic"), ("clicks", "pic3")])
def test_one_aac_overshoot_spot_is_dipped_not_the_whole_programme(tmp_path, dense_bed, bed, pic,
                                                                 monkeypatch):
    """C2: the encode's overshoot is a spot (one 5 ms hat at +0.6 dBTP over a
    file otherwise at -1.2), and the fix-up used to answer it by lowering the
    WHOLE ceiling by the full overshoot: -14.6 -> -15.3 LUFS on a -14 target
    (hats), or, re-rolling the encode each pass, never getting under at all
    (clicks: -0.8 -> -0.9 -> -0.9 -> 0.0 dBTP). Now only the spots are dipped:
    the delivered mp4 is under the ceiling and within 0.2 LU of what the
    encode delivered."""
    s = _store(tmp_path / "s", w=640, h=360)
    dispatch(s, "add_clip", {"track": "v1", "src": str(dense_bed[pic]), "in": 0, "out": 12, "start": 0})
    dispatch(s, "add_music", {"src": str(dense_bed[bed]), "start": 0, "in": 0, "out": 12,
                              "duck": False, "volume_db": 0})
    dispatch(s, "apply_export_preset", {"name": "youtube_16x9"})
    seen: dict[str, tuple[float, float]] = {}
    real = compositor._hold_delivery_true_peak

    def spy(dst, **kw):
        seen["encoded"] = _loudness(dst)
        real(dst, **kw)
    monkeypatch.setattr(compositor, "_hold_delivery_true_peak", spy)
    mp4 = render_export(s.edl, s.dir, height=144).path
    i, tp = _loudness(mp4)
    i_enc, tp_enc = seen["encoded"]
    # What holds on every encoder runs first; only the "a dip, not a lowered
    # programme" comparison needs an encode that really overshot.
    assert tp <= audio_mix.EXPORT_TRUE_PEAK_DBTP, (i_enc, tp_enc, i, tp)
    assert i == pytest.approx(s.edl.canvas.loudness_lufs, abs=1.0), (i, tp)
    assert not list((s.dir / "exports").glob("*.tpfix*")), "no fix-up candidate left behind"
    _skip_unless_encode_overshoots(tp_enc)
    assert i == pytest.approx(i_enc, abs=0.2), (i_enc, tp_enc, i, tp)


def test_overshoot_everywhere_falls_back_to_lowering_the_ceiling(tmp_path, dense_bed, monkeypatch):
    """More hot spots than `MAX_DIPS` is not a spot problem: the fix-up
    lowers the whole ceiling instead (the pre-C2 behaviour), which still
    brings the file under — at a loudness cost the dips avoid."""
    from video_ai_editor.render import delivery_peak as dp
    monkeypatch.setattr(dp, "MAX_DIPS", 0)
    s = _store(tmp_path / "s", w=640, h=360)
    dispatch(s, "add_clip", {"track": "v1", "src": str(dense_bed["pic"]), "in": 0, "out": 12, "start": 0})
    dispatch(s, "add_music", {"src": str(dense_bed["hats"]), "start": 0, "in": 0, "out": 12,
                              "duck": False, "volume_db": 0})
    dispatch(s, "apply_export_preset", {"name": "youtube_16x9"})
    seen: dict[str, tuple[float, float]] = {}
    real = compositor._hold_delivery_true_peak

    def spy(dst, **kw):
        seen["encoded"] = _loudness(dst)
        real(dst, **kw)
    monkeypatch.setattr(compositor, "_hold_delivery_true_peak", spy)
    m4a = render_export(s.edl, s.dir, container="m4a").path
    i, tp = _loudness(m4a)
    assert tp <= audio_mix.EXPORT_TRUE_PEAK_DBTP, (i, tp)
    # Against what the encode delivered, not an absolute level: the master
    # now lands its target through the limiter (final QA, 0.8.0), so this
    # take starts at -14.0 where it used to start 0.6 LU short.
    i_enc, tp_enc = seen["encoded"]
    _skip_unless_encode_overshoots(tp_enc)
    assert i < i_enc - 0.2, ("the whole ceiling came down", i_enc, i, tp)


def test_the_dip_envelope_is_local_smooth_and_as_deep_as_asked(tmp_path):
    """The fix-up's gain envelope, rendered by ffmpeg on a steady tone: -d dB
    at a dip's centre, untouched a half-width away, and two overshooting
    scan blocks closer than a half-width are one spot."""
    from video_ai_editor.render import delivery_peak as dp
    tone = tmp_path / "tone.wav"
    _ff(["-f", "lavfi", "-i", "sine=f=1000:r=48000:d=2", "-ac", "2", "-c:a", "pcm_f32le", str(tone)])
    dips = [(0.5, 3.0), (1.5, 1.2)]
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(tone), "-af", dp.dip_filter(dips),
                          "-f", "f32le", "-ac", "2", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.float32).reshape(-1, 2)[:, 0].astype(np.float64)
    flat = _rms_db(np.frombuffer(subprocess.run(
        [_pu.FFMPEG, "-v", "error", "-i", str(tone), "-f", "f32le", "-ac", "2", "-"],
        capture_output=True, check=True).stdout, np.float32).reshape(-1, 2)[:, 0].astype(np.float64))

    def level(t: float) -> float:                # dB against the undipped tone
        return _rms_db(x[int((t - 0.004) * SR):int((t + 0.004) * SR)]) - flat
    assert level(0.5) == pytest.approx(-3.0, abs=0.15)
    assert level(1.5) == pytest.approx(-1.2, abs=0.15)
    for t in (0.2, 0.5 - dp.DIP_HALF_S - 0.01, 1.0, 1.5 + dp.DIP_HALF_S + 0.01, 1.8):
        assert level(t) == pytest.approx(0.0, abs=0.05), t
    # Scan -> spots -> dips.
    peaks = np.full(400, -3.0)
    peaks[100], peaks[104], peaks[300] = -0.5, 0.4, -0.9      # 5 ms blocks
    spots = dp.hot_spots(peaks, -1.15)
    assert [round(t, 4) for t, _ in spots] == [0.5225, 1.5025]
    assert [round(o, 2) for _, o in spots] == [1.55, 0.25]
    deeper = dp.merge_dips(spots, [(0.53, 0.3), (1.0, 0.2)])
    assert [(round(t, 4), round(o, 2)) for t, o in deeper] == [(0.5225, 1.85), (1.0, 0.2), (1.5025, 0.25)]
    assert spots == dp.hot_spots(peaks, -1.15), "merge_dips returns a new list"
    assert dp.dip_filter([]) == ""


# ---------------------------------------------------------------- QA-122

@pytest.fixture(scope="module")
def left_only(tmp_path_factory) -> Path:
    """A camera file whose sound is on the LEFT input only."""
    p = tmp_path_factory.mktemp("lo") / "leftonly.mov"
    _ff(["-f", "lavfi", "-i", "color=c=gray:s=320x180:d=3:r=30",
         "-f", "lavfi", "-i", "aevalsrc='0.3*sin(2*PI*440*t)|0':s=48000:d=3",
         "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "pcm_s16le", str(p)])
    return p


@pytest.mark.parametrize("lane", ["v1", "music", "v2"])
def test_channel_mode_fills_a_one_sided_recording(tmp_path, left_only, lane):
    s = _store(tmp_path / lane, lufs=None)
    if lane == "v1":
        dispatch(s, "add_clip", {"track": "v1", "src": str(left_only), "in": 0, "out": 3, "start": 0})
        cid = s.edl.get_track("v1").clips[0].id
    else:
        bg = tmp_path / "bg.mov"
        _ff(["-f", "lavfi", "-i", "color=c=black:s=320x180:d=3:r=30", "-f", "lavfi", "-i",
             "anullsrc=r=48000:cl=stereo", "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264",
             "-preset", "ultrafast", "-c:a", "pcm_s16le", str(bg)])
        dispatch(s, "add_clip", {"track": "v1", "src": str(bg), "in": 0, "out": 3, "start": 0})
        dispatch(s, "add_clip", {"track": lane, "src": str(left_only), "in": 0, "out": 3, "start": 0})
        cid = s.edl.get_track(lane).clips[0].id
        c = s.edl.get_clip(cid)[1]
        dispatch(s, "set_property", {"clip_id": cid, "path": "audio.fade_in", "value": 0})
        dispatch(s, "set_property", {"clip_id": cid, "path": "audio.fade_out", "value": 0})
        assert c.audio.fade_out == 0
    a = _channels(render_export(s.edl, s.dir).path)[int(0.5 * SR):int(2.5 * SR)]
    left, right = _rms_db(a[:, 0]), _rms_db(a[:, 1])
    assert left > -20.0 and right < -90.0, ("as recorded: one-sided", left, right)

    for mode, want in (("left", "both"), ("mono", "both-6dB"), ("right", "silent")):
        dispatch(s, "set_property", {"clip_id": cid, "path": "audio.channels", "value": mode})
        a = _channels(render_export(s.edl, s.dir).path)[int(0.5 * SR):int(2.5 * SR)]
        l2, r2 = _rms_db(a[:, 0]), _rms_db(a[:, 1])
        if want == "both":
            assert l2 == pytest.approx(left, abs=0.3) and r2 == pytest.approx(left, abs=0.3), (mode, l2, r2)
        elif want == "both-6dB":
            assert l2 == pytest.approx(left - 6.02, abs=0.3) and r2 == pytest.approx(l2, abs=0.1), (mode, l2, r2)
        else:
            assert l2 < -90.0 and r2 < -90.0, (mode, l2, r2)


def test_channel_mode_is_validated_and_undoable(tmp_path, left_only):
    s = _store(tmp_path / "s", lufs=None)
    dispatch(s, "add_clip", {"track": "v1", "src": str(left_only), "in": 0, "out": 3, "start": 0})
    cid = s.edl.get_track("v1").clips[0].id
    with pytest.raises(Exception):
        dispatch(s, "set_property", {"clip_id": cid, "path": "audio.channels", "value": "surround"})
    assert s.edl.get_clip(cid)[1].audio.channels == "stereo"
    dispatch(s, "set_property", {"clip_id": cid, "path": "audio.channels", "value": "left"})
    s.undo()
    assert s.edl.get_clip(cid)[1].audio.channels == "stereo"


def test_waveform_reports_each_side(tmp_path, left_only):
    w = waveform.waveform_peaks(left_only, tmp_path / "wf")
    assert max(w["peaks_l"]) == pytest.approx(0.3, abs=0.01)
    assert max(w["peaks_r"]) == 0.0
    assert max(w["peaks"]) == pytest.approx(0.3, abs=0.01)
    assert len(w["peaks_l"]) == len(w["peaks_r"]) == len(w["peaks"])


# ---------------------------------------------------------------- QA-132

def test_a_cached_waveform_spawns_no_process(tmp_path, left_only, monkeypatch):
    cache = tmp_path / "wf"
    first = waveform.waveform_peaks(left_only, cache)
    spawned: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(args, *a, **k):
        spawned.append(list(args))
        return real_run(args, *a, **k)

    class Popen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            spawned.append(list(args))
            super().__init__(args, *a, **k)

    monkeypatch.setattr(waveform.subprocess, "run", run)
    monkeypatch.setattr(waveform.subprocess, "Popen", Popen)
    again = waveform.waveform_peaks(left_only, cache)
    assert again == first
    assert spawned == [], spawned
    # …and a changed file is re-read, not served stale.
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=2",
                    "-ac", "2", str(tmp_path / "x.wav")], check=True)
    waveform.waveform_peaks(tmp_path / "x.wav", cache)
    assert spawned, "a new source must be probed and decoded"


# ---------------------------------------------------------------- QA-131

@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    RATE.windows.clear()
    _main._STORES.clear()
    return TestClient(_main.app)


@pytest.fixture(scope="module")
def av_src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("av") / "clip.mp4"
    _ff(["-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3", "-f", "lavfi", "-i", "sine=f=440:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)])
    return p


def _session(c: TestClient, src: Path, name="Mix") -> str:
    sid = c.post("/api/sessions", json={"name": name}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "add_clip", "args": {"track": "v1", "src": str(src), "in": 0.0, "out": 3.0, "start": 0.0}})
    assert r.status_code == 200, r.text
    return sid


def test_a_marker_does_not_re_render_the_preview(client, av_src, tmp_path):
    c = client
    sid = _session(c, av_src)
    first = c.post(f"/api/sessions/{sid}/preview").json()
    assert c.post(f"/api/sessions/{sid}/preview").json()["cached"] is True
    head_before = c.get(f"/api/sessions/{sid}/head").json()["edl_hash"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_marker",
                                                       "args": {"time": 1.5, "label": "beat"}})
    assert r.status_code == 200, r.text
    # The marker IS an edit (history, stale-edit checks, "export outdated")…
    assert c.get(f"/api/sessions/{sid}/head").json()["edl_hash"] != head_before
    # …but not a render: the same file is served, nothing is rendered.
    previews = tmp_path / "wd" / sid / "previews"
    n_before = sorted(p.name for p in previews.glob("*.mp4"))
    again = c.post(f"/api/sessions/{sid}/preview").json()
    assert again["cached"] is True and again["url"] == first["url"], again
    got = c.get(f"/api/sessions/{sid}/preview.mp4")
    assert got.status_code == 200 and len(got.content) > 1000
    assert sorted(p.name for p in previews.glob("*.mp4")) == n_before
    # A real edit still re-renders.
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_loudness_target",
                                                      "args": {"lufs": -23.0}})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/sessions/{sid}/preview").json()["cached"] is False


# ----------------------------------------------------- QA-086 audio speed

@pytest.fixture(scope="module")
def click_bed(tmp_path_factory) -> Path:
    """A 4 ms 1 kHz click at every whole second, 8 s — the sound alone."""
    p = tmp_path_factory.mktemp("cb") / "clicks.wav"
    _ff(["-f", "lavfi", "-i",
         "aevalsrc='if(lt(mod(t\\,1)\\,0.004)\\,0.8*sin(2*PI*1000*t)\\,0)':s=48000:d=8:c=stereo",
         "-c:a", "pcm_s16le", str(p)])
    return p


# keep_pitch=True is atempo's WSOLA — the documented limit (see the QA-039
# tests at the bottom): every transient within KEEP_PITCH_MAX_OFFSET_MS, the
# number the Keep pitch tooltip states, and at 2x a 4 ms click can be dropped
# outright. v1 shares the filters (audio_mix.speed_filters).
_KP = audio_mix.KEEP_PITCH_MAX_OFFSET_MS


@pytest.mark.parametrize("speed,keep_pitch,tol_ms", [(0.5, False, 1.0), (2.0, False, 1.0),
                                                     (0.5, True, _KP), (0.75, True, _KP),
                                                     (2.0, True, _KP)])
def test_speed_on_a_music_lane_retimes_the_sound(tmp_path, click_bed, speed, keep_pitch, tol_ms):
    s = _store(tmp_path / f"s{speed}{keep_pitch}", lufs=None)
    pic = tmp_path / "black.mov"
    _ff(["-f", "lavfi", "-i", "color=c=black:s=320x180:d=17:r=30", "-f", "lavfi", "-i",
         "anullsrc=r=48000:cl=stereo", "-t", "17", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:a", "pcm_s16le", str(pic)])
    dispatch(s, "add_clip", {"track": "v1", "src": str(pic), "in": 0, "out": 17, "start": 0})
    dispatch(s, "add_music", {"src": str(click_bed), "start": 1.0, "in": 0, "out": 8, "duck": False,
                              "volume_db": 0})
    mid = s.edl.get_track("music").clips[0].id
    dispatch(s, "set_property", {"clip_id": mid, "path": "audio.fade_in", "value": 0})
    dispatch(s, "set_property", {"clip_id": mid, "path": "audio.fade_out", "value": 0})
    r = dispatch(s, "set_speed", {"clip_id": mid, "factor": speed, "keep_pitch": keep_pitch})
    assert "Speed" in r["summary"]
    clip = s.edl.get_clip(mid)[1]
    assert clip.effective_duration == pytest.approx(8.0 / speed)
    got = click_times(render_export(s.edl, s.dir).path)
    want = [1.0 + k / speed for k in range(8)]
    # every click found lands on its retimed instant (nearest expected one)
    err = [min(((g - w) * 1000 for w in want), key=abs) for g in got]
    # to the 0.1 ms the bound is stated in (a click's onset is sample-quantised)
    assert round(max(abs(e) for e in err), 1) <= tol_ms, [round(e, 2) for e in err]
    assert len(got) >= (len(want) if not keep_pitch else len(want) - 1), (got, want)
    # the lane's sound ends where the timeline says the clip ends
    a = _channels(render_export(s.edl, s.dir).path)
    end = 1.0 + 8.0 / speed
    assert _rms_db(a[int((end + 0.05) * SR):int((end + 0.5) * SR), 0]) < -90.0


def test_slowing_a_music_clip_pushes_the_next_one_only_as_far_as_it_must(tmp_path, click_bed):
    s = _store(tmp_path / "s", lufs=None)
    dispatch(s, "add_music", {"src": str(click_bed), "start": 0.0, "in": 0, "out": 4, "duck": False})
    dispatch(s, "add_music", {"src": str(click_bed), "start": 6.0, "in": 0, "out": 2, "duck": False})
    a, b = s.edl.get_track("music").clips
    dispatch(s, "set_speed", {"clip_id": a.id, "factor": 0.8})      # 4 s -> 5 s: fits the gap
    assert b.start == pytest.approx(6.0)
    dispatch(s, "set_speed", {"clip_id": a.id, "factor": 0.5})      # 4 s -> 8 s: 2 s over
    assert b.start == pytest.approx(8.0)
    assert a.start + a.effective_duration <= b.start + 1e-6


# ------------------------------------------------ QA-100 audio-only export

@pytest.mark.parametrize("container,codec", [("m4a", "aac"), ("wav", "pcm_s24le")])
def test_audio_only_export_over_http(client, av_src, click_bed, container, codec):
    c = client
    sid = _session(c, av_src, name="Podcast Ep 1")
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_music", "args": {
        "src": str(click_bed), "start": 0, "in": 0, "out": 3, "duck": False}})
    assert r.status_code == 200, r.text
    r = c.post(f"/api/sessions/{sid}/export", json={"container": container})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == f"Podcast Ep 1 audio -16LUFS.{container}"
    got = c.get(body["url"])
    assert got.status_code == 200
    path = Path(body["path"])
    info = _probe(path)
    kinds = [st["codec_type"] for st in info["streams"]]
    assert kinds == ["audio"], kinds                              # no picture at all
    a = info["streams"][0]
    assert a["codec_name"] == codec and int(a["sample_rate"]) == 48000 and int(a["channels"]) == 2
    assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.05)
    i, tp = _loudness(path)
    assert i == pytest.approx(-16.0, abs=1.0) and tp <= -1.0, (i, tp)


def test_audio_only_export_as_a_background_job(client, av_src):
    import time
    c = client
    sid = _session(c, av_src)
    r = c.post(f"/api/sessions/{sid}/export?wait=0", json={"container": "m4a"})
    assert r.status_code == 202, r.text
    job = r.json()["job_id"]
    for _ in range(300):
        j = c.get(f"/api/jobs/{job}").json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert j["status"] == "completed", j
    assert j["result"]["filename"].endswith(".m4a")
    assert [st["codec_type"] for st in _probe(Path(j["result"]["path"]))["streams"]] == ["audio"]


def test_an_unknown_export_format_is_a_400(client, av_src):
    sid = _session(client, av_src)
    r = client.post(f"/api/sessions/{sid}/export", json={"container": "flac"})
    assert r.status_code in (400, 422), r.text


# --------------------------------------------- QA-039 keep pitch (limit)

def test_render_binary_has_no_rubberband_so_keep_pitch_is_atempo():
    """The documented limitation (CLAUDE.md, the Keep pitch tooltip): the
    render binary cannot run `rubberband`, probed with a real 0.1 s null graph
    the way the encoder ladder probes. If this starts failing, the binary
    gained a phase-vocoder stretcher — wire it into audio_mix.speed_filters
    (with a per-speed centring delay, measured) and retire the ±12 ms note."""
    proc = subprocess.run([_pu.FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                           "anullsrc=r=48000:cl=stereo", "-t", "0.1", "-af", "rubberband=tempo=1.5",
                           "-f", "null", "-"], capture_output=True, text=True)
    if proc.returncode == 0:
        pytest.skip(f"this ffmpeg {_ffmpeg_version()} build has librubberband; the documented "
                    "limitation applies to builds without it")
    assert proc.returncode != 0, "rubberband is available — see the docstring"


@pytest.mark.parametrize("speed", [0.5, 0.75, 1.5, 2.0])
def test_keep_pitch_on_v1_stays_inside_the_stated_bound(tmp_path, speed):
    """QA-039-KEEP-PITCH: the tooltip and CLAUDE.md said later transients land
    within about ±15 ms; a v1 export measured −17.3 ms at 0.5x (and a music
    lane −19.8 ms). The stated bound is `KEEP_PITCH_MAX_OFFSET_MS`, and every
    click after a keep-pitch seam, the first one included, is inside it."""
    clap = make_clap(tmp_path / "clap.mp4", seconds=12, fps=30)
    s = _store(tmp_path / "s", lufs=None)
    dispatch(s, "add_clip", {"track": "v1", "src": str(clap), "in": 0, "out": 12, "start": 0})
    dispatch(s, "split_at", {"track": "v1", "time": 6.0})
    second = s.edl.get_track("v1").clips[1].id
    dispatch(s, "set_speed", {"clip_id": second, "factor": speed, "keep_pitch": True})
    offs = av_offsets_ms(render_export(s.edl, s.dir).path)
    after_seam = offs[6:]
    assert after_seam, offs
    assert round(max(abs(o) for o in after_seam), 1) <= audio_mix.KEEP_PITCH_MAX_OFFSET_MS, \
        [round(o, 1) for o in after_seam]
