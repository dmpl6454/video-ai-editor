"""EB1-D: per-file sync offsets by numpy FFT cross-correlation (no librosa)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402


@pytest.fixture(scope="module")
def p2():
    return F.p2_or_skip()


def test_p2_offsets_within_10ms(p2):
    from video_ai_editor.brain.analysis import sync
    ref = p2.p2.recorder_wav
    for key in ("cam_a", "cam_b"):
        planted = p2.p2.truth.offsets[key]
        r = sync.sync_member(ref, getattr(p2.p2, key))
        assert abs(r["offset_s"] - planted) <= 0.010, (key, r)
        assert r["confidence"] >= 0.5 and not r["unverified"], r
        assert len(r["anchors"]) >= 3
        assert abs(r["anchors_max_dev_ms"]) <= 40, r


def test_sign_convention_other_t_equals_ref_t_plus_offset():
    """A copy delayed by 0.30 s (its content plays LATER in its own file) has
    offset +0.30: `other_t = ref_t + offset_s`."""
    from video_ai_editor.brain.analysis import sync
    sr = 16000
    rng = np.random.default_rng(3)
    ref = rng.standard_normal(sr * 30).astype(np.float32) * 0.1
    other = np.concatenate([np.zeros(int(0.30 * sr), np.float32), ref])[: len(ref)]
    r = sync.estimate_offset(ref, other, sr=sr)
    assert abs(r["offset_s"] - 0.30) <= 0.002, r
    early = ref[int(0.20 * sr):]
    r2 = sync.estimate_offset(ref, early, sr=sr)
    assert abs(r2["offset_s"] + 0.20) <= 0.002, r2


def test_dead_angle_is_unverified():
    from video_ai_editor.brain.analysis import sync
    sr = 16000
    rng = np.random.default_rng(5)
    ref = rng.standard_normal(sr * 30).astype(np.float32) * 0.1
    dead = rng.standard_normal(sr * 30).astype(np.float32) * 0.001
    r = sync.estimate_offset(ref, dead, sr=sr)
    assert r["unverified"] and r["confidence"] < 0.5, r
    assert r["offset_s"] == 0.0
