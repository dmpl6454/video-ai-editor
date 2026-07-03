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
    )
    return dst


def _key(src: Path) -> str:
    return hashlib.sha256(str(src).encode()).hexdigest()[:14]


def _demucs_separate(audio_path: Path, out_dir: Path) -> dict[str, Path]:
    """Run demucs on a wav and return paths to {vocals, drums, bass, other}.

    Outputs land in `out_dir/<model>/<basename>/<stem>.wav`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "python", "-m", "demucs.separate",
            "-n", "htdemucs",
            "-o", str(out_dir),
            "--filename", "{stem}.{ext}",
            str(audio_path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"demucs failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
    base = audio_path.stem
    sub = out_dir / "htdemucs" / base
    return {
        "vocals":    sub / "vocals.wav",
        "drums":     sub / "drums.wav",
        "bass":      sub / "bass.wav",
        "other":     sub / "other.wav",
    }


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
    )
    return dst


def isolate_vocals(src: Path, cache_dir: Path) -> Path:
    stems = _ensure_stems(src, cache_dir)
    return stems["vocals"]


def isolate_instrumental(src: Path, cache_dir: Path) -> Path:
    stems = _ensure_stems(src, cache_dir)
    out = cache_dir / f"instrumental_{_key(src)}.wav"
    return _mix([stems["drums"], stems["bass"], stems["other"]], out)
