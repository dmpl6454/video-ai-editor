"""THE security boundary of the Prompt Editor (spec §1.3, §3.5, §4.2).

A plan is model-authored input. Before ANY dispatch — on a fresh run and on
a run resumed from a clarification — two things happen in order: P's
`validate_plan` (the complete rule set) and the executor's own `guard_step`
(the subset that must hold even if the validator has a bug or is missing).
This file proves:

  * the validator is called before the first dispatch in both run shapes,
    and a rejecting validator means zero dispatches;
  * when `agent/prompt/validate.py` cannot be imported the run is REFUSED,
    never passed through;
  * every §1.3 rule the guard enforces on its own: the deny list (each
    member), unknown tools, unknown args, write paths, read paths outside
    `facts.allowed_paths` (/etc/passwd, ~, ../, a real $HOME file, a WORKDIR
    file outside the session), LUTs as paths, and the three download
    triggers (whisper model, Piper voice, MADLAD) when the artefact is not
    on disk — with `restrict_paths_active()` False, as the desktop runs, and
    with `snapshot_download` / `download_voice` patched to fail loudly.

Rules that live only in the validator (enum whitelists such as
`translate_captions(target_lang="fr")`, `add_text(font="../x.ttf")`) are
exercised in the last section, which skips until P's module is on disk.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor import config  # noqa: E402
from video_ai_editor.agent import path_args  # noqa: E402
from video_ai_editor.agent.prompt import executor, service  # noqa: E402
from video_ai_editor.agent.prompt.schema import PLAN_DENY, NeedsInput, NeedsInputOption  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")


@pytest.fixture
def session(tmp_path: Path):
    store = F.make_store(tmp_path)
    return store, F.facts_for(store)


@pytest.fixture
def dispatch_spy(monkeypatch):
    calls: list[tuple[str, dict]] = []
    real = D.dispatch

    def spy(store, tool, args, **kw):
        calls.append((tool, dict(args)))
        return real(store, tool, args, **kw)

    monkeypatch.setattr(D, "dispatch", spy)
    return calls


def _refused(tool: str, facts, **args) -> str:
    with pytest.raises(executor.StepRefused) as ei:
        executor.guard_step(tool, args, facts)
    return "; ".join(ei.value.reasons)


# ---------------------------------------------------------------- posture

def test_desktop_posture_is_the_one_under_test():
    assert config.restrict_paths_active() is False


# ---------------------------------------------------------------- deny list + unknowns

@pytest.mark.parametrize("tool", sorted(PLAN_DENY))
def test_every_denied_tool_is_refused_before_dispatch(session, dispatch_spy, tool):
    store, facts = session
    args = {"set_property": {"clip_id": "c", "path": "src", "value": "/etc/hosts"},
            "add_effect": {"clip_id": "c", "type": "lut", "params": {"src": "/etc/hosts"}},
            "add_sticker": {"emoji": "🔥", "start": 0, "end": 1},
            "add_clip": {"track": "v1", "src": "/etc/passwd"},
            "export_srt": {"path": str(Path.home() / "x.srt")}}.get(tool, {})
    why = _refused(tool, facts, **args)
    assert "denied to plans" in why
    assert dispatch_spy == []


def test_unknown_tool_and_unknown_arg_are_refused(session, dispatch_spy):
    store, facts = session
    assert "unknown tool" in _refused("format_disk", facts)
    why = _refused("add_music", facts, src=str(Path(facts.allowed_paths and sorted(facts.allowed_paths)[0])), gain_db=-12)
    assert "unknown args ['gain_db']" in why                # baseline finding 8: silently ignored before
    assert "unknown args" in _refused("auto_reframe", facts, ratio="9:16", turbo=True)
    executor.guard_step("auto_reframe", {"ratio": "9:16", "subject_track": True}, facts)   # EXTRA_ARGS ok
    executor.guard_step("apply_hook_stack", {"text": "HEY", "duration": 3.0, "visual": "punch_in"}, facts)
    assert dispatch_spy == []


# ---------------------------------------------------------------- paths

def _upload(facts) -> str:
    return next(p for p in sorted(facts.allowed_paths) if p.endswith(".mp4"))


@pytest.mark.parametrize("bad", ["/etc/passwd", "~/Movies/x.wav", "../../x.wav", "HOME", "WORKDIR"])
def test_read_paths_outside_the_offered_set_are_refused(session, tmp_path, bad):
    store, facts = session
    if bad == "HOME":
        real = next((p for p in Path.home().iterdir() if p.is_file()), None)
        if real is None:
            pytest.skip("no file in $HOME to point at")
        bad = str(real)
    elif bad == "WORKDIR":
        other = tmp_path / "other_session" / "uploads" / "audio" / "bed.wav"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"RIFF")
        bad = str(other)
    why = _refused("add_music", facts, src=bad)
    assert "path not offered" in why or "plans may not" in why
    why = _refused("apply_brand_kit", facts, handle="@x", end_card=bad)
    assert "path not offered" in why


def test_the_offered_upload_passes_and_a_sibling_file_does_not(session):
    store, facts = session
    executor.guard_step("add_music", {"src": _upload(facts), "loop": True}, facts)
    sibling = str(Path(_upload(facts)).with_name("evil.mp4"))
    assert "path not offered" in _refused("add_music", facts, src=sibling)
    assert "non-empty string" in _refused("add_music", facts, src="")
    assert "non-empty string" in _refused("add_music", facts, src=123)


def test_write_paths_are_refused_outright(session):
    store, facts = session
    reasons: list[str] = []
    executor._check_path_arg("export_srt", "path", str(Path(store.dir) / "out.srt"), "write", facts, reasons)
    assert reasons == ["export_srt.path: plans may not write files"]
    assert path_args.guarded_args("write") == {("export_ass", "path"), ("export_srt", "path"), ("export_vtt", "path")}


def test_luts_are_bundled_names_never_paths(session):
    store, facts = session
    assert "bundled names, not paths" in _refused("apply_lut", facts, clip_id="$v1_all", lut_path="/x.cube")
    assert "bundled names, not paths" in _refused("apply_lut", facts, clip_id="$v1_all", src="../teal_orange.cube")
    assert "bundled names, not paths" in _refused("apply_lut", facts, src=".cube")
    assert "unknown LUT" in _refused("apply_lut", facts, src="evil.cube")
    executor.guard_step("apply_lut", {"clip_id": "$v1_all", "src": "teal_orange.cube", "intensity": 0.8}, facts)
    executor.guard_step("apply_lut", {"clip_id": "$v1_all", "src": "warm"}, facts)


def test_allowlist_is_honoured_when_the_lan_posture_is_on(session, monkeypatch):
    """With `restrict_paths_active()` True (a paired phone), an offered path
    that lies outside VAI_ALLOWED_ROOTS is still refused by the config
    guard — the offer set is not a bypass of the OS-level allowlist."""
    store, facts = session
    monkeypatch.setattr(config, "restrict_paths_active", lambda: True)
    monkeypatch.setattr(config, "assert_path_allowed", lambda p: (_ for _ in ()).throw(ValueError(f"read path {p} is outside the allowed roots")))
    assert "outside the allowed roots" in _refused("add_music", facts, src=_upload(facts))


# ---------------------------------------------------------------- no network without a yes

def test_whisper_model_not_on_disk_is_refused(session, monkeypatch):
    store, facts = session
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: m == "small")
    assert "'someone/huge-repo' is not downloaded" in _refused("auto_caption", facts, model="someone/huge-repo")
    assert "'large-v3' is not downloaded" in _refused("transcribe", facts, model="large-v3")
    executor.guard_step("transcribe", {"model": "small"}, facts)
    executor.guard_step("auto_caption", {"style": "ig_chunky"}, facts)      # no model arg → the cached default


def test_piper_voice_not_on_disk_is_refused_and_nothing_is_fetched(session, monkeypatch, tmp_path):
    store, facts = session
    from video_ai_editor.ai import tts as _tts
    monkeypatch.setattr(_tts, "voice_paths", lambda name: (tmp_path / f"{name}.onnx", tmp_path / f"{name}.onnx.json"))
    (tmp_path / "en_US-amy-medium.onnx").write_bytes(b"x")
    assert "'xx_XX-evil' is not downloaded" in _refused("tts_voiceover", facts, text="hi", voice="xx_XX-evil")
    executor.guard_step("tts_voiceover", {"text": "hi", "voice": "en_US-amy-medium"}, facts)
    executor.guard_step("tts_voiceover", {"text": "hi"}, facts)                 # default voice is cached


def test_translation_without_madlad_is_refused(session, monkeypatch, tmp_path):
    store, facts = session
    from video_ai_editor.ai import translate as _tr
    monkeypatch.setattr(_tr, "_model_dir", lambda: tmp_path / "no-madlad")
    assert "MADLAD" in _refused("translate_captions", facts, target_lang="hi")
    assert "MADLAD" in _refused("auto_caption", facts, target="hinglish")
    executor.guard_step("translate_captions", {"target_lang": "en"}, facts)   # English needs no model
    (tmp_path / "no-madlad").mkdir()
    (tmp_path / "no-madlad" / "model.bin").write_bytes(b"x")
    executor.guard_step("translate_captions", {"target_lang": "hi"}, facts)


# ---------------------------------------------------------------- the validator runs first, always

def _questioned_plan() -> "F.Plan":
    q = NeedsInput(key="target_lang", question="Which language?", kind="choice", required=True,
                   options=[NeedsInputOption(value="en", label="English")])
    return F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"),
                     F.step("translate_captions", target_lang=None, _optional=True), needs_input=[q], title="look")


def _order_spies(monkeypatch, order: list[str]):
    def validator(plan, facts):
        order.append("validate")
        return plan
    real = D.dispatch

    def spy(store, tool, args, **kw):
        order.append(f"dispatch:{tool}")
        return real(store, tool, args, **kw)

    monkeypatch.setattr(executor, "_validate_plan", validator)
    monkeypatch.setattr(D, "dispatch", spy)


def test_validator_precedes_every_dispatch_on_a_fresh_run(session, monkeypatch):
    store, facts = session
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    order: list[str] = []
    _order_spies(monkeypatch, order)
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"), F.step("add_marker", time=1.0))
    F.route_with(monkeypatch, F.FakeRouted(plan))
    events = F.collect(service.prompt_turn(store, "give it a warm look", []))
    assert events[-1]["type"] == "done" and not any(e["type"] == "error" for e in events)
    assert order[0] == "validate"
    assert order.index("validate") < order.index("dispatch:apply_lut") < order.index("dispatch:add_marker")
    assert order.count("validate") == 1                    # the service does not re-validate; the executor does, once


def test_validator_precedes_every_dispatch_on_a_resumed_run(session, monkeypatch):
    from video_ai_editor.agent.prompt import pending
    store, facts = session
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    order: list[str] = []
    _order_spies(monkeypatch, order)
    F.route_with(monkeypatch, F.FakeRouted(_questioned_plan()))
    first = F.collect(service.prompt_turn(store, "translate the captions", []))
    assert [e["type"] for e in first if e["type"] in ("clarify", "done")] == ["clarify", "done"]
    assert order == []                                     # paused: nothing validated or dispatched yet
    token = pending.load_pending(Path(store.dir))["token"]
    resumed = F.collect(service.resume(store, token, {"target_lang": "en"}))
    assert resumed[-1]["type"] == "done"
    assert order and order[0] == "validate" and order.index("validate") < order.index("dispatch:apply_lut")
    assert pending.load_pending(Path(store.dir)) is None


def test_validator_precedes_dispatch_when_the_answer_is_the_next_chat_message(session, monkeypatch):
    store, facts = session
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)
    order: list[str] = []
    _order_spies(monkeypatch, order)
    F.route_with(monkeypatch, F.FakeRouted(_questioned_plan()))
    F.collect(service.prompt_turn(store, "translate the captions", []))
    events = F.collect(service.prompt_turn(store, "English", []))
    assert order[0] == "validate" and any(o.startswith("dispatch:") for o in order)
    assert events[0]["type"] == "brain" and events[0]["status"] == "answered"     # a resume, not a new plan


def test_a_rejecting_validator_means_zero_dispatches(session, monkeypatch, dispatch_spy):
    store, facts = session
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: facts)

    def reject(plan, facts):
        raise ValueError("add_music.gain_db: unknown arg")

    monkeypatch.setattr(executor, "_validate_plan", reject)
    hash_before = store.edl.hash()
    F.route_with(monkeypatch, F.FakeRouted(F.plan_of(F.step("add_music", src="/etc/passwd", gain_db=-12))))
    events = F.collect(service.prompt_turn(store, "add music", []))
    err = [e for e in events if e["type"] == "error"]
    assert err and "Plan refused before any step ran: add_music.gain_db: unknown arg" in err[0]["message"]
    assert dispatch_spy == [] and store.edl.hash() == hash_before
    assert events[-1]["type"] == "done"


def test_missing_validate_module_refuses_the_run(session, monkeypatch, dispatch_spy):
    """`agent/prompt/validate.py` unimportable → PlanValidationUnavailable →
    the plan is refused, never executed unvalidated."""
    store, facts = session
    monkeypatch.setitem(sys.modules, "video_ai_editor.agent.prompt.validate", None)
    with pytest.raises(executor.PlanValidationUnavailable):
        executor._validate_plan(F.plan_of(), facts)
    res = executor.run_plan(store, F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube")), facts,
                            emit=lambda e: None, cancel_event=None, prompt="x")
    assert res.error and "validation is unavailable" in res.error and not res.committed
    assert dispatch_spy == []


def test_the_guard_runs_even_when_the_validator_lets_something_through(session, monkeypatch, dispatch_spy):
    """Defence in depth: an identity validator plus a plan carrying a path
    outside the session — the guard refuses at the last line."""
    store, facts = session
    hash_before = store.edl.hash()
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"),
                     F.step("add_music", src="/etc/passwd"))
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=None, prompt="x",
                            validator=F.identity_validator)
    assert res.error and "add_music.src: path not offered" in res.error
    assert [t for t, _ in dispatch_spy] == ["apply_lut"]            # step 1 ran, step 2 never reached dispatch
    assert store.edl.hash() == hash_before                          # …and step 1 was rolled back


def test_fan_out_guard_refuses_a_sentinel_that_names_nothing(session, dispatch_spy):
    store, facts = session
    res = executor.run_plan(store, F.plan_of(F.step("set_speed", clip_id="$selected", factor=1.5)), facts,
                            emit=lambda e: None, cancel_event=None, prompt="x", validator=F.identity_validator)
    assert res.error and "names no clip" in res.error and dispatch_spy == []


# ---------------------------------------------------------------- P's validator (skips until it lands)

def _validate():
    return pytest.importorskip("video_ai_editor.agent.prompt.validate",
                               reason="agent/prompt/validate.py (P) not on disk yet")


@pytest.mark.parametrize("tool, args", [
    ("translate_captions", {"target_lang": "fr"}),
    ("add_text", {"text": "hi", "start": 0, "end": 1, "font": "../x.ttf"}),
    ("tts_voiceover", {"text": "hi", "voice": "xx_XX-evil"}),
    ("auto_caption", {"model": "someone/huge-repo"}),
    ("set_property", {"clip_id": "c", "path": "src", "value": "/etc/hosts"}),
    ("add_effect", {"clip_id": "c", "type": "lut", "params": {"src": "/x.cube"}}),
    ("apply_lut", {"clip_id": "$v1_all", "lut_path": "/x.cube"}),
    ("add_sticker", {"emoji": "🔥", "start": 0, "end": 1}),
    ("add_music", {"src": "/etc/passwd"}),
    ("add_music", {"src": "upbeat_120bpm.wav", "gain_db": -12}),
    ("set_speed", {"clip_id": "$v1_all", "factor": 9.0}),
])
def test_validate_plan_rejects_the_rule_8_cases(session, tool, args):
    V = _validate()
    store, facts = session
    plan = F.plan_of(F.step(tool, **args))
    with pytest.raises(V.PlanRejected) as ei:
        V.validate_plan(plan, facts)
    assert ei.value.reasons


def test_validate_plan_accepts_sentinels_and_returns_a_new_sorted_plan(session):
    V = _validate()
    store, facts = session
    plan = F.plan_of(F.step("add_marker", time=1.0), F.step("cut_range", track="v1", start=4.0, end=6.0),
                     F.step("apply_lut", clip_id="$v1_all", src="teal_orange.cube", intensity=0.8))
    out = V.validate_plan(plan, facts)
    assert out is not plan and out.id and plan.model_dump() == plan.model_dump()
    tools = [s.tool for s in out.steps]
    assert tools.index("cut_range") < tools.index("apply_lut")           # stage order
    assert all(s.stage is not None for s in out.steps)
    assert any(pc.check != "tool_ok" for pc in out.postconditions)      # every executed plan is verified


# ---------------------------------------------------------------- rule 8 (validate_plan): no generate_hook, no first-use downloaders

def test_empty_hook_text_and_template_hook_stack_are_refused_before_generate_hook(session, monkeypatch, dispatch_spy):
    """Both handler paths reach `generate_hook` (an Anthropic call when a key
    is set); the boundary refuses the plan shapes that would get there."""
    from video_ai_editor.agent.prompt.validate import PlanRejected, validate_plan
    store, facts = session
    spy: list = []
    monkeypatch.setitem(D.DISPATCH, "generate_hook", lambda s, a: (spy.append(a), {"candidates": ["X"]})[1])
    for bad in (F.plan_of(F.step("apply_hook_stack", text="")),
                F.plan_of(F.step("apply_template", name="tech_tip", with_hook_stack=True))):
        with pytest.raises(PlanRejected):
            validate_plan(bad, facts)
    softened = validate_plan(F.plan_of(F.step("apply_template", name="tech_tip")), facts)
    assert softened.steps[0].args["with_hook_stack"] is False
    assert spy == [] and dispatch_spy == []


@pytest.mark.parametrize("tool", ["vocal_isolate", "instrumental_isolate", "diarize", "search_media"])
def test_tools_that_download_on_first_use_are_denied(session, dispatch_spy, tool):
    store, facts = session
    assert "denied to plans" in _refused(tool, facts, clip_id="$v1_first", query="x")
    assert dispatch_spy == []
