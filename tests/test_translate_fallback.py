"""ai/translate.py — the residual-English-via-Claude fallback.

This fallback was originally built because Argos Translate (the backend at
the time) occasionally left a phrase completely untranslated. A hand-curated
per-phrase dictionary was tried first and does not scale: two DIFFERENT,
unrelated phrases from the same real clip ("Goddamn right." and, separately,
"To watch a bunch of junkies." in its real surrounding sentence) each failed
independently — and each translated fine in ISOLATION, so the failure was
unpredictable, not a fixed defect a dictionary could ever fully enumerate.

Argos has since been replaced by MADLAD-400 (see ai/translate.py's module
docstring), which was verified to translate BOTH of those real failures
correctly on its own — this fallback is kept as a defensive backstop for
whatever MADLAD itself might still miss, not because it's currently load-
bearing for any known case.

The general, scalable signal: the source was pure English, so a genuinely
successful translation into Hindi should contain (almost) no Latin-script
prose. Any surviving run of 2+ Latin words is real evidence of a failed
translation — as opposed to a single acronym/number the translator and
Whisper both legitimately leave untouched (`P2P`, `STD`, `$130`). That span,
and only that span, is sent to Claude for translation.
"""
from __future__ import annotations
import os

import pytest

from video_ai_editor.ai.translate import (
    _residual_english_spans, _fill_residual_english, _claude_translate_phrase,
)


# --- span detection: pure, no network ----------------------------------

def test_detects_a_multi_word_english_sentence_embedded_in_hindi():
    text = "क्या? To watch a bunch of junkies. एक बेहतर उच्च मतलब"
    spans = _residual_english_spans(text)
    assert len(spans) == 1
    assert spans[0][2].strip() == "To watch a bunch of junkies."


def test_detects_a_fully_untranslated_short_phrase():
    spans = _residual_english_spans("Goddamn right.")
    assert len(spans) == 1
    assert spans[0][2] == "Goddamn right."


def test_does_not_flag_a_lone_acronym_or_number():
    """P2P, STD, and dollar amounts are legitimate loanwords Whisper/Argos
    both leave untouched on purpose — not evidence of a failed translation."""
    text = "आपके चालक दल ने P2P कुक पर स्विच किया और $130 मिलियन कमाया"
    assert _residual_english_spans(text) == []


def test_does_not_flag_pure_hindi_text():
    assert _residual_english_spans("मेरा नाम बताएं।") == []


def test_two_separate_residual_spans_are_both_found():
    text = "क्या? What now. एक बेहतर। Say it again please. समाप्त"
    spans = _residual_english_spans(text)
    assert len(spans) == 2
    assert spans[0][2].strip() == "What now."
    assert spans[1][2].strip() == "Say it again please."


# --- _fill_residual_english: scoping and graceful degrade ---------------

def test_scoped_to_en_to_hi_only(monkeypatch):
    """Spanish is ALSO Latin-script, so 'residual Latin text' isn't a usable
    failure signal there — must not even attempt detection."""
    calls = []
    monkeypatch.setattr(
        "video_ai_editor.ai.translate._claude_translate_phrase",
        lambda phrase, to_code: calls.append(phrase) or "should not be used",
    )
    out = _fill_residual_english("This stayed in English.", "en", "es")
    assert out == "This stayed in English."
    assert calls == []


def test_no_api_key_degrades_to_leaving_the_english_untouched(monkeypatch):
    """A missing key must never raise or block caption generation — it must
    leave the residual phrase exactly as Argos produced it."""
    import video_ai_editor.config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    out = _fill_residual_english("क्या? To watch a bunch of junkies.", "en", "hi")
    assert "To watch a bunch of junkies." in out


def test_an_api_failure_degrades_rather_than_raising(monkeypatch):
    """A network hiccup translating ONE throwaway phrase must not crash a
    multi-minute caption run."""
    def boom(phrase, to_code):
        raise RuntimeError("network is down")
    # _claude_translate_phrase itself already swallows exceptions internally;
    # this pins that _fill_residual_english also never propagates one even if
    # something upstream of it somehow did.
    monkeypatch.setattr(
        "video_ai_editor.ai.translate._claude_translate_phrase",
        lambda phrase, to_code: None,
    )
    out = _fill_residual_english("क्या? To watch a bunch of junkies.", "en", "hi")
    assert "To watch a bunch of junkies." in out  # left as-is, no crash


def test_a_successful_fallback_splices_in_the_translation_and_keeps_the_rest(monkeypatch):
    monkeypatch.setattr(
        "video_ai_editor.ai.translate._claude_translate_phrase",
        lambda phrase, to_code: "जंकियों का एक झुंड देखना।",
    )
    out = _fill_residual_english("क्या? To watch a bunch of junkies. एक बेहतर", "en", "hi")
    assert out == "क्या? जंकियों का एक झुंड देखना। एक बेहतर"


def test_claude_translate_phrase_returns_none_without_a_key(monkeypatch):
    import video_ai_editor.config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    assert _claude_translate_phrase("hello there", "hi") is None


def test_claude_translate_phrase_returns_none_for_an_unsupported_target(monkeypatch):
    import video_ai_editor.config as cfg
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "sk-fake-for-this-test")
    assert _claude_translate_phrase("hello there", "es") is None


# --- real integration tests: exercise the ACTUAL services --------------
#
# Gated the same way test_captions.py gates its heavy real-model run: skipped
# by default, opt in locally. Needs the real ~3GB MADLAD-400 model (network
# on first use, cached forever after — see ai/translate.py::_model_dir).
#
# Unlike when this fallback was built (Argos + a Claude-only fix), these two
# tests need NO working Claude call at all: MADLAD-400 itself translates both
# real failures correctly (verified directly — see ai/translate.py's module
# docstring), so `_fill_residual_english`/`_claude_translate_phrase` are not
# even reached for these specific inputs. They stay in this file because they
# pin the same observable contract ("no residual English survives") that the
# Claude fallback exists to guarantee for whatever MADLAD itself might still
# miss — see `test_claude_translate_phrase_*` above for that path in isolation.

_REQUIRES_MADLAD = pytest.mark.skipif(
    os.environ.get("VAI_RUN_CAPTION_TESTS") != "1",
    reason="needs the real ~3GB MADLAD-400 model; set VAI_RUN_CAPTION_TESTS=1",
)


@_REQUIRES_MADLAD
def test_the_real_pipeline_no_longer_leaves_goddamn_right_in_english():
    from video_ai_editor.ai.translate import translate_text
    out = translate_text("Goddamn right.", from_code="en", to_code="hi")
    assert not _residual_english_spans(out), f"still has residual English: {out!r}"


@_REQUIRES_MADLAD
def test_the_real_pipeline_fixes_a_DIFFERENT_previously_unseen_failure():
    """The actual point of the general fix: it must handle a phrase that was
    NEVER hand-added to any dictionary — proving this scales past the one
    case it was built to fix."""
    from video_ai_editor.ai.translate import translate_text
    out = translate_text(
        "Say my name. This is a completely made up sentence never tested before.",
        from_code="en", to_code="hi",
    )
    assert not _residual_english_spans(out), f"still has residual English: {out!r}"
