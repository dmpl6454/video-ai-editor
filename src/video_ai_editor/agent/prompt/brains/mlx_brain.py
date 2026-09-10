"""The `local_model` brain: Qwen2.5-Instruct-4bit through mlx-lm (spec §3.3).

What the baseline measured (findings 12/13) and what this adapter does about it:

  * Given raw tool schemas the 7B model invented arg names; given recipe
    cards it emitted none → the prompt is `prompt_text.mlx_system_prompt`
    (cards + compact IntentDraft schema), and the answer goes
    `jsonfix.repair` → `normalize_draft` → `recipes.from_intents`.
  * Warm load 2.4 s, plan ~6 s on the M4 Max → the model stays loaded in
    this object; the router's 12 s budget is enough for one attempt.
  * The 0.5B model emitted degenerate repeated steps → never offered
    (`models.MODEL_TIERS`), and `normalize_draft` dedupes anyway.

Measured through THIS adapter on the build Mac (M4 Max 36 GB, 2026-09-10,
`uv run --with mlx-lm==0.31.3`, `HF_HUB_OFFLINE=1`, the cached 7B loaded
from its snapshot directory): load 1.8 s; 8/8 representative prompts →
valid plans in 2.2–3.4 s with zero unknown recipes; the model filled the
hook `text` slot with a generic line ("Get ready for an amazing story!")
— stripped by `content.strip_model_hook_text` so the content pass asks it
again WITH the transcript (0.7 s, grounded: "the battery only lasts ninety
minutes"); its first `hook_candidates` answer was mis-bracketed
(`{"items": ["a"], ["b"]}`) → the worked example in the prompt plus
`content.parse_text_items`.

Rules that are not negotiable:
  * `mlx_lm` is imported lazily. Its absence is an honest `fix`
    ("uv sync --extra local-llm"), never an ImportError at import time.
  * Load is offline: `HF_HUB_OFFLINE=1` is set in-process before
    `mlx_lm.load(<snapshot DIRECTORY>)` — never a repo id.
  * One generation at a time (`_GEN_LOCK`). A held lock is `busy`. On
    timeout the worker keeps the lock until the abandoned generate returns
    (MLX has no cooperative cancel) and the brain reports `busy` meanwhile.
  * `MemoryError` unloads the model so whisper/reframe can have the RAM.
"""
from __future__ import annotations

import gc
import importlib
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .. import models
from ..recipes import from_intents
from ..schema import IntentDraft
from .base import (Availability, BrainRequest, BrainResult, TextResult, TextTask, available,
                   unavailable)
from .content import parse_text_items, strip_model_hook_text, text_task_prompts
from .jsonfix import JsonRepairFailed, repair
from .prompt_text import (DraftShapeError, draft_key, facts_to_prompt_block, mlx_system_prompt,
                          normalize_draft, user_prompt_with_answers)

__all__ = ["MLXBrain", "MAX_TOKENS", "PROBE_TTL_S", "PIP_FIX", "FROZEN_FIX", "MIN_RAM_GB",
           "LoaderFn", "GeneratorFn"]

_log = logging.getLogger(__name__)

MAX_TOKENS = 500
TEXT_MAX_TOKENS = 200
PROBE_TTL_S = 60.0
MIN_RAM_GB = 12
PIP_FIX = "uv sync --extra local-llm"
FROZEN_FIX = "Run from source: uv sync --extra local-llm"

LoaderFn = Callable[[str], tuple[Any, Any]]
GeneratorFn = Callable[[Any, Any, str, int], str]

#: Process-wide: load + generate never overlap, whatever brain instance asks.
_GEN_LOCK = threading.Lock()


def _default_import() -> Any:
    return importlib.import_module("mlx_lm")


def _default_load(snapshot: str) -> tuple[Any, Any]:
    mlx_lm = _default_import()
    return mlx_lm.load(snapshot)


def _default_generate(model: Any, tokenizer: Any, prompt: str, max_tokens: int) -> str:
    mlx_lm = _default_import()
    try:
        from mlx_lm.sample_utils import make_sampler
        sampler = make_sampler(temp=0.0)          # greedy
    except ImportError:                             # older mlx-lm: temp kwarg
        return mlx_lm.generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False)
    return mlx_lm.generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, sampler=sampler,
                           verbose=False)


def _chat_prompt(tokenizer: Any, system: str, user: str) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    apply = getattr(tokenizer, "apply_chat_template", None)
    if apply is not None:
        try:
            return apply(messages, tokenize=False, add_generation_prompt=True)
        except Exception:
            pass
    return f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"


def _display_home(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class MLXBrain:
    id = "local_model"

    def __init__(self, *, model_id: str | None = None, hf_home_dir: str | os.PathLike | None = None,
                 ram_bytes: int | None = None, frozen: bool | None = None,
                 importer: Callable[[], Any] | None = None, loader: LoaderFn | None = None,
                 generator: GeneratorFn | None = None, darwin_arm64: bool | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._model_override = model_id
        self._hf_home = hf_home_dir
        self._ram = ram_bytes
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._import = importer or _default_import
        self._load = loader or _default_load
        self._generate = generator or _default_generate
        self._darwin_arm64 = ((sys.platform == "darwin" and platform.machine() == "arm64")
                              if darwin_arm64 is None else darwin_arm64)
        self._clock = clock
        self._availability: Availability | None = None
        self._probe_at = -1e9
        self._loaded: tuple[Any, Any] | None = None
        self._loaded_from: str | None = None
        #: One-entry draft cache (`draft_key` → draft): the router's hook-text
        #: pass (§3.7) re-asks with `req.hook_text` set and gets this draft
        #: re-expanded — never a second 6 s generation.
        self._last_draft: tuple[str, IntentDraft] | None = None

    # --- availability -----------------------------------------------------------

    def invalidate(self) -> None:
        self._availability = None
        self._probe_at = -1e9

    def tier(self) -> models.ModelTier | None:
        return models.pick_tier(self._ram, override=self._model_override)

    def availability(self) -> Availability:
        now = self._clock()
        if self._availability is not None and now - self._probe_at < PROBE_TTL_S:
            return self._availability
        self._availability = self._compute_availability()
        self._probe_at = now
        return self._availability

    def _compute_availability(self) -> Availability:
        if not self._darwin_arm64:
            return unavailable("needs an Apple silicon Mac (mlx runs on Metal)", None)
        if self._frozen:
            return unavailable("excluded from the packaged app", FROZEN_FIX)
        tier = self.tier()
        try:
            self._import()
        except ImportError:
            return unavailable("not installed (pip)", PIP_FIX, action="install",
                               model=tier.id if tier else None)
        if tier is None:
            ram_gb = (models.total_ram_bytes() if self._ram is None else self._ram) / models.GB
            return unavailable(f"needs at least {MIN_RAM_GB} GB of RAM (this Mac has {ram_gb:.0f} GB)", None)
        st = models.status(tier.id, hf_home_dir=self._hf_home)
        if not st.installed:
            size = tier.approx_label if tier.approx_label != "custom" else "size unknown"
            return unavailable("installed; model not downloaded",
                               f"Download {tier.id} ({size}) from the brain menu — it asks first",
                               action="download", model=tier.id)
        return available(f"installed; model cached at {_display_home(models.hf_home(self._hf_home))}",
                         model=tier.id)

    # --- generation plumbing ------------------------------------------------------

    def unload(self) -> None:
        self._loaded = None
        self._loaded_from = None
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass

    def _ensure_loaded(self) -> tuple[Any, Any]:
        tier = self.tier()
        snapshot = models.snapshot_dir(tier.id, hf_home_dir=self._hf_home) if tier else None
        if snapshot is None:
            raise FileNotFoundError("model snapshot is not installed")
        if self._loaded is not None and self._loaded_from == str(snapshot):
            return self._loaded
        # WHY setdefault: a user who deliberately set HF_HUB_OFFLINE=0 keeps
        # it; everyone else can never trigger a fetch from a load.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        self._loaded = self._load(str(snapshot))
        self._loaded_from = str(snapshot)
        return self._loaded

    def _run_guarded(self, system: str, user: str, max_tokens: int, timeout_s: float
                     ) -> tuple[str | None, str]:
        """(output, reason). Runs load+generate under `_GEN_LOCK` in a worker
        so a wall-clock timeout can give up without killing MLX; the worker
        releases the lock when the abandoned generate finally returns."""
        if not _GEN_LOCK.acquire(blocking=False):
            return None, "busy"
        box: dict[str, Any] = {}

        def _work() -> None:
            try:
                model, tokenizer = self._ensure_loaded()
                box["out"] = self._generate(model, tokenizer, _chat_prompt(tokenizer, system, user), max_tokens)
            except MemoryError:
                self.unload()
                box["reason"] = "unavailable"
            except FileNotFoundError:
                box["reason"] = "unavailable"
            except Exception as e:  # a broken install must fall through, not crash the turn
                _log.warning("local model generate failed (%s): %s", type(e).__name__, e)
                box["reason"] = "decode"
            finally:
                _GEN_LOCK.release()

        worker = threading.Thread(target=_work, name="vai-mlx-generate", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            return None, "timeout"
        if "out" in box:
            return str(box["out"]), ""
        return None, box.get("reason", "decode")

    # --- planning ---------------------------------------------------------------

    def plan(self, req: BrainRequest, *, timeout_s: float) -> BrainResult:
        av = self.availability()
        model = av.get("model") or ""
        cached = self._cached_draft(req)
        if cached is not None:
            return self._expand(cached, req, latency=0, model=model)
        if not av["available"]:
            return BrainResult.failure(self.id, "unavailable", model=model)
        system = mlx_system_prompt(list(req.recipes), facts_to_prompt_block(req.facts))
        user = user_prompt_with_answers(req.prompt, req.prior_clarification)
        t0 = self._clock()
        out, reason = self._run_guarded(system, user, MAX_TOKENS, timeout_s)
        latency = int((self._clock() - t0) * 1000)
        if out is None:
            return BrainResult.failure(self.id, reason, latency_ms=latency, model=model)
        try:
            draft = normalize_draft(repair(out))
        except (JsonRepairFailed, ValidationError, DraftShapeError):
            return BrainResult.failure(self.id, "parse", latency_ms=latency, model=model)
        self._last_draft = (draft_key(req.prompt, req.prior_clarification), draft)
        return self._expand(draft, req, latency=latency, model=model)

    def _cached_draft(self, req: BrainRequest) -> IntentDraft | None:
        """The draft of the request just planned, only for the hook-text
        pass (`req.hook_text` set): a fresh request always generates."""
        if req.hook_text is None or self._last_draft is None:
            return None
        key, draft = self._last_draft
        return draft if key == draft_key(req.prompt, req.prior_clarification) else None

    def _expand(self, draft: IntentDraft, req: BrainRequest, *, latency: int, model: str) -> BrainResult:
        draft = strip_model_hook_text(draft, req.prompt)
        try:
            plan = from_intents(draft, req.facts, hook_text=req.hook_text)
        except KeyError as e:
            return BrainResult.failure(self.id, f"rejected:{str(e).strip(chr(39))[:140]}",
                                       latency_ms=latency, model=model)
        except Exception as e:
            return BrainResult.failure(self.id, f"rejected:{(str(e) or type(e).__name__)[:140]}",
                                       latency_ms=latency, model=model)
        changes: dict[str, Any] = {"brain": self.id}
        if draft.reply and not plan.reply:
            changes["reply"] = draft.reply[:400]
        return BrainResult(plan=plan.with_(**changes), brain=self.id, ok=True, latency_ms=latency, model=model)

    # --- content tasks (§3.7) ----------------------------------------------------------

    def text(self, task: TextTask, *, timeout_s: float) -> TextResult | None:
        av = self.availability()
        if not av["available"]:
            return None
        prompts = text_task_prompts(task)
        if prompts is None:
            return None
        system, user = prompts
        t0 = self._clock()
        out, _reason = self._run_guarded(system, user, TEXT_MAX_TOKENS, timeout_s)
        if out is None:
            return None
        items = parse_text_items(out, task.kind)
        if not items:
            return None
        return TextResult(items=items, brain=self.id, model=av.get("model") or "",
                          latency_ms=int((self._clock() - t0) * 1000))
