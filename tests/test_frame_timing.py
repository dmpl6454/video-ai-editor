"""Frame accuracy and A/V timing, measured on real renders (QA-002/030/038/039/040).

Every assertion here decodes what ffmpeg actually wrote — frame numbers from a
binary frame-counter clip, flash/click offsets from a clap track, per-second
RMS — never a container duration or a filter string. See timing_fixtures.py.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from timing_fixtures import (
    av_offsets_ms, click_times, decode_frame_numbers, flash_times,
    make_clap, make_frame_counter, probe_video, rms_db_per_window,
)
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.render import render_export, render_preview

FPS = 30


def _store(sd: Path) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=320, h=180, fps=FPS)
    # loudnorm (export-only) rides the gain over the first seconds, which
    # hides the t=0 click from the onset detector; timing is what's measured.
    e.canvas.loudness_lufs = None
    (sd / "edl.json").write_text(e.model_dump_json())
    return EDLStore(sd)


def _v1(store: EDLStore):
    return store.edl.get_track("v1").clips


def _on_grid(t: float) -> bool:
    return abs(t * FPS - round(t * FPS)) < 1e-6


def _discontinuities(nums: list[int]) -> list[tuple[int, int, int]]:
    return [(i, a, b) for i, (a, b) in enumerate(zip(nums, nums[1:])) if b != a + 1]


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("timing_media")
    return {
        # 600 frames; audio 0.25 s longer than the picture, like an AAC import.
        "fc": make_frame_counter(d / "fc30.mp4", frames=600, fps=FPS, audio_extra_s=0.25),
        "clap": make_clap(d / "clap.mp4", seconds=16, fps=FPS),
    }


# --------------------------------------------------------------- QA-002

def test_pure_split_is_lossless_and_frame_exact(tmp_path, media):
    """Split at 5.0125 s and 3 frames later: 600 frames 0..599 out, no dups.

    Pre-fix: the EDL kept 5.0125 and the container's audio-padded length as
    `out`, and the renderer's `-ss/-to %.3f` rounded every clip up a frame —
    602 frames with 151 and 154 doubled."""
    s = _store(tmp_path / "s")
    container = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(media["fc"])], capture_output=True, text=True, check=True).stdout)
    assert container > 20.2  # the fixture really is audio-padded
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["fc"]),
                             "in": 0.0, "out": container, "start": 0.0})
    dispatch(s, "split_at", {"time": 5.0125})
    dispatch(s, "split_at", {"time": 5.0125 + 3 / FPS})

    clips = _v1(s)
    assert [(c.in_, c.out, c.start) for c in clips] == [
        (0.0, 5.0, 0.0), (5.0, 5.1, 5.0), (5.1, 20.0, 5.1)]
    assert all(_on_grid(x) for c in clips for x in (c.in_, c.out, c.start))

    for path in (render_export(s.edl, s.dir).path, render_preview(s.edl, s.dir).path):
        nums = decode_frame_numbers(path)
        assert len(nums) == 600, (path.name, len(nums))
        assert nums == list(range(600)), _discontinuities(nums)[:5]


def test_edit_times_are_quantised_and_idempotent(tmp_path, media):
    """Every committed edit time lands on n/fps; re-applying is a no-op."""
    s = _store(tmp_path / "q")
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["fc"]),
                             "in": 0.0137, "out": 19.9911, "start": 0.0})
    dispatch(s, "cut_range", {"track": "v1", "start": 3.0171, "end": 4.2049})
    dispatch(s, "split_at", {"time": 7.7777})
    c = _v1(s)[-1]
    dispatch(s, "trim_clip", {"clip_id": c.id, "out": c.out - 0.3123})
    dispatch(s, "move_clip", {"clip_id": c.id, "new_start": c.start + 0.5019})
    for c in _v1(s):
        for x in (c.in_, c.out, c.start):
            assert _on_grid(x), (c.id, x)
            assert tb.quantize(x, FPS) == x
    # a cut narrower than half a frame is no cut at all — rejected, not stored
    with pytest.raises(ValueError):
        dispatch(s, "cut_range", {"track": "v1", "start": 1.001, "end": 1.011})


def test_ten_subframe_cuts_export_matches_edl_and_vo_stays_on_flash(tmp_path, media):
    """10 sub-frame cut_range cuts: the export is edl.duration long (±1 frame),
    every flash keeps its click, and a VO click placed on a flash stays on it.

    Pre-fix: each seam gained a frame, the file ran 0.3-0.6 s long and a VO
    placed on a flash after the cuts landed ~155 ms early against the picture."""
    s = _store(tmp_path / "c")
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["clap"]),
                             "in": 0, "out": 16, "start": 0})
    for k in range(10):
        t0 = 13.3 - k * 1.0 + 0.0137
        dispatch(s, "cut_range", {"track": "v1", "start": t0, "end": t0 + 0.2013})

    # VO: a single click, placed on the flash of source second 13. The cuts
    # snap to [13.3,13.5), [12.3,12.5) … [4.3,4.5) — nine of them before it,
    # 6 frames each — so on the EDL clock that flash is at 13.0 - 1.8 = 11.2.
    vo = tmp_path / "vo_click.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "aevalsrc='if(lt(t\\,0.004)\\,0.8*sin(2*PI*1000*t)\\,0)':s=48000:d=1",
         str(vo)], check=True)
    dispatch(s, "set_track_muted", {"track": "v1", "muted": True})
    dispatch(s, "record_voiceover", {"src": str(vo), "start": 11.2})

    out = render_export(s.edl, s.dir).path
    frames = probe_video(out)["frames"]
    assert abs(frames - tb.frame_of(s.edl.duration, FPS)) <= 1, (frames, s.edl.duration)

    flashes = flash_times(out)
    near = min(flashes, key=lambda f: abs(f - 11.2))
    assert abs(near - 11.2) < 0.5 / FPS, flashes          # picture on the EDL clock
    clicks = click_times(out)
    assert len(clicks) == 1
    assert abs(clicks[0] - near) * 1000 <= 5.0, (clicks, near)


def test_preview_audio_does_not_drift_across_cuts(tmp_path, media, monkeypatch):
    """QA-030: the preview's chunk fast path (concat demuxer, AAC copied)
    pushed the sound ~32 ms later per cut — 319 ms after 10 frame-aligned
    cuts — while the export stayed in sync. Now every flash keeps its click,
    in preview exactly as in export. The spy pins that the preview really
    went through the fast path (a fallback to the re-encode would pass
    without testing it)."""
    from video_ai_editor.render import compositor as C
    used: list[int] = []
    real = C._assemble_chunks_streamcopy

    def spy(*a, **k):
        used.append(1)
        return real(*a, **k)

    monkeypatch.setattr(C, "_assemble_chunks_streamcopy", spy)
    s = _store(tmp_path / "p")
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["clap"]),
                             "in": 0, "out": 16, "start": 0})
    for k in range(10):
        t0 = 13.3 - k * 1.0
        dispatch(s, "cut_range", {"track": "v1", "start": t0, "end": t0 + 0.2})
    preview = render_preview(s.edl, s.dir).path
    assert used, "preview did not take the chunk fast path"
    assert probe_video(preview)["frames"] == tb.frame_of(s.edl.duration, FPS)
    for path in (preview, render_export(s.edl, s.dir).path):
        offs = av_offsets_ms(path)
        assert len(offs) >= 14, offs
        assert max(abs(o) for o in offs) <= 5.0, (path.name, [round(o, 1) for o in offs])


# --------------------------------------------------------------- QA-039

@pytest.mark.parametrize("speed", [2.0, 1.5, 0.5])
def test_speed_changed_clip_keeps_picture_on_sound(tmp_path, media, speed):
    """Picture of a retimed clip lands exactly where the timeline says and the
    sound within half a frame of it. Pre-fix: 2x/1.5x clicks 55-67 ms early
    against a picture two frames late."""
    s = _store(tmp_path / f"sp{speed}")
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["clap"]),
                             "in": 0, "out": 16, "start": 0})
    dispatch(s, "split_at", {"time": 6.0})
    dispatch(s, "set_speed", {"clip_id": _v1(s)[1].id, "factor": speed})
    out = render_export(s.edl, s.dir).path

    assert probe_video(out)["frames"] == tb.frame_of(s.edl.duration, FPS)
    # picture: first bright frame of each flash at 6 + k/speed, exactly
    flashes = flash_times(out)
    starts = [f for i, f in enumerate(flashes) if i == 0 or f - flashes[i - 1] > 0.05]
    expect = [6.0 + k / speed for k in range(1, 6)]
    for e in expect:
        assert min(abs(f - e) for f in starts) < 0.001, (e, starts)
    # sound: every click after the seam within half a frame of its flash
    clicks = click_times(out)
    for e in expect:
        c = min(clicks, key=lambda x: abs(x - e))
        assert abs(c - e) * 1000 <= 0.5 * 1000 / FPS + 1.0, (speed, e, c)


# --------------------------------------------------------------- QA-038

@pytest.mark.parametrize("speed", [0.5, 2.0])
def test_audio_fade_out_on_speed_clip_is_in_timeline_time(tmp_path, speed):
    """fade_out=1 s on a retimed clip fades over the last TIMELINE second.
    Pre-fix at 0.5x: fade at 9-10 s then -180 dB silence to 20 s; at 2x no fade."""
    tone = tmp_path / "tone10.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=gray:s=320x180:r={FPS}:d=10",
         "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:duration=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(tone)],
        check=True)
    s = _store(tmp_path / "f")
    dispatch(s, "add_clip", {"track": "v1", "src": str(tone), "in": 0, "out": 10, "start": 0})
    cid = _v1(s)[0].id
    dispatch(s, "set_speed", {"clip_id": cid, "factor": speed})
    dispatch(s, "add_fade", {"clip_id": cid, "out_s": 1.0})
    out = render_export(s.edl, s.dir).path
    rms = rms_db_per_window(out, win_s=0.5)
    total = 10 / speed
    body = rms[1:int((total - 1.0) / 0.5)]        # everything before the fade
    ref = rms[1]
    assert all(abs(x - ref) < 1.5 for x in body), [round(x, 1) for x in rms]
    last = rms[int(total / 0.5) - 1]              # the final half second
    assert last < ref - 4.0, [round(x, 1) for x in rms]


# --------------------------------------------------------------- QA-040

def test_audio_less_v1_clip_still_previews_and_exports(tmp_path, media):
    """A picture-only source on v1 renders with silence of its exact length
    instead of failing the whole timeline on `[i:a]`."""
    mute = tmp_path / "noaudio.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(media["fc"]),
                    "-t", "2", "-an", "-c:v", "copy", str(mute)], check=True)
    s = _store(tmp_path / "n")
    dispatch(s, "add_clip", {"track": "v1", "src": str(media["clap"]),
                             "in": 0, "out": 3, "start": 0})
    dispatch(s, "add_clip", {"track": "v1", "src": str(mute), "in": 0, "out": 2, "start": 3})
    for path in (render_preview(s.edl, s.dir).path, render_export(s.edl, s.dir).path):
        assert probe_video(path)["frames"] == 150
        rms = rms_db_per_window(path, win_s=1.0)
        assert len(rms) == 5 and rms[4] < -100 and rms[3] < -100, rms


def test_smooth_slowmo_encode_keeps_stretched_audio(tmp_path, media):
    """The RIFE re-encode keeps the source's sound, stretched to the new
    length and still on the picture (it used to encode with -an)."""
    from video_ai_editor.ai.rife import encode_slowmo
    src = tmp_path / "clap4.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(media["clap"]),
                    "-t", "4", "-c", "copy", str(src)], check=True)
    frames = tmp_path / "frames"
    frames.mkdir()
    # Stand-in for RIFE: every source frame twice (frame count ×2, as -n asks).
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-vf", f"setpts=2*PTS,fps={FPS}", str(frames / "f%05d.png")], check=True)
    dst = encode_slowmo(frames, src, tmp_path / "slow.mp4", fps=FPS, factor=2)
    info = probe_video(dst)
    assert info["frames"] == 240
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_type,duration", "-of", "csv=p=0", str(dst)],
                             capture_output=True, text=True, check=True).stdout
    assert "audio" in streams
    offs = av_offsets_ms(dst)
    assert len(offs) >= 3 and max(abs(o) for o in offs[1:]) <= 17.0, offs


# --------------------------------------------------------------- QA-002 import

def test_imported_clip_out_is_the_pictures_frame_exact_length(tmp_path, media, monkeypatch):
    """/upload puts the clip on v1 with out = the VIDEO stream's frame-exact
    length, not format.duration (the AAC-padded container, 20.25 s here)."""
    from collections import OrderedDict
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main, storage

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(main, "_STORES", OrderedDict())
    c = TestClient(main.app)
    sid = c.post("/api/sessions", json={"name": "fc"}).json()["id"]
    with media["fc"].open("rb") as f:
        r = c.post(f"/api/sessions/{sid}/upload",
                   files={"file": ("fc30.mp4", f, "video/mp4")},
                   data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code == 200, r.text
    assert r.json()["duration"] > 20.1          # the container really is padded
    edl = main._store(sid).edl
    clip = edl.get_track("v1").clips[0]
    assert (clip.in_, clip.out) == (0.0, 20.0)
    assert clip.out * edl.canvas.fps == pytest.approx(round(clip.out * edl.canvas.fps), abs=1e-9)


def test_vo_record_start_lands_on_the_frame_grid(tmp_path, monkeypatch):
    """The mic-recording route commits its start like every other edit time:
    snapped to n/fps (1.0137 s → 1.0 s at 30 fps)."""
    from collections import OrderedDict
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main, storage

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(main, "_STORES", OrderedDict())
    wav = tmp_path / "take.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=f=300:sample_rate=48000:duration=1", str(wav)], check=True)
    c = TestClient(main.app)
    sid = c.post("/api/sessions", json={"name": "vo"}).json()["id"]
    with wav.open("rb") as f:
        r = c.post(f"/api/sessions/{sid}/vo_record",
                   files={"file": ("take.wav", f, "audio/wav")},
                   data={"start": "1.0137"})
    assert r.status_code == 200, r.text
    vo = main._store(sid).edl.get_track("vo").clips[0]
    assert vo.start == tb.quantize(1.0137, main._store(sid).edl.canvas.fps) == 1.0
