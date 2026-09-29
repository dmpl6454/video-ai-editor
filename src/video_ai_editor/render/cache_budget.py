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
import re
import signal
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


# ---- final QA (round 3): staged .part files a dead writer left behind -------
#
# `compositor._part_path` / `platformutil.part_path` stage every render as
# `.{stem}.{pid}.{tid}.part{suffix}` next to its destination. When the backend
# died mid-render (Force Quit, an out-of-memory kill, a crash of the in-process
# app) that file stayed for good — 340 MB for one interrupted 1080p export,
# dot-named so Finder hid it, invisible to the readout and to "Clear render
# cache" — and its ffmpeg kept encoding on its own. A part whose writer PID is
# dead is stale: counted and cleared with the caches and swept at startup; an
# encoder still writing one is stopped at startup. A LIVE writer's part (this
# process's own render in flight) is never touched.

#: `.{stem}.{pid}.{tid}.part{suffix}` (the stem may contain dots).
_PART_RE = re.compile(r"^\..+\.(\d+)\.(\d+)\.part(?:\.[A-Za-z0-9]+)?$")
#: Where renders stage their output inside a session.
PART_DIRS: tuple[str, ...] = ("exports", "previews", "cache/videos", "cache/chunks",
                              "cache/reversed", "cache/speed_audio", "cache/verify")


def part_writer_pid(name: str) -> int | None:
    """The writer PID embedded in a staged file's name, or None."""
    m = _PART_RE.match(name)
    return int(m.group(1)) if m else None


def _is_stale(pid: int) -> bool:
    return pid != os.getpid() and not _pu.pid_alive(pid)


def stale_parts(session_dir: Path) -> list[Path]:
    """Staged `.part` files of one session whose writer is dead."""
    out: list[Path] = []
    for sub in PART_DIRS:
        d = Path(session_dir) / sub
        if not d.is_dir():
            continue
        for p in d.glob(".*.part*"):
            pid = part_writer_pid(p.name)
            if pid is not None and p.is_file() and _is_stale(pid):
                out.append(p)
    return out


def _size(p: Path) -> int:
    try:
        return int(p.stat().st_size)
    except OSError:
        return 0


def _drop(paths: Iterable[Path]) -> int:
    freed = 0
    for p in paths:
        n = _size(p)
        try:
            _pu.unlink_with_retry(p)
        except OSError:
            continue
        if not p.exists():
            freed += n
    return freed


def sweep_stale_parts(workdir: Path) -> int:
    """Delete every session's stale `.part` files (startup). Bytes freed."""
    freed = 0
    for sd in Path(workdir).glob("s_*"):
        if sd.is_dir():
            freed += _drop(stale_parts(sd))
    return freed


def stop_orphan_encoders(workdir: Path) -> list[int]:
    """SIGTERM every ffmpeg still writing a `.part` file under `workdir` for a
    writer that is dead — the encoder a crashed backend left running (it
    kept encoding for ~15-25 s, longer for a long export). Only processes
    whose executable is an ffmpeg AND whose command line names such a file
    under `workdir` are touched. POSIX only (`platformutil.list_processes`).
    Returns the PIDs signalled."""
    root = str(Path(workdir).resolve())
    stopped: list[int] = []
    for pid, cmd in _pu.list_processes():
        # the executable is everything before the first option (its path may
        # hold spaces: the app bundle's own ffmpeg)
        if pid == os.getpid() or not re.search(r"(?:^|/)ffmpeg(?:\.exe)?$", cmd.split(" -", 1)[0]):
            continue
        at = cmd.find(root + os.sep)
        if at < 0:
            continue
        # the output path runs to the end of its `.part.<ext>` name
        m = re.search(r"/(\.[^/]+\.(\d+)\.\d+\.part(?:\.[A-Za-z0-9]+)?)(?:\s|$)", cmd[at:])
        if not m or not _is_stale(int(m.group(2))):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except OSError:
            continue
    return stopped


def usage(session_dir: Path) -> dict:
    """Bytes held per cache area, for the UI's cache readout — including the
    stale `.part` files a crashed render left (`stale_parts`), which "Clear
    render cache" removes."""
    by_area: dict[str, int] = {}
    for p in stale_parts(session_dir):
        area = p.parent.relative_to(session_dir).as_posix()
        by_area[area] = by_area.get(area, 0) + _size(p)
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
    freed = _drop(stale_parts(Path(session_dir)))       # a dead writer's .part (final QA r3)
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


# ---- final QA r4: the Prompt bar's dry-run artefacts, their own LRU class -----
#
# `agent/prompt/artefacts.py` keeps what a preview's dry run derived (a
# whisper transcript, a denoised or reframed render) under
# ``<workdir>/.prompt_preview/artefacts/<kind>/<key>/`` so Apply does not do
# the work twice. Entries are self-contained copies, nothing points at them,
# so the eviction unit is one whole entry directory: a half-evicted entry
# would otherwise read as a miss and be dropped anyway. Recency is the
# entry's marker file (transcript.json / manifest.json), touched on every
# hit. A cancelled or dropped card leaves its entries here to age out.

#: Marker files of the two entry kinds (touched on a hit).
ARTEFACT_MARKERS: tuple[str, ...] = ("transcript.json", "manifest.json")


def artefact_budget_bytes() -> int:
    """Byte cap of the dry-run artefacts (VAI_PROMPT_ARTEFACT_CACHE_MB, default 2048)."""
    return _env_mb("VAI_PROMPT_ARTEFACT_CACHE_MB", 2048)


def artefact_units(root: Path) -> list[Entry]:
    """One `Entry` per artefact entry directory: its total bytes and the
    marker's mtime (the directory's own when the marker is missing, i.e. an
    entry still being written or already torn)."""
    out: list[Entry] = []
    root = Path(root)
    if not root.is_dir():
        return out
    for kind in root.iterdir():
        if not kind.is_dir() or kind.name.startswith("."):
            continue
        for unit in kind.iterdir():
            if not unit.is_dir() or unit.name.startswith("."):
                continue
            size = 0
            for p in unit.rglob("*"):
                try:
                    if p.is_file():
                        size += int(p.stat().st_size)
                except OSError:
                    continue
            mtime = None
            for name in ARTEFACT_MARKERS:
                try:
                    mtime = float((unit / name).stat().st_mtime)
                    break
                except OSError:
                    continue
            if mtime is None:
                try:
                    mtime = float(unit.stat().st_mtime)
                except OSError:
                    continue
            out.append(Entry(unit, size, mtime))
    return out


def enforce_artefacts(root: Path, *, budget: int | None = None,
                      protect: Iterable = ()) -> list[Path]:
    """Trim the artefacts area to its byte budget, least recently used entry
    first, whole entries at a time. `protect` (the entry being written) and
    entries touched within PROTECT_RECENT_S (a run reading one) stay. Never
    raises — the cache must not fail the step that just filled it."""
    removed: list[Path] = []
    try:
        pool = artefact_units(Path(root))
        limit = artefact_budget_bytes() if budget is None else budget
        keep = _resolved(protect)
        now = time.time()
        total = sum(e.size for e in pool)
        for e in sorted(pool, key=lambda e: e.mtime):
            if total <= limit:
                break
            if e.path in keep or now - e.mtime < PROTECT_RECENT_S:
                continue
            _pu.rmtree_with_retry(e.path)
            if e.path.exists():
                continue
            total -= e.size
            removed.append(e.path)
    except Exception:
        pass
    return removed


def artefact_usage(root: Path) -> dict:
    pool = artefact_units(Path(root))
    return {"bytes": sum(e.size for e in pool), "entries": len(pool),
            "budget_bytes": artefact_budget_bytes()}
