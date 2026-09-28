"""Final QA: Apple Intelligence drafts grounded to what the prompt says.

Replayed through the real FMBrain → grounding → recipes → validate path
(the helper's JSON answer is stubbed, as in test_c6_prompt_paraphrase):

  * "revers clip 1 pls" / "revrse the 2nd clip" — the grammar misses the
    typo, the model's draft carries no clip_ref, and the recipe's default
    reversed EVERY clip ("Reverse: done 1/1");
  * "add a lower third saying Jane Doe, Producer" came back as a SUPER title
    with that text (top of frame, Anton 140 px, replacing the user's title).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_c6_prompt_paraphrase import _fm_steps  # noqa: E402

from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402

THREE = TimelineFacts.minimal(v1_clip_ids=["c_a", "c_b", "c_c"], clip_ids=["c_a", "c_b", "c_c", "t_1"],
                              v1_boundaries=[4.0, 8.0], duration=12.0, track_ids=["v1", "text"])


@pytest.mark.parametrize("prompt,draft,want", [
    ("revers clip 1 pls", {"recipe": "reverse"}, "$v1_first"),
    ("revrse the 2nd clip", {"recipe": "reverse", "clip_ref": "all"}, "c_b"),
    ("rverse the last clip plz", {"recipe": "reverse", "clip_ref": "$v1_all"}, "$v1_last"),
])
def test_a_typod_one_clip_prompt_edits_only_that_clip(prompt, draft, want):
    steps = _fm_steps(prompt, [dict(draft)], THREE)
    assert steps == [("set_clip_reverse", {"clip_id": want, "reverse": True})], steps


def test_an_all_clips_draft_stays_all_clips_when_the_prompt_names_none():
    steps = _fm_steps("revers everything", [{"recipe": "reverse"}], THREE)
    assert [a["clip_id"] for _, a in steps] == ["$v1_all"], steps


def test_a_lower_third_draft_with_text_becomes_a_name_card():
    steps = _fm_steps("add a lower third saying Jane Doe, Producer",
                      [{"recipe": "title", "text": "Jane Doe, Producer"}], THREE)
    assert [(t, a.get("name"), a.get("handle")) for t, a in steps] == [
        ("add_lower_third", "Jane Doe", "Producer")], steps
