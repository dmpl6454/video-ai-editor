"""Intent grammar — which recipes a prompt asks for, and how sure we are (§2.3).

`detect(prompt) -> Detection`: the prompt is split into clauses on `,`,
`and`, `then`, `aur`, `phir`, `;`; each clause is scored against a phrase
table (1.0 exact phrase · 0.85 synonym · 0.5 weak keyword) and yields at most
one intent — except the deliberate composites (`tighten`, `auto_edit`,
templates) which ARE one intent. Negated clauses ("no captions", "without
music", "bina music") become `exclusions` and never a hit.

Confidence (§2.7) = mean(clause_scores) × coverage, where coverage is the
share of clauses that produced a hit or an exclusion. The router runs recipes
alone at ≥ 0.75, lets the on-device brains normalise between 0.4 and 0.75,
and asks at < 0.4.

WHY a precedence table for `cut` instead of a smarter regex: "cut" is the
most overloaded verb in editing. `cut to the beat` is beat_sync, `cut the
first 5 seconds` is trim, `cut out the ums` is remove_fillers, `cut into 3`
is shorts, `cut the silences` is remove_silences, and bare `cut` with
nothing else is a trim only when it has a range. The table below is checked
IN ORDER before the phrase table so the specific reading always wins, and
tests/test_prompt_grammar.py pins every row.

WHY templates are read from presets, not hard-coded: `presets/templates/*.json`
name and describe whole edits ("lecture cleanup", "talking head reel"). Their
`keywords` join the grammar as `auto_edit` variants, so a new template file
is a new vocabulary entry with no code change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import slots as S

INTENTS: tuple[str, ...] = (
    "auto_edit", "captions", "translate_captions", "remove_silences", "remove_fillers", "tighten",
    "shorts", "reframe", "music", "duck", "beat_sync", "hook", "color_look", "clean_audio",
    "loudness", "speed", "trim", "title", "brand", "end_card", "transitions", "export_preset",
    "voiceover", "stabilize", "upscale", "undo", "redo", "ask",
)

EXACT, SYNONYM, WEAK = 1.0, 0.85, 0.5

#: Confidence policy (§2.7).
RUN_THRESHOLD = 0.75
NORMALISE_THRESHOLD = 0.4


@dataclass(frozen=True)
class IntentHit:
    intent: str
    score: float
    clause: str
    slots: S.Slots
    template: str | None = None      # auto_edit variant from presets/templates


@dataclass(frozen=True)
class Detection:
    prompt: str
    clauses: tuple[str, ...]
    hits: tuple[IntentHit, ...]
    exclusions: tuple[str, ...]
    slots: S.Slots                   # whole-prompt slots (cross-clause: platform, language …)
    unmatched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def confidence(self) -> float:
        total = len(self.clauses)
        if total == 0:
            return 0.0
        matched = len(self.hits) + len(self.unmatched_exclusion_clauses)
        scores = [h.score for h in self.hits] + [EXACT] * len(self.unmatched_exclusion_clauses)
        if not scores:
            return 0.0
        return round((sum(scores) / len(scores)) * (min(matched, total) / total), 3)

    @property
    def unmatched_exclusion_clauses(self) -> tuple[str, ...]:
        """Clauses that were pure negations — they count as understood."""
        hit_clauses = {h.clause for h in self.hits}
        return tuple(c for c in self.clauses if c not in hit_clauses and c not in self.unmatched)

    @property
    def intents(self) -> list[str]:
        return [h.intent for h in self.hits]

    @property
    def tier(self) -> str:
        c = self.confidence
        if c >= RUN_THRESHOLD:
            return "run"
        if c >= NORMALISE_THRESHOLD:
            return "normalise"
        return "clarify"


# --------------------------------------------------------------------------
# 1. Clause splitting
# --------------------------------------------------------------------------

_QUOTE_RE = re.compile(r"[\"“”']([^\"“”']{1,200})[\"“”']")
_SPLIT_RE = re.compile(r"\s*(?:,|;|\bthen\b|\band then\b|\band also\b|\band\b|\bplus\b|\bafter that\b)\s*")

#: `and` inside these phrases joins words, not clauses.
_PROTECTED = (
    "black and white", "you know", "so basically", "name and handle", "hook and", "back and forth",
    "in and out", "cut and pulse", "silences and fillers", "silences and filler words",
    "fillers and silences", "pauses and fillers", "ums and uhs", "ums and ahs", "um and uh",
    "clean and", "warm and", "loud and clear", "rock and roll", "drum and bass",
)


def split_clauses(prompt: str) -> list[str]:
    """Clauses of a normalised prompt. Quoted spans are opaque (an `and`
    inside quotes is text, and the quotes are restored afterwards)."""
    text = S.normalize(prompt)
    if not text:
        return []
    keep: dict[str, str] = {}

    def _stash(m: re.Match) -> str:
        key = f"\x00{len(keep)}\x00"
        keep[key] = m.group(0)
        return key

    text = _QUOTE_RE.sub(_stash, text)
    for i, phrase in enumerate(_PROTECTED):
        text = text.replace(phrase, phrase.replace(" and ", f"\x01{i}\x01"))
    parts = [p for p in _SPLIT_RE.split(text) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        for i, phrase in enumerate(_PROTECTED):
            p = p.replace(f"\x01{i}\x01", " and ")
        for key, val in keep.items():
            p = p.replace(key, val)
        p = p.strip(" .,")
        if p:
            out.append(p)
    return out


# --------------------------------------------------------------------------
# 2. Negation
# --------------------------------------------------------------------------

_NEG_TARGETS: tuple[tuple[str, str], ...] = (
    (r"captions?|subtitles?|subs", "captions"),
    (r"music|song|track|bed|bgm|soundtrack", "music"),
    (r"hook|hook text|opener", "hook"),
    (r"transitions?", "transitions"),
    (r"lut|colou?r (?:grade|look)|grade|look|filter", "color_look"),
    (r"watermark|brand(?: kit)?|logo", "brand"),
    (r"end ?card|outro", "end_card"),
    (r"voice ?over|narration|tts", "voiceover"),
    (r"reframe|crop|resize|reframing", "reframe"),
    (r"speed ?up|speed changes?|speed", "speed"),
    (r"cuts?|trims?|cutting|trimming", "trim"),
    (r"silences?|pauses?", "remove_silences"),
    (r"fillers?|ums|uhs", "remove_fillers"),
    (r"noise reduction|denoise|denoising|audio clean ?up", "clean_audio"),
    (r"loudness|normali[sz]ation|normali[sz]e", "loudness"),
    (r"translation|translate", "translate_captions"),
)
_NEG_RE = re.compile(
    r"(?:\bno\b|\bwithout\b|\bdon'?t\b|\bdo not\b|\bskip(?:\s+the)?\b|\bleave out\b|\bnot?\s+any\b|\bnever\b|\bminus\b|\bexcept\b|\bbut no\b|\bnahi\b|\bmat\b)"
    r"\s+(?:the\s+|any\s+|a\s+|an\s+)?(?:add(?:ing)?\s+|put(?:ting)?\s+)?(?:the\s+|a\s+|an\s+|any\s+)?"
    r"(?P<what>[a-z][a-z ]{1,40}?)(?=$|\s+(?:and|or|but|,|please|though|either|just)\b|[,.!?]|\s+(?:on|in|to|for)\b)")


def exclusions_in(clause: str) -> list[str]:
    out: list[str] = []
    for m in _NEG_RE.finditer(clause):
        what = m.group("what").strip()
        for pat, intent in _NEG_TARGETS:
            if re.fullmatch(rf"(?:{pat})(?:\s+\w+)?", what) or re.match(rf"(?:{pat})\b", what):
                if intent not in out:
                    out.append(intent)
                break
    return out


def strip_negations(clause: str) -> str:
    """The clause with negated phrases removed, so "add music but no captions"
    still yields `music`."""
    return _NEG_RE.sub(" ", clause).strip(" ,")


# --------------------------------------------------------------------------
# 3. Phrase tables
# --------------------------------------------------------------------------

_HAS_RANGE = r"(?:\bfirst\b|\blast\b|\bfrom\b|\bbetween\b|\d+\s*(?:s|sec|secs|seconds?|m|min|mins|minutes?)\b|\d{1,2}:\d{2})"

#: (regex, intent, score) checked in order BEFORE the phrase table (§2.3).
CUT_PRECEDENCE: tuple[tuple[str, str, float], ...] = (
    (r"\bcut(?:s|ting)?\s+(?:it\s+|this\s+|the\s+video\s+)?(?:to|on|with|along)\s+the\s+(?:beat|music|rhythm|drums?|bpm)\b|\bcut to the beat\b|\bon the beat\b|\bbeat[- ]sync\b|\bsync(?:ed)?\s+to\s+the\s+(?:beat|music)\b|\bbeat[- ]match\b", "beat_sync", EXACT),
    (r"\bcut\s+(?:out\s+|away\s+)?(?:the\s+|all\s+(?:the\s+)?|every\s+)?(?:ums?|uhs?|umms?|filler(?:s| words?)|hesitations?|stutters?)\b", "remove_fillers", EXACT),
    (r"\bcut\s+(?:out\s+|away\s+)?(?:the\s+|all\s+(?:the\s+)?|every\s+)?(?:silences?|pauses?|dead air|gaps?|quiet parts?)\b", "remove_silences", EXACT),
    (r"\bcut\s+(?:it\s+|this\s+|the\s+video\s+)?(?:up\s+)?into\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|several|some)\s*(?:shorts?|clips?|parts?|pieces?|highlights?|reels?|segments?|videos?)\b|\bcut\s+(?:it\s+)?into\s+shorts\b", "shorts", EXACT),
    (rf"\bcut\s+(?:off\s+|out\s+|away\s+)?(?:the\s+)?(?=.*{_HAS_RANGE})", "trim", EXACT),
    (r"\bcut\s+(?:it\s+|this\s+|the\s+video\s+)?(?:down|shorter|tighter)\b", "tighten", SYNONYM),
)

#: intent → list of (regex, score). First match per pattern; best score wins.
PHRASES: dict[str, tuple[tuple[str, float], ...]] = {
    "undo": ((r"^(?:undo|undo that|undo it|undo the last(?: one| edit| change)?|go back|revert(?: that| it)?|take that back|ctrl ?z|cmd ?z|wapas karo|wapas)$", EXACT),
             (r"\bundo\b|\brevert\b", SYNONYM)),
    "redo": ((r"^(?:redo|redo that|redo it|redo the last(?: one| edit)?)$", EXACT), (r"\bredo\b", SYNONYM)),
    "auto_edit": ((r"\b(?:auto[- ]?edit|full edit|complete (?:the )?(?:video|edit)|finish (?:the |this )?(?:video|edit)|do (?:the|a) (?:whole|full|complete) (?:edit|thing)|edit (?:the|this) (?:whole )?video|make (?:it|this) (?:ready|good|great|perfect|nice)|make it pop|polish (?:it|this|the video)|do everything|clean this up for|get (?:it|this) ready for|prepare (?:it|this) for|optimi[sz]e (?:it|this) for|turn this into a (?:reel|tiktok|short|youtube video)|make (?:a|this a|it a) (?:reel|tiktok|short))\b", EXACT),
                  (r"\b(?:for|ready for|good for|great for|fit for)\s+(?:instagram|reels|tiktok|youtube|shorts|linkedin|a reel|a short)\b", SYNONYM),
                  (r"\bedit (?:it|this)\b|\bfix (?:it|this) up\b|\bmake it better\b|\bimprove (?:it|this)\b", WEAK)),
    "translate_captions": ((r"\btranslate\b|\btranslation\b|\banuvad\b", EXACT),
                           (r"\bcaptions?\s+(?:in|into|to)\s+(?:hindi|hinglish|english|spanish|hi|en|es)\b|\b(?:hindi|hinglish|english|spanish)\s+(?:captions?|subtitles?)\b", SYNONYM)),
    "captions": ((r"\b(?:add|put|generate|create|make|lay|burn(?: in)?|do|turn on|give (?:it|me)|i want|need)\s+(?:some\s+|the\s+|auto\s+|automatic\s+|\w+\s+)?(?:captions?|subtitles?|subs|cc)\b|\b(?:auto[- ]?)?captions?\b|\bsubtitles?\b|\bcaption (?:it|this|the video)\b|\btranscribe and caption\b|\bkaraoke\b", EXACT),
                 (r"\bsubs\b|\bcc\b|\bwords on screen\b|\btext of what (?:i|he|she|they) (?:say|said)\b|\bon[- ]screen text of the speech\b", SYNONYM),
                 (r"\btranscri(?:be|ption)\b", WEAK)),
    "remove_fillers": ((r"\b(?:remove|delete|strip|drop|kill|clean(?: up)?|get rid of|take out|edit out|nix|cut)\s+(?:the\s+|all\s+(?:the\s+)?|every\s+|my\s+)?(?:ums?|uhs?|umms?|uhhs?|erms?|hmms?|filler(?:s| words?)|stutters?|hesitations?|verbal (?:tics|fillers))\b|\bfiller words?\b|\bno more ums\b|\bums and uhs\b|\bthe ums\b", EXACT),
                       (r"\bums?\b|\buhs?\b|\bfillers?\b", SYNONYM)),
    "remove_silences": ((r"\b(?:remove|delete|strip|drop|kill|trim|take out|edit out|cut)\s+(?:the\s+|all\s+(?:the\s+)?|every\s+|any\s+|long\s+|awkward\s+)?(?:silences?|silent (?:parts?|bits?|gaps?)|pauses?|dead air|gaps?|quiet parts?)\b|\bsilence removal\b|\bjump ?cut (?:it|this|the pauses)\b|\bauto[- ]?cut the (?:silence|pauses)\b", EXACT),
                        (r"\bsilences?\b|\bpauses?\b|\bdead air\b|\bawkward gaps?\b", SYNONYM)),
    "tighten": ((r"\btighten(?: it| this| the video| up| it up| this up)?\b|\bmake (?:it|this) (?:tighter|snappier|punchier|faster paced)\b|\bremove (?:the )?(?:silences?|pauses?) and (?:the )?(?:fillers?|filler words|ums)\b|\b(?:silences?|pauses?) and (?:fillers?|filler words|ums)\b|\bjump ?cut (?:it|this|the video)\b|\bcut the (?:fat|fluff|dead weight)\b|\btrim the fat\b|\bshorten (?:it|this|the video)\b|\btrim (?:it|this) down\b|\bsmart cut\b", EXACT),
                (r"\bsnappier\b|\bpacier\b|\bfaster paced\b|\bless rambling\b|\bconcise\b", SYNONYM)),
    "shorts": ((r"\b(?:make|create|generate|give me|produce|extract|pull|find|get)\s+(?:me\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a few|several|some|a couple of)?\s*(?:short|vertical|quick|viral|best)?\s*(?:shorts?|clips?|highlights?|reels?|snippets?|teasers?|moments)\b(?! (?:transitions?|captions?))|\bhighlights? reel\b|\bbest (?:bits|moments|parts)\b|\bsplit (?:it|this) into (?:\d+|shorts|clips)\b|\bchop (?:it|this) (?:up )?into\b|\bmake shorts\b|\bshorts out of (?:this|it)\b", EXACT),
               (r"\bhighlights?\b|\bclip (?:it|this) up\b|\bviral moments?\b", SYNONYM)),
    "reframe": ((r"\b(?:make|turn|flip|convert|reframe|crop|resize|change)\s+(?:it|this|the video|the canvas|the aspect(?: ratio)?)?\s*(?:to|into)?\s*(?:vertical|portrait|landscape|horizontal|square|9:16|16:9|1:1|4:5|1080x1920|1920x1080|widescreen)\b|\b(?:auto[- ]?)?reframe\b|\bvertical version\b|\bportrait mode\b|\baspect ratio\b|\bcrop (?:it|this) (?:to|for)\b|\bfit (?:it|this) (?:to|for) (?:reels|tiktok|shorts|instagram|youtube|story|stories)\b|\bresize (?:it|this|the video) for\b|\bsubject[- ]track(?:ed|ing)? crop\b", EXACT),
                (r"\bvertical\b|\bportrait\b|\blandscape\b|\bsquare\b|\b9:16\b|\b16:9\b|\b1:1\b|\b4:5\b", SYNONYM)),
    "duck": ((r"\bduck(?:ing)?\b|\blower the music (?:under|behind|when|during)\b|\bmusic (?:under|behind|below) (?:my|the) (?:voice|speech|talking|dialogue)\b|\bquiet(?:er)? (?:the )?music (?:when|while|under)\b|\bmusic (?:quieter|softer|lower|down) (?:when|while|under|during)\b|\bturn (?:the )?music down (?:when|while|under)\b|\bsidechain\b|\bauto[- ]?duck\b", EXACT),
             (r"\bmusic (?:too )?loud\b|\bmusic (?:is )?drowning\b|\bbalance (?:the )?music\b|\bmusic under\b", SYNONYM)),
    "beat_sync": ((r"\b(?:cut|edit|sync|snap|match|time|align)\w*\s+(?:it\s+|this\s+|the\s+(?:video|cuts|clips|footage)\s+)?(?:to|on|with|along)\s+(?:the\s+)?(?:beat|music|rhythm|drums?|bpm|tempo)\b|\bbeat[- ]?sync\b|\bon[- ]beat\b|\bbeat[- ]match(?:ed|ing)?\b|\bpulse (?:to|with|on) the (?:beat|music)\b|\bcuts? on (?:the )?beats?\b|\bbeat drops?\b", EXACT),
                  (r"\bto the (?:beat|music|rhythm)\b|\brhythm\b", SYNONYM)),
    "music": ((r"\b(?:add|put|drop|lay|throw in|give (?:it|me)|i want|need|play|with|use|set)\s+(?:some\s+|a\s+|the\s+|an?\s+\w+\s+|\w+\s+)?(?:background\s+)?(?:music|track|song|bed|beat|soundtrack|bgm|tune|score)\b|\bbackground music\b|\b(?:chill|upbeat|lo-?fi|cinematic|calm|energetic|epic|happy|dramatic|relaxing)\s+(?:background\s+)?(?:music|track|song|bed|beat|vibes?|tune)\b|\bmusic bed\b|\bbgm\b|\bsome music\b|\bmusic (?:please|pls)\b|\banother (?:track|song|music)\b|\breplace the (?:music|track|song)\b", EXACT),
              (r"\bmusic\b|\bsoundtrack\b|\bsong\b|\btune\b", SYNONYM)),
    "hook": ((r"\b(?:add|put|write|create|give (?:it|me)|make|need|i want|generate|open with)\s+(?:a\s+|an\s+|the\s+|some\s+|a\s+\w+\s+)?(?:hook|opener|opening (?:line|text|title|hook)|cold open|scroll[- ]stopper|attention grabber|punchy (?:intro|opening|start))\b|\bhook (?:it|this|them|the viewer)\b|\b(?:a |the )?hook\b|\bstop the scroll\b|\bgrab attention\b|\bpunchy (?:intro|opening|start)\b|\bfirst (?:3|three) seconds\b", EXACT),
             (r"\bintro text\b|\bopening text\b|\bopener\b|\battention\b", SYNONYM)),
    "color_look": ((r"\b(?:give|make|apply|add|put|use|grade|colou?r[- ]grade|slap on|throw on|set)\s+(?:it|this|the video|the footage|the clips?)?\s*(?:a\s+|an\s+|the\s+|some\s+)?(?:\w+[- ])?(?:look|grade|lut|filter|tone|vibe|feel|colou?r(?:s| grade| grading| correction| look)?|preset|teal[- ]orange|black and white|b ?and ?w)\b|\b(?:cinematic|warm(?:er)?|cool(?:er)?|cold|punchy|vivid|faded|vintage|retro|film|moody|teal(?: and orange| orange)?|black and white|monochrome|b ?and ?w|greyscale|grayscale)\s+(?:look|grade|lut|filter|tone|vibe|feel|colou?rs?|preset|footage)\b|\bmake (?:it|this|the (?:video|footage|colou?rs?)) (?:more )?(?:cinematic|warm(?:er)?|cool(?:er)?|cold(?:er)?|punchy|punchier|vivid|faded|vintage|retro|moody|black and white|monochrome|b ?and ?w|pop)\b|\bapply (?:a |the )?lut\b|\bcolou?r[- ]?grad(?:e|ing)\b|\blut\b", EXACT),
                   (r"\bcinematic\b|\bwarm\b|\bvintage\b|\bcolou?rs?\b|\bfilter\b|\bmoody\b", SYNONYM)),
    "clean_audio": ((r"\b(?:clean(?: up)?|fix|improve|enhance|de-?noise|denoise|reduce (?:the )?(?:background )?noise (?:in|on|of))\s+(?:up\s+)?(?:the\s+|my\s+|this\s+)?(?:audio|sound|voice|speech|mic|recording|hiss|hum|background noise|noise)\b|\bnoise (?:reduction|removal|cancel\w*)\b|\bremove (?:the )?(?:background )?(?:noise|hiss|hum|buzz|static)\b|\bdenois\w+\b|\baudio (?:clean ?up|enhance\w*|repair)\b|\bmake (?:the )?(?:audio|sound|voice) (?:clearer|cleaner|better|crisper)\b|\bbackground noise\b", EXACT),
                    (r"\bnoisy\b|\bhiss\b|\bhum\b|\bmuffled\b|\baudio\b", SYNONYM)),
    "loudness": ((r"\bnormali[sz]e\b|\bnormali[sz]ation\b|\blufs\b|\bloudness\b|\bset (?:the )?(?:volume|level|loudness) (?:to|at)\b|\b(?:broadcast|platform|youtube|spotify|streaming) (?:loudness|levels?|standard)\b|\bmake (?:it|the audio|the volume) (?:louder|consistent|even|level|uniform)\b|\bvolume (?:consistent|even|level)\b|\blevel (?:the|out the) (?:audio|volume|sound)\b", EXACT),
                 (r"\btoo quiet\b|\btoo loud\b|\bvolume\b|\blouder\b|\bquieter\b", SYNONYM)),
    "speed": ((r"\bspeed (?:it|this|the (?:video|clip|footage)|everything)?\s*(?:up|down)\b|\b(?:slow|speed) (?:it|this|the (?:video|clip|footage))? ?(?:down|up)\b|\bslow[- ]?mo(?:tion)?\b|\bslowmo\b|\b\d+(?:\.\d+)?\s*x\b(?![\dx:])|\b(?:double|half|quarter|twice the|half the|1\.5x|2x|0\.5x) (?:the )?speed\b|\bfaster\b|\bslower\b|\btime[- ]?lapse\b|\bplayback (?:speed|rate)\b|\bfast[- ]?forward\b|\bmake (?:it|this) (?:faster|slower|quicker)\b|\bspeed ramp\b|\btwice as fast\b", EXACT),
              (r"\bspeed\b|\bquick(?:er)?\b|\btempo of the video\b", SYNONYM)),
    "trim": ((rf"\b(?:trim|cut|remove|delete|drop|chop|lose|take (?:off|out)|get rid of|skip|shave)\s+(?:off\s+|out\s+|away\s+)?(?:the\s+)?(?:first|last|opening|closing|intro|outro)?\s*(?=.*{_HAS_RANGE})|\btrim (?:it|this|the (?:start|end|beginning|intro|outro|clip|video))\b|\bstart (?:it |the video )?(?:at|from)\s+\d|\bend (?:it |the video )?at\s+\d|\bkeep (?:only )?(?:the )?(?:first|last)\b|\bremove the (?:intro|outro|beginning|ending)\b|\bcut (?:the )?(?:intro|outro|beginning|ending|start|end)\b", EXACT),
             (r"\btrim\b|\bshorter\b|\bchop\b", SYNONYM)),
    "title": ((r"\blower[- ]?third\b|\bname (?:tag|plate|card|strap|title|banner)\b|\bnameplate\b|\bstrap(?:line)?\b|\b(?:add|put|show|display|write|overlay)\s+(?:a\s+|the\s+|some\s+|my\s+)?(?:title|text|caption text|label|heading|headline|super|on[- ]screen text|text overlay|name)\b|\btitle (?:card|it|this)\b|\bintroduce (?:me|him|her|them|the speaker|the guest)\b|\bname and handle\b|\bspeaker name\b|\bwho'?s talking\b", EXACT),
              (r"\btext\b|\btitle\b|\blabel\b|\bheadline\b", SYNONYM)),
    "brand": ((r"\bbrand(?:ing| kit| it| this|ed)?\b|\bwatermark\b|\bmy (?:handle|logo|colou?rs|brand)\b|\bapply (?:my |the )?(?:brand|kit)\b|\badd (?:my |the |a )?(?:handle|watermark|logo)\b|\bbrand colou?rs\b|\bhashtags?\b", EXACT),
              (r"\bhandle\b|\blogo\b|\b@\w+\b", SYNONYM)),
    "end_card": ((r"\bend[- ]?card\b|\bend (?:screen|slate|plate|title|frame)\b|\bouttro card\b|\boutro(?: card| screen)?\b|\bclosing (?:card|slate|screen)\b|\bfollow (?:me|us) (?:card|screen|at the end)\b|\bcall to action at the end\b|\bcta at the end\b|\bcta card\b|\bsubscribe (?:card|screen|reminder)\b", EXACT),
                 (r"\bcta\b|\bcall to action\b|\bfollow (?:me|us)\b", SYNONYM)),
    "transitions": ((r"\btransitions?\b|\bcross[- ]?(?:fade|dissolve)s?\b|\bdissolves?\b|\bwipes?\b|\bwhip pans?\b|\bswipes? between\b|\b(?:smooth|soft|clean|punchy|cinematic|fancy|nice|cool|fun)\s+(?:cuts|transitions?)\s+between\b|\bfade between (?:the )?clips\b|\bblend (?:the )?(?:clips|cuts)\b|\bsoften the cuts\b|\bcut points? (?:smoother|softer)\b"
                     # A named look + a seam reference is a transition request even without the word:
                     # "smooth zoom between every clip", "a glitch at every cut", "fade to black at the end".
                     r"|\b(?:zoom|glitch|whip|wipe|slide|push|blur|dissolve|flash|pixelat\w*|mosaic|spiral|spin|ripple|iris|diamond|blinds|checkerboard|film burn|(?:dip|fade) to (?:black|white))\b(?=.*\b(?:between|at|on) (?:the |every |each |all (?:the )?)?(?:clips?|cuts?|seams?|scenes?|shots?|hook|start|end|beginning|clip changes?)\b)", EXACT),
                    (r"\bbetween (?:the |every |each |all (?:the )?)?(?:clips?|cuts?|scenes?|shots?)\b|\bsmooth(?:er)? cuts\b|\bthe cuts\b", SYNONYM)),
    "export_preset": ((r"\bexport (?:preset|settings?|for|as|to)\b|\bexport[- ]ready\b|\brender (?:for|as|settings?)\b|\b(?:set|use|apply)\s+(?:the\s+)?(?:\w+\s+)?(?:export\s+)?preset\b|\boptimi[sz]e (?:the )?(?:export|output|render|settings) for\b|\bformat (?:it|this) for\b|\bsettings for (?:instagram|reels|tiktok|youtube|shorts|linkedin|stories)\b|\b(?:instagram|reels|tiktok|youtube|shorts|linkedin|story|stories) (?:export|settings?|specs?|format|preset|ready|spec)\b|\bbitrate\b|\bready to (?:upload|post|publish)\b", EXACT),
                      (r"\bexport\b|\bpreset\b|\bupload (?:it|this|ready)\b", SYNONYM)),
    "voiceover": ((r"\bvoice[- ]?over\b|\bnarrat(?:e|ion|or)\b|\btts\b|\btext[- ]to[- ]speech\b|\bai voice\b|\b(?:add|put|record|generate|make|have)\s+(?:a\s+|an\s+|some\s+|the\s+)?(?:\w+\s+)?voice\s+(?:say(?:ing)?|read(?:ing)?|that says|line|clip|narration)\b|\bvoice (?:say|saying|read|reading)\b|\bsay(?:ing)?\s+[\"“'].+[\"”']|\bread (?:this|it|the text) (?:out|aloud)\b|\bspoken (?:intro|outro|line)\b", EXACT),
                  (r"\bvoice\b|\bnarration\b|\bsay\b", SYNONYM)),
    "stabilize": ((r"\bstabili[sz](?:e|ation|er)\b|\b(?:fix|remove|reduce|smooth out|smooth)\s+(?:the\s+)?(?:shake|shakiness|shaky (?:footage|video|camera|cam|clip)|camera shake|jitter|wobble)\b|\bshaky\b|\bhandheld shake\b|\bsteady (?:it|this|the (?:shot|footage|video|clip))\b|\bsteadicam\b|\bwarp stabili[sz]er\b", EXACT),
                  (r"\bshake\b|\bjittery\b|\bwobbly\b", SYNONYM)),
    "upscale": ((r"\bupscal(?:e|ed|ing)\b|\bup-?res\b|\bsuper[- ]?resolution\b|\benhance (?:the )?resolution\b|\b(?:make|render|export) (?:it|this|the video) (?:4k|1080p|hd|sharper|higher res(?:olution)?)\b|\b(?:2|4)x (?:resolution|res|upscale)\b|\bincrease (?:the )?resolution\b|\bsharpen (?:it|this|the video) up\b|\bto 4k\b|\b(?:in|at) 4k\b", EXACT),
                (r"\bresolution\b|\b4k\b|\bsharper\b|\bblurry\b|\bhd\b", SYNONYM)),
    "ask": ((r"^(?:how long|what(?:'s| is) the (?:length|duration|aspect|resolution|size|canvas|loudness|language)|what (?:did|does|do) (?:i|we|you|this|it)|what(?:'s| is) (?:on|in) (?:the|this)|is there|are there|does (?:it|this) have|do (?:i|we) have|how many|which (?:brain|model|tools?)|what can you do|what tools|help|tell me about|describe (?:the|this)|summari[sz]e (?:the|this)|explain (?:the|this)|why (?:did|is|was)|where (?:is|are)|show me the|list the|kya hai|kitna lamba|kitne)\b|\?$", EXACT),
            (r"\bwhat\b|\bhow\b|\bwhich\b|\bwhy\b|\bstatus\b|\binfo\b", WEAK)),
}

#: Intents whose SYNONYM row is a bare noun that also appears inside other
#: intents' phrases; they only score when nothing more specific matched.
_NOUN_ONLY_FALLBACK: frozenset[str] = frozenset({"captions", "music", "hook", "color_look", "loudness",
                                                  "title", "brand", "transitions", "export_preset",
                                                  "voiceover", "speed", "trim", "clean_audio", "shorts",
                                                  "remove_fillers", "remove_silences", "ask", "reframe",
                                                  "beat_sync", "duck", "stabilize", "upscale", "end_card",
                                                  "auto_edit", "tighten", "translate_captions"})

_COMPILED: dict[str, tuple[tuple[re.Pattern, float], ...]] = {
    intent: tuple((re.compile(p), s) for p, s in rows) for intent, rows in PHRASES.items()}
_CUT_COMPILED = tuple((re.compile(p), i, s) for p, i, s in CUT_PRECEDENCE)

#: When two intents both match at EXACT in one clause, the more specific one
#: wins. Rows are (winner, loser).
_TIE_BREAKS: tuple[tuple[str, str], ...] = (
    ("translate_captions", "captions"), ("tighten", "remove_silences"), ("tighten", "remove_fillers"),
    ("tighten", "trim"), ("beat_sync", "music"), ("beat_sync", "trim"), ("beat_sync", "speed"),
    ("duck", "music"), ("duck", "loudness"), ("end_card", "title"), ("end_card", "brand"),
    ("brand", "title"), ("brand", "end_card"), ("hook", "title"), ("hook", "captions"),
    ("voiceover", "title"), ("voiceover", "captions"), ("voiceover", "speed"), ("voiceover", "trim"),
    ("shorts", "trim"), ("shorts", "reframe"), ("shorts", "auto_edit"), ("reframe", "export_preset"),
    ("export_preset", "auto_edit"), ("auto_edit", "captions"), ("auto_edit", "music"),
    ("auto_edit", "reframe"), ("auto_edit", "color_look"), ("auto_edit", "clean_audio"),
    ("color_look", "transitions"), ("transitions", "trim"), ("transitions", "speed"),
    ("transitions", "hook"), ("transitions", "reframe"), ("transitions", "upscale"), ("transitions", "end_card"),
    ("clean_audio", "loudness"), ("loudness", "clean_audio"), ("remove_fillers", "trim"),
    ("remove_silences", "trim"), ("remove_fillers", "captions"), ("upscale", "export_preset"),
    ("upscale", "reframe"), ("stabilize", "speed"), ("speed", "trim"), ("title", "captions"),
    ("captions", "translate_captions"), ("undo", "trim"), ("ask", "captions"),
)


def _resolve_clause(clause: str) -> tuple[str, float] | None:
    """The single best intent for one clause."""
    for rx, intent, score in _CUT_COMPILED:
        if rx.search(clause):
            return intent, score
    best: dict[str, float] = {}
    for intent, rows in _COMPILED.items():
        for rx, score in rows:
            if rx.search(clause):
                best[intent] = max(best.get(intent, 0.0), score)
                break
    if not best:
        return None
    # `ask` at EXACT only when the clause reads as a question — a stray "?"
    # after an edit verb ("add captions?") is still an edit.
    if "ask" in best and len(best) > 1 and best["ask"] >= EXACT:
        if re.match(r"^(?:add|put|make|remove|cut|trim|give|apply|turn|speed|slow|translate|tighten|reframe|crop|clean|normali[sz]e|export|upscale|stabili[sz]e|duck|brand)\b", clause):
            best["ask"] = WEAK
    top = max(best.values())
    tied = [i for i, s in best.items() if s == top]
    if len(tied) == 1:
        return tied[0], top
    # Specific-over-general at equal score, via the explicit table; a pair the
    # table does not order keeps phrase-table order (declared first wins).
    losers = {loser for winner, loser in _TIE_BREAKS if winner in tied and loser in tied}
    for intent in PHRASES:
        if intent in tied and intent not in losers:
            return intent, top
    return tied[0], top


# --------------------------------------------------------------------------
# 4. Templates (auto_edit variants)
# --------------------------------------------------------------------------

def _template_hit(clause: str) -> str | None:
    from .presets import edit_templates
    try:
        templates = edit_templates()
    except Exception:
        return None
    for name, tpl in templates.items():
        for kw in tpl.keywords:
            if re.search(rf"\b{re.escape(kw)}\b", clause):
                return name
        if re.search(rf"\b{re.escape(name.replace('_', ' '))}\b", clause):
            return name
    return None


# --------------------------------------------------------------------------
# 5. detect
# --------------------------------------------------------------------------

_UNDO_ONLY = re.compile(r"^(?:undo|redo)\b")


def _clause_slots(clause: str, whole: S.Slots) -> S.Slots:
    """The clause's own slots, with the case-bearing `name` restored from the
    whole prompt when the clause contains it.

    WHY: `split_clauses` lower-cases the prompt and `NAME_RE` reads a proper-
    noun run by its capitals, so "add a lower third for Priya Sharma
    @priya.codes" found the name at prompt level and lost it at clause level
    — the title recipe then asked "What should the title say?" for a name it
    had been given, and the benchmark's empty answer looped it to the round
    cap (case 16). Only a name the clause actually contains crosses over, so
    a two-clause prompt keeps its names apart."""
    own = S.extract(clause)
    if own.name is None and whole.name and whole.name.lower() in clause:
        return S.merge(own, S.Slots(name=whole.name))
    return own


def detect(prompt: str) -> Detection:
    clauses = tuple(split_clauses(prompt))
    whole = S.extract(prompt)
    hits: list[IntentHit] = []
    exclusions: list[str] = []
    unmatched: list[str] = []
    for clause in clauses:
        for ex in exclusions_in(clause):
            if ex not in exclusions:
                exclusions.append(ex)
        positive = strip_negations(clause) if exclusions_in(clause) else clause
        if not positive.strip():
            continue                       # pure negation clause: counted as understood
        template = _template_hit(positive)
        resolved = _resolve_clause(positive)
        if template and (resolved is None or resolved[0] != "shorts"):
            resolved = ("auto_edit", EXACT)
        if resolved is None:
            unmatched.append(clause)
            continue
        intent, score = resolved
        hits.append(IntentHit(intent=intent, score=score, clause=clause,
                              slots=_clause_slots(clause, whole), template=template))
    # undo/redo are whole-prompt intents: "undo that and add music" is a
    # sequence the executor cannot honour in one op, so undo alone wins.
    if hits and hits[0].intent in ("undo", "redo") and _UNDO_ONLY.match(S.normalize(prompt)):
        hits = [hits[0]]
    return Detection(prompt=prompt, clauses=clauses, hits=tuple(hits), exclusions=tuple(exclusions),
                     slots=whole, unmatched=tuple(unmatched))


__all__ = ["INTENTS", "EXACT", "SYNONYM", "WEAK", "RUN_THRESHOLD", "NORMALISE_THRESHOLD",
           "IntentHit", "Detection", "split_clauses", "exclusions_in", "strip_negations",
           "CUT_PRECEDENCE", "PHRASES", "detect"]
