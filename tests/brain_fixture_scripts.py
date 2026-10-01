"""The scripts of the Editor Brain fixtures (EB1-A): what is said, by whom,
with what planted property. Pure data; `brain_fixtures.py` synthesises it.

Talking head (TH, Piper amy, deterministic): 14 sentence utterances (13
distinct texts — S04 is spoken twice, the retake), one quotable claim with a
strong number and a superlative (S08), one throwaway with a year and "three
people" (S06), one sentence rendered +6 dB and ×1.15 slower whose comma
clause precedes the peak (S07, synthesised as two clauses so the clause
start is exact), one question (S10), six `um`/`umm` (whisper-small hears
them for this voice), three `uh` islands (0.2-0.4 s; whisper-small drops
them — measured in the narration module's docstring and pinned by
`test_th_truth_has_three_acoustic_fillers_whisper_drops`), five 1.5 s
pauses, and a closing "thanks for watching".

Two-camera podcast (P2, host = macOS `say` Daniel, guest = Piper amy): 40
turns — 4 backchannels under 0.6 s (T03 T09 host, T16 T26 guest), two `um`
per speaker (T06 T14 guest; T21 T29 host), one false start (T18), one 2.4 s
in-turn pause (T06), two 1.8 s turn-boundary silences (before T23, T33), one
1.0 s pause after an emotional line that must be kept (T20), one 0.8 s
overlap (T19 starts before T18 ends), a quotable line (T12), a throwaway
year-and-headcount line (T02), 12 question → answer pairs.

Feature flags are hand-annotated by the spec §3.4 definitions: a
`strong_number` is ≥ 10, a percentage, a currency amount or a number paired
with a comparative/superlative within 4 words; a `weak_number` is a year, a
date or a count < 10 without such a pairing; `contrast_words` counts the
`_sentence_score` list (never/nobody/secret/mistake/wrong/best/worst/only/
stop); `claim` = a declarative assertion about the subject; `weak_start` =
a greeting/pleasantry opener; `anaphora_start` = a sentence-initial pronoun
or demonstrative referring back; `conjunction_start` = so/because/but/and.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# talking head
# --------------------------------------------------------------------------

TH_FPS = 30
TH_PAUSE_S = 1.5
TH_RETAKE_GAP_S = 0.8
#: An acoustic-filler island sits 0.25 s after the sentence before it and
#: 0.35 s before the next one, 4 dB under the speech RMS (see brain_fixtures._schwa).
#: The gaps are MEASURED, not chosen for looks: whisper.cpp (the upload path's
#: backend) and faster-whisper both drop the schwa as a token, but each times
#: a NEIGHBOURING word onto it for most spacings (the DTW onset of the word
#: after / before falls in the silence and `transcribe._refine_words` snaps it
#: to the first voiced frame — the island). Of 14 spacings probed on 2026-09-29
#: only (0.20, 0.30) and (0.25, 0.35) leave both backends with no word over
#: any island; (0.4, 0.5) leaves whisper.cpp clean at two islands out of three.
TH_ISLAND_GAP_S = 0.25
TH_ISLAND_GAP_AFTER_S = 0.35
TH_ISLAND_GAIN_DB = -4.0
#: Every spoken part is levelled to this RMS over its voiced span (Piper
#: normalises each call to PEAK 1.0, which would make short lines louder);
#: the emphasised sentence sits `TH_EMPH_GAIN_DB` above it, an island
#: `TH_ISLAND_GAIN_DB` below. Speech then peaks under 0.5 (clicks are 0.95).
TH_SPEECH_RMS_DB = -29.0
TH_BASE_DB = 0.0
TH_EMPH_GAIN_DB = 6.0
TH_EMPH_SCALE = 1.25
TH_SIDS = {"16x9": 1, "9x16": 2}
#: Six scene colours (Y, U, V) for the strip, one per scene between the cuts.
TH_SCENE_YUV = ((110, 180, 80), (150, 60, 190), (120, 200, 120), (170, 100, 60), (90, 160, 170), (140, 120, 200))


def _f(len_words: int, **flags) -> dict:
    base = {"len_words": len_words, "strong_number": False, "weak_number": False, "superlative": False,
            "claim": False, "contrast_words": 0, "conclusion_marker": False, "imperative": False,
            "anaphora_start": False, "conjunction_start": False, "weak_start": False, "comma_clause": False}
    base.update(flags)
    return base


#: (sentence key, kind, text, features). The key groups utterances (S07 has two).
TH_SENTENCES: dict[str, tuple[str, str, dict]] = {
    "S01": ("statement", "Hey everyone, today I want to talk about the one habit that changed how I work.",
            _f(15, weak_start=True, comma_clause=True)),
    "S02": ("statement", "It is not an app, and it is not a fancy notebook.", _f(11, anaphora_start=True, comma_clause=True)),
    "S03": ("statement", "Every morning, I write the day's plan on a single index card.", _f(11, comma_clause=True)),
    "S04": ("retake", "The card has room for a handful of things, and that limit is the point.", _f(14, claim=True, comma_clause=True)),
    "S05": ("retake_dup", "The card has room for a handful of things, and that limit is the point.", _f(14, claim=True, comma_clause=True)),
    "S06": ("throwaway", "I started this in 2019 with three people in a shared office.", _f(11, weak_number=True)),
    "S07": ("emphasis", "When the card is full, the important decisions are already finished.",
            _f(11, claim=True, comma_clause=True)),
    "S08": ("quotable", "I now finish forty percent more of what I plan, the best stretch of my working life.",
            _f(17, strong_number=True, superlative=True, claim=True, contrast_words=1, comma_clause=True)),
    "S09": ("statement", "Some days I still fall behind, and the card just waits for tomorrow.", _f(13, comma_clause=True)),
    "S10": ("question", "So why does something this small work?", _f(7, conjunction_start=True)),
    "S11": ("statement", "Because it forces a choice before the day makes it for you.", _f(11, claim=True, conjunction_start=True)),
    "S12": ("contrast", "Nobody needs another productivity system.", _f(5, claim=True, contrast_words=1)),
    "S13": ("imperative", "Try it for a week and tell me what happens.", _f(9, imperative=True)),
    "S14": ("closing", "That is all for today, and thanks for watching.", _f(9, anaphora_start=True, comma_clause=True)),
}
TH_EMPHASIS_CLAUSES = ("When the card is full,", "the important decisions are already finished.")


def _th_sequence() -> tuple[tuple[tuple[str, str], ...], dict[int, str], dict[int, float], dict[int, float]]:
    items: list[tuple[str, str]] = []
    roles: dict[int, str] = {}
    gains: dict[int, float] = {}
    scales: dict[int, float] = {}

    def add(kind: str, text: str, role: str | None = None, *, gain: float | None = None, scale: float | None = None) -> None:
        idx = len(items)
        items.append((kind, text))
        if role:
            roles[idx] = role
        if kind in ("speech", "filler"):
            gains[idx] = TH_BASE_DB if gain is None else gain
        if scale is not None:
            scales[idx] = scale

    def sent(key: str) -> None:
        add("speech", TH_SENTENCES[key][1], key)

    def filler(tok: str) -> None:
        add("gap", ""), add("filler", tok, tok), add("gap", "")

    def island() -> None:
        add("silence", str(TH_ISLAND_GAP_S))
        add("filler", "uh", "uh", gain=TH_BASE_DB + TH_ISLAND_GAIN_DB)
        add("silence", str(TH_ISLAND_GAP_AFTER_S))

    def pause() -> None:
        add("silence", str(TH_PAUSE_S), "planted")

    sent("S01"); filler("um")
    sent("S02"); filler("um")
    sent("S03"); pause()
    sent("S04"); add("silence", str(TH_RETAKE_GAP_S), "retake_gap"); sent("S05"); island()
    sent("S06"); pause(); add("filler", "umm", "umm"); add("gap", "")
    add("speech", TH_EMPHASIS_CLAUSES[0], "S07", gain=TH_BASE_DB + TH_EMPH_GAIN_DB, scale=TH_EMPH_SCALE)
    add("gap", "")
    add("speech", TH_EMPHASIS_CLAUSES[1], "S07", gain=TH_BASE_DB + TH_EMPH_GAIN_DB, scale=TH_EMPH_SCALE)
    pause()
    sent("S08"); filler("um")
    sent("S09"); island()
    sent("S10"); pause()
    sent("S11"); filler("um")
    sent("S12"); island()
    sent("S13"); pause(); add("filler", "umm", "umm"); add("gap", "")
    sent("S14")
    return tuple(items), roles, gains, scales


TH_SCRIPT, TH_ROLES, TH_GAINS, TH_SCALES = _th_sequence()


# --------------------------------------------------------------------------
# two-camera podcast
# --------------------------------------------------------------------------

P2_FPS = 20
P2_SR = 44_100
P2_TURN_GAP_S = 0.45
P2_LONG_GAP_S = 1.8
P2_IN_TURN_PAUSE_S = 2.4
P2_EMOTION_PAUSE_S = 1.0
P2_OVERLAP_S = 0.8
P2_FALSE_START_GAP_S = 0.35
P2_ITEM_GAP_S = 0.12
#: RMS of every spoken part over its voiced span (dBFS), both speakers
P2_SPEECH_RMS_DB = -29.0
P2_EMOTION_GAIN_DB = 6.0
P2_EMOTION_SCALE = 1.25
P2_OFFSETS = {"recorder": 0.0, "cam_a": 0.35, "cam_b": -0.2}
P2_MIC = {"gain_db": -12.0, "other_speaker_db": -6.0, "echo_ms": 40.0, "echo_db": -6.0, "noise_dbfs": -55.0}
P2_SIDS = {"cam_a": 1, "cam_b": 2}
P2_OWN_MIC = {"cam_a": "S1", "cam_b": "S2"}
P2_SPEAKERS = {"S1": {"role": "host", "voice": "say:Daniel", "angle": "cam_a"},
               "S2": {"role": "guest", "voice": "en_US-amy-medium", "angle": "cam_b"}}
P2_STRIP_YUV = {"S1": (160, 70, 180), "S2": (90, 190, 100)}
#: `say -r` for backchannels (Daniel's default ≈ 175 wpm; "Okay." at 230 is 0.44 s voiced)
P2_HOST_RATE = 175
P2_HOST_BC_RATE = 230
P2_GUEST_BC_SCALE = {"Yeah.": 0.9, "Mm-hm.": 0.45}


@dataclass(frozen=True)
class Item:
    kind: str            # s (sentence) | f (filler) | bc (backchannel) | fs (false start) | sil (in-turn silence)
    text: str = ""
    skind: str = "statement"
    features: dict = field(default_factory=dict)
    seconds: float = 0.0
    gain_db: float = 0.0
    length_scale: float | None = None
    protection: str | None = None


@dataclass(frozen=True)
class TurnSpec:
    speaker: str
    kind: str            # q | a | s | bc
    items: tuple[Item, ...]
    gap_before: float | None = None
    overlap_s: float = 0.0


def S(text: str, skind: str = "statement", n: int = 0, **flags) -> Item:
    return Item("s", text, skind, _f(n or len(text.split()), **flags))


def Q(text: str, n: int = 0, **flags) -> Item:
    return S(text, "question", n, **flags)


def F(tok: str = "um") -> Item:
    return Item("f", tok, "filler")


def BC(tok: str) -> Item:
    return Item("bc", tok, "backchannel", _f(len(tok.split())))


def SIL(seconds: float, protection: str | None = None) -> Item:
    return Item("sil", "", "silence", seconds=seconds, protection=protection)


def _turn(spk: str, kind: str, *items: Item, gap_before: float | None = None, overlap_s: float = 0.0) -> TurnSpec:
    return TurnSpec(spk, kind, tuple(items), gap_before, overlap_s)


P2_TURNS: tuple[TurnSpec, ...] = (
    _turn("S1", "q", S("Welcome back to the show.", weak_start=True),
          S("Today I am talking to a founder who builds cameras in a garage."),
          Q("How did this start?")),
    _turn("S2", "a", S("We started in 2019 with three people and one soldering iron.", "throwaway", weak_number=True),
          S("Nobody thought a garage team could ship a camera.", "contrast", claim=True, contrast_words=1)),
    _turn("S1", "bc", BC("Okay.")),
    _turn("S2", "s", S("The first prototype was heavy and the battery barely lasted a coffee break.")),
    _turn("S1", "q", Q("So what changed?", conjunction_start=True)),
    _turn("S2", "a", F("um"), S("We threw away the case and started again from the sensor."),
          SIL(P2_IN_TURN_PAUSE_S), S("That was the hardest month of the whole project.", claim=True, superlative=True, anaphora_start=True)),
    _turn("S1", "q", Q("Why the sensor first?")),
    _turn("S2", "a", S("Because everything else follows from it.", conjunction_start=True, claim=True),
          S("The lens, the heat, the size of the body.", comma_clause=True)),
    _turn("S1", "bc", BC("Right.")),
    _turn("S2", "s", S("Once the sensor was fixed, the case almost designed itself.", comma_clause=True)),
    _turn("S1", "q", S("Let's talk about the number everyone quotes."), Q("What did the second prototype do?")),
    _turn("S2", "a", S("Our second prototype shot ninety minutes of four K on one charge, the longest in its class.", "quotable",
                       strong_number=True, superlative=True, claim=True, comma_clause=True)),
    _turn("S1", "q", S("That is a big claim.", anaphora_start=True), Q("How did you measure it?")),
    _turn("S2", "a", F("um"), S("We ran it on a bench with a fan pointed at the sensor, then again without the fan.", comma_clause=True)),
    _turn("S1", "s", S("Most teams would have stopped at the bench test.", claim=True)),
    _turn("S2", "bc", BC("Yeah.")),
    _turn("S1", "q", S("You did not."), Q("What did the field test look like?")),
    _turn("S2", "a", Item("fs", "So the second", "false_start"),
          S("So the second test was a week on a fishing boat in the rain.", conjunction_start=True)),
    _turn("S1", "s", S("A fishing boat."), S("That sounds miserable.", anaphora_start=True), overlap_s=P2_OVERLAP_S),
    _turn("S2", "s", Item("s", "It was the week I almost quit, and I still think about it every morning.", "emotional",
                          _f(15, anaphora_start=True, comma_clause=True, claim=True),
                          gain_db=P2_EMOTION_GAIN_DB, length_scale=P2_EMOTION_SCALE),
          SIL(P2_EMOTION_PAUSE_S, protection="emotion"),
          S("But the camera came back working, and that changed everything.", conjunction_start=True, comma_clause=True, claim=True)),
    _turn("S1", "q", F("um"), Q("What did the boat teach you about the design?")),
    _turn("S2", "a", S("Salt gets everywhere.", claim=True), S("Every seam, every button, every port needs a gasket.", claim=True, comma_clause=True)),
    _turn("S1", "q", S("Let's move to the team."), Q("How do you hire for something this specialised?"), gap_before=P2_LONG_GAP_S),
    _turn("S2", "a", S("Slowly."), S("We hire people who have shipped one hard thing, whatever it was.", comma_clause=True)),
    _turn("S1", "s", S("That rules out most fresh graduates.", anaphora_start=True)),
    _turn("S2", "bc", BC("Mm-hm.")),
    _turn("S1", "q", Q("Does it?"), Q("Who was the best hire you made?", contrast_words=1, superlative=True)),
    _turn("S2", "a", S("A retired watchmaker."), S("She taught us that tolerance is a habit, not a number.", claim=True, comma_clause=True)),
    _turn("S1", "s", F("um"), S("I love that."), S("Let's talk about money.")),
    _turn("S2", "s", S("Money was the boring part."), S("We sold prototypes to pay for the next one.")),
    _turn("S1", "q", Q("No investors at all?")),
    _turn("S2", "a", S("Not until the boat test proved the design.")),
    _turn("S1", "q", Q("What is next for the camera?"), gap_before=P2_LONG_GAP_S),
    _turn("S2", "a", S("A smaller body and a better screen."), S("The sensor stays.", claim=True)),
    _turn("S1", "s", S("So the hard part is done.", conjunction_start=True)),
    _turn("S2", "s", S("The hard part is never done.", "contrast", claim=True, contrast_words=1), S("It just moves.", anaphora_start=True)),
    _turn("S1", "q", S("Last question."), Q("What would you tell someone starting in a garage today?")),
    _turn("S2", "a", S("Ship one thing that works before you design the second thing.", "imperative", imperative=True, claim=True)),
    _turn("S1", "s", S("That is a good place to end.", anaphora_start=True), S("Thank you for coming.")),
    _turn("S2", "s", S("Thank you for having me.", "closing")),
)

assert len(P2_TURNS) == 40

__all__ = ["TH_FPS", "TH_PAUSE_S", "TH_RETAKE_GAP_S", "TH_ISLAND_GAP_S", "TH_ISLAND_GAP_AFTER_S", "TH_ISLAND_GAIN_DB", "TH_BASE_DB", "TH_SPEECH_RMS_DB", "TH_EMPH_GAIN_DB",
           "TH_EMPH_SCALE", "TH_SIDS", "TH_SCENE_YUV", "TH_SENTENCES", "TH_EMPHASIS_CLAUSES", "TH_SCRIPT",
           "TH_ROLES", "TH_GAINS", "TH_SCALES", "P2_FPS", "P2_SR", "P2_TURN_GAP_S", "P2_LONG_GAP_S",
           "P2_IN_TURN_PAUSE_S", "P2_EMOTION_PAUSE_S", "P2_OVERLAP_S", "P2_FALSE_START_GAP_S", "P2_ITEM_GAP_S",
           "P2_SPEECH_RMS_DB", "P2_EMOTION_GAIN_DB", "P2_EMOTION_SCALE", "P2_OFFSETS", "P2_MIC", "P2_SIDS",
           "P2_OWN_MIC", "P2_SPEAKERS", "P2_STRIP_YUV", "P2_HOST_RATE", "P2_HOST_BC_RATE", "P2_GUEST_BC_SCALE",
           "Item", "TurnSpec", "P2_TURNS"]
