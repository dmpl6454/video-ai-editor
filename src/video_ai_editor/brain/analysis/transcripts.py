"""The transcript of record (EB1: the upload's `ingest.json`; no new ASR pass).

A transcript is in its OWN file's seconds. The graph has one clock —
reference seconds — and the settled offset convention is

    file_t = ref_t + offsets[file]      so      ref_t = file_t − offsets[file]

When the reference is a recorder (an audio upload carries no transcript in a
real session: `uploads/audio/`, no `ingest.json`), the transcript of record is
the first synced angle's — v1 first — moved onto the reference clock by
`to_reference`. Word edges are then repaired against the REFERENCE's voiced
runs (`word_timing.py`), exactly as for a transcript of the reference itself.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: a word CLIPPED by the reference's extent that keeps less than this is dropped
MIN_WORD_S = 0.02
OF_REFERENCE = "reference"
OF_V1_ANGLE = "v1_angle"
OF_ANGLE = "angle"
#: An upload whose `ingest.json` has no transcript is still being transcribed for this long after the file
#: was written (the background whisper pass rewrites it when done); after that nothing is coming. Mirrors
#: `agent/prompt/facts.TRANSCRIPT_PENDING_MAX_AGE_S`, the rule the Prompt bar already lives by
#: (tests/test_brain_analysis_job.py pins the two together).
PENDING_MAX_AGE_S = 600
READY, PENDING, NONE = "ready", "pending", "none"
#: `ingest.json`'s `transcript_status` values that say no transcript is on its way.
NOTHING_COMING = ("failed", "skipped")


def read_transcript(path: str | os.PathLike, explicit: str | os.PathLike | None = None) -> dict | None:
    """The transcript beside `path` (its upload's `ingest.json`) or the
    `explicit` file: `{language, duration, segments[…words]}` or None."""
    cand = Path(explicit) if explicit else Path(path).parent / "ingest.json"
    if not cand.is_file():
        return None
    try:
        data = json.loads(cand.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tr = data if "segments" in data else data.get("transcript")
    return tr if isinstance(tr, dict) and tr.get("segments") else None


def transcript_state(path: str | os.PathLike, explicit: str | os.PathLike | None = None,
                     *, now: float | None = None) -> str:
    """`ready` (a transcript with words is on disk), `pending` (the upload's `ingest.json` exists without a
    transcript and is younger than `PENDING_MAX_AGE_S` — the background pass has not written it yet; an
    `ingest.json` that says `"transcript_status"` is `failed` or `skipped` is `none` at once) or
    `none` (no upload record, an explicit file with nothing in it, or a record too old to be waiting).
    Pure reads; never raises."""
    if read_transcript(path, explicit) is not None:
        return READY
    if explicit:
        return NONE
    ingest = Path(path).parent / "ingest.json"
    try:
        data = json.loads(ingest.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - ingest.stat().st_mtime
    except (OSError, ValueError):
        return NONE
    if not isinstance(data, dict) or data.get("transcript") or data.get("transcript_status") in NOTHING_COMING:
        return NONE                # a writer that KNOWS the pass failed says so; nothing is coming
    return PENDING if age < PENDING_MAX_AGE_S else NONE


def _moved_word(w: dict, offset_s: float, duration_s: float) -> dict | None:
    if "start" not in w or "end" not in w:
        return None
    a, b = float(w["start"]) - offset_s, float(w["end"]) - offset_s
    t0, t1 = max(0.0, a), min(duration_s, b)
    clipped = t0 != a or t1 != b
    if t1 < t0 or (clipped and t1 - t0 < MIN_WORD_S):
        return None         # outside the reference (a zero-length word INSIDE it is whisper's and stays)
    return {**w, "start": round(t0, 3), "end": round(t1, 3)}


def _moved_segment(seg: dict, offset_s: float, duration_s: float) -> dict | None:
    words = [m for m in (_moved_word(w, offset_s, duration_s) for w in seg.get("words") or []) if m is not None]
    if not words:
        return None
    text = seg.get("text", "")
    if len(words) != len(seg.get("words") or []):
        text = " ".join(str(w.get("word", w.get("text", ""))).strip() for w in words)
    return {**seg, "start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words}


def to_reference(transcript: dict, offset_s: float, duration_s: float) -> dict:
    """`transcript` (its file's seconds) in reference seconds: every time
    minus `offset_s`, clamped to `[0, duration_s]`; words that fall outside
    are dropped, a segment left without words is dropped. A new dict."""
    if not offset_s and float(transcript.get("duration") or 0.0) <= duration_s:
        return json.loads(json.dumps(transcript))
    segs = [m for m in (_moved_segment(s, float(offset_s), float(duration_s))
                        for s in transcript.get("segments") or []) if m is not None]
    return {**transcript, "duration": round(float(duration_s), 3), "segments": segs}


def transcript_of_record(ref: dict, members: list[dict]) -> tuple[dict | None, str | None]:
    """(transcript in reference seconds, whose it is). The reference's own
    first; else the first SYNCED member's that has one (members are in source
    order, v1 first) moved by its offset; a member whose sync is unverified is
    never used (its clock is unknown)."""
    own = read_transcript(ref["path"], ref.get("transcript"))
    if own is not None:
        return own, OF_REFERENCE
    for m in members:
        if (m.get("sync") or {}).get("unverified", True):
            continue
        tr = read_transcript(m["path"], m.get("transcript"))
        if tr is not None:
            moved = to_reference(tr, float(m["sync_offset_s"]), float(ref["duration"]))
            if moved["segments"]:
                return moved, OF_V1_ANGLE if "v1" in m.get("tags", ()) else OF_ANGLE
    return None, None


__all__ = ["MIN_WORD_S", "OF_REFERENCE", "OF_V1_ANGLE", "OF_ANGLE", "PENDING_MAX_AGE_S", "READY", "PENDING", "NONE",
           "read_transcript", "transcript_state", "to_reference", "transcript_of_record"]
