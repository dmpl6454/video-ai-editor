"""Regression test for a critical input-index bug introduced while fixing
TextClip.anim_in/anim_out: an animated text/sticker overlay's ffmpeg input is
10 args (-itsoffset X -loop 1 -framerate 30 -t D -i path), not the 2-arg
("-i", path) shape of a static overlay. compositor.py counted overlay inputs
as `len(txt_inputs) // 2`, so every animated item was under-counted as "2.5
inputs" — silently shifting the index of every subsequent -i (PIP audio,
music, vo) and breaking the whole downstream audio mix the moment ANY text
clip used an anim preset (found live: ffmpeg "Invalid file index"/"matches no
streams" whenever an anim clip coexisted with a music/vo clip).

A second, pre-existing bug in the same index-math family: PIP audio labels
were derived as `pre_pip = next_idx - pip_inputs_count` AFTER next_idx had
already been advanced by the (separate) text-overlay block — so a PIP clip
plus any baked text/sticker together shifted the PIP audio index too.

These are full-render (real ffmpeg) tests, not unit tests on the string
builders, because the bug is specifically about the caller's bookkeeping
across multiple filter-graph builders — a passing per-builder unit test
would not have caught it.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render.compositor import render_export


def _make_clip(tmp_path: Path, name: str, color: str, tone: int = 300) -> Path:
    p = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d=4:r=30",
         "-f", "lavfi", "-i", f"sine=f={tone}:duration=4",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)],
        check=True)
    return p


def _has_audio_stream(path: Path) -> bool:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return probe.stdout.strip() == "audio"


def test_anim_text_plus_music_export_has_audio(tmp_path: Path):
    """The exact failure scenario: one animated text clip + one music clip.
    Before the fix this raised RuntimeError from a bad ffmpeg filtergraph."""
    src = _make_clip(tmp_path, "v1.mp4", "0x202020")
    music = tmp_path / "music.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "sine=f=200:duration=4",
                    "-c:a", "mp3", str(music)], check=True)

    store = EDLStore(tmp_path / "sess")
    store.edl.get_track("v1").clips.append(Clip(src=str(src), in_=0, out=4, start=0))
    store.edl.get_track("music").clips.append(Clip(src=str(music), in_=0, out=4, start=0))
    store.edl.recompute_duration()
    dispatch(store, "add_text", {"text": "POP", "start": 1.0, "end": 3.0,
                                 "role": "hook", "anim_in": "pop", "anim_out": "fade"})

    res = render_export(store.edl, store.dir, height=240)
    assert _has_audio_stream(Path(res.path))


def test_pip_plus_text_export_has_audio(tmp_path: Path):
    """PIP clip's audio index must not shift when a text overlay is also
    baked — this is the pre-existing pre_pip-derived-too-late bug."""
    v1 = _make_clip(tmp_path, "v1.mp4", "0x202020")
    v2 = _make_clip(tmp_path, "v2.mp4", "0x808080", tone=500)

    store = EDLStore(tmp_path / "sess")
    store.edl.get_track("v1").clips.append(Clip(src=str(v1), in_=0, out=4, start=0))
    store.edl.get_track("v2").clips.append(Clip(src=str(v2), in_=0, out=2, start=0))
    store.edl.recompute_duration()
    dispatch(store, "add_text", {"text": "HI", "start": 0.5, "end": 2.0, "role": "hook"})

    res = render_export(store.edl, store.dir, height=240)
    assert _has_audio_stream(Path(res.path))


def test_pip_audio_plus_anim_text_plus_music(tmp_path: Path):
    """All three input-producing blocks (PIP, animated overlay, music/vo
    mixer) together — the maximal stress case for the index bookkeeping."""
    v1 = _make_clip(tmp_path, "v1.mp4", "0x202020")
    v2 = _make_clip(tmp_path, "v2.mp4", "0x808080", tone=500)
    music = tmp_path / "music.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "sine=f=200:duration=4",
                    "-c:a", "mp3", str(music)], check=True)

    store = EDLStore(tmp_path / "sess")
    store.edl.get_track("v1").clips.append(Clip(src=str(v1), in_=0, out=4, start=0))
    store.edl.get_track("v2").clips.append(Clip(src=str(v2), in_=0, out=2, start=0))
    store.edl.get_track("music").clips.append(Clip(src=str(music), in_=0, out=4, start=0))
    store.edl.recompute_duration()
    dispatch(store, "add_text", {"text": "POP", "start": 1.0, "end": 3.0,
                                 "role": "hook", "anim_in": "pop"})

    res = render_export(store.edl, store.dir, height=240)
    assert _has_audio_stream(Path(res.path))
