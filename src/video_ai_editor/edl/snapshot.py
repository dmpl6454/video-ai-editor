"""EDL store with snapshot-based undo/redo, persisted per session."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator, Literal
from .schema import EDL, Clip, empty_edl
from .ops_log import Op, OpsLog

# How this store's EDL came to be, recorded by _load_edl(). Callers that export
# the project (storage_project.save_project) must not treat "corrupt" like
# "new": both present as an empty timeline, but one of them means a real
# project failed to load and writing a .vae over it is a second data-loss path
# layered on the first (QA round 5, VAI-02).
LoadState = Literal["new", "clean", "recovered", "corrupt"]

# Same logger the request middleware uses, so a durability failure lands in the
# structured log next to the request that triggered it. Fetched by name rather
# than importing api.hardening — edl/ must not depend on the web layer.
_log = logging.getLogger("video_ai_editor")


class EDLStore:
    """Holds the current EDL + ops log + recent snapshots for undo/redo.

    Persistence: writes `edl.json`, `ops.json`, and last N snapshots to the session dir
    on every commit so a crash recovers the project.
    """

    # QA-046: undo depth is bounded by COUNT and by BYTES, not by 30. A
    # snapshot is the whole EDL (2-12 KB for an ordinary timeline, a few
    # hundred KB for a long captioned one), so 30 was ~29 undo steps while
    # History listed every op and the Undo button stayed enabled. The count
    # cap keeps a pathological session from holding thousands of files; the
    # byte budget keeps a heavy one from filling the disk. The newest
    # MIN_UNDO_SNAPSHOTS always survive the budget, so at least one step of
    # undo exists whatever the timeline weighs. `undo_depth` is the honest
    # number the UI binds to.
    MAX_UNDO = 500
    UNDO_DISK_BUDGET_BYTES = 64 * 1024 * 1024
    MIN_UNDO_SNAPSHOTS = 2

    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir = self.dir / "snapshots"
        self.snapshots_dir.mkdir(exist_ok=True)
        self.load_state: LoadState = "new"
        self.edl: EDL = self._load_edl()
        self.ops: OpsLog = self._load_ops()
        self._redo_stack: list[EDL] = self._load_redo_stack()
        # The op each redo entry re-applies, aligned with `_redo_stack` (None
        # for an entry written before Final QA — it redoes as a plain "Redo").
        self._redo_ops: list[Op | None] = self._load_redo_ops()
        # >0 while inside batch(): commit() then records nothing (see batch).
        self._batch_depth = 0
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

    @property
    def redo_ops_path(self) -> Path:
        return self.dir / "redo_ops.json"

    def _load_redo_ops(self) -> list[Op | None]:
        """The undone ops, aligned with the redo stack (Final QA). A stack
        written before this file existed — or a file that no longer lines
        up — pads the OLDEST entries with None rather than guessing."""
        ops: list[Op | None] = []
        if self.redo_ops_path.exists():
            try:
                raw = json.loads(self.redo_ops_path.read_text(encoding="utf-8"))
                ops = [Op.model_validate(o) if o else None for o in raw]
            except Exception:
                ops = []
        n = len(self._redo_stack)
        ops = ops[-n:] if n else []
        return [None] * (n - len(ops)) + ops

    def _redo_files(self, stack: list[EDL], ops: list[Op | None]) -> list[tuple[Path, str]]:
        """The redo_stack.json / redo_ops.json payloads for `stack`, or [] for
        an empty stack (whose files `_drop_empty_redo_files` removes)."""
        if not stack:
            return []
        return [(self.redo_stack_path,
                 json.dumps([e.model_dump(by_alias=True, mode="json") for e in stack])),
                (self.redo_ops_path,
                 json.dumps([o.model_dump(mode="json") if o else None for o in ops]))]

    def _drop_empty_redo_files(self) -> None:
        # Nothing to redo — remove the file rather than persist "[]" so a
        # stale file left behind doesn't need special-casing on load.
        if not self._redo_stack:
            self.redo_stack_path.unlink(missing_ok=True)
            self.redo_ops_path.unlink(missing_ok=True)

    def _save_redo_stack(self) -> None:
        files = self._redo_files(self._redo_stack, getattr(self, "_redo_ops", []))
        if files:
            self._publish(self._stage(files))
        else:
            self._drop_empty_redo_files()

    @staticmethod
    def _stage(files: list[tuple[Path, str]]) -> list[tuple[Path, Path]]:
        """Write each `(dst, text)` to a temp beside `dst` — where ENOSPC
        lands — and return the `(temp, dst)` pairs for `_publish`. A failed
        write removes every temp written so far and re-raises: nothing of
        the real files has changed."""
        temps: list[tuple[Path, Path]] = []
        try:
            for dst, text in files:
                tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
                temps.append((tmp, dst))
                tmp.write_text(text, encoding="utf-8")
        except OSError:
            for tmp, _dst in temps:
                tmp.unlink(missing_ok=True)
            raise
        return temps

    @staticmethod
    def _publish(temps: list[tuple[Path, Path]]) -> None:
        for tmp, dst in temps:
            os.replace(tmp, dst)

    @property
    def redo_available(self) -> bool:
        return bool(self._redo_stack)

    @property
    def undo_depth(self) -> int:
        """How many ⌘Z steps are actually available (QA-046).

        One per retained snapshot beyond the oldest, stopping at the project's
        own "init" op: create_session commits an empty timeline as op 1, and
        undoing THAT emptied History to "No edits yet" on a fresh project
        without changing anything the user made. A store whose ops log is
        shorter than its snapshots (an unreadable ops.json was reset) keeps
        the snapshot count — the snapshots are the state, the log is history.
        """
        n = max(0, len(self._snapshot_files()) - 1)
        depth = 0
        for op in reversed(self.ops.ops):
            if depth >= n or op.tool == "init":
                return depth
            depth += 1
        return n

    @property
    def undo_available(self) -> bool:
        return self.undo_depth > 0

    def _snapshot_files(self) -> list[Path]:
        return sorted(self.snapshots_dir.glob("*.json"))

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
            self.load_state = "new"
            return empty_edl()
        try:
            edl = EDL.model_validate_json(self.edl_path.read_text(encoding="utf-8"))
            self.load_state = "clean"
            return edl
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
                self.load_state = "recovered"
                return edl
            except Exception:
                continue
        _log.error("no valid snapshot for %s — starting from an empty timeline",
                   self.dir.name)
        self.load_state = "corrupt"
        return empty_edl()

    @property
    def is_data_loss_state(self) -> bool:
        """True when this store is showing an empty timeline that stands in for
        a project that failed to load.

        Deliberately AND-ed with "still empty": once the user has added media
        the session is a real project again and exporting it is legitimate, so
        a permanent latch would just trap them. Consumed by
        `storage_project.save_project`, which must refuse rather than write a
        silent empty `.vae` over the user's only other copy.
        """
        if self.load_state != "corrupt":
            return False
        return not any(isinstance(c, Clip) for t in self.edl.tracks for c in t.clips)

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

    def commit(self, tool: str, args: dict, summary: str, by: str = "user", *,
               record_unchanged: bool = False) -> None:
        """Persist current EDL after a mutation; record op; manage undo snapshots.

        An unchanged tree records nothing (QA-130) unless `record_unchanged`:
        the one caller that sets it is the Prompt Editor's
        make_shorts(save_as_sessions) run, whose op is the only provenance
        record of the child sessions it created.
        """
        if self._batch_depth:
            # Inside batch(): keep the duration honest for the next sub-tool's
            # timeline math, but no snapshot, no op, no redo clear — the
            # enclosing composite commits once when the batch closes.
            self.edl.recompute_duration()
            return None
        prev_hash = self._last_hash()
        self.edl.recompute_duration()
        new_hash = self.edl.hash()
        if prev_hash and new_hash == prev_hash and not record_unchanged:
            # QA-130: nothing changed (a second split at the same playhead, a
            # clip dropped back on its own start, a value set to what it
            # already was). Recording it gave History a step whose ⌘Z
            # restored an identical timeline — a "dead" undo — and cleared
            # Redo for no reason. The hash covers the whole tree, so equal
            # hashes mean equal state; `op` is then null in the /dispatch
            # answer, which is how callers tell "no change".
            return None

        payload = self.edl.to_json()
        try:
            self._assert_reloadable(payload, tool)
        except ValueError:
            self._restore_last_good()
            raise
        # Final QA (disk full): all-or-nothing. Every file is written to a
        # temp first — that is where ENOSPC lands — and only then moved into
        # place; a failed write drops the temps, takes the op back and puts
        # the in-memory tree back to the last good edl.json. It used to keep
        # the failed edit in memory: /head, /edl, preview and export showed
        # it, and the next edit's single Undo removed both.
        snap = self.snapshots_dir / f"{len(self.ops.ops) + 1:05d}_{new_hash}.json"
        op = self.ops.append(tool, args, summary, prev_hash, new_hash, by=by)
        try:
            temps = self._stage([(snap, payload), (self.edl_path, payload),
                                 (self.ops_path, self.ops.model_dump_json())])
        except OSError:
            self.ops.pop()
            self._restore_last_good()
            raise
        self._publish(temps)
        self._prune_snapshots()
        self._redo_stack.clear()
        self._redo_ops.clear()
        self._save_redo_stack()
        return op

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Fold every commit() made inside the block into none at all, so the
        caller can persist a whole composite edit with ONE commit — one op,
        one snapshot, one undo step.

        Why: undo() walks back exactly one snapshot per commit. Composites
        such as remove_silences let each inner cut_range commit and then
        added a "summary" commit whose snapshot was identical to the one
        before it — so the first ⌘Z did nothing visible and a clip with ten
        silences needed eleven undos. Re-entrant: nested batches
        (apply_template → apply_hook_stack → add_super_text) collapse into
        the outermost one. Nothing touches disk until the enclosing commit,
        so a crash mid-composite leaves the pre-composite state on disk
        rather than a half-applied one.

        An exception escaping the block rolls the IN-MEMORY tree back too.
        Deferring persistence alone was not enough: this store is cached
        process-wide (main._STORES) and nothing reloads it on error, so a
        remove_silences that died on its seventh cut left six cuts in
        `store.edl` with no op behind them — invisible to undo, and folded
        into whatever the user committed NEXT ("add_text" would snapshot the
        six cuts plus the text as one step). Before batch() existed each
        inner commit kept memory and disk consistent; this keeps that
        guarantee. Every level snapshots its own entry state, so a nested
        block that fails and is caught by the enclosing composite (the
        apply_template → apply_hook_stack pattern, which logs the failure and
        carries on) loses exactly its own edits and nothing above it. The
        deep copy is per batch entry, not per inner commit — composites nest
        two or three deep at most.
        """
        before = self.edl.model_copy(deep=True)
        self._batch_depth += 1
        try:
            yield
        except BaseException:
            self.edl = before
            self.edl.recompute_duration()
            raise
        finally:
            self._batch_depth -= 1

    def _restore_last_good(self) -> None:
        """Put the in-memory tree back to what is durable on disk (a refused
        or failed commit): edl.json, else the newest readable snapshot. A
        store with neither keeps its tree — there is nothing better to hold."""
        for src in [self.edl_path, *reversed(self._snapshot_files())]:
            try:
                self.edl = EDL.model_validate_json(src.read_text(encoding="utf-8"))
                self.edl.recompute_duration()
                return
            except (OSError, ValueError):
                continue

    def _assert_reloadable(self, payload: str, tool: str) -> None:
        """Refuse to persist an EDL that could not be read back.

        `commit()` is the single durability point, so it is also the single
        place where "this tree is invalid" can still be reported to the caller
        that caused it. Without this the write succeeds, the tree is fine
        in-memory for the rest of the process, and the damage only surfaces on
        the NEXT load — with every retained snapshot poisoned by then (VAI-01).

        Uses the EXACT call `_load_edl` uses, so the check means precisely
        "would a reload of this session survive?" — not an approximation of it.
        Measured at ~0.65 ms on a 130-clip timeline, against the two disk
        writes it guards.
        """
        try:
            EDL.model_validate_json(payload)
        except Exception as e:
            _log.error("refusing to persist an invalid EDL after %s in %s: %s",
                       tool, self.dir.name, e)
            raise ValueError(
                f"'{tool}' produced an EDL that cannot be read back, so it was "
                f"not saved (the timeline is unchanged on disk): {e}"
            ) from e

    def _last_hash(self) -> str:
        return self.ops.last().edl_hash_after if self.ops.last() else ""

    def _snapshot(self, h: str, payload: str | None = None) -> None:
        # Named by op seq + 1 so the initial snapshot (seeded by __init__ as
        # 00000) survives the first commit.
        snap = self.snapshots_dir / f"{len(self.ops.ops) + 1:05d}_{h}.json"
        snap.write_text(payload if payload is not None else self.edl.to_json(),
                        encoding="utf-8")
        self._prune_snapshots()

    def _prune_snapshots(self) -> None:
        """Drop the oldest snapshots past MAX_UNDO or past the byte budget."""
        snaps = self._snapshot_files()
        keep = min(len(snaps), self.MAX_UNDO)
        total = 0
        kept = 0
        for p in reversed(snaps[-keep:] if keep else []):
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            if kept >= self.MIN_UNDO_SNAPSHOTS and total + size > self.UNDO_DISK_BUDGET_BYTES:
                break
            total += size
            kept += 1
        for old in snaps[:len(snaps) - kept]:
            old.unlink(missing_ok=True)

    #: What Undo says when the step it would restore cannot be read.
    DAMAGED_HISTORY_MSG = ("Earlier undo history is damaged, so Undo stops here. "
                           "The timeline is unchanged.")

    def _end_history_at(self, snaps: list[Path], bad: Path, err: Exception) -> None:
        """Drop the unreadable snapshot `bad` and every older one: undo
        history now ends cleanly at the current step (`undo_depth` 0)."""
        _log.warning("undo snapshot %s of %s is unreadable (%s: %s) — undo history "
                     "ends here", bad.name, self.dir.name, type(err).__name__,
                     str(err).splitlines()[0] if str(err) else "")
        for old in snaps:
            if old.name <= bad.name:
                old.unlink(missing_ok=True)

    def undo(self) -> bool:
        """Step back one snapshot. All-or-nothing (final sweep 2 r2, like
        `commit`): the target is read and validated first, every file is
        written to a temp, and memory changes only once all of them are in
        place. It used to push the redo stack and save it before anything
        could fail — on a full disk that left redo_stack.json empty and an
        extra phantom Redo entry in memory per press; an unreadable snapshot
        failed every press with a raw pydantic dump and grew Redo each time."""
        if not self.undo_available:
            return False
        snaps = self._snapshot_files()
        prev = snaps[-2]
        try:
            prev_edl = EDL.model_validate_json(prev.read_text(encoding="utf-8"))
        except ValueError as e:        # pydantic ValidationError, bad UTF-8
            self._end_history_at(snaps[:-1], prev, e)
            raise ValueError(self.DAMAGED_HISTORY_MSG) from None
        # The current state goes onto the redo stack with the op it came from
        # (Final QA: redo used to re-append a generic "Redo" and lose the name).
        stack = [*self._redo_stack, self.edl.model_copy(deep=True)]
        redo_ops = [*self._redo_ops, self.ops.last()]
        undone = self.ops.pop()
        try:
            temps = self._stage([(self.edl_path, prev_edl.to_json()),
                                 (self.ops_path, self.ops.model_dump_json()),
                                 *self._redo_files(stack, redo_ops)])
        except OSError:
            if undone is not None:
                self.ops.ops.append(undone)
            raise
        self._publish(temps)
        self.edl, self._redo_stack, self._redo_ops = prev_edl, stack, redo_ops
        # Remove the snapshot we just left
        snaps[-1].unlink(missing_ok=True)
        return True

    def redo(self) -> bool:
        """Replay the most recently undone op without clearing the rest of
        the redo stack — naive `commit("redo")` would `clear()` the stack
        and limit redo to a single step. All-or-nothing, as `undo`."""
        if not self._redo_stack:
            return False
        target = self._redo_stack[-1].model_copy(deep=True)
        op = self._redo_ops[-1] if self._redo_ops else None
        stack, redo_ops = self._redo_stack[:-1], self._redo_ops[:-1]
        target.recompute_duration()
        new_hash = target.hash()
        prev_hash = self._last_hash()
        payload = target.to_json()
        # Named by op seq + 1, as `_snapshot` names it.
        snap = self.snapshots_dir / f"{len(self.ops.ops) + 1:05d}_{new_hash}.json"
        if op is not None:
            # The redone op itself, so History and the preview's changed-range
            # logic see what came back ("Animation — In Zoom In", not "Redo").
            self.ops.append(op.tool, op.args, op.summary, prev_hash, new_hash,
                            by=op.by, redo=True)
        else:
            self.ops.append("redo", {}, "Redo", prev_hash, new_hash, by="user", redo=True)
        try:
            temps = self._stage([(snap, payload), (self.edl_path, payload),
                                 (self.ops_path, self.ops.model_dump_json()),
                                 *self._redo_files(stack, redo_ops)])
        except OSError:
            self.ops.pop()
            raise
        self._publish(temps)
        self.edl, self._redo_stack, self._redo_ops = target, stack, redo_ops
        self._drop_empty_redo_files()
        self._prune_snapshots()
        return True
