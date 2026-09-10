"""The brain ladder (spec §3.4) and `brains_report()` (§0.2), driven with
fake brains: recipes-if-confident → FM → MLX → cloud-if-key → recipes ≥ 0.4
with "I read that as" → clarify. Every attempt is a `brain` event and the
report always says which rung answered and why a better one could not."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest


from video_ai_editor.agent.prompt import recipes, schema, service
from video_ai_editor.agent.prompt.brains import cloud_plan, router
from video_ai_editor.agent.prompt.brains.base import (BRAIN_IDS, BrainRequest, BrainResult, TextResult,
                                                      TextTask, available, unavailable)
from video_ai_editor.agent.prompt.brains.recipes_brain import RecipesBrain
from video_ai_editor.agent.prompt.facts import TimelineFacts
from video_ai_editor.agent.prompt.recipes import cards

CAMERA = ("Um, so today I want to show you three things about this camera that nobody tells you. "
          "First, the autofocus hunts in low light.")
SPOKEN = dict(has_transcript=True, words=40, speech_seconds=20.0, transcript_head=CAMERA)


def hook_step(plan: schema.Plan) -> schema.Step:
    return next(s for s in plan.steps if s.tool == "apply_hook_stack")


def mkplan(confidence: float, brain: str = "recipes", intent: str = "captions", **fields) -> schema.Plan:
    step = schema.Step(tool="add_caption_track", args={"style": "ig_chunky"}, why="captions")
    return schema.Plan.new(intent=intent, brain=brain, steps=[step], confidence=confidence, title="Add captions",
                           postconditions=schema.bind_postconditions("add_caption_track", step.args), **fields)


class Fake:
    """`result` may be a Plan, a failure reason string, or a callable
    `(req) -> Plan` (so a test can answer the hook-text pass differently);
    `text_items` is what `text()` returns (None = cannot write)."""

    def __init__(self, bid, *, result=None, avail=True, fix="do the thing", model="m", raise_=None, delay=0.0,
                 clock=None, text_items=None):
        self.id = bid
        self._result = result
        self._avail = avail
        self._fix = fix
        self._model = model
        self._raise = raise_
        self._delay = delay
        self._clock = clock
        self._text_items = text_items
        self.plans = 0
        self.texts = 0
        self.requests = []
        self.invalidated = 0

    def availability(self):
        return available("ready", model=self._model) if self._avail else unavailable("off", self._fix, model=self._model)

    def invalidate(self):
        self.invalidated += 1

    def plan(self, req, *, timeout_s):
        self.plans += 1
        self.requests.append(req)
        self.last_timeout = timeout_s
        if self._clock is not None:
            self._clock.t += self._delay
        if self._raise:
            raise self._raise
        if self._result is None:
            return BrainResult.failure(self.id, "decode", model=self._model)
        if isinstance(self._result, str):
            return BrainResult.failure(self.id, self._result, model=self._model)
        result = self._result(req) if callable(self._result) else self._result
        return BrainResult(plan=result, brain=self.id, ok=True, model=self._model, latency_ms=7)

    def text(self, task: TextTask, *, timeout_s):
        self.texts += 1
        if self._text_items is None:
            return None
        return TextResult(items=list(self._text_items), brain=self.id, model=self._model)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def req(prompt: str = "do a thing", **facts) -> BrainRequest:
    return BrainRequest(prompt=prompt, facts=TimelineFacts.minimal(**facts), recipes=cards())


def identity(plan, facts):
    return plan


class Rejected(ValueError):
    def __init__(self, *reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def run(brains: dict, request: BrainRequest | None = None, **kw):
    events = []
    kw.setdefault("validate", identity)
    kw.setdefault("prefetch", False)
    out = router.plan(request or req(), brains=brains, emit=events.append, order=kw.pop("order", tuple(BRAIN_IDS)),
                      **kw)
    return out, events


def test_confident_recipes_answers_without_touching_a_model():
    fm, mlx = Fake("apple_intelligence", result=mkplan(0.9)), Fake("local_model", result=mkplan(0.9))
    out, events = run({"recipes": Fake("recipes", result=mkplan(0.8)), "apple_intelligence": fm, "local_model": mlx})
    assert out.answered and out.brain == "recipes" and out.plan.brain == "recipes"
    assert fm.plans == 0 and mlx.plans == 0
    assert [a.status for a in out.attempts] == ["answered"]
    assert [e["status"] for e in events] == ["trying", "answered"]
    assert all(e["type"] == "brain" and e["status"] in service.BRAIN_STATUSES for e in events)
    assert events[-1]["label"] == "Recipes" and "confidence 0.80" in events[-1]["detail"]


def test_ladder_falls_through_unavailable_fm_to_mlx_and_validates():
    seen = []

    def validate(plan, facts):
        seen.append(plan.brain)
        return plan.with_(title="validated")

    brains = {"recipes": Fake("recipes", result=mkplan(0.6)),
              "apple_intelligence": Fake("apple_intelligence", avail=False, fix="Turn on Apple Intelligence in System Settings"),
              "local_model": Fake("local_model", result=mkplan(0.8, "local_model"), model="Qwen"),
              "claude": Fake("claude", result=mkplan(0.99, "claude"))}
    out, events = run(brains, validate=validate)
    assert out.brain == "local_model" and out.plan.title == "validated" and out.plan.brain == "local_model"
    assert seen == ["local_model"]
    assert brains["claude"].plans == 0
    fm_attempt = next(a for a in out.attempts if a.brain == "apple_intelligence")
    assert fm_attempt.status == "failed" and fm_attempt.reason == "unavailable"
    assert "Turn on Apple Intelligence" in fm_attempt.detail
    assert [e["status"] for e in events] == ["trying", "failed", "failed", "trying", "answered"]
    assert events[-1]["model"] == "Qwen" and events[-1]["latency_ms"] == 7


def test_rejected_and_low_confidence_llm_plans_continue_and_recipes_fallback_prefixes_reply():
    def validate(plan, facts):
        if plan.brain == "local_model":
            raise Rejected("unknown arg gain_db", "path not offered")
        return plan

    brains = {"recipes": Fake("recipes", result=mkplan(0.6, reply="Captions coming.")),
              "apple_intelligence": Fake("apple_intelligence", result=mkplan(0.3, "apple_intelligence")),
              "local_model": Fake("local_model", result=mkplan(0.9, "local_model")),
              "claude": Fake("claude", avail=False, fix="Add ANTHROPIC_API_KEY")}
    out, _ = run(brains, validate=validate)
    reasons = {a.brain: a.reason for a in out.attempts if a.status == "failed"}
    assert reasons["apple_intelligence"].startswith("rejected:low confidence")
    assert reasons["local_model"] == "rejected:unknown arg gain_db; path not offered"
    assert reasons["claude"] == "unavailable"
    assert out.brain == "recipes" and out.note == "I read that as: Add captions"
    assert out.plan.reply.startswith("I read that as: Add captions. Captions coming.")
    assert out.attempts[-1].status == "answered" and "fallback" in out.attempts[-1].detail


def test_everything_fails_and_recipes_is_below_point_four_gives_clarify_intent():
    brains = {"recipes": Fake("recipes", result=mkplan(0.2, intent="shorts")),
              "apple_intelligence": Fake("apple_intelligence", result="guardrail"),
              "local_model": Fake("local_model", result="timeout"),
              "claude": Fake("claude", raise_=RuntimeError("sdk exploded"))}
    out, _ = run(brains)
    assert not out.answered and out.plan is None and out.note == "clarify_intent"
    q = out.clarify
    assert q.key == "intent" and q.required and q.default is None and q.kind == "choice"
    assert [o.value for o in q.options][0] == "shorts" and len(q.options) == 3
    assert schema.Plan.new(intent="ask", brain="recipes", needs_input=[q]).blocking_questions == [q]
    claude_attempt = next(a for a in out.attempts if a.brain == "claude")
    assert claude_attempt.reason.startswith("rejected:sdk exploded")


def test_planners_own_top_three_guesses_are_reused():
    guess = schema.NeedsInput(key="intent", question="Did you mean…?", required=True,
                              options=[schema.NeedsInputOption(value="tighten", label="Tighten")])
    low = mkplan(0.1).with_(needs_input=[guess.model_dump()])
    out, _ = run({"recipes": Fake("recipes", result=low)})
    assert out.clarify == guess


def test_no_recipes_planner_yet_is_honest_not_fatal():
    out, _ = run({"recipes": Fake("recipes", result="unavailable"),
                  "local_model": Fake("local_model", result=mkplan(0.9, "local_model"))})
    assert out.brain == "local_model"
    assert out.attempts[0].reason == "unavailable"
    nothing, _ = run({"recipes": Fake("recipes", result="unavailable")})
    assert nothing.clarify is not None and nothing.clarify.options


def test_planning_budget_skips_rungs_that_cannot_finish():
    clock = Clock()
    brains = {"recipes": Fake("recipes", result=mkplan(0.5)),
              "apple_intelligence": Fake("apple_intelligence", result="timeout", delay=11.0, clock=clock),
              "local_model": Fake("local_model", result=mkplan(0.9, "local_model"))}
    out, _ = run(brains, clock=clock, budget_s=12.0)
    assert brains["apple_intelligence"].last_timeout == router.FM_ATTEMPT_CAP_S
    mlx = next(a for a in out.attempts if a.brain == "local_model")
    assert mlx.status == "skipped" and mlx.reason == "timeout" and "budget" in mlx.detail
    assert brains["local_model"].plans == 0
    assert out.brain == "recipes" and out.note.startswith("I read that as")


def test_pin_resolution_and_pinned_run_keeps_the_recipes_safety_net(monkeypatch):
    assert router.resolve_order("mlx") == ("local_model",)
    assert router.resolve_order("FM") == ("apple_intelligence",)
    assert router.resolve_order("cloud") == ("claude",)
    assert router.resolve_order("recipes") == ("recipes",)
    assert router.resolve_order("auto") == tuple(BRAIN_IDS) == router.resolve_order("nonsense")
    assert router.resolve_order(env={"VAI_BRAIN": "mlx"}) == ("local_model",)
    brains = {"recipes": Fake("recipes", result=mkplan(0.95)),
              "local_model": Fake("local_model", result="parse")}
    out, events = run(brains, order=("local_model",))
    assert brains["local_model"].plans == 1
    assert out.brain == "recipes" and out.note.startswith("I read that as")
    assert [e["brain"] for e in events if e["status"] == "trying"] == ["local_model", "recipes"]


def test_missing_validator_rejects_model_plans_rather_than_trusting_them(monkeypatch):
    monkeypatch.setattr(router, "_default_validate", lambda: None)
    brains = {"recipes": Fake("recipes", result=mkplan(0.5)),
              "local_model": Fake("local_model", result=mkplan(0.9, "local_model"))}
    out, _ = run(brains, validate=None)
    mlx = next(a for a in out.attempts if a.brain == "local_model")
    assert mlx.reason.startswith("rejected:validator unavailable")
    assert out.brain == "recipes"


def test_brain_availability_exception_is_a_failed_attempt():
    class Broken(Fake):
        def availability(self):
            raise OSError("probe crashed")
    out, _ = run({"recipes": Fake("recipes", result=mkplan(0.5)), "apple_intelligence": Broken("apple_intelligence")})
    fm = next(a for a in out.attempts if a.brain == "apple_intelligence")
    assert fm.reason == "unavailable" and "probe crashed" in fm.detail


def test_attempt_events_match_the_sse_contract():
    ev = router.Attempt("local_model", "answered", latency_ms=12, model="Qwen", detail="confidence 0.9").as_event()
    assert ev == {"type": "brain", "status": "answered", "brain": "local_model", "label": "Local model",
                  "model": "Qwen", "detail": "confidence 0.9", "latency_ms": 12}
    assert set(router.Attempt("claude", "failed", reason="busy").as_event()) == {"type", "status", "brain", "label", "detail"}
    assert service.is_known_event(ev)


def test_build_request_gives_recipe_cards_and_denies_tool_cards_on_the_deny_list():
    r = router.build_request("make it pop", TimelineFacts.minimal())
    names = {c.name for c in r.recipes}
    assert "auto_edit" in names and "transcribe" not in names and "ask" not in names
    tool_names = {c.name for c in r.tool_cards}
    assert tool_names and not (tool_names & schema.PLAN_DENY)
    assert all(c.input_schema.get("type") == "object" and c.description for c in r.tool_cards)
    assert not any(re.search(r"(^|\s)/(Users|tmp|etc|var)/", c.description) for c in r.tool_cards)
    light = router.build_request("x", TimelineFacts.minimal(), with_tool_cards=False)
    assert light.tool_cards == []


def test_brains_report_is_honest_per_rung_and_refresh_reprobes():
    brains = {"recipes": Fake("recipes"),
              "apple_intelligence": Fake("apple_intelligence", avail=False, fix="Turn on Apple Intelligence in System Settings", model="apple-fm"),
              "local_model": Fake("local_model", avail=False, fix="uv sync --extra local-llm", model="Qwen"),
              "claude": Fake("claude", avail=False, fix="Add ANTHROPIC_API_KEY")}
    rep = router.brains_report(brains=brains, env={"VAI_BRAIN": "auto", "VAI_PROMPT_CLOUD": "0"})
    assert rep["ladder"] == list(BRAIN_IDS) and rep["pinned"] is None and rep["cloud_allowed"] is False
    rows = {r["id"]: r for r in rep["brains"]}
    assert rows["recipes"]["available"] and rows["recipes"]["detail"].startswith("grammar + 27 recipes")
    assert "tools" in rows["recipes"]["detail"] and rows["recipes"]["fix"] is None
    for bid in ("apple_intelligence", "local_model", "claude"):
        assert rows[bid]["available"] is False and rows[bid]["fix"], bid
    assert rows["apple_intelligence"]["fix"].startswith("Turn on Apple Intelligence")
    assert rep["best_available"] == "recipes"
    assert {"os", "arch", "ram_gb", "frozen"} <= set(rep["machine"])
    rep2 = router.brains_report(brains={**brains, "local_model": Fake("local_model", model="Qwen")},
                                env={"VAI_BRAIN": "mlx"}, refresh=True)
    assert rep2["best_available"] == "local_model" and rep2["pinned"] == "local_model"
    assert rep2["ladder"] == ["local_model"]
    assert brains["apple_intelligence"].invalidated == 1


def test_real_report_on_this_machine_never_raises_and_always_explains(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    rep = router.brains_report(refresh=True)
    rows = {r["id"]: r for r in rep["brains"]}
    assert set(rows) == set(BRAIN_IDS)
    for r in rep["brains"]:
        assert isinstance(r["available"], bool) and r["detail"]
        if not r["available"]:
            assert r["fix"] or r["id"] == "local_model" and "Apple silicon" in r["detail"], r
    assert rows["recipes"]["available"]
    cached = router.brains_report()
    assert cached["generated_at"] == rep["generated_at"], "memoised for 60 s"


# --- the content pass (§3.7): hook text from a brain, through the planning brain ---------

def test_hook_text_pass_lets_a_content_brain_write_the_hook_through_the_recipes_brain():
    """The real grammar plans "add a hook" (confidence 1.0, heuristic text);
    the local model can only write words — and the badge must say so:
    Recipes planned, text by Local model."""
    fm = Fake("apple_intelligence", avail=False, fix="Turn on Apple Intelligence in System Settings")
    mlx = Fake("local_model", text_items=["1. Nobody tells you this about autofocus", "Another line"])
    out, events = run({"recipes": RecipesBrain(), "apple_intelligence": fm, "local_model": mlx},
                      req("add a hook", **SPOKEN))
    assert out.answered and out.brain == "recipes" and out.plan.brain == "recipes"
    assert out.plan.content_brain == "local_model"
    assert hook_step(out.plan).args["text"] == "Nobody tells you this about autofocus"
    assert "written by the local_model brain" in out.plan.reply
    assert mlx.texts == 1 and mlx.plans == 0 and fm.texts == 0, "unavailable brains are never asked"
    assert out.hook_text.brain == "local_model" and out.hook_text.source == "hook text: Local model"
    assert out.hook_text.candidates == ("Nobody tells you this about autofocus", "Another line")
    # The ladder itself is untouched: one answered attempt, no extra brain events.
    assert [a.status for a in out.attempts] == ["answered"] and [e["status"] for e in events] == ["trying", "answered"]


def test_hook_text_pass_keeps_the_transcript_heuristic_and_says_so_when_no_model_can_write():
    mlx = Fake("local_model", text_items=None)
    out, _ = run({"recipes": RecipesBrain(), "local_model": mlx, "claude": Fake("claude", avail=False)},
                 req("add a hook", **SPOKEN))
    assert out.brain == "recipes" and out.plan.content_brain is None
    assert hook_step(out.plan).args["text"] == recipes.heuristic_hook(CAMERA)
    assert "no local model available" in out.plan.reply
    assert mlx.texts == 1
    assert out.hook_text.brain == "recipes" and "heuristic" in out.hook_text.source
    assert all(c not in hook_step(out.plan).args["text"] for c in ("THEY LIED", "DON'T SCROLL", "WAIT FOR IT"))


def test_hook_text_pass_is_skipped_for_the_users_own_words_a_recipes_pin_and_no_transcript():
    mlx = Fake("local_model", text_items=["Model line"])
    brains = {"recipes": RecipesBrain(), "local_model": mlx}
    quoted, _ = run(brains, req('add a hook that says "hello world"', **SPOKEN))
    assert hook_step(quoted.plan).args["text"] == "hello world" and quoted.hook_text is None
    pinned, _ = run(brains, req("add a hook", **SPOKEN), order=("recipes",))
    assert pinned.plan.content_brain is None and pinned.hook_text is None
    assert hook_step(pinned.plan).args["text"] == recipes.heuristic_hook(CAMERA)
    silent, _ = run(brains, req("add a hook"))                # no transcript: nothing to ground a line in
    assert silent.plan.content_brain is None and silent.hook_text is None
    unrelated, _ = run(brains, req("add captions", **SPOKEN))
    assert unrelated.hook_text is None
    assert mlx.texts == 0


def test_hook_text_pass_reexpands_an_llm_plan_through_the_same_brain_and_revalidates():
    guess = recipes.heuristic_hook(CAMERA)

    def planned(request):
        text, content_brain = (request.hook_text if request.hook_text else (guess, None))
        step = schema.Step(tool="apply_hook_stack", args={"text": text, "duration": 3.0}, why="hook")
        return schema.Plan.new(intent="hook", brain="recipes", steps=[step], confidence=0.9,
                               content_brain=content_brain, title="Hook")

    validated = []

    def validate(plan, facts):
        validated.append(plan.content_brain)
        return plan

    mlx = Fake("local_model", result=planned, text_items=["Nobody tells you this"])
    out, _ = run({"recipes": Fake("recipes", result=mkplan(0.5)), "local_model": mlx},
                 req("add a hook", **SPOKEN), validate=validate)
    assert out.brain == "local_model" and out.plan.brain == "local_model"
    assert out.plan.content_brain == "local_model" and hook_step(out.plan).args["text"] == "Nobody tells you this"
    assert mlx.plans == 2 and mlx.requests[1].hook_text == ("Nobody tells you this", "local_model")
    assert mlx.requests[0].hook_text is None
    assert validated == [None, "local_model"], "the re-expanded plan goes through validate_plan again"


def test_hook_text_pass_failures_keep_the_first_plan():
    guess = recipes.heuristic_hook(CAMERA)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if request.hook_text is not None:
            raise RuntimeError("re-expansion exploded")
        step = schema.Step(tool="apply_hook_stack", args={"text": guess, "duration": 3.0}, why="hook")
        return schema.Plan.new(intent="hook", brain="recipes", steps=[step], confidence=0.9)

    mlx = Fake("local_model", result=flaky, text_items=["Words"])
    out, _ = run({"recipes": Fake("recipes", result=mkplan(0.5)), "local_model": mlx}, req("add a hook", **SPOKEN))
    assert out.brain == "local_model" and hook_step(out.plan).args["text"] == guess
    assert out.plan.content_brain is None and out.hook_text.brain == "local_model"
    # A rejected re-expansion is the same story.
    def reject(plan, facts):
        if plan.content_brain:
            raise Rejected("hook text too long")
        return plan
    def planned(request):
        text, cb = request.hook_text if request.hook_text else (guess, None)
        step = schema.Step(tool="apply_hook_stack", args={"text": text, "duration": 3.0}, why="hook")
        return schema.Plan.new(intent="hook", brain="recipes", steps=[step], confidence=0.9, content_brain=cb)
    out2, _ = run({"recipes": Fake("recipes", result=mkplan(0.5)),
                   "local_model": Fake("local_model", result=planned, text_items=["Words"])},
                  req("add a hook", **SPOKEN), validate=reject)
    assert out2.brain == "local_model" and hook_step(out2.plan).args["text"] == guess and out2.plan.content_brain is None


def test_hook_text_pass_asks_the_planning_brain_first_then_the_ladder():
    guess = recipes.heuristic_hook(CAMERA)

    def planned(request):
        text, cb = request.hook_text if request.hook_text else (guess, None)
        step = schema.Step(tool="apply_hook_stack", args={"text": text, "duration": 3.0}, why="hook")
        return schema.Plan.new(intent="hook", brain="recipes", steps=[step], confidence=0.9, content_brain=cb)

    fm = Fake("apple_intelligence", result="guardrail", text_items=["Apple line"])
    mlx = Fake("local_model", result=planned, text_items=["Local line"])
    out, _ = run({"recipes": Fake("recipes", result=mkplan(0.5)), "apple_intelligence": fm, "local_model": mlx},
                 req("add a hook", **SPOKEN))
    assert out.brain == "local_model" and out.plan.content_brain == "local_model"
    assert hook_step(out.plan).args["text"] == "Local line"
    assert mlx.texts == 1 and fm.texts == 0, "the brain that planned writes first; the ladder only if it cannot"
    mlx2 = Fake("local_model", result=planned, text_items=None)
    out2, _ = run({"recipes": Fake("recipes", result=mkplan(0.5)), "apple_intelligence": fm, "local_model": mlx2},
                  req("add a hook", **SPOKEN))
    assert out2.plan.content_brain == "apple_intelligence" and hook_step(out2.plan).args["text"] == "Apple line"
    assert out2.brain == "local_model", "Local model planned, Apple Intelligence wrote the words"


def test_hook_text_pass_on_the_recipes_fallback_keeps_the_i_read_that_as_prefix():
    mlx = Fake("local_model", result="timeout", text_items=["Nobody tells you this"])
    # "something punchy" is an unmatched clause: the grammar reads this at 0.5
    # (checked on the tree this test was written against), i.e. the fallback path.
    request = req("give me a hook and something punchy", **SPOKEN)
    rb = RecipesBrain()
    first = rb.plan(request, timeout_s=5).plan
    assert router.RECIPES_FALLBACK <= first.confidence < router.RECIPES_CONFIDENT, first.confidence
    out, _ = run({"recipes": rb, "local_model": mlx}, request)
    assert out.brain == "recipes" and out.note.startswith("I read that as")
    assert out.plan.reply.startswith("I read that as") and out.plan.reply.count("I read that as") == 1
    assert out.plan.content_brain == "local_model" and hook_step(out.plan).args["text"] == "Nobody tells you this"


# --- the claude brain: gated twice, tool cards self-supplied, raw steps validated ----------

class FakeAnthropic:
    """A `messages.create` that records the request and answers with the
    forced `emit_plan` tool_use."""

    def __init__(self, plan_input):
        self.plan_input = plan_input
        self.calls = []

    class _Block:
        def __init__(self, input):
            self.type, self.name, self.input = "tool_use", "emit_plan", input

    def __call__(self, api_key):
        self.key = api_key
        return self

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[self._Block(self.plan_input)])


def test_cloud_brain_is_gated_by_key_and_flag():
    no_key = cloud_plan.CloudBrain(api_key="", allowed=True)
    av = no_key.availability()
    assert not av["available"] and av["action"] == "add_key" and "ANTHROPIC_API_KEY" in av["fix"]
    assert no_key.plan(req(), timeout_s=5).reason == "unavailable"
    off = cloud_plan.CloudBrain(api_key="sk-test", allowed=False)
    assert not off.availability()["available"] and cloud_plan.CLOUD_ENV in off.availability()["detail"]
    assert cloud_plan.cloud_allowed({cloud_plan.CLOUD_ENV: "0"}) is False and cloud_plan.cloud_allowed({}) is True


def test_cloud_brain_supplies_tool_cards_when_the_request_has_none_and_strips_owned_fields():
    client = FakeAnthropic({"version": 1, "intent": "captions", "title": "Captions", "confidence": 0.9,
                            "brain": "recipes", "id": "p_deadbeef", "content_brain": "claude",
                            "estimated_seconds": 999, "downloads_needed": [{"what": "x", "bytes": 1, "tool": "t"}],
                            "steps": [{"tool": "add_caption_track", "args": {"style": "ig_chunky"}, "why": "captions"}],
                            "needs_input": [], "postconditions": [], "reply": "Adding captions."})
    brain = cloud_plan.CloudBrain(api_key="sk-test", allowed=True, model="claude-test", client_factory=client)
    request = req("add captions")                    # tool_cards == [] — the chat service builds it this way
    assert request.tool_cards == []
    res = brain.plan(request, timeout_s=7)
    assert res.ok and res.brain == "claude" and res.model == "claude-test"
    assert client.key == "sk-test"
    call = client.calls[-1]
    assert call["tool_choice"] == {"type": "tool", "name": "emit_plan"} and call["tools"] == [cloud_plan.EMIT_PLAN_TOOL]
    assert call["timeout"] == 7 and call["model"] == "claude-test"
    assert "add_caption_track" in call["system"] and "auto_reframe" in call["system"], "tool cards were self-supplied"
    assert not any(t in call["system"] for t in ("- add_sticker:", "- set_property:", "- undo:"))
    assert "recipe:hook" in call["system"] and "seven words" in call["system"]
    assert "/Users" not in call["system"] and "/Users" not in call["messages"][0]["content"]
    plan = res.plan
    assert plan.brain == "claude" and plan.content_brain is None and plan.id != "p_deadbeef"
    assert plan.estimated_seconds is None and plan.downloads_needed == []
    assert [s.tool for s in plan.steps] == ["add_caption_track"] and plan.reply == "Adding captions."
    explicit = router.build_request("x", TimelineFacts.minimal())
    brain.plan(explicit, timeout_s=7)
    assert all(f"- {c.name}:" in client.calls[-1]["system"] for c in explicit.tool_cards[:5])


def test_cloud_brain_maps_sdk_failures_and_bad_plans_to_reasons():
    class Boom:
        def __init__(self, exc):
            self.exc = exc

        def __call__(self, api_key):
            raise self.exc

    class RateLimitError(Exception):
        pass

    class APITimeoutError(Exception):
        pass

    assert cloud_plan.CloudBrain(api_key="k", allowed=True, client_factory=Boom(RateLimitError())).plan(req(), timeout_s=1).reason == "busy"
    assert cloud_plan.CloudBrain(api_key="k", allowed=True, client_factory=Boom(APITimeoutError())).plan(req(), timeout_s=1).reason == "timeout"
    bad = FakeAnthropic({"version": 1, "intent": "captions", "confidence": 0.9, "steps": [{"tool": "nope!", "args": {}, "why": "x"}],
                         "needs_input": [], "postconditions": []})
    res = cloud_plan.CloudBrain(api_key="k", allowed=True, client_factory=bad).plan(req(), timeout_s=1)
    assert not res.ok and res.reason.startswith("rejected:steps")
    empty = FakeAnthropic(None)
    empty._Block = lambda input: SimpleNamespace(type="text", text="hi")     # no tool_use block
    res = cloud_plan.CloudBrain(api_key="k", allowed=True, client_factory=empty).plan(req(), timeout_s=1)
    assert res.reason == "parse"
