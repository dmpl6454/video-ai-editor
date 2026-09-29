"""Several clips, one prompt (K3): "speed up clip 2 and clip 3", "mute clip 1
and clip 3", "delete clips 1 and 2", "make every clip except the first one
warm", "slow down clip 2 and speed up clip 3".

The recipe composer keeps ONE intent per recipe (`planner._merge_intents`:
the same recipe asked twice merges its slots — right for "add captions …
yellow captions", wrong for two clips), and a recipe's `clip_ref` names ONE
clip. So every multi-clip phrase above made a wrong edit: the first clip
only, every clip, or one speed for both. `semantics.resolve_scopes` reads
which clips each clause names; this module fans a per-clip intent out into
one intent PER CLIP (bound to the clip's id on the timeline the prompt was
written against, so a delete earlier in the run cannot shift which clip a
later one means), groups them so no group repeats a recipe, and the planner
composes each group and merges the plans in stage order.

Nothing here decides WHAT a clause asks — the intents come from the grammar
and `planner.bind` as before; only WHICH clips.
"""
from __future__ import annotations

import re
from typing import Any

from . import semantics as M
from .facts import TimelineFacts
from .recipes import Intent
from .schema import Plan

#: Recipes whose `clip_ref` names the main-lane clip they edit.
PER_CLIP: frozenset[str] = frozenset({"speed", "mute", "color_look", "reverse", "flip", "volume", "voice_effect",
                                      "zoom", "rotate", "adjust", "delete_clip", "animation", "fade"})


def _resolve(ref: M.ClipRef, facts: TimelineFacts) -> str | None:
    ids = list(facts.v1_clip_ids)
    n = len(ids)
    if not n:
        return None
    if ref == "sel":
        return facts.selection if facts.selection in ids else None
    if ref == "mid":
        return ids[n // 2] if n % 2 else None
    if isinstance(ref, int):
        i = ref - 1 if ref > 0 else n + ref
        return ids[i] if 0 <= i < n else None
    return None


def _targets(scope: M.Scope, facts: TimelineFacts) -> list[str] | None:
    """The ids a clause names when it names SEVERAL (≥ 2) clips, else None."""
    ids = list(facts.v1_clip_ids)
    if len(scope.refs) >= 2:
        got = [_resolve(r, facts) for r in scope.refs]
        if any(g is None for g in got):
            return None
        return list(dict.fromkeys(g for g in got if g))
    if scope.all and scope.except_refs:
        ex = {_resolve(r, facts) for r in scope.except_refs}
        if None in ex:
            return None
        keep = [i for i in ids if i not in ex]
        return keep if keep else None
    return None


#: A clause that is nothing but clip names ("clip 3", "the last one too").
_ONLY_CLIPS_RE = re.compile(r"(?:(?:and|also|plus|the|clip|clips|shot|shots|one|ones|too|as well|number|no|nr"
                            r"|first|second|third|fourth|fifth|last|final|middle|opening|closing"
                            r"|\d{1,2}(?:st|nd|rd|th)?|one|two|three|four|five|six|seven|eight|nine|ten)[\s.,]*)+")


def _clause_audio_lane(it: Intent) -> bool:
    """A volume / mute / fade / voice clause about the music or voice-over."""
    media = M.media_of(it.clause)
    return bool(media) and media[0] in ("music", "vo", "captions", "text", "overlay", "sticker")


def fan_out(intents: list[Intent], clauses: list[str], facts: TimelineFacts) -> list[list[Intent]] | None:
    """Groups of intents to compose separately, or None when every per-clip
    recipe appears once and names at most one clip (the ordinary path)."""
    scopes = dict(zip(clauses, M.resolve_scopes(clauses))) if clauses else {}
    # "mute clip 1 and clip 3": the grammar splits "clip 3" off as a clause
    # of its own with no edit — its clips belong to the clause before it.
    extra: dict[str, list[M.ClipRef]] = {}
    with_intent = {it.clause for it in intents}
    prev: str | None = None
    for c in clauses:
        if c in with_intent:
            prev = c
            continue
        if prev is not None and _ONLY_CLIPS_RE.fullmatch(M.norm(c)) and M.clip_refs(c):
            extra.setdefault(prev, []).extend(M.clip_refs(c))
    expanded: list[Intent] = []
    fanned = False
    for it in intents:
        if it.recipe not in PER_CLIP or _clause_audio_lane(it) or it.get("_everything"):
            # "mute everything except clip 1" scopes itself (it ran once per kept-out clip)
            expanded.append(it)
            continue
        sc = scopes.get(it.clause)
        if sc is not None and extra.get(it.clause):
            sc = M.Scope(refs=tuple(dict.fromkeys([*sc.refs, *extra[it.clause]])), media=sc.media)
        tg = _targets(sc, facts) if sc is not None else None
        if tg:
            fanned = True
            expanded.extend(Intent(it.recipe, {**it.slots, "clip_ref": cid}, it.score, it.clause) for cid in tg)
        else:
            expanded.append(it)
    per_clip = [it for it in expanded if it.recipe in PER_CLIP and not _clause_audio_lane(it)]
    seen: dict[str, set[Any]] = {}
    for it in per_clip:
        seen.setdefault(it.recipe, set()).add(str(it.get("clip_ref")))
    # two RANGES in one prompt ("remove the first second and the last
    # second") are two cuts, never one range overriding the other
    ranges = [it for it in expanded if it.recipe == "trim" and it.get("range") is not None]
    two_ranges = len({repr(it.get("range")) for it in ranges}) > 1
    # "turn the music down and the voice up": two levels, two lanes
    lanes: dict[str, set[str]] = {}
    for it in expanded:
        if it.recipe in ("volume", "mute", "fade"):
            lanes.setdefault(it.recipe, set()).add(f"{it.get('target')}|{it.get('clip_ref')}")
    two_lanes = any(len(v) > 1 for v in lanes.values())
    if not fanned and not two_ranges and not two_lanes and all(len(v) <= 1 for v in seen.values()):
        return None
    groups: list[list[Intent]] = []
    for it in expanded:
        for g in groups:
            if all(x.recipe != it.recipe for x in g):
                g.append(it)
                break
        else:
            groups.append([it])
    return groups if len(groups) > 1 else None


def _cut_start(step) -> float:
    v = step.args.get("start")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _combined_durations(pcs: list, base: float | None) -> list:
    """Several groups each checked the duration their OWN cut leaves against
    the run-start length ("delete clips 1 and 2": two checks of 8 s, and the
    result is 4 s — "done with issues" on a right edit). One check of what
    ALL the cuts leave replaces them when the start length is known."""
    durs = [p for p in pcs if p.check == "duration_between"]
    if len(durs) < 2 or not base:
        return pcs
    removed = 0.0
    for p in durs:
        a = p.args
        if a.get("target") is not None:
            removed += base - float(a["target"])
        elif a.get("start") is not None and a.get("end") is not None:
            removed += float(a["end"]) - float(a["start"])
        else:
            return pcs
    first = durs[0]
    merged = first.model_copy(update={"args": {"target": round(base - removed, 3),
                                               "tol": round(0.1 * len(durs), 3)},
                                      "human": "the duration matches"})
    out, done = [], False
    for p in pcs:
        if p.check == "duration_between":
            if not done:
                out.append(merged)
                done = True
            continue
        out.append(p)
    return out


def merge_plans(plans: list[Plan], *, base_duration: float | None = None) -> Plan:
    """One Plan from the composed groups: steps in stage order (stable), every
    question and postcondition once, the titles joined."""
    first = plans[0]
    steps = [s for p in plans for s in p.steps]
    # stage order; within the cut stage, range cuts LATEST first — each was
    # planned against the timeline as it was, and a cut shifts what follows
    steps.sort(key=lambda s: (s.stage if s.stage is not None else 12,
                              -_cut_start(s) if s.tool == "cut_range" else 0.0))
    pcs, seen = [], set()
    for p in plans:
        for pc in p.postconditions:
            key = (pc.check, repr(sorted(pc.args.items())))
            if key not in seen:
                seen.add(key)
                pcs.append(pc)
    per_plan = []
    for p in plans:
        own: dict[str, Any] = {}
        for x in p.postconditions:
            if x.check == "duration_between":
                own.setdefault(repr(sorted(x.args.items())), x)
        per_plan.extend(own.values())
    if len(per_plan) >= 2:
        pcs = _combined_durations([*per_plan, *[x for x in pcs if x.check != "duration_between"]], base_duration) \
            if base_duration else pcs
    qs, qseen = [], set()
    for p in plans:
        for q in p.needs_input:
            if q.key not in qseen:
                qseen.add(q.key)
                qs.append(q)
    titles = list(dict.fromkeys(p.title for p in plans if p.title))
    replies = list(dict.fromkeys(p.reply for p in plans if p.reply))
    downloads = [d for p in plans for d in p.downloads_needed]
    intents = list(dict.fromkeys(p.intent for p in plans))
    return first.with_(steps=steps[:24], postconditions=pcs[:20], needs_input=qs[:4],
                       title=(" · ".join(titles))[:80] or None, reply=("; ".join(replies))[:400] or None,
                       downloads_needed=downloads[:6], intent="+".join(intents)[:64],
                       estimated_seconds=sum(p.estimated_seconds or 0 for p in plans) or None,
                       confidence=min(p.confidence for p in plans))


__all__ = ["PER_CLIP", "fan_out", "merge_plans"]
