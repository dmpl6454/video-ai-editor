"""CapCut's voice changer from a key-free prompt (wave E, lane F3).

"make my voice sound like a robot", "add echo to the voiceover", "chipmunk
voice on the second clip", "a little reverb on the music", "remove the voice
effect". The ONE table is `edl/voice_effects.py`; the tool is
`set_voice_effect`; the words are `voice_vocab.py`.

The rules `clip_expanders` keeps hold here too:

  * Everything is bound at plan time from `TimelineFacts` (`clips` carries
    every media clip's lane): a named clip, a named lane ("the voiceover" →
    the `vo` lane, "the music" → the bed), the selected clip, or — for "my
    voice" / no target — the voice-over lane when it has clips, else every
    main-track clip (the talking clip). A whole lane is ONE step (one undo).
  * What the prompt leaves open is ASKED, never guessed: an effect that is
    not named ("change my voice" → which of the eleven), a lane with nothing
    on it, a clip reference that does not resolve. The expansion is then a
    question with no steps, so the plan commits nothing.
  * The step carries `voice_effect_is`, measured on the EDL the renderer
    reads (the render tests decode the sound).
"""
from __future__ import annotations

import re
from typing import Any

from ...edl import voice_effects as VFX
from . import grammar as G
from . import slots as S
from .clip_expanders import _label, bind_clip
from .facts import TimelineFacts
from .recipes import Context, Expansion, Intent, pc, step
from .schema import STAGE_AUDIO
from .voice_vocab import AMBIGUOUS, WORD_TO_ID

# --------------------------------------------------------------------------
# 1. Reading a clause (clip_slots.READERS)
# --------------------------------------------------------------------------

_WORDS = sorted(WORD_TO_ID, key=len, reverse=True)
_WORD_RE = [(w, re.compile(r"\b" + re.escape(w).replace(r"\ ", r"[\s-]+") + r"\b")) for w in _WORDS]
_VOICE_CONTEXT = re.compile(r"\bvoice|\bvocal|\bsound(?:s|ing)?\s+(?:like|as if)|\beffect|\bfilter")
_OFF_RE = re.compile(
    r"\b(?:remove|delete|turn\s+off|switch\s+off|take\s+(?:off|out)|get\s+rid\s+of|drop|clear|disable|kill|undo|lose)\b"
    r"|\b(?:normal|natural|original|regular)\s+voice\b|\bback\s+to\s+normal\b|\bno\s+(?:more\s+)?voice\s+effects?\b"
    # Final QA: a split particle — "turn the robot voice off", "switch the
    # chipmunk voice off on clip 2" (it APPLIED the effect and said done).
    r"|\b(?:turn|switch|take|shut|knock)\s+(?:\S+\s+){1,5}?(?:off|out)\b")
_LANE_VO = re.compile(r"\b(?:voice[- ]?overs?|vo|narration|narrator|voice\s+track)\b")
_LANE_MUSIC = re.compile(r"\b(?:music|song|bed|soundtrack|backing\s+track|bgm)\b")
#: review RE: the singular only, so "add echo to the overlays" / "put reverb
#: on the pips" fell through to EVERY main-track clip
_LANE_OVERLAY = re.compile(r"\b(?:overlays?|pips?|picture[- ]in[- ]pictures?|top\s+(?:clips?|layers?|videos?))\b")
_MY_VOICE = re.compile(r"\b(?:my|the|his|her|their|our)\s+(?:voice|vocals?|speech|dialogue)\b|\bme\b")
_ALL_RE = re.compile(r"\b(?:every|all)\s+(?:the\s+)?clips?\b|\beverything\b|\bwhole\s+(?:video|timeline|thing)\b")
_PCT_RE = re.compile(r"\b(\d{1,3})\s*(?:%|percent)")
_LITTLE = re.compile(r"\b(?:a\s+(?:little|bit|touch|tad)(?:\s+of)?|slight(?:ly)?|subtle|light(?:ly)?|gentle|some)\b")
_HALF = re.compile(r"\b(?:half|medium|moderate)\b")


def effect_in(clause: str) -> str | None:
    """The preset a clause names ("robot", "Hall", "walkie talkie", "make my
    voice deeper"), or None. A word that is common on its own ("phone",
    "deep", "radio") counts only with voice / effect / sound-like around it."""
    context = bool(_VOICE_CONTEXT.search(clause))
    for w, rx in _WORD_RE:
        if rx.search(clause) and (context or w not in AMBIGUOUS):
            return WORD_TO_ID[w]
    return None


def intensity_in(clause: str) -> float | None:
    m = _PCT_RE.search(clause)
    if m:
        return max(0.0, min(1.0, int(m.group(1)) / 100.0))
    if _LITTLE.search(clause):
        return 0.5
    if _HALF.search(clause):
        return 0.5
    return None


def read_voice(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    """effect / off / lane / clip_ref / voice / all / intensity, from ONE clause."""
    clause = hit.clause
    out: dict[str, Any] = {}
    eff = effect_in(clause)
    off = bool(_OFF_RE.search(clause))
    if off:
        out["off"] = True
        if eff:
            out["_only"] = eff                   # "turn off the echo": the echo ones
    elif eff:
        out["effect"] = eff
    inten = intensity_in(clause)
    if inten is not None and not off:
        out["intensity"] = inten
    if _LANE_VO.search(clause):
        out["lane"] = "vo"
    elif _LANE_MUSIC.search(clause) and not re.search(r"\bsound(?:s|ing)?\s+like\b[^.]*\b(?:song|music)\b", clause):
        out["lane"] = "music"
    elif _LANE_OVERLAY.search(clause):
        out["lane"] = "overlay"
    ref = G.clip_ref_of(clause)
    if ref == "$v1_all" or _ALL_RE.search(clause):
        out["all"] = True
    elif ref is not None:
        out["clip_ref"] = ref
    if _MY_VOICE.search(clause):
        out["_voice"] = True
    return out


# --------------------------------------------------------------------------
# 2. The expander (expanders.EXPANDERS)
# --------------------------------------------------------------------------

def _names() -> str:
    labels = [p.label for p in VFX.PRESETS]
    return ", ".join(labels[:-1]) + " or " + labels[-1]


def _ask(q: str) -> Expansion:
    return Expansion(notes=(q,))


def _lane_ids(f: TimelineFacts, lane: str) -> list[str]:
    if lane == "overlay":
        return [c.id for c in f.clips if re.match(r"^v\d+$", c.track or "") and c.track != "v1" and c.freeze is None]
    return [c.id for c in f.clips if c.track == lane and c.freeze is None]


def _sounding(f: TimelineFacts, ids: list[str]) -> list[str]:
    """`ids` without the freeze frames (a still has no sound) and without a
    clip whose source has no sound stream (review RE)."""
    return [i for i in ids if (fc := f.clip(i)) is None or (fc.freeze is None and fc.has_audio is not False)]


#: What a voice recording is called in its file name.
_VO_NAME = re.compile(r"(?:^|[^a-z])(?:voice[ _-]?over|vo|narration|narrator|voice|vox|dialogue|speech)(?:[^a-z]|$)",
                      re.I)
_AUDIO_LANES = ("music", "a1", "audio", "a2", "a3")


def _voiceover_elsewhere(f: TimelineFacts) -> tuple[list[str], Expansion | None]:
    """A voice-over the user dropped on an AUDIO lane (the Timeline routes an
    audio drop to Music; review RE): the clips whose file name says voice /
    vo / narration, else ONE question listing the audio clips by name."""
    audio = [c for c in f.clips if c.track in _AUDIO_LANES and c.freeze is None]
    named = [c.id for c in audio if _VO_NAME.search(c.name or "")]
    if named:
        return named, None
    if not audio:
        return [], None
    names = ", ".join(f"'{c.name or c.id}'" for c in audio[:5])
    return [], _ask(f"There is no voice-over on the timeline. Is one of the audio clips ({names}) your voice? "
                    f"Select it and say 'this clip', or name a clip like 'the second clip'.")


def _targets(it: Intent, f: TimelineFacts) -> tuple[dict[str, Any], list[str], str] | Expansion:
    """(step target args, the clip ids it covers, words for them) — or the
    question to ask instead."""
    lane = it.get("lane")
    ref = it.get("clip_ref")
    if ref is not None and not it.get("all"):
        cid, q = bind_clip(ref, f)
        if q:
            return _ask(q)
        ids = list(f.v1_clip_ids)
        cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
        if cid == "$v1_all":
            return {"track": "v1"}, _sounding(f, ids), "every main-track clip"
        if cid is None:
            return _ask("Which clip? There is no clip on the main track yet.")
        fc = f.clip(cid)
        if fc is not None and fc.freeze is not None:
            return _ask(f"{_label(cid, f).capitalize()} is a freeze frame — it has no sound. Which clip did you mean?")
        if fc is not None and fc.has_audio is False:
            return _ask(f"{_label(cid, f).capitalize()} has no sound. Which clip did you mean?")
        return {"clip_id": cid}, [cid], _label(cid, f)
    if lane == "vo" and not _lane_ids(f, "vo"):
        named, q = _voiceover_elsewhere(f)
        if q is not None:
            return q
        if named:
            who = "the voice-over" if len(named) == 1 else f"{len(named)} voice clips"
            return ({"clip_id": named[0]} if len(named) == 1 else {"clip_ids": named}), named, who
    if lane in ("vo", "music", "overlay"):
        ids = _sounding(f, _lane_ids(f, lane))
        name = {"vo": "the voice-over", "music": "the music", "overlay": "the overlay clips"}[lane]
        if not ids:
            what = {"vo": "voice-over", "music": "music", "overlay": "overlay clip"}[lane]
            return _ask(f"There is no {what} on the timeline. Which clip should get the voice effect — "
                        f"select it and say 'this clip', or name it like 'the second clip'?")
        if lane == "overlay":
            return ({"clip_ids": ids} if len(ids) > 1 else {"clip_id": ids[0]}), ids, name
        return {"track": "vo" if lane == "vo" else "music"}, ids, name
    if it.get("all"):
        return {"track": "v1"}, _sounding(f, list(f.v1_clip_ids)), "every main-track clip"
    sel = f.selection if f.selection and f.clip(f.selection) is not None else None
    if not it.get("_voice") and sel is not None:
        fc = f.clip(sel)
        if fc is not None and fc.freeze is None and fc.has_audio is not False:
            return {"clip_id": sel}, [sel], "the selected clip"
    vo = _lane_ids(f, "vo")
    if vo:
        return {"track": "vo"}, vo, "the voice-over"
    if it.get("_voice"):
        named, _q = _voiceover_elsewhere(f)
        if named:                        # "my voice" = the voice recording on the Music lane
            return ({"clip_id": named[0]} if len(named) == 1 else {"clip_ids": named}), named, "your voice"
    ids = _sounding(f, list(f.v1_clip_ids))
    if not ids:
        return _ask("There is no clip with sound on the timeline yet — add one first.")
    return {"track": "v1"}, ids, ("your voice" if it.get("_voice") else "every main-track clip")


def _off(it: Intent, f: TimelineFacts) -> Expansion:
    """Take the effect off: the clips the prompt names (a lane, a clip), or —
    with nothing named — every clip that carries one; "turn off the echo"
    only those with that effect. Nothing to take off is said, not guessed."""
    only = it.get("_only")
    named = it.get("clip_ref") is not None or it.get("lane") is not None or it.get("all")
    if named:
        got = _targets(it, f)
        if isinstance(got, Expansion):
            return got
        _args, scope, who = got
    else:
        scope, who = [c.id for c in f.clips], "every clip"
    ids = [i for i in scope if (fc := f.clip(i)) is not None and fc.voice_effect is not None
           and (only is None or fc.voice_effect == only)]
    if not ids:
        what = f"a {VFX.PRESET_BY_ID[only].label} voice effect" if only else "a voice effect"
        return _ask(f"No clip{' there' if named else ''} has {what}, so there is nothing to take off. Did you "
                    f"mean the room's own echo? Say 'clean up the audio' for that.")
    if len(ids) > 1 and not named:
        who = f"{len(ids)} clips"
    elif len(ids) == 1:
        who = _label(ids[0], f)
    target: dict[str, Any] = {"clip_id": ids[0]} if len(ids) == 1 else {"clip_ids": ids}
    return Expansion(
        steps=(step("set_voice_effect", STAGE_AUDIO, f"no voice effect on {who}", **target, effect="none"),),
        postconditions=(pc("voice_effect_is", f"{who} {'has' if len(ids) == 1 else 'have'} no voice effect",
                           clip_id=ids[0] if len(ids) == 1 else ids, effect=None),),
        notes=(f"the voice effect is off on {who}",))


def x_voice(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut Voice effects on the clip(s) the prompt names (see the module
    docstring for who that is), or off them."""
    if it.get("off"):
        return _off(it, f)
    got = _targets(it, f)
    if isinstance(got, Expansion):
        return got
    args, ids, who = got
    eff = it.get("effect")
    if eff is None:
        return _ask(f"Which voice effect? {_names()} — say like 'make my voice sound like a robot' "
                    f"or 'add echo to the voiceover'.")
    preset = VFX.PRESET_BY_ID[eff]
    inten = it.get("intensity")
    extra: dict[str, Any] = {} if inten is None else {"intensity": float(inten)}
    what = VFX.describe(eff, 1.0 if inten is None else float(inten))
    return Expansion(
        steps=(step("set_voice_effect", STAGE_AUDIO, f"{what} voice on {who}", **args, effect=eff, **extra),),
        postconditions=(pc("voice_effect_is", f"{who} sound{'s' if len(ids) == 1 else ''} {preset.label}",
                           clip_id=ids[0] if len(ids) == 1 else ids, effect=eff,
                           **({"intensity": float(inten)} if inten is not None else {})),),
        notes=(f"{who}: {preset.label} — {preset.hint[0].lower() + preset.hint[1:]}",))


__all__ = ["effect_in", "intensity_in", "read_voice", "x_voice"]
