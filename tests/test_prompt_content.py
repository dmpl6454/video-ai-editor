"""Content tasks (spec §3.7): hook text and window ranking come from a brain
when one can answer and from the SPEAKER'S OWN WORDS when none can — never
from the canned clickbait the baseline caught (findings 3 and 9)."""
from __future__ import annotations

from video_ai_editor.agent.prompt.brains import content
from video_ai_editor.agent.prompt.brains.base import TextResult, available, unavailable

CAMERA = ("Um, so today I want to show you three things about this camera that nobody tells you. "
          "First, the autofocus hunts in low light. Second, uh, the battery lasts about ninety minutes. "
          "Thanks for watching and subscribe.")

CANNED = ("THEY LIED", "DON'T SCROLL", "EVERYONE'S MISSING", "WAIT FOR IT", "COSTS MOST PEOPLE")


class FakeBrain:
    def __init__(self, bid: str, items=None, *, avail=True, raise_=False):
        self.id = bid
        self._items = items
        self._avail = avail
        self._raise = raise_
        self.calls = 0

    def availability(self):
        return available("fake") if self._avail else unavailable("off", "turn it on")

    def plan(self, req, *, timeout_s):
        raise AssertionError("content tasks never plan")

    def text(self, task, *, timeout_s):
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        if self._items is None:
            return None
        return TextResult(items=self._items, brain=self.id, model="fake")


def test_heuristic_hook_comes_from_the_transcript_not_a_template():
    items = content.heuristic_hook_candidates(CAMERA)
    assert items, "a transcript with claims must yield candidates"
    top = items[0]
    assert top == top.upper()
    assert len(top.split()) <= content.HOOK_MAX_WORDS and len(top) <= content.HOOK_MAX_CHARS
    assert "THREE THINGS" in top or "NOBODY TELLS" in top
    assert not top.startswith(("UM", "SO", "TODAY"))
    assert all(c not in line for line in items for c in CANNED)
    assert not any("SUBSCRIBE" in line for line in items)


def test_heuristic_is_empty_on_an_empty_transcript_and_hook_text_still_answers():
    assert content.heuristic_hook_candidates("") == []
    ht = content.hook_text([], "")
    assert ht.text and ht.brain == "recipes" and ht.content_brain is None
    assert ht.source == f"hook text: {content.HEURISTIC_SOURCE}"


def test_sanitize_hook_items_is_strict_about_shape():
    raw = [None, 42, "1. STOP DOING THIS WITH YOUR CAMERA RIGHT NOW PLEASE", "  stop doing this with your camera right now please ",
           "'short one'", "x" * 90, "ok"]
    out = content.sanitize_hook_items(raw)
    assert out[0] == "STOP DOING THIS WITH YOUR CAMERA RIGHT"          # numbering stripped, 7 words
    assert len(out) == 3                                             # dedupe (case-insensitive), "ok" too short
    assert all(len(s) <= content.HOOK_MAX_CHARS for s in out)
    assert "short one" in out
    # Real 7B line (2026-09-10): the transcript's "Second, …" connector is not a hook.
    assert content.sanitize_hook_items(["Second, the battery only lasts ninety minutes", "First: autofocus hunts"]) == [
        "the battery only lasts ninety minutes", "autofocus hunts"]


def test_sanitize_ranking_keeps_in_range_unique_ints_up_to_k():
    assert content.sanitize_ranking([2, "0", 2, 9, True, 1.0, None], n_windows=4, k=2) == [2, 0]
    assert content.sanitize_ranking([], n_windows=4, k=2) == []


def test_hook_text_prefers_the_first_brain_that_answers_and_records_it():
    fm = FakeBrain("apple_intelligence", avail=False)
    mlx = FakeBrain("local_model", ["Nobody tells you this about autofocus", "Another line"])
    ht = content.hook_text([fm, mlx], CAMERA)
    assert fm.calls == 0 and mlx.calls == 1
    assert ht.text == "Nobody tells you this about autofocus"
    assert ht.brain == "local_model" and ht.content_brain == "local_model"
    assert ht.source == "hook text: Local model"
    assert ht.candidates == ("Nobody tells you this about autofocus", "Another line")


def test_hook_text_falls_back_to_the_transcript_when_brains_fail_or_answer_nothing():
    ht = content.hook_text([FakeBrain("local_model", raise_=True), FakeBrain("claude", []),
                            FakeBrain("apple_intelligence", [None, 7])], CAMERA)
    assert ht.brain == "recipes" and ht.content_brain is None
    assert "heuristic" in ht.source
    assert "THREE THINGS" in ht.text or "NOBODY TELLS" in ht.text


def test_rank_windows_uses_the_brain_or_the_windows_own_order():
    windows = [{"start": 0, "end": 10, "text": "a"}, {"start": 10, "end": 20, "text": "b"},
               {"start": 20, "end": 30, "text": "c"}]
    good = content.rank_windows([FakeBrain("local_model", [2, 0, 2, 9])], windows, 2)
    assert good.order == (2, 0) and good.brain == "local_model"
    fallback = content.rank_windows([FakeBrain("local_model", raise_=True)], windows, 2)
    assert fallback.order == (0, 1) and fallback.brain == "recipes"
    assert content.rank_windows([], [], 3).order == ()


def test_needs_hook_text_only_for_the_transcript_heuristic_line():
    from video_ai_editor.agent.prompt import recipes, schema
    from video_ai_editor.agent.prompt.facts import TimelineFacts

    facts = TimelineFacts.minimal(has_transcript=True, words=30, transcript_head=CAMERA)
    guess = recipes.heuristic_hook(CAMERA)
    assert guess

    def plan_with(text, **fields):
        step = schema.Step(tool="apply_hook_stack", args={"text": text, "duration": 3.0}, why="hook")
        return schema.Plan.new(intent="hook", brain="recipes", steps=[step], **fields)

    assert content.needs_hook_text(plan_with(guess), facts)
    assert not content.needs_hook_text(plan_with("hello world"), facts), "the user's own words stay"
    assert not content.needs_hook_text(plan_with(guess, content_brain="local_model"), facts), "already written"
    assert not content.needs_hook_text(plan_with(guess), facts.with_(transcript_head="")), "nothing to ground in"
    captions = schema.Plan.new(intent="captions", brain="recipes",
                               steps=[schema.Step(tool="add_caption_track", args={}, why="c")])
    assert not content.needs_hook_text(captions, facts)


def test_tasks_are_path_free_and_bounded():
    task = content.hook_candidates_task("x" * 5000, n=3)
    assert task.kind == "hook_candidates" and len(task.payload["transcript_head"]) == 1500
    rank = content.rank_windows_task([{"start": "1", "end": 2, "text": "t" * 500, "src": "/tmp/x.mp4"}], 1)
    assert rank.payload["windows"] == [{"start": 1.0, "end": 2.0, "text": "t" * 300}]
    assert "src" not in rank.payload["windows"][0]


def test_model_written_hook_text_is_stripped_but_the_users_own_words_stay():
    """Real 7B run (2026-09-10): asked for "a hook at the start" the model
    filled `text` with "Get ready for an amazing story!" — generic, because
    the planning prompt has no transcript words. That slot goes; a line the
    user actually typed (quoted or not) stays."""
    from video_ai_editor.agent.prompt.schema import IntentDraft

    draft = IntentDraft.model_validate({
        "intents": [{"recipe": "hook", "slots": {"text": "Get ready for an amazing story!", "duration_s": 3}},
                    {"recipe": "music", "slots": {"mood": "chill"}}],
        "confidence": 1.0})
    out = content.strip_model_hook_text(draft, "add a hook at the start and some chill music, no captions")
    assert out.intents[0].slots == {"duration_s": 3} and out.intents[1] == draft.intents[1]
    assert draft.intents[0].slots["text"] == "Get ready for an amazing story!", "the input is not mutated"
    quoted = IntentDraft.model_validate({"intents": [{"recipe": "hook", "slots": {"text": "Nobody tells you this"}}],
                                         "confidence": 1.0})
    kept = content.strip_model_hook_text(quoted, 'add a hook that says "nobody tells you this"')
    assert kept is quoted
    kept2 = content.strip_model_hook_text(quoted, "hook: Nobody   tells you this")
    assert kept2.intents[0].slots["text"] == "Nobody tells you this"
    other = IntentDraft.model_validate({"intents": [{"recipe": "captions", "slots": {"text": "x"}}], "confidence": 1.0})
    assert content.strip_model_hook_text(other, "add captions") is other, "only the hook recipe's text is judged"


def test_parse_text_items_recovers_mis_bracketed_answers():
    # Real 7B answer to the first hook prompt: each line in its own list.
    raw = '{"items": ["Autofocus hunts in low light"], ["Battery lasts just ninety minutes"], ["Three hidden flaws"]}'
    assert content.parse_text_items(raw, "hook_candidates") == [
        "Autofocus hunts in low light", "Battery lasts just ninety minutes", "Three hidden flaws"]
    assert content.parse_text_items('```json\n{"items": ["A line", "B line"]}\n```', "hook_candidates") == ["A line", "B line"]
    assert content.parse_text_items('{"items": [0, 2]}', "rank_windows") == [0, 2]
    assert content.parse_text_items("best windows: [2, 0, 1] because", "rank_windows") == [2, 0, 1]
    assert content.parse_text_items("no quotes here", "hook_candidates") is None
    assert content.parse_text_items('{"items": "not a list"}', "rank_windows") is None
    assert content.parse_text_items("", "other") is None


def test_text_task_prompts_carry_a_worked_example_and_no_paths():
    task = content.hook_candidates_task(CAMERA, n=2, max_words=6)
    system, user = content.text_task_prompts(task)
    assert '{"items": ["hook line 1", "hook line 2"]}' in system and "6 words" in system
    assert CAMERA[:40] in user and "/Users" not in system + user
    rank = content.rank_windows_task([{"start": 0, "end": 5, "text": "alpha", "src": "/tmp/x"}, {"text": "beta"}], 1)
    system, user = content.text_task_prompts(rank)
    assert "[0] alpha" in user and "[1] beta" in user and "/tmp" not in user and "1 best" in system
    assert content.text_task_prompts(content.hook_candidates_task("")) is None
    assert content.text_task_prompts(content.rank_windows_task([], 3)) is None
    assert content.text_task_prompts(content.TextTask(kind="rank_windows", payload={})) is None
