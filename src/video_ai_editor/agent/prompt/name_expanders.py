"""Edits a key-free prompt makes BY NAME (wave E, lane F4b).

"remove the captions", "take off the filter", "delete the title", "remove the
transition between clip 2 and 3", "trim the second clip to 2 seconds",
"make the first clip 3 seconds long", "flip the second clip". Before this
module the first four were an honest "the Prompt bar cannot remove that yet"
(and "remove the black and white filter" APPLIED the mono look), the length
edits cut the VIDEO's first or last seconds, and a flip was "not available".

The same two rules as `clip_expanders` keep a wrong edit from being committed:

  * Everything is bound at plan time to real ids from `TimelineFacts`
    (`caption_clip_ids`, `texts`, `transitions`, `clips`), and every step is
    checked by a postcondition measured on the EDL afterwards
    (`clips_absent`, `effect_absent`, `transitions_absent`, `clip_duration`,
    `clip_flipped`).
  * When the prompt does not say which one (two titles, three transitions,
    no clip named or selected), or names one that is not there, the expansion
    is a QUESTION in `notes` with no steps — the plan is read-only and the
    reply asks. Nothing to remove is said plainly, with no edit.

Pure functions `(Intent, TimelineFacts, Context) -> Expansion`, registered in
`expanders.EXPANDERS`.
"""
from __future__ import annotations

import re
from typing import Any

from ...edl import speed_curve as SC
from ...edl import timebase as tb
from .clip_expanders import _label, bind_clip, one_clip, other_cuts
from .facts import ClipFact, TextFact, TimelineFacts, TransitionFact
from .recipes import Context, Expansion, Intent, ask, pc, placeholder, step
from .schema import STAGE_AUDIO, STAGE_CAPTIONS, STAGE_CUTS, STAGE_LOOK, STAGE_TEXT, STAGE_TRANSITIONS

#: The effect types "the filter" and "the colour grade" name.
FILTER_TYPES: tuple[str, ...] = ("lut",)
GRADE_TYPES: tuple[str, ...] = ("lut", "color")
#: A bundled look's name as a person says it ("the black and white filter").
LOOK_WORDS: dict[str, str] = {"mono.cube": "black and white", "teal_orange.cube": "teal and orange",
                              "warm.cube": "warm", "cool.cube": "cool", "punch.cube": "punchy",
                              "faded.cube": "faded"}
#: Seam tolerance, the same 0.05 s the compositor and remove_transition use.
SEAM_TOL_S = 0.05
#: The shortest clip a length edit may leave (the Inspector's floor).
MIN_CLIP_S = 0.1


def _ask(question: str) -> Expansion:
    return Expansion(notes=(question,))


def _concrete_clip(ref: Any, f: TimelineFacts) -> tuple[ClipFact | None, str | None]:
    """The ONE media clip `ref` names, as its facts, or a question."""
    cid, q = bind_clip(ref, f)
    if q:
        return None, q
    ids = list(f.v1_clip_ids)
    cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
    if cid == "$v1_all":
        return None, "Which clip? Name one, like 'the second clip'."
    fact = f.clip(cid)
    if fact is None:
        return None, "Which clip? That one is not a video or audio clip on this timeline."
    return fact, None


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


# --------------------------------------------------------------------------
# 1. Remove by name
# --------------------------------------------------------------------------

def x_remove_feature(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    what = it.get("what") or "other"
    if what == "captions":
        return _remove_captions(f)
    if what == "filter":
        return _remove_filter(it, f)
    if what == "transition":
        return _remove_transition(it, f, ctx)
    if what == "text":
        return _remove_text(it, f)
    if it.get("_glitch"):
        return _remove_glitch(it, f, ctx)
    if it.get("_voiceover"):
        return _remove_voiceover(f)
    from .planner import unsupported_removal_reply
    return _ask(unsupported_removal_reply(it.clause or ""))


def _remove_captions(f: TimelineFacts) -> Expansion:
    ids = list(f.caption_clip_ids)
    if not ids:
        return _ask("There are no captions on the timeline to remove.")
    return Expansion(
        steps=(step("bulk_delete", STAGE_CAPTIONS, f"remove the captions ({len(ids)} cues)", clip_ids=ids),),
        postconditions=(pc("clips_absent", "the captions are gone", clip_ids=ids),),
        notes=(f"removed the captions ({len(ids)} cue{'s' if len(ids) != 1 else ''})",))


#: Effects-panel labels (EffectsPanel.tsx) for the replies.
FX_LABELS: dict[str, str] = {"vignette": "Vignette", "grain": "Grain", "vintage": "Vintage", "vhs": "VHS",
                             "glow": "Glow", "rgb_split": "RGB Split", "sharpen": "Sharpen", "blur": "Blur",
                             "hflip": "Flip H", "vflip": "Flip V"}


def _remove_filter(it: Intent, f: TimelineFacts) -> Expansion:
    types = GRADE_TYPES if it.get("_grade") else FILTER_TYPES
    look = it.get("_look")
    fx = [t for t in (it.get("_fx") or []) if t in FX_LABELS]
    if it.get("_all_fx"):
        # "clear all effects from clip 2": every effect the clip carries
        types = tuple(sorted({e for c in f.clips for e in c.effects})) or FILTER_TYPES
    # review RE: a named EFFECT ("the vintage filter", "the vignette") is
    # looked for in each clip's effect chain first; the LUT look of the same
    # name ("vintage" → faded.cube) only when no clip carries the effect
    if fx and any(e in fx for c in f.clips for e in c.effects):
        types, look = tuple(fx), None
        word = " and ".join(FX_LABELS[t] for t in fx) + " effect"
    else:
        if fx and not look:
            return _ask(f"There is no {' or '.join(FX_LABELS[t] for t in fx)} effect on any clip to remove.")
        word = "colour grade" if it.get("_grade") else (f"{LOOK_WORDS.get(look, look.rsplit('.', 1)[0])} filter"
                                                         if look else "effects" if it.get("_all_fx") else "filter")

    def carries(c: ClipFact) -> bool:
        # a NAMED look ("the black and white filter") is only on the clips
        # whose LUT is that file — the others keep theirs
        return (look in c.looks) if look else any(e in types for e in c.effects)

    ref = it.get("clip_ref")
    if ref is not None and ref != "$v1_all":
        fact, q = _concrete_clip(ref, f)
        if q:
            return _ask(q)
        assert fact is not None
        targets = [fact] if carries(fact) else []
        if not targets:
            return _ask(f"There is no {word} on {_label(fact.id, f)} to remove.")
    else:
        targets = [c for c in f.clips if carries(c)]
        if not targets:
            return _ask(f"There is no {word} on any clip to remove.")
    ids = [c.id for c in targets]
    who = _label(ids[0], f) if len(ids) == 1 else f"{len(ids)} clips"
    return Expansion(
        steps=(step("remove_effects", STAGE_LOOK, f"take the {word} off {who}", clip_ids=ids, types=list(types)),),
        postconditions=(pc("effect_absent", f"the {word} is off", clip_id=ids, types=list(types)),),
        notes=(f"removed the {word} from {who}",))


#: A voice-over recorded or imported onto another audio lane keeps its name.
_VO_NAME_RE = re.compile(r"voice[ _-]?over|narration|^vo_\d", re.I)


def _remove_voiceover(f: TimelineFacts) -> Expansion:
    """"remove the voiceover" / "delete the narration" (Final QA: it offered
    a NEW spoken voice-over): every clip on the voice-over lane, plus a take
    named as one on another audio lane, in one step."""
    ids = [c.id for c in f.clips
           if c.track == "vo" or (c.track not in ("v1",) and not c.track.startswith("v")
                                  and _VO_NAME_RE.search(c.name or ""))]
    if not ids:
        return _ask("There is no voice-over on the timeline to remove.")
    n = len(ids)
    return Expansion(
        steps=(step("bulk_delete", STAGE_AUDIO, f"remove the voice-over ({n} clip{'s' if n != 1 else ''})",
                    clip_ids=ids),),
        postconditions=(pc("clips_absent", "the voice-over is gone", clip_ids=ids),),
        notes=(f"removed the voice-over ({n} clip{'s' if n != 1 else ''})",))


def _remove_glitch(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """"remove the glitch effect" (Final QA): the Glitch TRANSITION(s) when
    the timeline has one, else the RGB Split effect (the glitchy look in the
    Effects panel), else an honest "there is none"."""
    from ...render.transitions import canonical
    from dataclasses import replace
    if any(canonical(tr.type) == "glitch" for tr in f.transitions):
        return _remove_transition(replace(it, slots={**it.slots, "what": "transition", "_type": "glitch",
                                                     "all": True}), f, ctx)
    if any("rgb_split" in c.effects for c in f.clips):
        return _remove_filter(replace(it, slots={**it.slots, "what": "filter", "_fx": ["rgb_split"]}), f)
    return _ask("There is no Glitch transition or RGB Split effect on the timeline to remove.")


def _transition_label(tr: TransitionFact, f: TimelineFacts) -> str:
    seams = list(f.v1_boundaries)
    k = next((i + 1 for i, b in enumerate(seams) if abs(b - tr.at) < SEAM_TOL_S), None)
    return f"the {tr.type} between clip {k} and {k + 1}" if k else f"the {tr.type} at {_mmss(tr.at)}"


def _pick_transition(it: Intent, f: TimelineFacts) -> tuple[list[TransitionFact], str | None]:
    trs = list(f.transitions)
    look = it.get("_type")
    if look:
        # A named look ("take the glitch transition out") picks only those.
        from ...render.transitions import canonical, display_name
        trs = [tr for tr in trs if canonical(tr.type) == look]
        if not trs:
            return [], f"There is no {display_name(look)} transition on the timeline to remove."
    seam, which, at = it.get("_seam"), it.get("_which"), it.get("_at")
    if seam is not None:
        seams = list(f.v1_boundaries)
        if not 1 <= int(seam) <= len(seams):
            return [], f"Which transition? There is no cut between clip {seam} and {int(seam) + 1}."
        b = seams[int(seam) - 1]
        hit = [tr for tr in trs if abs(tr.at - b) < SEAM_TOL_S]
        return (hit, None) if hit else ([], f"There is no transition between clip {seam} and {int(seam) + 1} to remove.")
    if at is not None:
        hit = [tr for tr in trs if abs(tr.at - float(at)) < max(SEAM_TOL_S, 0.5 / max(1, f.fps))]
        return (hit, None) if hit else ([], f"There is no transition at {_mmss(float(at))} to remove.")
    if which in ("first", "last"):
        return [trs[0] if which == "first" else trs[-1]], None
    if it.get("all") or len(trs) == 1:
        return trs, None
    listing = "; ".join(_transition_label(tr, f) for tr in trs[:4])
    return [], (f"Which transition? There are {len(trs)}: {listing}. Say like 'remove the transition between "
                f"clip 1 and 2', or 'remove all the transitions'.")


def _remove_transition(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if not f.transitions:
        return _ask("There are no transitions on the timeline to remove.")
    picked, q = _pick_transition(it, f)
    if q:
        return _ask(q)
    if len(picked) == len(f.transitions) and len(picked) > 1:
        return Expansion(
            steps=(step("remove_transition", STAGE_TRANSITIONS, f"remove all {len(picked)} transitions", all=True),),
            postconditions=(pc("transitions_absent", "no transition is left"),),
            notes=(f"removed all {len(picked)} transitions",))
    if other_cuts(ctx, "remove_feature"):
        return _ask("Remove that transition in its own prompt: the cuts in this one move the seams.")
    steps = tuple(step("remove_transition", STAGE_TRANSITIONS, f"remove {_transition_label(tr, f)}", at=tr.at)
                  for tr in picked)
    return Expansion(steps=steps,
                     postconditions=tuple(pc("transitions_absent", "the transition is gone", at=tr.at) for tr in picked),
                     notes=tuple(f"removed {_transition_label(tr, f)}" for tr in picked))


def _text_label(t: TextFact) -> str:
    words = t.text.replace("\n", " ").strip()
    return f"'{words[:24]}{'…' if len(words) > 24 else ''}' at {_mmss(t.start)}"


def _remove_text(it: Intent, f: TimelineFacts) -> Expansion:
    texts = [t for t in f.texts if t.role != "watermark"]
    role, words = it.get("_role"), (it.get("text") or "").strip().lower()
    noun = {"lower_third": "lower third", "hook": "hook"}.get(role or "", "text")
    if role:
        texts = [t for t in texts if t.role == role]
    if words:
        texts = [t for t in texts if words in t.text.lower()]
    if not texts:
        said = f" saying '{it.get('text')}'" if words else ""
        return _ask(f"There is no {noun}{said} on the timeline to remove.")
    if len(texts) > 1 and not it.get("all"):
        chosen = [t for t in texts if t.id == f.selection]
        if not chosen:
            listing = "; ".join(_text_label(t) for t in texts[:4])
            return _ask(f"Which {noun}? There are {len(texts)}: {listing}. Say like \"delete the text "
                        f"'{texts[0].text.split(chr(10))[0][:16]}'\", select it, or say 'delete all the {noun}'.")
        texts = chosen
    ids = [t.id for t in texts]
    who = _text_label(texts[0]) if len(texts) == 1 else f"{len(texts)} {noun} overlays"
    return Expansion(
        steps=(step("bulk_delete", STAGE_TEXT, f"delete {who}", clip_ids=ids),),
        postconditions=(pc("clips_absent", f"the {noun} is gone", clip_ids=ids),),
        notes=(f"deleted {who}",))


def _norm_words(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", s.lower()).split())


def x_retext(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """Change what an EXISTING text says (Final QA): "change the Summer Trip
    text to Winter Trip" ADDED a new title reading "to Winter Trip" in the
    Anton 140 px pop preset and deleted the user's own one as an "overlap
    replace". The text is found by its current words (or is the only one /
    the selected one); `set_text` changes only its words."""
    texts = [t for t in f.texts if t.role not in ("watermark", "caption")]
    if it.get("_role"):
        texts = [t for t in texts if t.role == it.get("_role")] or texts
    if not texts:
        return _ask("There is no text on the timeline to change. To add one, say like "
                    "\"add a title saying 'Summer Trip'\".")
    old = (it.get("old") or "").strip()
    if old:
        want = _norm_words(old)
        cand = [t for t in texts if want and want in _norm_words(t.text)]
        if not cand:
            listing = "; ".join(_text_label(t) for t in texts[:4])
            return _ask(f"There is no text saying '{old}'. The texts are: {listing}. Which one should change?")
    else:
        cand = [t for t in texts if t.id == f.selection] or texts
    if len(cand) > 1:
        listing = "; ".join(_text_label(t) for t in cand[:4])
        return _ask(f"Which text? There are {len(cand)}: {listing}. Say like \"change the "
                    f"'{cand[0].text.split(chr(10))[0][:16]}' text to …\", or select it first.")
    target = cand[0]
    new = (it.get("text") or "").strip()
    if not new:
        return Expansion(
            questions=(ask("text", f"What should '{target.text[:40]}' say instead?", kind="text"),),
            steps=(step("set_text", STAGE_TEXT, f"change the words of {_text_label(target)}",
                        clip_id=target.id, text=placeholder("text")),))
    if new == target.text:
        return _ask(f"The text {_text_label(target)} already says '{new}'.")
    return Expansion(
        steps=(step("set_text", STAGE_TEXT, f"change {_text_label(target)} to '{new}'",
                    clip_id=target.id, text=new),),
        postconditions=(pc("text_present", "the text says the new words", contains=new),),
        notes=(f"'{target.text[:40]}' now says '{new}'",))


def named_texts(clause: str, texts: list[TextFact], f: TimelineFacts) -> list[TextFact]:
    """The texts a clause means (K3): the ones whose words it quotes ("the
    Summer Trip title", "the SALE text"), else the selected text, else all
    of them (the caller asks when that is more than one)."""
    said = f" {_norm_words(clause or '')} "
    named = [t for t in texts if _norm_words(t.text.split(chr(10))[0])
             and f" {_norm_words(t.text.split(chr(10))[0])} " in said]
    if named:
        return named
    return [t for t in texts if t.id == f.selection] or texts


def x_restyle_text(it: Intent, f: TimelineFacts, ctx: Context | None = None) -> Expansion:
    """Restyle an EXISTING text (K3): "make the title red", "make the Summer
    Trip text bigger", "change the title font to Anton", "put the title at
    the top" — one `set_text_style` step on the text the clause names. Its
    words, timing and animation are never touched (they used to become a
    NEW title reading "red" that replaced the user's own)."""
    look = dict(it.get("_look") or {})
    texts = [t for t in f.texts if t.role not in ("watermark", "caption")]
    if not texts:
        return _ask("There is no title on the timeline to restyle. To add one, say like "
                    "\"add a title saying 'Summer Trip'\".")
    if not look:
        return _ask("How should the title look? Say like 'make the title red', 'make the title bigger', "
                    "'make the title bold' or 'put the title at the top'.")
    cand = named_texts(it.clause, texts, f)
    every = bool(re.search(r"\b(?:all|every|each|both)\s+(?:of\s+)?(?:the\s+|my\s+)?(?:texts?|titles?|words)\b",
                           it.clause or ""))
    if len(cand) > 1 and not every:
        listing = "; ".join(_text_label(t) for t in cand[:4])
        return _ask(f"Which text? There are {len(cand)}: {listing}. Say like \"make the "
                    f"'{cand[0].text.split(chr(10))[0][:16]}' text …\", or select it first.")
    if every and len(cand) > 1:
        # "make all the text red" asked which text (Final sweep 2)
        parts = [x_restyle_text(Intent(it.recipe, it.slots, it.score, f"the {t.text.split(chr(10))[0]} text"), f, ctx)
                 for t in cand[:12]]
        return Expansion(steps=tuple(s for p in parts for s in p.steps),
                         postconditions=tuple(c for p in parts for c in p.postconditions),
                         notes=(f"restyled all {len(cand)} texts",))
    t = cand[0]
    said = []
    if "color" in look:
        said.append(f"colour {look['color']}")
    if "size" in look:
        said.append(f"size {look['size']:g} px")
    elif "size_scale" in look:
        said.append("bigger" if look["size_scale"] > 1 else "smaller")
    if "font" in look:
        said.append(f"font {look['font']}")
    elif look.get("bold"):
        said.append("bold")
    if "position" in look:
        said.append(f"at the {look['position']}")
    if "upper" in look:
        said.append("ALL CAPS" if look["upper"] else "normal case")
    if "stroke_w" in look:
        said.append("no outline" if not look["stroke_w"] else "an outline")
    if "background" in look:
        said.append("a box behind it" if look["background"] else "no box")
    check = {k: look[k] for k in ("color", "size", "font", "bold", "position") if k in look}
    return Expansion(
        steps=(step("set_text_style", STAGE_TEXT, f"restyle {_text_label(t)}: " + ", ".join(said),
                    clip_id=t.id, **look),),
        postconditions=(pc("text_style_is", "the text has the requested look", clip_id=t.id, **check),
                        pc("text_present", "the text keeps its words", contains=t.text[:60])),
        notes=(f"{_text_label(t)}: " + ", ".join(said),))


def x_retime_text(it: Intent, f: TimelineFacts, ctx: Context | None = None) -> Expansion:
    """Move / lengthen / shorten an EXISTING text (Final QA r3): "make the
    title longer" (×1.5), "move the title to 4 seconds" (same length),
    "title from 1s to 4s", "make the title last 5 seconds". It used to DELETE
    the user's title and add one saying "longer" / "to 4 seconds"."""
    texts = [t for t in f.texts if t.role not in ("watermark", "caption")]
    if not texts:
        return _ask("There is no text on the timeline to move or lengthen. To add one, say like "
                    "\"add a title saying 'Summer Trip' for 5 seconds\".")
    cand = named_texts(it.clause, texts, f)
    if len(cand) > 1:
        listing = "; ".join(_text_label(t) for t in cand[:4])
        return _ask(f"Which text? There are {len(cand)}: {listing}. Select it first, then say it again.")
    t = cand[0]
    r = dict(it.get("_retime") or {})
    s0, e0 = float(t.start), float(t.end)
    dur = max(0.1, e0 - s0)
    if "start" in r and "end" in r:
        s, e = float(r["start"]), float(r["end"])
    elif "start" in r:
        s = float(r["start"])
        e = s + dur
    elif "end" in r:
        s, e = s0, float(r["end"])
    elif "extend" in r:
        s, e = s0, e0 + float(r["extend"])          # "extend the SALE text by 2 seconds"
    elif "dur" in r:
        s, e = s0, s0 + float(r["dur"])
    elif "shift" in r:
        s, e = s0 + float(r["shift"]), e0 + float(r["shift"])
        if s < -1e-6:
            return _ask(f"The title {_text_label(t)} already starts at {s0:g}s — it can move at most "
                        f"{s0:g}s earlier. Say like 'show the title from 0s to {dur:g}s'.")
    elif "to_end" in r:
        s, e = s0, float(f.video_end or f.duration or e0)
    else:
        s, e = s0, s0 + dur * float(r.get("factor") or 1.0)
    vend = float(f.video_end or f.duration or e)
    notes: list[str] = []
    if vend > 0 and e > vend + 1e-3:
        e = vend
        notes.append(f"it ends with the video at {vend:g}s")
    s = max(0.0, s)
    if e - s < 0.1:
        return _ask(f"{_text_label(t).capitalize()} would not be on screen at all — say like "
                    f"'show the title from {s:g}s to {s + 3:g}s'.")
    s, e = round(s, 3), round(e, 3)
    if abs(s - s0) < 1e-3 and abs(e - e0) < 1e-3:
        return _ask(f"{_text_label(t).capitalize()} is already on screen {s0:g}–{e0:g}s — nothing to change.")
    return Expansion(
        steps=(step("set_clip_timing", STAGE_TEXT, f"{_text_label(t)} on screen {s:g}–{e:g}s",
                    clip_id=t.id, start=s, end=e),),
        postconditions=(pc("text_present", "the text is on screen at its new time", contains=t.text[:60],
                           start_geq=round(s - 0.01, 3)),),
        notes=tuple([f"{_text_label(t)} now shows {s:g}–{e:g}s"] + notes))


# --------------------------------------------------------------------------
# 2. A clip's length
# --------------------------------------------------------------------------

def _source_span_for(c: ClipFact, length: float, *, from_end: bool = False) -> float:
    """Source seconds a clip consumes over `length` timeline seconds: a
    constant speed's product, a curve's INTEGRAL when the clip is shortened
    (`dispatch._trim_curve` keeps exactly that piece of the curve), the mean
    speed when it is lengthened (the plain trim lays the whole curve over
    the longer span, `effective_duration` = S / mean)."""
    if c.curve and length < c.duration - 1e-9:
        S = c.src_out - c.src_in
        cm = SC.curve_map(list(c.curve), S)
        if cm is not None:
            # a head trim keeps the curve's TAIL: the source after the point
            # the curve reaches at (footprint - length)
            return S - SC.source_seconds(cm, c.duration - length) if from_end else SC.source_seconds(cm, length)
    return length * (c.speed if c.speed > 0 else 1.0)


def x_clip_length(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """"trim the second clip to 2 seconds" → `trim_clip` moving the clip's
    END (its head stays where it is, like the Inspector's Duration field);
    "2 seconds off the start of" moves the head. The check measures the
    clip's timeline footprint afterwards."""
    ref, q = one_clip(it, f, "trim")
    if q:
        return _ask(q)
    if ref == "$v1_all":
        return _ask("Which clip? Name one, like 'trim the second clip to 2 seconds'.")
    c, q = _concrete_clip(ref, f)
    if q:
        return _ask(q)
    assert c is not None
    if other_cuts(ctx, "clip_length"):
        return _ask("Change that clip's length in its own prompt: the other cuts in this one change the clips.")
    what = _label(c.id, f)
    if c.freeze is not None:
        return _ask(f"{what.capitalize()} is a freeze frame — how long should it hold? Drag its edge, or set "
                    f"its Duration in the Inspector.")
    if c.reverse and c.curve:
        return _ask(f"{what.capitalize()} plays a speed curve backwards — trim it by dragging its edge on the "
                    f"timeline.")
    delta, edge = it.get("_delta"), it.get("_edge") or "tail"
    target = it.get("seconds")
    if target is None and delta is None:
        return _ask(f"How long should {what} be? Say like 'trim {what} to 2 seconds'.")
    target = float(target) if target is not None else c.duration + float(delta)
    target = tb.quantize(target, f.fps)
    if target < MIN_CLIP_S:
        return _ask(f"{what.capitalize()} is {c.duration:.2f}s — how long should it be? It cannot be shorter than "
                    f"{MIN_CLIP_S:g}s.")
    if abs(target - c.duration) < 0.5 / max(1, f.fps):
        return _ask(f"{what.capitalize()} is already {c.duration:g}s — how long should it be?")
    # the kept end: a forward clip keeps its head (in) unless "off the start";
    # a reversed clip SHOWS its out first, so its head is `out`
    keep_head = (edge != "head") != c.reverse
    src = _source_span_for(c, target, from_end=not keep_head)
    if keep_head:
        args: dict[str, Any] = {"out": round(c.src_in + src, 4)}
        reach = c.src_in + src
    else:
        args = {"in": round(c.src_out - src, 4)}
        reach = c.src_out - src
    if reach < -1e-6 or (c.src_duration is not None and reach > c.src_duration + 1e-3):
        room = (c.src_duration or c.src_out) - c.src_in if keep_head else c.src_out
        longest = room / (c.speed if c.speed > 0 else 1.0)
        return _ask(f"{what.capitalize()} can be at most {longest:.1f}s long — its source runs out. "
                    f"How long should it be?")
    tol = 1.0 / max(1, f.fps) + 1e-3
    verb = "trimmed" if target < c.duration else "extended"
    return Expansion(
        steps=(step("trim_clip", STAGE_CUTS, f"{'trim' if verb == 'trimmed' else 'extend'} {what} to {target:g}s "
                    f"(was {c.duration:.2f}s)",
                    clip_id=c.id, **args),),
        postconditions=(pc("clip_duration", f"{what} is {target:g}s long", clip_id=c.id, seconds=target, tol=tol),),
        notes=(f"{what} {verb} to {target:g}s (was {c.duration:.2f}s)",))


# --------------------------------------------------------------------------
# 3. Flip / mirror
# --------------------------------------------------------------------------

_AXES: dict[str, tuple[str, ...]] = {"horizontal": ("horizontal",), "vertical": ("vertical",),
                                     "both": ("horizontal", "vertical")}
_FIELD = {"horizontal": "flip_h", "vertical": "flip_v"}


def _flip_sticker(it: Intent, f: TimelineFacts) -> Expansion:
    """Final QA r3: "flip the sticker horizontally" answered "Want a sticker?
    … cannot place one" although a sticker was there and `flip_clip` flips
    stickers (the release notes: Mirror and Flip for any clip, overlay or
    sticker)."""
    ids = list(f.sticker_ids)
    if not ids:
        return _ask("There is no sticker on the timeline to flip — add one from the Stickers panel first.")
    if len(ids) == 1:
        sid = ids[0]
    elif f.selection in ids:
        sid = f.selection
    else:
        return _ask(f"Which sticker? There are {len(ids)} — select one and say 'flip this sticker'.")
    axes = _AXES.get(it.get("axis") or "horizontal", ("horizontal",))
    on = it.get("on")
    cur_flags = f.sticker_flips.get(sid, "")
    want: dict[str, bool] = {}
    steps = []
    for axis in axes:
        cur = ("h" if axis == "horizontal" else "v") in cur_flags
        value = (not cur) if on is None else bool(on)
        if value == cur:
            continue
        want[_FIELD[axis]] = value
        steps.append(step("flip_clip", STAGE_LOOK, f"{'mirror' if value else 'unmirror'} the sticker {axis}ly",
                          clip_id=sid, axis=axis, value=value))
    if not steps:
        return _ask(f"The sticker is already {'flipped' if on else 'not flipped'} that way — flip it the other way?")
    return Expansion(steps=tuple(steps),
                     postconditions=(pc("clip_flipped", "the sticker is flipped as asked", clip_id=sid, **want),),
                     notes=(f"the sticker {'flipped' if any(want.values()) else 'unflipped'} "
                            f"{' and '.join(a for a in axes if _FIELD[a] in want)}ly",))


def x_flip(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut's Mirror (horizontal) / Flip (vertical) as `flip_clip` with an
    EXPLICIT value — the toggle is resolved here from the clip's current
    state so the check knows what to expect."""
    from ...edl.schema import Transform
    if "flip_h" not in Transform.model_fields:          # a build without the fields (lane F4a)
        return _ask("Flipping is not in this build — want it turned upside down instead (a 180° rotation)?")
    target = it.get("_target")
    if target == "sticker":
        return _flip_sticker(it, f)
    if target == "overlay" and it.get("clip_ref") is None:
        ov = [c.id for c in f.clips if (c.track or "").startswith("v") and c.track != "v1"
              and (c.track or "")[1:].isdigit()]
        if not ov:
            return _ask("There is no overlay clip to flip. Which clip did you mean?")
        if len(ov) > 1 and f.selection not in ov:
            return _ask(f"Which overlay? There are {len(ov)} — select one and say 'flip this clip'.")
        ref, q = (f.selection if f.selection in ov else ov[0]), None
    else:
        ref, q = one_clip(it, f, "flip")
    if q:
        return _ask(q)
    ids = list(f.v1_clip_ids) if ref == "$v1_all" else None
    if ids is None:
        c, q = _concrete_clip(ref, f)
        if q:
            return _ask(q)
        assert c is not None
        facts = [c]
    else:
        facts = [c for c in (f.clip(i) for i in ids) if c is not None]
        if not facts:
            return _ask("There is no clip on the main track to flip.")
    axes = _AXES.get(it.get("axis") or "horizontal", ("horizontal",))
    on = it.get("on")
    steps, pcs, notes = [], [], []
    for c in facts:
        want: dict[str, bool] = {}
        for axis in axes:
            cur = bool(getattr(c, _FIELD[axis]))
            value = (not cur) if on is None else bool(on)
            if value == cur:
                continue
            want[_FIELD[axis]] = value
            steps.append(step("flip_clip", STAGE_LOOK, f"{'mirror' if value else 'unmirror'} {_label(c.id, f)} "
                                                      f"{axis}ly", clip_id=c.id, axis=axis, value=value))
        if want:
            pcs.append(pc("clip_flipped", f"{_label(c.id, f)} is flipped as asked", clip_id=c.id, **want))
            notes.append(f"{_label(c.id, f)} {'flipped' if any(want.values()) else 'unflipped'} "
                         f"{' and '.join(a for a in axes if _FIELD[a] in want)}ly")
    if not steps:
        return _ask(f"{_label(facts[0].id, f).capitalize() if len(facts) == 1 else 'Every clip'} is already "
                    f"{'flipped' if on else 'not flipped'} that way — flip it the other way?")
    return Expansion(steps=tuple(steps), postconditions=tuple(pcs), notes=tuple(notes))


__all__ = ["FILTER_TYPES", "GRADE_TYPES", "x_remove_feature", "x_clip_length", "x_flip", "x_retime_text",
           "x_restyle_text", "named_texts"]
