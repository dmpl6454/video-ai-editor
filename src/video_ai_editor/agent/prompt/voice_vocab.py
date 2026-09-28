"""The words of CapCut's voice changer (wave E, F3), for the key-free grammar.

Built from the ONE table (`edl/voice_effects.py`): every preset's id, label
and aliases. Kept apart from `voice_expanders.py` so `grammar.py` can read
the phrase row without importing the expanders (which import the grammar).

A bare effect word is not always a voice effect ("turn the music lower",
"a deep blue look", "the phone shot"), so the row asks for structure: an
effect word next to "voice / effect / filter", "sound(s) like …", "add
echo / reverb …", "make my voice deeper", "voice effect / changer", or a
removal of one of those. The unambiguous words (echo, reverb, chipmunk,
robot, megaphone, vibrato, underwater …) also count after add / put / apply.
"""
from __future__ import annotations

import re

from ...edl import voice_effects as VFX


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("-", " ").replace("_", " ").split())


#: word → preset id, every spelling the table knows.
WORD_TO_ID: dict[str, str] = {}
for _p in VFX.PRESETS:
    for _w in (_p.id, _p.label, *_p.aliases):
        WORD_TO_ID.setdefault(_norm(_w), _p.id)
for _w in ("low", "lower"):
    WORD_TO_ID.pop(_w, None)
#: Comparatives editors use for the two pitch directions.
WORD_TO_ID.update({"higher": "chipmunk", "squeakier": "chipmunk", "higher pitched": "chipmunk",
                   "deeper": "deep", "lower pitched": "deep", "robotic": "robot",
                   "echoing": "echo", "echoy": "echo", "reverby": "reverb", "wobbly": "vibrato"})

#: Words too common to mean a voice effect on their own (they need "voice",
#: "sound like" or "effect" around them).
AMBIGUOUS: frozenset[str] = frozenset({
    "low", "lower", "deeper", "higher", "phone", "delay", "hall", "church", "broadcast", "cartoon", "machine",
    "wobble", "warble", "muffled", "muffle", "trembling", "tremble", "shaky voice", "wavering", "evil",
    "beast", "villain", "ogre", "demon", "roomy", "big room", "high pitched", "lower pitched", "squeaky",
    "higher pitched", "baritone", "bass voice", "old radio", "vintage radio", "radio", "wobbly", "deep",
    "cell phone", "landline", "phone call", "droid", "android", "cyborg", "concert hall", "canyon",
})

_ALL_WORDS = sorted(WORD_TO_ID, key=len, reverse=True)
EFFECT_WORD = "(?:" + "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") for w in _ALL_WORDS) + ")"
STRONG_WORD = "(?:" + "|".join(re.escape(w).replace(r"\ ", r"[\s-]+") for w in _ALL_WORDS
                                if w not in AMBIGUOUS) + ")"

_VOICE = r"(?:voice|vocals?|narration|voice[- ]?over|speech|dialogue|audio|sound)"
_WHO = r"(?:my|the|his|her|their|our|this|that|your|a|an)"

#: Taking an effect off: "remove the voice effect", "turn off the echo",
#: "voice back to normal". grammar.CUT_PRECEDENCE reads it BEFORE the generic
#: "remove the ___ effect" row (remove_feature: video effects and looks).
VOICE_OFF_PHRASE = (
    rf"\b(?:remove|delete|turn\s+off|switch\s+off|take\s+off|take\s+out|get\s+rid\s+of|drop|clear|disable|kill|undo|lose)\s+"
    rf"(?:the\s+|my\s+|that\s+|this\s+|all\s+(?:the\s+)?)?(?:{STRONG_WORD}\s+)?(?:voice[- ]?(?:effects?|changer|filters?)|{STRONG_WORD})\b"
    r"|\b(?:normal|natural|original|regular)\s+voice\s+(?:again|back)\b|\bvoice\s+back\s+to\s+normal\b"
    r"|\bno\s+(?:more\s+)?voice\s+effects?\b"
    # Final QA: the particle AFTER the object — "turn the robot voice off",
    # "switch the voice changer off" — used to fall through to the ADD row
    # and turn Robot ON while replying "done". A named effect, or the voice
    # effect/changer/filter noun, then `off`/`out` ("turn the voice off"
    # alone stays a mute).
    rf"|\b(?:turn|switch|take|shut|knock)\s+(?:the\s+|my\s+|that\s+|this\s+|all\s+(?:the\s+)?)?"
    rf"(?:{STRONG_WORD}(?:\s+(?:voice|sound))?(?:\s+(?:effects?|filters?|changer))?|voice[- ]?(?:effects?|changer|filters?))"
    r"\s+(?:off|out)\b"
)

#: The grammar row (EXACT).
VOICE_PHRASE = (
    # "voice effect", "voice changer", "voice filter"
    r"\bvoice[- ]?(?:effects?|changer|changing|filters?|fx|mod(?:ifier|ulation)?)\b"
    r"|\bchange\s+(?:my|the|his|her|their)\s+voice\b"
    # "sound like a robot", "sounds like an old radio", "sound underwater"
    rf"|\bsound(?:s|ing)?\s+(?:like|as\s+if)\s+(?:(?:it'?s|its|i'?m|we'?re|they'?re|you'?re|he'?s|she'?s)\s+)?"
    r"(?:(?:on|in|from|through|under)\s+)?"
    rf"(?:a\s+|an\s+|the\s+|some\s+)?(?:old\s+|little\s+|big\s+|scary\s+|giant\s+)?{EFFECT_WORD}\b"
    rf"|\bsound(?:s|ing)?\s+{STRONG_WORD}\b"
    # "robot voice", "echo effect", "telephone filter", "deep voice"
    rf"|\b{EFFECT_WORD}[\s-]+(?:voice|vocals?|effect|filter|fx|sound)\b"
    # "make my voice deeper / sound like a chipmunk / robotic"
    rf"|\bmake\s+{_WHO}?\s*{_VOICE}\s+(?:sound\s+)?(?:a\s+(?:bit|little|lot)\s+|much\s+|way\s+)?"
    rf"(?:like\s+(?:a\s+|an\s+)?)?{EFFECT_WORD}\b"
    # "make the voice on clip 1 deeper", "make the voice in the second clip
    # robotic" (review RE: planned a NEW voice-over and a 60 MB download)
    rf"|\bmake\s+{_WHO}?\s*{_VOICE}\s+(?:on|of|in|for|from)\s+(?:[\w'#-]+\s+){{1,3}}?(?:sound\s+)?"
    rf"(?:a\s+(?:bit|little|lot)\s+|much\s+|way\s+)?(?:like\s+(?:a\s+|an\s+)?)?{EFFECT_WORD}\b"
    # "add echo to the voiceover", "put reverb on it", "apply a chipmunk"
    rf"|\b(?:add|put|apply|use|give\s+(?:it|me|the\s+\w+)|slap\s+on|throw\s+on|with)\s+"
    rf"(?:an?\s+|some\s+|the\s+|a\s+(?:bit|little|touch)\s+of\s+|lots\s+of\s+|more\s+)?{STRONG_WORD}\b"
    # "robotise my voice", "chipmunk it"
    r"|\brobot(?:i[sz]e|ify)\b|\bchipmunk\s+(?:it|this|me|my voice)\b"
    # "reverb on the music", "a little echo on the voiceover"
    rf"|\b{STRONG_WORD}\s+(?:on|to|onto|over|for)\s+(?:the\s+|my\s+|this\s+|that\s+|it\b|all\b|every\b|clip\b)"
    # removal (also its own row, read before the generic "remove the ___ effect")
    "|" + VOICE_OFF_PHRASE
)

__all__ = ["WORD_TO_ID", "AMBIGUOUS", "EFFECT_WORD", "STRONG_WORD", "VOICE_OFF_PHRASE", "VOICE_PHRASE"]
