"""Where a cut edge lands (spec §4.6.2) and which pauses are protected (§4.6.3).

Whisper's word boundaries are the right neighbourhood and the wrong instant:
a cut exactly at a token edge lands on a plosive's decay or a breath's onset.
Every audio cut edge is therefore placed by the 10 ms envelope:

  1. trough snap — the frame of minimum `env_10ms` within ±80 ms of the word
     boundary that lies OUTSIDE every kept word's voiced span
     `[w.t0 + 0.02, w.t1 − 0.02]`; ties within 1 dB break toward the LATER
     trough for an out-point and the EARLIER for an in-point (leave the
     decay, take the onset clean);
  2. the pad is measured from the trough, never less than 40 ms of air before
     a kept onset;
  3. protected pauses (after emotion or a laugh, before a hard answer, after
     an important conclusion) keep `max(pad, 45 %)` capped at 1.2 s, and a
     turn-boundary pause keeps the Energy table's floor.

Level matching and fades are the tool's and the Reviewer's (EB2).
"""
from __future__ import annotations

from typing import Any

from .. import energy as E
from .graph_view import Graph
from .types import Ctx

Span = tuple[float, float]


def trough_time(env: list[int], hz: int, t: float, *, kind: str, kept: list[Span]) -> float:
    """The envelope's minimum within ±`TROUGH_SEARCH_S` of `t`, outside the
    kept voiced spans; `kind` is "out" (a kept piece ends here) or "in" (a
    kept piece starts here). With no envelope the boundary itself."""
    if not env:
        return round(t, 4)
    f_lo = int(round((t - E.TROUGH_SEARCH_S) * hz))
    f_hi = int(round((t + E.TROUGH_SEARCH_S) * hz))
    voiced = [(a + E.VOICED_INSET_S, b - E.VOICED_INSET_S) for a, b in kept]
    cands: list[int] = []
    for f in range(max(0, f_lo), min(len(env), f_hi + 1)):
        tf = f / hz
        if any(a - 1e-9 <= tf <= b + 1e-9 for a, b in voiced):
            continue
        cands.append(f)
    if not cands:
        return round(t, 4)
    low = min(env[f] for f in cands)
    ties = [f for f in cands if env[f] <= low + E.TIE_DB]
    best = max(ties) if kind == "out" else min(ties)
    return round(best / hz, 4)


def place_cut(env: list[int], hz: int, *, prev_end: float, next_start: float, pad: float,
              kept: list[Span]) -> tuple[float, float]:
    """The removed range between a kept word ending at `prev_end` and one
    starting at `next_start`: pad after the out-trough, pad before the
    in-trough, never inside a word, always ≥ 40 ms of air before the onset."""
    tr_out = trough_time(env, hz, prev_end, kind="out", kept=kept)
    tr_in = trough_time(env, hz, next_start, kind="in", kept=kept)
    t0 = max(prev_end, tr_out + pad)
    t1 = min(tr_in - pad, next_start - E.AIR_MIN_S)
    return round(t0, 4), round(t1, 4)


def protection(g: Graph, ctx: Ctx, before: dict | None, after: dict | None) -> tuple[str | None, list[str]]:
    """Why a pause between sentence `before` and sentence `after` is kept
    (spec §4.6.3), with the graph facts that say so; `(None, [])` when it
    is ordinary air."""
    mode = ctx.energy["protected_pauses"]
    facts: list[str] = []
    if before is not None:
        sc = g.scores(before["id"])
        laugh = _laughter_after(g, before)
        if laugh:
            return "laughter", [before["id"], laugh]
        if mode == "all" and float(sc.get("emotion") or 0.0) >= E.PROTECT_EMOTION:
            return "emotion", [before["id"]]
        if mode == "all" and g.feature(before, "conclusion_marker") and float(sc.get("importance") or 0.0) >= E.PROTECT_IMPORTANCE:
            return "conclusion", [before["id"]]
    if mode == "all" and after is not None:
        q = g.sentence(after.get("answer_of") or "") if after.get("answer_of") else None
        if q is None and before is not None and before.get("is_question") and before.get("spk") != after.get("spk"):
            q = before
        if q is not None and float(g.scores(q["id"]).get("hook") or 0.0) >= E.PROTECT_HOOK:
            return "hard_question", [q["id"], after["id"]]
    return None, facts


def _laughter_after(g: Graph, s: dict) -> str | None:
    for ev in g.audio.get("events") or []:
        if ev.get("kind") in ("laughter", "applause") and s["t1"] - 0.2 <= float(ev["t0"]) <= s["t1"] + 2.0:
            return str(ev["id"])
    return None


def kept_air(ctx: Ctx, pause_s: float, *, why: str | None, turn_boundary: bool) -> float:
    """How much of a pause of `pause_s` survives: the pads, the turn floor, the protection."""
    pad = float(ctx.energy["keep_pad_s"])
    keep = 2.0 * pad
    if turn_boundary:
        keep = max(keep, float(ctx.energy["turn_floor_s"]))
    if why is not None:
        keep = max(keep, min(E.KEEP_PAUSE_MAX_S, max(pad, E.KEEP_PAUSE_FRAC * pause_s)))
    return min(pause_s, round(keep, 4))


def kept_voiced(g: Graph) -> list[Span]:
    """The voiced spans a cut edge must stay out of: every word that is not a filler."""
    return [(w["t0"], w["t1"]) for w in g.words if not w.get("filler")]


#: When two removals meet, the card names the more specific reason.
CODE_PRIORITY = ("false_start", "repeat", "filler", "filler_acoustic", "dead_air", "silence")
#: A kept piece shorter than this that holds no kept word is air between two
#: removals (the pad before a filler that is itself removed): the two are one cut.
MIN_KEPT_PIECE_S = 0.5


def _rank(code: str) -> int:
    return CODE_PRIORITY.index(code) if code in CODE_PRIORITY else len(CODE_PRIORITY)


def _union(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """One removal for two that touch: the span of both, the more specific
    reason (its words), the facts of both."""
    lead, other = (a, b) if _rank(a["code"]) <= _rank(b["code"]) else (b, a)
    facts = list(dict.fromkeys([*lead.get("facts", []), *other.get("facts", [])]))
    out = {**lead, "t0": min(a["t0"], b["t0"]), "t1": max(a["t1"], b["t1"]), "facts": facts[:6]}
    covers = [x["cover"] for x in (a, b) if x.get("cover")]
    if covers:                       # what the removal must take whole, and how far it may reach to do so
        first, last = min(covers, key=lambda c: c[0]), max(covers, key=lambda c: c[1])
        out["cover"] = (first[0], last[1], first[2], last[3])
    return out


def merge_ranges(ranges: list[dict[str, Any]], kept: list[Span] | None = None) -> list[dict[str, Any]]:
    """Disjoint, time-ordered removals. Two that overlap become ONE with the
    more specific reason (a silence that holds an acoustic "uh" is the
    filler's removal, and the card says so); two separated by a sliver of
    air with no kept word in it (`kept` given) are one cut, not two jump
    cuts 0.2 s apart; empties are dropped."""
    out: list[dict[str, Any]] = []
    for r in sorted((dict(x) for x in ranges), key=lambda x: (x["t0"], x["t1"])):
        if r["t1"] - r["t0"] < MIN_CUT_S:
            continue
        if out and r["t0"] < out[-1]["t1"] + 1e-9:
            out[-1] = _union(out[-1], r)
        elif out and kept is not None and _air_only(out[-1]["t1"], r["t0"], kept):
            out[-1] = _union(out[-1], r)
        else:
            out.append(r)
    return out


def _air_only(t0: float, t1: float, kept: list[Span]) -> bool:
    return t1 - t0 < MIN_KEPT_PIECE_S and not any(a < t1 - 1e-6 and t0 < b - 1e-6 for a, b in kept)


def unvoiced_parts(t0: float, t1: float, voiced: list[Span], pad: float) -> list[Span]:
    """Every stretch of `[t0, t1]` that stays `pad` clear of every `voiced`
    run, in time order — what a silence cut may take when sound no word
    names lies inside the word gap (the planner never cuts what it cannot
    hear as silence)."""
    pieces: list[Span] = []
    cur = t0
    for a, b in sorted(voiced):
        if b + pad <= cur or a - pad >= t1:
            continue
        if a - pad > cur:
            pieces.append((cur, a - pad))
        cur = max(cur, b + pad)
    if cur < t1:
        pieces.append((cur, t1))
    return [(round(a, 4), round(b, 4)) for a, b in pieces]


def unvoiced_part(t0: float, t1: float, voiced: list[Span], pad: float) -> Span | None:
    """The longest of `unvoiced_parts` (earlier on a tie), None when nothing is left."""
    pieces = unvoiced_parts(t0, t1, voiced, pad)
    return max(pieces, key=lambda p: (p[1] - p[0], -p[0])) if pieces else None


MIN_CUT_S = 0.02

__all__ = ["trough_time", "place_cut", "protection", "kept_air", "kept_voiced", "merge_ranges", "unvoiced_part", "unvoiced_parts",
           "MIN_CUT_S", "MIN_KEPT_PIECE_S", "CODE_PRIORITY"]
