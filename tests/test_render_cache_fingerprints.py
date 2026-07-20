"""Render-cache fingerprint correctness.

The chunk cache bakes per-clip audio (gain/fade/mute) into each chunk, and
the video-only cache exists so audio-only edits skip the video re-encode.
Both caches are only correct if their fingerprints match what is actually
baked:

- ``fingerprint_clip`` must include ``clip.audio`` — otherwise a volume/fade
  edit reuses the stale chunk and the edit is silently dropped from the
  preview.
- ``_video_only_fingerprint`` must EXCLUDE per-clip audio — audio never
  changes pixels, and including it forces a full video re-encode on every
  gain edit instead of the cheap remux.
- the audio remux fast path must produce the same audible mix as the full
  render, including V2/PIP audio.
"""
from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path

from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas
from video_ai_editor.render import render_preview
from video_ai_editor.render.chunks import fingerprint_clip
from video_ai_editor.render.compositor import _video_only_fingerprint


def _mk_video(path: Path, *, freq: int = 440, duration: float = 2.0,
              color: str = "blue", w: int = 320, h: int = 180):
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=f={freq}:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _mk_audio(path: Path, *, freq: int = 220, duration: float = 2.0):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=f={freq}:duration={duration}",
         "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )


def _mean_volume(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, f"volumedetect produced no mean_volume:\n{proc.stderr[-1500:]}"
    return float(m.group(1))


def _clip(src: Path, **kw) -> Clip:
    return Clip(src=str(src), in_=0, out=2, start=0, id="c1", **kw)


# ---------------------------------------------------------------- unit level

def test_chunk_fingerprint_changes_when_audio_changes():
    c = _clip(Path("x.mp4"))
    kw = dict(canvas_w=320, canvas_h=180, fps=30, encoder_args=["-c:v", "libx264"])
    base = fingerprint_clip(c, **kw)

    gained = c.model_copy(deep=True)
    gained.audio.gain_db = -20.0
    assert fingerprint_clip(gained, **kw) != base

    muted = c.model_copy(deep=True)
    muted.audio.mute = True
    assert fingerprint_clip(muted, **kw) != base

    faded = c.model_copy(deep=True)
    faded.audio.fade_in = 0.5
    assert fingerprint_clip(faded, **kw) != base


def _one_clip_edl(src: Path) -> EDL:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[_clip(src)]),
    ])
    edl.recompute_duration()
    return edl


def test_video_only_fingerprint_ignores_v1_clip_audio():
    edl = _one_clip_edl(Path("x.mp4"))
    base = _video_only_fingerprint(edl)
    edl.tracks[0].clips[0].audio.gain_db = -20.0
    edl.tracks[0].clips[0].audio.fade_out = 0.3
    assert _video_only_fingerprint(edl) == base


def test_video_only_fingerprint_ignores_v1_track_mute():
    """v1 mute is audio-only (the base layer stays visible) — it must not
    force a video re-encode. V2 mute hides the overlay, so it stays."""
    edl = _one_clip_edl(Path("x.mp4"))
    base = _video_only_fingerprint(edl)
    edl.tracks[0].muted = True
    assert _video_only_fingerprint(edl) == base


def test_video_only_fingerprint_tracks_visual_changes():
    edl = _one_clip_edl(Path("x.mp4"))
    base = _video_only_fingerprint(edl)
    edl.tracks[0].clips[0].transform.scale = 0.5
    assert _video_only_fingerprint(edl) != base


# ---------------------------------------------------------- integration level

def test_warm_chunk_cache_volume_change_is_audible(tmp_path: Path):
    """Chunk path: a gain edit must re-bake the chunk, not reuse stale audio."""
    src = tmp_path / "src.mp4"
    _mk_video(src)
    edl = _one_clip_edl(src)

    loud = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    edl.tracks[0].clips[0].audio.gain_db = -40.0
    # Force the full-render (chunk) path: drop the video-only cache so the
    # remux fast path can't answer for this edit.
    shutil.rmtree(tmp_path / "cache" / "videos", ignore_errors=True)
    quiet = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    assert quiet <= loud - 25, (
        f"gain -40dB edit not audible via chunk path: {loud=} {quiet=}"
    )


def test_volume_change_via_remux_path_is_audible(tmp_path: Path):
    """Remux path: with the video-only cache warm, a v1 gain edit must still
    change the audible mix (and may skip the video re-encode)."""
    src = tmp_path / "src.mp4"
    _mk_video(src)
    edl = _one_clip_edl(src)

    loud = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    edl.tracks[0].clips[0].audio.gain_db = -40.0
    quiet = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    assert quiet <= loud - 25, (
        f"gain -40dB edit not audible via remux path: {loud=} {quiet=}"
    )


def test_remux_path_preserves_pip_audio(tmp_path: Path):
    """A music-only edit takes the remux fast path; the PIP (V2) audio that
    the full render mixes in must survive the remux."""
    v1_src = tmp_path / "v1.mp4"
    pip_src = tmp_path / "pip.mp4"
    music_src = tmp_path / "music.m4a"
    _mk_video(v1_src, freq=440)
    _mk_video(pip_src, freq=880, color="red")
    _mk_audio(music_src)

    v1_clip = _clip(v1_src)
    v1_clip.audio.gain_db = -50.0  # near-silent: the PIP tone dominates
    pip_clip = Clip(src=str(pip_src), in_=0, out=2, start=0, id="p1")
    music_clip = Clip(src=str(music_src), in_=0, out=2, start=0, id="m1")
    music_clip.audio.gain_db = -50.0
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[v1_clip]),
        Track(id="v2", type="video", z=1, clips=[pip_clip]),
        Track(id="music", type="music", clips=[music_clip]),
    ])
    edl.recompute_duration()

    with_pip = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    # Audio-only edit on the music track → video fingerprint unchanged →
    # remux fast path.
    music_clip.audio.gain_db = -45.0
    after = _mean_volume(render_preview(edl, tmp_path, height=180).path)

    assert after >= with_pip - 10, (
        f"PIP audio lost on remux path: {with_pip=} {after=}"
    )
