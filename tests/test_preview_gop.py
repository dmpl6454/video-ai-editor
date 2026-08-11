"""The preview GOP bound must reach EVERY encoder, not just libx264.

The frontend scrubber decodes from the nearest prior keyframe on each paused
playhead-drag tick, so the average cost of a tick is GOP/2 frames of H.264
decode. That makes the keyframe interval the single number controlling how
smooth scrubbing feels.

The bound existed for a long time — but only on the libx264 FALLBACK, guarded
by a comment asserting "HW encoders already emit ~0.4-1s GOPs". That was never
measured and is false: h264_qsv produced a 60-frame (2s) GOP, so on every
machine with a usable GPU — the fast path the encoder ladder exists to prefer —
the mitigation did not apply at all. Measured on the frame-numbered ramp
fixture, a 6-second playhead drag painted 12.4 fps with 301ms worst-case gaps.

These tests pin the args rather than the rendered file: the defect was an
argument never being passed, and an args test states that directly and runs
everywhere, including on runners with no GPU at all.
"""
import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.render import compositor as comp
from video_ai_editor.edl import schema as sch


def _gop(args: list[str]) -> str | None:
    return args[args.index("-g") + 1] if "-g" in args else None


@pytest.mark.parametrize("name", comp._HW_ENCODER_ORDER)
def test_every_hw_encoder_gets_the_preview_gop(monkeypatch, name):
    """Whichever encoder the probe ladder picks, preview must carry -g."""
    monkeypatch.setattr(comp, "_usable_encoder", lambda n, _n=name: n == _n)
    args = comp._video_encoder_args(preview=True)
    assert "-c:v" in args and name in args, f"expected the ladder to pick {name}: {args}"
    assert _gop(args) == str(comp._PREVIEW_GOP), (
        f"{name} preview args carry no/short -g: {args}")


def test_libx264_fallback_gets_it_too(monkeypatch):
    monkeypatch.setattr(comp, "_usable_encoder", lambda _n: False)
    args = comp._video_encoder_args(preview=True)
    assert "libx264" in args
    assert _gop(args) == str(comp._PREVIEW_GOP)


@pytest.mark.parametrize("name", comp._HW_ENCODER_ORDER)
def test_export_is_left_alone(monkeypatch, name):
    """Long GOPs are the right size trade for a delivered file, and nothing
    scrubs an export inside the app."""
    monkeypatch.setattr(comp, "_usable_encoder", lambda n, _n=name: n == _n)
    assert _gop(comp._video_encoder_args(preview=False)) is None


def test_export_libx264_is_left_alone(monkeypatch):
    monkeypatch.setattr(comp, "_usable_encoder", lambda _n: False)
    assert _gop(comp._video_encoder_args(preview=False)) is None


def test_the_gop_is_short_enough_to_matter():
    """A guard on the VALUE, not just its presence. At 30fps a 15-frame GOP is
    half a second, so a drag tick averages ~7 frames of decode instead of ~30.
    Raising this materially is the regression this file exists to catch."""
    assert 1 <= comp._PREVIEW_GOP <= 30


def test_behavior_version_was_bumped_for_it():
    """The preview cache is keyed on RENDER_BEHAVIOR_VERSION. Without a bump,
    every existing session keeps being served its long-GOP preview and the fix
    reads as having done nothing — the exact failure mode the salt exists for."""
    assert sch.RENDER_BEHAVIOR_VERSION >= 9


@pytest.mark.skipif(not comp._usable_encoder("h264_qsv")
                    and not comp._usable_encoder("h264_nvenc")
                    and not comp._usable_encoder("h264_amf")
                    and not comp._usable_encoder("h264_videotoolbox"),
                    reason="no hardware encoder on this machine")
def test_a_real_hw_preview_encode_honours_it(tmp_path):
    """End-to-end on whatever GPU this machine has: the emitted file's keyframe
    spacing must actually follow the flag. An args test cannot prove the
    encoder obeys -g, and this defect was precisely an encoder not doing what
    the code assumed."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:d=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True)
    out = tmp_path / "preview.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), *comp._video_encoder_args(preview=True), str(out)],
        check=True, capture_output=True)
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=key_frame", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True, encoding="utf-8").stdout
    flags = [f for f in re.split(r"[,\s]+", frames.strip()) if f in ("0", "1")]
    idx = [i for i, f in enumerate(flags) if f == "1"]
    assert len(idx) >= 2, f"only {len(idx)} keyframes in {len(flags)} frames"
    spacing = max(b - a for a, b in zip(idx, idx[1:]))
    assert spacing <= comp._PREVIEW_GOP + 1, (
        f"keyframe spacing {spacing} exceeds the requested {comp._PREVIEW_GOP}")
