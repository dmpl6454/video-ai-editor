"""apply_lut intensity + effect-chain label uniqueness.

The old _lut() validated and stored `intensity` and then returned the identical
full-strength `lut3d=` string on both branches — a LUT at intensity 0.2
rendered at 1.0 with zero signal anywhere. These tests pin the split+blend
implementation, the graph-unique labels (fixed labels like `[a]` broke any
monolithic graph with the same effect on two clips), and — with a real ffmpeg
render — that mid-intensity output actually lands between none and full.
"""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import Effect
from video_ai_editor.render.effects import effect_chain


LUT = {"src": "/tmp/some.cube"}


def test_full_intensity_is_plain_lut3d():
    chain = effect_chain([Effect(type="lut", params={**LUT, "intensity": 1.0})], uid="c_a")
    assert chain == "lut3d=/tmp/some.cube"


def test_partial_intensity_emits_split_blend():
    chain = effect_chain([Effect(type="lut", params={**LUT, "intensity": 0.4})], uid="c_a")
    assert "split=2" in chain
    assert "lut3d=/tmp/some.cube" in chain
    assert "blend=all_mode=normal:all_opacity=0.400" in chain


def test_zero_intensity_is_noop():
    assert effect_chain([Effect(type="lut", params={**LUT, "intensity": 0.0})], uid="c_a") == ""


def test_labels_unique_across_clips_and_effects():
    """Same effect on two clips (or twice on one clip) must never emit the
    same link label — ffmpeg labels are global to the whole filter_complex."""
    lut = Effect(type="lut", params={**LUT, "intensity": 0.5})
    glow = Effect(type="glow", params={})
    one = effect_chain([lut, glow], uid="c_one")
    two = effect_chain([lut, glow], uid="c_two")
    labels_one = set(re.findall(r"\[[^\]]+\]", one))
    labels_two = set(re.findall(r"\[[^\]]+\]", two))
    assert labels_one and labels_two
    assert not labels_one & labels_two
    # and two same-type effects within ONE clip don't collide either: every
    # distinct label appears exactly twice (once defined, once consumed) —
    # a third occurrence would mean two branches share a label.
    both = effect_chain([lut, Effect(type="lut", params={**LUT, "intensity": 0.7})], uid="c_x")
    labels = re.findall(r"\[[^\]]+\]", both)
    from collections import Counter
    assert all(n == 2 for n in Counter(labels).values()), Counter(labels)


@pytest.fixture
def lut_file(tmp_path: Path) -> Path:
    """A small red-boost .cube (the same shape the bundled presets use)."""
    n = 2
    rows = [f"LUT_3D_SIZE {n}"]
    for b in range(n):
        for g in range(n):
            for r in range(n):
                rows.append(f"{min(1.0, r + 0.25):.6f} {float(g):.6f} {float(b):.6f}")
    p = tmp_path / "boost.cube"
    p.write_text("\n".join(rows) + "\n")
    return p


def _mean_red(video: Path, tmp_path: Path, tag: str) -> float:
    frame = tmp_path / f"{tag}.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", "0.2",
                    "-i", str(video), "-frames:v", "1", str(frame)],
                   check=True)
    from PIL import Image
    import numpy as np
    return float(np.asarray(Image.open(frame).convert("RGB"))[:, :, 0].mean())


def test_intensity_actually_blends_pixels(tmp_path: Path, lut_file: Path):
    """Render the same frame at intensity 0 / 0.5 / 1.0 — the 0.5 red mean must
    sit strictly between the other two (the exact bug: it used to equal 1.0)."""
    reds = {}
    for intensity in (None, 0.5, 1.0):
        vf = "hue=s=1.05"
        if intensity is not None:
            vf += "," + effect_chain(
                [Effect(type="lut", params={"src": str(lut_file), "intensity": intensity})],
                uid="c_t")
        out = tmp_path / f"i{intensity}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=160x120:d=0.5:r=30",
                        "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", str(out)],
                       check=True)
        reds[intensity] = _mean_red(out, tmp_path, f"f{intensity}")
    assert reds[None] < reds[0.5] < reds[1.0], reds
    # near the midpoint, not just barely different
    mid = (reds[None] + reds[1.0]) / 2
    assert abs(reds[0.5] - mid) < 10, reds


def test_intensity_direction_not_inverted(tmp_path: Path, lut_file: Path):
    """A regression pin for a real bug: an earlier version of _lut listed the
    blend operands in the wrong order, so `intensity` weighted the ORIGINAL
    frame instead of the LUT'd one (e.g. intensity=0.9 rendered ~90%
    original). Intensity=0.5 alone can't catch this — swapping symmetric
    operands at the midpoint is a no-op — so this pins asymmetric values."""
    reds = {}
    for intensity in (None, 0.15, 0.85, 1.0):
        vf = "hue=s=1.05"
        if intensity is not None:
            vf += "," + effect_chain(
                [Effect(type="lut", params={"src": str(lut_file), "intensity": intensity})],
                uid="c_t")
        out = tmp_path / f"dir{intensity}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=160x120:d=0.5:r=30",
                        "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", str(out)],
                       check=True)
        reds[intensity] = _mean_red(out, tmp_path, f"dir{intensity}")
    # monotonic: more intensity -> more LUT -> more red (this LUT boosts red)
    assert reds[None] < reds[0.15] < reds[0.85] < reds[1.0], reds
    # a HIGH intensity must land closer to the FULL-lut value than to the
    # untouched original — the inverted version instead landed close to
    # `reds[None]` at intensity=0.85.
    assert abs(reds[0.85] - reds[1.0]) < abs(reds[0.85] - reds[None]), reds
    # symmetric check in the other direction
    assert abs(reds[0.15] - reds[None]) < abs(reds[0.15] - reds[1.0]), reds
