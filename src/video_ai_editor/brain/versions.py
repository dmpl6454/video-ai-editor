"""Named versions on the snapshots `commit()` already wrote (spec §7.1).

`<session>/brain/versions.json` holds `{"versions": [{id, label, op_seq,
edl_hash, decisions_id, kind, created, pinned}]}`. Nothing is copied: a
version names the snapshot `snapshots/{op_seq + 1:05d}_{edl_hash}.json`
that the op it labels produced. `edl/snapshot.py` keeps a PINNED version's
snapshot when it prunes — moved to `snapshots/pinned/`, out of the undo
sequence, so an old pin never becomes an undo step — and an unpinned one
whose snapshot was pruned is shown "no longer restorable".

Restore is ONE op: the snapshot tree becomes `store.edl` and
`commit("restore_version", {id, label}, …)` records it — a History row, one
⌘Z, nothing deleted. The `restore_version` dispatch tool is EB2; this wave
F's route calls `restore()` directly (the brief's explicit exception to
"dispatch() is the only way" — the same footing as `EDLStore.undo`).
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..edl.ops_log import Op
from ..edl.schema import EDL
from ..edl.snapshot import EDLStore, version_row_ok
from . import store as _store


class UnknownVersion(ValueError):
    """`restore` was given an id `versions.json` does not list."""


class NotRestorable(ValueError):
    """The version exists but its snapshot was pruned."""


VERSION_KINDS = ("origin", "brain", "manual", "restore")
#: `versions.json` is read-modified-written; two recorders (a run's `done` and a manual label) must not share an id.
_WRITE_LOCK = threading.Lock()


@dataclass
class Version:
    id: str
    label: str
    op_seq: int
    edl_hash: str
    decisions_id: str | None
    kind: str
    created: float
    pinned: bool
    undone: bool = False
    restorable: bool = field(default=True, compare=False)

    def as_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("restorable", None)
        return d


def versions_path(session_dir: Path | str) -> Path:
    return _store.brain_dir(session_dir) / "versions.json"


def snapshot_name(v: Version) -> str:
    return f"{v.op_seq + 1:05d}_{v.edl_hash}.json"


def snapshot_file(session_dir: Path | str, v: Version) -> Path | None:
    """Where the version's snapshot is (live sequence or the pinned side), or None."""
    snaps = Path(session_dir) / "snapshots"
    for p in (snaps / snapshot_name(v), snaps / "pinned" / snapshot_name(v)):
        if p.is_file():
            return p
    return None


def _load(session_dir: Path | str) -> list[Version]:
    """The rows of `versions.json` this app could have written; anything else (a hand-made or imported file's
    strings, nulls, rows without a hash) is skipped, never raised."""
    raw = _store.read_json(versions_path(session_dir))
    rows = raw.get("versions") if isinstance(raw, dict) else None
    out: list[Version] = []
    for r in rows if isinstance(rows, list) else []:
        if not version_row_ok(r):
            continue
        try:
            out.append(Version(**{k: r.get(k) for k in ("id", "label", "op_seq", "edl_hash", "decisions_id",
                                                        "kind", "created", "pinned")},
                               undone=bool(r.get("undone", False))))
        except TypeError:
            continue
    return out


def _save(session_dir: Path | str, rows: list[Version]) -> None:
    _store.write_json(versions_path(session_dir), {"versions": [v.as_row() for v in rows]})


def list_versions(session_dir: Path | str) -> list[Version]:
    rows = _load(session_dir)
    for v in rows:
        v.restorable = snapshot_file(session_dir, v) is not None
    return rows


def next_label(session_dir: Path | str, name: str) -> str:
    return f"V{len(_load(session_dir)) + 1} {name}".strip()


def record(store: EDLStore, *, label: str, decisions_id: str | None, kind: str = "brain",
           pinned: bool = True) -> Version:
    """Label the CURRENT committed state. The snapshot the last op wrote must
    exist under the session (it does right after `commit`)."""
    if kind not in VERSION_KINDS:
        raise ValueError(f"version kind must be one of {VERSION_KINDS}")
    with _WRITE_LOCK:
        last = store.ops.last()
        rows = _load(store.dir)
        v = Version(id=f"v_{len(rows) + 1}", label=label, op_seq=(last.seq if last else -1), edl_hash=store.edl.hash(),
                    decisions_id=decisions_id, kind=kind, created=time.time(), pinned=bool(pinned))
        if snapshot_file(store.dir, v) is None:
            raise ValueError(f"no snapshot for the current state ({snapshot_name(v)}) — commit first")
        _save(store.dir, [*rows, v])
    return v


def record_after_run(store: EDLStore, *, decisions_id: str | None, title: str, kind: str = "brain") -> Version:
    """What the service records after an applied brain run: "V1 Reel". The label is computed here, at record
    time, from the versions file — the same plan applied twice is "V1 Reel" then "V2 Reel"."""
    return record(store, label=next_label(store.dir, title), decisions_id=decisions_id, kind=kind, pinned=True)


def current_version(store: EDLStore) -> Version | None:
    h = store.edl.hash()
    return next((v for v in reversed(_load(store.dir)) if v.edl_hash == h), None)


def restore(store: EDLStore, version_id: str) -> Op | None:
    """The snapshot tree becomes the live EDL through ONE commit. Returns the
    op, or None when the live tree already IS that version (no dead undo step)."""
    rows = _load(store.dir)
    v = next((r for r in rows if r.id == version_id), None)
    if v is None:
        raise UnknownVersion(f"version {version_id} does not exist")
    p = snapshot_file(store.dir, v)
    if p is None:
        raise NotRestorable(f"{v.label} is no longer restorable (its snapshot was pruned)")
    edl = EDL.model_validate_json(p.read_text(encoding="utf-8"))
    if edl.hash() == store.edl.hash():
        return None
    store.edl = edl
    store.edl.recompute_duration()
    store.commit("restore_version", {"id": v.id, "label": v.label}, f"Restored {v.label}")
    return store.ops.last()


__all__ = ["VERSION_KINDS", "Version", "UnknownVersion", "NotRestorable", "versions_path", "snapshot_name", "snapshot_file", "list_versions",
           "next_label", "record", "record_after_run", "current_version", "restore"]
