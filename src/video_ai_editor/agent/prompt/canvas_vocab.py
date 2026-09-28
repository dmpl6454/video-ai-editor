"""The words of the CapCut Canvas and blend-mode prompts (wave E, lane F2):
the grammar's phrase rows and the blend-mode word list, built from the ONE
table `edl/canvas_blend.py`. Separate from `canvas_expanders` so `grammar`
can import it without a cycle."""
from __future__ import annotations

import re

from ...edl import canvas_blend as CB

# --------------------------------------------------------------------------
# 1. Vocabulary (grammar.py reads these)
# --------------------------------------------------------------------------

#: Every word a blend mode goes by, longest first (so "linear burn" beats
#: "burn", "color dodge" beats "dodge").
def _blend_words() -> list[str]:
    words = set()
    for b in CB.BLENDS:
        if b.id == "normal":
            words.update(("normal",))
            continue
        words.update((b.id.replace("_", " "), b.label.lower(), b.css.replace("-", " "), *b.aliases))
    words.update(("colour burn", "colour dodge", "soft-light", "hard-light", "color-burn", "color-dodge",
                  "linear-burn", "linear-dodge"))
    # comparatives and fillers read as something else in a sentence ("make
    # the overlay darker" is a brightness request, "none" a negation)
    words -= {"darker", "lighter", "none", "default", "regular", "no blend", "exclude", "diff"}
    return sorted(words, key=len, reverse=True)


BLEND_WORDS = r"(?:" + "|".join(re.escape(w) for w in _blend_words()) + r")"
#: What an overlay clip is called in a prompt.
OVERLAY_NOUN = (r"(?:overlay(?:\s+clip|\s+video|\s+layer)?|pip|picture[- ]in[- ]picture|top\s+(?:clip|layer|video)"
                r"|upper\s+(?:clip|layer|video)|second\s+layer|layer\s+(?:on\s+top|above)|clip\s+on\s+top"
                r"|video\s+on\s+top|overlaid\s+(?:clip|video))")
#: The letterbox: what a canvas background fills.
BG_NOUN = r"(?:back\s*ground|bg|canvas|black\s+bars|bars|letter\s*box(?:ing)?|borders?|empty\s+(?:space|area)|sides)"
COLOUR_WORDS = r"(?:" + "|".join(re.escape(w) for w in sorted(CB.color_names(), key=len, reverse=True)) + r")"
HEX = r"#?[0-9a-f]{6}\b"
PICTURE = r"(?:image|picture|photo|pic|photograph|wallpaper)"

#: grammar PHRASES rows (EXACT).
CANVAS_PHRASE = (
    rf"\bblur(?:red|ry|s)?\s+(?:out\s+)?(?:the\s+|my\s+)?{BG_NOUN}\b(?!\s+(?:music|noise|sound|audio))"
    rf"|\b(?:blurred|blurry|colou?red|solid(?:\s+colou?r)?)\s+{BG_NOUN}\b"
    rf"|\b(?:blur|colou?r)\s+(?:on|in|for|behind|to)\s+(?:the\s+|my\s+)?{BG_NOUN}\b"
    rf"|\bblur\s+behind\s+(?:the\s+|my\s+)?(?:video|clip|picture|footage|shot)\b"
    rf"|\b(?:change|edit|pick|choose)\s+(?:the\s+|my\s+)?(?:canvas|back\s*ground)(?:\s+colou?r)?$"
    rf"|\b{BG_NOUN}\s+(?:blur(?:red)?|colou?r)\b(?!\s+(?:grade|grading|correct))"
    rf"|\b(?:make|turn|set|change|switch|fill|paint|colou?r|use)\s+(?:the\s+|my\s+)?{BG_NOUN}\s+"
    rf"(?:to\s+|with\s+|in\s+|into\s+)?(?:a\s+|an\s+)?(?:solid\s+)?"
    rf"(?:{HEX}|{COLOUR_WORDS}\b|blur(?:red|ry)?\b|colou?r\b|{PICTURE}\b|this\s+{PICTURE}|(?:my|the)\s+{PICTURE})"
    rf"|\b(?:fill|replace)\s+(?:the\s+)?(?:black\s+)?(?:bars|letter\s*box(?:ing)?|borders?|empty\s+(?:space|area))\s+with\b"
    rf"|\b(?:use|put|set|make)\s+(?:this|that|the|my|an?)\s+(?:[\w.-]+\s+)?{PICTURE}\s+(?:as|for|in|behind)\s+(?:the\s+)?(?:back\s*ground|bg|canvas)\b"
    rf"|\b(?:use|put|set|make)\s+(?:the\s+)?\S+\.(?:png|jpe?g|webp|heic|heif|bmp|tiff?)\s+(?:as|for)\s+(?:the\s+)?(?:back\s*ground|bg|canvas)\b"
    rf"|\bcanvas\s+(?:blur|colou?r|{PICTURE}|background)\b"
    rf"|\b(?:reset|clear)\s+(?:the\s+)?(?:canvas|back\s*ground\s+(?:blur|colou?r|{PICTURE}))\b"
    rf"|\bback\s+to\s+black\s+bars\b|\bno\s+(?:more\s+)?(?:canvas|back\s*ground)\s+(?:blur|colou?r|{PICTURE})\b"
    # review RE: "put a red background behind the video", "blue canvas behind
    # the first clip" made no edit
    rf"|\b(?:put|add|use|give(?:\s+(?:it|me|them))?|want|need)\s+(?:a\s+|an\s+|some\s+)?(?:solid\s+|plain\s+)?"
    rf"(?:{HEX}|{COLOUR_WORDS}\b|blur(?:red|ry)?\b)\s*(?:colou?r(?:ed)?\s+)?(?:back\s*ground|bg|canvas|backdrop)\b"
    rf"|\b(?:{HEX}|{COLOUR_WORDS})\s+(?:colou?r(?:ed)?\s+)?(?:canvas|back\s*ground|backdrop|bg)\s+"
    r"(?:behind|for|on|under|to|in)\b"
)
#: Taking a blend OFF (Final QA r3): whatever mode it names, the result is Normal.
BLEND_OFF = (r"\b(?:remove|delete|clear|reset|drop|undo|kill|disable|take\s+(?:off|out|away)|turn\s+off"
             r"|switch\s+off|get\s+rid\s+of)\s+(?:the\s+|its\s+|that\s+|this\s+|any\s+)?(?:[\w-]+\s+)?"
             r"blend(?:ing)?(?:[- ]?mode)?s?\b"
             r"|\bno\s+(?:more\s+)?blend(?:ing)?(?:[- ]?mode)?\b|\bwithout\s+(?:the\s+|a\s+)?blend(?:ing)?\b"
             r"|\bblend(?:ing)?(?:[- ]?mode)?\s+(?:back\s+)?(?:to\s+)?(?:off|normal)\b")
BLEND_PHRASE = (
    rf"\b(?:set|make|change|switch|turn|put)\s+(?:the\s+|this\s+|that\s+|my\s+)?{OVERLAY_NOUN}\s+(?:back\s+)?(?:to|into|on|as|in)\s+"
    rf"(?:a\s+|the\s+)?{BLEND_WORDS}\b"
    rf"|\b{BLEND_WORDS}\s+blend(?:ed|ing|s)?\b"
    rf"|\b(?:make|turn|set)\s+(?:the\s+|this\s+|that\s+|my\s+)?{OVERLAY_NOUN}\s+{BLEND_WORDS}\b"
    rf"|\bblend(?:ing)?[- ]mode\b"
    rf"|\bblend(?:ed|ing|s)?\s+(?:it|this|that|the\s+{OVERLAY_NOUN}|{OVERLAY_NOUN})\s+(?:with|using|as|in|to|by)\b"
    rf"|\bblend\s+(?:the\s+)?{OVERLAY_NOUN}\b"
    rf"|\b(?:use|apply)\s+(?:a\s+|the\s+)?{BLEND_WORDS}\s+(?:on|to|for)\s+(?:the\s+)?{OVERLAY_NOUN}\b"
    # review RE: "set the overlay's blend to overlay", "set blend to normal on
    # the overlay" were unread ("I did not catch that … Trim | Speed | Title")
    rf"|\b{OVERLAY_NOUN}(?:'s|s)?\s+blend(?:ing)?(?:[- ]mode)?\s+(?:back\s+)?(?:to|into|as)\s+(?:a\s+|the\s+)?{BLEND_WORDS}\b"
    rf"|\b(?:set|change|switch|make|put|turn)\s+(?:the\s+|its\s+|a\s+)?blend(?:ing)?\s+(?:back\s+)?(?:to|as|into)\s+"
    rf"(?:a\s+|the\s+)?{BLEND_WORDS}\b"
    # Final QA r3: "remove the screen blend (mode)", "turn off the blend
    # mode", "remove the blend" — back to Normal
    rf"|{BLEND_OFF}"
)


__all__ = ["BLEND_OFF", "BLEND_WORDS", "OVERLAY_NOUN", "BG_NOUN", "CANVAS_PHRASE", "BLEND_PHRASE", "COLOUR_WORDS", "HEX",
           "PICTURE"]
