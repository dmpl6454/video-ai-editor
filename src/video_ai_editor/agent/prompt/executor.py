"""Run a validated Plan inside one `EDLStore.batch()` (spec §4.2).

The contract, in the order things happen on the run thread:

  1. take `api.locks.session_lock(sid)` and register the run so `/dispatch`
     answers 409 instead of queueing behind a caption pass;
  2. re-resolve the store INSIDE the lock (the LRU `main._STORES` may have
     evicted the object the route captured);
  3. wait ≤ 30 s for an in-flight upload transcript when the plan needs one;
  4. `validate_plan` again — THE security boundary — and then, per step, the
     executor's own last-line `guard_step` (deny list, unknown args, path
     rule, no-download rule). Two checks, one policy: the validator is the
     complete rule set; the guard is the subset that must hold even if a
     validator bug lets something through, and it is what refuses a plan when
     the validator module itself is missing;
  5. snapshot the files a step may rewrite outside the EDL (`ingest.json`,
     `transcript.json`) — `batch()` only rolls back the in-memory tree;
  6. `with store.batch():` resolve clip sentinels against the LIVE edl,
     dispatch, emit `step` / `tool_use` / `tool_result`; a required step that
     raises aborts the whole plan (rollback + snapshot restore); an optional
     one is recorded `skipped`;
  7. ONE `store.commit("prompt", …)` — one op, one undo step — but only when
     the tree actually changed (a read-only or all-no-effect plan commits
     nothing);
  8. verify (verify.py), `op`, the summary `text_delta`, `done`.

Execution outlives the SSE stream: every event goes to a `RunBus`
(runlog.py); the service subscribes. A disconnect only unsubscribes.
Cancellation is `cancel_event` alone, checked between steps and handed to
handlers that take it.

WHY dispatch gets `dict(step.args)`: `dispatch._validate_tool_args` rewrites
enum spellings in place; the Plan must stay the object that was validated
(tests assert it is unchanged after `run_plan`).
"""
from __future__ import annotations

import inspect
import importlib
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...api import locks
from ...api.jobs import JobCancelled
from ...edl import EDLStore
from ...edl.schema import EDL, Clip
from .. import path_args as _path_args
from ..tools import input_schema_for
from .facts import TimelineFacts
from .runlog import RunBus, RunLog, RunRecord, new_run_id
from .schema import CLIP_SENTINELS, PLAN_DENY, SEAM_SENTINEL, Plan, Step
from .service import SNAPSHOT_DIR, TRANSCRIPT_WAIT_S

_D = importlib.import_module("video_ai_editor.agent.dispatch")

Emit = Callable[[dict[str, Any]], None]

#: Args (beyond each tool's advertised schema) a plan may carry — the
#: `EXTRA_TOOL_SCHEMAS` / `EXTRA_ARGS` of spec §1.3. `apply_hook_stack` has
#: no schema in tools.py at all, so its whole arg set is listed here.
EXTRA_PLAN_ARGS: dict[str, frozenset[str]] = {
    "apply_hook_stack": frozenset({"text", "duration", "visual", "audio"}),
    "auto_reframe": frozenset({"subject_track"}),
    "apply_template": frozenset({"with_hook_stack"}),
    "add_caption_track": frozenset({"chunk_size"}),
    "add_music": frozenset({"loop"}),
}

#: `add_music(volume_db)` and friends are bounded by the validator; the guard
#: only re-checks the two free strings that can trigger a DOWNLOAD, because
#: "no network without a yes" (§1.4) must hold at the last line too.
_TRANSLATED_TARGETS = ("hi", "hinglish", "es")

#: Rough wall-clock per tool for the synthetic progress bar on handlers that
#: report none (§4.2). Seconds for a ~60 s source; scaled by duration.
_STEP_COST_S: dict[str, float] = {
    "auto_caption": 60.0, "transcribe": 30.0, "auto_reframe": 20.0, "make_shorts": 15.0,
    "noise_reduce": 20.0, "remove_silences": 6.0, "remove_fillers": 3.0,
    "upscale": 120.0, "stabilize": 90.0, "smooth_slow_motion": 90.0,
    "vocal_isolate": 60.0, "instrumental_isolate": 60.0, "tts_voiceover": 4.0,
    "auto_cut_to_beats": 8.0,
}
_DEFAULT_STEP_COST_S = 2.0
_PROGRESS_TICK_S = 1.0
_PROGRESS_CAP = 0.9


class StepRefused(ValueError):
    """The executor's own boundary refused a step (never dispatched)."""

    def __init__(self, reasons: list[str] | str):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


class PlanValidationUnavailable(RuntimeError):
    """`agent/prompt/validate.py` is not importable — the plan cannot be
    validated, so it cannot run. Degrade honestly, never silently."""


def estimated_step_seconds(tool: str, duration_s: float) -> float:
    base = _STEP_COST_S.get(tool, _DEFAULT_STEP_COST_S)
    if tool in _STEP_COST_S:
        return max(1.0, base * max(0.2, duration_s / 60.0))
    return base


# --------------------------------------------------------------------------
# validation + the last-line guard
# --------------------------------------------------------------------------

def _validate_plan(plan: Plan, facts: TimelineFacts) -> Plan:
    """`validate.validate_plan` (P's module). Imported lazily so the executor
    imports on a tree where P's file has not landed, and RAISES rather than
    passing the plan through when it is missing."""
    try:
        from .validate import validate_plan  # type: ignore[import-not-found]
    except ImportError as e:
        raise PlanValidationUnavailable(
            "plan validation is unavailable (agent/prompt/validate.py could not be "
            "imported) — refusing to execute an unvalidated plan") from e
    return validate_plan(plan, facts)


def _allowed_arg_names(tool: str) -> set[str] | None:
    schema = input_schema_for(tool)
    names = set((schema or {}).get("properties") or {}) if schema else set()
    names |= EXTRA_PLAN_ARGS.get(tool, frozenset())
    if not names and schema is None and tool not in EXTRA_PLAN_ARGS:
        return None
    return names


def _check_path_arg(tool: str, arg: str, value: Any, guard: str,
                    facts: TimelineFacts, reasons: list[str]) -> None:
    if guard == "write":
        reasons.append(f"{tool}.{arg}: plans may not write files")
        return
    values = value if isinstance(value, (list, tuple)) else [value]
    for v in values:
        if not isinstance(v, str) or not v:
            reasons.append(f"{tool}.{arg}: path must be a non-empty string")
            continue
        if tool == "apply_lut":
            # LUTs travel as bare bundled names, never as paths (§1.3 rule 6).
            if "/" in v or "\\" in v or v.startswith("."):
                reasons.append(f"{tool}.{arg}: LUTs are bundled names, not paths ({v!r})")
                continue
            from ...config import PRESETS_DIR
            name = Path(v).name
            if not any((PRESETS_DIR / "luts" / c).exists() for c in (name, f"{name}.cube")):
                reasons.append(f"{tool}.{arg}: unknown LUT {v!r}")
            continue
        try:
            resolved = str(Path(v).expanduser().resolve())
        except OSError:
            reasons.append(f"{tool}.{arg}: unresolvable path {v!r}")
            continue
        if resolved not in facts.allowed_paths:
            reasons.append(f"{tool}.{arg}: path not offered ({Path(v).name})")
            continue
        from ...config import assert_path_allowed, restrict_paths_active
        if restrict_paths_active():
            try:
                assert_path_allowed(resolved)
            except ValueError as e:
                reasons.append(f"{tool}.{arg}: {e}")


def _missing_downloads(tool: str, args: dict[str, Any]) -> list[tuple[str, str]]:
    """`(artefact key, human reason)` for every model/voice this step would
    FETCH because it is not on disk — the same probes `facts.first_use` uses."""
    out: list[tuple[str, str]] = []
    if tool in ("auto_caption", "transcribe") and args.get("model"):
        model = str(args["model"])
        if not _D.whisper_model_on_disk(model):
            out.append((f"whisper:{model}", f"{tool}.model: {model!r} is not downloaded"))
    if tool == "tts_voiceover":
        voice = str(args.get("voice") or "en_US-amy-medium")
        from ...ai.tts import voice_paths
        if not voice_paths(voice)[0].exists():
            out.append((f"piper:{voice}", f"tts_voiceover.voice: {voice!r} is not downloaded"))
    target = None
    if tool == "auto_caption":
        target = args.get("target") or args.get("target_lang") or args.get("caption_lang")
    elif tool == "translate_captions":
        target = args.get("target_lang") or args.get("to")
    if target is not None and str(target).lower() in _TRANSLATED_TARGETS:
        from ...ai import translate as _tr
        if not (_tr._model_dir() / "model.bin").exists():
            out.append(("madlad", f"{tool}: translating to {target!r} needs the MADLAD model, "
                                  "which is not downloaded"))
    return out


def _check_download_strings(tool: str, args: dict[str, Any], reasons: list[str],
                            consented: frozenset[str] = frozenset()) -> None:
    """The last-line no-download rule (§1.4). `consented` names the tools the
    user answered **download** for on THIS run (service.resume passes them);
    for any other tool a missing artefact is a refusal, never a fetch."""
    for _key, why in _missing_downloads(tool, args):
        if tool in consented:
            continue
        reasons.append(why)


def guard_step(tool: str, args: dict[str, Any], facts: TimelineFacts, *,
               consented: frozenset[str] = frozenset()) -> None:
    """Refuse anything a model-authored step must never do, even after
    `validate_plan` said yes: an unknown or denied tool, an unknown arg, a
    write path, a read path outside `facts.allowed_paths`, a LUT given as a
    path, or a model/voice/translation model that is not on disk (unless the
    user consented to the download for that tool on this run and the
    executor's pre-step fetched it — see `fetch_consented_downloads`).
    Raises `StepRefused` with every reason found."""
    reasons: list[str] = []
    if tool in PLAN_DENY:
        raise StepRefused(f"{tool}: tool is denied to plans")
    if tool not in _D.DISPATCH:
        raise StepRefused(f"{tool}: unknown tool")
    allowed = _allowed_arg_names(tool)
    if allowed is None:
        raise StepRefused(f"{tool}: tool has no advertised schema")
    extra = sorted(k for k in args if k not in allowed)
    if extra:
        reasons.append(f"{tool}: unknown args {extra}")
    for arg, guard in _path_args.path_args_for(tool).items():
        if arg in args and args[arg] is not None:
            _check_path_arg(tool, arg, args[arg], guard, facts, reasons)
    _check_download_strings(tool, args, reasons, consented)
    if reasons:
        raise StepRefused(reasons)


# --------------------------------------------------------------------------
# consented downloads (§1.4: the ONLY way a model or voice is fetched here)
# --------------------------------------------------------------------------

def _fetch_artefact(key: str, *, set_progress: Callable[[float], None] | None = None) -> None:
    """Download ONE first-use artefact by its `facts.FIRST_USE_BYTES` key.
    Loopback-initiated, user-consented, and the only network call the prompt
    executor can make. Raises on any failure (the step then refuses honestly)."""
    kind, _, name = key.partition(":")
    if kind == "whisper":
        try:
            from faster_whisper.utils import download_model
        except ImportError as e:
            # whisper-cli (the packaged Mac) has no downloader: a ggml model is
            # dropped into the models dir by hand — say so instead of fetching.
            raise RuntimeError(f"no downloader for the {name} model in this build — drop a ggml-{name} "
                               "file into the whisper-cpp models dir, or run from source") from e
        download_model(name)
        if not _D.whisper_model_on_disk(name):
            raise RuntimeError(f"whisper {name} download finished but the model is not on disk")
        return
    if kind == "piper":
        from ...ai.tts import ensure_voice, voice_paths
        ensure_voice(name)
        if not voice_paths(name)[0].exists():
            raise RuntimeError(f"Piper voice {name} download finished but the voice is not on disk")
        return
    if key == "madlad":
        from ...ai import translate as _tr
        _tr._ensure_model_downloaded()
        return
    raise RuntimeError(f"unknown download artefact {key!r}")


def fetch_consented_downloads(steps: list[Step], consented: frozenset[str], *, emit: Emit, total: int,
                              cancel_event: threading.Event | None,
                              fetch: Callable[..., None] = _fetch_artefact) -> list[str]:
    """The §1.4 'yes' path. Before the batch, download every artefact a
    CONSENTED step still lacks, as explicit `step{tool:"download"}` frames
    (index `total`, after the real steps, the way `verify_render` sits after
    them), then re-probe. Returns the artefact keys fetched. A failure raises
    — the caller reports it as a failed plan, and `guard_step` would have
    refused the dependent step anyway. WHY this exists: without it the user
    answered **download**, `validate_plan` accepted the step (listed in
    `downloads_needed`) and `guard_step` then refused it as 'not downloaded'
    — the consent flow was a dead end (verified 2026-09-10)."""
    wanted: list[str] = []
    for st in steps:
        if st.tool not in consented:
            continue
        for key, _why in _missing_downloads(st.tool, st.args):
            if key not in wanted:
                wanted.append(key)
    if not wanted:
        return []
    fetched: list[str] = []
    for i, key in enumerate(wanted):
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        emit({"type": "step", "index": total, "total": total, "tool": "download", "status": "running",
              "progress": i / len(wanted), "summary": f"downloading {key} (you said yes)"})
        try:
            fetch(key)
        except Exception as e:  # noqa: BLE001 — reported as a failed download step, never swallowed
            emit({"type": "step", "index": total, "total": total, "tool": "download", "status": "failed",
                  "error": f"{key}: {e}"})
            raise RuntimeError(f"download of {key} failed: {e}") from e
        fetched.append(key)
    emit({"type": "step", "index": total, "total": total, "tool": "download", "status": "ok",
          "progress": 1.0, "summary": "downloaded " + ", ".join(fetched)})
    return fetched


# --------------------------------------------------------------------------
# clip sentinels
# --------------------------------------------------------------------------

def _v1_media(edl: EDL) -> list[Clip]:
    t = edl.get_track("v1")
    return sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)


def resolve_clip_ref(edl: EDL, ref: Any, facts: TimelineFacts) -> list[str]:
    """Clip ids for a sentinel (§1.1), read from the LIVE edl. A plain id
    passes through untouched. Raises `StepRefused` when a sentinel names
    nothing (an empty v1, no selection, playhead in a gap)."""
    if ref not in CLIP_SENTINELS:
        return [str(ref)]
    clips = _v1_media(edl)
    if ref == "$v1_all":
        ids = [c.id for c in clips]
    elif ref == "$v1_first":
        ids = [clips[0].id] if clips else []
    elif ref == "$v1_last":
        ids = [clips[-1].id] if clips else []
    elif ref == "$selected":
        ids = [facts.selection] if facts.selection and edl.get_clip(facts.selection) else []
    else:  # $playhead
        ph = facts.playhead
        ids = [c.id for c in clips
               if ph is not None and c.start <= ph < c.start + c.effective_duration][:1]
    if not ids:
        raise StepRefused(f"clip sentinel {ref} names no clip on this timeline")
    return ids


#: `add_transition(at=$v1_seams)` fans out over at most this many seams — the
#: same cap the `transitions` recipe applies to per-seam steps.
MAX_SEAM_FANOUT = 12
_SEAM_TOL_S = 0.05


def live_v1_seams(edl: EDL) -> list[float]:
    """Timeline seconds where two adjacent v1 media clips touch, read from the
    LIVE edl (after any cuts earlier in the same batch)."""
    clips = _v1_media(edl)
    return [round(nxt.start, 4) for cur, nxt in zip(clips, clips[1:])
            if abs((cur.start + cur.effective_duration) - nxt.start) <= _SEAM_TOL_S]


def resolve_step_args(edl: EDL, args: dict[str, Any], facts: TimelineFacts) -> list[dict[str, Any]]:
    """The dispatch arg dicts for one step: one per clip for a `$v1_all`
    `clip_id` (fan-out), one per live seam for `add_transition(at=$v1_seams)`,
    else exactly one. `clip_ids` sentinels become the resolved list in place."""
    out = dict(args)
    if out.get("at") == SEAM_SENTINEL:
        seams = live_v1_seams(edl)
        if not seams:
            raise StepRefused(f"{SEAM_SENTINEL} names no seam — v1 has no two touching clips")
        # `add_transition` never moves a clip (the overlap is accounted for by
        # `EDL.transition_overlap`), so the seam list stays valid across the fan-out.
        return [{**out, "at": at} for at in seams[:MAX_SEAM_FANOUT]]
    if "clip_ids" in out and isinstance(out["clip_ids"], (list, tuple, str)):
        refs = [out["clip_ids"]] if isinstance(out["clip_ids"], str) else list(out["clip_ids"])
        ids: list[str] = []
        for r in refs:
            ids.extend(resolve_clip_ref(edl, r, facts))
        out["clip_ids"] = ids
    ref = out.get("clip_id")
    if ref in CLIP_SENTINELS:
        ids = resolve_clip_ref(edl, ref, facts)
        if ref == "$v1_all":
            return [{**out, "clip_id": cid} for cid in ids]
        out["clip_id"] = ids[0]
    return [out]


# --------------------------------------------------------------------------
# side-effect snapshot
# --------------------------------------------------------------------------

@dataclass
class SideEffectSnapshot:
    """Byte copies of the files a step may rewrite outside the EDL. `paths`
    maps original → (copy | None); None records "did not exist", so a file a
    failed run CREATED is removed on restore."""
    root: Path
    paths: dict[Path, Path | None] = field(default_factory=dict)

    @classmethod
    def take(cls, store: EDLStore, facts: TimelineFacts, run_id: str) -> "SideEffectSnapshot":
        root = Path(store.dir) / SNAPSHOT_DIR / run_id
        root.mkdir(parents=True, exist_ok=True)
        snap = cls(root=root)
        candidates: list[Path] = []
        if facts.ingest_json_path:
            candidates.append(Path(facts.ingest_json_path))
        ing = _D._current_v1_ingest_json(store)
        if ing is not None:
            candidates.append(ing)
        candidates.append(Path(store.dir) / "transcript.json")
        for i, src in enumerate(dict.fromkeys(candidates)):
            if src.exists():
                dst = root / f"{i}_{src.name}"
                shutil.copyfile(src, dst)
                snap.paths[src] = dst
            else:
                snap.paths[src] = None
        return snap

    def restore(self) -> list[str]:
        restored: list[str] = []
        for src, copy in self.paths.items():
            if copy is None:
                if src.exists():
                    src.unlink()
                    restored.append(src.name)
                continue
            shutil.copyfile(copy, src)
            restored.append(src.name)
        return restored

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class StepOutcome:
    index: int
    tool: str
    args: list[dict[str, Any]]                  # one per dispatch (fan-out)
    status: str = "running"                     # ok | failed | skipped
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    effect: str | None = None
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class ExecResult:
    plan: Plan
    steps: list[StepOutcome]
    edl_before: EDL
    duration_before: float
    committed: bool = False
    op: dict[str, Any] | None = None
    error: str | None = None
    cancelled: bool = False
    new_sessions: list[str] = field(default_factory=list)
    child_runs: list[dict[str, Any]] = field(default_factory=list)
    restored_files: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)        # artefact keys fetched on a consented run

    @property
    def applied(self) -> int:
        return sum(1 for s in self.steps if s.ok and s.effect != "none")

    @property
    def failed(self) -> bool:
        return self.error is not None

    def results_for(self, tool: str) -> list[dict[str, Any]]:
        return [r for s in self.steps if s.tool == tool for r in s.results]

    def outcomes_for(self, tool: str) -> list[StepOutcome]:
        return [s for s in self.steps if s.tool == tool]


def _effect_of(result: Any) -> str | None:
    """`"none"` when the handler reports it had nothing to do (§4.2)."""
    if not isinstance(result, dict):
        return None
    for key in ("cuts", "words", "n", "lines", "cues", "translated"):
        if key in result and result[key] in (0, 0.0):
            return "none"
    if result.get("reframed") == []:
        return "none"
    if result.get("reused") is True:
        return "none"
    return None


def _result_summary(result: Any) -> str:
    if isinstance(result, dict) and isinstance(result.get("summary"), str):
        return result["summary"]
    return ""


# --------------------------------------------------------------------------
# transcript wait
# --------------------------------------------------------------------------

def _transcript_present(store: EDLStore) -> bool:
    tx = _D._load_transcript(store)
    return tx is not None and bool(tx.words)


def wait_for_upload_transcript(store: EDLStore, *, emit: Emit, index: int, total: int,
                               cancel_event: threading.Event | None,
                               timeout_s: float = TRANSCRIPT_WAIT_S,
                               poll_s: float = 2.0, sleep=time.sleep) -> bool:
    """Poll for the upload's background transcript (§4.2). True when it
    landed within `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    while True:
        if _transcript_present(store):
            return True
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        if time.monotonic() >= deadline:
            return False
        emit({"type": "step", "index": index, "total": total, "tool": "transcribe",
              "status": "running", "summary": "waiting for upload transcript"})
        sleep(poll_s)


# --------------------------------------------------------------------------
# run_plan
# --------------------------------------------------------------------------

def _handler_reports_progress(tool: str) -> bool:
    fn = _D.DISPATCH.get(tool)
    try:
        return bool(fn) and "set_progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class _ProgressTicker:
    """Synthetic time-based progress for handlers with no `set_progress`
    (`elapsed / estimated`, capped at 0.9) — the step is not hung, it is a
    reframe that reports nothing (§4.2)."""

    def __init__(self, emit_progress: Callable[[float], None], estimate_s: float) -> None:
        self._emit = emit_progress
        self._estimate = max(1.0, estimate_s)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vai-prompt-progress")

    def _loop(self) -> None:
        started = time.monotonic()
        while not self._stop.wait(_PROGRESS_TICK_S):
            if self._stop.is_set():
                return
            self._emit(min(_PROGRESS_CAP, (time.monotonic() - started) / self._estimate))

    def __enter__(self) -> "_ProgressTicker":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        # Set the flag AND wait for the thread: a tick already past `wait()`
        # could otherwise publish `step running` AFTER the step's `ok` frame
        # (flipping the run record and the desktop row back to running), or
        # after `done` (RunBus.publish raises in the daemon thread).
        self._stop.set()
        self._thread.join(timeout=2.0)


def _dispatch_step(store: EDLStore, step: Step, index: int, total: int, facts: TimelineFacts,
                   *, emit: Emit, cancel_event: threading.Event | None,
                   plan_id: str, consented: frozenset[str] = frozenset()) -> StepOutcome:
    """One step: guard → resolve sentinels → dispatch (fan-out) → events.
    Raises on failure; the caller decides optional-vs-required."""
    tool = step.tool
    started = time.monotonic()
    call_id = f"{plan_id}_s{index}"
    guard_step(tool, step.args, facts, consented=consented)
    arg_sets = resolve_step_args(store.edl, step.args, facts)
    outcome = StepOutcome(index=index, tool=tool, args=arg_sets)
    emit({"type": "step", "index": index, "total": total, "tool": tool, "status": "running",
          "progress": 0.0})
    if len(arg_sets) == 1:
        shown_args = dict(step.args)
    elif "clip_id" in arg_sets[0] and step.args.get("clip_id") in CLIP_SENTINELS:
        shown_args = {**step.args, "clip_id": [a["clip_id"] for a in arg_sets]}
    else:
        shown_args = {**step.args, "at": [a["at"] for a in arg_sets]}
    emit({"type": "tool_use", "name": tool, "args": shown_args, "id": call_id})
    last_tick = [0.0]
    finished = threading.Event()        # set before the terminal frame; a late tick is dropped

    def _progress(p: float) -> None:
        if finished.is_set():
            return
        now = time.monotonic()
        if now - last_tick[0] < 0.25 and p < 1.0:
            return
        last_tick[0] = now
        emit({"type": "step", "index": index, "total": total, "tool": tool, "status": "running",
              "progress": max(0.0, min(1.0, float(p)))})

    n = len(arg_sets)
    for k, args in enumerate(arg_sets):
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        dispatch_args = dict(args)
        if _handler_reports_progress(tool) or n > 1:
            def _sub_progress(p: float, _k=k) -> None:
                _progress((_k + max(0.0, min(1.0, float(p)))) / n)
            result = _D.dispatch(store, tool, dispatch_args, set_progress=_sub_progress,
                                 cancel_event=cancel_event)
        else:
            with _ProgressTicker(_progress, estimated_step_seconds(tool, facts.duration)):
                result = _D.dispatch(store, tool, dispatch_args, cancel_event=cancel_event)
        outcome.results.append(result if isinstance(result, dict) else {"result": result})
        if n > 1:
            _progress((k + 1) / n)

    finished.set()
    outcome.latency_ms = int((time.monotonic() - started) * 1000)
    effects = [_effect_of(r) for r in outcome.results]
    outcome.effect = "none" if effects and all(e == "none" for e in effects) else None
    outcome.status = "ok"
    result_payload: Any = outcome.results[0] if n == 1 else {"results": outcome.results}
    emit({"type": "tool_result", "name": tool, "result": result_payload, "id": call_id})
    summary = _result_summary(outcome.results[0]) if n == 1 else (
        f"{n} seams" if step.args.get("at") == SEAM_SENTINEL else f"{n} clips")
    if outcome.effect == "none":
        summary = f"{summary} · no effect" if summary else "no effect"
    evt: dict[str, Any] = {"type": "step", "index": index, "total": total, "tool": tool,
                           "status": "ok", "progress": 1.0, "summary": summary}
    if outcome.effect:
        evt["effect"] = outcome.effect
    emit(evt)
    return outcome


def run_plan(store: EDLStore, plan: Plan, facts: TimelineFacts, *, emit: Emit,
             cancel_event: threading.Event | None, prompt: str,
             run_id: str | None = None, validator: Callable[[Plan, TimelineFacts], Plan] | None = None,
             wait_transcript: bool = True, consented_downloads: frozenset[str] = frozenset(),
             fetch: Callable[..., None] = _fetch_artefact) -> ExecResult:
    """Execute `plan` on `store` (caller holds the session lock). Returns an
    `ExecResult`; never raises for a step failure — `result.error` carries
    the message and the timeline is guaranteed unchanged in that case.

    `consented_downloads` = the tools the user answered **download** for on
    this run (only `service.resume` can set it, from a `downloads` answer);
    their missing artefacts are fetched as explicit pre-steps, and every
    other missing model is refused by `guard_step` (§1.4)."""
    run_id = run_id or new_run_id()
    validate = validator or _validate_plan
    edl_before = store.edl.model_copy(deep=True)
    duration_before = store.edl.duration
    hash_before = store.edl.hash()
    result = ExecResult(plan=plan, steps=[], edl_before=edl_before, duration_before=duration_before)

    try:
        validated = validate(plan, facts)
    except (PlanValidationUnavailable, ValueError) as e:
        result.error = f"Plan refused before any step ran: {e}. Timeline unchanged."
        emit({"type": "error", "message": result.error})
        return result

    steps = list(validated.steps)
    total = len(steps)
    if total == 0:
        return result

    snapshot = SideEffectSnapshot.take(store, facts, run_id)
    skip_index: int | None = None
    if wait_transcript and facts.transcript_pending:
        for i, s in enumerate(steps):
            if s.tool == "transcribe":
                try:
                    if wait_for_upload_transcript(store, emit=emit, index=i, total=total,
                                                  cancel_event=cancel_event):
                        skip_index = i
                except JobCancelled:
                    result.cancelled = True
                    result.error = "Cancelled — timeline unchanged."
                    emit({"type": "error", "message": result.error})
                    snapshot.discard()
                    return result
                break

    if consented_downloads:
        try:
            result.downloads = fetch_consented_downloads(
                steps, consented_downloads, emit=emit, total=total, cancel_event=cancel_event, fetch=fetch)
        except JobCancelled:
            result.cancelled = True
            result.error = "Cancelled — timeline unchanged."
            emit({"type": "error", "message": result.error})
            snapshot.discard()
            return result
        except Exception as e:  # noqa: BLE001
            result.error = f"{e}. Timeline unchanged."
            emit({"type": "error", "message": result.error})
            snapshot.discard()
            return result

    failed_step: tuple[int, Step, Exception] | None = None
    try:
        with store.batch():
            for i, step in enumerate(steps):
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelled()
                if i == skip_index:
                    outcome = StepOutcome(index=i, tool=step.tool, args=[dict(step.args)],
                                          status="ok", effect="none")
                    result.steps.append(outcome)
                    emit({"type": "step", "index": i, "total": total, "tool": step.tool,
                          "status": "ok", "progress": 1.0, "effect": "none",
                          "summary": "upload transcript arrived — nothing to do"})
                    continue
                try:
                    result.steps.append(_dispatch_step(
                        store, step, i, total, facts, emit=emit, cancel_event=cancel_event,
                        plan_id=validated.id or "p_00000000", consented=consented_downloads))
                except JobCancelled:
                    raise
                except Exception as e:  # noqa: BLE001 — every handler error is a step failure
                    if step.optional:
                        result.steps.append(StepOutcome(index=i, tool=step.tool, args=[dict(step.args)],
                                                        status="skipped", error=str(e)))
                        emit({"type": "tool_result", "name": step.tool, "result": {"error": str(e)},
                              "id": f"{validated.id}_s{i}", "is_error": True})
                        emit({"type": "step", "index": i, "total": total, "tool": step.tool,
                              "status": "skipped", "error": str(e)})
                        continue
                    failed_step = (i, step, e)
                    raise
    except JobCancelled:
        result.cancelled = True
        result.restored_files = snapshot.restore()
        snapshot.discard()
        result.error = "Cancelled — timeline unchanged."
        result.new_sessions = _sessions_created(result)
        emit({"type": "error", "message": _with_kept_sessions(result.error, result.new_sessions)})
        return result
    except Exception as e:  # noqa: BLE001
        result.restored_files = snapshot.restore()
        snapshot.discard()
        i, step, err = failed_step if failed_step else (len(result.steps), None, e)
        tool = step.tool if step else "plan"
        result.steps.append(StepOutcome(index=i, tool=tool, args=[dict(step.args)] if step else [],
                                        status="failed", error=str(err)))
        emit({"type": "tool_result", "name": tool, "result": {"error": str(err)},
              "id": f"{validated.id}_s{i}", "is_error": True})
        emit({"type": "step", "index": i, "total": total, "tool": tool, "status": "failed",
              "error": str(err)})
        restored = " Transcript restored." if result.restored_files else ""
        result.error = (f"Step {i + 1}/{total} {tool} failed: {err}. "
                        f"Timeline unchanged.{restored}")
        result.new_sessions = _sessions_created(result)
        emit({"type": "error", "message": _with_kept_sessions(result.error, result.new_sessions)})
        return result

    result.new_sessions = _sessions_created(result)
    # Commit when the tree changed — or when a step created sessions
    # (`make_shorts(save_as_sessions)` leaves the PARENT tree untouched, yet
    # the ops entry is the only record that these shorts came from here, and
    # the benchmark asserts "parent has one op"). A read-only or no-effect
    # plan still commits nothing.
    if store.edl.hash() != hash_before or result.new_sessions:
        title = validated.title or validated.intent
        store.commit("prompt", {"prompt": prompt, "plan_id": validated.id,
                                "steps": [s.tool for s in steps]},
                     f"Prompt: {title} ({result.applied} steps)")
        result.committed = True
        last = store.ops.last()
        result.op = last.model_dump() if last else None
    snapshot.discard()
    return result


def _sessions_created(result: ExecResult) -> list[str]:
    out: list[str] = []
    for r in result.results_for("make_shorts"):
        out.extend(str(s) for s in (r.get("new_sessions") or []))
    return out


def _with_kept_sessions(message: str, sessions: list[str]) -> str:
    if not sessions:
        return message
    return f"{message} Created sessions kept: {', '.join(sessions)}."


# --------------------------------------------------------------------------
# the run thread
# --------------------------------------------------------------------------

@dataclass
class RunHandle:
    run_id: str
    sid: str
    plan: Plan
    log: RunLog
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    result: ExecResult | None = None
    verify: dict[str, Any] | None = None
    final_text: str | None = None
    consented_downloads: frozenset[str] = frozenset()

    @property
    def bus(self) -> RunBus:
        return self.log.bus

    @property
    def finished(self) -> bool:
        return self.bus.closed

    def cancel(self) -> None:
        self.cancel_event.set()


#: The current-or-last run per session. Kept after completion so a reconnect
#: can replay the bus and `POST …/prompt/cancel` can answer honestly.
RUNS: dict[str, RunHandle] = {}
_RUNS_GUARD = threading.Lock()


def get_run(sid: str, run_id: str | None = None) -> RunHandle | None:
    with _RUNS_GUARD:
        h = RUNS.get(sid)
    if h is None or (run_id and h.run_id != run_id):
        return None
    return h


def active_run(sid: str) -> RunHandle | None:
    h = get_run(sid)
    return h if h is not None and not h.finished else None


def _finish_children(store_resolver: Callable[[str], EDLStore], parent: ExecResult,
                     facts: TimelineFacts, *, emit: Emit, cancel_event: threading.Event,
                     prompt: str) -> None:
    """`shorts.finish` (§4.2): reframe + captions + hook in every session
    `make_shorts` created, each its own lock, snapshot and one-op commit.
    Failures are recorded per child, never raised into the parent."""
    from ...ai.features import cached_feature_report
    from .facts import build_facts
    from .recipes import from_intents
    from .schema import IntentDraft, IntentItem

    plan_id = parent.plan.id or "p_00000000"
    for k, child_sid in enumerate(parent.new_sessions):
        record: dict[str, Any] = {"session": child_sid, "status": "running"}
        call_id = f"{plan_id}_child{k}"
        emit({"type": "tool_use", "name": "finish_short", "args": {"session": child_sid}, "id": call_id})
        try:
            child_store = store_resolver(child_sid)
            with locks.session_lock(child_sid):
                child_facts = build_facts(child_store, None, feature_report=cached_feature_report())
                draft = IntentDraft(intents=[IntentItem(recipe="reframe", slots={"ratio": "9:16"}),
                                             IntentItem(recipe="captions", slots={}),
                                             IntentItem(recipe="hook", slots={})],
                                    confidence=1.0, reply="finish short")
                child_plan = from_intents(draft, child_facts).with_(brain=parent.plan.brain)
                child = run_plan(child_store, child_plan, child_facts, emit=lambda e: None,
                                 cancel_event=cancel_event, prompt=f"{prompt} (finish short)")
            record.update(status="failed" if child.error else "ok", error=child.error,
                          applied=child.applied, op=child.op)
        except Exception as e:  # noqa: BLE001 — a child must never take the parent down
            record.update(status="failed", error=f"{type(e).__name__}: {e}")
        parent.child_runs.append(record)
        emit({"type": "tool_result", "name": "finish_short", "result": record, "id": call_id,
              **({"is_error": True} if record["status"] != "ok" else {})})


def _wants_children(plan: Plan) -> bool:
    return any(pc.check == "shorts_finished" for pc in plan.postconditions)


def _run_thread(handle: RunHandle, store_resolver: Callable[[str], EDLStore],
                facts: TimelineFacts, prompt: str, history_writer: Any) -> None:
    from . import verify as _verify
    from .summary import compose_reply

    log = handle.log
    sid = handle.sid
    final_text = ""
    try:
        log.set_status("running")
        with locks.session_lock(sid):
            locks.register_prompt_run(sid, handle.run_id)
            try:
                store = store_resolver(sid)
                result = run_plan(store, handle.plan, facts, emit=log.emit,
                                  cancel_event=handle.cancel_event, prompt=prompt,
                                  run_id=handle.run_id, consented_downloads=handle.consented_downloads)
                handle.result = result
                if result.error:
                    log.set_status("cancelled" if result.cancelled else "failed", error=result.error)
                    # The `error` frame already carried the bare message; the
                    # record and history get the same text with the brain
                    # prefix every reply carries (§4.5).
                    final_text = compose_reply(handle.plan, result, None)
                    log.set_reply(final_text)
                    return
                # Children run on success whenever sessions were created —
                # never gated on the parent's own tree changing (see run_plan).
                if not result.error and result.new_sessions and _wants_children(handle.plan):
                    _finish_children(store_resolver, result, facts, emit=log.emit,
                                     cancel_event=handle.cancel_event, prompt=prompt)
                    log.record.child_runs = list(result.child_runs)
                log.set_status("verifying")
                verify_result = _verify.verify_plan(store, handle.plan, result, facts,
                                                    emit=log.emit, cancel_event=handle.cancel_event,
                                                    store_resolver=store_resolver)
                handle.verify = verify_result
                log.emit({"type": "verify", **verify_result})
                if result.op is not None:
                    log.emit({"type": "op", "op": result.op})
                final_text = compose_reply(handle.plan, result, verify_result)
                log.set_reply(final_text)
                log.emit({"type": "text_delta", "text": final_text})
                log.set_status("done")
            finally:
                locks.clear_prompt_run(sid, handle.run_id)
    except Exception as e:  # noqa: BLE001 — the thread must always close the bus
        final_text = f"Prompt run failed: {type(e).__name__}: {e}"
        log.set_status("failed", error=final_text)
        log.emit({"type": "error", "message": final_text})
    finally:
        handle.final_text = final_text
        if not log.bus.closed:
            log.emit({"type": "done"})
        if history_writer is not None and final_text:
            try:
                history_writer.finalize(sid, handle.run_id, final_text)
            except Exception:  # noqa: BLE001 — history is a courtesy, the run already happened
                pass


def start_run(store_resolver: Callable[[str], EDLStore], sid: str, plan: Plan,
              facts: TimelineFacts, *, prompt: str, history_writer: Any = None,
              bus: RunBus | None = None, run_id: str | None = None,
              consented_downloads: frozenset[str] = frozenset()) -> RunHandle:
    """Spawn the daemon run thread; returns immediately with the handle the
    service subscribes to. `bus` lets the service pre-publish the planning
    events (`brain`, `plan`) on the same bus so a reconnect replays them.
    `consented_downloads` comes only from a **download** answer (§1.4)."""
    run_id = run_id or new_run_id()
    session_dir = Path(store_resolver(sid).dir)
    record = RunRecord(run_id=run_id, plan_id=plan.id, prompt=prompt, brain=plan.brain)
    log = RunLog(session_dir, record, bus=bus)
    handle = RunHandle(run_id=run_id, sid=sid, plan=plan, log=log,
                       consented_downloads=frozenset(consented_downloads))
    with _RUNS_GUARD:
        RUNS[sid] = handle
    t = threading.Thread(target=_run_thread, args=(handle, store_resolver, facts, prompt, history_writer),
                         daemon=True, name=f"vai-prompt-{run_id}")
    handle.thread = t
    t.start()
    return handle


__all__ = ["EXTRA_PLAN_ARGS", "StepRefused", "PlanValidationUnavailable", "guard_step",
           "fetch_consented_downloads", "live_v1_seams", "MAX_SEAM_FANOUT",
           "resolve_clip_ref", "resolve_step_args", "SideEffectSnapshot", "StepOutcome",
           "ExecResult", "wait_for_upload_transcript", "run_plan", "RunHandle", "RUNS",
           "get_run", "active_run", "start_run", "estimated_step_seconds"]
