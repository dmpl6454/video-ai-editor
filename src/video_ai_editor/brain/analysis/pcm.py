"""16 kHz mono float32 PCM from a source via ffmpeg — the one decode the
analysers share. Reads the instant-preview FLAC chunks when `ingest/proxy`
holds them all (the only media the brain should touch on a big project), else
the file itself. Never librosa, never a network."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from ... import platformutil as _pu

SR = 16000
#: A decode above this many seconds is read in windows so peak RSS stays flat.
WINDOW_S = 600.0


def duration_s(path: str | os.PathLike) -> float:
    proc = subprocess.run([_pu.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_pu.SUBPROCESS_FLAGS)
    try:
        return float(proc.stdout.strip() or 0.0)
    except ValueError:
        return 0.0


def stream_kinds(path: str | os.PathLike) -> set[str]:
    """{"audio", "video"} as ffprobe reports them (a still is `video` too)."""
    proc = subprocess.run([_pu.FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
                           "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_pu.SUBPROCESS_FLAGS)
    return {line.strip().strip(",") for line in proc.stdout.splitlines() if line.strip()}


def video_info(path: str | os.PathLike) -> dict | None:
    """`{width, height, fps}` of the first video stream, None without one.
    `fps` is an int when whole (30), else a float rounded to 3 places."""
    proc = subprocess.run([_pu.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_pu.SUBPROCESS_FLAGS)
    parts = proc.stdout.strip().splitlines()[0].split(",") if proc.stdout.strip() else []
    if len(parts) < 3:
        return None
    try:
        num, _, den = parts[2].partition("/")
        fps = float(num) / float(den or 1)
        return {"width": int(parts[0]), "height": int(parts[1]),
                "fps": int(fps) if abs(fps - round(fps)) < 1e-3 else round(fps, 3)}
    except (ValueError, ZeroDivisionError):
        return None


def _decode(args_in: list[str], *, sr: int) -> np.ndarray:
    proc = subprocess.run([_pu.FFMPEG, "-v", "error", "-nostdin", *args_in, "-vn", "-ac", "1",
                           "-ar", str(sr), "-f", "f32le", "-"],
                          capture_output=True, **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"pcm decode failed: {err}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32, copy=False)


def _proxy_chunks(path: str | os.PathLike) -> list[Path] | None:
    """Every FLAC chunk of the source's proxy when all are on disk at unit
    gain (a chunk with a stored gain would need per-chunk scaling — then the
    master is read instead), else None."""
    try:
        from ...ingest import proxy as _proxy
        key = _proxy.proxy_key(path)
        idx = _proxy.read_index(key) or {}
    except Exception:
        return None
    audio = idx.get("audio") or {}
    n = int(audio.get("chunks") or 0)
    if n <= 0 or any(float(g) != 1.0 for g in (audio.get("chunk_gain") or {}).values()):
        return None
    chunks = [_proxy.chunk_path(key, i) for i in range(n)]
    return chunks if all(c.exists() for c in chunks) else None


def _read_via_proxy(chunks: list[Path], *, sr: int) -> np.ndarray:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        for c in chunks:
            fh.write(f"file '{c.as_posix()}'\n")
        listing = fh.name
    try:
        return _decode(["-f", "concat", "-safe", "0", "-i", listing], sr=sr)
    finally:
        try:
            os.unlink(listing)
        except OSError:
            pass


def read_pcm(path: str | os.PathLike, *, t0: float = 0.0, t1: float | None = None, sr: int = SR) -> np.ndarray:
    """Mono float32 PCM of `[t0, t1)` at `sr`. The whole file when `t1` is
    None; an empty array when the file has no decodable audio."""
    path = Path(path)
    if t0 <= 0.0 and t1 is None:
        chunks = _proxy_chunks(path)
        if chunks:
            return _read_via_proxy(chunks, sr=sr)
        total = duration_s(path)
        if total > WINDOW_S:
            parts = [read_pcm(path, t0=s, t1=min(total, s + WINDOW_S), sr=sr)
                     for s in np.arange(0.0, total, WINDOW_S)]
            return np.concatenate(parts) if parts else np.zeros(0, np.float32)
    args = ["-ss", f"{max(0.0, t0):.3f}"]
    if t1 is not None:
        args += ["-to", f"{max(t0, t1):.3f}"]
    try:
        return _decode([*args, "-i", str(path)], sr=sr)
    except RuntimeError:
        if "audio" in stream_kinds(path):
            raise
        return np.zeros(0, np.float32)


def frame_view(pcm: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """(n_frames, frame) strided view without copying; the tail is dropped."""
    if len(pcm) < frame:
        return np.zeros((0, frame), dtype=pcm.dtype)
    n = 1 + (len(pcm) - frame) // hop
    stride = pcm.strides[0]
    return np.lib.stride_tricks.as_strided(pcm, shape=(n, frame), strides=(hop * stride, stride), writeable=False)


__all__ = ["SR", "duration_s", "stream_kinds", "video_info", "read_pcm", "frame_view"]
