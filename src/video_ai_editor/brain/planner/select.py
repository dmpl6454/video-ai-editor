"""`select` — the reel's window scorer and the whole-sentence duration fit
(spec §4.1.1 steps 2-3), the shape of `ai/shorts._score_window` with
`importance` replacing density:

    0.28·importance_mean + 0.20·energy + 0.24·hook(first) + 0.10·ending
    + 0.18·length_fit − 0.30·dead_air − 0.25·fillers + 0.10·standalone(first)

Lengths are what SURVIVES the tighten cuts (`ctx.state["cuts"]`), so the fit
counts the seconds the viewer will see. The fit drops the lowest-importance
whole sentences from the middle first, never an answer to a kept question,
never the topic peak, keeps ≥ 60 % of the body contiguous, and every join it
makes passes `join_ok` against the NEW predecessor.
"""
from __future__ import annotations

from typing import Callable

from .. import energy as E
from .graph_view import Graph
from .types import Ctx

MIN_BODY_SENTS = 2


def removed_inside(ctx: Ctx, t0: float, t1: float) -> float:
    return sum(max(0.0, min(c["t1"], t1) - max(c["t0"], t0)) for c in ctx.state.get("cuts") or [])


def kept_length(g: Graph, ctx: Ctx, sents: list[dict | None]) -> float:
    """Seconds the viewer sees for these sentences: their padded, merged
    spans minus what tighten removes inside them."""
    pad = float(ctx.energy["keep_pad_s"])
    order = {s["id"]: i for i, s in enumerate(g.sentences)}
    spans: list[list[float]] = []
    for s in sorted((x for x in sents if x), key=lambda x: x["t0"]):
        a, b = max(0.0, s["t0"] - pad), min(g.ref_end, s["t1"] + pad)
        if spans and order[s["id"]] == order[spans[-1][2]] + 1:  # type: ignore[index]
            spans[-1][1] = b
            spans[-1][2] = s["id"]  # type: ignore[assignment]
        else:
            spans.append([a, b, s["id"]])  # type: ignore[list-item]
    return round(sum((b - a) - removed_inside(ctx, a, b) for a, b, _ in spans), 4)


def _window_score(g: Graph, ctx: Ctx, sents: list[dict], target: float, total: float | None = None,
                  opener: dict | None = None) -> float:
    """`opener` is what the viewer hears first — the hook when one opens the
    reel (`open_on`), else the run's first sentence."""
    imp = sum(float(g.scores(s["id"]).get("importance") or 0.0) for s in sents) / len(sents)
    energy = sum(min(1.0, max(0.0, 0.5 + 0.25 * g.rms_z(s))) for s in sents) / len(sents)
    first = opener or sents[0]
    hook = float(g.scores(first["id"]).get("hook") or 0.0)
    ending = 1.0 if sents[-1].get("complete", True) else 0.4
    span = kept_length(g, ctx, sents) if total is None else total
    length_fit = 1.0 - min(1.0, abs(span - target) / max(target, 1.0))
    dead = sum(1 for s in sents for c in ctx.state.get("cuts") or [] if c["code"] == "dead_air" and s["t0"] <= c["t0"] <= s["t1"])
    fillers = sum(int(g.feature(s, "fillers", 0) or 0) for s in sents)
    n = sum(g.n_words(s) for s in sents) or 1
    standalone = float(g.scores(first["id"]).get("standalone") or 0.0)
    return round(0.28 * imp + 0.20 * energy + 0.24 * hook + 0.10 * ending + 0.18 * length_fit
                 - 0.30 * min(1.0, dead / max(1, len(sents))) - 0.25 * min(1.0, 8.0 * fillers / n) + 0.10 * standalone, 6)


def total_length(g: Graph, ctx: Ctx, run: list[dict], hook: dict | None) -> float:
    """The programme's kept seconds: the run, plus the hook when it lies outside it."""
    if hook is not None and not any(s["id"] == hook["id"] for s in run):
        return kept_length(g, ctx, [*run, hook])
    return kept_length(g, ctx, run)


def best_body(g: Graph, ctx: Ctx, asked: float, *, hook: dict | None) -> list[dict]:
    """The best contiguous sentence run whose programme (with the hook) is
    roughly `asked` seconds; ties by (score, earlier, id)."""
    sents = [s for s in g.sentences if not _is_dup(g, s)]
    best: tuple[float, float, str, list[dict]] | None = None
    for i in range(len(sents)):
        for j in range(i + MIN_BODY_SENTS - 1, len(sents)):
            run = sents[i:j + 1]
            total = total_length(g, ctx, run, hook)
            if total > 1.4 * asked and len(run) > MIN_BODY_SENTS:
                break
            if total < 0.6 * asked:
                continue
            score = _window_score(g, ctx, run, asked, total, opener=hook)
            key = (score, -run[0]["t0"], run[0]["id"], run)
            if best is None or key[:3] > best[:3]:
                best = key
    if best is None:                                       # a short source: take everything
        return sents
    return best[3]


#: A later take is preferred unless its delivery scores this much below the earlier one.
RETAKE_DELIVERY_MARGIN = 0.1


def resolve_retakes(g: Graph) -> list[dict]:
    """Decide ONCE, before any pass looks at them, which take of a repeated
    sentence stays (review UX-15). An editor keeps the LAST take — it is the
    one the speaker meant to keep saying — unless its delivery was clearly
    flatter than the earlier one's. The graph flags a pair as
    `{of: first take, dup: second take}`; this rewrites the planner's VIEW of
    those flags so `dup` is always the take that GOES and `of` the take that
    STAYS (the graph on disk and the caller's dict are untouched). Every
    consumer — the reel's window scorer (`_is_dup`), tighten's `repeat`
    removal, the card — then reads the same answer. Idempotent. Returns the
    resolved pairs."""
    from .hooks import delivery
    speech = g.layers.get("speech")
    repeats = [dict(r) for r in ((speech or {}).get("flags") or {}).get("repeats") or []]
    if not repeats:
        return []
    for r in repeats:
        if r.get("resolved"):
            continue
        first, second = g.sentence(str(r.get("of"))), g.sentence(str(r.get("dup")))
        if first is None or second is None:
            continue
        if second["t0"] < first["t0"]:
            first, second = second, first
        keep_later = delivery(g, second) >= delivery(g, first) - RETAKE_DELIVERY_MARGIN
        keep, drop = (second, first) if keep_later else (first, second)
        r.update(of=keep["id"], dup=drop["id"], resolved=True)
    g.layers["speech"] = {**speech, "flags": {**(speech.get("flags") or {}), "repeats": repeats}}   # type: ignore[union-attr]
    return repeats


def retake_kept(g: Graph, sid: str) -> dict | None:
    """The sentence a repeat removed in favour of `sid` (the take that stays), if `sid` is one."""
    r = next((x for x in g.flags.get("repeats") or [] if x.get("of") == sid), None)
    return g.sentence(str(r["dup"])) if r else None


def _is_dup(g: Graph, s: dict) -> bool:
    return any(r.get("dup") == s["id"] for r in g.flags.get("repeats") or [])


def duration_fit(g: Graph, ctx: Ctx, body: list[dict], asked: float, *, hook: dict | None,
                 join_ok: Callable[[dict | None, dict, Graph], bool]) -> list[dict]:
    """Greedy, deterministic: drop the lowest-importance whole sentence from
    the middle while the programme is too long, then trim the tail to a
    complete sentence; every removal's join must pass the guard."""
    tol = min(max(1.0, E.DURATION_TOL_FRAC * asked), 1.0)
    kept = list(body)
    order = {s["id"]: i for i, s in enumerate(g.sentences)}
    kept_q = {s["answer_of"] for s in kept if s.get("answer_of")}
    peak = max(kept, key=lambda s: (float(g.scores(s["id"]).get("importance") or 0.0), -s["t0"])) if kept else None
    while len(kept) > MIN_BODY_SENTS and total_length(g, ctx, kept, hook) - asked > tol:
        middle = kept[1:-1]
        cands = [s for s in middle if not (s.get("answer_of") in kept_q and s.get("answer_of")) and (peak is None or s["id"] != peak["id"])]
        cands.sort(key=lambda s: (float(g.scores(s["id"]).get("importance") or 0.0), -s["t0"]))
        dropped = False
        for s in cands:
            k = kept.index(s)
            if join_ok(kept[k - 1], kept[k + 1], g) and _contiguous_share(kept, s, order) >= E.BODY_CONTIGUOUS_MIN:
                kept.pop(k)
                dropped = True
                break
        if not dropped:
            break
    # too long still: trim whole sentences off the tail, ending on a complete one
    while len(kept) > MIN_BODY_SENTS and total_length(g, ctx, kept, hook) - asked > tol:
        kept.pop()
    while len(kept) > MIN_BODY_SENTS and not kept[-1].get("complete", True):
        kept.pop()
    return kept


def pieces(ctx: Ctx) -> list[tuple[float, float]]:
    """The kept spans in PROGRAMME order (reference seconds): the kept
    windows minus the removals inside them, the hook's span first when the
    reel opens on it. Every edge is already on the frame grid, so the sum
    of the lengths is the programme's length to the frame."""
    windows = [(a, b) for a, b, _ in ctx.state.get("kept") or []]
    cuts = sorted((c["t0"], c["t1"]) for c in ctx.state.get("cuts") or [])
    out: list[tuple[float, float]] = []
    for a, b in sorted(windows):
        cur = a
        for c0, c1 in cuts:
            if c1 <= cur or c0 >= b:
                continue
            if c0 > cur:
                out.append((cur, min(c0, b)))
            cur = max(cur, c1)
        if cur < b:
            out.append((cur, b))
    span = ctx.state.get("hook_span") if ctx.state.get("open_on") else None
    if span is None:
        return out
    front, rest = [], []
    for a, b in out:
        lo, hi = max(a, span[0]), min(b, span[1])
        if hi - lo <= 1e-6:
            rest.append((a, b))
            continue
        front.append((lo, hi))
        rest += [x for x in ((a, lo), (hi, b)) if x[1] - x[0] > 1e-6]
    return front + rest


def programme_time(ctx: Ctx, t: float) -> float | None:
    """Where reference second `t` plays in the programme (None when removed)."""
    at = 0.0
    for a, b in pieces(ctx):
        if a - 1e-6 <= t <= b + 1e-6:
            return round(at + (t - a), 4)
        at += b - a
    return None


def programme_length(ctx: Ctx) -> float:
    return round(sum(b - a for a, b in pieces(ctx)), 4)


def _contiguous_share(kept: list[dict], drop: dict, order: dict[str, int]) -> float:
    ids = [order[s["id"]] for s in kept if s["id"] != drop["id"]]
    if not ids:
        return 0.0
    best = cur = 1
    for a, b in zip(ids, ids[1:]):
        cur = cur + 1 if b == a + 1 else 1
        best = max(best, cur)
    return best / len(ids)


__all__ = ["resolve_retakes", "retake_kept", "kept_length", "total_length", "removed_inside", "best_body", "duration_fit", "pieces", "programme_time",
           "programme_length"]
