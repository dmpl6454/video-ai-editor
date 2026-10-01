"""The Story Planner for this wave (spec §4.1.1 reels; §4.1.2 episodes as
chronology with no cold open): the shared antecedent guard `join_ok`, the
reel's hook + body + whole-sentence duration fit, `open_on` when the hook
is not already early and the guard allows it, and the episode's chronology.

Units are SENTENCES (the graph's `s_` ids); scenes are what the reasons
cite. The kept set becomes `keep_window` decisions (one per contiguous
kept span, in the primary file's seconds) and the story beats in the
summary; `select.py` holds the window scorer this pass calls.
"""
from __future__ import annotations

from .. import energy as E
from .. import reasons as R
from . import seams, select
from .graph_view import Graph, content_tokens
from .hooks import standalone
from .types import WORD_EDGE_TOL_S, Ctx, decision, ref


def join_ok(prev: dict | None, nxt: dict, g: Graph) -> bool:
    """The antecedent guard (spec §4.1.1 step 3): a join of two sentences
    that were not adjacent in the source needs the follower's `standalone`
    RECOMPUTED against its new predecessor to be ≥ 0.6. Shared by every
    mode. `prev is None` is the front of the programme."""
    order = g.sentences
    idx = {s["id"]: i for i, s in enumerate(order)}
    adjacent = prev is not None and idx.get(prev["id"], -9) == idx.get(nxt["id"], -1) - 1
    if adjacent:
        return True
    score = standalone(g, nxt, prev, adjacent=False)
    if score < E.JOIN_STANDALONE_MIN and prev is not None:
        # a shared content word restores the antecedent ("that lens" after a sentence about the lens)
        if content_tokens(nxt.get("text", "")) & content_tokens(prev.get("text", "")) and g.first_token(nxt) not in ("and", "but", "so", "or"):
            return True
    return score >= E.JOIN_STANDALONE_MIN


def _kept_windows(g: Graph, ctx: Ctx, sents: list[dict]) -> list[tuple[float, float, list[str]]]:
    """Contiguous runs of kept sentences → (t0, t1, sent ids) in reference seconds, padded."""
    pad = float(ctx.energy["keep_pad_s"])
    order = {s["id"]: i for i, s in enumerate(g.sentences)}
    runs: list[list[dict]] = []
    for s in sorted(sents, key=lambda x: x["t0"]):
        if runs and order[s["id"]] == order[runs[-1][-1]["id"]] + 1:
            runs[-1].append(s)
        else:
            runs.append([s])
    out = []
    for run in runs:
        t0, t1 = _span(g, ctx, run[0]["t0"], run[-1]["t1"], pad)
        out.append((t0, t1, [s["id"] for s in run]))
    return out


def _span(g: Graph, ctx: Ctx, s0: float, s1: float, pad: float) -> tuple[float, float]:
    """A kept span around the speech `[s0, s1]`: it opens where a removal
    just before it ends and closes where the next begins (so no sliver of
    air is left between two seams), else the pad; both edges on the primary
    file's frame grid, rounded OUTWARD (what surrounds it is a removal)."""
    cuts = ctx.state.get("all_cuts") or ctx.state.get("cuts") or []
    before = [c["t1"] for c in cuts if s0 - seams.MIN_KEPT_PIECE_S <= c["t1"] <= s0 + 1e-6]
    after = [c["t0"] for c in cuts if s1 - 1e-6 <= c["t0"] <= s1 + seams.MIN_KEPT_PIECE_S]
    t0 = max(before) if before else max(0.0, s0 - pad)
    t1 = min(after) if after else min(g.ref_end, s1 + pad)
    # an edge the pad or a removal put INSIDE a word (a filler that leads the sentence, the tail of the sentence
    # before) keeps a piece of that word: it moves to the word's far edge and rounds INWARD (closer review: 'Um,'
    # at 2507.42-2507.78, a window at 2507.6, "no cut lands inside a word", a reel that could not be made)
    w0, w1 = _word_around(g, t0), _word_around(g, t1)
    t0, t1 = (w0["t1"] if w0 else t0), (w1["t0"] if w1 else t1)
    f0 = ctx.removal_start(g.to_file(t0, ctx.primary)) if w0 else ctx.removal_end(g.to_file(t0, ctx.primary))
    f1 = ctx.removal_end(g.to_file(t1, ctx.primary)) if w1 else ctx.removal_start(g.to_file(t1, ctx.primary))
    # and the grid itself must not put one back inside a word the words under it are not on (a window never keeps
    # a piece of a word at its edge: the word goes with the removal beside it)
    f0 = ctx.word_safe(f0, start=False, whole=lambda _w: True)        # the window's start ENDS the removal before it
    f1 = ctx.word_safe(min(f1, g.duration_of(ctx.primary)), start=True, whole=lambda _w: True)
    return g.to_ref(f0, ctx.primary), g.to_ref(f1, ctx.primary)


def _word_around(g: Graph, t: float) -> dict | None:
    """The word a cut at reference second `t` would split, or None."""
    return g.word_straddling(t, WORD_EDGE_TOL_S)


def _clip_cuts(ctx: Ctx, windows: list[tuple[float, float, list[str]]]) -> None:
    """A reel keeps only the removals inside a kept window (the keep step
    removes the rest), clipped to it."""
    ctx.state["all_cuts"] = list(ctx.state.get("cuts") or [])
    out = []
    for c in ctx.state["all_cuts"]:
        for a, b, _ in windows:
            lo, hi = max(c["t0"], a), min(c["t1"], b)
            if hi - lo >= ctx.frame() - 1e-6 and lo > a + 1e-6 and hi < b - 1e-6:
                out.append({**c, "t0": lo, "t1": hi})
    ctx.state["cuts"] = out


#: The reel is fitted to the asked length in AIR once whole sentences are
#: chosen: a pause this plan shortens gives back up to AIR_GIVE_MAX_S of
#: itself (never below a cut of AIR_CUT_MIN_S), or loses one more frame a side.
#: The air is SPREAD — always the pause that has given least — and never turns
#: a pause into a hole: what is left of it stays under the length at which a reel's
#: tighten itself cuts a pause (the smaller of `min_silence_s` and half `dead_air_s`,
#: 0.6 s at energy 5), so a length is never bought with a ~1 s gap between two
#: sentences (EB1 UX-11: 0.45 s on the two longest pauses made 0.98 s holes).
AIR_FIT_TOL_S = 0.2
AIR_GIVE_MAX_S = 0.3        # what tighten itself keeps of a pause (2 x the 0.15 s pad): a pause never gets back more than it kept
AIR_CUT_MIN_S = 0.2
AIR_CODES = ("silence", "dead_air")
AIR_NOTE_S = 0.5            # further than this from the asked length (the air is capped): say so


def hole_limit(ctx: Ctx) -> float:
    """The most a pause the reel gives air back at may be left as."""
    return min(float(ctx.energy["min_silence_s"]), float(ctx.energy["dead_air_s"]) / 2.0)


def _hole(runs: list[tuple[float, float]], c: dict) -> float:
    """What is left of the pause a removal was cut from: the seconds between the sound that ends before it
    and the sound that follows it, once the removal itself is taken out."""
    prev_end = max((b for _a, b in runs if b <= c["t0"] + 1e-6), default=c["t0"])
    next_start = min((a for a, _b in runs if a >= c["t1"] - 1e-6), default=c["t1"])
    return (c["t0"] - prev_end) + (next_start - c["t1"])


def _air_fit(g: Graph, ctx: Ctx, asked: float) -> float:
    """Spend or save air at the pauses until the programme is `asked` long
    (to the frame where the pauses allow); returns the programme length."""
    f = ctx.frame()
    runs = g.voiced_runs
    limit = hole_limit(ctx)
    for _ in range(96):
        total = select.programme_length(ctx)
        short = asked - total
        if abs(short) <= AIR_FIT_TOL_S:
            break
        cuts = ctx.state["cuts"]
        if short > 0:
            cands = [c for c in cuts if c["code"] in AIR_CODES and c.get("given", 0.0) + f <= AIR_GIVE_MAX_S + 1e-6
                     and (c["t1"] - c["t0"]) - f >= AIR_CUT_MIN_S and _hole(runs, c) + f <= limit + 1e-6]
            pick = min(cands, key=lambda c: (round(c.get("given", 0.0), 4), -(c["t1"] - c["t0"]), c["t0"]), default=None)
            if pick is None:
                break
            pick["t0"] = round(pick["t0"] + f, 4)
            pick["given"] = round(pick.get("given", 0.0) + f, 4)
        else:
            cands = [c for c in cuts if c["code"] in AIR_CODES and c.get("given", 0.0) - f >= -f - 1e-6]
            pick = min(cands, key=lambda c: (c.get("given", 0.0) * -1.0, c["t0"]), default=None)
            if pick is None:
                break
            pick["t0"] = round(pick["t0"] - f, 4)
            pick["given"] = round(pick.get("given", 0.0) - f, 4)
    _regrid(g, ctx)
    return select.programme_length(ctx)


def _regrid(g: Graph, ctx: Ctx) -> None:
    for c in ctx.state["cuts"]:
        c["t0"] = g.to_ref(ctx.removal_start(g.to_file(c["t0"], ctx.primary)), ctx.primary)


def _pick_hook(g: Graph, ctx: Ctx) -> tuple[float, dict, list[str]] | None:
    for v, sid, names in ctx.state.get("hooks") or []:
        s = g.sentence(sid)
        if s is not None and float(g.scores(sid).get("standalone") or 0.0) >= E.HOOK_STANDALONE_MIN:
            return v, s, names
    return None


def _decide_open_on(g: Graph, ctx: Ctx, hook_s: dict, body: list[dict], asked: float) -> bool:
    """`open_on` (spec §4.1.1 step 5) for reels ≤ 60 s in the styles that
    want it, unless the hook already opens the body or the guard fails.
    (A hook that starts a few seconds into the body still moves: a viewer
    decides in the first 3 s, so "already inside the first 8 s" is read as
    "already the first sentence" — EB1 lane note.)"""
    if ctx.style != "viral_reel" or asked > E.OPEN_ON_MAX_S:
        if body and body[0]["id"] != hook_s["id"]:
            ctx.defer("open_on", f"{ctx.style} keeps the chronology; the hook stays where it was said")
        return False
    if body and body[0]["id"] == hook_s["id"]:
        return False
    if not _open_on_guard_ok(g, hook_s, body):
        ctx.defer("open_on", "the line after the hook leans on it; kept chronological")
        return False
    return True


def _reel(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    asked = float(ctx.asked_s or 45.0)
    hook = _pick_hook(g, ctx)
    hook_s = hook[1] if hook else None
    body = select.best_body(g, ctx, asked, hook=hook_s)
    body = select.duration_fit(g, ctx, body, asked, hook=hook_s, join_ok=join_ok)
    kept = list(body)
    open_on = False
    if hook_s is not None:
        if not any(s["id"] == hook_s["id"] for s in body):
            kept.append(hook_s)
        open_on = _decide_open_on(g, ctx, hook_s, body, asked)
    kept = sorted({s["id"]: s for s in kept}.values(), key=lambda s: s["t0"])
    windows = _kept_windows(g, ctx, kept)
    ctx.state["kept"] = windows
    ctx.state["kept_sents"] = [s["id"] for s in kept]
    ctx.state["hook"] = hook_s
    ctx.state["open_on"] = open_on
    _clip_cuts(ctx, windows)
    pad = float(ctx.energy["keep_pad_s"])
    ctx.state["hook_span"] = _span(g, ctx, hook_s["t0"], hook_s["t1"], pad) if hook_s is not None else None
    total = _air_fit(g, ctx, asked)
    ctx.state["duration_s"] = round(total, 4)
    if abs(total - asked) > AIR_NOTE_S:
        ctx.defer(f"{asked:g} s", f"{R.dur(total)} of whole sentences is the closest fit")
    topic = real_topic(g)
    tid = topic_id(g)
    for t0, t1, sids in windows:
        is_hook = hook_s is not None and sids == [hook_s["id"]] and open_on
        code = "duration_fit" if is_hook else "best_window"
        fields = ({"kept": R.dur(select.kept_length(g, ctx, [g.sentence(x) for x in sids])), "tc": R.tc(g.sentence(sids[0])["t0"], ctx.fps),
                   **({"topic": topic} if topic else {}), **_retake_field(g, ctx, sids)} if code == "best_window"
                  else {"kept": R.dur(total), "asked": f"{asked:g} s"})
        decisions.append(decision("keep_window", ref=ref(ctx.primary, g.to_file(t0, ctx.primary), g.to_file(t1, ctx.primary)),
                                  reason=R.reason(code, sids[:3] + ([tid] if tid else []), **fields), score=0.81, confidence=0.86))
    if open_on and hook_s is not None and hook is not None:
        v, _, names = hook
        h0, h1 = ctx.state["hook_span"]
        decisions.append(decision("open_on", ref=ref(ctx.primary, g.to_file(h0, ctx.primary), g.to_file(h1, ctx.primary)),
                                  params={"sent": hook_s["id"]},
                                  reason=R.reason("hook_strongest_opening", [hook_s["id"]], axes=" + ".join(names) or "standalone",
                                                  score=f"{v:.2f}"), score=v, confidence=0.8))
    ctx.state["story"] = _beats(g, kept, hook_s if open_on else None)
    return decisions


def _retake_field(g: Graph, ctx: Ctx, sids: list[str]) -> dict[str, str]:
    """A window that holds the take that STAYS of a repeated sentence says
    which earlier take went with it (the earlier take lies outside every
    window, so no cut names it — review UX-15)."""
    kept = set(ctx.state.get("kept_sents") or [])
    for sid in sids:
        gone = select.retake_kept(g, sid)
        if gone is not None and gone["id"] not in kept:
            return {"retake_of": R.tc(gone["t0"], ctx.fps)}
    return {}


#: What a topic title says when nothing was learnt about the subject: the
#: content type or the fallback words. Never shown as "about …".
_GENERIC_TOPICS = frozenset({"talking head", "talking_head", "podcast", "interview", "reel", "episode", "footage", "the footage",
                             "video", "untitled", "monologue", "conversation"})


def real_topic(g: Graph) -> str | None:
    """The first topic's title when it names a subject, else None."""
    title = str(((g.header.get("topics") or [{}])[0]).get("title") or "").strip()
    return title if title and title.lower() not in _GENERIC_TOPICS else None


def topic_id(g: Graph) -> str | None:
    tops = g.header.get("topics") or []
    return str(tops[0]["id"]) if tops else None


def _open_on_guard_ok(g: Graph, hook_s: dict, body: list[dict]) -> bool:
    """`open_on` is skipped when the sentence AFTER the hook's original
    position fails the guard against the one before it."""
    order = g.sentences
    i = next((k for k, s in enumerate(order) if s["id"] == hook_s["id"]), None)
    if i is None:
        return False
    kept_ids = {s["id"] for s in body}
    after = next((s for s in order[i + 1:] if s["id"] in kept_ids), None)
    before = next((s for s in reversed(order[:i]) if s["id"] in kept_ids), None)
    if after is None:
        return True
    return join_ok(before, after, g)


def _beats(g: Graph, kept: list[dict], hook_s: dict | None) -> list[dict]:
    body = [s for s in kept if hook_s is None or s["id"] != hook_s["id"]]
    beats: list[dict] = []
    if hook_s is not None:
        beats.append({"beat": "hook", "sents": [hook_s["id"]]})
    if not body:
        return beats
    payoff = next((s for s in reversed(body) if g.feature(s, "conclusion_marker")), body[-1])
    rest = [s for s in body if s["id"] != payoff["id"]]
    ctx_n = max(1, len(rest) // 3) if len(rest) > 2 else len(rest)
    if rest[:ctx_n]:
        beats.append({"beat": "context", "sents": [s["id"] for s in rest[:ctx_n]]})
    if rest[ctx_n:]:
        beats.append({"beat": "information", "sents": [s["id"] for s in rest[ctx_n:]]})
    beats.append({"beat": "payoff", "sents": [payoff["id"]]})
    return beats


def _episode(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    ctx.state["kept"] = []
    ctx.state["kept_sents"] = [s["id"] for s in g.sentences]
    ctx.state["hook"] = None
    ctx.state["open_on"] = False
    ctx.state["duration_s"] = round(g.ref_end - sum(c["t1"] - c["t0"] for c in ctx.state.get("cuts") or []), 4)
    ctx.state["story"] = [{"beat": "chronology", "sents": [s["id"] for s in g.sentences]}]
    ctx.defer("cold open", "an episode's duplicated cold open and chapters arrive next wave; chronological")
    return decisions


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    return _reel(g, ctx, decisions) if ctx.reel else _episode(g, ctx, decisions)


__all__ = ["run", "join_ok", "topic_id", "real_topic"]
