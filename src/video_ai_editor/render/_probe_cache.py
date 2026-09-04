"""Cached source probes the render graph needs BEFORE it can be built.

Two questions, both about a source file rather than the EDL, both answerable
only by asking ffmpeg, and both needed by more than one module — which is why
they live here rather than in `compositor.py`. `audio_mix.py` imports them too,
and importing them from `compositor` would be circular.

The two have DELIBERATELY OPPOSITE failure fallbacks. That asymmetry is the
whole design, so it is spelled out on each function: for stream presence the
safe answer is "assume audio" (the graph then fails loudly with ffmpeg's own
message), and for loudness the safe answer is "assume silent" (loudnorm is
skipped, which costs normalization but never fails an export).
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from .. import platformutil as _pu

# Digital silence measures about -91 dB (the 16-bit floor); genuinely quiet but
# real audio sits well above -80. Anything at or below this is treated as
# nothing-to-normalise.
SILENCE_FLOOR_DB = -80.0

# What we report when a level cannot be measured, and the value that means
# "silent" throughout. Not -inf: it flows into comparisons and formatting.
SILENT_DB = -200.0


def _key(src: str | Path) -> tuple[str, int, int] | None:
    """Cache key: path + mtime_ns + size, so a file replaced under the same
    name is re-probed rather than trusted. None when it cannot be stat'd."""
    p = Path(src)
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p), st.st_mtime_ns, st.st_size)


class _Unprobeable(Exception):
    """Raised inside the cached probes so a FAILURE IS NEVER MEMOIZED.

    `lru_cache` caches return values, not exceptions — so raising is how a
    transient ffprobe hiccup avoids being remembered for the life of the
    process. Caching one would be much worse than it sounds: the wrong answer
    also decides the render, and the resulting output is then cached on disk
    under the EDL hash, so the mistake outlives both the probe cache and a
    restart.
    """


@lru_cache(maxsize=256)
def _probe_has_audio_cached(key: tuple[str, int, int]) -> bool:
    try:
        # Lazy: `ingest.probe` sits in a package whose __init__ pulls the whole
        # ingest pipeline, and nothing else in the render path needs it.
        from ..ingest.probe import probe
        return probe(Path(key[0])).audio is not None
    except Exception as e:
        raise _Unprobeable(str(e)) from e


def source_has_audio(src: str | Path) -> bool:
    """Does this media file carry an audio stream?

    Callers use this to decide between `[<i>:a]` and a generated silence, so a
    wrong answer is not cosmetic: referencing `:a` on a file that has none
    cannot bind the filtergraph ("Stream specifier ':a' … matches no streams"
    → "Error binding filtergraph inputs/outputs", rc=234) and kills the WHOLE
    render, not just that clip's audio.

    Costs one ffprobe per distinct file (~58 ms with Homebrew's ffprobe, ~9 ms
    with the bundled static one), cached on (path, mtime_ns, size). A ten-clip
    timeline over one source pays once; a warm re-render pays nothing.

    ON FAILURE THIS RETURNS True, and is not cached. True is the pre-existing
    behaviour — every caller emitted `[<i>:a]` unconditionally before this
    function existed — so an unprobeable file behaves exactly as it always did
    and ffmpeg reports the real problem with the input. Answering False instead
    would hand back a graph that binds and renders a SILENT export, which is a
    wrong result presented as success, and it would be cached on disk under the
    EDL hash.
    """
    key = _key(src)
    if key is None:
        return True
    try:
        return _probe_has_audio_cached(key)
    except _Unprobeable:
        return True


@lru_cache(maxsize=256)
def _probe_peak_db_cached(key: tuple[str, int, int, float, float]) -> float:
    path, _mtime, _size, start, end = key
    # NOT "-v error": volumedetect prints its summary through av_log at INFO,
    # so quietening the log throws away the very line being parsed and every
    # file reads as silent — which would skip loudnorm for everyone.
    cmd = [_pu.FFMPEG, "-v", "info", "-hide_banner", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if end and end > start:
        cmd += ["-to", f"{end:.3f}"]
    # -vn: decode audio only. On a long clip this is what keeps the probe from
    # costing a full video decode.
    cmd += ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              **_pu.SUBPROCESS_FLAGS)
    except Exception as e:
        raise _Unprobeable(str(e)) from e
    if proc.returncode != 0:
        raise _Unprobeable(f"volumedetect exited {proc.returncode}")
    m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    if m:
        return float(m.group(1))
    # volumedetect prints nothing measurable for a stream of pure zeroes on
    # some builds; no max_volume line with a clean exit means silence.
    return SILENT_DB


def source_peak_db(src: str | Path, in_: float = 0.0, out: float = 0.0) -> float:
    """Peak level in dB of `src`'s audio over [in_, out), or SILENT_DB.

    Exists because STREAM PRESENCE IS NOT AUDIBILITY, and `loudnorm` cares
    about the latter. `ingest/normalize.py` welds a digitally-silent AAC track
    onto every upload that has no audio (so the app's own uploads always have a
    stream), and `loudnorm` on an inaudible mix measures -inf LUFS, computes an
    unbounded gain and emits NaN samples that the AAC encoder rejects —
    "Input contains (near) NaN/+-Inf", rc=234, export dead. Measured on a real
    normalized silent upload, which is the ordinary case for a screen recording.

    ON FAILURE THIS RETURNS SILENT_DB — the opposite fallback to
    `source_has_audio`, and for the opposite reason. Here the only consequence
    of being wrong is that a quiet export skips loudness normalization; the
    alternative is failing the export outright, which is never the better
    trade. Also not cached on failure.
    """
    key = _key(src)
    if key is None:
        return SILENT_DB
    try:
        return _probe_peak_db_cached((*key, float(in_ or 0.0), float(out or 0.0)))
    except _Unprobeable:
        return SILENT_DB


def source_is_audible(src: str | Path, in_: float = 0.0, out: float = 0.0) -> bool:
    """True when `src` carries audio ABOVE the digital-silence floor."""
    return source_peak_db(src, in_, out) > SILENCE_FLOOR_DB


def clear_caches() -> None:
    """Test hook — the caches are keyed on mtime/size, so this is only needed
    when a test wants to observe probe CALLS rather than results."""
    _probe_has_audio_cached.cache_clear()
    _probe_peak_db_cached.cache_clear()
