"""The per-project media library behind the Media panel (QA-010).

WHY THIS EXISTS
---------------
The Media panel used to build its list from the clips on the timeline, so it
was not a library at all: delete the last clip that used a file and the file
vanished from the bin (and stayed gone after reload), although its bytes were
still on disk under ``uploads/``. Getting it back meant Undo or a re-import —
40 s of re-transcoding for a 163 MB take. Every other editor keeps imported
media in the project until it is removed *from the bin*.

The filesystem is the database here (CLAUDE.md "Storage"), so the library is a
scan of what the upload routes already wrote, not a second registry the routes
must remember to update:

* a video import is one ``ingest.json`` anywhere under ``uploads/`` — its
  ``normalized`` file is what the timeline uses, its ``display_name`` (or the
  raw upload's name) is what the user calls it;
* audio imports (``uploads/audio``), voice-over takes (``uploads/vo``) and the
  media a ``.vae`` brought in (``uploads/imported``) are the loose media files;
* anything a timeline clip references that the scan did not find (a reframe
  or other derived render in ``cache/``) is listed too, so the bin never shows
  less than the timeline uses.

``media_library.json`` in the session holds only what the scan cannot know:
which items were removed from the bin (keyed on the file's mtime, so importing
the same file again brings it back) and a probe cache, so listing does not
re-run ffprobe on every audio file each time the panel refreshes.

Removing an item never deletes bytes. Timeline clips are what reference the
file, undo can bring them back at any time, and a bin removal is a view
decision — the same reason ``DELETE /sessions/{sid}`` is the only route that
deletes media.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

_VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".aif", ".aiff"})
# Lanes whose clips reference a media file. Stickers also carry `src`, but they
# are images with their own panel; text/captions/effects reference nothing.
_MEDIA_TRACK_TYPES = frozenset({"video", "audio", "music", "vo"})
# Upload subdirectories that hold loose media files (not ingest dirs).
_LOOSE_DIRS = frozenset({"audio", "vo", "imported"})
_LIBRARY_FILE = "media_library.json"
# Read-modify-write of media_library.json: a listing persisting its probe cache
# must not drop a removal another request recorded in between.
_STATE_LOCK = threading.Lock()
# `<stem>_<8 hex>.<ext>` is how the upload routes make a stored name unique.
_UNIQUE_SUFFIX = re.compile(r"^(?P<stem>.+)_[0-9a-f]{8}(?P<ext>\.[^.]+)$")


def media_id(src: str | Path) -> str:
    """Stable id for one library entry: a hash of its resolved path."""
    return hashlib.sha1(str(Path(src).resolve()).encode("utf-8")).hexdigest()[:12]


def _state_path(session_dir: Path) -> Path:
    return session_dir / _LIBRARY_FILE


def _load_state(session_dir: Path) -> dict[str, Any]:
    p = _state_path(session_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"removed": {}, "probe": {}, "names": {}}
    if not isinstance(data, dict):
        return {"removed": {}, "probe": {}, "names": {}}
    removed = data.get("removed") if isinstance(data.get("removed"), dict) else {}
    probe = data.get("probe") if isinstance(data.get("probe"), dict) else {}
    names = data.get("names") if isinstance(data.get("names"), dict) else {}
    return {"removed": removed, "probe": probe, "names": names}


def record_display_name(session_dir: Path, src: str | Path, name: str) -> None:
    """Remember what the user called a LOOSE upload (audio, voice-over, an
    audio-only file handed off from the video ingress) — QA-045. A video
    import keeps its name in its own ingest.json; a loose file has only its
    sanitised disk name (`upload_6f3d3a.mp3` for a Hindi title), so the name
    is kept here, keyed like every other row, by resolved path."""
    name = (name or "").strip()
    if not name:
        return
    session_dir = Path(session_dir)
    with _STATE_LOCK:
        state = _load_state(session_dir)
        state["names"] = {**state["names"], str(Path(src).resolve()): name[:255]}
        _save_state(session_dir, state)


def forget_display_name(session_dir: Path, src: str | Path) -> None:
    """Drop the name `record_display_name` kept for a file that is gone (a
    refused relink's replacement), so no stale row outlives it."""
    session_dir = Path(session_dir)
    key = str(Path(src).resolve())
    with _STATE_LOCK:
        state = _load_state(session_dir)
        if key not in state["names"]:
            return
        state["names"] = {k: v for k, v in state["names"].items() if k != key}
        _save_state(session_dir, state)


def _save_state(session_dir: Path, state: dict[str, Any]) -> None:
    """Atomic replace: a concurrent listing never reads a half-written file."""
    p = _state_path(session_dir)
    fd, tmp = tempfile.mkstemp(prefix=".media_library.", dir=str(session_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _signature(p: Path) -> list[int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _display_from_disk(name: str) -> str:
    """`song_1a2b3c4d.mp3` → `song.mp3`: undo the uniquifying suffix for display."""
    m = _UNIQUE_SUFFIX.match(name)
    return f"{m.group('stem')}{m.group('ext')}" if m else name


def _uses(edl) -> dict[str, list[str]]:
    """Resolved src → ids of the timeline clips that reference it."""
    out: dict[str, list[str]] = {}
    for track in getattr(edl, "tracks", []) or []:
        if track.type not in _MEDIA_TRACK_TYPES:
            continue
        for clip in track.clips:
            src = getattr(clip, "src", None)
            if not src:
                continue
            out.setdefault(str(Path(src).resolve()), []).append(clip.id)
    return out


def _first_video_stream(probe: dict) -> dict:
    return next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {}) or {}


def _probe_facts(path: Path, cache: dict[str, Any]) -> dict[str, Any] | None:
    """Duration / size of a loose file, from the cache while the file is unchanged."""
    key = str(path)
    sig = _signature(path)
    if sig is None:
        return None
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("sig") == sig:
        return hit
    from .ingest.probe import probe as _probe
    try:
        p = _probe(path)
    except Exception:
        return None
    v = p.video
    facts = {"sig": sig, "duration": p.duration,
             "width": v.width if v else None, "height": v.height if v else None,
             "has_video": v is not None}
    cache[key] = facts
    return facts


def _entry(src: Path, *, name: str, kind: str, origin: str, duration: float | None,
           width: int | None, height: int | None) -> dict[str, Any]:
    try:
        added = src.stat().st_mtime
    except OSError:
        added = 0.0
    return {"id": media_id(src), "src": str(src), "name": name, "kind": kind,
            "origin": origin, "duration": duration, "width": width, "height": height,
            "added": added}


def _scan_ingests(uploads: Path, represented: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ij in sorted(uploads.rglob("ingest.json")):
        if "stickers" in ij.relative_to(uploads).parts:
            continue
        try:
            data = json.loads(ij.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        norm_s = data.get("normalized")
        if not norm_s:
            continue
        norm = Path(norm_s)
        if not norm.exists():
            # A moved workdir: the JSON's absolute path is stale, the file sits beside it.
            norm = ij.parent / norm.name
            if not norm.exists():
                continue
        raw = data.get("src")
        represented.add(str(norm.resolve()))
        if raw:
            represented.add(str(Path(raw).resolve()))
            represented.add(str((ij.parent / Path(raw).name).resolve()))
        probe = data.get("probe") or {}
        vs = _first_video_stream(probe)
        name = data.get("display_name") or (_display_from_disk(Path(raw).name) if raw else norm.name)
        still = bool(data.get("still"))
        if still:
            # QA-090: the source runs for minutes so the clip can be extended;
            # what the bin shows (and a drag onto the timeline uses) is the
            # length a photo is placed at.
            from .ingest.still import STILL_DEFAULT_SECONDS
            duration = STILL_DEFAULT_SECONDS
        else:
            duration = probe.get("duration")
        # QA-089: a clamped source is listed at its REAL size — the 1080p
        # file is only the editing proxy; exports render from the original.
        dims = data.get("clamped_from") or [vs.get("width"), vs.get("height")]
        entry = _entry(norm.resolve(), name=name, kind="video", origin="upload",
                       duration=duration, width=dims[0], height=dims[1])
        if still:
            entry["still"] = True
        out.append(entry)
    return out


def _scan_loose(uploads: Path, represented: set[str], cache: dict[str, Any],
                names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sub in sorted(_LOOSE_DIRS):
        base = uploads / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            ext = f.suffix.lower()
            if not f.is_file() or ext not in (_VIDEO_EXTS | _AUDIO_EXTS):
                continue
            key = str(f.resolve())
            # A voice-over's raw browser recording is the input to its .m4a,
            # never used on the timeline itself.
            if key in represented or f.name.startswith("raw_"):
                continue
            facts = _probe_facts(f, cache)
            if facts is None:
                continue
            kind = "video" if facts.get("has_video") and ext not in _AUDIO_EXTS else "audio"
            origin = {"audio": "audio", "vo": "voiceover", "imported": "imported"}[sub]
            represented.add(key)
            name = (names or {}).get(key) or _display_from_disk(f.name)
            out.append(_entry(f.resolve(), name=name, kind=kind, origin=origin,
                              duration=facts.get("duration"), width=facts.get("width"),
                              height=facts.get("height")))
    return out


def list_media(session_dir: Path, edl) -> list[dict[str, Any]]:
    """Every media item in the project's library, oldest import first, each
    with `uses` (timeline clips referencing it) and `clip_ids`."""
    session_dir = Path(session_dir)
    uploads = session_dir / "uploads"
    state = _load_state(session_dir)
    cache = dict(state["probe"])
    represented: set[str] = set()
    items: list[dict[str, Any]] = []
    if uploads.is_dir():
        items += _scan_ingests(uploads, represented)
        items += _scan_loose(uploads, represented, cache, state["names"])
    uses = _uses(edl)
    by_src = {it["src"]: it for it in items}
    # On the timeline but not found by the scan: derived renders, legacy paths.
    for track in getattr(edl, "tracks", []) or []:
        if track.type not in _MEDIA_TRACK_TYPES:
            continue
        for clip in track.clips:
            src = getattr(clip, "src", None)
            if not src:
                continue
            p = Path(src).resolve()
            if str(p) in by_src:
                continue
            kind = "audio" if track.type in ("music", "vo", "audio") or p.suffix.lower() in _AUDIO_EXTS else "video"
            from .media_offline import display_name_for
            if not p.exists():
                # QA-095: missing media is LISTED (offline, with its real
                # name and a Relink action), never silently dropped.
                it = _entry(p, name=display_name_for(session_dir, src), kind=kind,
                            origin="timeline", duration=None, width=None, height=None)
                it["missing"] = True
                items.append(it)
                by_src[it["src"]] = it
                continue
            facts = _probe_facts(p, cache) or {}
            it = _entry(p, name=display_name_for(session_dir, str(p)), kind=kind, origin="timeline",
                        duration=facts.get("duration"), width=facts.get("width"), height=facts.get("height"))
            items.append(it)
            by_src[it["src"]] = it
    removed = state["removed"]
    visible: list[dict[str, Any]] = []
    for it in items:
        ids = uses.get(it["src"], [])
        sig = _signature(Path(it["src"]))
        gone = it["src"] in removed and sig is not None and sig[0] <= int(removed[it["src"]])
        # Something on the timeline is always listed, even after a bin removal
        # (an Undo can bring its clips back after the item was removed).
        if gone and not ids:
            continue
        visible.append({"missing": False, **it, "uses": len(ids), "clip_ids": ids})
    if cache != state["probe"]:
        # Drop cache rows for files that no longer exist, then persist — merged
        # into the file as it is NOW, so a concurrent removal survives.
        try:
            with _STATE_LOCK:
                fresh = _load_state(session_dir)
                fresh["probe"] = {k: v for k, v in cache.items() if Path(k).exists()}
                _save_state(session_dir, fresh)
        except OSError:
            pass  # a read-only session still lists; it just probes again next time
    visible.sort(key=lambda it: (it["added"], it["name"]))
    return visible


class MediaInUse(Exception):
    """The item still has clips on the timeline."""

    def __init__(self, item: dict[str, Any]):
        super().__init__(f"{item['name']} is used by {item['uses']} clip(s) on the timeline")
        self.item = item


def remove_media(session_dir: Path, edl, item_id: str) -> dict[str, Any]:
    """Take one item out of the bin (bytes stay on disk). Raises KeyError for an
    unknown id and MediaInUse while timeline clips still reference it."""
    session_dir = Path(session_dir)
    item = next((it for it in list_media(session_dir, edl) if it["id"] == item_id), None)
    if item is None:
        raise KeyError(item_id)
    if item["uses"]:
        raise MediaInUse(item)
    sig = _signature(Path(item["src"]))
    with _STATE_LOCK:
        state = _load_state(session_dir)
        state["removed"] = {**state["removed"], item["src"]: sig[0] if sig else 0}
        _save_state(session_dir, state)
    return item
