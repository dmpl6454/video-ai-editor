"""The `apple_intelligence` brain: FoundationModels via the `fm-planner`
child process (spec §3.2).

Why a child process and not PyObjC: FoundationModels' `@Generable` macro is
the whole point — constrained decoding into typed slot fields is what keeps
a 3B on-device model from inventing argument names — and macros exist only
in Swift. The helper is a plain stdin/stdout JSON program; this adapter
never hands it a secret, a path, or a working directory with files in it.

Honesty ledger, recorded on the build Mac on 2026-09-08 (§8 pre-flight):
`fm-planner probe` → `state=appleIntelligenceNotEnabled`, macOS 26.6.2,
supported languages `da de en es fr it ja ko nb nl pt sv tr vi zh` — no
`hi`. So on that machine this brain is a greyed-out rung with the fix
"Turn on Apple Intelligence in System Settings", and even once enabled a
Devanagari prompt skips it without spawning (the pre-check below). The
generation path is therefore UNTESTED against the live model there; every
exit-code row is exercised through a fake helper in
tests/test_prompt_fm_adapter.py instead.

Negative caches (all per process, all in this object):
  * `language` → the prompt's script is blocked for the rest of the process;
  * `busy` (rateLimited / concurrentRequests) → 60 s. A CLI child of a
    PyWebView app is never the foreground app and Apple rate-limits
    background callers, so retrying immediately only burns the budget;
  * `unavailable` (assetsUnavailable) → 5 min, and the probe cache is
    dropped so `availability()` re-asks afterwards;
  * `context` → one retry with ≤ 8 recipe cards, then fall through.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .... import platformutil as _pu
from ..recipes import from_intents
from .base import (Availability, BrainRequest, BrainResult, TextResult, TextTask, available,
                   unavailable)
from ..schema import IntentDraft
from .content import strip_model_hook_text
from .prompt_text import (DraftShapeError, contains_devanagari, draft_key, facts_to_prompt_block,
                          flatten_fm_item, normalize_draft, user_prompt_with_answers)

__all__ = ["FMBrain", "helper_path", "default_runner", "macos_version", "EXIT_REASONS",
           "PROBE_TTL_S", "BUSY_BACKOFF_S", "UNAVAILABLE_BACKOFF_S", "CONTEXT_RETRY_CARDS",
           "MIN_MACOS", "HELPER_ENV", "MODEL_NAME", "RunnerFn"]

HELPER_ENV = "VAI_FM_HELPER"
MODEL_NAME = "apple-fm"
MIN_MACOS = (26, 0)
PROBE_TTL_S = 60.0
PROBE_TIMEOUT_S = 5.0
BUSY_BACKOFF_S = 60.0
UNAVAILABLE_BACKOFF_S = 300.0
CONTEXT_RETRY_CARDS = 8
DEV_BUILD_FIX = "cd tools/fm-planner && swift build -c release --arch arm64"

#: Helper exit status → `BrainResult.reason` (tools/fm-planner/Errors.swift).
EXIT_REASONS: dict[int, str] = {
    2: "unavailable", 3: "timeout", 4: "parse", 5: "guardrail",
    6: "decode", 7: "language", 8: "context", 9: "busy",
}
#: The JSON `code` is more specific than the exit status (refusal vs guardrail).
_JSON_CODE_REASONS: dict[str, str] = {
    "unavailable": "unavailable", "timeout": "timeout", "bad_input": "parse",
    "guardrail": "guardrail", "refusal": "refusal", "decode": "decode",
    "language": "language", "context": "context", "busy": "busy",
}

RunnerFn = Callable[[list[str], bytes, float], tuple[int, bytes, bytes]]


def macos_version() -> tuple[int, int] | None:
    """`(major, minor)` as INTEGERS — `"9.x" < "26"` is False as strings."""
    raw = platform.mac_ver()[0]
    if not raw:
        return None
    try:
        parts = [int(x) for x in raw.split(".")[:2]]
    except ValueError:
        return None
    return (parts[0], parts[1] if len(parts) > 1 else 0)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def helper_path(*, env: dict[str, str] | None = None, frozen: bool | None = None,
                repo_root: Path | None = None) -> str | None:
    """`VAI_FM_HELPER` → the frozen bundle → the dev build (`.binpath`
    written by `swift build --show-bin-path`, then the conventional SwiftPM
    output dirs) → PATH. None when nothing exists."""
    env = os.environ if env is None else env
    override = env.get(HELPER_ENV)
    if override:
        return override if Path(override).exists() else None
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "fm-planner"
        return str(bundled) if bundled.exists() else None
    root = (repo_root or _repo_root()) / "tools" / "fm-planner"
    candidates: list[Path] = []
    binpath = root / ".binpath"
    if binpath.exists():
        try:
            candidates.append(Path(binpath.read_text(encoding="utf-8").strip()) / "fm-planner")
        except OSError:
            pass
    candidates += [root / ".build" / "arm64-apple-macosx" / "release" / "fm-planner",
                   root / ".build" / "release" / "fm-planner"]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return _pu.find_binary("fm-planner", [Path(sys.executable).parent])


def default_runner(argv: list[str], stdin: bytes, timeout_s: float) -> tuple[int, bytes, bytes]:
    """Spawn the helper with a scrubbed environment (no ANTHROPIC_API_KEY, no
    HUGGINGFACE_TOKEN — only PATH/HOME/TMPDIR) inside an EMPTY temp cwd."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "TMPDIR": tempfile.gettempdir()}
    with tempfile.TemporaryDirectory(prefix="fm-planner-") as cwd:
        try:
            proc = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout_s,
                                  env=env, cwd=cwd, **_pu.SUBPROCESS_FLAGS)
        except subprocess.TimeoutExpired as e:
            return 3, e.stdout or b"", b"timeout"
        except OSError as e:
            return 2, b"", str(e).encode("utf-8", "replace")
    return proc.returncode, proc.stdout, proc.stderr


def _parse_json(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _script_of(text: str) -> str:
    return "devanagari" if contains_devanagari(text) else "latin"


class FMBrain:
    id = "apple_intelligence"

    def __init__(self, *, helper: str | None = None, runner: RunnerFn | None = None,
                 darwin: bool | None = None, macos: tuple[int, int] | None = None,
                 frozen: bool | None = None, clock: Callable[[], float] = time.monotonic):
        self._helper_override = helper
        self._runner = runner or default_runner
        self._darwin = (sys.platform == "darwin") if darwin is None else darwin
        self._macos = macos_version() if macos is None else macos
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._clock = clock
        self._probe: dict[str, Any] | None = None
        self._probe_at = -1e9
        self._availability: Availability | None = None
        self._busy_until = 0.0
        self._unavailable_until = 0.0
        self._blocked_scripts: set[str] = set()
        #: One-entry draft cache (`draft_key` → draft): the router's hook-text
        #: pass re-asks with `req.hook_text` set and gets the same draft
        #: re-expanded, never a second spawn (§3.7).
        self._last_draft: tuple[str, IntentDraft] | None = None

    # --- availability ------------------------------------------------------

    def invalidate(self) -> None:
        self._availability = None
        self._probe_at = -1e9

    def helper(self) -> str | None:
        if self._helper_override:
            return self._helper_override if Path(self._helper_override).exists() else None
        return helper_path(frozen=self._frozen)

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple((self._probe or {}).get("languages") or ())

    @property
    def state(self) -> str:
        """The probe state, or the Python-side one (`unsupportedOS`,
        `helperMissing`) — what `brains_report()` shows as `detail`."""
        if not self._darwin or self._macos is None or tuple(self._macos) < MIN_MACOS:
            return "unsupportedOS"
        if self.helper() is None:
            return "helperMissing"
        return str((self._probe or {}).get("state") or "unknown")

    def availability(self) -> Availability:
        now = self._clock()
        if self._availability is not None and now - self._probe_at < PROBE_TTL_S:
            return self._availability
        self._availability = self._compute_availability(now)
        self._probe_at = now
        return self._availability

    def _compute_availability(self, now: float) -> Availability:
        if not self._darwin:
            return unavailable("unsupportedOS: Apple Intelligence exists on macOS only", None)
        if self._macos is None or tuple(self._macos) < MIN_MACOS:
            shown = ".".join(str(x) for x in self._macos) if self._macos else "unknown"
            return unavailable(f"unsupportedOS: needs macOS 26, this is {shown}",
                               "Update to macOS 26 or later to use Apple Intelligence")
        helper = self.helper()
        if helper is None:
            fix = ("this build was made without the Apple Intelligence helper (macOS 26 SDK "
                   "needed at build time)") if self._frozen else f"Build the helper: {DEV_BUILD_FIX}"
            return unavailable("helperMissing: fm-planner binary not found", fix)
        if now < self._unavailable_until:
            return unavailable("assetsUnavailable: the on-device model reported its assets missing",
                               "Wait a few minutes and try again", model=MODEL_NAME)
        rc, out, _err = self._runner([helper, "probe"], b"", PROBE_TIMEOUT_S)
        probe = _parse_json(out)
        if rc != 0 or not probe or not probe.get("ok"):
            return unavailable("helperFailed: the fm-planner probe did not answer",
                               f"Rebuild the helper: {DEV_BUILD_FIX}")
        self._probe = probe
        state = str(probe.get("state") or "unknown")
        if probe.get("available"):
            langs = probe.get("languages") or []
            return available(f"{state} · macOS {probe.get('os', '?')} · {len(langs)} languages",
                             model=MODEL_NAME)
        action = "enable_in_settings" if state == "appleIntelligenceNotEnabled" else "none"
        return unavailable(state, probe.get("fix") or "Apple Intelligence is unavailable on this Mac",
                           action=action, model=MODEL_NAME)

    # --- planning -----------------------------------------------------------

    def plan(self, req: BrainRequest, *, timeout_s: float) -> BrainResult:
        cached = self._cached_draft(req)
        if cached is not None:
            return self._expand(cached, req, latency=0)
        if not self.availability()["available"]:
            return BrainResult.failure(self.id, "unavailable", model=MODEL_NAME)
        now = self._clock()
        if now < self._busy_until:
            return BrainResult.failure(self.id, "busy", model=MODEL_NAME)
        script = _script_of(req.prompt)
        if script in self._blocked_scripts or (script == "devanagari" and "hi" not in self.languages):
            return BrainResult.failure(self.id, "language", model=MODEL_NAME)
        cards = list(req.recipes)
        result = self._plan_once(req, cards, timeout_s)
        if not result.ok and result.reason == "context" and len(cards) > CONTEXT_RETRY_CARDS:
            result = self._plan_once(req, cards[:CONTEXT_RETRY_CARDS], timeout_s)
        return result

    def _cached_draft(self, req: BrainRequest) -> IntentDraft | None:
        """The draft of the request just planned, only for the hook-text
        pass (`req.hook_text` set): a fresh request always spawns."""
        if req.hook_text is None or self._last_draft is None:
            return None
        key, draft = self._last_draft
        return draft if key == draft_key(req.prompt, req.prior_clarification) else None

    def _plan_once(self, req: BrainRequest, cards: list, timeout_s: float) -> BrainResult:
        payload = {
            "prompt": user_prompt_with_answers(req.prompt, req.prior_clarification),
            "recipes": [c.as_prompt_dict() for c in cards],
            "timeline_summary": facts_to_prompt_block(req.facts),
            "timeout_ms": int(timeout_s * 1000),
        }
        t0 = self._clock()
        rc, out, _err = self._runner([self.helper() or "fm-planner", "plan"],
                                     json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout_s + 2)
        latency = int((self._clock() - t0) * 1000)
        body = _parse_json(out)
        if rc != 0 or not body or not body.get("ok"):
            reason = self._failure_reason(rc, body, script=_script_of(req.prompt))
            return BrainResult.failure(self.id, reason, latency_ms=latency, model=MODEL_NAME)
        return self._draft_to_result(body, req, latency)

    def _failure_reason(self, rc: int, body: dict[str, Any] | None, *, script: str) -> str:
        code = str((body or {}).get("code") or "")
        reason = _JSON_CODE_REASONS.get(code) or EXIT_REASONS.get(rc, "decode")
        if reason == "language":
            self._blocked_scripts.add(script)
        elif reason == "busy":
            self._busy_until = self._clock() + BUSY_BACKOFF_S
        elif reason == "unavailable":
            self._unavailable_until = self._clock() + UNAVAILABLE_BACKOFF_S
            self.invalidate()
        return reason

    def _draft_to_result(self, body: dict[str, Any], req: BrainRequest, latency: int) -> BrainResult:
        raw = body.get("draft") or {}
        try:
            intents = raw.get("intents") if isinstance(raw, dict) else None
            if not isinstance(intents, list):
                raise DraftShapeError("draft.intents must be a list")
            items = [flatten_fm_item(i) for i in intents if isinstance(i, dict)]
            draft = normalize_draft({**raw, "intents": items})
        except (ValidationError, AttributeError, TypeError):
            return BrainResult.failure(self.id, "parse", latency_ms=latency, model=MODEL_NAME)
        self._last_draft = (draft_key(req.prompt, req.prior_clarification), draft)
        return self._expand(draft, req, latency=int(body.get("latency_ms") or latency))

    def _expand(self, draft: IntentDraft, req: BrainRequest, *, latency: int) -> BrainResult:
        draft = strip_model_hook_text(draft, req.prompt)
        try:
            plan = from_intents(draft, req.facts, hook_text=req.hook_text)
        except KeyError as e:
            return BrainResult.failure(self.id, f"rejected:{str(e).strip(chr(39))[:140]}",
                                       latency_ms=latency, model=MODEL_NAME)
        except Exception as e:      # ValidationError inside the expander, Day-0 stub
            return BrainResult.failure(self.id, f"rejected:{(str(e) or type(e).__name__)[:140]}",
                                       latency_ms=latency, model=MODEL_NAME)
        changes: dict[str, Any] = {"brain": self.id}
        if draft.reply and not plan.reply:
            changes["reply"] = draft.reply[:400]
        return BrainResult(plan=plan.with_(**changes), brain=self.id, ok=True, latency_ms=latency,
                           model=MODEL_NAME)

    # --- content tasks (§3.7) --------------------------------------------------

    def text(self, task: TextTask, *, timeout_s: float) -> TextResult | None:
        if not self.availability()["available"] or self._clock() < self._busy_until:
            return None
        head = str(task.payload.get("transcript_head") or "")
        if task.kind == "hook_candidates" and _script_of(head) in self._blocked_scripts:
            return None
        payload = {"task": task.kind, "payload": task.payload, "timeout_ms": int(timeout_s * 1000)}
        t0 = self._clock()
        rc, out, _err = self._runner([self.helper() or "fm-planner", "text"],
                                     json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout_s + 2)
        body = _parse_json(out)
        if rc != 0 or not body or not body.get("ok"):
            self._failure_reason(rc, body, script=_script_of(head))
            return None
        items = body.get("items")
        if not isinstance(items, list):
            return None
        return TextResult(items=items, brain=self.id, model=MODEL_NAME,
                          latency_ms=int(body.get("latency_ms") or (self._clock() - t0) * 1000))
