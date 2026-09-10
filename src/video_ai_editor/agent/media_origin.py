"""Derived media → the upload it was made from.

Several tools replace a clip's `src` with a file they render into the
session cache — `auto_reframe` (cache/reframe_*.mp4), `noise_reduce`
(cache/denoise/*), `stabilize`, `smooth_slow_motion`, `upscale`, the stem
isolators. The transcript, though, is keyed to the UPLOAD: `ingest.json`
sits beside `uploads/<stem>/<stem>.normalized.mp4`, and every transcript
time is a source second of that file. Before this module the transcript
was resolved from the CURRENT first-v1 `src`, so the first src-rewriting
step of a plan silently lost the transcript for the rest of the session —
the flagship "clean this up for TikTok" prompt reframed at stage 6 and then
laid ZERO captions at stage 7, and the verifier could no longer measure
speech (`speech_preserved pass=null`). Measured on the 84.5 s benchmark
clip, 2026-09-10.

The derivation is recorded as a sidecar, `<derived file>.origin`, holding
the ORIGIN path (chains collapse: a denoise of a reframe points straight at
the upload). A sidecar rather than an EDL field because every derived file
keeps the source's duration and timing (the reframe handler already relies
on that to keep `in`/`out` valid), so the mapping is a property of the
FILE, not of the clip: two clips playing the same derived file share it,
and an EDL written by an older build reads back unchanged.

`origin_of(path)` is what `timemap._same_source`, `dispatch._first_v1_media_src`
and `_current_v1_ingest_json` compare — pure `Path` reads, bounded depth,
never raising.
"""
from __future__ import annotations

import os
from pathlib import Path

ORIGIN_SUFFIX = ".origin"
#: A chain longer than this is a loop or a corrupt sidecar; stop and use the
#: last path seen rather than spinning.
_MAX_DEPTH = 8


def origin_sidecar(path: str | os.PathLike) -> Path:
    return Path(str(path) + ORIGIN_SUFFIX)


def record_origin(derived: str | os.PathLike, source: str | os.PathLike) -> Path | None:
    """Write `<derived>.origin` = the ROOT origin of `source` (so chains
    collapse). Returns the sidecar path, or None when it could not be written
    — a missing sidecar degrades to today's behaviour (transcript keyed to
    the derived file), never to an exception inside a handler."""
    derived_p = Path(derived)
    root = origin_of(source)
    if os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(derived_p)):
        return None                       # a tool that returned its own input
    try:
        side = origin_sidecar(derived_p)
        side.write_text(root, encoding="utf-8")
        return side
    except OSError:
        return None


def origin_of(path: str | os.PathLike | None) -> str:
    """The upload `path` was derived from, following `.origin` sidecars;
    `path` itself when it has none. Pure reads; a corrupt sidecar is ignored."""
    if path is None:
        return ""
    current = str(path)
    for _ in range(_MAX_DEPTH):
        side = origin_sidecar(current)
        try:
            if not side.is_file():
                return current
            nxt = side.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return current
        if not nxt or nxt == current:
            return current
        current = nxt
    return current


def is_derived(path: str | os.PathLike | None) -> bool:
    return bool(path) and origin_sidecar(path).is_file()


__all__ = ["ORIGIN_SUFFIX", "origin_sidecar", "record_origin", "origin_of", "is_derived"]
