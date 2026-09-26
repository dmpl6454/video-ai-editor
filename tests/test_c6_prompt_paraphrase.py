"""QA-018 (wave C remainder): paraphrases the grammar cannot read, grounded.

Wave B left the on-device model's honest-but-wrong readings in place: "the
soundtrack needs to breathe less loudly" planned ducking, "can the tune sit
under me more" planned add-music (a no-op: "music is already on the
timeline"). A live pass of 20 paraphrases through the real Apple Intelligence
helper on this Mac (macOS 27) found more of the same: add-music for "too
timid" / "barely audible" / "more presence", remove-music (rejected as an
ungrounded delete → "did you mean") for "the backing track is overpowering
me", a PICTURE fade for "let the track ease in at the beginning", and three
readings the grammar itself got wrong (loudness target, add-music, noise
reduction). Before: 8/20 correct; after: 20/20 (the pass script and both
JSON records live in the lane scratch dir).

Every FM answer below is the helper's real output, replayed through the real
FMBrain → grounding → recipes → validate_plan path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from video_ai_editor.agent.prompt import grammar as G  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import recipes  # noqa: E402
from video_ai_editor.agent.prompt.brains import content  # noqa: E402
from video_ai_editor.agent.prompt.brains.base import BrainRequest  # noqa: E402
from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402

BED = TimelineFacts.minimal(has_music=True, music_ducked=False, music_clip_ids=["m_1"],
                            music_gain_db=-14.0, has_transcript=True, v1_clip_ids=["c_a"],
                            clip_ids=["c_a", "m_1"], track_ids=["v1", "music"])


def _fm_brain(drafts: list[dict]):
    import json
    from video_ai_editor.agent.prompt.brains import fm
    probe = {"ok": True, "available": True, "state": "available", "fix": None, "os": "27.0.0",
             "languages": ["en"]}
    queue = [{"ok": True, "model": "apple-fm", "latency_ms": 900,
              "draft": {"intents": d, "exclusions": [], "needs_input": [], "confidence": 0.85,
                        "reply": "ok"}} for d in drafts]

    def runner(argv, stdin, timeout_s):
        if argv[-1] == "probe":
            return 0, json.dumps(probe).encode(), b""
        return 0, json.dumps(queue.pop(0)).encode(), b""

    return fm.FMBrain(runner=runner, helper=sys.executable, darwin=True, macos=(27, 0), frozen=False)


def _fm_steps(prompt: str, intents: list[dict], facts: TimelineFacts = BED) -> list[tuple[str, dict]]:
    res = _fm_brain([intents]).plan(BrainRequest(prompt=prompt, facts=facts, recipes=recipes.cards()),
                                    timeout_s=8)
    assert res.ok, res.reason
    return [(s.tool, dict(s.args)) for s in res.plan.steps]


#: (prompt, the helper's real intents, expected set_volume db on a -14 dB bed)
RECORDED = [
    # wave B's recorded misread (duck with no speaker named)
    ("the soundtrack needs to breathe less loudly", [{"recipe": "duck"}], -20.0),
    ("can the tune sit under me more", [{"recipe": "music"}], -20.0),
    ("the backing track is overpowering me", [{"recipe": "remove_music"}], -20.0),
    ("give the music a little more presence", [{"enabled": False, "mood": "more presence", "recipe": "music"}], -8.0),
    ("the tune should be barely audible", [{"db": -20, "recipe": "music"}], -20.0),
    ("the song feels too timid", [{"enabled": False, "mood": "energetic", "recipe": "music"}], -8.0),
    ("the music drowns everything, tame it",
     [{"change": "down", "db": -10, "enabled": False, "recipe": "volume"}], -20.0),
]


@pytest.mark.parametrize("prompt,intents,db", RECORDED, ids=[r[0] for r in RECORDED])
def test_recorded_level_misreads_become_a_level_change_on_the_bed(prompt, intents, db):
    steps = _fm_steps(prompt, intents)
    assert steps == [("set_volume", {"target": "music", "db": db})], steps


def test_a_track_easing_in_fades_the_bed_not_the_picture():
    steps = _fm_steps("let the track ease in at the beginning",
                      [{"duration_s": 16, "edge": "in", "recipe": "fade"}])
    tools = [t for t, _ in steps]
    assert "set_video_fade" not in tools and "fit_music_to_video" in tools, steps
    assert next(a for t, a in steps if t == "fit_music_to_video").get("fade_in")


def test_ducking_with_a_speaker_named_stays_ducking():
    steps = _fm_steps("could the song dip whenever I talk", [{"recipe": "duck"}])
    assert [t for t, _ in steps] == ["set_duck"], steps


def test_asking_for_another_song_is_still_a_new_bed():
    assert content.ground_music_level(
        _draft([{"recipe": "music"}]), "add a different song that sits lower", BED).intents[0].recipe == "music"


def test_no_bed_on_the_timeline_means_no_level_rewrite():
    empty = TimelineFacts.minimal(has_music=False)
    d = _draft([{"recipe": "music"}])
    assert content.ground_music_level(d, "the tune should be barely audible", empty) is d


def test_a_removal_that_says_remove_stays_a_removal():
    d = _draft([{"recipe": "remove_music"}])
    assert content.ground_music_level(d, "remove the song, it is too loud", BED) is d


@pytest.mark.parametrize("prompt,direction", [
    ("the music hits too softly, make it hit harder", "up"),
    ("the backing track is overpowering me", "down"),
    ("the song feels too timid", "up"),
    ("can the tune sit under me more", "down"),
    ("the tune", None),
])
def test_level_direction(prompt, direction):
    assert content.music_level_direction(prompt) == direction


@pytest.mark.parametrize("prompt,intent,tool,args", [
    ("let the song swell louder", "volume", "set_volume", {"target": "music", "db": -8.0}),
    ("I want the beat pushed further back in the mix", "volume", "set_volume", {"target": "music", "db": -20.0}),
    ("kill the audio from the camera", "mute", "set_clip_muted", {"clip_id": "$v1_all", "muted": True}),
])
def test_grammar_reads_the_live_pass_paraphrases(prompt, intent, tool, args):
    d = G.detect(prompt)
    assert d.intents == [intent] and d.confidence >= G.RUN_THRESHOLD, (d.intents, d.confidence)
    p = P.plan(prompt, BED)
    assert any(s.tool == tool and all(s.args.get(k) == v for k, v in args.items()) for s in p.steps), \
        [(s.tool, s.args) for s in p.steps]


def test_music_outliving_the_video_reads_as_fit_music():
    assert G.detect("the music outlives the video").intents == ["fit_music"]


def _draft(intents: list[dict]):
    from video_ai_editor.agent.prompt.schema import IntentDraft, IntentItem
    return IntentDraft(intents=[IntentItem(recipe=i.pop("recipe"), slots=i) for i in intents],
                       confidence=0.85, reply="")


# ------------------------------------------------ nonsense never runs a plan

def _route_fm(prompt: str, intents: list[dict]):
    """The real ladder: recipes first, then the real FMBrain replaying a
    recorded helper draft; the router's default validation."""
    from video_ai_editor.agent.prompt.brains import router
    from video_ai_editor.agent.prompt.brains.recipes_brain import RecipesBrain
    fm_brain = _fm_brain([intents])
    spawned = []
    real_runner = fm_brain._runner

    def counting(argv, stdin, timeout_s):
        spawned.append(argv[-1])
        return real_runner(argv, stdin, timeout_s)

    fm_brain._runner = counting
    req = BrainRequest(prompt=prompt, facts=BED, recipes=recipes.cards())
    routed = router.plan(req, order=("recipes", "apple_intelligence"),
                         brains={"recipes": RecipesBrain(), "apple_intelligence": fm_brain},
                         prefetch=False)
    return routed, spawned


@pytest.mark.parametrize("prompt", ["banana wobble zebra", "purple monkey dishwasher"])
def test_nonsense_gets_did_you_mean_even_when_the_model_plans_captions(prompt):
    """REGRESSION (wave C review): with a music bed on the timeline, Apple
    Intelligence answered "banana wobble zebra" with captions (transcribe +
    add_caption_track), and the draft ran unconfirmed — 35 s of Whisper and
    a caption track nobody asked for. ground_to_prompt keeps a draft whose
    recipes are ALL unnamed (that is what a paraphrase looks like), so a
    prompt with no editing words at all must not reach the model."""
    routed, spawned = _route_fm(prompt, [{"recipe": "captions"}])
    assert routed.plan is None, [(s.tool, s.args) for s in routed.plan.steps]
    assert routed.clarify is not None and routed.clarify.key == "intent"
    assert len(routed.clarify.options or []) >= 2
    fm = [a for a in routed.attempts if a.brain == "apple_intelligence"]
    assert fm and "ungrounded" in fm[-1].reason, fm
    assert "plan" not in spawned                     # not even a model call


@pytest.mark.parametrize("prompt,intents,tool", [
    ("let the ending melt to black", [{"edge": "out", "recipe": "fade"}], "set_video_fade"),
    ("slap LAUNCH DAY across the top", [{"recipe": "title", "text": "LAUNCH DAY"}], None),
    ("I want the backing track to sit lower", [{"recipe": "music"}], "set_volume"),
])
def test_paraphrases_the_grammar_cannot_read_still_reach_the_model(prompt, intents, tool):
    """The other side of the gate: these share nonsense's zero grammar
    reading (no intent phrase matches, recipes confidence 0.00) but speak
    about the edit — they are what the on-device model is for."""
    routed, spawned = _route_fm(prompt, intents)
    assert "plan" in spawned
    assert routed.brain == "apple_intelligence" and routed.plan is not None, routed.attempts
    if tool:
        assert tool in [s.tool for s in routed.plan.steps], [(s.tool, s.args) for s in routed.plan.steps]
