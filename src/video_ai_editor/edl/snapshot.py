"""EDL store with snapshot-based undo/redo, persisted per session."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from .schema import EDL, empty_edl
from .ops_log import OpsLog

# Same logger the request middleware uses, so a durability failure lands in the
# structured log next to the request that triggered it. Fetched by name rather
# than importing api.hardening — edl/ must not depend on the web layer.
_log = logging.getLogger("video_ai_editor")


class EDLStore:
    """Holds the current EDL + ops log + recent snapshots for undo/redo.

    Persistence: writes `edl.json`, `ops.json`, and last N snapshots to the session dir
    on every commit so a crash recovers the project.
    """

    MAX_UNDO = 30

    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir = self.dir / "snapshots"
        self.snapshots_dir.mkdir(exist_ok=True)
        self.edl: EDL = self._load_edl()
        self.ops: OpsLog = self._load_ops()
        self._redo_stack: list[EDL] = self._load_redo_stack()
        # Seed snapshot 0 = initial state so undo can walk back to it.
        # Without this, undo can never restore "before any ops were applied"
        # because the first snapshot is taken inside commit() AFTER the first op.
        if not list(self.snapshots_dir.glob("*.json")):
            initial_snap = self.snapshots_dir / f"00000_{self.edl.hash()}.json"
            initial_snap.write_text(self.edl.to_json(), encoding="utf-8")

    @property
    def redo_stack_path(self) -> Path:
        return self.dir / "redo_stack.json"

    def _load_redo_stack(self) -> list[EDL]:
        # Redo used to be an in-memory-only list — it silently emptied on a
        # process restart or eviction from main.py's LRU _STORES cache (the
        # session itself survives fine via edl.json; only "what can Redo
        # bring back" was lost), which read as "Redo does nothing" with no
        # explanation. Persisting it the same way edl.json/ops.json already
        # are closes that gap.
        if not self.redo_stack_path.exists():
            return []
        try:
            raw = json.loads(self.redo_stack_path.read_text(encoding="utf-8"))
            return [EDL.model_validate(item) for item in raw]
        except Exception:
            return []

    def _save_redo_stack(self) -> None:
        if self._redo_stack:
            payload = json.dumps([e.model_dump(by_alias=True, mode="json") for e in self._redo_stack])
            self.redo_stack_path.write_text(payload, encoding="utf-8")
        else:
            # Nothing to redo — remove the file rather than persist "[]" so a
            # stale file left behind doesn't need special-casing on load.
            self.redo_stack_path.unlink(missing_ok=True)

    @property
    def redo_available(self) -> bool:
        return bool(self._redo_stack)

    @property
    def edl_path(self) -> Path:
        return self.dir / "edl.json"

    @property
    def ops_path(self) -> Path:
        return self.dir / "ops.json"

    def _load_edl(self) -> EDL:
        """Load edl.json, recovering from the newest VALID snapshot if it's broken.

        This used to `except Exception: pass` straight into `empty_edl()`. Since
        the filesystem IS the database, that turned one unparseable float into
        "my entire project vanished" — and worse, the next `commit()` would
        overwrite the damaged-but-recoverable file with the empty timeline, so
        the data was then genuinely gone. There are already numbered, hashed
        snapshots on disk; falling back to the newest one that parses turns a
        total loss into losing at most one edit.
        """
        if not self.edl_path.exists():
            return empty_edl()
        try:
            return EDL.model_validate_json(self.edl_path.read_text(encoding="utf-8"))
        except Exception as e:
            _log.error("edl.json is unreadable (%s: %s) — attempting snapshot "
                       "recovery for %s", type(e).__name__, e, self.dir.name)
            # Keep the bad file for forensics instead of letting commit() clobber it.
            try:
                bad = self.edl_path.with_suffix(".corrupt.json")
                if not bad.exists():
                    bad.write_text(self.edl_path.read_text(encoding="utf-8", errors="replace"),
                                   encoding="utf-8")
            except OSError:
                pass
        snaps = sorted(self.snapshots_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True) \
            if self.snapshots_dir.exists() else []
        for snap in snaps:
            try:
                edl = EDL.model_validate_json(snap.read_text(encoding="utf-8"))
                _log.warning("recovered %s from snapshot %s", self.dir.name, snap.name)
                return edl
            except Exception:
                continue
        _log.error("no valid snapshot for %s — starting from an empty timeline",
                   self.dir.name)
        return empty_edl()

    def _load_ops(self) -> OpsLog:
        if self.ops_path.exists():
            try:
                return OpsLog.model_validate_json(self.ops_path.read_text(encoding="utf-8"))
            except Exception as e:
                # The ops log is history, not state — an empty one is survivable,
                # but it must not be silent, or "my undo history disappeared" has
                # no trail at all.
                _log.error("ops.json is unreadable (%s: %s) for %s — starting a "
                           "fresh log", type(e).__name__, e, self.dir.name)
        return OpsLog()

    def commit(self, tool: str, args: dict, summary: str, by: str = "user") -> None:
        """Persist current EDL after a mutation; record op; manage undo snapshots."""
        prev_hash = self._last_hash()
        self.edl.recompute_duration()
        new_hash = self.edl.hash()

        self._snapshot(new_hash)
        self.edl_path.write_text(self.edl.to_json(), encoding="utf-8")

        op = self.ops.append(tool, args, summary, prev_hash, new_hash, by=by)
        self.ops_path.write_text(self.ops.model_dump_json(), encoding="utf-8")
        self._redo_stack.clear()
        self._save_redo_stack()
        return op

    def _last_hash(self) -> str:
        return self.ops.last().edl_hash_after if self.ops.last() else ""

    def _snapshot(self, h: str) -> None:
        # Keep last MAX_UNDO snapshots; named by op seq + 1 so the initial
        # snapshot (seeded by __init__ as 00000) survives the first commit.
        snap = self.snapshots_dir / f"{len(self.ops.ops) + 1:05d}_{h}.json"
        snap.write_text(self.edl.to_json(), encoding="utf-8")
        snaps = sorted(self.snapshots_dir.glob("*.json"))
        for old in snaps[:-self.MAX_UNDO]:
            old.unlink(missing_ok=True)

    def undo(self) -> bool:
        snaps = sorted(self.snapshots_dir.glob("*.json"))
        if len(snaps) < 2:
            return False
        # Push current onto redo stack
        self._redo_stack.append(self.edl.model_copy(deep=True))
        self._save_redo_stack()
        # Restore previous snapshot
        prev = snaps[-2]
        self.edl = EDL.model_validate_json(prev.read_text(encoding="utf-8"))
        self.edl_path.write_text(self.edl.to_json(), encoding="utf-8")
        # Pop the last op (it's now undone)
        if self.ops.pop():
            self.ops_path.write_text(self.ops.model_dump_json(), encoding="utf-8")
        # Remove the snapshot we just left
        snaps[-1].unlink(missing_ok=True)
        return True

    def redo(self) -> bool:
        """Replay the most recently undone op without clearing the rest of
        the redo stack — naive `commit("redo")` would `clear()` the stack
        and limit redo to a single step."""
        if not self._redo_stack:
            return False
        self.edl = self._redo_stack.pop()
        self._save_redo_stack()
        self.edl.recompute_duration()
        new_hash = self.edl.hash()
        self._snapshot(new_hash)
        self.edl_path.write_text(self.edl.to_json(), encoding="utf-8")
        self.ops.append("redo", {}, "Redo", "", new_hash, by="user")
        self.ops_path.write_text(self.ops.model_dump_json(), encoding="utf-8")
        return True
