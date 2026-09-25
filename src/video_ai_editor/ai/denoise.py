"""Spectral noise reduction for clip audio.

Uses `noisereduce` (a stationary-noise spectral-gate method that's good for
constant background hiss / fans / room tone). For speech-only clips this is
a sweet spot — fast, no model download, runs on CPU.

Output: new audio-replaced video at `cache/denoise/<hash>.mp4`. Original
video stream is `-c:v copy`'d so this is fast to chain into renders.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import platformutil as _pu


def available() -> bool:
    try:
        import importlib
        importlib.import_module("noisereduce")
        importlib.import_module("soundfile")
        return True
    except ImportError:
        return False


def _has_video_stream(src: Path) -> bool:
    """True when `src` carries a real video stream (not just cover art).

    `attached_pic` is excluded on purpose: an MP3 with embedded album art DOES
    report a video stream, and treating it as a video would put us straight back
    into the failing `-map 0:v -c:v copy` path this guard exists to avoid.
    """
    try:
        proc = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_pu.SUBPROCESS_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (proc.stdout or "").splitlines():
        parts = [p for p in line.strip().split(",") if p != ""]
        if not parts or parts[0] != "video":
            continue
        if len(parts) > 1 and parts[1] == "1":
            continue  # attached_pic → cover art, not video
        return True
    return False


#: Bumped whenever the DSP changes what the same input produces, so a cached
#: output from the old algorithm (mono, 10 dB quieter — QA-028) is never reused.
_DSP_VERSION = "dsp2"
#: Analysis frame for the noise-profile / level measurements.
_FRAME_S = 0.05
#: Frames at or below this energy percentile are treated as noise-only.
_NOISE_PERCENTILE = 15.0
#: Frames above this percentile are the programme (speech) the gain match
#: keeps at its original level.
_ACTIVE_PERCENTILE = 60.0
#: The programme must sit at least this far above the quiet frames for them to
#: count as a measurable noise floor.
_MIN_SEPARATION_DB = 10.0
#: Never add more than this much make-up gain (a near-silent clip must not be
#: pumped up to meet a meaningless "speech" level).
_MAX_MAKEUP_DB = 12.0


def _frame_rms(x, n: int):
    import numpy as np
    usable = (len(x) // n) * n
    if usable == 0:
        return np.array([float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0])
    frames = x[:usable].reshape(-1, n)
    return np.sqrt(np.mean(np.square(frames), axis=1))


def _noise_sample(x, sr: int):
    """The quietest frames of `x`, concatenated: the noise profile.

    QA-028: `reduce_noise(stationary=True)` with no `y_noise` estimates the
    noise from the WHOLE signal — speech included — so its gate threshold sat
    near the speech level and cut dialogue by 10-11 dB. The quietest ~15 % of
    50 ms frames of a speech clip are the pauses between words, i.e. the room
    tone / hiss the user wants gone."""
    import numpy as np
    n = max(1, int(sr * _FRAME_S))
    rms = _frame_rms(x, n)
    if len(rms) < 4:
        return x
    cut = np.percentile(rms, _NOISE_PERCENTILE)
    idx = np.nonzero(rms <= cut)[0]
    return np.concatenate([x[i * n:(i + 1) * n] for i in idx]) if len(idx) else x


def _active_level(x, sr: int, ref_mask=None):
    """RMS of the loud (programme) frames; `ref_mask` reuses the INPUT's frame
    selection so before/after compare the same instants."""
    import numpy as np
    n = max(1, int(sr * _FRAME_S))
    rms = _frame_rms(x, n)
    if ref_mask is None:
        ref_mask = rms >= np.percentile(rms, _ACTIVE_PERCENTILE)
    sel = rms[ref_mask[:len(rms)]] if len(rms) else rms
    return (float(np.sqrt(np.mean(np.square(sel)))) if len(sel) else 0.0), ref_mask


def _clean_channel(x, sr: int, strength: float):
    """Denoise ONE channel against its own noise profile, then gain-match its
    programme level back to the input's (QA-028: the output used to be
    ~9.5 LU quieter than what went in)."""
    import numpy as np
    import noisereduce as nr  # type: ignore
    x = x.astype(np.float32)
    noise = _noise_sample(x, sr)
    before, mask = _active_level(x, sr)
    noise_rms = float(np.sqrt(np.mean(np.square(noise)))) if len(noise) else 0.0
    if before <= 1e-6 or noise_rms <= 1e-9 or \
            20 * np.log10(before / noise_rms) < _MIN_SEPARATION_DB:
        # No quiet frames distinct from the programme (a steady tone, wall-to-
        # wall music, digital silence): there is no noise floor to measure, and
        # gating against the programme itself is exactly the old bug. Leave
        # this channel untouched.
        return x
    clean = nr.reduce_noise(y=x, sr=sr, stationary=True, y_noise=noise,
                            prop_decrease=max(0.0, min(1.0, float(strength))))
    clean = np.asarray(clean, dtype=np.float32)[: len(x)]
    after, _ = _active_level(clean, sr, mask)
    if before > 1e-6 and after > 1e-6:
        gain = min(before / after, 10 ** (_MAX_MAKEUP_DB / 20))
        clean = clean * np.float32(gain)
    return clean


def denoise_clip(src: Path, cache_dir: Path, *,
                 strength: float = 0.85, sample_rate: int = 48000) -> Path:
    """Return a new mp4 with the audio track noise-reduced.

    `strength` ∈ [0,1]: higher = more aggressive (with diminishing returns and
    growing artifacts above ~0.9). Default 0.85 is the speech sweet spot.

    Level- and channel-preserving (QA-028): every channel is processed on its
    own against a noise profile taken from the quiet frames, and the result is
    gain-matched so speech leaves at the level it arrived at. It used to fold
    stereo to mono (`-ac 1` + a channel average) and estimate the noise from
    speech as well, costing 10-11 dB of dialogue.
    """
    if not available():
        raise RuntimeError("noisereduce not installed (uv add noisereduce soundfile)")

    cache_dir.mkdir(parents=True, exist_ok=True)
    # An audio-only source (an MP3 on the music lane) has no video stream to copy,
    # so it gets an .m4a container and a mux that never mentions `0:v`. Without
    # this branch the mux below asked for `-map 0:v`, ffmpeg answered
    # "Stream map '' matches no streams", and the user saw "ffmpeg mux failed" —
    # noise_reduce worked on a video clip's audio but always failed on music.
    has_video = _has_video_stream(src)
    ext = "mp4" if has_video else "m4a"
    # `has_video`/ext are part of the cache key so a path cached under the old
    # shape can never be served for the new one.
    h = hashlib.sha256(
        f"{src}|{strength}|{sample_rate}|{src.stat().st_mtime}|{ext}|{_DSP_VERSION}".encode()
    ).hexdigest()[:14]
    dst = cache_dir / f"denoise_{h}.{ext}"
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    import soundfile as sf  # type: ignore
    import numpy as np

    with tempfile.TemporaryDirectory() as td:
        wav_in = Path(td) / "in.wav"
        wav_out = Path(td) / "out.wav"
        # Float wav at the source's own channel count: no downmix, no clipping
        # headroom lost to 16-bit.
        proc = subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(src),
             "-vn", "-ar", str(sample_rate),
             "-c:a", "pcm_f32le", str(wav_in)],
            # text= is required for the error string below to be readable — without
            # it stderr is bytes and the message interpolated a b"..." repr. The
            # encoding/errors kwargs are mandatory alongside text on Windows
            # (cp1252-strict would raise on a Devanagari path). See CLAUDE.md.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg audio extract failed: {(proc.stderr or '')[-500:]}")
        if not wav_in.exists() or wav_in.stat().st_size < 100:
            raise RuntimeError("source has no usable audio track")

        data, sr = sf.read(str(wav_in), dtype="float32", always_2d=True)
        clean = np.stack([_clean_channel(data[:, ch], sr, strength)
                          for ch in range(data.shape[1])], axis=1)
        peak = float(np.max(np.abs(clean))) if clean.size else 0.0
        if peak > 0.999:
            # Make-up gain must not clip; trade a fraction of a dB instead.
            clean = clean * np.float32(0.999 / peak)
        sf.write(str(wav_out), clean, sr, subtype="FLOAT")

        # Mux back. With video: keep the original video, swap in cleaned audio.
        # Without video: just encode the cleaned wav — the source isn't an input
        # at all, so there is no `0:v` to fail to match.
        if has_video:
            mux = [_pu.FFMPEG, "-y", "-i", str(src), "-i", str(wav_out),
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dst)]
        else:
            mux = [_pu.FFMPEG, "-y", "-i", str(wav_out),
                   "-c:a", "aac", "-b:a", "192k", str(dst)]
        proc = subprocess.run(
            mux, capture_output=True, text=True, encoding="utf-8",
            errors="replace", **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed: {(proc.stderr or '')[-500:]}")
    return dst
