"""QA-005 (partial): an audio-only edit's preview is a fast remux.

A volume/fade/duck change never touches the picture, so render_preview remuxes
the cached video-only mp4 against a fresh sound mix (`_remux_with_new_audio`).
That path's ONLY encode is the timeline's AAC — and it used ffmpeg's native
AAC coder, the slow one (~0.8 s per minute of sound here: 10.4 s for a volume
change on a 12-min timeline), while the preview's other audio-only encode (the
chunk streamcopy assembly) already used the fastest working coder.

Measured on real renders:
  * the remux takes well under the time a native-AAC encode of the same sound
    takes on this machine, measured back to back (a ratio, so machine load
    affects both sides);
  * the remuxed preview is still sample-aligned with its source and carries
    the new gain (the encoder swap must not shift or change the sound).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl.schema import EDL, AudioProps, Canvas, Clip, Track
from video_ai_editor.render import compositor as C
from video_ai_editor.render import render_preview

FPS = 30
DUR = 180.0
SR = 48000


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    """Tiny picture, 3 minutes of a CHIRP (non-periodic, so a lag is
    unambiguous; tonal, so AAC reproduces it closely)."""
    p = tmp_path_factory.mktemp("remux") / "chirp.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=160x90:r={FPS}:d={DUR}",
         "-f", "lavfi", "-i",
         f"aevalsrc='0.4*sin(2*PI*(220*t+2*t*t))':s={SR}:c=stereo:d={DUR}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "pcm_s16le", "-shortest", str(p.with_suffix(".mov"))],
        check=True, capture_output=True)
    return p.with_suffix(".mov")


def _edl(src: Path, gain_db: float) -> EDL:
    c = Clip(id="c0", src=str(src), in_=0.0, out=DUR, start=0.0,
             audio=AudioProps(gain_db=gain_db))
    e = EDL(canvas=Canvas(w=160, h=90, fps=FPS), tracks=[Track(id="v1", type="video", clips=[c])])
    e.recompute_duration()
    return e


def _native_aac_s(src: Path, out: Path) -> float:
    """The encode the remux used to pay for: this sound through native AAC."""
    t = time.perf_counter()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", *C._AAC_OUT, str(out)],
                   check=True, capture_output=True)
    return time.perf_counter() - t


def _pcm(p: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "f32le", "-ac", "1",
                          "-ar", str(SR), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def test_audio_only_edit_remuxes_faster_than_a_native_aac_encode(src, tmp_path, monkeypatch):
    sess = tmp_path / "s"
    sess.mkdir()
    render_preview(_edl(src, 0.0), sess)                     # cold: caches the video-only mp4

    remuxes: list[float] = []
    real = C._remux_with_new_audio

    def spy(*a, **kw):
        t = time.perf_counter()
        real(*a, **kw)
        remuxes.append(time.perf_counter() - t)

    monkeypatch.setattr(C, "_remux_with_new_audio", spy)
    native: list[float] = []
    last = None
    for k, gain in enumerate((-6.0, -7.0)):
        last = render_preview(_edl(src, gain), sess)
        native.append(_native_aac_s(src, tmp_path / f"native{k}.m4a"))
    assert len(remuxes) == 2, "the volume edit did not take the audio-only remux path"
    # Before: the remux WAS a native AAC encode plus decode/mux (ratio >= 1).
    ratio = min(remuxes) / min(native)
    assert ratio < 0.75, (remuxes, native)

    # Still the same sound, at the new gain, sample-aligned with the source.
    got, ref = _pcm(last.path), _pcm(src) * (10 ** (-7.0 / 20))
    for t in (1.0, 45.0, 90.5, 170.0):
        i, n = int(t * SR), 4800
        w = got[i:i + n]
        lag = min(range(-96, 97), key=lambda k: float(np.sum((w - ref[i + k:i + k + n]) ** 2)))
        err = float(np.sqrt(np.mean((w - ref[i + lag:i + lag + n]) ** 2))
                    / (np.sqrt(np.mean(ref[i:i + n] ** 2)) + 1e-12))
        assert lag == 0 and err < 0.05, (t, lag, err)
