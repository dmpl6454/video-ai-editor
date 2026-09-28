"""Splitting a clip with a FILTER voice effect leaves no click at the split
(final QA, round 3).

THE DEFECT: only the pitch and vibrato stages were primed with the sound
before `in` (`audio_mix.voice_prime_s`, review RE). The biquad stages of
Telephone, Radio, Megaphone and Underwater (and Monster's lowpass) restarted
from zero state at every piece's head while the unsplit clip carried its
state straight through: on a steady tone the split was a hard step (11.7x
the largest sample step elsewhere for Telephone, 13x Radio, 18x Underwater)
followed by ~4 ms of filter settling — a click at every split.

THE RULE NOW: a biquad stage is primed too — 50 ms of real input settles
these IIR filters far below the noise floor — so the split clip's sound is
the unsplit clip's around the seam. The client mirrors the rule
(`voiceFx.voicePlan(...).prime`).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.edl.schema import AudioProps, Canvas, Clip, empty_edl
from video_ai_editor.render import audio_mix, compositor


@pytest.fixture(scope="module")
def tone(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("vpf") / "tone.mov"
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=30:d=4",
                    "-f", "lavfi", "-i", "aevalsrc=0.4*sin(2*PI*440*t):s=48000:d=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_f32le", "-shortest", str(p)],
                   check=True, capture_output=True)
    return p


def _sound(clips) -> np.ndarray:
    e = empty_edl(Canvas(w=64, h=36, fps=30))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips = list(clips)
    e.recompute_duration()
    inputs, fc, label = compositor._audio_only_graph(e, fps=30, first_input=0, apply_loudnorm=False)
    p = subprocess.run([_pu.FFMPEG, "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                        "-f", "f32le", "-ac", "2", "-ar", "48000", "-"], capture_output=True, check=True)
    return np.frombuffer(p.stdout, "<f4").reshape(-1, 2)[:, 0].astype(np.float64)


@pytest.mark.parametrize("effect", ["telephone", "radio", "megaphone"])
def test_a_filter_voice_effect_is_primed(effect):
    assert audio_mix.voice_prime_s(AudioProps(voice_effect=effect)) == audio_mix.VOICE_PRIME_S


@pytest.mark.parametrize("effect", ["telephone", "radio", "megaphone"])
def test_a_split_filter_voice_matches_the_unsplit_clip_at_the_seam(tone, effect):
    def mk(cid, start, i, o):
        return Clip(src=str(tone), id=cid, start=start, in_=i, out=o, audio=AudioProps(voice_effect=effect))
    whole = _sound([mk("a", 0.0, 0.3, 3.3)])
    cuts = [0.3, 1.1, 1.9, 2.6, 3.3]
    pieces, t = [], 0.0
    for k, (i, o) in enumerate(zip(cuts, cuts[1:])):
        pieces.append(mk(f"p{k}", t, i, o))
        t += pieces[-1].effective_duration
    split = _sound(pieces)
    n = min(len(whole), len(split))
    steps = np.abs(np.diff(whole[:n]))
    ref = float(np.percentile(steps, 99.9))
    for c in cuts[1:-1]:
        seam = int(round((c - 0.3) * 48000))
        dev = float(np.abs(split[seam - 48:seam + 2400] - whole[seam - 48:seam + 2400]).max())
        jump = float(np.abs(np.diff(split[seam - 2:seam + 3])).max())
        assert dev < 1e-3, f"{effect}: split deviates {dev:.4f} from the unsplit clip at {c} s"
        assert jump <= 1.05 * ref, f"{effect}: a {jump / ref:.1f}x step at the split at {c} s"
