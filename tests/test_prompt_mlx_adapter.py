"""The local-model adapter (spec §3.3, §3.6): `HF_HUB_OFFLINE` is set and
`load()` receives a DIRECTORY, the busy lock, the wall-clock timeout that
leaves the lock with the abandoned worker, MemoryError unloads, the honest
availability ladder, and cache discovery against a fake `HF_HOME` tree."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from video_ai_editor.agent.prompt import models, recipes, schema
from video_ai_editor.agent.prompt.brains import mlx_brain
from video_ai_editor.agent.prompt.brains.base import BrainRequest, TextTask
from video_ai_editor.agent.prompt.facts import TimelineFacts
from test_prompt_models import make_snapshot

SEVEN_B = "mlx-community/Qwen2.5-7B-Instruct-4bit"
GB = models.GB
GOOD = ('```json\n{"intents": [{"recipe": "remove_silences", "slots": {}}, {"recipe": "captions", "slots": '
        '{"style": "ig_chunky"}}], "exclusions": [], "needs_input": [], "confidence": 0.9, "reply": "ok"}\n```')


class Tokenizer:
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return "\n".join(f"<{m['role']}>{m['content']}" for m in messages) + "\n<assistant>"


class Harness:
    def __init__(self, tmp_path, *, output=GOOD, installed=True, import_ok=True, generate=None):
        self.tmp = tmp_path
        if installed:
            make_snapshot(tmp_path, SEVEN_B, shards={"model.safetensors": True})
        self.output = output
        self.import_ok = import_ok
        self.loads: list[str] = []
        self.prompts: list[str] = []
        self.offline_at_load: list[str | None] = []
        self._generate = generate

    def importer(self):
        if not self.import_ok:
            raise ImportError("No module named 'mlx_lm'")
        return object()

    def loader(self, path):
        self.loads.append(path)
        self.offline_at_load.append(os.environ.get("HF_HUB_OFFLINE"))
        return object(), Tokenizer()

    def generator(self, model, tokenizer, prompt, max_tokens):
        self.prompts.append(prompt)
        if self._generate is not None:
            return self._generate(prompt, max_tokens)
        return self.output

    def brain(self, **kw) -> mlx_brain.MLXBrain:
        kw.setdefault("ram_bytes", 36 * GB)
        kw.setdefault("frozen", False)
        kw.setdefault("darwin_arm64", True)
        return mlx_brain.MLXBrain(hf_home_dir=self.tmp, importer=self.importer, loader=self.loader,
                                  generator=self.generator, **kw)


def fake_from_intents(draft, facts, *, hook_text=None, allow_downloads=True):
    fake_from_intents.hook_texts.append(hook_text)
    unknown = [i.recipe for i in draft.intents if i.recipe not in recipes.RECIPE_BY_NAME]
    if unknown:                                   # the frozen contract (recipes.from_intents)
        raise KeyError(f"unknown recipe(s): {unknown}")
    steps = [schema.Step(tool=f"recipe:{i.recipe}", args=dict(i.slots), why="test") for i in draft.intents]
    return schema.Plan.new(intent=draft.intents[0].recipe if draft.intents else "ask", brain="recipes",
                           steps=steps, confidence=draft.confidence,
                           content_brain=hook_text[1] if hook_text else None)


fake_from_intents.hook_texts = []   # type: ignore[attr-defined]


@pytest.fixture
def expander(monkeypatch):
    monkeypatch.setattr(mlx_brain, "from_intents", fake_from_intents)
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)


@pytest.fixture(autouse=True)
def free_lock():
    yield
    # A test that timed out leaves the worker holding the lock briefly; never
    # let that leak into the next test.
    for _ in range(50):
        if mlx_brain._GEN_LOCK.acquire(blocking=False):
            mlx_brain._GEN_LOCK.release()
            return
        time.sleep(0.05)


def request(prompt="tighten it up and add captions") -> BrainRequest:
    return BrainRequest(prompt=prompt, facts=TimelineFacts.minimal(), recipes=recipes.cards())


# --- availability ladder ---------------------------------------------------------

def test_not_installed_is_a_pip_fix_never_an_import_error(tmp_path, expander):
    av = Harness(tmp_path, import_ok=False).brain().availability()
    assert av == {"available": False, "detail": "not installed (pip)", "fix": mlx_brain.PIP_FIX,
                  "action": "install", "model": SEVEN_B}


def test_frozen_and_non_apple_silicon_and_low_ram(tmp_path, expander):
    h = Harness(tmp_path)
    assert h.brain(frozen=True).availability()["fix"] == mlx_brain.FROZEN_FIX
    assert not h.brain(darwin_arm64=False).availability()["available"]
    low = h.brain(ram_bytes=8 * GB).availability()
    assert not low["available"] and "12 GB" in low["detail"] and "8 GB" in low["detail"]


def test_installed_but_model_missing_offers_a_download(tmp_path, expander):
    av = Harness(tmp_path, installed=False).brain().availability()
    assert not av["available"] and av["detail"] == "installed; model not downloaded"
    assert av["action"] == "download" and av["model"] == SEVEN_B and "asks first" in av["fix"]


def test_cached_model_is_available_and_names_the_cache(tmp_path, expander):
    av = Harness(tmp_path).brain().availability()
    assert av["available"] and av["model"] == SEVEN_B
    assert av["detail"].startswith("installed; model cached at")
    assert "/Users/" not in av["detail"] or str(Path.home()) not in av["detail"]


def test_availability_is_cached_sixty_seconds(tmp_path, expander, monkeypatch):
    calls = []
    real = models.status
    monkeypatch.setattr(models, "status", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    t = [0.0]
    b = Harness(tmp_path).brain(clock=lambda: t[0])
    b.availability(); b.availability()
    assert len(calls) == 1
    t[0] += 61
    b.availability()
    assert len(calls) == 2
    b.invalidate(); b.availability()
    assert len(calls) == 3


# --- generation --------------------------------------------------------------------

def test_plan_loads_offline_from_a_directory_and_expands_the_draft(tmp_path, expander, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    h = Harness(tmp_path)
    b = h.brain()
    res = b.plan(request(), timeout_s=5)
    assert res.ok and res.brain == "local_model" and res.model == SEVEN_B
    assert h.offline_at_load == ["1"], "HF_HUB_OFFLINE must be set before load()"
    assert len(h.loads) == 1 and Path(h.loads[0]).is_dir() and "/" in h.loads[0]
    assert h.loads[0] != SEVEN_B                      # never a repo id
    assert [s.tool for s in res.plan.steps] == ["recipe:remove_silences", "recipe:captions"]
    assert res.plan.brain == "local_model" and res.plan.reply == "ok"
    # The prompt carries recipe cards and the facts block, no tool schemas, no paths.
    assert "remove_silences" in h.prompts[0] and "auto_caption" not in h.prompts[0]
    assert "intents" in h.prompts[0] and "/Users" not in h.prompts[0]
    b.plan(request(), timeout_s=5)
    assert len(h.loads) == 1, "the model stays loaded between turns"


def test_user_offline_override_is_respected(tmp_path, expander, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    h = Harness(tmp_path)
    h.brain().plan(request(), timeout_s=5)
    assert h.offline_at_load == ["0"]


def test_busy_when_the_lock_is_held(tmp_path, expander):
    h = Harness(tmp_path)
    assert mlx_brain._GEN_LOCK.acquire(blocking=False)
    try:
        res = h.brain().plan(request(), timeout_s=5)
    finally:
        mlx_brain._GEN_LOCK.release()
    assert not res.ok and res.reason == "busy" and h.loads == []


def test_timeout_leaves_the_lock_with_the_worker_then_recovers(tmp_path, expander):
    release = threading.Event()

    def slow(prompt, max_tokens):
        release.wait(5)
        return GOOD

    h = Harness(tmp_path, generate=slow)
    b = h.brain()
    res = b.plan(request(), timeout_s=0.2)
    assert res.reason == "timeout"
    assert b.plan(request(), timeout_s=0.2).reason == "busy", "abandoned generate still owns the lock"
    release.set()
    for _ in range(50):
        if mlx_brain._GEN_LOCK.acquire(blocking=False):
            mlx_brain._GEN_LOCK.release()
            break
        time.sleep(0.05)
    assert b.plan(request(), timeout_s=5).ok


def test_memory_error_unloads_and_reports_unavailable(tmp_path, expander):
    def oom(prompt, max_tokens):
        raise MemoryError("Metal out of memory")
    h = Harness(tmp_path, generate=oom)
    b = h.brain()
    res = b.plan(request(), timeout_s=5)
    assert res.reason == "unavailable" and b._loaded is None
    assert not mlx_brain._GEN_LOCK.locked()


def test_garbage_is_parse_and_unknown_recipe_is_rejected(tmp_path, expander):
    assert Harness(tmp_path, output="I cannot help with that.").brain().plan(request(), timeout_s=5).reason == "parse"
    # Valid JSON with the wrong shape is still `parse` — never a zero-step plan.
    assert Harness(tmp_path, output='{"intents": "captions please"}').brain().plan(request(), timeout_s=5).reason == "parse"
    bad = json.dumps({"intents": [{"recipe": "explode"}], "confidence": 0.9})
    res = Harness(tmp_path, output=bad).brain().plan(request(), timeout_s=5)
    assert res.reason.startswith("rejected:") and "explode" in res.reason


def test_flat_slot_keys_and_list_values_are_normalised(tmp_path, expander):
    flat = json.dumps({"intents": [{"recipe": "reframe", "ratio": "9:16"},
                                   {"recipe": "remove_fillers", "slots": {"words": ["um", "uh"]}},
                                   {"recipe": "reframe", "ratio": "9:16"}],
                       "confidence": "0.8", "reply": "done"})
    res = Harness(tmp_path, output=flat).brain().plan(request(), timeout_s=5)
    assert res.ok
    assert [s.args for s in res.plan.steps] == [{"ratio": "9:16"}, {"words": "um, uh"}]   # deduped
    assert res.plan.confidence == 0.8


def test_unstated_confidence_gets_the_documented_default(tmp_path, expander):
    # Pre-flight 2026-09-08: the 7B model left confidence at 0.0 on 12/24
    # prompts whose intents were right; a stated value is kept as-is.
    from video_ai_editor.agent.prompt.brains.prompt_text import UNSTATED_CONFIDENCE
    zero = json.dumps({"intents": [{"recipe": "captions", "slots": {}}], "confidence": 0.0})
    assert Harness(tmp_path, output=zero).brain().plan(request(), timeout_s=5).plan.confidence == UNSTATED_CONFIDENCE
    absent = json.dumps({"intents": [{"recipe": "captions", "slots": {}}]})
    assert Harness(tmp_path, output=absent).brain().plan(request(), timeout_s=5).plan.confidence == UNSTATED_CONFIDENCE
    stated = json.dumps({"intents": [{"recipe": "captions", "slots": {}}], "confidence": 0.35})
    assert Harness(tmp_path, output=stated).brain().plan(request(), timeout_s=5).plan.confidence == 0.35
    empty = json.dumps({"intents": [], "confidence": 0})
    res = Harness(tmp_path, output=empty).brain().plan(request(), timeout_s=5)
    assert res.ok and res.plan.confidence == 0.0 and res.plan.steps == []


def test_generator_exception_falls_through_as_decode(tmp_path, expander):
    def broken(prompt, max_tokens):
        raise RuntimeError("metal kernel failed")
    assert Harness(tmp_path, generate=broken).brain().plan(request(), timeout_s=5).reason == "decode"


def test_text_tasks(tmp_path, expander):
    h = Harness(tmp_path, output='{"items": ["Nobody tells you this", "Second one"]}')
    res = h.brain().text(TextTask(kind="hook_candidates", payload={"transcript_head": "three things", "n": 2}),
                         timeout_s=5)
    assert res.items == ["Nobody tells you this", "Second one"] and res.brain == "local_model"
    r = Harness(tmp_path, output='{"items": [2, 0]}').brain().text(
        TextTask(kind="rank_windows", payload={"windows": [{"text": "a"}, {"text": "b"}, {"text": "c"}], "k": 2}),
        timeout_s=5)
    assert r.items == [2, 0]
    assert Harness(tmp_path, output="nope").brain().text(
        TextTask(kind="hook_candidates", payload={"transcript_head": "x"}), timeout_s=5) is None
    assert Harness(tmp_path, import_ok=False).brain().text(
        TextTask(kind="hook_candidates", payload={"transcript_head": "x"}), timeout_s=5) is None


def test_default_generate_uses_a_greedy_sampler(monkeypatch):
    calls = {}

    class FakeMLX:
        @staticmethod
        def generate(model, tokenizer, **kw):
            calls.update(kw)
            return "{}"

    import types
    fake_sample = types.ModuleType("mlx_lm.sample_utils")
    fake_sample.make_sampler = lambda temp: ("sampler", temp)
    monkeypatch.setitem(__import__("sys").modules, "mlx_lm", FakeMLX)
    monkeypatch.setitem(__import__("sys").modules, "mlx_lm.sample_utils", fake_sample)
    monkeypatch.setattr(mlx_brain, "_default_import", lambda: FakeMLX)
    assert mlx_brain._default_generate(None, None, "p", 500) == "{}"
    assert calls["sampler"] == ("sampler", 0.0) and calls["max_tokens"] == 500 and calls["verbose"] is False


def test_hook_text_pass_reuses_the_draft_without_a_second_generation(tmp_path, expander):
    h = Harness(tmp_path)
    b = h.brain()
    first = b.plan(request(), timeout_s=5)
    assert first.ok and first.plan.content_brain is None and len(h.prompts) == 1
    del fake_from_intents.hook_texts[:]
    again = b.plan(request().with_(hook_text=("NOBODY TELLS YOU THIS", "local_model")), timeout_s=5)
    assert again.ok and again.plan.content_brain == "local_model" and again.brain == "local_model"
    assert len(h.prompts) == 1, "the hook-text pass re-expands the cached draft, never re-generates"
    assert fake_from_intents.hook_texts == [("NOBODY TELLS YOU THIS", "local_model")]
    assert b.plan(request("other").with_(hook_text=("X", "local_model")), timeout_s=5).ok
    assert len(h.prompts) == 2
    b.plan(request(), timeout_s=5)
    assert len(h.prompts) == 3, "a fresh request always generates"


def test_tier_follows_ram_and_the_override(tmp_path, expander, monkeypatch):
    h = Harness(tmp_path, installed=False)
    assert h.brain(ram_bytes=36 * GB).tier().id == SEVEN_B
    mid = h.brain(ram_bytes=16 * GB)
    assert mid.tier().id == "mlx-community/Qwen2.5-3B-Instruct-4bit"
    av = mid.availability()
    assert av["model"] == mid.tier().id and av["action"] == "download" and "1.8 GB" in av["fix"]
    assert h.brain(ram_bytes=8 * GB).tier() is None
    monkeypatch.setenv(models.DEFAULT_MODEL_ENV, "mlx-community/Custom-4bit")
    custom = h.brain(ram_bytes=8 * GB)
    assert custom.tier().id == "mlx-community/Custom-4bit"
    assert "size unknown" in custom.availability()["fix"]


@pytest.mark.skipif(not (__import__("sys").platform == "darwin" and __import__("platform").machine() == "arm64"),
                    reason="the real local-model ladder is an Apple-silicon question")
def test_real_machine_availability_is_honest(monkeypatch):
    """No fakes: what `brains_report()` says about THIS Mac must be true of
    it — mlx_lm importable or not, the tier model cached or not."""
    import importlib.util
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    b = mlx_brain.MLXBrain(frozen=False)
    av = b.availability()
    tier = b.tier()
    if tier is None:
        assert not av["available"] and "GB of RAM" in av["detail"]
        return
    if importlib.util.find_spec("mlx_lm") is None:
        assert av == {"available": False, "detail": "not installed (pip)", "fix": mlx_brain.PIP_FIX,
                      "action": "install", "model": tier.id}
        return
    cached = models.status(tier.id).installed
    assert av["available"] is cached and av["model"] == tier.id
    if cached:
        assert av["detail"].startswith("installed; model cached at")
    else:
        assert av["action"] == "download" and av["fix"]


def test_cache_discovery_matches_the_manager(tmp_path, expander):
    h = Harness(tmp_path)
    st = models.status(SEVEN_B, hf_home_dir=tmp_path)
    assert st.installed and h.brain().availability()["available"]
    assert models.snapshot_dir(SEVEN_B, hf_home_dir=tmp_path) == Path(st.snapshot_path)


def test_model_written_hook_text_is_dropped_so_the_content_pass_can_ground_it(tmp_path, monkeypatch):
    """The REAL expander: a draft whose hook carries the model's own generic
    line expands to the transcript heuristic (content_brain None), which is
    exactly what makes the router ask this brain for grounded words."""
    monkeypatch.delenv(models.DEFAULT_MODEL_ENV, raising=False)
    from video_ai_editor.agent.prompt import recipes as R
    from video_ai_editor.agent.prompt.brains import content
    head = "Um, so today I want to show you three things about this camera that nobody tells you."
    facts = TimelineFacts.minimal(has_transcript=True, words=30, speech_seconds=10.0, transcript_head=head)
    generic = json.dumps({"intents": [{"recipe": "hook", "slots": {"text": "Get ready for an amazing story!"}}],
                          "confidence": 1.0, "reply": "Hook added."})
    b = Harness(tmp_path, output=generic).brain()
    res = b.plan(BrainRequest(prompt="add a hook at the start", facts=facts, recipes=R.cards()), timeout_s=5)
    assert res.ok
    hook = next(s for s in res.plan.steps if s.tool == "apply_hook_stack")
    assert hook.args["text"] == R.heuristic_hook(head) and res.plan.content_brain is None
    assert content.needs_hook_text(res.plan, facts)
    # The user's own words survive verbatim.
    own = json.dumps({"intents": [{"recipe": "hook", "slots": {"text": "nobody tells you this"}}], "confidence": 1.0})
    res2 = Harness(tmp_path, output=own).brain().plan(
        BrainRequest(prompt='add a hook saying "nobody tells you this"', facts=facts, recipes=R.cards()), timeout_s=5)
    hook2 = next(s for s in res2.plan.steps if s.tool == "apply_hook_stack")
    assert hook2.args["text"] == "nobody tells you this" and not content.needs_hook_text(res2.plan, facts)


def test_text_task_parses_the_real_models_mis_bracketed_answer(tmp_path, expander):
    raw = '{"items": ["Autofocus hunts in low light"], ["Battery lasts just ninety minutes"], ["Three hidden flaws"]}'
    h = Harness(tmp_path, output=raw)
    res = h.brain().text(TextTask(kind="hook_candidates", payload={"transcript_head": "three things", "n": 3}),
                         timeout_s=5)
    assert res.items == ["Autofocus hunts in low light", "Battery lasts just ninety minutes", "Three hidden flaws"]
    assert '{"items": ["hook line 1", "hook line 2", "hook line 3"]}' in h.prompts[-1], "the worked example is in the prompt"
    assert Harness(tmp_path, output=raw).brain().text(
        TextTask(kind="hook_candidates", payload={"transcript_head": ""}), timeout_s=5) is None, "nothing to ground in"
