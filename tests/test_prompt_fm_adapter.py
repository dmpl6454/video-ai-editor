"""The Apple Intelligence adapter (spec §3.2) driven through a fake helper:
every exit code, the JSON-code override, the Devanagari pre-check, the three
negative caches, the context retry, the scrubbed environment, and the real
binary's probe when it is built on this Mac."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from video_ai_editor.agent.prompt import recipes, schema
from video_ai_editor.agent.prompt.brains import fm
from video_ai_editor.agent.prompt.brains.base import BrainRequest, TextTask
from video_ai_editor.agent.prompt.facts import TimelineFacts

REPO = Path(__file__).resolve().parents[1]
PROBE_OFF = {"ok": True, "available": False, "state": "appleIntelligenceNotEnabled",
             "fix": "Turn on Apple Intelligence in System Settings → Apple Intelligence & Siri",
             "os": "26.6.2", "languages": ["en", "es"]}
PROBE_ON = {**PROBE_OFF, "available": True, "state": "available", "fix": None}
DRAFT_OK = {"ok": True, "model": "apple-fm", "latency_ms": 900,
            "draft": {"intents": [{"recipe": "reframe", "ratio": "9:16", "platform": "reels", "count": None},
                                  {"recipe": "captions", "style": "ig_chunky"}],
                      "exclusions": ["music"], "needs_input": [], "confidence": 0.9, "reply": "Reframing and captioning."}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeHelper:
    """Scripted `runner`: `probe` answers `probe`; each `plan`/`text` call pops
    the next `(rc, body)` from `script` (the last one repeats)."""

    def __init__(self, probe=PROBE_ON, script=None):
        self.probe = probe
        self.script = list(script or [(0, DRAFT_OK)])
        self.calls: list[tuple[list[str], dict, float]] = []

    def __call__(self, argv, stdin, timeout_s):
        payload = json.loads(stdin) if stdin else {}
        self.calls.append((argv, payload, timeout_s))
        if argv[-1] == "probe":
            return 0, json.dumps(self.probe).encode(), b""
        rc, body = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        out = json.dumps(body).encode() if body is not None else b""
        return rc, out, b""

    def spawns(self, sub: str) -> int:
        return sum(1 for argv, _, _ in self.calls if argv[-1] == sub)


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
    monkeypatch.setattr(fm, "from_intents", fake_from_intents)


def brain(runner, **kw) -> fm.FMBrain:
    kw.setdefault("helper", sys.executable)          # any existing file; the runner is fake
    kw.setdefault("darwin", True)
    kw.setdefault("macos", (26, 6))
    kw.setdefault("frozen", False)
    return fm.FMBrain(runner=runner, **kw)


def request(prompt="make it vertical for reels and add captions") -> BrainRequest:
    return BrainRequest(prompt=prompt, facts=TimelineFacts.minimal(uploads_audio=["/Users/x/uploads/bed.wav"]),
                        recipes=recipes.cards())


# --- availability -----------------------------------------------------------------

def test_not_darwin_or_old_macos_short_circuits_without_spawning():
    h = FakeHelper()
    for kw in ({"darwin": False}, {"macos": (15, 6)}, {"macos": (9, 9)}):
        b = brain(h, **kw)
        av = b.availability()
        assert not av["available"] and "unsupportedOS" in av["detail"] and b.state == "unsupportedOS"
    assert h.calls == []


def test_helper_missing_has_a_build_fix_in_dev_and_an_honest_note_when_frozen(tmp_path):
    missing = str(tmp_path / "nope")
    dev = brain(FakeHelper(), helper=missing)
    assert not dev.availability()["available"] and "swift build" in dev.availability()["fix"]
    assert dev.state == "helperMissing"
    frozen = brain(FakeHelper(), helper=missing, frozen=True)
    assert "without the Apple Intelligence helper" in frozen.availability()["fix"]


def test_probe_off_is_an_honest_fix_and_is_cached_for_sixty_seconds():
    h = FakeHelper(probe=PROBE_OFF)
    clock = Clock()
    b = brain(h, clock=clock)
    av = b.availability()
    assert av == {"available": False, "detail": "appleIntelligenceNotEnabled",
                  "fix": "Turn on Apple Intelligence in System Settings → Apple Intelligence & Siri",
                  "action": "enable_in_settings", "model": "apple-fm"}
    b.availability()
    assert h.spawns("probe") == 1
    clock.t += 61
    b.availability()
    assert h.spawns("probe") == 2
    assert b.plan(request(), timeout_s=5).reason == "unavailable"
    assert h.spawns("plan") == 0
    assert b.languages == ("en", "es") and b.state == "appleIntelligenceNotEnabled"


def test_probe_on_reports_available_with_model():
    b = brain(FakeHelper())
    av = b.availability()
    assert av["available"] and av["model"] == "apple-fm" and "available" in av["detail"]


def test_probe_failure_is_reported_not_raised():
    def broken(argv, stdin, timeout_s):
        return 1, b"garbage", b"segfault"
    b = brain(broken)
    av = b.availability()
    assert not av["available"] and "helperFailed" in av["detail"] and "Rebuild" in av["fix"]


# --- planning -----------------------------------------------------------------------

def test_plan_happy_path_maps_typed_fields_to_slots(expander):
    h = FakeHelper()
    res = brain(h).plan(request(), timeout_s=6)
    assert res.ok and res.brain == "apple_intelligence" and res.model == "apple-fm"
    assert res.latency_ms == 900
    plan = res.plan
    assert plan.brain == "apple_intelligence" and plan.reply == "Reframing and captioning."
    assert [s.tool for s in plan.steps] == ["recipe:reframe", "recipe:captions"]
    assert plan.steps[0].args == {"ratio": "9:16", "platform": "reels"}      # None fields dropped
    argv, payload, timeout = h.calls[-1]
    assert argv[-1] == "plan" and timeout == 8
    assert set(payload) == {"prompt", "recipes", "timeline_summary", "timeout_ms"}
    assert payload["timeout_ms"] == 6000
    assert all(set(c) == {"name", "description", "slots"} for c in payload["recipes"])
    assert "transcribe" not in {c["name"] for c in payload["recipes"]}
    assert "/Users" not in json.dumps(payload) and "bed.wav" in payload["timeline_summary"]


@pytest.mark.parametrize("rc,expected", [(2, "unavailable"), (3, "timeout"), (4, "parse"), (5, "guardrail"),
                                         (6, "decode"), (7, "language"), (8, "context"), (9, "busy"),
                                         (11, "decode"), (-9, "decode")])
def test_every_exit_code_maps_to_a_reason(expander, rc, expected):
    res = brain(FakeHelper(script=[(rc, None)])).plan(request(), timeout_s=5)
    assert not res.ok and res.reason == expected


def test_json_code_is_more_specific_than_the_exit_status(expander):
    res = brain(FakeHelper(script=[(5, {"ok": False, "code": "refusal", "reason": "nope"})])).plan(request(), timeout_s=5)
    assert res.reason == "refusal"


def test_devanagari_precheck_skips_fm_without_spawning(expander):
    h = FakeHelper()
    res = brain(h).plan(request("कैप्शन जोड़ो"), timeout_s=5)
    assert res.reason == "language" and h.spawns("plan") == 0
    h2 = FakeHelper(probe={**PROBE_ON, "languages": ["en", "hi"]})
    assert brain(h2).plan(request("कैप्शन जोड़ो"), timeout_s=5).ok and h2.spawns("plan") == 1


def test_language_error_negative_caches_the_script_for_the_process(expander):
    h = FakeHelper(script=[(7, None), (0, DRAFT_OK)])
    b = brain(h)
    assert b.plan(request("hola"), timeout_s=5).reason == "language"
    assert b.plan(request("otra vez"), timeout_s=5).reason == "language"
    assert h.spawns("plan") == 1


def test_busy_backs_off_sixty_seconds(expander):
    clock = Clock()
    h = FakeHelper(script=[(9, None), (0, DRAFT_OK)])
    b = brain(h, clock=clock)
    assert b.plan(request(), timeout_s=5).reason == "busy"
    assert b.plan(request(), timeout_s=5).reason == "busy" and h.spawns("plan") == 1
    clock.t += fm.BUSY_BACKOFF_S + 1
    assert b.plan(request(), timeout_s=5).ok and h.spawns("plan") == 2


def test_assets_unavailable_marks_the_brain_unavailable_for_five_minutes(expander):
    clock = Clock()
    h = FakeHelper(script=[(2, {"ok": False, "code": "unavailable", "reason": "assets"}), (0, DRAFT_OK)])
    b = brain(h, clock=clock)
    assert b.plan(request(), timeout_s=5).reason == "unavailable"
    probes = h.spawns("probe")
    av = b.availability()
    assert not av["available"] and "assetsUnavailable" in av["detail"] and h.spawns("probe") == probes
    clock.t += fm.UNAVAILABLE_BACKOFF_S + 1
    assert b.availability()["available"] and h.spawns("probe") == probes + 1


def test_context_error_retries_once_with_eight_cards(expander):
    h = FakeHelper(script=[(8, None), (0, DRAFT_OK)])
    res = brain(h).plan(request(), timeout_s=5)
    assert res.ok
    plans = [p for argv, p, _ in h.calls if argv[-1] == "plan"]
    assert len(plans) == 2 and len(plans[0]["recipes"]) > 8 and len(plans[1]["recipes"]) == fm.CONTEXT_RETRY_CARDS
    h2 = FakeHelper(script=[(8, None)])
    assert brain(h2).plan(request(), timeout_s=5).reason == "context" and h2.spawns("plan") == 2


def test_unknown_recipe_from_the_model_is_rejected_not_executed(expander):
    bad = {**DRAFT_OK, "draft": {**DRAFT_OK["draft"], "intents": [{"recipe": "explode"}]}}
    res = brain(FakeHelper(script=[(0, bad)])).plan(request(), timeout_s=5)
    assert not res.ok and res.reason.startswith("rejected:") and "explode" in res.reason


def test_malformed_draft_is_parse():
    # WHY `parse` and not an empty plan: `"not a list"` iterated would yield
    # one bogus recipe per character, and zero intents would read as a valid
    # "nothing to do" — both would hide that the model failed.
    for draft in ({"intents": "not a list"}, {"intents": 42}, "not an object", {"intents": [{"recipe": 7}]}):
        res = brain(FakeHelper(script=[(0, {"ok": True, "draft": draft})])).plan(request(), timeout_s=5)
        assert not res.ok and res.reason == "parse", draft


def test_hook_text_pass_reuses_the_draft_without_a_second_spawn(expander):
    h = FakeHelper()
    b = brain(h)
    first = b.plan(request(), timeout_s=5)
    assert first.ok and first.plan.content_brain is None and h.spawns("plan") == 1
    del fake_from_intents.hook_texts[:]
    again = b.plan(request().with_(hook_text=("NOBODY TELLS YOU THIS", "local_model")), timeout_s=5)
    assert again.ok and again.plan.content_brain == "local_model" and again.brain == "apple_intelligence"
    assert h.spawns("plan") == 1, "the hook-text pass re-expands the cached draft, never re-generates"
    assert fake_from_intents.hook_texts == [("NOBODY TELLS YOU THIS", "local_model")]
    assert again.latency_ms == 0
    # A different prompt is a different request: the cache never answers it.
    other = b.plan(request("something else").with_(hook_text=("X", "local_model")), timeout_s=5)
    assert other.ok and h.spawns("plan") == 2
    # And a fresh request (no hook_text) always spawns, even for the same prompt.
    b.plan(request(), timeout_s=5)
    assert h.spawns("plan") == 3


def test_text_task_round_trip_and_failure():
    h = FakeHelper(script=[(0, {"ok": True, "items": ["Nobody tells you this", "Second"], "latency_ms": 300})])
    task = TextTask(kind="hook_candidates", payload={"transcript_head": "three things", "n": 3, "max_words": 7})
    res = brain(h).text(task, timeout_s=4)
    assert res.items == ["Nobody tells you this", "Second"] and res.brain == "apple_intelligence"
    assert h.calls[-1][1] == {"task": "hook_candidates", "payload": task.payload, "timeout_ms": 4000}
    assert brain(FakeHelper(script=[(5, None)])).text(task, timeout_s=4) is None


# --- the real process boundary ------------------------------------------------------------

def test_default_runner_scrubs_the_environment_and_uses_an_empty_cwd(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        seen["cwd_contents"] = os.listdir(kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf-secret")
    monkeypatch.setattr(fm.subprocess, "run", fake_run)
    rc, out, _ = fm.default_runner(["fm-planner", "plan"], b"{}", 3.0)
    assert rc == 0 and out == b"{}"
    assert set(seen["env"]) == {"PATH", "HOME", "TMPDIR"}
    assert seen["env"]["PATH"] == "/usr/bin:/bin"
    assert seen["cwd_contents"] == [] and seen["timeout"] == 3.0 and seen["input"] == b"{}"


def test_default_runner_maps_timeout_and_oserror_to_exit_codes(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"")
    monkeypatch.setattr(fm.subprocess, "run", timeout)
    assert fm.default_runner(["x", "plan"], b"{}", 1.0)[0] == 3
    def missing(argv, **kwargs):
        raise FileNotFoundError("no such binary")
    monkeypatch.setattr(fm.subprocess, "run", missing)
    assert fm.default_runner(["x", "plan"], b"{}", 1.0)[0] == 2


def test_helper_path_resolution_order(tmp_path, monkeypatch):
    exe = tmp_path / "custom-fm"
    exe.write_text("")
    assert fm.helper_path(env={fm.HELPER_ENV: str(exe)}) == str(exe)
    assert fm.helper_path(env={fm.HELPER_ENV: str(tmp_path / "missing")}) is None
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "fm-planner").write_text("")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert fm.helper_path(env={}, frozen=True) == str(bundle / "fm-planner")
    repo = tmp_path / "repo"
    binroot = repo / "tools" / "fm-planner" / "out"
    binroot.mkdir(parents=True)
    (binroot / "fm-planner").write_text("")
    (repo / "tools" / "fm-planner" / ".binpath").write_text(str(binroot) + "\n")
    assert fm.helper_path(env={}, frozen=False, repo_root=repo) == str(binroot / "fm-planner")


def test_macos_version_is_a_tuple_of_ints():
    v = fm.macos_version()
    if sys.platform == "darwin":
        assert isinstance(v, tuple) and all(isinstance(x, int) for x in v)
        assert (v < fm.MIN_MACOS) == (v[0] < 26)          # not a string compare
    else:
        assert v is None


_REAL = fm.helper_path(env={}, frozen=False)


@pytest.mark.skipif(sys.platform != "darwin" or not _REAL, reason="fm-planner not built on this machine")
def test_real_helper_probe_and_input_guard():
    rc, out, _ = fm.default_runner([_REAL, "probe"], b"", 10.0)
    probe = json.loads(out)
    assert rc == 0 and probe["ok"] is True
    assert probe["state"] in {"available", "deviceNotEligible", "appleIntelligenceNotEnabled", "modelNotReady"}
    assert probe["available"] == (probe["state"] == "available")
    assert (probe["fix"] is None) == probe["available"]
    assert "en" in probe["languages"]
    rc, out, _ = fm.default_runner([_REAL, "plan"], b"not json", 10.0)
    assert rc == 4 and json.loads(out)["code"] == "bad_input"
    rc, out, _ = fm.default_runner([_REAL, "plan"], b"a" * (64 * 1024 + 1), 10.0)
    assert rc == 4
    real = fm.FMBrain(helper=_REAL)
    av = real.availability()
    assert av["available"] == probe["available"]
    if not av["available"]:
        assert av["fix"], "an unavailable brain must always carry a fix"
