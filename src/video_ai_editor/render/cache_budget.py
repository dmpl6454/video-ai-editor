"""Byte-budgeted LRU for the render caches (QA-106).

Every render cache used to be bounded by a FILE COUNT — 200 chunks, 400
segments, 15 cached videos, 10 previews — never by bytes. A count says nothing
about size: a 12-minute project's preview is ~130 MB where a 10-second one is
1 MB, so nine ordinary edits on the 12-minute project left ~1.6 GB behind
(previews 776 MB, cache/videos 390 MB, cache/chunks 443 MB for 212 MB of
uploads) and a QA workdir grew by 4.7 GB in an hour.

What this module owns — and ONLY this — is the set of files that are pure
functions of the EDL and can always be rendered again:

    previews/<hash>.mp4                 the preview the <video> plays
    cache/videos/video_*.mp4            video-only previews (audio remux path)
    cache/chunks/chunk_*.mp4, seg_*.mp4 per-clip chunks and their segments
    cache/reversed/rev_*                reversed-clip intermediates (QA-037)
    cache/speed_audio/sa_*              speed-curve sound intermediates (Wave D S1)
    cache/verify/verify_*.mp4           prompt-verifier renders

Everything else under cache/ is NOT a render cache and is never touched here:
stabilize/upscale/rife/lama/stems/denoise/bgremove outputs become a clip's
`src`, and transcripts, TTS, diarisation and indices are expensive or
irreplaceable. Exports are deliverables, not cache.

Recency is the file's mtime. Readers "touch" a cached file when they reuse it
(`touch`), so an entry that is hit on every edit stays young however old it
is. Eviction removes the least recently used entries until the session is
under `session_budget_bytes()`, then (throttled) does the same across every
session in the workdir against `total_budget_bytes()`. Two things are never
evicted: paths the caller names as in use (`protect`), and anything touched
within `PROTECT_RECENT_S` — a concurrent render may have resolved a cache path
it has not opened yet.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .. import platformutil as _pu

_MB = 1024 * 1024

#: (sub-directory of the session dir, glob) of every render-cache entry.
CACHE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("previews", "*.mp4"),
    ("cache/videos", "video_*.mp4"),
    ("cache/chunks", "chunk_*.mp4"),
    ("cache/chunks", "seg_*.mp4"),
    ("cache/reversed", "rev_*"),
    ("cache/speed_audio", "sa_*"),
    ("cache/verify", "verify_*.mp4"),
)

#: A file touched this recently is in use by some render; never evict it.
PROTECT_RECENT_S = 30.0
#: mtime granularity allowance for `since`. APFS/NTFS/ext4 keep sub-µs
#: mtimes; on a FAT/exFAT volume (2 s) a working-set file may read as older
#: than the render and be evicted — which costs a rebuild, never correctness.
_MTIME_SLACK_S = 0.01
#: Minimum seconds between two workdir-wide sweeps (each one stats every
#: cache file of every project).
GLOBAL_SWEEP_INTERVAL_S = 60.0


def session_budget_bytes() -> int:
    """Per-project render-cache budget (VAI_RENDER_CACHE_MB, default 1024)."""
    return _env_mb("VAI_RENDER_CACHE_MB", 1024)


def total_budget_bytes() -> int:
    """Budget across every project in the workdir (VAI_RENDER_CACHE_TOTAL_MB,
    default 6144)."""
    return _env_mb("VAI_RENDER_CACHE_TOTAL_MB", 6144)


def _env_mb(name: str, default: int) -> int:
    try:
        return max(0, int(float(os.environ.get(name, default)))) * _MB
    except (TypeError, ValueError):
        return default * _MB


@dataclass(frozen=True)
class Entry:
    path: Path
    size: int
    mtime: float


def touch(path: str | os.PathLike) -> None:
    """Mark a cache entry as just used (LRU recency). Best-effort."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def entries(session_dir: Path) -> list[Entry]:
    """Every render-cache file of one session. Staged `.part` files (dot
    names) belong to renders in flight and are never listed."""
    out: list[Entry] = []
    for sub, pattern in CACHE_PATTERNS:
        d = session_dir / sub
        if not d.is_dir():
            continue
        for p in d.glob(pattern):
            if p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            out.append(Entry(p, int(st.st_size), float(st.st_mtime)))
    return out


def usage(session_dir: Path) -> dict:
    """Bytes held per cache area, for the UI's cache readout."""
    by_area: dict[str, int] = {}
    for e in entries(session_dir):
        area = e.path.parent.relative_to(session_dir).as_posix()
        by_area[area] = by_area.get(area, 0) + e.size
    return {"bytes": sum(by_area.values()), "by_area": by_area,
            "budget_bytes": session_budget_bytes()}


def _evict(pool: list[Entry], budget: int, protect: set[Path], now: float,
           since: float | None = None) -> list[Path]:
    """Delete least-recently-used entries of `pool` until it fits `budget`.
    Entries used at or after `since` (a render's own working set) are kept
    even if that leaves the pool over budget: evicting them would only make
    the next edit rebuild what this one just built."""
    total = sum(e.size for e in pool)
    removed: list[Path] = []
    if total <= budget:
        return removed
    for e in sorted(pool, key=lambda e: e.mtime):
        if total <= budget:
            break
        if e.path in protect or now - e.mtime < PROTECT_RECENT_S:
            continue
        if since is not None and e.mtime >= since - _MTIME_SLACK_S:
            continue
        try:
            _pu.unlink_with_retry(e.path)
        except OSError:
            continue
        if e.path.exists():
            continue        # still held open somewhere (Windows): keep counting it
        total -= e.size
        removed.append(e.path)
    return removed


def _resolved(paths: Iterable[str | os.PathLike | None]) -> set[Path]:
    out: set[Path] = set()
    for p in paths:
        if p:
            out.add(Path(p))
    return out


def enforce_session(session_dir: Path, *, protect: Iterable = (),
                    budget: int | None = None, since: float | None = None) -> list[Path]:
    """Bring one project's render caches under its byte budget (LRU)."""
    return _evict(entries(Path(session_dir)),
                  session_budget_bytes() if budget is None else budget,
                  _resolved(protect), time.time(), since)


_SWEEP_LOCK = threading.Lock()
_LAST_SWEEP = [0.0]


def enforce_workdir(workdir: Path, *, protect: Iterable = (),
                    budget: int | None = None, force: bool = False) -> list[Path]:
    """The same LRU across EVERY project in `workdir`: the projects you have
    not opened for a while give up their caches first. Throttled to one sweep
    per GLOBAL_SWEEP_INTERVAL_S unless `force`."""
    now = time.time()
    with _SWEEP_LOCK:
        if not force and now - _LAST_SWEEP[0] < GLOBAL_SWEEP_INTERVAL_S:
            return []
        _LAST_SWEEP[0] = now
    pool: list[Entry] = []
    for sd in Path(workdir).glob("s_*"):
        if sd.is_dir():
            pool.extend(entries(sd))
    return _evict(pool, total_budget_bytes() if budget is None else budget,
                  _resolved(protect), now)


def enforce(session_dir: Path, *, protect: Iterable = (),
            since: float | None = None) -> list[Path]:
    """What a render calls when it finishes: its own project first, then the
    workdir-wide budget (throttled). `since` = when the render started: what
    it used is its working set and stays. Never raises — a cache that could
    not be trimmed must not fail the render that just succeeded."""
    removed: list[Path] = []
    protect = list(protect)
    try:
        removed += enforce_session(session_dir, protect=protect, since=since)
        removed += enforce_workdir(Path(session_dir).parent, protect=protect)
    except Exception:
        pass
    return removed


def clear(session_dir: Path, *, protect: Iterable = (), protect_recent_s: float = 0.0) -> int:
    """Delete every render-cache entry of a project except `protect` (the
    user's "Clear render cache"). Returns the bytes freed.

    `protect_recent_s` additionally keeps entries touched that recently — the
    route passes PROTECT_RECENT_S while a render of the project is running,
    for the same reason `_evict` always does: a concurrent render may have
    resolved a path (a concat list's chunk) it has not opened yet."""
    keep = _resolved(protect)
    freed = 0
    now = time.time()
    for e in entries(Path(session_dir)):
        if e.path in keep:
            continue
        if protect_recent_s > 0 and now - e.mtime < protect_recent_s:
            continue
        try:
            _pu.unlink_with_retry(e.path)
        except OSError:
            continue
        if not e.path.exists():
            freed += e.size
    return freed


# ---- wave D: preview proxies, their own LRU class (spec §5.1 "Disk") ---------
#
# Proxies live OUTSIDE the sessions (``WORKDIR/proxies/<key>/``, keyed on the
# source file's identity, so two projects sharing a file share its proxy) and
# are NOT counted against the render-cache budgets above: a proxy is rebuilt
# span by span on demand, so the eviction unit is one span pack (v/*.bin) or
# FLAC chunk (a/*.flac). The small per-proxy files (index/source/refs/init)
# stay; a proxy whose source file no longer exists goes entirely.

PROXY_PATTERNS: tuple[tuple[str, str], ...] = (("v", "*.bin"), ("a", "*.flac"))


def proxy_budget_bytes() -> int:
    """Byte cap of the proxies class (VAI_PROXY_CACHE_MB, default 10240 ≈ 250
    source-minutes at the measured ~40 MB per source-minute)."""
    return _env_mb("VAI_PROXY_CACHE_MB", 10240)


def proxy_entries(root: Path) -> list[Entry]:
    out: list[Entry] = []
    root = Path(root)
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.is_dir():
            continue
        for sub, pattern in PROXY_PATTERNS:
            sd = d / sub
            if not sd.is_dir():
                continue
            for p in sd.glob(pattern):
                if p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append(Entry(p, int(st.st_size), float(st.st_mtime)))
    return out


def _orphaned_proxy(d: Path) -> bool:
    """A proxy dir whose recorded source file is gone (never re-requested)."""
    import json as _json
    try:
        src = _json.loads((d / "source.json").read_text(encoding="utf-8")).get("src")
    except (OSError, ValueError, AttributeError):
        return False
    return bool(src) and not Path(src).exists()


def enforce_proxies(root: Path, *, budget: int | None = None,
                    protect: Iterable = ()) -> list[Path]:
    """Trim the proxies class to its byte budget (LRU by mtime; files touched
    in the last PROTECT_RECENT_S stay), after dropping proxies of deleted
    sources. Never raises."""
    removed: list[Path] = []
    root = Path(root)
    try:
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir() and _orphaned_proxy(d):
                    _pu.rmtree_with_retry(d)
                    removed.append(d)
        removed += _evict(proxy_entries(root),
                          proxy_budget_bytes() if budget is None else budget,
                          _resolved(protect), time.time())
    except Exception:
        pass
    return removed


def proxy_usage(root: Path) -> dict:
    pool = proxy_entries(root)
    return {"bytes": sum(e.size for e in pool), "files": len(pool),
            "budget_bytes": proxy_budget_bytes()}
