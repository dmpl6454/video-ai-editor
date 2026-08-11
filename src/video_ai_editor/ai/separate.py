"""Demucs vocal/instrumental separation.

Splits an audio (or video) source into stems. We expose two entry points:
- isolate_vocals(src) → path to a WAV containing only the vocal stem
- isolate_instrumental(src) → path to a WAV containing the rest (no vocals)

Demucs is heavyweight (PyTorch) so we only import it lazily on first call.
The htdemucs model downloads on first use (~80MB).
"""
from __future__ import annotations
import hashlib
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .. import platformutil as _pu


def _audio_extract(src: Path, dst: Path) -> Path:
    """Pull the audio out of a video into a WAV (cached)."""
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(src), "-vn", "-acodec", "pcm_s16le",
         "-ar", "44100", "-ac", "2", str(dst)],
        capture_output=True, check=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    return dst


def _key(src: Path) -> str:
    return hashlib.sha256(str(src).encode()).hexdigest()[:14]


def available() -> bool:
    """True when demucs can actually run here.

    Mirrors bgremove.available() / upscale.available(). This module had NO
    availability probe, so `vocal_isolate`/`instrumental_isolate` were advertised
    to Claude with no gate at all and failed deep inside a subprocess instead of
    returning a clean "feature not installed".

    Checks soundfile and torch too, because separation now decodes the WAV
    itself (see _demucs_separate) — a probe that only saw `demucs` would report
    the feature as ready and then fail at run time.
    """
    if getattr(sys, "frozen", False):
        # The packaged app deliberately EXCLUDES torch/demucs to stay ~150MB
        # (see CLAUDE.md Packaging), so there is no interpreter to run it with.
        return False
    try:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None
                   for m in ("demucs", "torch", "soundfile"))
    except (ImportError, ValueError):
        return False


@lru_cache(maxsize=1)
def _load_model():
    """The htdemucs model, kept for the process (~80MB, downloads on first use)."""
    from demucs.pretrained import get_model
    model = get_model("htdemucs")
    model.eval()
    return model


def _demucs_separate(audio_path: Path, out_dir: Path) -> dict[str, Path]:
    """Separate a WAV into {vocals, drums, bass, other} under out_dir/htdemucs/.

    Runs demucs through its PYTHON API on audio we decode ourselves, rather than
    shelling out to `python -m demucs.separate`.

    The CLI is not usable on a normal Windows install: it loads audio through
    torchaudio → torchcodec, whose `libtorchcodec_core*.dll` links against
    FFmpeg's SHARED libraries. Windows users install ffmpeg as a static
    `ffmpeg.exe` (the winget Gyan build this app documents), so the DLL fails to
    load with `FileNotFoundError: Could not find module … libtorchcodec_core4.dll`
    and demucs exits 1 — reported to the user as "vocal isolation is not
    installed" when demucs, torch and the model were all present and fine.

    We already hand it a decoded 44.1k stereo WAV (_audio_extract), so the whole
    torchcodec decode path was never needed. soundfile reads it, and this also
    drops an interpreter start-up and keeps the model resident between calls.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "Stem separation needs the full Python install (torch + demucs are "
            "deliberately excluded from the packaged app). Run "
            "`uv run video-ai-editor` instead.")
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model

    model = _load_model()
    data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)  # (n, ch)
    if sr != model.samplerate:
        # _audio_extract writes model.samplerate already; re-extract rather than
        # resample here so there is exactly one place that owns the decode.
        raise RuntimeError(
            f"expected {model.samplerate} Hz audio for demucs, got {sr}")
    if data.shape[1] == 1 and model.audio_channels == 2:
        data = np.repeat(data, 2, axis=1)
    wav = torch.from_numpy(data.T).contiguous()                # (ch, n)

    # Demucs normalises by the mixture's own statistics and un-normalises the
    # stems afterwards; skipping this measurably degrades separation.
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        sources = apply_model(model, ((wav - mean) / std)[None],
                              device="cpu", progress=False)[0]
    sources = sources * std + mean

    flat = out_dir / "htdemucs"
    flat.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, source in zip(model.sources, sources):
        dst = flat / f"{name}.wav"
        sf.write(str(dst), source.T.numpy(), sr)
        written[name] = dst
    missing = {"vocals", "drums", "bass", "other"} - set(written)
    if missing:
        raise RuntimeError(f"demucs produced no {'/'.join(sorted(missing))} stem")
    return written


def _ensure_stems(src: Path, cache_dir: Path) -> dict[str, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _key(src)
    audio_wav = cache_dir / f"src_{key}.wav"
    _audio_extract(src, audio_wav)
    out_dir = cache_dir / f"stems_{key}"
    # `--filename {stem}.{ext}` makes demucs drop stems directly under
    # out_dir/htdemucs/ (no per-track subfolder).
    flat = out_dir / "htdemucs"
    if not (flat / "vocals.wav").exists():
        _demucs_separate(audio_wav, out_dir)
    return {
        "vocals":    flat / "vocals.wav",
        "drums":     flat / "drums.wav",
        "bass":      flat / "bass.wav",
        "other":     flat / "other.wav",
    }


def _mix(stems: list[Path], dst: Path) -> Path:
    """Mix multiple stem WAVs to a single WAV via ffmpeg amix."""
    if dst.exists():
        return dst
    inputs: list[str] = []
    for s in stems:
        inputs += ["-i", str(s)]
    fc = "".join(f"[{i}:a]" for i in range(len(stems))) + f"amix=inputs={len(stems)}:normalize=0[out]"
    subprocess.run(
        [_pu.FFMPEG, "-y", *inputs, "-filter_complex", fc, "-map", "[out]",
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, check=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    return dst


def isolate_vocals(src: Path, cache_dir: Path) -> Path:
    stems = _ensure_stems(src, cache_dir)
    return stems["vocals"]


def isolate_instrumental(src: Path, cache_dir: Path) -> Path:
    stems = _ensure_stems(src, cache_dir)
    out = cache_dir / f"instrumental_{_key(src)}.wav"
    return _mix([stems["drums"], stems["bass"], stems["other"]], out)
