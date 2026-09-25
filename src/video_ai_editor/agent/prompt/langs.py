"""Which caption targets need a TRANSLATION model, and which do not.

One rule, used by the recipe expanders (whether to ask for the 3 GB MADLAD
download), `validate_plan` (whether a plan that does not ask may run), the
executor's last-line guard (whether a step would fetch a model) and the
verifier (whether a Hinglish caption can be judged):

    target   spoken / source       what it takes
    -------  --------------------  ---------------------------------------
    en       anything              Whisper's own translate task — no MADLAD
    hi       hi                    nothing: the words are already Hindi
    hinglish hi (or hi-Latn)       TRANSLITERATION only (`ai.romanize`, pure
                                   Python, bundled) — never a translation
    hi/es/hinglish  another lang   MADLAD-400 (en→hi / en→es) first
    any      unknown               unknown until the audio is transcribed

WHY this exists (QA-043): the expanders gated every `hinglish` target on
MADLAD without looking at the spoken language, so "add hinglish captions" on
Hindi audio asked for a 3 GB download, and a Skip dropped the target and laid
Devanagari — the one script the user had not asked for.
"""
from __future__ import annotations

#: Caption targets that name a language a translation model may be needed for.
TARGET_BASE: dict[str, str] = {"hi": "hi", "hinglish": "hi", "es": "es"}

#: Spellings of "Hindi written in Latin script" (the dispatch aliases, and the
#: `language` a Hinglish caption run records in `ingest.json`).
HINGLISH_ALIASES: frozenset[str] = frozenset({"hinglish", "hi-latn", "roman", "romanized", "romanised"})


def base_lang(code: object) -> str | None:
    """The base language of a code: `hi-Latn`/`hinglish` → `hi`, `en_US` →
    `en`; None for an empty or missing code."""
    s = str(code or "").strip().lower()
    if not s:
        return None
    if s in HINGLISH_ALIASES:
        return "hi"
    return s.replace("_", "-").split("-")[0] or None


def is_latin_hindi(code: object) -> bool:
    return str(code or "").strip().lower() in HINGLISH_ALIASES


def needs_translation(target: object, source: object) -> bool | None:
    """True when reaching `target` from speech/text in `source` needs the
    MADLAD model, False when it does not, None when `source` is unknown and
    only a transcription can say."""
    t = str(target or "").strip().lower()
    if t in HINGLISH_ALIASES:
        t = "hinglish"
    if t not in TARGET_BASE:
        return False
    b = base_lang(source)
    if b is None:
        return None
    return b != TARGET_BASE[t]


__all__ = ["TARGET_BASE", "HINGLISH_ALIASES", "base_lang", "is_latin_hindi", "needs_translation"]
