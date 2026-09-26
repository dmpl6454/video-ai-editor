"""The Prompt Editor does what was asked and verifies THAT (QA-031, QA-032, QA-043).

QA-031  "turn off ducking" / "stop ducking" / "don't duck the music" produced
        set_duck(enabled=True) at confidence 1.0 and a VERIFIED card.
QA-032  the Chat pane's own example burned the literal "$ask:handle" into the
        video (watermark + end card): an unresolved placeholder must never
        reach dispatch — validate refuses it, the planner asks, the executor's
        guard is the last line, and a check holding one fails.
QA-043  "add hinglish captions" on Hindi speech demanded the 3 GB MADLAD model
        and Skip laid Devanagari: Hindi → Hinglish is transliteration (the
        bundled `ai.romanize`), local and immediate.

Every behaviour is asserted on executed code: the planner and validator, the
executor's guard, dispatch on a real EDLStore, and turns through the real
`service.prompt_turn` (grammar → plan → validate → execute → verify).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.prompt import executor as X  # noqa: E402
from video_ai_editor.agent.prompt import grammar as G  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import recipes as R  # noqa: E402
from video_ai_editor.agent.prompt import schema as Sc  # noqa: E402
from video_ai_editor.agent.prompt import service  # noqa: E402
from video_ai_editor.agent.prompt import validate as V  # noqa: E402
from video_ai_editor.agent.prompt import verify as VF  # noqa: E402
from video_ai_editor.agent.prompt.brains import router  # noqa: E402
from video_ai_editor.agent.prompt.brains.base import BrainRequest, BrainResult, available  # noqa: E402
from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

DUCKED = TimelineFacts.minimal(session_id="s_duck", clip_ids=["c_v1", "c_bed"], track_ids=["v1", "music"],
                               has_music=True, music_ducked=True, music_clip_ids=["c_bed"], music_gain_db=-14.0)
CHAT_EXAMPLE = ("Apply my brand kit @quicksolutions.in with #techtips, generate a hook, burn IG-style "
                "captions, then audit and render the preview.")
MADLAD_MISSING = {"madlad": 3_000_000_000}
HINDI = TimelineFacts.minimal(session_id="s_hi", has_transcript=True, language="hi", spoken_language="hi",
                              words=12, first_use=MADLAD_MISSING, transcript_head="नमस्ते दोस्तों")
HINDI_WORDS = [("नमस्ते", 0.20, 0.80), ("दोस्तों", 0.90, 1.50), ("आज", 5.50, 5.80), ("हम", 6.00, 6.30),
               ("बात", 6.40, 6.80), ("करेंगे", 6.90, 7.60), ("धन्यवाद", 10.50, 11.50)]


def _tools(p: Sc.Plan) -> list[str]:
    return [s.tool for s in p.steps]


def _step(p: Sc.Plan, tool: str) -> Sc.Step:
    return next(s for s in p.steps if s.tool == tool)


def _turn(store, message: str) -> list[dict]:
    events = F.collect(service.prompt_turn(store, message, [], brain="recipes"))
    assert events[-1]["type"] == "done", events[-3:]
    return events


def _verify(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "verify")


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch, desktop_posture):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)        # chat.json lands in the test's own dir
    # The run commits to THIS store object (an app resolver configured by an
    # earlier test would hand back a second EDLStore on the same directory).
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    return tmp_path


# =========================================================================== QA-031

DUCK_OFF = ["turn off ducking", "stop ducking", "don't duck the music", "no ducking on the music",
            "turn ducking off", "disable ducking", "do not duck the music", "undo the ducking",
            "get rid of the ducking"]


@pytest.mark.parametrize("prompt", DUCK_OFF)
def test_negated_duck_prompts_turn_ducking_off_and_check_off(prompt):
    p = P.plan(prompt, DUCKED)
    assert p.intent == "duck" and p.confidence >= G.RUN_THRESHOLD, (p.intent, p.confidence)
    s = _step(p, "set_duck")
    assert s.args["enabled"] is False, s.args
    duck_checks = [c for c in p.postconditions if c.check == "music_ducked"]
    assert duck_checks and all(c.args.get("enabled") is False for c in duck_checks)
    assert "off" in (p.title or "").lower()
    out = V.validate_plan(p, DUCKED)
    assert _step(out, "set_duck").args["enabled"] is False


def test_positive_duck_prompts_still_turn_it_on():
    for prompt in ("duck the music", "duck the music under my voice", "add ducking"):
        p = P.plan(prompt, DUCKED.with_(music_ducked=False))
        assert _step(p, "set_duck").args["enabled"] is True, prompt


def test_add_music_without_ducking_lays_an_unducked_bed():
    f = TimelineFacts.minimal(uploads_audio=["/u/bed.wav"], allowed_paths={"/u/bed.wav"})
    for prompt in ("add music without ducking", "add music but don't duck it"):
        p = P.plan(prompt, f)
        assert _step(p, "add_music").args["duck"] is False, prompt
        assert "set_duck" not in _tools(p)


def test_already_off_or_no_music_is_said_not_verified():
    p = P.plan("turn off ducking", DUCKED.with_(music_ducked=False))
    assert not p.steps and "already off" in (p.reply or "")
    p = P.plan("turn off ducking", TimelineFacts.minimal())
    assert not p.steps and "no music" in (p.reply or "")


def test_a_toggle_without_a_state_is_pinned_so_the_check_knows_what_to_expect():
    raw = Sc.Plan.new(intent="x", brain="claude", steps=[Sc.Step(tool="set_duck", args={"track": "music"}, why="x")])
    out = V.validate_plan(raw, DUCKED)
    assert _step(out, "set_duck").args["enabled"] is False
    assert any(c.check == "music_ducked" and c.args.get("enabled") is False for c in out.postconditions)


def test_turn_off_ducking_end_to_end_leaves_ducking_off_and_verifies_off(workdir, no_downloads):
    store = F.make_store(workdir)
    bed = F.music_bed(workdir, dur=12.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "duck": True})
    assert store.edl.get_track("music").duck is not None
    ev = _turn(store, "turn off ducking")
    assert store.edl.get_track("music").duck is None, [e for e in ev if e["type"] in ("plan", "error", "text_delta", "brain")]
    v = _verify(ev)
    duck = next(c for c in v["checks"] if c["check"] == "music_ducked")
    assert duck["pass"] is True and duck["measured"] == "off" and v["passed"] == v["total"]


def test_the_duck_check_measures_the_requested_state():
    from video_ai_editor.edl.schema import Clip, MusicDuck, empty_edl
    edl = empty_edl()
    music = edl.get_track("music")
    music.clips.append(Clip(src="/x.wav", in_=0.0, out=5.0, start=0.0))
    music.duck = MusicDuck(to_db=-18.0, track_ref="a1")
    ctx = VF.VerifyCtx(store=type("S", (), {"edl": edl, "dir": "/tmp"})(), plan=Sc.Plan.new(intent="x", brain="recipes"),
                       exec_result=None, facts_before=DUCKED)
    off = Sc.Postcondition(check="music_ducked", args={"enabled": False}, human="ducking is off")
    assert VF.run_check(ctx, off).passed is False          # still ducked → the "off" request failed
    music.duck = None
    assert VF.run_check(ctx, off).passed is True


# =========================================================================== QA-032

def _brand_plan(handle="$ask:handle", questions=()) -> Sc.Plan:
    step = Sc.Step(tool="apply_brand_kit", args={"handle": handle}, why="brand")
    return Sc.Plan.new(intent="brand", brain="apple_intelligence", steps=[step], needs_input=list(questions),
                       confidence=0.8)


def test_validate_refuses_a_placeholder_no_question_will_fill():
    with pytest.raises(V.PlanRejected) as e:
        V.validate_plan(_brand_plan(), TimelineFacts.minimal())
    assert "$ask:handle" in str(e.value)
    blank = R.ask("handle", "Which handle?", kind="text", default="", required=False)
    with pytest.raises(V.PlanRejected):
        V.validate_plan(_brand_plan(questions=[blank]), TimelineFacts.minimal())
    with pytest.raises(V.PlanRejected):
        V.validate_plan(_brand_plan(handle="$arg:handle"), TimelineFacts.minimal())


def test_a_real_default_prefills_and_a_blocking_question_pauses():
    given = R.ask("handle", "Which handle?", kind="text", default="@acme", required=False)
    out = V.validate_plan(_brand_plan(questions=[given]), TimelineFacts.minimal())
    assert _step(out, "apply_brand_kit").args["handle"] == "@acme"
    asking = R.ask("handle", "Which handle?", kind="text")
    out = V.validate_plan(_brand_plan(questions=[asking]), TimelineFacts.minimal())
    assert out.blocking_questions and out.blocking_questions[0].key == "handle"


def test_a_brains_blank_default_question_cannot_shadow_the_recipes_blocking_one():
    """The Apple Intelligence shape: the brand intent without a handle, and a
    draft question `handle` with `default_value: ""`."""
    draft = Sc.IntentDraft(intents=[{"recipe": "brand", "slots": {}}],
                           needs_input=[{"key": "handle", "question": "Your handle?", "default": ""}],
                           confidence=0.8)
    p = R.from_intents(draft, TimelineFacts.minimal())
    assert _step(p, "apply_brand_kit").args["handle"] == "$ask:handle"
    assert [q.key for q in p.blocking_questions] == ["handle"]      # it PAUSES


def test_the_executor_guard_refuses_a_placeholder_even_past_validation():
    with pytest.raises(X.StepRefused) as e:
        X.guard_step("apply_brand_kit", {"handle": "$ask:handle"}, TimelineFacts.minimal())
    assert "placeholder" in str(e.value)
    with pytest.raises(X.StepRefused):
        X.guard_step("apply_text_template", {"name": "end_card_handle", "fields": {"handle": "$ask:handle"}},
                     TimelineFacts.minimal())


def test_a_check_still_holding_a_placeholder_fails():
    from video_ai_editor.edl.schema import TextClip, empty_edl
    from video_ai_editor.agent.dispatch import ensure_track
    edl = empty_edl()
    ensure_track(edl, "tx_super", "text", z=11).clips.append(
        TextClip(text="$ask:handle", start=0.0, end=3.0, role="watermark"))   # what QA-032 burned in
    ctx = VF.VerifyCtx(store=type("S", (), {"edl": edl, "dir": "/tmp"})(), plan=Sc.Plan.new(intent="x", brain="recipes"),
                       exec_result=None, facts_before=TimelineFacts.minimal())
    pc = Sc.Postcondition(check="text_present", args={"contains": "$ask:handle"}, human="the end card shows the handle")
    res = VF.run_check(ctx, pc)
    assert res.passed is False and "placeholder" in (res.detail or "")


def test_the_chat_example_is_understood_in_full():
    f = TimelineFacts.minimal(has_transcript=True, words=30, speech_seconds=20.0,
                              transcript_head="Today we look at the new camera and why it beats the old one.")
    p = P.plan(CHAT_EXAMPLE, f)
    assert p.confidence >= G.RUN_THRESHOLD and not p.blocking_questions, (p.confidence, p.reply)
    assert {"apply_brand_kit", "apply_hook_stack", "add_caption_track", "audit_aesthetic",
            "render_preview"} <= set(_tools(p)), _tools(p)
    brand = _step(p, "apply_brand_kit").args
    assert brand["handle"] == "@quicksolutions.in" and brand["hashtags"] == ["#techtips"]
    assert not V._placeholders([s.args for s in p.steps])
    V.validate_plan(p, f)


class _FakeFM:
    id = "apple_intelligence"

    def __init__(self, plan):
        self._plan = plan

    def availability(self):
        return available("ready", model="m")

    def plan(self, req, *, timeout_s):
        return BrainResult(plan=self._plan, brain=self.id, ok=True, model="m", latency_ms=3)

    def text(self, task, *, timeout_s):
        return None


def _recipes_at(conf: float, plan: Sc.Plan):
    class _R:
        id = "recipes"

        def availability(self):
            return available("ready", model="grammar")

        def plan(self, req, *, timeout_s):
            return BrainResult(plan=plan.with_(confidence=conf), brain="recipes", ok=True, model="grammar")

        def text(self, task, *, timeout_s):
            return None
    return _R()


def test_the_router_drops_a_model_plan_with_an_unfilled_placeholder():
    f = TimelineFacts.minimal()
    full = P.plan("apply my brand kit @acme and add a hook", f)
    brains = {"recipes": _recipes_at(0.6, full), "apple_intelligence": _FakeFM(_brand_plan())}
    routed = router.plan(BrainRequest(prompt="x the brand", facts=f, recipes=R.cards()),
                         order=("recipes", "apple_intelligence"), brains=brains,
                         validate=V.validate_plan, prefetch=False)
    assert routed.brain == "recipes" and routed.plan is not None
    assert _step(routed.plan, "apply_brand_kit").args["handle"] == "@acme"
    fm = next(a for a in routed.attempts if a.brain == "apple_intelligence")
    assert fm.status == "failed" and "$ask:handle" in fm.reason


def test_the_fuller_recipe_reading_wins_over_a_thinner_model_plan():
    f = TimelineFacts.minimal()
    full = P.plan("apply my brand kit @acme and add a hook", f)
    thin = full.with_(steps=[s for s in full.steps if s.tool == "apply_brand_kit"], brain="apple_intelligence")
    brains = {"recipes": _recipes_at(0.6, full), "apple_intelligence": _FakeFM(thin)}
    routed = router.plan(BrainRequest(prompt="x", facts=f, recipes=R.cards()),
                         order=("recipes", "apple_intelligence"), brains=brains,
                         validate=lambda p, _f: p, prefetch=False)
    assert routed.brain == "recipes" and set(_tools(routed.plan)) == set(_tools(full))
    assert (routed.plan.reply or "").startswith("I read that as")


def test_the_chat_example_end_to_end_burns_the_handle_never_a_placeholder(workdir, no_downloads):
    store = F.make_store(workdir)
    ev = _turn(store, CHAT_EXAMPLE)
    assert not [e for e in ev if e["type"] == "error"], [e for e in ev if e["type"] == "error"]
    plan = next(e for e in ev if e["type"] == "plan")["plan"]
    assert plan["brain"] == "recipes" and not plan["needs_input"]
    edl = store.edl
    texts = [c.text for t in edl.tracks if t.type in ("text", "captions") for c in t.clips if hasattr(c, "text")]
    assert not [t for t in texts if "$ask" in t or "$arg" in t], texts
    assert edl.brand_kit is not None and edl.brand_kit.handle == "@quicksolutions.in"
    assert any("@quicksolutions.in" in t for t in texts)
    tools_run = [e["tool"] for e in ev if e["type"] == "step" and e.get("status") == "ok"]
    assert {"apply_brand_kit", "apply_hook_stack", "add_caption_track", "audit_aesthetic"} <= set(tools_run), tools_run


# =========================================================================== QA-043

def test_hinglish_from_hindi_is_transliteration_no_download_no_question():
    p = P.plan("add hinglish captions", HINDI)
    assert not p.downloads_needed and not p.blocking_questions, (p.downloads_needed, p.needs_input)
    assert _tools(p) == ["add_caption_track", "translate_captions"]
    tr = _step(p, "translate_captions").args
    assert tr == {"target_lang": "hinglish", "source_lang": "hi"}
    assert any(c.check == "captions_language" and c.args.get("target") == "hinglish" for c in p.postconditions)
    out = V.validate_plan(p, HINDI)                                    # no "needs MADLAD" refusal
    X.guard_step("translate_captions", _step(out, "translate_captions").args, HINDI)   # no model to fetch


def test_hinglish_from_english_still_asks_for_the_translation_model():
    en = HINDI.with_(language="en", spoken_language="en")
    p = P.plan("add hinglish captions", en)
    assert p.downloads_needed and p.blocking_questions[0].key == "downloads"
    skipped = P.apply_answers(p, {"downloads": "no"}, en)
    assert "translate_captions" not in _tools(skipped)               # English captions, never Devanagari
    with pytest.raises(X.StepRefused):
        X.guard_step("translate_captions", {"target_lang": "hinglish", "source_lang": "en"}, en)


def test_translate_to_hinglish_from_hindi_captions_needs_no_model():
    p = P.plan("translate the captions to hinglish", HINDI.with_(has_captions=True))
    assert not p.downloads_needed and _step(p, "translate_captions").args["target_lang"] == "hinglish"


def test_skip_never_turns_a_hinglish_request_into_devanagari():
    """No transcript yet (the language is unknown) and an accurate-captions
    request: the plan asks for MADLAD; Skip keeps a transliteration pass."""
    unknown = TimelineFacts.minimal(first_use=MADLAD_MISSING)
    p = P.plan("accurate captions in hinglish", unknown)
    assert _step(p, "auto_caption").args.get("target") == "hinglish"
    skipped = P.apply_answers(p, {"downloads": "no"}, unknown)
    tools = _tools(skipped)
    assert "auto_caption" in tools and tools.index("translate_captions") > tools.index("auto_caption")
    assert _step(skipped, "translate_captions").args["target_lang"] == "hinglish"
    assert any(c.check == "captions_language" for c in skipped.postconditions)


def test_devanagari_delivered_for_hinglish_fails_the_language_check():
    from video_ai_editor.edl.schema import TextClip, empty_edl
    edl = empty_edl()
    edl.get_track("captions").clips.append(TextClip(text="नमस्ते दोस्तों", start=0.0, end=1.0, role="caption"))
    pc = Sc.Postcondition(check="captions_language", args={"target": "hinglish"}, human="hinglish")
    # Whatever the facts said about the source — Hindi, or nothing at all (the
    # QA-043 run: no language known before the run → "cannot be judged", and
    # the card read 4/4 held over Devanagari captions).
    for before in (HINDI, TimelineFacts.minimal()):
        ctx = VF.VerifyCtx(store=type("S", (), {"edl": edl, "dir": "/tmp"})(),
                           plan=Sc.Plan.new(intent="x", brain="recipes"), exec_result=None, facts_before=before)
        assert VF.run_check(ctx, pc).passed is False, before.language


def test_dispatch_romanises_hindi_captions_without_calling_the_translator(workdir, monkeypatch):
    from video_ai_editor.ai import translate as _tr

    def _no_mt(*a, **k):
        raise AssertionError("MADLAD was called for Hindi → Hinglish")
    monkeypatch.setattr(_tr, "translate_text", _no_mt)
    src = F.speech_clip(workdir)
    F.write_ingest(src, F.transcript(HINDI_WORDS, language="hi"))
    store = F.make_store(workdir, src=src)
    D.dispatch(store, "add_caption_track", {"style": "ig_chunky"})
    before = " ".join(c.text for c in store.edl.get_track("captions").clips)
    assert "नमस्ते" in before
    res = D.dispatch(store, "translate_captions", {"target_lang": "hinglish"})
    after = " ".join(c.text for c in store.edl.get_track("captions").clips)
    assert res["translated"] >= 1 and "dhanyvaad" in after and "doston" in after
    assert not any(0x0900 <= ord(ch) <= 0x097F for ch in after), after


def test_add_hinglish_captions_end_to_end_on_hindi_speech(workdir, monkeypatch, no_downloads):
    from video_ai_editor.ai import translate as _tr

    def _no_mt(*a, **k):
        raise AssertionError("MADLAD was called for Hindi → Hinglish")
    monkeypatch.setattr(_tr, "translate_text", _no_mt)
    monkeypatch.setattr(_tr, "_ensure_model_downloaded", _no_mt, raising=False)
    src = F.speech_clip(workdir)
    F.write_ingest(src, F.transcript(HINDI_WORDS, language="hi"))
    store = F.make_store(workdir, src=src)
    ev = _turn(store, "add hinglish captions")
    assert not [e for e in ev if e["type"] in ("error", "clarify")], [e for e in ev if e["type"] in ("error", "clarify")]
    plan = next(e for e in ev if e["type"] == "plan")["plan"]
    assert not plan["downloads_needed"] and not plan["needs_input"]
    text = " ".join(c.text for c in store.edl.get_track("captions").clips)
    assert text and not any(0x0900 <= ord(ch) <= 0x097F for ch in text), text
    lang = next(c for c in _verify(ev)["checks"] if c["check"] == "captions_language")
    assert lang["pass"] is True, lang
    json.dumps(ev)          # the stream stays serialisable


def test_the_romaniser_never_leaks_devanagari():
    """The bench Hindi clip's upload transcript carries a 29 s Whisper
    hallucination of "ॐ ॐ ॐ …"; left as-is it kept Hinglish captions at 88 %
    Latin (the language check failed on the live run). Precomposed nukta
    letters (U+0958–U+095F) leaked the same way."""
    from video_ai_editor.ai.romanize import romanize
    assert romanize("ॐ ॐ ॐ") == "om om om"
    assert romanize("ज़िंदगी") == romanize("ज़िंदगी") == "zindgi"
    for cp in range(0x0900, 0x0980):
        out = romanize("क" + chr(cp) + "क")
        assert not any(0x0900 <= ord(ch) <= 0x097F for ch in out), (hex(cp), out)


def test_accurate_hinglish_on_hindi_speech_retranscribes_without_the_translation_model():
    p = P.plan("accurate captions in hinglish", HINDI)
    s = _step(p, "auto_caption")
    assert s.args["target"] == "hinglish" and s.args["language"] == "hi"
    assert not p.downloads_needed and not p.blocking_questions
    X.guard_step("auto_caption", s.args, HINDI)                        # no model to fetch
    V.validate_plan(p, HINDI)
