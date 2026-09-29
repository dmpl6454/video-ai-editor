"""The recipes brain, corrected by the shared semantics (K3).

The grammar (`grammar.detect`) decides WHICH recipe a clause asks for from a
phrase table; three sweep rounds showed the same table misreading the same
few things in new words. Rather than another row per phrase, the planner
runs the grammar's reading through `semantics.py` — the module the contract
net judges with — and corrects it where the two disagree on something the
semantics reads with confidence:

  * `rewrite_hits` — a hit whose RECIPE contradicts the clause's words:
    "the music's way too loud" is the music's volume, not the programme
    loudness; "turn the music way down" is a level, not "add music"; "take
    the music out" removes it; "silence clip 3" mutes a clip (it is not
    "remove silences"); "add a title saying 'Welcome'" is a title, not a
    voice-over; "delete the first and last clips" deletes clips (not a
    range cut). A clause the grammar read NOTHING in, whose words the
    semantics reads as one edit ("black and white please", "the voice up",
    "the last second" after a trim, "no sound on clip 2", "go backwards"),
    gets that edit;
  * `fix_intents` — WHICH clip / lane: "the clip i selected" is the
    selection, "mute clip 2 but keep the music" mutes clip 2's own sound;
  * `fix_typos` — a prompt the grammar reads nothing in is retried with
    common slips fixed ("spead up teh second clip").

Anything the semantics does not read confidently is left alone; the
contract judges the result either way.
"""
from __future__ import annotations

import re
from dataclasses import replace

from . import grammar as G
from . import semantics as M
from . import slots as S
from .facts import TimelineFacts
from .recipes import Intent

_SYN = 0.85

_LOOK_WORD_RE = re.compile(r"\bblack and white\b|\bb\s*(?:and|&)\s*w\b|\bgr[ae]y\s*scale\b|\bmonochrome\b|\bsepia\b"
                           r"|\b(?:warm(?:er)?|cool(?:er)?|cinematic|vintage|faded)\s+(?:look|tone|filter|grade|lut|vibe"
                           r"|feel|colou?rs?)\b")
_ADD_VERB_RE = re.compile(r"\b(?:add|put|lay|throw in|drop in|use|play|another|new|different|replace|swap|switch|pick"
                          r"|choose|find|want (?:some|a|an)|need (?:some|a|an)|give (?:it|me) (?:some|a))\b")
_REMOVE_MUSIC_RE = re.compile(r"\b(?:remove|delete|get rid of|take\s+(?:the\s+)?(?:music|song|soundtrack|bgm|track)\s+(?:out|off)"
                              r"|take (?:out|off)|lose|ditch|scrap|no more|without|(?:don'?t|do not|dont) (?:want|need)"
                              r" (?:any |the )?(?:music|song|soundtrack|bgm))\b")
_LUFS_RE = re.compile(r"\blufs\b|\bloudness\b|\bnormali[sz]|\boverall\b|\bthe whole (?:mix|video|thing)\b|\beverything\b")
_SILENCE_CLIP_RE = re.compile(r"\bsilence\s+(?:the\s+)?(?:(?:\w+\s+)?(?:clip|shot|one)|clip\s+\w+|it|this|that)\b")
_TEXT_NOUN_RE = re.compile(r"\b(?:title|text|heading|headline|caption text|on[- ]screen|lower[- ]?third|label)\b")
_VO_WORD_RE = re.compile(r"\bvoice[- ]?overs?\b|\bvo\b|\bnarrat\w*|\bread (?:it|this|that) (?:out|aloud)\b|\btts\b"
                         r"|\bspeak\b|\bsay it out loud\b|\btext to speech\b")
_NO_SOUND_RE = re.compile(r"\bno (?:sound|audio)\b|\bwithout (?:sound|audio)\b|\b(?:sound|audio) off\b"
                          r"|\bkill (?:the )?(?:audio|sound)\b|\b(?:shut|switch|turn) off (?:the |its )?(?:sound|audio)\b"
                          r"|\b(?:make|keep)\s+(?:\w+\s+){0,3}?silent\b|\bsilent\b")
#: "turn the audio back on for clip 2": the clip's own sound, unmuted
_SOUND_ON_RE = re.compile(r"\b(?:sound|audio)\s+back\s+on\b|\bturn\s+(?:the\s+)?(?:sound|audio)\s+(?:back\s+)?on\b"
                          r"|\bbring\s+(?:the\s+)?(?:sound|audio)\s+back\b")
_TRIM_VERB_RE = re.compile(r"\b(?:cut|trim|remove|delete|chop|take|lose|drop|shave|shorten|knock|get rid of)\b")
_BACKWARDS_RE = re.compile(r"\b(?:go|goes|going|run|runs|play|plays)\s+backwards?\b|\bbackwards\b|\bin reverse\b")
_DELETE_VERB_RE = re.compile(r"\b(?:delete|remove|get rid of|lose|drop|trash|erase|ditch|scrap|kill|cut out|take out)\b")
_FEATURE_RE = re.compile(r"\b(?:speed|ramp|curve|filters?|luts?|looks?|effects?|transitions?|cross ?fades?|dissolves?"
                         r"|animations?|audio|sound|music|voice|text|titles?|captions?|subtitles?|blend|background"
                         r"|fades?|zoom|keyframes?|flip|rotation|colou?r|grade|vignett\w*|grain|freeze|black and white"
                         r"|volume|level|gain|db|decibels?|loudness|brightness|contrast|saturation)\b")


def _hit(intent: str, clause: str, det: G.Detection) -> G.IntentHit:
    return G.IntentHit(intent=intent, score=_SYN, clause=clause, slots=G._clause_slots(clause, det.slots))


def _primary_media(clause: str) -> str | None:
    """The lane a clause is about — ignoring a "but keep the music" tail."""
    head = re.split(r"\b(?:but|except|apart from|other than|besides|and keep|keep(?:ing)?)\b", M.norm(clause))[0]
    media = M.media_of(head)
    return media[0] if media else None


def rewrite_hits(det: G.Detection, facts: TimelineFacts) -> G.Detection:
    hits: list[G.IntentHit] = []
    covered: set[str] = set()          # clauses a hit was split out of (read already)
    changed = False
    for h in det.hits:
        c = h.clause
        new = None
        media = _primary_media(c)
        la = M.level_ask(c)
        refs = M.clip_refs(c)
        if h.intent == "loudness" and not _LUFS_RE.search(c) and (media in ("music", "voice", "vo") or refs):
            new = "volume"
        elif h.intent == "music" and media == "music" and not _ADD_VERB_RE.search(c) and not _REMOVE_MUSIC_RE.search(c) \
                and (la.direction in ("up", "down") or la.db is not None or re.search(r"\bless music\b", c)):
            new = "volume"
        elif h.intent == "music" and _REMOVE_MUSIC_RE.search(c) and not _ADD_VERB_RE.search(c):
            new = "remove_music"
        elif h.intent == "remove_silences" and _SILENCE_CLIP_RE.search(c) and not re.search(r"\bsilences\b|\bpauses?\b", c):
            new = "mute"
        elif h.intent == "voiceover" and _TEXT_NOUN_RE.search(c) and not _VO_WORD_RE.search(c):
            new = "title"
        elif h.intent == "voiceover" and la.direction in ("up", "down") and not _VO_WORD_RE.search(c) \
                and not S.QUOTED_RE.search(c):
            new = "volume"                              # "… and the voice up"
        elif h.intent == "trim" and re.search(r"\bspeed\b|\bpace\b", c) and M.speed_ask(c).direction in ("up", "down"):
            new = "speed"                               # "drop the speed of the last clip by half"
        elif h.intent == "transitions" and re.search(r"\bzoom", c) and not re.search(
                r"\btransitions?\b|\bbetween\b|\bcuts?\b|\bseams?\b|\bwipe|\bdissolve|\bfade|\bcross ?fade|\bglitch"
                r"|\bwhip|\bslide|\bflash|\bevery clip\b", c):
            new = "zoom"                                # "zoom a bit on clip one" put transitions on every seam
        elif h.intent in ("clean_audio", "loudness", "remove_silences", "volume", "remove_feature") and refs \
                and (_NO_SOUND_RE.search(c) or _SOUND_ON_RE.search(c)):
            new = "mute"                                # "no audio on the last clip" de-noised every clip
        elif h.intent in ("color_look", "adjust") and _TEXT_NOUN_RE.search(c) and _COLOUR_WORD_RE.search(c) \
                and not _LOOK_WORD_RE.search(c):
            new = "title"                               # "change the title colour to white" asked "Which look?"
        elif h.intent in ("trim", "delete_clip", "remove_feature") and _FADE_NOUN_RE.search(c) \
                and (_DELETE_VERB_RE.search(c) or re.search(r"\b(?:take|turn|switch)\b.*\boff\b|\bclear\b", c)) \
                and not re.search(r"\b(?:transitions?|cross ?fades?|dissolves?)\b", c):
            new = "fade"                                # "remove the fade from the last clip" (asked what to cut)
        elif h.intent == "trim" and refs and not M.time_refs(c) and _DELETE_VERB_RE.search(c) \
                and not _FEATURE_RE.search(c) and not re.search(r"\b(?:in half|in two|at)\b", c) \
                and not _PART_OF_RE.search(c):
            new = "delete_clip"
        elif h.intent == "delete_clip" and _PART_OF_RE.search(c) and not M.time_refs(c):
            new = "trim"                                # part of a clip is not the clip
        elif h.intent == "tighten" and M.whole_video_by(c) is not None:
            new = "trim"                                # run 4: "shorten the video by 3 seconds" is its tail, not its pauses
        elif h.intent == "fade" and _TEXT_NOUN_RE.search(c) and not _PICTURE_OR_SOUND_RE.search(c) \
                and not M.clip_refs(c) and set(M.media_of(c)) <= {"text"}:
            new = "title"                               # run 4: "fade the title in" faded the first CLIP's picture
        elif h.intent == "delete_clip" and M.ui_range(c) is not None:
            new = "trim"                                # "delete everything after the playhead": a range cut
        if new and new != h.intent:
            h = replace(h, intent=new)
            changed = True
        if h.intent in ("trim", "delete_clip") and M.keeps_only(c) and refs and not M.time_refs(c):
            # "only keep the last clip": delete every OTHER clip
            keep = {facts.selection if k == "$selected" else k for k in (_ref_sentinel(r, facts) for r in refs)}
            ids = list(facts.v1_clip_ids)
            if None not in keep and ids:
                for i, cid in enumerate(ids, start=1):
                    if cid not in keep:
                        hits.append(replace(_hit("delete_clip", f"delete clip {i}", det), score=h.score))
                covered.add(c)
                changed = True
                continue
        cuts = [t for t in M.time_refs(c) if t.kind in ("first", "last", "range")]
        if h.intent == "trim" and len(cuts) > 1 and not M.keeps_only(c):
            # "remove the first second and the last second": one clause (the
            # grammar keeps "second and the last" together), two cuts
            covered.add(c)
            for t in cuts:
                part = (f"cut the first {t.a:g} seconds" if t.kind == "first" else
                        f"cut the last {t.a:g} seconds" if t.kind == "last" else f"cut from {t.a:g}s to {t.b:g}s")
                hits.append(replace(_hit("trim", part, det), score=h.score))
            changed = True
            continue
        hits.append(h)
    # clauses the grammar read nothing in
    hit_clauses = {h.clause for h in hits} | covered
    prev_intents: list[str] = []
    extra: list[G.IntentHit] = []
    for c in det.clauses:
        if c in hit_clauses:
            prev_intents = [h.intent for h in hits if h.clause == c]
            continue
        got = _orphan_reading(c, prev_intents)
        if got:
            extra.append(_hit(got, c, det))
            prev_intents = [got]
    if extra:
        order = {c: i for i, c in enumerate(det.clauses)}
        hits = sorted(hits + extra, key=lambda h: order.get(h.clause, 99))
        changed = True
    return replace(det, hits=tuple(hits)) if changed else det


#: PART of a clip: "the middle of clip 2", "a bit of the last clip".
_PART_OF_RE = re.compile(r"\b(?:the|a|some)\s+(?:middle|centre|center|start|end|beginning|ending|part|bit|section|piece"
                         r"|chunk|rest|portion)\s+of\b")
_COLOUR_WORD_RE = re.compile(r"\b(?:red|blue|green|yellow|white|black|pink|orange|purple|violet|cyan|gold|grey|gray"
                             r"|lime|teal|magenta|brown|navy|silver)\b|#[0-9a-f]{6}\b")
_FADE_NOUN_RE = re.compile(r"\bfades?\b|\bfade[- ](?:ins?|outs?)\b")
#: A fade clause about the PICTURE or the SOUND, not a text ("fade the video
#: in under the title", "fade the audio out").
_PICTURE_OR_SOUND_RE = re.compile(r"\b(?:video|picture|footage|clip|clips|shot|screen|image|visuals?|black|audio|sound"
                                  r"|music|song|voice|volume|track)\b")
_TURN_IT_RE = re.compile(r"\bturn\s+(?:it|them|this|that)\s+(?:up|down)\b")


def _orphan_reading(clause: str, prev: list[str]) -> str | None:
    """The one edit a clause with no grammar hit asks for, when its words say
    it unambiguously; None otherwise."""
    c = M.norm(clause)
    if (_NO_SOUND_RE.search(c) or _SOUND_ON_RE.search(c)) and M.clip_refs(c):
        return "mute"
    if M.keeps_only(c) and [x for x in M.time_refs(c) if x.kind != "at"] and not M.clip_refs(c):
        return "trim"                   # "keep 4s-8s only"
    if M.ui_range(c) is not None and (M.keeps_only(c) or _TRIM_VERB_RE.search(c)) and not _FEATURE_RE.search(c):
        return "trim"                   # run 4: "keep from here to the end", "delete everything before the marker"
    if _TRIM_VERB_RE.search(c) and [x for x in M.time_refs(c) if x.kind in ("first", "last", "range")] \
            and not M.clip_refs(c) and not _FEATURE_RE.search(c):
        return "trim"                   # "remove seconds 10 through 12", "take 2 seconds off the end"
    if _DELETE_VERB_RE.search(c) and M.clip_refs(c) and _PART_OF_RE.search(c) and not M.time_refs(c):
        return "trim"                   # "remove the middle of clip 2": which part? (it deleted all of clip 2)
    if _DELETE_VERB_RE.search(c) and M.clip_refs(c) and not M.time_refs(c) and not _FEATURE_RE.search(c):
        return "delete_clip"
    if _BACKWARDS_RE.search(c):
        return "reverse"
    if _LOOK_WORD_RE.search(c) and not re.search(r"\b(?:remove|take off|get rid of|no)\b", c):
        return "color_look"
    media = M.media_of(c)
    la = M.level_ask(c)
    if media and media[0] in ("music", "voice", "vo") and (la.direction in ("up", "down") or la.db is not None):
        return "volume"
    if M.clip_refs(c) and not media and (la.db is not None or la.delta_db is not None):
        return "volume"                 # "set clip 3 to 0 db"
    if M.rotation_degrees(c) is not None and re.search(r"\b(?:tilt\w*|rotat\w*)\b", c):
        return "rotate"                 # "tilt clip one by 10 degrees"
    if not media and la.direction in ("up", "down") and (la.delta_db is not None or la.db is not None
                                                          or _TURN_IT_RE.search(c)):
        return "volume"                 # "… and turn it up 3db": a dB amount / the volume idiom is a level
    if M.time_refs(c) and "trim" in prev and not M.clip_refs(c):
        return "trim"
    sa = M.speed_ask(c)
    if (sa.direction in ("up", "down") or sa.factor) and not sa.ambiguous and not media \
            and re.search(r"\bspeed|\bslow|\bfast|\bquick|\d\s*x\b", c):
        return "speed"
    return None


# --------------------------------------------------------------------------
# which clip / which lane
# --------------------------------------------------------------------------

_PER_CLIP = frozenset({"speed", "mute", "color_look", "reverse", "flip", "volume", "voice_effect", "zoom", "rotate",
                       "adjust", "delete_clip", "animation"})


def _ref_sentinel(ref: M.ClipRef, facts: TimelineFacts) -> str | None:
    ids = list(facts.v1_clip_ids)
    if ref == "sel":
        return "$selected" if facts.selection in ids else None
    if isinstance(ref, int) and ids:
        i = ref - 1 if ref > 0 else len(ids) + ref
        return ids[i] if 0 <= i < len(ids) else None
    return None


def _trimmed_end(intents: list[Intent], facts: TimelineFacts) -> float | None:
    """The picture's end after the range trims this prompt plans, or None."""
    vend = float(facts.video_end or facts.duration or 0.0)
    cut = 0.0
    found = False
    for it in intents:
        r = it.get("range") if it.recipe == "trim" else None
        if r is None or it.get("_keep"):
            continue
        found = True
        if r.kind in ("first", "last") and r.end:
            cut += min(float(r.end), vend)
        elif r.kind == "abs" and r.start is not None and r.end is not None:
            cut += max(0.0, min(float(r.end), vend) - float(r.start))
    return round(max(0.0, vend - cut), 3) if found and vend else None


def fix_intents(intents: list[Intent], det: G.Detection, facts: TimelineFacts) -> list[Intent]:
    scopes = dict(zip(det.clauses, M.resolve_scopes(list(det.clauses))))
    trimmed_end = _trimmed_end(intents, facts)
    out: list[Intent] = []
    for it in intents:
        sc = scopes.get(it.clause)
        slots = dict(it.slots)
        bt = M.between_clips(it.clause or "")
        if it.recipe in _PER_CLIP and bt and bt[1] - bt[0] >= 2 and sc is not None and sc.refs:
            # final sweep 4: "cut out the part between clip 1 and clip 3" is
            # clip 2 — the grammar's own reader had picked clip 1
            slots["clip_ref"] = None
        if it.recipe in _PER_CLIP and sc is not None and len(sc.refs) == 1 and not sc.all and not sc.carried \
                and slots.get("clip_ref") in (None, "$v1_all") and not M.time_refs(it.clause):
            ref = _ref_sentinel(sc.refs[0], facts)
            if ref is not None:
                slots["clip_ref"] = ref
        if it.recipe == "delete_clip" and isinstance(slots.get("clip_ref"), str):
            # a delete names the clip on the timeline the prompt was written
            # against: a sentinel would re-resolve after an earlier delete
            ids = list(facts.v1_clip_ids)
            ref = slots["clip_ref"]
            conc = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(ref)
            if ref.startswith(G.NTH_REF) and ids:
                try:
                    n = int(float(ref[len(G.NTH_REF):]))
                    conc = ids[n - 1 if n > 0 else len(ids) + n] if -len(ids) <= n <= len(ids) and n else None
                except ValueError:
                    conc = None
            if conc:
                slots["clip_ref"] = conc
        if it.recipe in ("mute", "volume") and sc is not None and sc.refs and slots.get("target") == "music" \
                and _primary_media(it.clause) != "music":
            slots["target"] = "voice"                  # "mute clip 2 but keep the music"
        if it.recipe == "volume" and sc is not None and not sc.refs and slots.get("target") == "music" \
                and _primary_media(it.clause) == "voice":
            slots["target"] = "voice"                  # "… and the voice up"
        if it.recipe == "title" and slots.get("at") == "end" and trimmed_end is not None:
            slots["_video_end"] = trimmed_end
        out.append(it if slots == it.slots else Intent(it.recipe, slots, it.score, it.clause))
    return out


# --------------------------------------------------------------------------
# typos
# --------------------------------------------------------------------------

def fix_typos(prompt: str) -> str:
    """`prompt` with the shared typo table (semantics.TYPOS) applied, then a
    one-slip vocabulary fix (the planner's `_TYPO_VOCAB`) for the remaining
    lower-case words. Quoted words and Capitalised words (names: "Summer
    Trip" must not become "Summer trim") are never touched."""
    if S.QUOTED_RE.search(prompt or ""):
        return prompt
    import difflib
    from .planner import _TYPO_VOCAB
    out: list[str] = []
    for i, raw in enumerate((prompt or "").split()):
        tok = raw.strip(".,!?;:")
        low = M.fix_typos(tok.lower())
        if low != tok.lower():
            out.append(low)
            continue
        if tok[:1].isupper() and i > 0:
            out.append(tok.lower())
            continue
        if low.isalpha() and len(low) >= 4 and low not in _TYPO_VOCAB:
            near = difflib.get_close_matches(low, _TYPO_VOCAB, n=1, cutoff=0.8)
            low = near[0] if near else low
        out.append(low)
    return S.normalize(" ".join(out))


__all__ = ["rewrite_hits", "fix_intents", "fix_typos"]
