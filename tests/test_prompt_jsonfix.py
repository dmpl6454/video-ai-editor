"""`jsonfix.repair` turns a small model's almost-JSON into a dict — one
repair at a time, re-parsing after each, never touching string contents
(spec §3.3). Every case below is a shape Qwen2.5-7B/3B actually emits."""
from __future__ import annotations

import pytest

from video_ai_editor.agent.prompt.brains import jsonfix
from video_ai_editor.agent.prompt.brains.jsonfix import JsonRepairFailed, repair

DEVANAGARI = "कैप्शन जोड़ो और साइलेंस हटाओ"


def test_valid_json_passes_through_untouched():
    src = '{"intents": [{"recipe": "captions", "slots": {"style": "ig_chunky"}}], "reply": "%s"}' % DEVANAGARI
    out = repair(src)
    assert out["intents"][0]["slots"]["style"] == "ig_chunky"
    assert out["reply"] == DEVANAGARI


def test_code_fence_and_prose_are_stripped():
    src = 'Sure! Here is the plan:\n```json\n{"intents": [], "confidence": 0.8}\n```\nLet me know.'
    assert repair(src) == {"intents": [], "confidence": 0.8}
    assert repair('Plan: {"a": 1} trailing words') == {"a": 1}


def test_trailing_commas_are_removed_outside_strings_only():
    assert repair('{"a": [1, 2,], "b": {"c": 3,},}') == {"a": [1, 2], "b": {"c": 3}}
    # The comma-brace inside the string is content, not syntax.
    assert repair('{"reply": "wait,}", "n": 1,}') == {"reply": "wait,}", "n": 1}


def test_python_literals_only_outside_strings():
    out = repair('{"ok": True, "none": None, "off": False, "reply": "True story"}')
    assert out == {"ok": True, "none": None, "off": False, "reply": "True story"}


def test_unquoted_keys_are_quoted():
    assert repair('{recipe: "reframe", slots: {ratio: "9:16"}}') == {"recipe": "reframe", "slots": {"ratio": "9:16"}}


def test_single_quotes_keep_apostrophes_and_devanagari():
    out = repair("{'reply': 'don't scroll', 'intents': [{'recipe': 'hook', 'slots': {'text': 'it's here'}}]}")
    assert out["reply"] == "don't scroll"
    assert out["intents"][0]["slots"]["text"] == "it's here"
    out = repair("{'reply': '%s', 'confidence': 0.9}" % DEVANAGARI)
    assert out["reply"] == DEVANAGARI and out["confidence"] == 0.9


def test_double_quoted_apostrophe_is_never_a_string_boundary():
    out = repair('{"reply": "don\'t scroll", "x": [1,],}')     # only the trailing commas need fixing
    assert out == {"reply": "don't scroll", "x": [1]}


def test_single_quote_conversion_is_the_last_gentle_repair():
    # A string with `,}` is fine until the single-quote step, which must be
    # reached only after trailing-comma removal failed to produce a parse.
    out = repair("{'a': 'x,}',}")
    assert out == {"a": "x,}"}


def test_truncated_output_is_closed_after_the_last_complete_value():
    src = '{"intents": [{"recipe": "captions", "slots": {"style": "ig_chunky"}}], "confidence": 0.9, "reply": "adding cap'
    out = repair(src)
    assert out["intents"][0]["recipe"] == "captions" and out["confidence"] == 0.9
    src2 = '{"intents": [{"recipe": "reframe", "slots": {"ratio": "9:16"}}, {"recipe": "capt'
    out2 = repair(src2)
    assert [i["recipe"] for i in out2["intents"]] == ["reframe"]
    assert repair('{"intents": [{').get("intents", []) == []
    assert repair('{"intents": [{"recipe": "hook"}, {"recipe":')["intents"] == [{"recipe": "hook"}]


def test_failure_lists_the_steps_tried_and_never_returns_a_non_object():
    with pytest.raises(JsonRepairFailed) as ei:
        repair("no json here at all")
    assert ei.value.attempts == [name for name, _ in jsonfix.REPAIR_STEPS]
    with pytest.raises(JsonRepairFailed):
        repair("[1, 2, 3]")          # a list is not a plan
    with pytest.raises(JsonRepairFailed):
        repair(None)  # type: ignore[arg-type]


def test_segmenter_treats_escaped_quotes_correctly():
    out = repair('{"reply": "he said \\"go\\"", "n": 1,}')
    assert out["reply"] == 'he said "go"'
