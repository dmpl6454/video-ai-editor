"""auto_caption's three caption targets, and the routing behind them.

Whisper is stubbed out — these tests are about which ROUTE is chosen, which is
the part that has to be right before a 200-second decode is spent on it. The
routing rule exists because of two hard external constraints, both verified
against the installed packages:

  * Whisper translates INTO English only.
  * Argos publishes exactly one package into Hindi: en→hi.

So Hindi captions for non-Hindi audio must ask Whisper for English on the single
decode pass and translate afterwards, and choosing that requires knowing the
spoken language BEFORE decoding.
"""
import json
import threading
from pathlib import Path

import pytest

# agent/__init__.py re-exports the `dispatch` FUNCTION, which shadows the
# submodule of the same name — so both `from video_ai_editor.agent import
# dispatch` and `import video_ai_editor.agent.dispatch as D` hand back the
# function (the `as` form resolves by attribute lookup too). import_module goes
# to sys.modules and gets the module itself.
from importlib import import_module

D = import_module("video_ai_editor.agent.dispatch")
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip
from video_ai_editor.ingest import transcribe as T


def _seg(text: str, start: float = 0.0, end: float = 2.5) -> dict:
    """A whisper-shaped segment whose `words` actually cover its `text`.

    Cue building reads the WORD stream, not the segment text (caption_format.
    cues_from_segments), so a fixture with partial words produces partial cues
    and would test the fixture rather than the routing.
    """
    toks = text.split(" ")
    step = (end - start) / max(1, len(toks))
    return {
        "id": 0, "start": start, "end": end, "text": text,
        "words": [{"start": start + i * step, "end": start + (i + 1) * step,
                   "word": tok, "prob": 1.0} for i, tok in enumerate(toks)],
    }


HINDI_SEGMENTS = [_seg("अपनी last meeting के बाद")]
CHINESE_SEGMENTS = [_seg("这是 中文 视频")]
ENGLISH_SEGMENTS = [_seg("after our last meeting")]
SPANISH_SEGMENTS = [_seg("después de nuestra última reunión")]


def _store(tmp_path: Path) -> EDLStore:
    src = tmp_path / "v.mp4"
    src.write_bytes(b"not really a video, nothing decodes it in these tests")
    store = EDLStore(tmp_path / "session")
    v1 = store.edl.get_track("v1")
    v1.clips.append(Clip(src=str(src), in_=0.0, out=5.0, start=0.0))
    store.commit("seed", {}, "seed")
    return store


class _FakeTranscript:
    """Stands in for ingest.transcribe.Transcript (only .language/.model_dump)."""

    def __init__(self, language, segments):
        self.language = language
        self._segments = segments
        self.duration = 5.0

    def model_dump(self):
        return {"language": self.language, "duration": self.duration,
                "segments": [dict(s) for s in self._segments]}


@pytest.fixture
def spy(monkeypatch):
    """Record how transcribe/translate were called, and control what they return."""
    calls = {"transcribe": [], "translate": [], "detect": 0}
    plan = {"spoken": "hi", "detected": "hi",
            "transcribe_segments": HINDI_SEGMENTS,
            "translate_segments": ENGLISH_SEGMENTS}

    def fake_transcribe(path, language=None, model_size=None, backend=None,
                        task="transcribe", on_progress=None, should_cancel=None):
        calls["transcribe"].append({"language": language, "task": task,
                                    "has_progress": on_progress is not None,
                                    "has_cancel": should_cancel is not None})
        if on_progress is not None:
            on_progress(0.5, 2.5, 5.0)
            on_progress(1.0, 5.0, 5.0)
        if task == "translate":
            return _FakeTranscript(plan["spoken"], plan["translate_segments"])
        return _FakeTranscript(plan["spoken"], plan["transcribe_segments"])

    def fake_detect(path, model_size=None):
        calls["detect"] += 1
        return plan["detected"]

    def fake_translate_segments(segments, from_code="en", to_code="hi"):
        calls["translate"].append((from_code, to_code))
        # Models the real Argos coverage this feature depends on: exactly one
        # package into Hindi (en->hi) and, for these tests, the same shape into
        # Spanish (en->es) — pt->es also exists for real but no test scenario
        # here exercises a Portuguese source, so it is out of scope.
        if to_code in ("hi", "es") and from_code != "en":
            raise RuntimeError(f"no Argos package for {from_code}->{to_code}")
        text = "नमस्ते दुनिया" if to_code == "hi" else "Hola mundo"
        return [{**s, "text": text} for s in segments]

    monkeypatch.setattr(T, "transcribe", fake_transcribe)
    monkeypatch.setattr(T, "detect_language", fake_detect)
    import video_ai_editor.ai.translate as TR
    monkeypatch.setattr(TR, "translate_segments", fake_translate_segments)
    return calls, plan


def _cues(store):
    cap = store.edl.get_track("captions")
    return [c.text for c in (cap.clips if cap else [])]


# --- English target ---------------------------------------------------------

def test_english_target_uses_whispers_own_translation_and_no_second_model(tmp_path, spy):
    """One decode pass, no translation package. Whisper's translate task is the
    whole job, so a missing Argos install cannot break English captions."""
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "en"})
    assert [c["task"] for c in calls["transcribe"]] == ["translate"]
    assert calls["translate"] == []            # no Argos involvement at all
    assert out["language"] == "en"
    assert out["spoken"] == "zh"
    assert "after our last meeting" in " ".join(_cues(store)).lower()


def test_english_target_does_not_pay_for_language_detection(tmp_path, spy):
    """The task is 'translate' whatever was spoken, so probing would be waste."""
    calls, plan = spy
    plan.update(spoken="zh", detected="zh")
    D.auto_caption(_store(tmp_path), {"target": "en"})
    assert calls["detect"] == 0


def test_reported_model_reflects_the_substitution_not_the_request(tmp_path, spy):
    """turbo cannot serve task="translate" (fine-tuned on transcription only —
    asked to translate, it returns five tokens of ellipses), so `transcribe()`
    silently substitutes large-v3. Without resolving that HERE too, the summary,
    the op log, and the caption-button toast would all say "turbo" while
    large-v3 actually ran — a caption run reporting the wrong model is exactly
    the kind of stale-info bug this whole module exists to prevent."""
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "en", "model": "turbo"})
    assert out["model"] == T.TRANSLATION_MODEL
    assert "turbo" not in out["summary"]
    assert T.TRANSLATION_MODEL in out["summary"]


def test_reported_model_is_unchanged_when_the_request_can_run_as_asked(tmp_path, spy):
    """The substitution must be conditional — turbo transcribing (not
    translating) is exactly what it is for, and must be reported as itself."""
    calls, plan = spy
    plan.update(spoken="hi", detected="hi", transcribe_segments=HINDI_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "hi", "model": "turbo"})
    assert out["model"] == "turbo"


# --- Hindi target -----------------------------------------------------------

def test_hindi_audio_hindi_target_is_a_plain_transcription(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="hi", detected="hi", transcribe_segments=HINDI_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "hi"})
    assert [c["task"] for c in calls["transcribe"]] == ["transcribe"]
    assert calls["translate"] == []
    assert out["language"] == "hi"
    assert "अपनी" in " ".join(_cues(store))


def test_chinese_audio_hindi_target_pivots_through_english_in_one_pass(tmp_path, spy):
    """The scenario this feature exists for: a Chinese video, Hindi subtitles.

    Whisper is asked for English on the ONE decode pass (asking for Chinese and
    translating afterwards would need a zh→hi package that does not exist), then
    Argos does en→hi.
    """
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "hi"})
    assert [c["task"] for c in calls["transcribe"]] == ["translate"], \
        "must not decode twice — the task has to be chosen before the pass"
    assert calls["translate"] == [("en", "hi")]
    assert out["language"] == "hi"
    assert out["spoken"] == "zh"
    assert "→" in out["summary"]              # the summary names both languages
    # The actual regression: `translate_segments` only overwrites `.text`,
    # leaving each segment's word-level timestamps as the ORIGINAL English
    # tokens. `cues_from_segments` prefers `.words` over `.text` when present,
    # so every cue was built from the stale, UNTRANSLATED word list — this
    # scenario reported success (routing metadata all correct) while every
    # caption on screen was still in English. Checking the actual rendered
    # text is the only way this is caught; routing assertions alone pass
    # either way.
    text = " ".join(_cues(store))
    assert "नमस्ते" in text, f"cue text was not translated — still: {text!r}"
    assert "after our last meeting" not in text.lower(), (
        "cues were built from the STALE English word list, not the translated text")


def test_the_spoken_language_hint_is_honoured_without_probing(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="zh", detected="hi")   # detection would disagree
    D.auto_caption(_store(tmp_path), {"target": "hi", "language": "zh"})
    assert calls["detect"] == 0                # the caller told us; don't probe
    assert [c["task"] for c in calls["transcribe"]] == ["translate"]


def test_a_third_language_with_no_hindi_package_falls_back_to_two_hops(tmp_path, spy):
    """When detection fails we decode natively and can land on, say, Japanese.
    src→hi does not exist, so src→en→hi is tried before giving up."""
    calls, plan = spy
    plan.update(spoken="ja", detected=None, transcribe_segments=CHINESE_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "hi"})
    assert calls["translate"] == [("ja", "hi"), ("ja", "en"), ("en", "hi")]
    assert out["language"] == "hi"
    assert "नमस्ते" in " ".join(_cues(store))


def test_no_route_into_hindi_refuses_loudly_and_names_the_english_way_out(tmp_path, spy, monkeypatch):
    """A caption track that silently stayed in the source language while the UI
    reported success is worse than an error, so this must raise."""
    calls, plan = spy
    plan.update(spoken="ja", detected=None)
    import video_ai_editor.ai.translate as TR

    def always_fail(segments, from_code="en", to_code="hi"):
        raise RuntimeError("offline")
    monkeypatch.setattr(TR, "translate_segments", always_fail)
    with pytest.raises(RuntimeError) as ei:
        D.auto_caption(_store(tmp_path), {"target": "hi"})
    msg = str(ei.value)
    assert "English" in msg and "ja" in msg


# --- Hinglish target -------------------------------------------------------

def test_hinglish_romanises_hindi_and_leaves_no_devanagari(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="hi", detected="hi", transcribe_segments=HINDI_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "hinglish"})
    text = " ".join(_cues(store))
    assert not any("ऀ" <= ch <= "ॿ" for ch in text), text
    assert "apni" in text.lower()
    assert out["language"] == "hi-Latn"
    # Latin words Whisper already produced survive untouched.
    assert "last meeting" in text.lower()


def test_hinglish_from_chinese_goes_translate_then_hindi_then_romanise(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "hinglish"})
    assert [c["task"] for c in calls["transcribe"]] == ["translate"]
    assert calls["translate"] == [("en", "hi")]
    assert out["language"] == "hi-Latn"
    text = " ".join(_cues(store))
    assert not any("ऀ" <= ch <= "ॿ" for ch in text)
    # "not any Devanagari" alone passes EITHER way — plain untranslated English
    # (the bug: stale `.words` overriding the translated `.text`) has no
    # Devanagari either, so it would slip through this assertion unnoticed.
    # Check for the actual romanized content instead.
    assert "namste duniya" in text, f"expected the romanized translation, got: {text!r}"
    assert "after our last meeting" not in text.lower()


# --- Spanish target ----------------------------------------------------------
# Same shape as Hindi throughout — deliberately, since `_translate_segments_to`
# is the SAME function for both, keyed only by `to_code`. These tests exist to
# catch a target-specific regression (a hardcoded "hi" reappearing somewhere),
# not to re-prove the pivot logic itself.

def test_spanish_audio_spanish_target_is_a_plain_transcription(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="es", detected="es", transcribe_segments=SPANISH_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "es"})
    assert [c["task"] for c in calls["transcribe"]] == ["transcribe"]
    assert calls["translate"] == []
    assert out["language"] == "es"
    assert "después" in " ".join(_cues(store))


def test_chinese_audio_spanish_target_pivots_through_english_in_one_pass(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "es"})
    assert [c["task"] for c in calls["transcribe"]] == ["translate"], \
        "must not decode twice — the task has to be chosen before the pass"
    assert calls["translate"] == [("en", "es")]
    assert out["language"] == "es"
    assert out["spoken"] == "zh"
    assert "→" in out["summary"]
    assert "hi" not in out["summary"], (
        "a hardcoded Hindi suffix in the summary-builder would silently "
        "misreport a Spanish run")
    # Same regression as the Hindi pivot test: `.words` surviving translation
    # would make cues_from_segments render the STALE English word list instead
    # of the translated `.text` — routing metadata alone can't catch this.
    text = " ".join(_cues(store))
    assert "Hola mundo" in text, f"cue text was not translated — still: {text!r}"
    assert "after our last meeting" not in text.lower()


def test_a_third_language_with_no_spanish_package_falls_back_to_two_hops(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="ja", detected=None, transcribe_segments=CHINESE_SEGMENTS)
    store = _store(tmp_path)
    out = D.auto_caption(store, {"target": "es"})
    assert calls["translate"] == [("ja", "es"), ("ja", "en"), ("en", "es")]
    assert out["language"] == "es"
    assert "Hola mundo" in " ".join(_cues(store))


def test_no_route_into_spanish_refuses_loudly_and_names_the_english_way_out(tmp_path, spy, monkeypatch):
    calls, plan = spy
    plan.update(spoken="ja", detected=None)
    import video_ai_editor.ai.translate as TR

    def always_fail(segments, from_code="en", to_code="es"):
        raise RuntimeError("offline")
    monkeypatch.setattr(TR, "translate_segments", always_fail)
    with pytest.raises(RuntimeError) as ei:
        D.auto_caption(_store(tmp_path), {"target": "es"})
    msg = str(ei.value)
    assert "Spanish" in msg and "ja" in msg


def test_spanish_target_accepts_aliases(tmp_path, spy):
    calls, plan = spy
    plan.update(spoken="es", detected="es", transcribe_segments=SPANISH_SEGMENTS)
    for alias in ("spanish", "Spanish", "español", "ES"):
        store = _store(tmp_path)
        out = D.auto_caption(store, {"target": alias})
        assert out["language"] == "es", f"alias {alias!r} did not normalize to es"


def test_the_spec_bundles_argostranslate():
    """`ai/translate.py` loads Argos via `importlib.import_module("argostranslate")`
    — a STRING, not a static `import argostranslate` — so PyInstaller's analysis
    has no way to discover it needs bundling. The packaged exe raised
    `ModuleNotFoundError: No module named 'argostranslate'` the first time any
    target actually exercised the translate-via-Argos path (every earlier
    Hindi/Hinglish test happened to use already-Hindi-spoken audio, so the
    translation branch was never reached) — meaning EVERY translated target
    (hi, hinglish, es) was broken in the packaged app for any non-matching
    spoken language, the exact "third-language video" scenario this feature
    exists for. No dev path notices this (argostranslate is a plain pip
    package there); only a real frozen build does, so assert it rather than
    trusting review to catch a dynamic-import gap again.
    """
    from pathlib import Path
    spec = Path(__file__).resolve().parents[1] / "Video AI Editor.spec"
    text = spec.read_text(encoding="utf-8")
    assert "collect_submodules('argostranslate')" in text


def test_the_macos_build_script_also_bundles_argostranslate():
    """`build_app.sh` does NOT use Video AI Editor.spec — PyInstaller's CLI mode
    it invokes there regenerates/overwrites the .spec as a side effect, so the
    two build paths are independent (see CLAUDE.md). The .spec getting the
    `collect_submodules('argostranslate')` fix above does nothing for a macOS
    build; this pins the same fix into the actual macOS build path so this
    exact gap can't reappear on the one platform we can't test directly.
    """
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "build_app.sh"
    text = script.read_text(encoding="utf-8")
    assert "--collect-submodules argostranslate" in text
    # Must not be sitting in the (unrelated) --exclude-module list either.
    assert "--exclude-module argostranslate" not in text


def test_translate_segments_to_strips_stale_word_level_timing(monkeypatch):
    """Direct unit test of the actual defect: `ai.translate.translate_segments`
    only overwrites `.text`; the per-word timestamps are still the ORIGINAL
    language's tokens at their original positions (a translation has no 1:1
    word alignment to preserve them against). `cues_from_segments` prefers
    `.words` over `.text` whenever `.words` is non-empty, so a segment that
    still carried them rendered as the STALE untranslated words no matter what
    `.text` said — silently, since every routing/summary field still correctly
    named the requested target language. This is the fix at its source, not
    through the full auto_caption pipeline.
    """
    import video_ai_editor.ai.translate as TR

    def fake_translate_segments(segments, from_code="en", to_code="hi"):
        # Faithfully mimics the real function's shape: rewrites `.text`,
        # leaves everything else — including `.words` — untouched.
        return [{**s, "text": "TRANSLATED"} for s in segments]

    monkeypatch.setattr(TR, "translate_segments", fake_translate_segments)
    segs = [{"id": 0, "start": 0.0, "end": 1.0, "text": "original english",
            "words": [{"start": 0.0, "end": 0.5, "word": "original", "prob": 1.0},
                      {"start": 0.5, "end": 1.0, "word": "english", "prob": 1.0}]}]
    out = D._translate_segments_to(segs, "en", "hi")
    assert out[0]["text"] == "TRANSLATED"
    assert "words" not in out[0], (
        "stale word-level timing survived translation — cues_from_segments "
        "will render the OLD language's words, ignoring the translated text")


def test_hinglish_is_not_accidentally_confused_with_spanish():
    """Sanity check on _TARGET_TO_CODE: hinglish still maps to hi, not es —
    a copy-paste slip here would silently romanise Spanish captions."""
    assert D._TARGET_TO_CODE["hinglish"] == "hi"
    assert D._TARGET_TO_CODE["es"] == "es"


# --- no target: the long-standing behaviour ---------------------------------

def test_omitting_target_keeps_the_previous_behaviour_exactly(tmp_path, spy):
    """Every existing caller — chat, MCP, templates — sends no target and must
    keep getting captions in whatever was spoken, with no probe and no
    translation."""
    calls, plan = spy
    plan.update(spoken="hi", detected="hi")
    store = _store(tmp_path)
    out = D.auto_caption(store, {})
    assert [c["task"] for c in calls["transcribe"]] == ["transcribe"]
    assert calls["translate"] == [] and calls["detect"] == 0
    assert out["target"] is None
    assert "अपनी" in " ".join(_cues(store))


# --- argument validation ---------------------------------------------------

@pytest.mark.parametrize("alias,expected", [
    ("hinglish", "hinglish"), ("HINGLISH", "hinglish"), ("roman", "hinglish"),
    ("hindi", "hi"), ("HI", "hi"), ("english", "en"), ("EN", "en"),
])
def test_target_aliases_and_case(tmp_path, spy, alias, expected):
    out = D.auto_caption(_store(tmp_path), {"target": alias})
    assert out["target"] == expected


def test_target_also_accepts_the_target_lang_spelling(tmp_path, spy):
    out = D.auto_caption(_store(tmp_path), {"target_lang": "hinglish"})
    assert out["target"] == "hinglish"


def test_an_unknown_target_is_a_clean_error_naming_the_choices(tmp_path, spy):
    with pytest.raises(ValueError) as ei:
        D.auto_caption(_store(tmp_path), {"target": "klingon"})
    assert "hinglish" in str(ei.value)


# --- progress and cancellation ---------------------------------------------

def test_progress_is_reported_and_ends_at_one(tmp_path, spy):
    seen: list[float] = []
    D.auto_caption(_store(tmp_path), {"target": "hi"}, set_progress=seen.append)
    assert seen, "no progress was reported"
    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert seen[-1] == pytest.approx(1.0)
    # Decode must not claim the whole bar — translating and cue building follow.
    assert max(s for s in seen if s < 1.0) <= 0.98


def test_the_cancel_event_reaches_the_decoder(tmp_path, spy):
    ev = threading.Event()
    ev.set()
    calls, _ = spy
    D.auto_caption(_store(tmp_path), {"target": "hi"}, cancel_event=ev)
    assert calls["transcribe"][0]["has_cancel"] is True


def test_dispatch_injects_the_hooks_only_when_the_handler_asks(tmp_path, spy):
    """The injection is opt-in by signature, so a two-argument handler such as
    get_timeline must never be handed these keywords."""
    store = _store(tmp_path)
    seen: list[float] = []
    ev = threading.Event()
    D.dispatch(store, "auto_caption", {"target": "hi"},
               set_progress=seen.append, cancel_event=ev)
    assert seen and seen[-1] == pytest.approx(1.0)
    # get_timeline declares neither hook; passing them must not raise.
    out = D.dispatch(store, "get_timeline", {}, set_progress=seen.append, cancel_event=ev)
    assert "duration" in out


def test_chat_and_mcp_callers_pass_nothing_and_still_work(tmp_path, spy):
    out = D.dispatch(_store(tmp_path), "auto_caption", {"target": "hinglish"})
    assert out["language"] == "hi-Latn"


# --- the transcript written back to disk -----------------------------------

def test_the_persisted_transcript_matches_the_captions_not_the_audio(tmp_path, spy):
    """An .srt export reads this file, so it must hold the caption-language text.
    `language` has to be rewritten too — translate_captions auto-detects its
    source from it, and leaving 'zh' on Hindi text would mistranslate later."""
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    D.auto_caption(store, {"target": "hi"})
    src = Path(store.edl.get_track("v1").clips[0].src)
    data = json.loads((src.parent / "ingest.json").read_text(encoding="utf-8"))
    assert data["transcript"]["language"] == "hi"
    assert "नमस्ते" in data["transcript"]["segments"][0]["text"]


def test_the_spoken_language_is_remembered_separately_from_the_transcript(tmp_path, spy):
    """A second Hindi run on Chinese footage must still know the audio is
    Chinese. The transcript's own `language` says 'hi' by then (it holds the
    translated text), so the spoken language is stored beside it."""
    calls, plan = spy
    plan.update(spoken="zh", detected="zh", translate_segments=ENGLISH_SEGMENTS)
    store = _store(tmp_path)
    D.auto_caption(store, {"target": "hi"})
    src = Path(store.edl.get_track("v1").clips[0].src)
    data = json.loads((src.parent / "ingest.json").read_text(encoding="utf-8"))
    assert data["spoken_language"] == "zh"
    assert data["transcript"]["language"] == "hi"

    # Second run: the hint comes from spoken_language, so no probe is needed
    # and the pivot route is chosen again.
    calls["detect"] = 0
    calls["transcribe"].clear()
    calls["translate"].clear()
    D.auto_caption(store, {"target": "hi"})
    assert calls["detect"] == 0
    assert [c["task"] for c in calls["transcribe"]] == ["translate"]
    assert calls["translate"] == [("en", "hi")]
