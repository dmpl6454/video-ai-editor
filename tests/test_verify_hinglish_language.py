"""The Hinglish caption check must know the source is Hindi when the
transcript was made DURING the run.

A fresh upload with transcribe=false, then "add hinglish captions": the plan
transcribes first, so `facts_before` carries no language at all, and the
check answered "source language is not Hindi; romanisation cannot be judged"
over Hindi audio. It now reads the transcript the run persisted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.prompt import verify as V  # noqa: E402
from video_ai_editor.agent.prompt.schema import CHECK_SPECS, Postcondition  # noqa: E402
from video_ai_editor.edl.schema import TextClip  # noqa: E402

pytestmark = pytest.mark.usefixtures("desktop_posture")

_PC = Postcondition(check="captions_language", args={"target": "hinglish"}, human="hinglish",
                    needs_render=CHECK_SPECS["captions_language"].needs_render,
                    headline=CHECK_SPECS["captions_language"].headline)


def _run(tmp_path, language):
    src = F.speech_clip(tmp_path)
    tx = F.transcript()
    tx["language"] = language
    F.write_ingest(src, tx)
    store = F.make_store(tmp_path, src=src)
    store.edl.get_track("captions").clips.append(
        TextClip(text="namaste doston aaj main", start=0.2, end=2.8, role="caption"))
    # Facts taken BEFORE the run: no transcript existed, so no language.
    facts = F.facts_for(store, has_transcript=False, language=None, spoken_language=None)
    ctx = V.VerifyCtx(store=store, plan=F.plan_of(), exec_result=None, facts_before=facts)
    return V.run_check(ctx, _PC)


def test_hindi_transcript_made_during_the_run_is_judged(tmp_path):
    r = _run(tmp_path, "hi")
    assert r.passed is True, r.as_dict()


def test_unknown_language_is_not_called_not_hindi(tmp_path):
    r = _run(tmp_path, None)
    assert r.passed is None
    assert "unknown" in r.detail and "not Hindi" not in r.detail, r.detail


def test_english_source_still_cannot_be_judged(tmp_path):
    r = _run(tmp_path, "en")
    assert r.passed is None and "not Hindi" in r.detail
