"""Beat detection — librosa-backed, hot-cached for cheap repeats.

Used by the `auto_cut_to_beats` dispatch tool, by style_match, and exposed
publicly so anyone can ask "where are the beats in this audio?" without
re-implementing the librosa boilerplate.
"""
from __future__ import annotations
import hashlib
import json
import subprocess
from pathlib import Path

from .. import platformutil as _pu


def detect_beats(src: Path, *, sr: int = 22050) -> list[float]:
    """Return beat onsets in seconds. Cached at `<user cache dir>/beats_<hash>.json`.

    `src` may be a video or audio file; we extract the audio first.
    """
    _legacy_root = Path.home() / ".cache" / "video-ai-editor" / "beats"
    cache_root = _legacy_root if _legacy_root.exists() else \
        _pu.user_cache_dir("Video AI Editor") / "beats"
    cache_root.mkdir(parents=True, exist_ok=True)
    sig = hashlib.sha256(
        f"{src.resolve()}|{src.stat().st_mtime}|{sr}".encode()
    ).hexdigest()[:14]
    cache = cache_root / f"beats_{sig}.json"
    if cache.exists():
        try:
            return list(json.loads(cache.read_text(encoding="utf-8")))
        except Exception:
            pass

    # librosa wants a wav-like input; let ffmpeg normalise.
    import tempfile
    import librosa  # type: ignore
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "in.wav"
        proc = subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", str(sr),
             "-c:a", "pcm_s16le", str(wav)],
            capture_output=True,
            **_pu.SUBPROCESS_FLAGS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"detect_beats: ffmpeg audio extract failed: "
                f"{proc.stderr[-500:].decode(errors='replace')}"
            )
        y, sr_loaded = librosa.load(str(wav), sr=sr, mono=True)
    _, beat_frames = librosa.beat.beat_track(y=y, sr=sr_loaded)
    times = librosa.frames_to_time(beat_frames, sr=sr_loaded).tolist()
    cache.write_text(json.dumps(times), encoding="utf-8")
    return times
