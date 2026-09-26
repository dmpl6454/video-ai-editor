"""Bakes: server frames for the BAKED ranges of the instant preview (wave D).

Spec: docs/design/INSTANT_PREVIEW_SPEC.md §5.3, §6 R13, §7, §13 P1-B1.

WHAT A BAKE IS. Where the client cannot draw a range exactly (colour, LUTs,
effects, transitions, masks, a structural disagreement — ``support.ts``
decides), it splices the SERVER's frames of that range into the same MSE
player instead of swapping to another ``<video>``. Those frames are
``previews/<render_hash>.mp4``: ``render_preview`` renders the render clock
from 0 at the project rate, so preview frame ``k`` IS output frame ``k``.

A bake is simply a PROXY of that preview file (§5.3 "Phase 1"): the same
all-intra, stitchable, BT.709-tagged recipe as every source proxy
(``ingest/proxy.py``), at the preview's own size (540 short edge is under the
recipe's 720 and the recipe never upscales), cut into span packs. So the
client appends bake frames exactly as it appends proxy frames; the bake's
``init_key`` (its avcC) is simply another init class — its own size and the
project rate — and the writer re-appends an init when the class changes.
Frame identity (R13) is the proxy's R5: bake frame ``k`` is preview frame
``k`` in presentation order after the edit list.

KEYED BY RENDER HASH + RANGE. The bake key is derived from the preview file
(its path — which names the session and the render hash — and its size),
NOT from its mtime the way a source proxy's is: the render cache touches a
preview every time it is reused (``cache_budget.touch``), and an
mtime-keyed bake would be thrown away on every reuse. Spans are the range
unit: span ``n`` holds output frames ``[n·S, (n+1)·S)`` (S = 2 s of frames),
and only the spans a BAKED range touches are ever encoded — on demand, by
the proxy queue's urgent span jobs, after the preview render has landed
(before that every bake route is 404).

STORAGE AND EVICTION. Bakes live beside the source proxies
(``WORKDIR/proxies/<bake key>/``) and are one LRU class with them
(``cache_budget.enforce_proxies``, spec §9.4 "proxies/bakes"): span packs are
the eviction unit and rebuild on demand, and a bake whose preview file was
evicted is an orphan the next sweep removes. ``source.json`` records the
preview path; ``refs.json`` records the session (the key routes' access
rule is the same as for proxies).

LATEST WINS PER SESSION. A bake of a hash the session has moved past is
superseded: asking for a newer hash cancels the queued and running span
encodes of the older one (``ProxyManager.cancel`` on its preview path).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Iterable

from ..ingest import proxy as P
from ..ingest.proxy_queue import MANAGER

#: ``EDL.render_hash()``: 16 lowercase hex characters.
HASH_RE = re.compile(r"^[0-9a-f]{16}$")

_PROBE_LOCK = threading.Lock()
_LATEST_LOCK = threading.Lock()
#: sid -> the preview path of the newest bake that session asked for.
_LATEST: dict[str, str] = {}


def is_valid_hash(h: object) -> bool:
    return isinstance(h, str) and bool(HASH_RE.fullmatch(h))


def preview_path(session_dir: Path | str, render_hash: str) -> Path:
    """``<session>/previews/<render_hash>.mp4`` (the hash validated first, so
    no request text ever becomes a path component unparsed)."""
    if not is_valid_hash(render_hash):
        raise ValueError("invalid render hash")
    return Path(session_dir) / "previews" / f"{render_hash}.mp4"


def preview_ready(session_dir: Path | str, render_hash: str) -> Path | None:
    """The preview file of ``render_hash`` once the render has landed (a
    finished, non-empty file; in-flight renders write a dot-named ``.part``)."""
    p = preview_path(session_dir, render_hash)
    try:
        return p if p.is_file() and p.stat().st_size > 0 else None
    except OSError:
        return None


def bake_key(preview: Path | str) -> str:
    """Key of the bake of one preview file: realpath + size + recipe (never
    mtime — see the module docstring). Same shape as a proxy key."""
    real = os.path.realpath(os.fspath(preview))
    size = os.stat(real).st_size
    ident = f"bake\0{real}\0{size}\0{P._recipe_tag()}"
    return hashlib.sha256(ident.encode("utf-8", "surrogatepass")).hexdigest()[:P.KEY_LEN]


def _probe(key: str, preview: str) -> P.SourceInfo | None:
    """Probe the preview once (stream facts + pts table) and write the
    bake's source.json / index.json, as the proxy queue does for a source."""
    info = P.load_source(key)
    if info is not None:
        return info
    with _PROBE_LOCK:
        info = P.load_source(key)
        if info is not None:
            return info
        try:
            info = P.probe_source(preview, key)
        except Exception as e:           # ffprobe failure, no stream, ...
            P.mark_failed(key, f"bake probe failed: {e}")
            return None
        if not info.has_video:
            P.mark_failed(key, "preview has no video stream")
            return None
        P.save_source(info)
        idx = P.read_index(key) or {}
        if "frames" not in idx:
            keep = {k: v for k, v in idx.items() if k in ("codec", "init_key")}
            P.write_index(key, {**P.static_index(info), **keep})
        return info


def _supersede(sid: str, preview: str) -> None:
    with _LATEST_LOCK:
        old = _LATEST.get(sid)
        _LATEST[sid] = preview
    if old and old != preview:
        MANAGER.cancel(old)


def ensure(sid: str, session_dir: Path | str, render_hash: str) -> str | None:
    """Key of the bake of ``render_hash`` for session ``sid`` (probed, the
    session recorded as a reference), or None while no preview of that hash
    exists. Raises ValueError for a malformed hash."""
    p = preview_ready(session_dir, render_hash)
    if p is None:
        return None
    real = os.path.realpath(p)
    key = bake_key(real)
    P.proxy_dir(key).mkdir(parents=True, exist_ok=True)
    P.add_ref(key, sid)
    _supersede(sid, real)
    _probe(key, real)
    return key


def spans_for_ranges(info: P.SourceInfo, ranges: Iterable[tuple[int, int]]) -> list[int]:
    """Span indices whose frames intersect any half-open output range."""
    S = info.span_frames
    out: set[int] = set()
    for a, b in ranges:
        a, b = max(0, int(a)), min(info.frames, int(b))
        if b <= a:
            continue
        out.update(range(a // S, (b - 1) // S + 1))
    return sorted(out)


def parse_ranges(text: str | None, *, limit: int = 256) -> list[tuple[int, int]]:
    """``"k0-k1,k2-k3"`` (half-open output frame ranges) → pairs. Raises
    ValueError on anything else, so a malformed query is a clean 400."""
    if not text:
        return []
    out: list[tuple[int, int]] = []
    for part in text.split(","):
        m = re.fullmatch(r"(\d{1,7})-(\d{1,7})", part.strip())
        if not m:
            raise ValueError(f"bad range {part!r}")
        a, b = int(m.group(1)), int(m.group(2))
        if b <= a:
            raise ValueError(f"empty range {part!r}")
        out.append((a, b))
        if len(out) > limit:
            raise ValueError("too many ranges")
    return out


def queue_ranges(key: str, ranges: Iterable[tuple[int, int]]) -> list[int]:
    """Queue the span encodes a set of BAKED ranges needs (non-blocking);
    returns the span indices, in order."""
    info = P.load_source(key)
    if info is None:
        return []
    spans = spans_for_ranges(info, ranges)
    for n in spans:
        MANAGER.request_span(key, n, timeout=0.0)
    return spans


def index(key: str, render_hash: str) -> dict | None:
    """The bake's live index: the proxy index (init_key, codec, w, h,
    span_frames, span_ready, ...) plus what makes it a bake."""
    idx = P.live_index(key)
    if idx is None or "frames" not in idx:
        return None
    out = {k: v for k, v in idx.items() if k != "audio"}
    out.update({"bake": True, "render_hash": render_hash,
                "rate": idx.get("src_rate"),
                "init_url": "init.mp4", "span_url": "v/{n}.bin"})
    return out


__all__ = ["HASH_RE", "is_valid_hash", "preview_path", "preview_ready", "bake_key",
           "ensure", "spans_for_ranges", "parse_ranges", "queue_ranges", "index"]
