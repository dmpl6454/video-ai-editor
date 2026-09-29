"""CapCut clip animations from a key-free prompt (wave E, lane F1).

"add a zoom in animation to the first clip", "make the sticker bounce in",
"slide the last clip out", "make the second clip shake", "give the overlay
a spin", "remove the animation from the sticker". The ONE table is
`edl/clip_animations.py`; the tool is `set_animation`; the words are
`anim_vocab.py`.

The rules `clip_expanders` keeps hold here too:

  * The target is bound at plan time from `TimelineFacts`: a named main-track
    clip (`bind_clip`), "the sticker" (the only one, the selected one, or
    "the second sticker"), "the overlay" (the overlay lane's clip), "it" /
    no target (the selection). Several stickers / overlays with none named
    or selected is a question.
  * What the prompt leaves open is ASKED, never guessed: an animation that
    is not named ("animate the first clip"), a slide with no direction
    ("slide the last clip out" → which way), a zoom with no direction. The
    expansion is then a question with no steps: the plan commits nothing.
  * A motion that is only an In or an Out ("bounce", "spin", "zoom in") is
    the In unless the prompt says out / exit / leave / away / at the end.
  * The step carries `animation_is`, measured on the EDL the renderers read
    (tests/test_clip_anim_render.py decodes the renders).
"""
from __future__ import annotations

import re
from typing import Any

from ...edl import clip_animations as A
from . import grammar as G
from . import slots as S
from .anim_vocab import ANIM_OFF, BARE_WORDS, COMBO_WORDS, IN_OUT_WORDS
from .clip_expanders import _label, bind_clip
from .facts import TimelineFacts
from .recipes import Context, Expansion, Intent, pc, step
from .schema import STAGE_LOOK
from .semantics import STICKER_NOUNS

# --------------------------------------------------------------------------
# 1. Reading a clause (clip_slots.READERS)
# --------------------------------------------------------------------------


def _word_rx(w: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(w).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+") + r"\b")


_COMBO_RX = [(w, _word_rx(w)) for w in sorted(COMBO_WORDS, key=len, reverse=True)]
_INOUT_RX = [(w, _word_rx(w)) for w in sorted(IN_OUT_WORDS, key=len, reverse=True)]
_BARE_RX = [(w, _word_rx(w)) for w in BARE_WORDS]
_OUT_RE = re.compile(r"\b(?:out|exit(?:s|ing)?|outro|leav(?:e|es|ing)|disappear(?:s|ing)?|away|off|goes|going)\b"
                     r"|\bat\s+the\s+(?:very\s+)?end\b|\bas\s+it\s+(?:ends|leaves|goes)\b")
_IN_RE = re.compile(r"\b(?:in|into|entrance|intro|enter(?:s|ing)?|appear(?:s|ing)?|arriv(?:e|es|ing)|onto|on\s+screen)\b"
                    r"|\bat\s+the\s+(?:very\s+)?(?:start|beginning)\b")
#: "slide in from the left" enters moving RIGHT (CapCut names the motion).
_FROM_SIDE = {"left": "slide_right", "right": "slide_left", "top": "slide_down", "above": "slide_down",
              "bottom": "slide_up", "below": "slide_up"}
_FROM_RE = re.compile(r"\bfrom\s+(?:the\s+)?(left|right|top|above|bottom|below)\b")
_TO_SIDE = {"left": "slide_left", "right": "slide_right", "top": "slide_up", "up": "slide_up",
            "bottom": "slide_down", "down": "slide_down"}
_TO_RE = re.compile(r"\b(?:to|towards?|off)\s+(?:the\s+)?(left|right|top|bottom)\b")
_STICKER_RE = re.compile(rf"\b(?:{STICKER_NOUNS})\b")
_OVERLAY_RE = re.compile(r"\b(?:overlays?|pips?|picture[- ]in[- ]picture|top\s+(?:clip|layer|video)|upper\s+(?:clip|layer))\b")
_TEXT_RE = re.compile(r"\b(?:titles?|text|captions?|subtitles?|lower[- ]?thirds?|hooks?)\b")
_ALL_RE = re.compile(r"\b(?:every|all)\s+(?:the\s+)?(?:clips?|shots?)\b|\beverything\b")
_ORD_STICKER = re.compile(rf"\b(?:the\s+)?({G.ORDINAL})\s+(?:stickers?|emojis?|logos?)\b"
                          r"|\b(?:stickers?|emojis?)\s+(?:#\s*|number\s+)?(\d{1,2})\b")
_IT_RE = re.compile(r"\b(?:it|this|that|them)\b")
_SECONDS_RE = re.compile(r"\b(?:over|for|in|lasting|taking)?\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b")
_ORD_WORDS = {"first": 1, "opening": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1, "final": -1, "closing": -1}


def _strip(clause: str, words: list[str]) -> str:
    for w in words:
        clause = _word_rx(w).sub(" ", clause)
    return clause


def motion_in(clause: str) -> dict[str, Any]:
    """{kind, preset} a clause names, {"_ask": "slide"|"zoom"} when a direction
    is missing, {"_ask": "which"} when no motion is named at all."""
    for w, rx in _COMBO_RX:
        if rx.search(clause):
            return {"kind": "combo", "preset": COMBO_WORDS[w]}
    for w, rx in _INOUT_RX:
        if rx.search(clause):
            rest = _strip(clause, [w, "zoom in", "zoom out"])
            both = bool(re.search(r"\bin\s+and\s+(?:then\s+)?out\b", rest))
            if both and IN_OUT_WORDS[w][0] and IN_OUT_WORDS[w][1]:
                # final sweep 3 r2: "fade the overlay in and out" planned the
                # In alone — both sides, one set_animation
                return {"kind": "in", "preset": IN_OUT_WORDS[w][0], "_also": {"out": IN_OUT_WORDS[w][1]}}
            out = bool(_OUT_RE.search(rest))
            kind = "out" if out else "in"
            pid = IN_OUT_WORDS[w][0 if kind == "in" else 1]
            return {"kind": kind, "preset": pid}
    for w, rx in _BARE_RX:
        if rx.search(clause):
            rest = _strip(clause, [w])
            out = bool(_OUT_RE.search(rest))
            kind = "out" if out else "in"
            if w.startswith("slid"):
                m = _FROM_RE.search(clause) if kind == "in" else _TO_RE.search(clause)
                if m:
                    side = m.group(1)
                    pid = (_FROM_SIDE if kind == "in" else _TO_SIDE).get(side)
                    if pid:
                        return {"kind": kind, "preset": pid}
                return {"kind": kind, "_ask": "slide"}
            return {"kind": kind, "_ask": "zoom"}
    return {"_ask": "which"}


def read_animation(hit: G.IntentHit, c: S.Slots) -> dict[str, Any]:
    """kind / preset / off / target / clip_ref / nth / duration, from ONE clause."""
    clause = hit.clause
    out: dict[str, Any] = {}
    if ANIM_OFF.search(clause):
        out["off"] = True
        # Final QA r3: "remove all animations" / "remove the zoom animation"
        # asked "Which clip should I animate?" — they name WHICH animations.
        if re.search(r"\b(?:all|every|any|each)\s+(?:the\s+|of\s+the\s+)?(?:clip\s+)?animations?\b"
                     r"|\banimations?\s+(?:from|off)\s+(?:everything|every(?:thing)?|all)\b", clause):
            out["_off_all"] = True
        m = re.search(r"\b([a-z]+)(?:[\s-]+(?:in|out))?\s+animations?\b", clause)
        if m and m.group(1) not in _NOT_A_MOTION:
            out["_off_type"] = m.group(1)
    else:
        out.update(motion_in(clause))
    if _STICKER_RE.search(clause):
        out["target"] = "sticker"
        m = _ORD_STICKER.search(clause)
        if m:
            word = (m.group(1) or "").strip()
            n = _ORD_WORDS.get(word) if word else None
            if n is None and word:
                d = re.match(r"(\d{1,2})", word)
                n = int(d.group(1)) if d else None
            if m.group(2):
                n = int(m.group(2))
            if n is not None:
                out["nth"] = n
    elif _OVERLAY_RE.search(clause):
        out["target"] = "overlay"
    elif _TEXT_RE.search(clause) and not G.clip_ref_of(clause):
        out["target"] = "text"
    else:
        ref = G.clip_ref_of(clause)
        if ref == "$v1_all" or _ALL_RE.search(clause):
            out["all"] = True
        elif ref is not None:
            out["clip_ref"] = ref
        elif _IT_RE.search(clause):
            out["clip_ref"] = "$selected"
    m = _SECONDS_RE.search(clause)
    if m and not out.get("off"):
        out["duration_s"] = float(m.group(1))
    return out


# --------------------------------------------------------------------------
# 2. The expander (expanders.EXPANDERS)
# --------------------------------------------------------------------------

def _names(kind: str) -> str:
    labels = [p.label for p in A.PRESETS[kind]]
    return ", ".join(labels[:-1]) + " or " + labels[-1]


def _ask(q: str) -> Expansion:
    return Expansion(notes=(q,))


def _overlay_ids(f: TimelineFacts) -> list[str]:
    return [c.id for c in f.clips if re.match(r"^v\d+$", c.track or "") and c.track != "v1"]


def _nth(ids: list[str], n: int) -> str | None:
    idx = n - 1 if n > 0 else len(ids) + n
    return ids[idx] if 0 <= idx < len(ids) else None


#: Words before "animation" that do not name a motion.
_NOT_A_MOTION = frozenset({"all", "every", "any", "each", "the", "my", "this", "that", "these", "those", "its",
                           "clip", "sticker", "overlay", "an", "a", "remove", "delete", "clear", "no", "off"})


def _off_targets(it: Intent, f: TimelineFacts) -> list[tuple[str, dict[str, str]]] | None:
    """(id, the sides to switch off) for "remove all animations" / "remove the
    zoom animation" when no clip is named (Final QA r3), or None to target as
    usual."""
    if not it.get("off") or it.get("clip_ref") or it.get("target") or it.get("all"):
        return None
    kind = it.get("_off_type")
    if not (it.get("_off_all") or kind):
        return None
    out: list[tuple[str, dict[str, str]]] = []
    for cid, desc in f.animations.items():
        sides = dict(part.split(":", 1) for part in desc.split(",") if ":" in part)
        hit = {side: "none" for side, pid in sides.items() if not kind or kind in pid}
        if hit:
            out.append((cid, hit))
    return out


def _targets(it: Intent, f: TimelineFacts) -> tuple[list[str], str] | Expansion:
    """(the clip / sticker ids, words for them) — or the question to ask."""
    target = it.get("target")
    sel = f.selection
    if target == "text":
        return _ask("A title animates with its own In / Out in the Text panel (or say 'add a title that pops "
                    "in'). Which clip or sticker should I animate instead?")
    if target == "sticker":
        ids = list(f.sticker_ids)
        if not ids:
            return _ask("There is no sticker on the timeline yet — add one from the Stickers panel, then say "
                        "'make the sticker bounce in'. Which clip should I animate instead?")
        n = it.get("nth")
        if n is not None:
            got = _nth(ids, int(n))
            if got is None:
                return _ask(f"Which sticker? There {'is only one' if len(ids) == 1 else f'are {len(ids)}'}.")
            return [got], f"sticker {ids.index(got) + 1}" if len(ids) > 1 else "the sticker"
        if len(ids) == 1:
            return [ids[0]], "the sticker"
        if sel in ids:
            return [sel], "the selected sticker"
        return _ask(f"Which sticker? There are {len(ids)} — select one and say 'this sticker', "
                    f"or say 'the first sticker'.")
    if target == "overlay":
        ids = _overlay_ids(f)
        if not ids:
            return _ask("There is no overlay clip — drag a clip onto a track above the main video first. "
                        "Which clip should I animate instead?")
        if len(ids) == 1:
            return [ids[0]], "the overlay"
        if sel in ids:
            return [sel], "the selected overlay"
        return _ask(f"Which overlay? There are {len(ids)} — select one and say 'this clip'.")
    if it.get("all"):
        ids = list(f.v1_clip_ids)
        if not ids:
            return _ask("There is no clip on the main track yet — add one first.")
        return ids, "every main-track clip"
    ref = it.get("clip_ref")
    if ref is None:
        if sel and (sel in f.clip_ids or sel in f.sticker_ids):
            ref = sel
        else:
            return _ask("Which clip should I animate? Name it like 'the second clip' or 'the sticker', "
                        "or select it and say 'this clip'.")
    cid, q = bind_clip(ref, f)
    if q:
        return _ask(q)
    ids = list(f.v1_clip_ids)
    cid = {"$v1_first": ids[0] if ids else None, "$v1_last": ids[-1] if ids else None}.get(cid, cid)
    if cid is None:
        return _ask("There is no clip on the main track yet — add one first.")
    if cid.startswith("$"):
        return [cid], _label(cid, f)       # a live sentinel the executor resolves
    if cid in f.sticker_ids:
        return [cid], "the sticker"
    fc = f.clip(cid)
    if fc is not None and fc.track in ("music", "vo", "a1", "audio") or (fc is None and cid not in f.clip_ids):
        return _ask(f"{_label(cid, f).capitalize()} is sound only — it has no picture to animate. Which clip did you mean?")
    return [cid], _label(cid, f)


def x_animation(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """CapCut In / Out / Combo on the clip(s) or sticker the prompt names."""
    offs = _off_targets(it, f)
    if offs is not None:
        if not offs:
            what = f"no {it.get('_off_type')} animation" if it.get("_off_type") else "no animation"
            return _ask(f"There is {what} on the timeline — nothing to take off.")
        steps, pcs = [], []
        for cid, sides in offs:
            who = "the sticker" if cid in f.sticker_ids else _label(cid, f)
            steps.append(step("set_animation", STAGE_LOOK, f"take the animation off {who}", clip_id=cid, **sides))
            pcs.append(pc("animation_is", f"{who} no longer animates that way", clip_id=cid, **sides))
        return Expansion(steps=tuple(steps), postconditions=tuple(pcs),
                         notes=(f"animation taken off {len(offs)} clip(s)/sticker(s)",))
    got = _targets(it, f)
    if isinstance(got, Expansion):
        return got
    ids, who = got
    target = {"clip_id": ids[0]} if len(ids) == 1 else {"clip_ids": ids}
    if it.get("off"):
        have = [i for i in ids if i in f.animations]
        if not have:
            return _ask(f"{who.capitalize()} has no animation, so there is nothing to take off. "
                        "Which clip or sticker did you mean?")
        return Expansion(
            steps=(step("set_animation", STAGE_LOOK, f"take the animation off {who}",
                        **target, **{"in": "none", "out": "none", "combo": "none"}),),
            postconditions=(pc("animation_is", "the animation is off", clip_id=target.get("clip_id") or ids,
                               **{"in": "none", "out": "none", "combo": "none"}),),
            notes=(f"{who} no longer animates",))
    ask = it.get("_ask")
    kind = it.get("kind")
    if ask == "which" or (not ask and not it.get("preset")):
        return _ask(f"Which animation? In: {_names('in')}. Out: {_names('out')}. "
                    f"Combo (loops): {_names('combo')}. Say like 'add a zoom in animation to {who}'.")
    if ask == "slide":
        side = "in" if kind != "out" else "out"
        return _ask(f"Which way should {who} slide {side} — left, right, up or down? "
                    f"Say like 'slide {who} {side} to the left'." if side == "out" else
                    f"Which way should {who} slide in — left, right, up or down? "
                    f"Say like 'slide {who} in from the right'.")
    if ask == "zoom":
        return _ask(f"Zoom In (grows into place) or Zoom Out (settles down from enlarged) for {who}?")
    pid = it.get("preset")
    if kind not in ("in", "out", "combo") or A.preset(kind, pid) is None:
        return _ask(f"Which animation? In: {_names('in')}. Out: {_names('out')}. Combo (loops): {_names('combo')}.")
    label = A.preset(kind, pid).label
    args: dict[str, Any] = {**target, kind: pid}
    dur = it.get("duration_s")
    if dur is not None and kind != "combo":
        lo, hi = A.ANIM_DUR_RANGE
        args[f"{kind}_duration"] = round(min(hi, max(lo, float(dur))), 2)
    words = {"in": "In", "out": "Out", "combo": "Combo"}[kind]
    check = {"clip_id": target.get("clip_id") or ids, kind: pid}
    # final sweep 3 r2: the other side said in the same sentence ("pop in and
    # spin out", "fade … in and out") — it was dropped without a word
    for side, other in (it.get("_also") or {}).items():
        if kind != "combo" and side in ("in", "out") and side != kind and A.preset(side, other) is not None:
            args[side] = other
            check[side] = other
            label = f"{label} + {A.preset(side, other).label}"
            words = "In and Out"
    return Expansion(
        steps=(step("set_animation", STAGE_LOOK, f"{label} ({words}) on {who}", **args),),
        postconditions=(pc("animation_is", f"{who} animates {label}", **check),),
        notes=(f"{who} {'loops' if kind == 'combo' else 'animates'} {label} ({words})",))


def pair_animation_sides(intents: list, clauses: tuple[str, ...]) -> list:
    """One animation for "make the logo pop in and spin out" (the grammar
    splits it into two clauses and the merge kept only the Out), and "fade it
    out" after an animation on a sticker / overlay is THAT thing's Out — it
    faded the last video clip (final sweep 3 r2)."""
    from .recipes import Intent
    out: list = []
    for it in intents:
        prev = out[-1] if out else None
        if prev is not None and prev.recipe == "animation" and prev.get("kind") in ("in", "out") \
                and prev.get("preset") and not prev.get("off"):
            side = None
            other = None
            if it.recipe == "animation" and it.get("kind") in ("in", "out") and it.get("kind") != prev.get("kind") \
                    and it.get("preset") and not it.get("off") and _same_target(prev, it):
                side, other = it.get("kind"), it.get("preset")
            elif it.recipe == "fade" and prev.get("target") in ("sticker", "overlay") and not it.get("clip_ref") \
                    and re.search(r"\b(?:it|them|this|that)\b", it.clause or "") \
                    and re.search(r"\bout\b", it.clause or "") and not re.search(r"\bin\b", it.clause or ""):
                side, other = "out", "fade_out"
            if side is not None and side not in (prev.get("_also") or {}):
                out[-1] = Intent(prev.recipe, {**prev.slots, "_also": {**(prev.get("_also") or {}), side: other}},
                                 max(prev.score, it.score), prev.clause)
                continue
        out.append(it)
    return out


def _same_target(a, b) -> bool:
    """`b` names nothing of its own, or the same thing as `a`."""
    keys = ("target", "clip_ref", "nth", "all")
    if not any(b.get(k) for k in keys):
        return True
    return all(a.get(k) == b.get(k) for k in keys if b.get(k))


__all__ = ["motion_in", "read_animation", "x_animation", "pair_animation_sides"]
