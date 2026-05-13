"""EDL store with snapshot-based undo/redo, persisted per session."""
from __future__ import annotations
import json
from pathlib import Path
from .schema import EDL, empty_edl
from .ops_log import OpsLog


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
        self._redo_stack: list[EDL] = []
        # Seed snapshot 0 = initial state so undo can walk back to it.
        # Without this, undo can never restore "before any ops were applied"
        # because the first snapshot is taken inside commit() AFTER the first op.
        if not list(self.snapshots_dir.glob("*.json")):
            initial_snap = self.snapshots_dir / f"00000_{self.edl.hash()}.json"
            initial_snap.write_text(self.edl.to_json())

    @property
    def edl_path(self) -> Path:
        return self.dir / "edl.json"

    @property
    def ops_path(self) -> Path:
        return self.dir / "ops.json"

    def _load_edl(self) -> EDL:
        if self.edl_path.exists():
            try:
                return EDL.model_validate_json(self.edl_path.read_text())
            except Exception:
                pass
        return empty_edl()

    def _load_ops(self) -> OpsLog:
        if self.ops_path.exists():
            try:
                return OpsLog.model_validate_json(self.ops_path.read_text())
            except Exception:
                pass
        return OpsLog()

    def commit(self, tool: str, args: dict, summary: str, by: str = "user") -> None:
        """Persist current EDL after a mutation; record op; manage undo snapshots."""
        prev_hash = self._last_hash()
        self.edl.recompute_duration()
        new_hash = self.edl.hash()

        self._snapshot(new_hash)
        self.edl_path.write_text(self.edl.to_json())

        op = self.ops.append(tool, args, summary, prev_hash, new_hash, by=by)
        self.ops_path.write_text(self.ops.model_dump_json())
        self._redo_stack.clear()
        return op

    def _last_hash(self) -> str:
        return self.ops.last().edl_hash_after if self.ops.last() else ""

    def _snapshot(self, h: str) -> None:
        # Keep last MAX_UNDO snapshots; named by op seq + 1 so the initial
        # snapshot (seeded by __init__ as 00000) survives the first commit.
        snap = self.snapshots_dir / f"{len(self.ops.ops) + 1:05d}_{h}.json"
        snap.write_text(self.edl.to_json())
        snaps = sorted(self.snapshots_dir.glob("*.json"))
        for old in snaps[:-self.MAX_UNDO]:
            old.unlink(missing_ok=True)

    def undo(self) -> bool:
        snaps = sorted(self.snapshots_dir.glob("*.json"))
        if len(snaps) < 2:
            return False
        # Push current onto redo stack
        self._redo_stack.append(self.edl.model_copy(deep=True))
        # Restore previous snapshot
        prev = snaps[-2]
        self.edl = EDL.model_validate_json(prev.read_text())
        self.edl_path.write_text(self.edl.to_json())
        # Pop the last op (it's now undone)
        if self.ops.pop():
            self.ops_path.write_text(self.ops.model_dump_json())
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
        self.edl.recompute_duration()
        new_hash = self.edl.hash()
        self._snapshot(new_hash)
        self.edl_path.write_text(self.edl.to_json())
        self.ops.append("redo", {}, "Redo", "", new_hash, by="user")
        self.ops_path.write_text(self.ops.model_dump_json())
        return True
