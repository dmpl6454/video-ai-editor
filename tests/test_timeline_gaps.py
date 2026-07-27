"""The rendered file must be exactly `edl.duration` long and match the timeline.

v1 used to be assembled as a bare `concat` of its clips, packing them from t=0
and discarding `clip.start`. The timeline drew a gap, the render silently closed
it, and the output was shorter than the UI's duration — proven in the wild by a
session whose `edl.duration` was 84.27s while every rendered preview ffprobed at
62.31s. Downstream that mismatch is what made dragging a clip look like a no-op,
let the playhead run past the end of the video, and cut music off mid-timeline
(`amix=duration=first` truncates to input 0, the v1 chain).

These tests pin the invariant at the only level that can't lie: the actual bytes
ffmpeg produced.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track
from video_ai_editor.render.compositor import (_has_v1_gaps, _v1_segments,
                                               render_preview)

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


# ---------------------------------------------------------------------------
# fixtures

def _mkvideo(p: Path, dur: float, color: str = "white") -> str:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=320x180:d={dur}:r=30",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(p)], check=True, capture_output=True)
    return str(p)


def _mkaudio(p: Path, dur: float) -> str:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=220:duration={dur}",
                    "-c:a", "libmp3lame", str(p)], check=True, capture_output=True)
    return str(p)


def _store(tmp_path: Path, tracks: list[Track]) -> EDLStore:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=tracks)
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def _luma(p: Path, t: float, tmp_path: Path) -> float:
    png = tmp_path / f"probe_{t}.png"
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", str(p), "-frames:v", "1",
                    str(png)], check=True, capture_output=True)
    with Image.open(png) as im:
        return ImageStat.Stat(im.convert("L")).mean[0]


def _max_volume(p: Path, ss: float, dur: float) -> float:
    r = subprocess.run(["ffmpeg", "-ss", str(ss), "-t", str(dur), "-i", str(p),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    for line in r.stderr.splitlines():
        if "max_volume" in line:
            return float(line.split(":")[1].strip().split()[0])
    return -999.0


# ---------------------------------------------------------------------------
# 1) segment planning (pure, no ffmpeg)

def test_segments_gapless_timeline_has_no_filler():
    clips = [Clip(id="c1", src="/x.mp4", in_=0.0, out=4.0, start=0.0),
             Clip(id="c2", src="/x.mp4", in_=0.0, out=4.0, start=4.0)]
    assert _v1_segments(clips, 8.0) == [("clip", 0), ("clip", 1)]
    assert _has_v1_gaps(clips, 8.0) is False


def test_segments_leading_interior_and_trailing_gaps():
    clips = [Clip(id="c1", src="/x.mp4", in_=0.0, out=4.0, start=2.0),
             Clip(id="c2", src="/x.mp4", in_=0.0, out=4.0, start=10.0)]
    assert _v1_segments(clips, 20.0) == [
        ("gap", 2.0), ("clip", 0), ("gap", 4.0), ("clip", 1), ("gap", 6.0),
    ]


def test_segments_never_emit_negative_filler_for_overlaps():
    """Legacy EDLs can hold overlapping clips; they keep the old packing."""
    clips = [Clip(id="c1", src="/x.mp4", in_=0.0, out=4.0, start=0.0),
             Clip(id="c2", src="/x.mp4", in_=0.0, out=4.0, start=1.0)]
    segs = _v1_segments(clips, 8.0)
    assert all(kind != "gap" or float(v) > 0 for kind, v in segs)
    assert [k for k, _ in segs] == ["clip", "clip"]


def test_segments_speed_uses_effective_duration():
    """A 2x clip occupies half the timeline, so the trailing gap grows."""
    c = Clip(id="c1", src="/x.mp4", in_=0.0, out=4.0, start=0.0, speed=2.0)
    assert c.effective_duration == pytest.approx(2.0)
    assert _v1_segments([c], 5.0) == [("clip", 0), ("gap", 3.0)]


# ---------------------------------------------------------------------------
# 2) rendered output actually matches the timeline

def test_interior_gap_renders_black_and_keeps_total_duration(tmp_path):
    src = _mkvideo(tmp_path / "v.mp4", 4)
    store = _store(tmp_path, [Track(id="v1", type="video", clips=[
        Clip(id="c1", src=src, in_=0.0, out=4.0, start=0.0),
        Clip(id="c2", src=src, in_=0.0, out=4.0, start=10.0)])])
    assert store.edl.duration == pytest.approx(14.0)

    out = render_preview(store.edl, store.dir, height=180).path
    assert _duration(out) == pytest.approx(14.0, abs=0.35)
    assert _luma(out, 2.0, tmp_path) > 240      # first clip
    assert _luma(out, 7.0, tmp_path) < 2        # the gap is black
    assert _luma(out, 12.0, tmp_path) > 240     # second clip, at its real time


def test_leading_offset_is_rendered(tmp_path):
    src = _mkvideo(tmp_path / "v.mp4", 4)
    store = _store(tmp_path, [Track(id="v1", type="video", clips=[
        Clip(id="c1", src=src, in_=0.0, out=4.0, start=3.0)])])

    out = render_preview(store.edl, store.dir, height=180).path
    assert _duration(out) == pytest.approx(7.0, abs=0.35)
    assert _luma(out, 1.0, tmp_path) < 2
    assert _luma(out, 5.0, tmp_path) > 240


def test_music_outlasting_video_is_not_truncated(tmp_path):
    """`amix=duration=first` cut the mix to the v1 length — the reported
    "music stops unexpectedly in the middle of the timeline"."""
    src = _mkvideo(tmp_path / "v.mp4", 4)
    mus = _mkaudio(tmp_path / "m.mp3", 12)
    store = _store(tmp_path, [
        Track(id="v1", type="video", clips=[
            Clip(id="c1", src=src, in_=0.0, out=4.0, start=0.0)]),
        Track(id="music", type="music", z=5, clips=[
            Clip(id="m1", src=mus, in_=0.0, out=12.0, start=0.0)])])

    out = render_preview(store.edl, store.dir, height=180).path
    assert _duration(out) == pytest.approx(12.0, abs=0.35)
    # Audible well past the end of the video, and the picture is black there.
    assert _max_volume(out, 8.0, 2.0) > -60.0
    assert _luma(out, 9.0, tmp_path) < 2


def test_music_only_timeline_is_not_silent(tmp_path):
    """No v1 clips used to short-circuit to black+anullsrc, dropping the music."""
    mus = _mkaudio(tmp_path / "m.mp3", 8)
    store = _store(tmp_path, [
        Track(id="v1", type="video", clips=[]),
        Track(id="music", type="music", z=5, clips=[
            Clip(id="m1", src=mus, in_=0.0, out=8.0, start=0.0)])])

    out = render_preview(store.edl, store.dir, height=180).path
    assert _duration(out) == pytest.approx(8.0, abs=0.35)
    assert _max_volume(out, 2.0, 3.0) > -60.0


def test_plain_audio_lane_a1_is_audible(tmp_path):
    """`a1` ("Main audio") was read by no render path at all."""
    src = _mkvideo(tmp_path / "v.mp4", 4)
    aud = _mkaudio(tmp_path / "a.mp3", 4)
    store = _store(tmp_path, [
        Track(id="v1", type="video", clips=[
            Clip(id="c1", src=src, in_=0.0, out=4.0, start=0.0)]),
        Track(id="a1", type="audio", z=4, clips=[
            Clip(id="a1c", src=aud, in_=0.0, out=4.0, start=0.0)])])

    out = render_preview(store.edl, store.dir, height=180).path
    assert _max_volume(out, 1.0, 2.0) > -60.0


def test_gapless_timeline_length_unchanged(tmp_path):
    """Regression guard: the common (gapless) case must be byte-for-byte sane."""
    src = _mkvideo(tmp_path / "v.mp4", 4)
    store = _store(tmp_path, [Track(id="v1", type="video", clips=[
        Clip(id="c1", src=src, in_=0.0, out=4.0, start=0.0),
        Clip(id="c2", src=src, in_=0.0, out=4.0, start=4.0)])])

    out = render_preview(store.edl, store.dir, height=180).path
    assert _duration(out) == pytest.approx(8.0, abs=0.35)


# ---------------------------------------------------------------------------
# 3) transitions vs. gaps (filtergraph shape only — no encode needed)

def _fc(clips, total, transitions=None):
    from video_ai_editor.render.compositor import _build_filter_complex
    fc, _, _, _ = _build_filter_complex(
        clips, 320, 180, transitions=transitions or [], fps=30,
        total_duration=total)
    return fc


def test_transition_still_applies_at_a_gapless_boundary():
    from video_ai_editor.edl.schema import Transition
    clips = [Clip(id="c1", src="/x.mp4", in_=0.0, out=2.0, start=0.0),
             Clip(id="c2", src="/x.mp4", in_=0.0, out=2.0, start=2.0)]
    fc = _fc(clips, 4.0, [Transition(at=2.0, type="fade", duration=0.5)])
    assert "xfade" in fc


def test_transition_across_a_gap_is_dropped_not_crossfaded():
    """A cross-fade over intervening black is meaningless — and the two clips
    are no longer adjacent streams, so xfading them would also silently delete
    the gap again."""
    from video_ai_editor.edl.schema import Transition
    clips = [Clip(id="c1", src="/x.mp4", in_=0.0, out=2.0, start=0.0),
             Clip(id="c2", src="/x.mp4", in_=0.0, out=2.0, start=5.0)]
    fc = _fc(clips, 7.0, [Transition(at=2.0, type="fade", duration=0.5)])
    assert "xfade" not in fc
    assert "color=c=black" in fc          # the gap is still rendered
    assert "concat=n=3" in fc             # clip, filler, clip


def test_transition_boundary_uses_timeline_time_not_packed_time():
    """`tr.at` is a TIMELINE position. Matching it against a running sum of
    durations from 0 drifted the moment the timeline had a leading offset."""
    from video_ai_editor.edl.schema import Transition
    clips = [Clip(id="c1", src="/x.mp4", in_=0.0, out=2.0, start=3.0),
             Clip(id="c2", src="/x.mp4", in_=0.0, out=2.0, start=5.0)]
    # Real boundary is at 5.0s; the old packed-sum logic would have looked for 2.0s.
    assert "xfade" in _fc(clips, 7.0, [Transition(at=5.0, type="fade", duration=0.5)])
    assert "xfade" not in _fc(clips, 7.0, [Transition(at=2.0, type="fade", duration=0.5)])


# ---------------------------------------------------------------------------
# 4) the trailing gap is a VIDEO input, so it must be in the video-only key

def test_video_only_fingerprint_tracks_timeline_extent():
    """v1 now pads with black out to `edl.duration`, and that duration is set by
    every lane — including the music/vo tracks `_video_only_fingerprint`
    deliberately excludes. Without the extent in the key, trimming a 12s music
    bed to 6s reuses the cached 12s video and remuxes it against 6s of audio,
    yielding 6s of silent black welded onto the end."""
    from video_ai_editor.render.compositor import _video_only_fingerprint

    def edl_with_music(music_out: float) -> EDL:
        e = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
            Track(id="v1", type="video", clips=[
                Clip(id="c1", src="/v.mp4", in_=0.0, out=4.0, start=0.0)]),
            Track(id="music", type="music", z=5, clips=[
                Clip(id="m1", src="/m.mp3", in_=0.0, out=music_out, start=0.0)]),
        ])
        e.recompute_duration()
        return e

    long_bed, short_bed = edl_with_music(12.0), edl_with_music(6.0)
    assert long_bed.duration != short_bed.duration
    assert _video_only_fingerprint(long_bed) != _video_only_fingerprint(short_bed)

    # A pure gain edit does NOT move the timeline end, so the cheap
    # audio-only remux path must still be reachable.
    quiet = edl_with_music(12.0)
    quiet.get_track("music").clips[0].audio.gain_db = -12.0
    assert _video_only_fingerprint(quiet) == _video_only_fingerprint(long_bed)


def test_streamcopy_rejects_a_short_assembly(tmp_path, monkeypatch):
    """The packet-copy preview shortcut must verify it produced the whole
    timeline. On Windows CI a timeline whose clips share ONE source file
    (identical chunk fingerprint → the same chunk listed twice) concatenated
    to a single clip's length with rc=0 and no warning. The caller falls back
    to the re-encode on any exception, so the guard just has to raise."""
    from video_ai_editor.render import compositor as C

    src = _mkvideo(tmp_path / "v.mp4", 4)
    store = _store(tmp_path, [Track(id="v1", type="video", clips=[
        Clip(id="c1", src=src, in_=0.0, out=4.0, start=0.0),
        Clip(id="c2", src=src, in_=0.0, out=4.0, start=4.0)])])

    # Simulate the platform quirk: the assembly silently yields half the length.
    monkeypatch.setattr(C, "_probe_duration", lambda p: 3.99)
    with pytest.raises(RuntimeError, match="falling back to re-encode"):
        C._assemble_chunks_streamcopy(store.edl, [tmp_path / "v.mp4"],
                                      tmp_path / "out.mp4")


def test_duplicate_source_clips_render_full_length(tmp_path):
    """End-to-end guard for the same case, through the real preview path."""
    src = _mkvideo(tmp_path / "v.mp4", 4)
    store = _store(tmp_path, [Track(id="v1", type="video", clips=[
        Clip(id="c1", src=src, in_=0.0, out=4.0, start=0.0),
        Clip(id="c2", src=src, in_=0.0, out=4.0, start=4.0)])])
    out = render_preview(store.edl, store.dir, height=180).path
    assert _duration(out) == pytest.approx(8.0, abs=0.35)
