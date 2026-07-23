"""Heuristic short-form highlight extraction.

Score every detected shot by:
  - audio loudness peaks (more energetic = more interesting)
  - shot duration (mid-length shots win over very short fragments)
  - transcript density (more spoken words → likely the punchline)
  - position in the source (mid-clip slightly preferred over edges)

Pick the top N non-overlapping clips and return them as alternative-EDL
recipes the caller can render or save as separate sessions.
"""
from __future__ import annotations
import json
import statistics
import subprocess
from pathlib import Path

from ..ingest.probe import probe
from ..ingest.scenes import detect_shots, Shot
from .. import platformutil as _pu


def _audio_levels(src: Path, *, n_buckets: int = 200) -> list[float]:
    """Return RMS-ish energy per equal-length bucket across the clip."""
    info = probe(src)
    duration = info.duration
    if duration <= 0:
        return []
    sr = 8000  # cheap mono extraction
    proc = subprocess.run(
        [_pu.FFMPEG, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        capture_output=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    if proc.returncode != 0:
        return []
    raw = proc.stdout
    n = len(raw) // 2
    if n == 0:
        return []
    import struct
    samples = struct.unpack(f"<{n}h", raw)
    bucket = max(1, n // n_buckets)
    levels: list[float] = []
    for i in range(0, n, bucket):
        chunk = samples[i:i + bucket]
        if not chunk:
            continue
        m = max(abs(v) for v in chunk) / 32768.0
        levels.append(m)
    return levels


def _level_at(t: float, levels: list[float], duration: float) -> float:
    if not levels or duration <= 0:
        return 0.0
    idx = min(len(levels) - 1, int((t / duration) * len(levels)))
    return levels[idx]


def make_shorts(src: Path, transcript: dict | None, cache_dir: Path,
                *, target_count: int = 3, max_dur: float = 60.0,
                min_dur: float = 12.0) -> list[dict]:
    """Pick `target_count` highlight ranges from a long source.

    Returns a list of {start, end, score, why} dicts. The caller owns turning
    them into EDLs / separate sessions.
    """
    info = probe(src)
    duration = info.duration
    if duration < min_dur:
        return [{"start": 0.0, "end": duration, "score": 1.0,
                 "why": "source shorter than min_dur — single highlight"}]

    shots = detect_shots(src, threshold=0.3) or [Shot(index=0, start=0.0, end=duration)]
    levels = _audio_levels(src)

    # Score every shot
    scored: list[tuple[float, str, Shot]] = []
    median_shot = statistics.median([s.end - s.start for s in shots if s.end > s.start]) or 1.0
    seg_words = []
    if transcript:
        for s in transcript.get("segments", []) or []:
            for w in (s.get("words") or [{"start": s["start"], "end": s["end"]}]):
                seg_words.append((float(w["start"]), float(w["end"])))

    for s in shots:
        sd = s.end - s.start
        if sd < 0.5:
            continue
        # Energy: max audio level inside the shot
        n_samples = max(1, int((sd / duration) * len(levels))) if levels else 1
        if levels:
            start_idx = int((s.start / duration) * len(levels))
            chunk = levels[start_idx:start_idx + n_samples]
            energy = max(chunk) if chunk else 0.0
        else:
            energy = 0.5
        # Word density inside this shot
        words_in = sum(1 for ws, we in seg_words if we > s.start and ws < s.end)
        word_density = min(1.0, words_in / max(1.0, sd) / 3.0)
        # Length fit: prefer shots near median (smooth bell)
        length_fit = 1.0 - abs(sd - median_shot) / max(median_shot, sd)
        # Position bias: slightly prefer middle 60% of source
        rel_pos = (s.start + sd / 2) / duration
        pos_bias = 1.0 - abs(rel_pos - 0.5) * 0.6
        score = (0.45 * energy + 0.30 * word_density + 0.15 * length_fit + 0.10 * pos_bias)
        why_parts = []
        if energy > 0.5: why_parts.append(f"loud (energy={energy:.2f})")
        if word_density > 0.4: why_parts.append(f"talky ({words_in} words/{sd:.1f}s)")
        if length_fit > 0.7: why_parts.append("good length")
        scored.append((score, "+".join(why_parts) or "balanced", s))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Pick top N non-overlapping, expanded to min_dur if too short
    picked: list[dict] = []
    used_ranges: list[tuple[float, float]] = []
    for score, why, s in scored:
        sd = s.end - s.start
        # Expand small shots up to min_dur by including neighbors
        start = s.start
        end = s.end
        if sd < min_dur:
            pad = (min_dur - sd) / 2
            start = max(0.0, start - pad)
            end = min(duration, end + pad)
        if end - start > max_dur:
            mid = (start + end) / 2
            start = mid - max_dur / 2
            end = mid + max_dur / 2
        # Skip if it overlaps an already-picked range
        if any(not (end <= a or start >= b) for a, b in used_ranges):
            continue
        picked.append({"start": float(start), "end": float(end),
                       "score": round(float(score), 3), "why": why or "balanced"})
        used_ranges.append((start, end))
        if len(picked) >= target_count:
            break
    return picked
