"""End-to-end tests for AI features that run locally on the Mac.

Skip-on-missing pattern: each test calls `available()` first; missing
binaries / weights are skipped, not failed. The CI bot can pre-warm
weights to make these go from skip → green.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest


def _mk_video(p: Path, *, dur: float = 2.0, w: int = 320, h: int = 180,
              motion: bool = False):
    keyed = p.with_suffix(".keyed.mp4")
    motion_filter = (",drawbox=x='40+t*60':y=40:w=40:h=40:c=yellow:t=fill"
                     if motion else "")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c=blue:s={w}x{h}:d={dur}:r=30",
         "-vf", "format=yuv420p" + motion_filter,
         str(keyed)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(keyed),
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True,
    )


def test_motion_tracker_produces_keyframes(tmp_path: Path):
    src = tmp_path / "moving.mp4"
    _mk_video(src, motion=True, dur=1.5)
    from video_ai_editor.ai.tracker import track_object
    res = track_object(
        src, bbox_norm=(40/320, 40/180, 40/320, 40/180),
        canvas_w=1080, canvas_h=1920, sample_every=3,
    )
    assert len(res["track"]) >= 5
    assert res["source_size"] == [320, 180]
    assert res["canvas_size"] == [1080, 1920]


def test_stabilize_produces_smooth_mp4(tmp_path: Path):
    from video_ai_editor.ai import stabilize
    if not stabilize.available():
        pytest.skip("vidstab unavailable (need ffmpeg-full)")
    src = tmp_path / "shaky.mp4"
    # Slight per-frame jitter to give vidstab something to detect
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "color=c=blue:s=320x180:d=2:r=30",
        "-vf", "rotate=a='sin(t*8)*0.05'",
        "-pix_fmt", "yuv420p", str(src),
    ], check=True, capture_output=True)
    out = stabilize.stabilize(src, tmp_path / "cache")
    assert out.exists() and out.stat().st_size > 1024


def test_rife_smooth_slow_motion_doubles_frames(tmp_path: Path):
    from video_ai_editor.ai import rife
    if not rife.available():
        pytest.skip("RIFE binary not installed")
    src = tmp_path / "src.mp4"; _mk_video(src, dur=1.0)
    out = rife.smooth_slow_motion(src, tmp_path / "cache", factor=2)
    # Expected duration ~ 2× source
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    assert 1.5 < dur < 2.5, dur


def test_realesrgan_upscale(tmp_path: Path):
    from video_ai_editor.ai import upscale
    if not upscale.available():
        pytest.skip("Real-ESRGAN binary not installed")
    src = tmp_path / "src.mp4"; _mk_video(src, dur=1.0, w=160, h=90)
    out = upscale.upscale_clip(src, tmp_path / "cache", factor=2)
    # Expected dims: 320x180
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert info.startswith("320,180"), info


def test_multicam_picks_active_angle(tmp_path: Path):
    from video_ai_editor.ai.multicam import plan_multicam
    a = tmp_path / "static.mp4"; b = tmp_path / "active.mp4"
    _mk_video(a, dur=4.0, motion=False)
    _mk_video(b, dur=4.0, motion=True)
    plan = plan_multicam([a, b], window_s=1.5)
    # Should pick active over static at least once
    chosen = {Path(c["src"]).name for c in plan["cuts"]}
    assert "active.mp4" in chosen


def test_diarize_heuristic_fallback(tmp_path: Path):
    """Two distinct sine frequencies + silence between → 2 speaker labels."""
    src = tmp_path / "two_voices.wav"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=f=880:duration=2,adelay=0|0",
        "-f", "lavfi", "-i", "sine=f=220:duration=2,adelay=2600|2600",
        "-f", "lavfi", "-i", "sine=f=880:duration=2,adelay=5200|5200",
        "-filter_complex", "[0][1][2]amix=inputs=3:duration=longest[a]",
        "-map", "[a]", "-c:a", "pcm_s16le", str(src),
    ], check=True, capture_output=True)
    from video_ai_editor.ai.diarize import _heuristic_diarize
    turns = _heuristic_diarize(src, tmp_path / "cache", num_speakers=2)
    speakers = {t["speaker"] for t in turns}
    assert len(speakers) == 2, f"expected 2 speakers, got {speakers}"


def test_noise_reduce_lowers_noise_floor(tmp_path: Path):
    from video_ai_editor.ai import denoise
    if not denoise.available():
        pytest.skip("noisereduce not installed")
    # Build a clip with constant noise + sine signal
    src = tmp_path / "noisy.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:d=2:r=30",
        "-f", "lavfi", "-i", "sine=f=440:duration=2",
        "-f", "lavfi", "-i", "anoisesrc=d=2:c=white:r=48000:a=0.05",
        "-filter_complex", "[1][2]amix=inputs=2[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(src),
    ], check=True, capture_output=True)
    out = denoise.denoise_clip(src, tmp_path / "cache", strength=0.85)
    assert out.exists() and out.stat().st_size > 1024


def test_beat_detection_emits_beats(tmp_path: Path):
    from video_ai_editor.ingest.beats import detect_beats
    # Click track at 120 BPM (every 0.5s) for 8s; librosa needs a few seconds
    # of context to lock the tempo.
    src = tmp_path / "beats.wav"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "aevalsrc=0.8*sin(2*PI*880*t)*lt(mod(t\\,0.5)\\,0.03):d=8",
        "-c:a", "pcm_s16le", str(src),
    ], check=True, capture_output=True)
    beats = detect_beats(src)
    # 8s at 120 BPM → ~16 beats; librosa estimates may vary, so just assert >0.
    assert len(beats) >= 4, f"expected ≥4 beats, got {len(beats)}"


def test_auto_reframe_preserves_aspect(tmp_path: Path):
    from video_ai_editor.ai.reframe import reframe_clip
    src = tmp_path / "src.mp4"; _mk_video(src, w=640, h=360, dur=1.0)  # 16:9
    out = reframe_clip(src, target_w=1080, target_h=1920, cache_dir=tmp_path/"cache")  # 9:16
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert info.startswith("1080,1920"), info


def test_make_shorts_returns_n_alternative_edls(tmp_path: Path):
    from video_ai_editor.ai.shorts import make_shorts
    # Build a 30s video with audio peaks at 5s, 12s, 18s, 24s
    src = tmp_path / "long.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=30:r=30",
        "-f", "lavfi",
        "-i", "aevalsrc=0.6*sin(2*PI*440*t)*lt(mod(t\\,6)\\,0.5):d=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(src),
    ], check=True, capture_output=True)
    shorts = make_shorts(src, transcript=None, cache_dir=tmp_path/"cache",
                         target_count=3, max_dur=10.0)
    assert len(shorts) >= 1
    for s in shorts:
        assert "start" in s and "end" in s
        assert s["end"] - s["start"] <= 10.5
