"""Smart punch-ins (spec §4.4) and the single-camera jump-cut hides (§4.3
rule 1b).

A punch-in is a SHOT, not a pump on the last word: it starts at the clause
that contains the peak (≥ 0.8 s before it), holds through the sentence and
releases as a STEP at the next seam — a tighten cut, a camera switch, the
kept window's end — which costs no key: the piece ends at its held scale
and the next piece starts at 1.0. With no seam within 8 s it eases out over
≥ 1.2 s ending on a sentence boundary. `Keyframe.interp` is ONE mode per
property, so a punch emits `ease-out` for a step release and `ease-in-out`
for a timed one, never two modes; a piece (the span between two seams) gets
one keyed decision at most.

Key times are in the primary file's seconds; the resolver maps each to a
clip and a clip-local time (`params.keys[].t`, `.values`, `.interp`).

A hide (`jump_cut_hide`) is a HOLD: two keys with one value, so the export —
which draws a Keyframe only from two keys — and the browser preview show the
same picture. It is claimed only where the piece before the seam and the piece
it opens differ by at least `E.HIDE_STEP_MIN` as DECODED (an unkeyed piece is
1.0, a punched piece ends at what its release holds); `params` carry both
numbers (`from_scale`, `scale`, `step`) so the card can say what it did.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .. import energy as E
from .. import reasons as R
from .graph_view import CONJUNCTIONS, Graph, content_tokens
from .types import Ctx, decision, r4, ref

EMPHASIS_Z = 1.5
EMOTION_MIN = 0.6
IMPORTANCE_MIN = 0.5
CLAUSE_PAUSE_S = 0.25
PROPS = ["scale", "x", "y"]
SEAM_WEIGHT_MAX_S = 3.0


def _seams(g: Graph, ctx: Ctx) -> list[float]:
    ts: set[float] = set()
    for c in ctx.state.get("cuts") or []:
        ts.add(r4(c["t0"]))
        ts.add(r4(c["t1"]))
    for t0, t1, _ in ctx.state.get("kept") or []:
        ts.add(r4(t0))
        ts.add(r4(t1))
    span = ctx.state.get("hook_span")
    if span is not None and ctx.state.get("open_on"):
        ts.add(r4(span[0]))
        ts.add(r4(span[1]))
    ts.update(r4(t) for t in ctx.state.get("switch_times") or [])
    ts.add(r4(g.ref_end))
    ts.add(0.0)
    return sorted(ts)


def _piece(seams: list[float], t: float) -> tuple[float, float]:
    before = [s for s in seams if s <= t + 1e-6]
    after = [s for s in seams if s > t + 1e-6]
    return (before[-1] if before else 0.0, after[0] if after else t)


def _peak_word(g: Graph, s: dict) -> dict | None:
    """The loudest CONTENT word (emphasis lands on a word that carries
    meaning; "the" before a stressed word is loud and short, and is not it)."""
    words = [w for w in g.words_of(s["id"]) if not w.get("filler")]
    if not words:
        return None
    words = [w for w in words if content_tokens(w.get("text", ""))] or words
    return max(words, key=lambda w: (round(g.env_mean(w["t0"], w["t1"]) or -99.0, 3), len(w["text"]), w["t0"]))


def _clause_start(g: Graph, s: dict, peak: dict) -> dict:
    """The latest clause boundary ≥ LEAD_S before the peak; never before the sentence."""
    words = [w for w in g.words_of(s["id"]) if not w.get("filler")]
    starts = [words[0]]
    for a, b in zip(words, words[1:]):
        if a["text"].rstrip().endswith((",", ";", ":")) or b["text"].lower().strip(",.") in CONJUNCTIONS \
                or b["t0"] - a["t1"] >= CLAUSE_PAUSE_S:
            starts.append(b)
    ok = [w for w in starts if peak["t0"] - w["t0"] >= E.LEAD_S - 1e-9]
    return ok[-1] if ok else words[0]


def _candidates(g: Graph, ctx: Ctx) -> list[tuple[dict, str]]:
    kept = set(ctx.state.get("kept_sents") or [s["id"] for s in g.sentences])
    dups = {r.get("dup") for r in g.flags.get("repeats") or []}
    out: list[tuple[dict, str]] = []
    hook = ctx.state.get("hook")
    for s in g.sentences:
        if s["id"] not in kept or s["id"] in dups:
            continue
        turn = g.turn_of(s["id"])
        if turn is not None and len(g.turns) > 1 and (turn["t1"] - turn["t0"] < E.BACKCHANNEL_MAX_S or g.is_backchannel(turn)):
            continue                                           # a "mm-hm" is never a shot
        if hook is not None and s["id"] == hook["id"] and ctx.reel:
            out.append((s, "hook_emphasis"))
            continue
        sc = g.scores(s["id"])
        loud, moved = g.rms_z(s) >= EMPHASIS_Z, float(sc.get("emotion") or 0.0) >= EMOTION_MIN
        weighty = float(sc.get("importance") or 0.0) >= IMPORTANCE_MIN or bool(g.feature(s, "conclusion_marker"))
        # a line the speaker BOTH raised and coloured is emphasis by delivery
        # alone: measured on fixture TH, §3.4's importance tops out at 0.30,
        # so §4.4's 0.5 floor by itself would never punch in on real footage
        if (loud or moved) and (weighty or (loud and moved)):
            out.append((s, "emphasis_peak"))
    return out


def _release(g: Graph, s: dict, seams: list[float]) -> tuple[str, float]:
    nxt = next((t for t in seams if t >= s["t1"] - 1e-6), None)
    if nxt is not None and nxt - s["t1"] <= E.RELEASE_WINDOW_S:
        return "step", nxt
    t_rel = s["t1"]
    end = next((x["t1"] for x in g.sentences if x["t1"] >= t_rel + E.RELEASE_MIN_S - 1e-6), t_rel + E.RELEASE_MIN_S)
    return "ease", r4(end)


def _key(g: Graph, ctx: Ctx, t: float, scale: float, interp: str) -> dict:
    return {"t": g.to_file(t, ctx.primary), "values": {"scale": scale, "x": 0.0, "y": 0.0}, "interp": interp}


def _punches(g: Graph, ctx: Ctx, seams: list[float]) -> list[dict]:
    gap = E.REEL_PUNCH_GAP_S if ctx.reel else float(ctx.energy["punch_gap_s"])
    push = float(ctx.energy["punch_push_s"])
    out: list[dict] = []
    last_t, last_scale = -1e9, 0.0
    used: set[tuple[float, float]] = set()
    cands = []
    for s, code in _candidates(g, ctx):
        peak = _peak_word(g, s)
        if peak is None:
            continue
        clause = _clause_start(g, s, peak)
        when = _programme_t(ctx, float(clause["t0"]))
        if when is not None:
            cands.append((when, s, code, peak, clause))
    for when, s, code, peak, clause in sorted(cands, key=lambda c: (c[0], c[1]["id"])):
        t_in = float(clause["t0"])
        piece = _piece(seams, t_in)
        if when - last_t < gap or piece in used:
            continue
        scale = E.PUNCH_HOOK_SCALE if code == "hook_emphasis" else float(ctx.energy["punch_scale"])
        if abs(scale - last_scale) < 1e-9:
            scale = round(scale - 0.02 if scale >= 1.10 else scale + 0.02, 2)
        mode, t_rel = _release(g, s, seams)
        interp = "ease-out" if mode == "step" else "ease-in-out"
        keys = [_key(g, ctx, t_in, 1.0, interp), _key(g, ctx, min(t_in + push, s["t1"]), scale, interp)]
        if mode == "ease":
            keys += [_key(g, ctx, s["t1"], scale, interp), _key(g, ctx, t_rel, 1.0, interp)]
        fields = ({"rms_z": f"{g.rms_z(s):.1f}", "stretch": f"{g.stretch(s):.2f}"} if code == "emphasis_peak" else {})
        out.append(decision(
            "punch_in", ref=ref(ctx.primary, g.to_file(t_in, ctx.primary), g.to_file(s["t1"], ctx.primary)),
            params={"scale": scale, "interp": interp, "props": list(PROPS), "keys": keys, "push_s": push,
                    "release": f"{mode}@{g.to_file(t_rel, ctx.primary)}", "peak_word": peak["id"], "clause_start": clause["id"],
                    "piece": [g.to_file(piece[0], ctx.primary), g.to_file(piece[1], ctx.primary)]},
            reason=R.reason(code, [s["id"], peak["id"]], **fields), score=0.8, confidence=0.77, optional=True))
        used.add(piece)
        last_t, last_scale = when, scale
    return out


def _programme_t(ctx: Ctx, t: float) -> float | None:
    """The cadence is counted where the viewer sees it: in programme time
    on a reel (the hook plays first wherever it was said), else as said."""
    if not ctx.reel:
        return t
    from .select import programme_time
    return programme_time(ctx, t)


@dataclass(frozen=True)
class _Piece:
    """One kept stretch of the programme, in reference seconds."""
    t0: float
    t1: float
    opens: str                 # first | hide (a ≥ 0.4 s removal ends here) | switch | join
    entry: float = 1.0         # the scale a punch fixes at the piece's start …
    exit: float = 1.0          # … and at its end (the held scale of a step release)
    punched: int | None = None  # index of the punch decision keyed on this piece
    peak: float = 1.0          # what that punch pushes to
    jump_s: float = 0.0        # how much source the seam that opens it skipped (a bigger jump is worth hiding first)


def _spans(g: Graph, ctx: Ctx) -> list[tuple[float, float]]:
    """The kept spans in programme order: a reel's (windows minus removals,
    the hook first), else the whole clip minus its removals."""
    from .select import pieces
    if ctx.reel and ctx.state.get("kept"):
        return pieces(ctx)
    out, cur = [], 0.0
    for c in sorted(ctx.state.get("cuts") or [], key=lambda c: c["t0"]):
        if c["t0"] - cur > 1e-6:
            out.append((cur, c["t0"]))
        cur = max(cur, c["t1"])
    if g.ref_end - cur > 1e-6:
        out.append((cur, g.ref_end))
    return out


def _programme(g: Graph, ctx: Ctx, punches: list[dict]) -> list[_Piece]:
    """Pieces in programme order with what a piece OPENS on (a hide seam: a
    ≥ 0.4 s removal, or a join of two stretches the story pass put next to
    each other; a camera change; a plain join) and, for a punched one, the
    scale it holds."""
    hide_at = {r4(t) for t, _ in ctx.state.get("hide_seams") or []}
    switches = {r4(t) for t in ctx.state.get("switch_times") or []}
    half = ctx.frame() / 2.0
    held: dict[float, tuple[int, float, float, float]] = {}
    for i, p in enumerate(punches):
        k0 = p["params"]["keys"][0]
        peak = float(p["params"]["scale"])
        held[g.to_ref(float(k0["t"]), ctx.primary)] = (i, float(k0["values"]["scale"]), peak if str(p["params"]["release"]).startswith("step") else 1.0, peak)
    out: list[_Piece] = []
    for a, b in _spans(g, ctx):
        cuts_at = sorted({a, *(t for t in switches if a + 1e-6 < t < b - 1e-6)})
        for i, t0 in enumerate(cuts_at):
            t1 = cuts_at[i + 1] if i + 1 < len(cuts_at) else b
            cut_here = i == 0 and (r4(t0) in hide_at or bool(out) and abs(t0 - out[-1].t1) >= E.HIDE_CUT_MIN_S - E.HIDE_TOL_S)
            opens = "first" if not out else "switch" if any(abs(x - t0) <= half for x in switches) else "hide" if cut_here else "join"
            key = next((t for t in held if t0 - 1e-6 <= t < t1 - 1e-6), None)
            idx, entry, exit_, peak = held[key] if key is not None else (None, 1.0, 1.0, 1.0)
            out.append(_Piece(t0, t1, opens, entry, exit_, idx, peak, abs(t0 - out[-1].t1) if out and i == 0 else 0.0))
    return out


def _seam_facts(ctx: Ctx, seam: float) -> list[str]:
    """The graph ids that say why there is a seam at `seam`: the removal that ends
    there, else the sentence the kept window that opens there starts with."""
    for t, facts in ctx.state.get("hide_seams") or []:
        if abs(t - seam) < 1e-6:
            return list(facts)
    for t0, _t1, sids in ctx.state.get("kept") or []:
        if abs(t0 - seam) < 1e-3 and sids:
            return list(sids[:1])
    return []


def _options(p: _Piece, hide_scale: float, need: float) -> list[tuple[float, float]]:
    """(entry, exit) a piece may take. An unkeyed one is 1.0 or a hold; a punched one keeps its own entry and exit —
    or, where the push starts later, opens on a hold the push then continues from (`HIDE_PUNCH_ROOM` left to push)."""
    holdable = p.opens == "hide" and p.t1 - p.t0 >= need
    if p.punched is not None:
        pushed = _raised(p.peak, hide_scale) if p.exit != 1.0 else 1.0      # a step release holds the (raised) peak
        return [(p.entry, p.exit)] + ([(hide_scale, pushed)] if holdable and p.peak >= hide_scale - 1e-9 else [])
    return [(1.0, 1.0)] + ([(hide_scale, hide_scale)] if holdable else [])


def _choose_entries(pieces: list[_Piece], hide_scale: float, ctx: Ctx) -> list[float]:
    """The scale each piece OPENS at, chosen so that the seams worth most show a
    step of at least `HIDE_STEP_MIN` (a seam is worth 1 s + the source it skipped,
    up to `SEAM_WEIGHT_MAX_S`), with the fewest holds. A hold is only ever placed
    where it is itself the step at the seam it opens. The chain is decoded piece
    by piece, so a skipped seam cannot leave the alternation out of step with
    what is on screen."""
    need = (E.HIDE_KEY_LAG_FRAMES + 1) * ctx.frame()
    best: dict[float, tuple[tuple[float, int], list[float]]] = {1.0: ((0.0, 0), [])}
    for i, p in enumerate(pieces):
        nxt: dict[float, tuple[tuple[float, int], list[float]]] = {}
        for prev_exit, (score, path) in best.items():
            for entry, exit_ in _options(p, hide_scale, need):
                shown = i > 0 and p.opens == "hide" and abs(prev_exit - entry) >= E.HIDE_STEP_MIN - 1e-9
                hold = entry != p.entry if p.punched is not None else entry == hide_scale
                if hold and not shown:
                    continue
                worth = (1.0 + min(SEAM_WEIGHT_MAX_S, p.jump_s)) if shown else 0.0     # a bigger jump is hidden first
                cand = ((round(score[0] + worth, 6), score[1] - int(hold)), path + [entry])
                if exit_ not in nxt or cand[0] > nxt[exit_][0]:
                    nxt[exit_] = cand
        best = nxt
    return max(best.values(), key=lambda v: v[0])[1]


def _shown_seam(g: Graph, ctx: Ctx, seam: float) -> float:
    """The seam on the frame grid of the picture that PLAYS there: with two cameras whose files start at different
    instants, the other angle's frame boundaries are not the primary's (at 29.97 fps, half a frame off), and the
    piece that opens the seam starts on ITS grid — a hide stated on the primary's would sit before its own piece."""
    line = ctx.state.get("angle_line") or []
    key = next((k for t, k in reversed(line) if t <= seam + 1e-6), ctx.primary)
    if key == ctx.primary:
        return seam
    return g.to_ref(ctx.removal_start(g.to_file(seam, key)), key)


def _hide_decision(g: Graph, ctx: Ctx, p: _Piece, level: float, from_scale: float, interp: str) -> dict:
    """The claim that the seam opening `p` is a step from `from_scale` to `level`: two keys holding `level`."""
    seam = r4(_shown_seam(g, ctx, r4(p.t0)))
    lag = E.HIDE_KEY_LAG_FRAMES * ctx.frame()
    piece_file = [g.to_file(seam, ctx.primary), g.to_file(p.t1, ctx.primary)]
    return decision(
        "jump_cut_hide", ref=ref(ctx.primary, g.to_file(seam, ctx.primary), g.to_file(seam + ctx.frame(), ctx.primary)),
        params={"scale": level, "from_scale": r4(from_scale), "step": r4(abs(level - from_scale)), "interp": interp,
                "props": list(PROPS), "keys": [_key(g, ctx, seam, level, interp), _key(g, ctx, seam + lag, level, interp)],
                "piece": piece_file},
        reason=R.reason("jump_cut_hide", _seam_facts(ctx, r4(p.t0)), tc=R.tc(seam, ctx.fps),
                        from_scale=from_scale, scale=level), score=0.7, confidence=0.8, optional=True)


def _raised(punch_scale: float, level: float) -> float:
    """What a punch that opens on the hold `level` pushes to: its own scale, and never less than `HIDE_PUNCH_ROOM`
    above the hold (1.08 -> 1.10 is 2 %, not a push the viewer can see)."""
    return round(max(punch_scale, level + E.HIDE_PUNCH_ROOM), 4)


def _open_on_hold(punch: dict, level: float) -> dict:
    """The punch, continued from the hold that opens its piece (its first key starts at `level`, not at 1.0) and, where
    that leaves it under `HIDE_PUNCH_ROOM` to push, pushing to `level + HIDE_PUNCH_ROOM` instead."""
    was = float(punch["params"]["scale"])
    peak = _raised(was, level)
    keys = [{**punch["params"]["keys"][0], "values": {**punch["params"]["keys"][0]["values"], "scale": level}}]
    for k in punch["params"]["keys"][1:]:
        v = k["values"]
        keys.append({**k, "values": {**v, "scale": peak}} if abs(float(v["scale"]) - was) < 1e-9 else k)
    return {**punch, "params": {**punch["params"], "keys": keys, "entry_scale": level, "scale": peak}}


def _hides(g: Graph, ctx: Ctx, punches: list[dict]) -> tuple[list[dict], list[dict]]:
    """Single camera — and any seam no camera change sits on: a step of at
    least `HIDE_STEP_MIN` at every ≥ 0.4 s seam the picture can show one at.
    Returns (the hides, the punches — one may now open on the hold).

    Decoded in PROGRAMME order: an unkeyed piece is at 1.0, a punched piece
    ends at the scale its step release holds, a hold is the scale held for the
    whole piece. A seam a punch's release already steps at, and a seam into a
    punched piece the previous hold already steps at, need no decision of
    their own; a punched piece whose push starts later opens on a hold the push
    continues from; a seam no level can make different is left plain and NOT
    claimed. Each hold is TWO keys with the same value — the export draws a
    Keyframe only from two keys (`is_keyframed`), and the preview shows one."""
    hide_scale = float(ctx.energy["jump_cut_scale"])
    pieces = _programme(g, ctx, punches)
    entries = _choose_entries(pieces, hide_scale, ctx)
    out: list[dict] = []
    punches = list(punches)
    prev_exit = 1.0
    for i, (p, entry) in enumerate(zip(pieces, entries)):
        step = abs(prev_exit - entry)
        shown = p.opens == "hide" and step >= E.HIDE_STEP_MIN - 1e-9
        if p.punched is not None:
            if shown and entry != p.entry:
                punch = punches[p.punched]
                out.append(_hide_decision(g, ctx, p, entry, prev_exit, punch["params"]["interp"]))
                punches[p.punched] = _open_on_hold(punch, entry)
                p = replace(p, exit=float(punches[p.punched]["params"]["scale"]) if p.exit != 1.0 else 1.0)
        elif shown and not (i > 0 and pieces[i - 1].punched is not None and pieces[i - 1].exit != 1.0 and entry == 1.0):
            out.append(_hide_decision(g, ctx, p, entry, prev_exit, "step"))
        prev_exit = p.exit if p.punched is not None else entry
    return out, punches


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    if ctx.controls.get("scope") == "cleanup":          # "clean this up": no punch-ins, no scale steps
        return decisions
    seams = _seams(g, ctx)
    hides, punches = _hides(g, ctx, _punches(g, ctx, seams))
    return decisions + punches + hides


__all__ = ["run"]
