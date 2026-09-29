"""The prompt CONTRACT — the K3 safety net's judge.

A plan's own postconditions only check that the plan did what the PLAN
said; when a reader misread the prompt ("reduce the speed" → 1.25x, "mute
clip 2 but keep the music" → mute the music, "title text red please" → a
new title reading "text red please") the plan verified and the wrong edit
was committed. Fixing readers phrase by phrase did not converge (three
sweep rounds, 5-8 new wrong edits each).

So the executor asks a second, independent question before it commits:
does what CHANGED (contract_diff.diff of the EDL before vs after the run)
fit what the PROMPT said (semantics.py, read here with no reference to the
plan)? Four kinds of violation roll the whole run back to a question
(executor.run_plan → a clarify card, nothing in history):

  * `direction` — the prompt said slower / quieter / smaller / zoom out and
    something went the other way (or a direction contradicts its own
    amount: "slow it down to 2x");
  * `scope`     — the prompt named clips (or a lane: "the music") and an
    attribute changed on a clip / lane it did not name;
  * `partial`   — a clause the contract can read with confidence ("mute
    clip 1 and clip 3", "captions red AND bigger", "remove the first second
    and the last second") is not true of the result;
  * `unasked`   — a kind of change no clause licenses: a voice-over for "add
    a title", noise reduction for "black and white", a NEW title for "make
    the title red", a cut for "move the title".

Precision over recall on the `partial` and `unasked` rules: a clause the
contract cannot read confidently adds no expectation, and composite asks
("auto edit", "make it a reel", templates, platforms) license every kind of
change — their own checks stay the plan's postconditions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ...edl.schema import EDL, TextClip
from . import semantics as M
from .contract_diff import ClipDelta, Diff, diff, media_v1

TOL_SPEED = 0.011
TOL_DB = 0.06

# --------------------------------------------------------------------------
# 1. What each clause talks about (families)
# --------------------------------------------------------------------------

_COLOURS = (r"red|blue|green|yellow|white|black|pink|orange|purple|violet|cyan|gold|golden|grey|gray|lime|teal"
            r"|magenta|brown|navy|silver")
_FAMILY_RX: tuple[tuple[str, str], ...] = (
    ("composite", r"\bauto[- ]?edit|\bfull edit\b|\bpolish\b|\breels?\b|\btiktok\b|\byou ?tube\b|\binstagram\b|\bshorts?\b"
                  r"|\btemplate\b|\bmake (?:it|this) (?:ready|good|great|perfect|nice|better|pop|professional)\b"
                  r"|\bdo everything\b|\bclean (?:this|it) up\b|\btighten\b|\bready (?:for|to)\b|\bprepare\b|\boptimi[sz]e\b"
                  r"|\bviral\b|\bimprove\b|\benhance\b|\bbetter\b|\bfinish (?:the|this|it)\b|\bpodcast\b|\blecture\b"
                  r"|\btalking[- ]head\b|\bvlog\b|\blinkedin\b|\bstor(?:y|ies)\b|\bhighlights?\b|\bmontage\b(?!\s+(?:speed|curve|ramp))"
                  # beat sync (INTENT_FAMILIES "beat_sync" → composite): its cuts
                  # AND beat pulses (scale keyframes) are the recipe (gate, 0.8.0)
                  r"|\bbeat[- ]?sync|\bbeat[- ]?match|\bon[- ]beat\b"
                  r"|\b(?:cut|edit|sync|snap|match|time|align)\w*\s+(?:it\s+|this\s+|the\s+(?:video|cuts|clips|footage)\s+)?"
                  r"(?:to|on|with|along)\s+(?:the\s+)?(?:beats?|rhythm|drums?|bpm)\b"),
    ("speed", r"\bspeed|\bpace\b|\btempo\b|(?<![\w.])\d+(?:\.\d+)?\s*x\b|\bramp|\bcurve\b|\bhero\b|\bbullet\b|\bjump[- ]?cut\b"
              r"|\bflash[- ]?(?:in|out)\b|\btime[- ]?lapse|\bslow|\bfast|\bquick|\bdouble speed|\bhalf speed|\bnormal speed"),
    ("reverse", r"\breverse\b(?!\s+(?:the\s+)?order)|\bbackwards?\b|\bforwards?\b|\bun-?reverse|\brewind\b"),
    ("order", r"\breverse\s+(?:the\s+)?order\b|\bmove\b|\bswap\b|\breorder|\brearrange|\bshove\b|\bswitch\b"
              r"|\bput\s+.*\b(?:first|last|second|third|before|after|at the (?:end|start|beginning)|to the (?:end|start))\b"
              r"|\b(?:put|place|drag|stick|slot|insert)\s+.*\bbetween\b"),
    ("mute", r"\bun-?mute|\bmute|\bsilence\s+(?:the\s+)?(?:clip|shot|audio|sound|music|it|this|that|everything|all|\w+\s+clip)"
             r"|\bno sound\b|\bkill the (?:audio|sound)|\b(?:turn|switch) off the (?:audio|sound|music|background music|song)"
             r"|\btake the (?:sound|audio) (?:out|off)|\bwithout sound\b|\bsound off\b|\baudio off\b|\bsound back on\b"
             r"|\bno audio\b|\bwithout audio\b|\b(?:shut|switch|turn) off (?:the |its )?(?:sound|audio)\b|\bsilent\b"
             r"|\baudio back on\b|\bturn (?:the )?(?:sound|audio) (?:back )?on\b"),
    ("level", r"\bvolume\b|\blevels?\b|\bloud|\bquiet|\bsofter\b|\bgain\b|\bdb\b|\bdecibels?\b|\bturn\s+(?:\w+\s+){0,4}(?:up|down)\b"
              r"|\bbring\s+(?:\w+\s+){0,4}(?:up|down)\b|\bboost\b|\blower\b|\braise\b|\bkam\b|\bzyada\b|\bthoda\b|\bless music\b"),
    ("loudness", r"\blufs\b|\bloudness\b|\bnormali[sz]"),
    ("effect", r"\beffects?\b|\bvignett|\bgrain\b|\bglitch\b|\bvhs\b|\bglow\b|\brgb split\b|\bsharpen|\bblur"
               r"|\bfilters?\b|\bvintage\b"),
    ("look", r"\blook\b|\bfilter\b|\blut\b|\bgrade\b|\bgrading\b|\btone\b|\bvibe\b|\bblack and white\b|\bb\s*and\s*w\b|\bb&w\b"
             r"|\bgr[ae]y\s*scale\b|\bmono(?:chrome)?\b|\bwarm|\bcool(?:er)?\b|\bcold\b|\bcinematic\b|\bteal\b|\bpunch|\bvivid\b"
             r"|\bfaded\b|\bfilm\b|\bvintage\b|\bretro\b|\bsepia\b|\bnoir\b|\bpop\b|\bcolou?r(?:s|ful)?\b|\bblak\b|\bmatte\b"),
    ("adjust", r"\bbright|\bdark|\bdim\b|\bcontrast\b|\bsaturat|\bvibran|\bexposure\b|\bwashed\b|\bvivid\b|\bdesaturat"),
    ("zoom", r"\bzoom|\bpunch|\bpush[- ]in\b|\bken[- ]?burns\b|\bcloser\b|\bscale\b|\bcrop in\b|\bpull back\b"),
    ("rotate", r"\brotat|\btilt|\bupside down\b|\bdegrees?\b|°|\bsideways\b|\bturn\s+.*\b(?:90|180|270)\b"),
    ("flip", r"\b(?:un-?)?flip|\bmirror"),
    ("fade", r"\bfade|\bfrom black\b|\bto black\b|\bdip to\b"),
    ("transition", r"\btransitions?\b|\bcross ?fades?\b|\bdissolves?\b|\bwipes?\b|\bglitch\b|\bwhip\b|\bflash transition"
                   r"|\bbetween\s+(?:the\s+|all\s+|every\s+|each\s+)?(?:clips|shots|every|each|all|clip\s+\d|the\s+\w+\s+(?:and|&))"
                   r"|\bat every cut\b|\bat each cut\b|\bslide transition|\bzoom transition"),
    ("captions", r"\bcaptions?\b|\bsubtitles?\b|\bsubs\b|\bcaptoins?\b|\bcc\b|\btranslat"),
    ("text", r"\btitles?\b|\btext\b|\bheading\b|\bheadline\b|\blower[- ]?third\b|\blabel\b|\bon screen\b|\bon-screen\b"
             # final sweep 2 r2: "SALE should show until the end" names a text by its words
             r"|\b(?:should|must|needs to|has to)\s+(?:show|stay|be on screen|stay on screen)\s+(?:until|till|to)\b"
             r"|\bsuper\b|\bname card\b|\bwords?\b|[\"“”']"),
    ("music", r"\bmusic|\bmusci\b|\bsong\b|\bsoundtrack\b|\bbgm\b|\btune\b|\bbed\b|\bbacking track\b"),
    ("duck", r"\bduck|\bsidechain\b|\bunder (?:my|the) voice\b|\bwhen i (?:talk|speak)\b|\bwhile i (?:talk|speak)\b"),
    ("cut", r"\bcut\b|\btrim|\bdelet|\bdelte\b|\bremove\b|\bchop|\blose\b|\bdrop\b|\bget rid of\b|\bshorten|\bshorter\b"
            r"|\bkeep\b|\bsilence|\bpauses?\b|\bdead air\b|\bfillers?\b|\bumm?s?\b|\buhs?\b|\bcut out\b|\bcrop out\b"
            r"|\bseconds? long\b|\bmake (?:it|the video|this) \d+(?:\.\d+)?\s*(?:s|sec|seconds?)\b|\bstrip\b|\berase\b"
            r"|\bditch\b|\bscrap\b|\bkill\b|\btrash\b|\bout\b|\bduration\b|\blong\b|\blonger\b|\blength\b"
            r"|\bextend|\blengthen|\btake\s+(?:\d+(?:\.\d+)?|a|one|two|three|four|five|ten)\s+(?:s|sec|secs|seconds?|minutes?)\s+off\b"),
    ("split", r"\bsplit|\bslice|\bblade\b|\bcut\s+(?:it\s+|this\s+|the clip\s+)?(?:here|at)\b|\bin half\b|\bin two\b"
              r"|\binto two\b|\bchop\s+(?:it\s+)?at\b|\bcut\s+\w+\s+clip\s+at\b|\bcut\s+clip\s+\w+\s+at\b"),
    ("freeze", r"\bfreeze|\bhold\s+(?:the|on the|on)\s+(?:\w+\s+)?frame\b|\bstill frame\b"),
    ("duplicate", r"\bduplicat|\bcopy\b|\bclone\b|\brepeat\b"),
    ("canvas", r"\b\d{1,2}\s*:\s*\d{1,2}\b|\bvertical\b|\bportrait\b|\blandscape\b|\bsquare\b|\bwidescreen\b|\baspect\b"
               r"|\bratio\b|\breframe|\bcrop\b|\bresize|\bhorizontal\b|\b\d{3,4}\s*x\s*\d{3,4}\b|\bfill the frame\b"),
    ("canvas_bg", r"\bbackground\b|\bbackdrop\b|\bblack bars\b|\bbars\b|\bletter\s*box|\bcanvas\b|\bbehind the video\b"),
    ("blend", r"\bblend|\bmultiply\b|\bscreen\b|\blighten\b|\bdarken mode\b|\boverlay mode\b|\bsoft light\b|\bhard light\b"
              r"|\bcolou?r burn\b|\bcolou?r dodge\b|\blinear dodge\b|\bdifference\b|\bexclusion\b|\badditive\b"),
    ("voice_fx", r"\brobot|\becho\b|\breverb\b|\bchipmunk|\bdeep(?:er)?\b|\btelephone\b|\bmegaphone\b|\bradio\b|\bunderwater\b"
                 r"|\bvoice (?:effect|changer|filter)\b|\bpitch\b|\bhelium\b|\bmonster\b|\balien\b|\bvocoder\b"
                 r"|\bvoice\b.*\b(?:normal|off|back|reset|clean|plain)\b"),
    ("animation", r"\banimat|\bbounce|\bslides?\b|\bspin\b|\bshake\b|\bswing\b|\brock\b|\bpendulum\b"
                  r"|\bpop in\b|\bwobble\b|\bentrance\b|\bexit\b|\bin animation\b|\bout animation\b|\bcombo\b"),
    ("export", r"\bexport|\brender|\bpreset\b|\bbitrate\b|\b4k\b|\b1080p\b|\b720p\b|\bupload\b|\bpublish\b|\bformat (?:it|this) for\b"),
    ("vo", r"\bvoice[- ]?overs?\b|\bnarrat|\bvo\b|\bread (?:it|this|that) (?:out|aloud)\b|\bspeak\b|\bsay it\b|\btts\b"
           r"|\btext to speech\b|\bvoice track\b"),
    ("hook", r"\bhook\b"),
    ("brand", r"\bbrand|\bwatermark|\blogo\b|\bhandle\b|@\w|\bend ?card\b|\bsubscribe\b|\boutro card\b|\bhashtags?\b"),
    ("noise", r"\bnoise\b|\bhiss\b|\bhum\b|\bdenois|\bclean (?:up )?(?:the )?(?:audio|sound)\b|\bclean audio\b"
              r"|\bbackground sound\b|\bvoice isolat|\benhance (?:the )?(?:audio|voice|speech)\b"),
    ("stabilize", r"\bstabili|\bshaky\b|\bsteady\b|\bjitter"),
    ("upscale", r"\bupscal|\bsharper\b|\bresolution\b|\bhd\b"),
    ("smooth", r"\bsmooth\b|\binterpolat|\bbuttery\b|\boptical flow\b"),
    ("history", r"^(?:please\s+)?(?:undo|redo|go back|revert)\b"),
)
_FAMILY_COMPILED = tuple((f, re.compile(p)) for f, p in _FAMILY_RX)

#: Family names of the recipes a user can PICK from a "which did you mean"
#: menu (the pick licenses what it names).
INTENT_FAMILIES: dict[str, set[str]] = {
    "speed": {"speed"}, "trim": {"cut", "split"}, "title": {"text"}, "retext": {"text"}, "music": {"music"},
    "volume": {"level"}, "color_look": {"look"}, "adjust": {"adjust", "look"}, "fade": {"fade"},
    "freeze": {"freeze"}, "transitions": {"transition"}, "reverse": {"reverse"}, "mute": {"mute"},
    "captions": {"captions"}, "zoom": {"zoom"}, "rotate": {"rotate"}, "split": {"split"},
    "delete_clip": {"cut"}, "canvas": {"canvas_bg"}, "blend": {"blend"}, "voice_effect": {"voice_fx"},
    "animation": {"animation"}, "flip": {"flip"}, "remove_feature": {"look", "transition", "captions", "text"},
    "duplicate": {"duplicate"}, "move_clip": {"order"}, "clip_length": {"cut"}, "reframe": {"canvas"},
    "export_preset": {"export"}, "loudness": {"loudness"}, "clean_audio": {"noise"}, "duck": {"duck"},
    "remove_silences": {"cut"}, "remove_fillers": {"cut"}, "tighten": {"cut"}, "hook": {"hook", "text"},
    "voiceover": {"vo"}, "brand": {"brand", "text"}, "end_card": {"brand", "text"}, "stabilize": {"stabilize"},
    "upscale": {"upscale"}, "auto_edit": {"composite"}, "shorts": {"composite"}, "beat_sync": {"composite"},
    "fit_music": {"music"}, "remove_music": {"music"}, "translate_captions": {"captions"},
}

_TEXT_ADD_RE = re.compile(r"\b(?:add|put|insert|write|create|place|type|show|display|include|slap|stick|overlay|new|another"
                          r"|caption it with|says?|saying|reading|reads)\b|[\"“”']|\blower[- ]?third\b|\bhook\b"
                          r"|\bend ?card\b|\bsubscribe\b|\btitle\s*:")
_TEXT_RETEXT_RE = re.compile(r"\brename\b|\breplace\b|\breword|\bto say\b|\bto read\b|\bshould (?:say|read)\b|\bsays?\b"
                             r"|\bfix (?:the )?(?:typo|spelling)|\bedit (?:the )?(?:text|title|words?)\b|\bchange\b.*\bto\b"
                             r"|\bretext\b|\bwording\b")
_TEXT_REMOVE_RE = re.compile(r"\b(?:remove|delete|delet|get rid of|take (?:off|out|away)|clear|hide|drop|lose|kill|erase|ditch)\b")
_TEXT_RETIME_RE = re.compile(r"\bmove\b|\blater\b|\bearlier\b|\bshow(?:s)? up\b|\bappear|\bdisappear|\buntil\b|\btill\b"
                             r"|\blasts?\b|\blonger\b|\bshorter\b|\bextend|\bshorten|\bstay|\bfrom\s+\d|\bat\s+\d|\bfor\s+\d"
                             r"|\bstart(?:s)? at\b|\bend(?:s)? at\b|\bon screen for\b|\bseconds?\b")
_TEXT_RESTYLE_RE = re.compile(rf"\b(?:{_COLOURS})\b|#[0-9a-f]{{6}}|\bbigger\b|\bsmaller\b|\blarger\b|\bbiger\b|\bsize\b|\bfont\b"
                              r"|\b(?:twice|double|triple|half)\s+as\s+(?:big|large)\b|\bhalf\s+the\s+size\b"
                              r"|\bbold|\bitalic\b|\boutline|\bstroke\b|\bshadow|\bbox\b|\bbackground\b|\btop\b|\bbottom\b"
                              r"|\bmiddle\b|\bcent(?:er|re)\b|\bleft\b|\bright\b|\bposition\b|\bcaps\b|\buppercase\b"
                              r"|\blowercase\b|\banton\b|\bbebas\b|\bmontserrat\b|\binter\b|\bcolou?r\b")
_NEG_BEFORE = r"\b(?:no|without|don'?t|dont|do not|never|skip|bina|minus|not)\s+(?:\w+\s+){0,2}"


_AUDIO_CTX_RE = re.compile(r"\b(?:volume|levels?|loud\w*|quiet\w*|soft(?:er)?|sound\w*|audio|music|musci|song|voice"
                           r"|vocals?|dialogue|speech|db|decibels?|gain|mute\w*|bgm|soundtrack|tune|bed|narration"
                           r"|voice[- ]?over|mix|kam|zyada|hear|lufs)\b")


#: "turn clip 2 up", "turn it down", "turn up clip 2": the volume idiom
#: itself ("turn up clip 2 and turn down clip 1" was rolled back as unasked).
_TURN_CLIP_RE = re.compile(r"\bturn\s+(?:clip\s+\w+|it|them|this(?:\s+clip)?|that(?:\s+clip)?|the\s+\w+\s+clip)\s+(?:up|down)\b"
                           r"|\bturn\s+(?:up|down)\s+(?:clip\s+\w+|the\s+\w+\s+clip|this\s+clip|that\s+clip)\b"
                           # final sweep 2 r2: "lower clip 2 a lot" is its level
                           r"|\b(?:lower|raise|boost)\s+(?:clip\s+\w+|the\s+\w+\s+clip|this\s+clip|that\s+clip)"
                           r"(?:\s+(?:a\s+(?:lot|bit|little|touch)|much|way\s+(?:down|up)))?$")


def families_of(clause: str) -> set[str]:
    t = M.strip_quotes(M.norm(clause))
    # "do something cool", "make it nice": praise, not a colour look
    t = re.sub(r"\b(?:something|anything|it|this)\s+(?:cool|nice|fun|good|great|interesting|different|awesome)\b", " ", t)
    fams = {f for f, rx in _FAMILY_COMPILED if rx.search(t)}
    if re.search(r"[\"“”']", clause or ""):
        fams.add("text")
    if M.direction(t, "speed"):
        fams.add("speed")
    if M.direction(t, "level"):
        fams.add("level")
    if "transition" in fams and "order" in fams and re.search(r"\b(?:put|move|place|drag|stick|slot|insert)\b.*\bbetween\b", t) \
            and not re.search(r"\btransitions?\b|\bcross ?fades?\b|\bdissolves?\b|\bwipes?\b|\bfade\b|\bglitch\b|\bwhip\b"
                              r"|\bflash\b|\bslide\b", t):
        fams.discard("transition")     # "put clip 1 between clip 2 and clip 3" moves it
    if "transition" in fams and "speed" in fams and not re.search(r"\bspeed\b|\b(?:clips?|shots?)\b", t):
        fams.discard("speed")          # "make the transitions faster" is their length
    if "level" in fams and not _AUDIO_CTX_RE.search(t) and not _TURN_CLIP_RE.search(t):
        # "lower the contrast", "turn clip two upside down", "slow it down a
        # little", "speed up by 50%": level words with nothing audible
        fams.discard("level")
    if M.keeps_only(t):
        fams.add("cut")
    # "fade" inside a transition word is the transition
    if "transition" in fams and re.fullmatch(r".*\bcross ?fades?\b.*", t) and not re.search(r"\bfade (?:in|out)\b", t):
        fams.discard("fade")
    if re.search(r"\bcut\b", t) and ("split" in fams):
        pass
    return fams


def _negated(clause: str, noun: str) -> bool:
    return bool(re.search(_NEG_BEFORE + rf"(?:{noun})", M.norm(clause)))


# --------------------------------------------------------------------------
# 2. The reading of one prompt
# --------------------------------------------------------------------------

@dataclass
class ClauseRead:
    text: str
    families: set[str]
    scope: M.Scope
    inherited: bool = False

    @property
    def t(self) -> str:
        return M.strip_quotes(self.text)


@dataclass
class Violation:
    kind: str                 # direction | scope | partial | unasked | range
    clause: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "clause": self.clause, "message": self.message}


@dataclass
class Contract:
    prompt: str
    selection: str | None = None
    playhead: float | None = None
    #: intents the user explicitly picked ("which did you mean?") — they
    #: license their families even when the prompt's words do not.
    picked: tuple[str, ...] = ()
    reads: list[ClauseRead] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, prompt: str, *, selection: str | None = None, playhead: float | None = None,
             picked: Iterable[str] = ()) -> "Contract":
        c = cls(prompt=prompt, selection=selection, playhead=playhead, picked=tuple(picked))
        cls_ = M.clauses(prompt)
        scopes = M.resolve_scopes(cls_)
        prev: set[str] = set()
        for text, sc in zip(cls_, scopes):
            fams = families_of(text)
            inherited = False
            meaningful = fams - {"text"} if not re.search(r"[\"“”']", text) else fams
            if not meaningful and prev and not re.search(r"\b(?:undo|redo)\b", text):
                fams = set(prev)
                inherited = True
            c.reads.append(ClauseRead(text=text, families=fams, scope=sc, inherited=inherited))
            if fams:
                prev = fams
        return c

    # -- helpers ---------------------------------------------------------
    @property
    def families(self) -> set[str]:
        out: set[str] = set()
        for r in self.reads:
            out |= r.families
        for p in self.picked:
            out |= INTENT_FAMILIES.get(p, {p})
        return out

    @property
    def composite(self) -> bool:
        return "composite" in self.families

    def reads_with(self, *fams: str) -> list[ClauseRead]:
        return [r for r in self.reads if r.families & set(fams)]

    # -- the judge ---------------------------------------------------------
    def judge(self, before: EDL, after: EDL) -> list[Violation]:
        if "history" in self.families:
            return []
        d = diff(before, after)
        from .contract_rules import RULES
        ctx = _Ctx(self, d, before, after)
        out: list[Violation] = []
        for rule in RULES:
            try:
                out.extend(rule(ctx))
            except Exception as e:  # noqa: BLE001 — a broken rule must never block a correct edit
                ctx.errors.append(f"{rule.__name__}: {type(e).__name__}: {e}")
        self.errors = list(ctx.errors)
        seen: set[tuple[str, str]] = set()
        uniq = []
        for v in out:
            if (v.kind, v.message) not in seen:
                seen.add((v.kind, v.message))
                uniq.append(v)
        return uniq


# --------------------------------------------------------------------------
# 3. Judging context: targets resolved against the BEFORE timeline
# --------------------------------------------------------------------------

@dataclass
class _Ctx:
    c: Contract
    d: Diff
    before: EDL
    after: EDL
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.v1 = media_v1(self.before)
        self.ids = [x.id for x in self.v1]

    def resolve(self, ref: M.ClipRef) -> str | None:
        n = len(self.v1)
        if not n:
            return None
        if ref == "sel":
            return self.c.selection if self.c.selection in self.ids else None
        if ref == "mid":
            return self.ids[n // 2] if n % 2 == 1 else None
        if ref == "playhead":
            if self.c.playhead is None:
                return None
            ref = f"at:{float(self.c.playhead):g}"
        if isinstance(ref, str) and ref.startswith("at:"):
            t = float(ref[3:])
            hit = [x.id for x in self.v1 if x.start - 1e-6 <= t < x.start + x.effective_duration - 1e-6]
            return hit[0] if hit else None
        if isinstance(ref, int):
            i = ref - 1 if ref > 0 else n + ref
            return self.ids[i] if 0 <= i < n else None
        return None

    def targets(self, r: ClauseRead) -> list[set[str]] | None:
        """Alternative target sets (any one of them satisfies the clause);
        None when the clause's clips cannot be resolved."""
        sc = r.scope
        if re.search(r"\b(?:first|second|other|last|latter|right|left|back|front)\s+(?:half|part|piece)\b", r.t):
            return None       # a piece a split in this prompt makes: its id exists only at run time
        ex = {self.resolve(x) for x in sc.except_refs}
        if sc.refs:
            got = [self.resolve(x) for x in sc.refs]
            if any(g is None for g in got):
                return None
            return [set(got)]  # type: ignore[arg-type]
        if sc.all:
            return [set(self.ids) - ex]
        alts = [set(self.ids)]
        if self.c.selection in self.ids:
            alts.append({self.c.selection})
        return alts

    def explicit(self, r: ClauseRead) -> bool:
        return bool(r.scope.refs) or r.scope.all

    def delta(self, cid: str) -> ClipDelta | None:
        return self.d.clip(cid)


def _msg_clip(ctx: _Ctx, cid: str) -> str:
    return f"clip {ctx.ids.index(cid) + 1}" if cid in ctx.ids else "a clip"


# --------------------------------------------------------------------------
# 4. Licensing: every kind of change needs a clause that asks for it
# --------------------------------------------------------------------------

def _has(ctx: _Ctx, *fams: str) -> bool:
    return bool(ctx.c.families & set(fams)) or ctx.c.composite


def _text_media_reads(ctx: _Ctx) -> list[ClauseRead]:
    # final sweep 2 r2: "make Day One purple" names a text by its own words
    names = [n for t in ctx.before.tracks for c in t.clips if isinstance(c, TextClip)
             for n in [M.norm(c.text.split("\n")[0])] if len(n) >= 3]
    return [r for r in ctx.c.reads if "text" in r.families or "brand" in r.families or "hook" in r.families
            or any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", r.t) for n in names)]


def _v1_retimed(ctx: _Ctx) -> bool:
    cats = ctx.d.categories
    return bool(cats & {"v1:cut", "v1:move", "v1:duplicate", "v1:freeze", "v1:delete", "clip:speed", "clip:cut",
                        "transitions:add", "transitions:remove", "transitions:duration", "clip:speed_shape"})


def _look_words_only(text: str) -> bool:
    """A new text whose every word describes a look or a place ("red",
    "at the top", "font to Anton") — a misread restyle, never a title."""
    from .planner import _STYLE_WORD_RE
    toks = [t for t in re.split(r"[\s,]+", (text or "").lower().strip(" .!?")) if t]
    return bool(toks) and all(_STYLE_WORD_RE.fullmatch(t) for t in toks)


def _lic_text_add(ctx: _Ctx) -> bool:
    if any(_look_words_only(t.text) or re.search(r"\b(?:saying|that says|reading)\b", t.text, re.I)
           for t in ctx.d.texts.get("added", [])):
        return False
    for r in _text_media_reads(ctx):
        if r.families & {"brand", "hook"}:
            return True
        if _TEXT_ADD_RE.search(r.text) and not (_TEXT_RESTYLE_RE.search(r.t) and not re.search(
                r"\b(?:add|put|insert|write|create|new|another)\b|[\"“”']", r.text)):
            return True
    return ctx.c.composite or bool({"title", "hook", "brand", "end_card"} & set(ctx.c.picked))


def _lic_text_remove(ctx: _Ctx) -> bool:
    if ctx.c.composite:
        return True
    for r in _text_media_reads(ctx):
        if _TEXT_REMOVE_RE.search(r.t) or re.search(r"\breplace\b", r.t):
            return True
    # a v1 cut drops text that sat wholly inside the removed span (ripple)
    return bool(ctx.d.categories & {"v1:cut", "v1:delete"})


def _lic_text_content(ctx: _Ctx) -> bool:
    if ctx.c.composite or "retext" in ctx.c.picked:
        return True
    said = M.norm(ctx.c.prompt)
    if re.search(r"\b(?:change|rename|replace|edit|fix|update|correct|swap)\b.+\b(?:to|into|with|for)\b", said) and any(
            len(t.text.strip()) >= 2 and M.norm(t.text.split("\n")[0]) in said for t, _a in ctx.d.texts.get("retexted", [])):
        return True
    return any(_TEXT_RETEXT_RE.search(r.text) or re.search(r"[\"“”']", r.text) for r in _text_media_reads(ctx))


def _lic_text_style(ctx: _Ctx) -> bool:
    if ctx.c.composite or "canvas:size" in ctx.d.categories:
        return True
    return any(_TEXT_RESTYLE_RE.search(r.t) or r.families & {"animation", "brand"} for r in _text_media_reads(ctx)) \
        or bool(set(ctx.c.picked) & {"title", "retext"})


def _lic_text_time(ctx: _Ctx) -> bool:
    if ctx.c.composite or _v1_retimed(ctx):
        return True
    return any(_TEXT_RETIME_RE.search(r.t) for r in _text_media_reads(ctx)) or "title" in ctx.c.picked


def _lic_level_clip(ctx: _Ctx) -> bool:
    if ctx.c.composite or _has(ctx, "noise", "loudness"):
        return True
    for r in ctx.c.reads_with("level"):
        media = set(r.scope.media)
        if r.scope.refs or "voice" in media or "vo" in media or not (media & {"music", "captions", "text"}):
            return True
        if r.scope.all:
            return True
    return "volume" in ctx.c.picked


def _lic_level_music(ctx: _Ctx) -> bool:
    if ctx.c.composite:
        return True
    for r in ctx.c.reads_with("level", "music", "duck"):
        media = set(r.scope.media)
        if "music" in r.scope.except_media:
            continue                   # "turn down all the clips except the music"
        if "music" in media or ("level" in r.families and not r.scope.refs and not (media & {"voice", "vo"})):
            return True
    return bool({"volume", "music"} & set(ctx.c.picked))


def _lic_loudness(ctx: _Ctx) -> bool:
    if ctx.c.composite or _has(ctx, "loudness", "export", "noise"):
        return True
    for r in ctx.c.reads_with("level"):
        if not r.scope.refs and not (set(r.scope.media) & {"music", "vo", "captions", "text", "overlay"}):
            return True
    return bool({"volume", "loudness"} & set(ctx.c.picked))


def _lic_music_mute(ctx: _Ctx) -> bool:
    for r in ctx.c.reads_with("mute", "music"):
        if "music" in r.scope.media and not _negated(r.text, "music|song|soundtrack") and not re.search(
                r"\b(?:keep|but|except|apart from|other than|besides)\s+(?:the\s+)?(?:music|song|soundtrack)", r.t):
            return True
        if "mute" in r.families and r.scope.all and not re.search(
                r"\b(?:but|except|apart from|other than|besides|keep)\b.*\b(?:music|song|soundtrack)\b", r.t):
            return True
    return ctx.c.composite or bool({"mute", "remove_music"} & set(ctx.c.picked))


def _lic_music_structure(ctx: _Ctx) -> bool:
    if ctx.c.composite or _v1_retimed(ctx) or _has(ctx, "music", "cut"):
        return True
    return False


_CAPTIONS_REMOVED_RE = re.compile(r"\b(?:remove|delete|get rid of|clear|lose|ditch|kill|drop|strip|take (?:off|out)"
                                  r"|turn off|hide)\b[^,;.]*\b(?:captions?|subtitles?|subs)\b")
_CAPTIONS_ADDED_RE = re.compile(r"\b(?:add|generate|create|put|burn|redo|regenerate|re-?transcribe|caption (?:it|this)"
                                r"|new|translate)\b")


def _lic_captions_add(ctx: _Ctx) -> bool:
    if ctx.c.composite or "captions" in ctx.c.picked or "translate_captions" in ctx.c.picked:
        return True
    said = M.norm(ctx.c.prompt)
    if _CAPTIONS_REMOVED_RE.search(said) and not _CAPTIONS_ADDED_RE.search(said):
        return False              # "remove the music and the captions" re-laid the captions
    return any("captions" in r.families and not _negated(r.text, r"captions?|subtitles?|subs") for r in ctx.c.reads)


_PICTURE_RE = re.compile(r"\b(?:video|clips?|picture|image|footage|shots?|screen|black|visuals?|scene|voice|audio"
                         r"|sound)\b")


def _fades_music_only(ctx: _Ctx) -> bool:
    """Every fade clause is about the MUSIC — by its own words, or as "fade
    it out" / "fade out 3s" right after a clause about the music. Then a
    fade on a main-track clip is unasked ("music fade in 1s and fade out 3s"
    faded the last clip's picture and sound)."""
    reads = ctx.c.reads_with("fade")
    if not reads or ctx.c.composite:
        return False
    prev_media: tuple[str, ...] = ()
    for r in ctx.c.reads:
        media = r.scope.media
        if r in reads:
            if "music" in media:
                pass
            elif media or r.scope.refs or r.scope.all or _PICTURE_RE.search(r.t) or not prev_media \
                    or prev_media[0] != "music":
                return False
        if media or r.scope.refs:
            prev_media = media or ("v1",)
    return True


#: category → license predicate. A category not listed is licensed.
_LICENSE: dict[str, Callable[[_Ctx], bool]] = {
    "clip:speed": lambda x: _has(x, "speed") or (_has(x, "cut") and all(
        isinstance(dl.before.speed, dict) for dl in x.d.changed_clips("speed"))),
    "clip:speed_shape": lambda x: _has(x, "speed", "cut", "split", "freeze"),
    "clip:reverse": lambda x: _has(x, "reverse"),
    "clip:mute": lambda x: _has(x, "mute"),
    "clip:gain": _lic_level_clip,
    "clip:look": lambda x: _has(x, "look", "adjust", "effect"),
    "clip:effects": lambda x: _has(x, "look", "adjust", "effect", "stabilize", "upscale", "noise"),
    "clip:zoom": lambda x: _has(x, "zoom", "canvas", "export", "animation", "rotate", "hook"),
    "clip:rotate": lambda x: _has(x, "rotate", "canvas"),
    "clip:position": lambda x: _has(x, "zoom", "canvas", "export", "rotate"),
    "clip:opacity": lambda x: _has(x, "fade", "blend", "look", "adjust"),
    "clip:flip": lambda x: _has(x, "flip", "rotate"),
    "clip:fade": lambda x: _has(x, "fade", "transition") and not _fades_music_only(x),
    "clip:audio_fade": lambda x: _has(x, "fade", "transition", "music", "level", "hook", "brand")
    and not _fades_music_only(x),
    "clip:anim": lambda x: _has(x, "animation", "zoom", "fade"),
    "clip:voice_fx": lambda x: _has(x, "voice_fx"),
    "clip:canvas_bg": lambda x: _has(x, "canvas_bg", "canvas"),
    "clip:fit": lambda x: _has(x, "canvas", "canvas_bg", "export", "zoom"),
    "clip:blend": lambda x: _has(x, "blend"),
    "clip:src": lambda x: _has(x, "noise", "stabilize", "upscale", "smooth", "speed", "canvas", "export"),
    "clip:framing": lambda x: _has(x, "canvas", "zoom", "export"),
    "clip:mask": lambda x: _has(x, "canvas", "look"),
    "clip:chroma": lambda x: _has(x, "look"),
    "clip:freeze": lambda x: _has(x, "freeze"),
    "clip:cut": lambda x: _has(x, "cut", "split", "speed", "freeze", "duplicate", "order"),
    "v1:cut": lambda x: _has(x, "cut", "speed"),
    "v1:delete": lambda x: _has(x, "cut", "speed"),
    "v1:duplicate": lambda x: _has(x, "duplicate", "freeze"),
    "v1:new_source": lambda x: _has(x, "noise", "stabilize", "upscale", "smooth"),
    "v1:extend": lambda x: _has(x, "cut", "speed"),
    "v1:freeze": lambda x: _has(x, "freeze"),
    "v1:move": lambda x: _has(x, "order", "duplicate", "cut"),
    "v1:split": lambda x: True,
    "v1:track": lambda x: _has(x, "mute", "level"),
    "transitions:add": lambda x: _has(x, "transition", "fade"),
    "transitions:remove": lambda x: _has(x, "transition", "cut", "order", "fade") or bool(
        x.d.categories & {"v1:cut", "v1:delete", "v1:move", "v1:duplicate", "v1:freeze", "v1:split", "clip:speed"}),
    "transitions:type": lambda x: _has(x, "transition"),
    "transitions:duration": lambda x: _has(x, "transition"),
    "transitions:moved": lambda x: _v1_retimed(x) or _has(x, "transition"),
    "music:gain": _lic_level_music,
    "music:mute": _lic_music_mute,
    "music:fade": lambda x: _has(x, "fade", "music") or _v1_retimed(x),
    "music:voice_fx": lambda x: _has(x, "voice_fx"),
    "music:speed": lambda x: False,
    "music:trim": _lic_music_structure,
    "music:remove": lambda x: _has(x, "music"),
    "music:add": lambda x: _has(x, "music"),
    "music:duck": lambda x: _has(x, "duck", "music", "level"),
    "vo:change": lambda x: _has(x, "vo") or any(
        "vo" in r.scope.media or ("voice" in r.scope.media and r.families & {"voice_fx", "level", "mute"})
        or ("mute" in r.families and r.scope.all) for r in x.c.reads),
    "overlay:change": lambda x: _has(x, "blend") or any("overlay" in r.scope.media for r in x.c.reads)
    or x.c.composite,
    "sticker:change": lambda x: any("sticker" in r.scope.media for r in x.c.reads) or _v1_retimed(x),
    "text:add": _lic_text_add,
    "text:remove": _lic_text_remove,
    "text:content": _lic_text_content,
    "text:style": _lic_text_style,
    "text:time": _lic_text_time,
    "text:position": lambda x: _lic_text_style(x) or _has(x, "canvas"),
    "captions:add": _lic_captions_add,
    "captions:remove": lambda x: _has(x, "captions") or bool(x.d.categories & {"v1:cut", "v1:delete"}),
    "captions:text": lambda x: _has(x, "captions", "cut") or _v1_retimed(x),
    "captions:time": lambda x: _has(x, "captions") or _v1_retimed(x),
    "captions:style": lambda x: _has(x, "captions", "canvas"),
    "canvas:size": lambda x: _has(x, "canvas", "export"),
    "canvas:fps": lambda x: _has(x, "export", "canvas"),
    "canvas:loudness": _lic_loudness,
    "canvas:export": lambda x: _has(x, "export"),
}

_CATEGORY_WORDS: dict[str, str] = {
    "clip:speed": "changed a clip's speed", "clip:reverse": "reversed a clip", "clip:mute": "muted a clip",
    "clip:gain": "changed a clip's volume", "clip:look": "added a colour look", "clip:effects": "added an effect",
    "clip:zoom": "zoomed a clip", "clip:rotate": "rotated a clip", "clip:flip": "flipped a clip",
    "clip:fade": "added a fade", "clip:audio_fade": "added a sound fade", "clip:voice_fx": "added a voice effect",
    "clip:src": "re-processed the footage (noise reduction / stabilising)", "clip:anim": "added an animation",
    "clip:canvas_bg": "changed a canvas background", "clip:freeze": "added a freeze frame",
    "clip:cut": "cut a clip", "clip:fit": "changed how a clip fills the frame", "clip:position": "moved a clip",
    "v1:cut": "cut footage from the video", "v1:delete": "deleted a clip", "v1:duplicate": "duplicated a clip",
    "v1:move": "reordered the clips", "v1:freeze": "added a freeze frame", "v1:extend": "lengthened a clip",
    "v1:new_source": "added new footage", "transitions:add": "added transitions",
    "transitions:remove": "removed transitions", "transitions:type": "changed the transitions",
    "transitions:duration": "changed the transitions' length", "music:gain": "changed the music's volume",
    "music:mute": "muted the music", "music:remove": "removed the music", "music:add": "added music",
    "music:speed": "changed the music's speed", "music:fade": "faded the music", "music:trim": "trimmed the music",
    "music:duck": "changed the ducking", "vo:change": "added or changed a voice-over", "text:add": "added a new title",
    "text:remove": "removed a title", "text:content": "changed a title's words", "text:style": "restyled a title",
    "text:time": "moved a title in time", "text:position": "moved a title", "captions:add": "added captions",
    "captions:remove": "removed the captions", "captions:text": "changed the captions' words",
    "captions:style": "restyled the captions", "canvas:size": "changed the frame size",
    "canvas:loudness": "changed the overall loudness", "canvas:export": "changed the export settings",
    "overlay:change": "changed the overlay clip", "sticker:change": "changed a sticker",
}


def _rule_licensed(ctx: _Ctx) -> list[Violation]:
    out = []
    for cat in sorted(ctx.d.categories):
        lic = _LICENSE.get(cat)
        if lic is None or lic(ctx):
            continue
        out.append(Violation("unasked", ctx.c.prompt,
                             f"it {_CATEGORY_WORDS.get(cat, 'changed ' + cat.replace(':', ' '))}, which the request "
                             "did not ask for"))
    return out


# --------------------------------------------------------------------------
# 7. The reply after a rollback
# --------------------------------------------------------------------------

def rollback_question(messages: list[str]) -> str:
    """"I undid that — <what went wrong>. Which of these did you mean?" — at
    most two reasons, plain words, never check names; ≤ 200 characters (the
    wire schema's question length)."""
    body = "; ".join(m.rstrip(" .?!") for m in messages[:2] if m) or "the result did not match the request"
    body = body[0].upper() + body[1:]
    tail = " Nothing was changed. Which did you mean?"
    room = 200 - len("I undid that: ") - len(tail)
    if len(body) > room:
        body = body[:room - 1].rstrip(" ,;—") + "…"
    return f"I undid that: {body}.{tail}".replace("…." , "…")


__all__ = ["Contract", "ClauseRead", "Violation", "families_of", "rollback_question", "INTENT_FAMILIES"]
