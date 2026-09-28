"""Final QA round 3: an on-device draft is grounded to what the prompt SAYS.

* "make the whole video black and white" reached Apple Intelligence, which
  answered noise_reduce + a loudness target — every clip's sound was
  replaced by a denoised copy and the reply said "Clean audio: done". A
  prompt that names a colour look (and no sound) keeps no audio recipe.
* "add a wipe between clip 1 and clip 2" came back as two Cross Dissolves:
  the transition TYPE the prompt names wins over the model's.
* Only uploaded pictures are "my photo": a sticker's emoji artwork is not.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402

from video_ai_editor.agent.prompt.brains.content import ground_to_prompt  # noqa: E402
from video_ai_editor.agent.prompt.schema import IntentDraft  # noqa: E402


def _recipes(d: IntentDraft) -> list[str]:
    return [it.recipe for it in d.intents]


def test_a_colour_look_prompt_keeps_no_audio_cleanup_draft():
    draft = IntentDraft(intents=[{"recipe": "clean_audio", "slots": {"clip_ref": "$v1_all", "strength": 0.85}},
                                 {"recipe": "loudness", "slots": {"lufs": -16}}], confidence=0.8, reply="")
    got = ground_to_prompt(draft, "make the whole video black and white")
    assert _recipes(got) == [], _recipes(got)
    # a prompt that DOES name the sound keeps it
    kept = ground_to_prompt(draft, "clean up the audio and make it black and white")
    assert "clean_audio" in _recipes(kept)


def test_the_named_transition_type_wins_over_the_models():
    draft = IntentDraft(intents=[{"recipe": "transitions", "slots": {"type": "crossdissolve", "look": "smooth"}}],
                        confidence=0.8, reply="")
    got = ground_to_prompt(draft, "add a wipe between clip 1 and clip 2")
    (it,) = got.intents
    assert it.slots.get("type") == "wiperight" and not it.slots.get("look"), it.slots


def test_sticker_artwork_is_not_a_picture_for_the_canvas(tmp_path):
    from PIL import Image
    from video_ai_editor.agent.prompt.facts import build_facts
    store = F.make_store(tmp_path)
    art = Path(store.dir) / "uploads" / "stickers" / "1f525.png"
    art.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (32, 32), (255, 80, 0, 255)).save(art)
    facts = build_facts(store, None)
    assert facts.uploads_images == []
    assert any(p.endswith("1f525.png") for p in facts.allowed_paths)   # an explicit name still resolves
