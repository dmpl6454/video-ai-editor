"""The phrases the Editor Brain's `edit` recipe answers to (EB1).

These rows are NOT in `grammar.PHRASES` / `grammar.CUT_PRECEDENCE`: with
`brain.enabled` OFF the Prompt bar must read every sentence exactly as 0.8.0
did (review SC-04), so the tables the rest of the prompt package reads stay
the 0.8.0 ones and `grammar._resolve_clause` consults these only when the
brain is on. Pure data: this module imports nothing from the package.

Review UX-12 added the rows for "clean this up", "edit this", "make it
punchier", "cut this down to a 60s vertical for tiktok", camera switching
("switch to whoever is speaking") and the Hinglish forms ("podcast ko tight
karo, premium feel"). `SYNC_ROWS` is the one way to re-sync a stale dialogue
lane by hand (review SC-12 / EX-06): "sync the dialogue" runs
`sync_dialogue_lane` through the same `edit` intent — see `sync_only`.
"""
from __future__ import annotations

import re

#: A reel / short / tiktok / vertical OF a length ("make a 45-second reel",
#: "cut a 20 second reel out of this", "make a 2 minute reel") — checked
#: before the cut table, because a bare `cut` with a duration is a trim.
EDIT_LENGTH = (
    r"\b(?:make|cut|create|build|give me|turn (?:this|it) into|make (?:this|it) into|edit (?:this|it) into)\s+"
    r"(?:me\s+)?(?:an?\s+)?(?:engaging\s+|viral\s+|punchy\s+|short\s+|tight\s+|quick\s+|snappy\s+)*"
    r"\d+(?:\.\d+)?\s*[- ]?\s*(?:s|sec|secs|seconds?|min|mins|minutes?)\b[\w\s-]{0,24}?\b(?:reel|short|tiktok|vertical)s?\b")

#: "cut this down to a 60s vertical for tiktok"
_CUT_DOWN = (
    r"\b(?:cut|trim|shrink|squeeze|boil|get|make)\s+(?:this|it|the\s+(?:video|footage|clip|episode|podcast))\s+"
    r"(?:down\s+)?(?:to|into)\s+(?:an?\s+)?(?:about\s+|around\s+)?\d+(?:\.\d+)?\s*[- ]?\s*"
    r"(?:s|sec|secs|seconds?|min|mins|minutes?)\b[\w\s-]{0,30}?\b(?:vertical|reel|shorts?|tiktok|instagram|linkedin|portrait)\b")

_PODCAST = (
    r"\btighten\s+(?:up\s+)?(?:this|the|my|that|our)\s+(?:whole\s+)?(?:podcast|episode|interview|conversation|chat|talk|show)\b"
    r"|\bpremium\s+(?:business\s+|video\s+)?podcast\b"
    r"|\bedit\s+(?:this|it|the video|this podcast|the podcast|this interview|the interview|this episode|the episode"
    r"|this footage|the footage|my podcast|my video|my interview)\s+(?:like|as|into|the way)\b"
    r"|\bedit\s+(?:this|the|my|our)\s+(?:podcast|interview|episode|footage)\b"
    r"|\bcut\s+(?:this|it|the footage)\s+like\s+an?\s+(?:interview|podcast|reel|pro|editor)\b"
    r"|\blike\s+an?\s+(?:premium|pro|professional)\s+(?:podcast\s+)?editor\b")

#: UX-12: the vague asks an editor still understands
_VAGUE = (
    r"\bclean\s+(?:this|it|the\s+(?:video|footage|podcast|episode))\s+up\b(?!\s+for\b)"
    r"|^(?:please\s+)?edit\s+(?:(?:this|that|it)(?:\s+(?:video|footage|clip|episode|podcast|interview))?"
    r"|the\s+(?:video|footage|clip)|my\s+(?:video|footage|clip))(?:\s+(?:for me|please))?$"
    r"|\bmake\s+(?:it|this)\s+(?:a\s+bit\s+|a\s+little\s+|much\s+)?(?:punchier|snappier|tighter|crisper"
    r"|more\s+(?:engaging|dynamic|energetic|punchy|watchable))\b"
    r"|\b(?:remove|cut|drop|lose|trim)\s+(?:out\s+)?(?:all\s+)?(?:of\s+)?the\s+(?:boring|dull|dead|slow|rambling|useless)"
    r"\s+(?:parts?|bits?|stuff|sections?|segments?)\b")

#: "clean this up" / "remove the boring parts": a CLEAN-UP — cuts, fillers, pauses; not captions, punch-ins or a
#: new audio lane (closer review: 'clean this up' returned a full episode edit, more than was asked). The
#: other vague asks ("edit this", "make it punchier") keep the whole recipe.
CLEANUP = (
    r"\bclean\s+(?:this|it|the\s+(?:video|footage|podcast|episode))\s+up\b(?!\s+for\b)"
    r"|\b(?:remove|cut|drop|lose|trim)\s+(?:out\s+)?(?:all\s+)?(?:of\s+)?the\s+(?:boring|dull|dead|slow|rambling|useless)"
    r"\s+(?:parts?|bits?|stuff|sections?|segments?)\b")
_CLEANUP_RE = re.compile(CLEANUP)


def is_cleanup(clause: str) -> bool:
    """True for the clean-up asks (`CLEANUP`) that name nothing else the edit does."""
    text = clause or ""
    return bool(_CLEANUP_RE.search(text)) and not re.search(
        r"\b(?:caption|subtitle|music|reel|short|tiktok|vertical|podcast\s+like|punch|zoom|camera|angle)", text)


#: UX-12: choosing the camera is part of the edit
_CAMERA = (
    r"\bswitch(?:ing)?\s+(?:the\s+)?(?:cameras?|angles?|cams?)\b"
    r"|\bswitch\s+to\s+(?:whoever|who\s+is|the\s+(?:person|speaker|guest|host|one)\s+(?:who\s+is\s+)?(?:speaking|talking))"
    r"|\bcut\s+(?:between|to)\s+(?:the\s+)?(?:cameras?|angles?|speakers?)\b"
    r"|\bfollow\s+the\s+(?:speaker|conversation)\b")

#: UX-12: Hinglish ("podcast ko tight karo, premium feel")
_HINGLISH = (
    r"\b(?:podcast|interview|episode|video|footage|isko|ise|isse)\s+ko\s+(?:tight|tighten|edit|clean|premium|pro|professional"
    r"|sundar|badiya)\b"
    r"|\btight\s+(?:kar(?:o|do|na|dena)?|do)\b"      # the grammar's normaliser turns "karo" into "do"
    r"|\bpremium\s+(?:feel|vibe|quality)\b")

#: "sync the dialogue" — a stale lane re-synced by hand (SC-12 / EX-06)
SYNC = (
    r"\b(?:re-?sync|sync|resync)\s+(?:up\s+)?(?:the\s+|my\s+)?(?:dialogue|dialog)(?:\s+lane)?\b"
    r"|\bfix\s+(?:the\s+)?(?:dialogue|dialog)\s+lane\b"
    r"|\b(?:dialogue|dialog)(?:\s+lane)?\s+(?:is\s+|has\s+gone\s+)?out\s+of\s+(?:sync|step)\b")

EDIT_ROWS: tuple[str, ...] = (EDIT_LENGTH, _CUT_DOWN, _PODCAST, _VAGUE, _CAMERA, _HINGLISH)

#: (regex, score) rows for the `edit` intent, EXACT
EDIT_PHRASES: dict[str, tuple[tuple[str, float], ...]] = {
    "edit": (("|".join(EDIT_ROWS + (SYNC,)), 1.0),),
}

#: Checked before the cut table: a bare `cut` / `trim` with a duration is otherwise a trim.
EDIT_FIRST_RE = re.compile(EDIT_LENGTH + "|" + _CUT_DOWN)
_SYNC_RE = re.compile(SYNC)
_OTHER_RES = tuple(re.compile(r) for r in EDIT_ROWS)
_ANY_EDIT_RE = re.compile("|".join(EDIT_ROWS))

#: What the brain's `edit` subsumes: it tightens, cuts silences and fillers
#: and does the whole edit itself, so those clauses beside it are read as
#: parts of ONE edit (never run twice).
SUBSUMED_INTENTS: frozenset[str] = frozenset({"auto_edit", "tighten", "remove_silences", "remove_fillers"})


def sync_only(clause: str) -> bool:
    """True when the clause asks for the dialogue lane to be re-synced and for
    nothing else the `edit` recipe does."""
    text = clause or ""
    return bool(_SYNC_RE.search(text)) and not any(rx.search(text) for rx in _OTHER_RES)


def reads_as_edit(clause: str) -> bool:
    """True when the (normalised) clause is one of the whole-edit asks the `edit` rows read — the
    contract (contract.py) then judges it as a composite ask, exactly as it does "make it a reel"."""
    return bool(_ANY_EDIT_RE.search(clause or ""))


__all__ = ["CLEANUP", "is_cleanup", "EDIT_LENGTH", "EDIT_FIRST_RE", "EDIT_PHRASES", "SYNC", "SUBSUMED_INTENTS", "sync_only", "reads_as_edit"]
