"""One small render per prompt run, for the checks that need pixels or a mix
(spec §4.4): `silence_total_leq` and `loudness_within`.

WHY a separate entry point and not `render_preview`: the preview path skips
loudnorm on purpose (Safari and 96 kHz AAC — see audio_mix.mix_audio), so a
preview cannot answer "did the export hit −16 LUFS". This renders at 360p,
`preview=False` (loudnorm on, the export's own audio path), CRF 30, through
`compositor._render` — the single chokepoint that holds `_RENDER_SLOTS`, so
it cannot run beside a user export and it never competes with the preview
render the `op` event triggers, because the executor emits `op` only after
verification (§4.1).

Skipped (returns None) when the timeline is longer than
`VERIFY_RENDER_MAX_DURATION_S`; the render-based checks then report
`pass=None` and say why. Cached by EDL hash under `<session>/cache/verify/`
so a second run on an unchanged tree costs nothing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .. import platformutil as _pu
from ..edl.schema import EDL

VERIFY_HEIGHT = 360
VERIFY_CRF = 30

_LUFS_RE = re.compile(r"I:\s*(-?[\d.]+)\s*LUFS")
_SILENCE_DUR_RE = re.compile(r"silence_duration:\s*([\d.]+)")


def speech_only(edl: EDL) -> EDL:
    """A copy of `edl` with the music track emptied — what `silence_total_leq`
    measures on. A bed at −14 dB hides every pause from silencedetect, so a
    mix render answers "is there silence under the music", not "is there
    silence in the speech". Same hash discipline as the main render: the copy
    hashes differently, so it is cached separately."""
    copy = edl.model_copy(deep=True)
    for t in copy.tracks:
        if t.id == "music" or t.type == "music":
            t.clips = []
    copy.recompute_duration()
    return copy


def render_for_verify(edl: EDL, session_dir: Path, *, max_duration_s: float,
                      on_progress: Callable[[float], None] | None = None,
                      cancel_event: Any = None, speech_only: bool = False) -> Path | None:
    """The 360p verify render for `edl`, or None when the timeline is too
    long to verify by rendering (the caller reports the checks as unmeasured).
    `speech_only=True` renders the music-muted variant (see `speech_only`)."""
    if edl.duration > max_duration_s or edl.duration <= 0.0:
        return None
    from .compositor import _render
    if speech_only:
        edl = globals()["speech_only"](edl)
    out_dir = Path(session_dir) / "cache" / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_speech" if speech_only else ""
    dst = out_dir / f"verify_{edl.hash()}_{VERIFY_HEIGHT}{suffix}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    _render(edl, dst, height=VERIFY_HEIGHT, fps=edl.canvas.fps, preview=False,
            cache_dir=Path(session_dir) / "cache", crf=VERIFY_CRF,
            on_progress=on_progress, cancel_event=cancel_event)
    return dst


def _ffmpeg_stderr(args: list[str]) -> str:
    proc = subprocess.run([_pu.FFMPEG, "-hide_banner", "-nostats", *args, "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_pu.SUBPROCESS_FLAGS)
    return proc.stderr


def integrated_loudness(path: Path) -> float | None:
    """EBU R128 integrated loudness (LUFS) of `path`'s audio, via ffmpeg's
    `ebur128` filter; None when the file has no measurable audio."""
    err = _ffmpeg_stderr(["-i", str(path), "-vn", "-af", "ebur128=framelog=verbose"])
    hits = _LUFS_RE.findall(err)
    if not hits:
        return None
    value = float(hits[-1])
    return None if value == float("-inf") or value < -120 else value


def total_silence(path: Path, *, noise_db: float = -30.0, min_dur: float = 0.5) -> float:
    """Seconds of silence `silencedetect` finds in `path` (sum of the detected
    stretches at least `min_dur` long below `noise_db`)."""
    err = _ffmpeg_stderr(["-i", str(path), "-vn", "-af",
                          f"silencedetect=noise={noise_db}dB:d={min_dur}"])
    return round(sum(float(d) for d in _SILENCE_DUR_RE.findall(err)), 3)


__all__ = ["render_for_verify", "speech_only", "integrated_loudness", "total_silence",
           "VERIFY_HEIGHT", "VERIFY_CRF"]
