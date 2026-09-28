"""The words of CapCut's clip animations (wave E, F1), for the key-free
grammar. Built from the ONE table (`edl/clip_animations.py`). Kept apart
from `anim_expanders.py` so `grammar.py` can read the phrase row without
importing the expanders (which import the grammar).

A motion word is not always an animation — "zoom in on the second clip" is a
Ken Burns push (the `zoom` recipe), "fade in the second clip" a video fade,
"rotate it" a rotation, "make the colours pop" a look — so the row asks for
structure: the word "animation"/"animate"; "make <it / a clip / the sticker>
bounce / spin / rock / swing / shake / slide"; "<slide / spin / bounce> <a
clip> in / out"; "add a bounce / a shake / a slide-in to <…>"; and, for a
sticker or an overlay (which have no fade or Ken Burns of their own), "make
the sticker fade in", "zoom the overlay out".
"""
from __future__ import annotations

import re

from ...edl import clip_animations as A

#: Motion words that are an In or an Out (the kind decides which).
IN_OUT_WORDS: dict[str, tuple[str, str]] = {
    "zoom in": ("zoom_in", "zoom_in"), "zoom out": ("zoom_out", "zoom_out"),
    "slide left": ("slide_left", "slide_left"), "slide right": ("slide_right", "slide_right"),
    "slide up": ("slide_up", "slide_up"), "slide down": ("slide_down", "slide_down"),
    "fade": ("fade_in", "fade_out"), "fading": ("fade_in", "fade_out"),
    "rotate": ("rotate", "rotate"), "rotating": ("rotate", "rotate"), "rotation": ("rotate", "rotate"),
    "spin": ("spin", "spin"), "spinning": ("spin", "spin"), "twirl": ("spin", "spin"),
    "blur": ("blur_in", "blur_out"), "blurry": ("blur_in", "blur_out"), "focus": ("blur_in", "blur_out"),
    "bounce": ("bounce", "bounce"), "bouncing": ("bounce", "bounce"), "bouncy": ("bounce", "bounce"),
    "pop": ("bounce", "bounce"), "popping": ("bounce", "bounce"),
}
#: Motion words that loop (a Combo).
COMBO_WORDS: dict[str, str] = {
    "zoom in and out": "zoom_in_out", "zoom in out": "zoom_in_out", "zoom in-out": "zoom_in_out",
    "pulse": "zoom_in_out", "pulsing": "zoom_in_out", "pulsate": "zoom_in_out", "breathe": "zoom_in_out",
    "breathing": "zoom_in_out", "heartbeat": "zoom_in_out",
    "rock": "rock", "rocking": "rock", "rocks": "rock",
    "swing": "swing", "swinging": "swing", "sway": "swing", "swaying": "swing",
    "pendulum": "pendulum",
    "shake": "shake", "shaking": "shake", "shakes": "shake", "jiggle": "shake", "wobble": "shake",
    "wobbling": "shake", "vibrate": "shake", "jitter": "shake",
}
# every preset label reads as its own word too ("Blur In", "Zoom In-Out")
for _p in A.COMBO_PRESETS:
    COMBO_WORDS.setdefault(_p.label.lower().replace("-", " "), _p.id)
#: "slide" / "zoom" with no direction: the reader asks which way.
BARE_WORDS = ("slide", "sliding", "slides", "zoom")

_MOVE = r"(?:bounce|spin|twirl|rock|swing|sway|shake|jiggle|wobble|pulse|pulsate|breathe|slide)(?:s|es|ing|d)?"
_SOFT = r"(?:fade|zoom|rotate|blur|pop)(?:s|es|ing|d)?"
_NOUN = r"(?:clips?|videos?|shots?|stickers?|emojis?|overlays?|pips?|logos?|images?|pictures?|photos?)"
_OVR_NOUN = r"(?:stickers?|emojis?|overlays?|pips?|logos?|picture[- ]in[- ]picture)"
#: What an animation is put on.
TARGET = (rf"(?:it|this|that|them|everything"
          rf"|(?:the\s+|this\s+|that\s+|my\s+)?(?:[\w'-]+\s+){{0,2}}{_NOUN}\b(?:\s+(?:#\s*|number\s+)?\d{{1,2}}\b)?"
          rf"|{_NOUN}\s+(?:#\s*|number\s+)?(?:\d{{1,2}}|one|two|three|four|five|six|seven|eight|nine|ten)\b)")
#: A sticker or an overlay (they have no video fade and no Ken Burns).
OVR_TARGET = rf"(?:(?:the\s+)?(?:[\w'-]+\s+){{0,2}}{_OVR_NOUN}\b)"

#: The grammar row (EXACT) — checked in grammar.CUT_PRECEDENCE ahead of the
#: zoom / remove-feature rows, so "add a zoom in animation" is not a Ken Burns.
#: A NEW title / text that animates is the title recipe, not a clip
#: animation (review RE: "add an animated title saying hello" stopped adding
#: the title and asked which clip to animate).
_TEXT_NOUN = r"(?:titles?|text|captions?|subtitles?|lower[- ]?thirds?|words?|headlines?|labels?)"
ANIM_PHRASE = (
    r"^(?!.*\b(?:between|transitions?|cuts?|seams?)\b)"
    rf"(?!.*\banimated\s+(?:[\w'-]+\s+)?{_TEXT_NOUN}\b)"
    rf"(?!.*\b(?:add|create|make|put|write|insert|type)\s+(?:an?\s+|the\s+|some\s+)?(?:[\w'-]+\s+)?{_TEXT_NOUN}\b"
    r".*\b(?:that\s+)?animat)(?:"
    # "animation", "animate", "an entrance animation", "remove the animation"
    r".*\banimat(?:e|es|ed|ing|ions?)\b"
    # an imperative loop or motion on a clip (review RE: "shake the second
    # clip the whole time" ran Stabilise, the opposite edit)
    rf"|^(?:please\s+)?(?:shake|wobble|jiggle|rock|swing|sway|bounce|spin|twirl|pulse)\s+{TARGET}"
    # CapCut's In Zoom at the head / Out at the tail ("make the first clip zoom
    # in at the start" was a whole-clip Ken Burns push); a slow push stays zoom
    r"|^(?!.*\b(?:slow(?:ly)?|gradual(?:ly)?|ken[- ]?burns|push(?:es|ed|ing)?|over\s+the\s+(?:whole\s+)?(?:clip|shot))\b)"
    r".*\bzoom(?:s|ing)?[- ](?:in|out)\b.*\b(?:at|in)\s+the\s+(?:very\s+)?(?:start|beginning|end)\b"
    r"|^(?!.*\bslow)(?=.*\bzoom(?:s|ing)?[- ](?:in|out)\b).*\bas\s+it\s+(?:comes\s+in|enters|appears|ends|leaves|goes)\b"
    # "make the sticker bounce in", "make the second clip shake"
    rf"|.*\b(?:make|let|have|get)\s+{TARGET}\s+{_MOVE}\b"
    # "make the sticker fade in", "make the overlay zoom out"
    rf"|.*\b(?:make|let|have|get)\s+{OVR_TARGET}\s+{_SOFT}\b"
    # "slide the last clip out", "bounce the sticker in", "spin it in"
    rf"|.*\b(?:slide|spin|bounce|twirl|pop)\s+{TARGET}\s+(?:in|out|away|off|into\s+place|onto\s+(?:the\s+)?screen)\b"
    # "fade the sticker out", "zoom the overlay in"
    rf"|.*\b(?:fade|zoom|rotate|blur)\s+{OVR_TARGET}\s+(?:in|out|away)\b"
    # "add a bounce to the sticker", "put a slide-in on the first clip", "give it a shake"
    rf"|.*\b(?:add|put|apply|give|use)\s+(?:it\s+|them\s+|the\s+[\w ]{{0,20}}?(?:clip|sticker|overlay)\s+)?"
    rf"(?:a|an|the|some)?\s*(?:bounce|spin|twirl|rock|swing|sway|pendulum|shake|wobble|jiggle|pulse|heartbeat"
    rf"|slide(?:[- ](?:left|right|up|down))?|blur)(?:[- ](?:in|out))?\s*(?:effect|motion|loop|combo)?\s*(?:to|on|for)?\b"
    r"(?=\s+(?:it|this|that|them|the|my|clip|sticker|overlay)\b|\s*$)"
    # "a bounce-in", "slide out", "spin in" as a named motion
    r"|.*\b(?:bounce|spin|slide|twirl)[- ](?:in|out)\b"
    # the loop presets by name
    r"|.*\bpendulum\b|.*\bzoom[- ]in[- ](?:and[- ])?out\b"
    r")"
)

#: Taking animations off: "remove the animation", "no animation on the sticker".
ANIM_OFF = re.compile(
    r"\b(?:remove|delete|clear|take\s+off|take\s+away|turn\s+off|switch\s+off|get\s+rid\s+of|drop|kill|strip|reset)\b"
    r"(?=.*\banimat)|\bno\s+(?:more\s+)?animations?\b|\bstop\s+(?:it\s+|the\s+[\w ]{0,20}?\s+)?"
    r"(?:bouncing|spinning|rocking|swinging|shaking|animating|moving)\b|\bwithout\s+(?:the\s+)?animations?\b")


__all__ = ["IN_OUT_WORDS", "COMBO_WORDS", "BARE_WORDS", "TARGET", "OVR_TARGET", "ANIM_PHRASE", "ANIM_OFF"]
