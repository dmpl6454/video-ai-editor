"""`tighten` (spec §4.6, this wave's rows): silences, lexical and acoustic
fillers, false starts and dead air become `cut_range` decisions whose edges
`seams.py` placed on envelope troughs; a protected pause becomes a
`keep_pause` decision and a shorter cut. Repeats (`repeat`) are emitted only
when lane C's closed vocabulary carries the code, else deferred with a note.

Every removal is in REFERENCE seconds while the pass works and lands in the
decision as `{src: primary key, t0, t1}` in that file's seconds. The list is
kept in `ctx.state["cuts"]` (reference clock, with codes) for the story,
camera and emphasis passes.
"""
from __future__ import annotations

from typing import Any

from .. import energy as E
from .. import reasons as R
from . import seams
from .graph_view import Graph
from .types import Ctx, decision, ref


#: A voiced stretch this long inside a word gap is sound, not a click.
UNNAMED_MIN_S = 0.12
#: A voiced run up to this long that holds a filler word is the filler.
ISLAND_MAX_S = 0.6
#: Voiced pieces under no word closer than this are one phrase of speech (a VAD splits at breaths).
UNHEARD_JOIN_S = 0.3
#: A head shorter than this is left alone.
HEAD_CUT_MIN_S = 0.1


def _repeat_allowed() -> bool:
    try:
        from ..schema import REASON_CODES
        return "repeat" in REASON_CODES
    except Exception:  # noqa: BLE001 — C's model is the authority when present
        return False


def _dead_air_at(g: Graph, a: dict, b: dict) -> dict | None:
    for d in g.flags.get("dead_air") or []:
        lo, hi = max(d["t0"], a["t1"]), min(d["t1"], b["t0"])
        if hi - lo >= 0.5 * (d["t1"] - d["t0"]):
            return d
    return None


def _silence_at(g: Graph, a: dict, b: dict) -> dict | None:
    for s in g.silences:
        lo, hi = max(s["t0"], a["t1"]), min(s["t1"], b["t0"])
        if hi > lo:
            return s
    return None


def _gap_cuts(g: Graph, ctx: Ctx, kept: list[tuple[float, float]]) -> tuple[list[dict], list[dict]]:
    """Silences and dead air between consecutive words (fillers included: a
    filler owns its own removal, the gaps around it are judged on their own)
    → (cuts, keep_pause decisions). Sound no word names inside a gap is
    speech: the gap is cut only where it is silent, on either side of it."""
    words = list(g.words)
    pad = float(ctx.energy["keep_pad_s"])
    min_sil = float(ctx.energy["min_silence_s"])
    dead_s = float(ctx.energy["dead_air_s"]) / (2.0 if ctx.reel else 1.0)
    cuts: list[dict] = []
    pauses: list[dict] = []
    for a, b in zip(words, words[1:]):
        gap = b["t0"] - a["t1"]
        dead = _dead_air_at(g, a, b)
        if dead is None and gap < min_sil:
            continue
        if dead is not None and gap < dead_s and gap < min_sil:
            continue
        sa, sb = g.sentence(str(a.get("sent"))), g.sentence(str(b.get("sent")))
        turn_boundary = _changes_speaker(g, a, b)
        why, pfacts = seams.protection(g, ctx, sa, sb)
        keep = seams.kept_air(ctx, gap, why=why, turn_boundary=turn_boundary)
        t0, t1 = seams.place_cut(g.env, g.hz, prev_end=a["t1"], next_start=b["t0"], pad=pad, kept=kept)
        tail = b["t0"] - t1
        t0 = max(t0, round(a["t1"] + keep - tail, 4))
        heard = _unnamed_sound(g, ctx, t0, t1)
        spans = [(t0, t1)]
        if heard:                                        # sound no word names lies in the gap: cut only the silence around it
            floor = min(min_sil, dead_s) - 2.0 * pad
            spans = [x for x in seams.unvoiced_parts(t0, t1, [(h[0], h[1]) for h in heard], pad) if x[1] - x[0] >= floor]
            if not spans:
                ctx.defer(f"pause at {R.tc(a['t1'], ctx.fps)}", "sound the transcript has no word for; left in place")
                continue
        if why is not None and sa is not None:
            pauses.append(decision("keep_pause", ref=ref(ctx.primary, g.to_file(a["t1"], ctx.primary), g.to_file(spans[0][0], ctx.primary)),
                                   params={"why": why, "pause": [g.to_file(a["t1"], ctx.primary), g.to_file(b["t0"], ctx.primary)],
                                           "pause_s": round(gap, 4), "kept_s": round(min(gap, (spans[0][0] - a["t1"]) + tail), 4)},
                                   reason=R.reason(f"pause_kept:{why}", pfacts, kept=R.dur(min(gap, (spans[0][0] - a["t1"]) + tail)),
                                                   tc=R.tc(a["t1"], ctx.fps)),
                                   score=0.9, confidence=0.85))
        for c0, c1 in spans:
            if c1 - c0 < seams.MIN_CUT_S:
                continue
            cuts.append(_gap_cut(g, ctx, a, b, dead, gap, turn_boundary, c0, c1, heard))
    return cuts, pauses


def _gap_cut(g: Graph, ctx: Ctx, a: dict, b: dict, dead: dict | None, gap: float, turn_boundary: bool,
             t0: float, t1: float, heard: list[tuple[float, float, list[str]]]) -> dict:
    """One removal inside the word gap `a`→`b`; it says which speech it left standing (`kept_sound`, with the flags' ids)."""
    spared = list(heard)                       # every unheard stretch inside the gap this removal was cut from
    more = {"kept_sound": [(h[0], h[1]) for h in spared]} if spared else {}
    ids = [i for h in spared for i in h[2]]
    if dead is not None and not turn_boundary:
        return {"t0": t0, "t1": t1, "code": "dead_air", "facts": [dead["id"], str(a.get("sent")), *ids],
                "fields": {"dur": R.dur(gap), "speaker": _speaker_name(g, a.get("spk"))}, "gap": (a["t1"], b["t0"]), **more}
    sil = _silence_at(g, a, b)
    return {"t0": t0, "t1": t1, "code": "silence", "facts": [*([sil["id"]] if sil else [a["id"], b["id"]]), *ids],
            "fields": {"dur": R.dur(gap)}, "gap": (a["t1"], b["t0"]), **more}


def _head_cut(g: Graph, ctx: Ctx) -> dict | None:
    """An episode opens at most `HEAD_KEEP_S` before its first sound: the
    primary file's dead air ahead of it is a removal too (EB1 UX-10: 0.87 s
    of nothing before the first word). It starts at the file's own first
    frame — which is before reference 0 when the camera rolled first. A reel
    opens where its window does."""
    if ctx.reel or not g.words:
        return None
    first = min([w["t0"] for w in g.words] + [a for a, _b, _ids in ctx.state.get("unheard") or []])
    onset = g.onset(first)
    t0 = g.to_ref(0.0, ctx.primary)
    t1 = g.to_ref(ctx.removal_start(g.to_file(onset - E.HEAD_KEEP_S, ctx.primary)), ctx.primary)     # the frame AFTER: ≤ 0.3 s is left
    if t1 - t0 < HEAD_CUT_MIN_S:
        return None
    sil = next((s for s in g.silences if s["t0"] <= t0 + 0.3 and s["t1"] >= t1 - 0.3), None)
    return {"t0": t0, "t1": t1, "code": "silence", "facts": [sil["id"]] if sil else [g.words[0]["id"]],
            "fields": {"dur": R.dur(onset - t0)}, "gap": (t0, onset), "head": True}


def _changes_speaker(g: Graph, a: dict, b: dict) -> bool:
    """Is the gap between words `a` and `b` a change of speaker? By the
    turns (cut from the sound) when they say who spoke on each side, else by
    the words' own labels."""
    before, after = g.speaker_before(a["t1"] + 0.02), g.speaker_after(b["t0"] + 0.02)
    if before is not None and after is not None:
        return before != after or g.turn_at(a["t1"] - 0.02) is not g.turn_at(b["t0"] + 0.02)
    return a.get("spk") != b.get("spk") or (g.turn_of(str(a.get("sent"))) is not g.turn_of(str(b.get("sent"))))


def _switch_edges(g: Graph, ctx: Ctx, cuts: list[dict], kept: list[tuple[float, float]]) -> list[dict]:
    """With several angles, a removal after which ANOTHER speaker is first
    heard ends where the camera changes: the anticipation lead before their
    first sound (one seam, two reasons), never later than the air floor."""
    if len(g.members) < 2:
        return cuts
    lead = E.lead_s(ctx.fps)
    words = [w for w in g.words if not w.get("filler") and (w["t0"], w["t1"]) in set(kept)]
    out = []
    for c in cuts:
        if c.get("head"):                                  # the opening has no speaker before it to change from
            out.append(c)
            continue
        nxt = min((w for w in words if w["t0"] >= c["t1"] - 0.05), key=lambda w: w["t0"], default=None)
        if nxt is not None and g.speaker_before(c["t0"] - 0.02) != g.speaker_after(nxt["t0"] + 0.02):
            real = g.onset(nxt["t0"])
            edge = g.to_ref(ctx.removal_start(g.to_file(real - lead, ctx.primary)), ctx.primary)
            c = {**c, "t1": round(min(max(c["t1"], edge), real - E.AIR_MIN_S), 4)}
        out.append(c)
    return out


def _filler_island(g: Graph, w: dict) -> tuple[float, float]:
    """The sound a lexical filler IS: the voiced run it sits in when that run
    is an island (≤ ISLAND_MAX_S — §3.4's filler length), else the token's
    own span. Whisper times the NEXT word into the tail of an "um"; the
    island is what the ear hears as the filler."""
    run = g.voiced_run_at(0.5 * (w["t0"] + w["t1"]))
    if run is not None and run[1] - run[0] <= ISLAND_MAX_S and run[0] <= w["t0"] + 0.05:
        return min(run[0], w["t0"]), max(run[1], w["t1"])
    return w["t0"], w["t1"]


def _removed_islands(g: Graph, ctx: Ctx) -> list[tuple[float, float]]:
    """The voiced runs this plan removes as a filler, an acoustic filler or a false start."""
    conf = ctx.energy["acoustic_filler_conf"]
    out = [(w["t0"], w["t1"]) for w in g.words if w.get("filler")]
    if conf is not None:
        out += [(f["t0"], f["t1"]) for f in g.acoustic_fillers if float(f.get("confidence") or 0.0) >= float(conf)]
    return out + [(f["t0"], f["t1"]) for f in g.flags.get("false_starts") or []]


def _unheard(g: Graph, ctx: Ctx) -> list[tuple[float, float, list[str]]]:
    """Speech no word names — whisper dropped the phrase: the audio layer's
    voiced runs minus the words, joined into phrases (a VAD splits a
    sentence at every breath), plus the speech layer's `unheard_voice` flags.
    `(t0, t1, ids of the flags that say so)`, reference seconds. A run the
    plan removes as a filler is not speech to keep."""
    words = sorted((w["t0"], w["t1"]) for w in g.words if not w.get("filler"))
    flags = [(float(x["t0"]), float(x["t1"]), str(x.get("id"))) for x in g.flags.get("technical") or [] if x.get("why") == "unheard_voice"]
    runs = [(a, b) for a, b in g.voiced_runs] + [(a, b) for a, b, _ in flags]
    pieces: list[list[float]] = []
    for a, b in sorted(runs):
        cur = a
        for w0, w1 in words:
            if w1 <= cur or w0 >= b:
                continue
            if w0 - cur >= UNNAMED_MIN_S:
                pieces.append([cur, w0])
            cur = max(cur, w1)
        if b - cur >= UNNAMED_MIN_S:
            pieces.append([cur, b])
    pieces.sort()
    phrases: list[list[float]] = []
    for a, b in pieces:
        if phrases and a - phrases[-1][1] <= UNHEARD_JOIN_S:
            phrases[-1][1] = max(phrases[-1][1], b)
        else:
            phrases.append([a, b])
    islands = _removed_islands(g, ctx)
    out = []
    for a, b in phrases:
        covered = sum(max(0.0, min(b, i1) - max(a, i0)) for i0, i1 in islands)
        if covered < 0.5 * (b - a):
            out.append((round(a, 4), round(b, 4), [i for f0, f1, i in flags if f1 > a and f0 < b]))
    return out


def _unnamed_sound(g: Graph, ctx: Ctx, t0: float, t1: float) -> list[tuple[float, float, list[str]]]:
    """The unheard speech that lies inside `[t0, t1]` (lane D's request — a
    gap between two WORDS is not yet a silence; whisper drops whole phrases)."""
    if "unheard" not in ctx.state:
        ctx.state["unheard"] = _unheard(g, ctx)
    out = []
    for a, b, ids in ctx.state["unheard"]:
        lo, hi = max(a, t0), min(b, t1)
        if hi - lo >= UNNAMED_MIN_S:
            out.append((lo, hi, ids))
    return out


def _spare_unheard(g: Graph, ctx: Ctx, cuts: list[dict]) -> list[dict]:
    """The last word before the grid: no removal, whatever produced it (a gap,
    a filler that reached for the next onset, two cuts merged, a speaker
    switch's edge), takes speech the transcript has no word for. What is
    left of a removal stays `pad` clear of it; the cut that spared speech
    names it in `kept_sound` and cites the flags' ids."""
    unheard = ctx.state.get("unheard") or []
    pad = float(ctx.energy["keep_pad_s"])
    out: list[dict] = []
    for c in cuts:
        hits = [(a, b, ids) for a, b, ids in unheard if b + pad > c["t0"] and a - pad < c["t1"]]
        if not hits:
            out.append(c)
            continue
        ids = [i for h in hits for i in h[2]]
        more = {"kept_sound": [*(c.get("kept_sound") or []), *[(h[0], h[1]) for h in hits]],
                "facts": list(dict.fromkeys([*c["facts"], *ids]))[:6]}
        for a, b in seams.unvoiced_parts(c["t0"], c["t1"], [(h[0], h[1]) for h in hits], pad):
            if b - a >= seams.MIN_CUT_S:
                out.append({**c, **more, "t0": a, "t1": b})
        ctx.defer(f"removal at {R.tc(c['t0'], ctx.fps)}", "speech the transcript has no word for lies inside it; left in place")
    return out


def _on_grid(g: Graph, ctx: Ctx, c: dict) -> dict | None:
    """The removal with both edges on the primary file's frame grid, rounded
    inward; None when less than a frame is left."""
    f0 = ctx.removal_start(g.to_file(c["t0"], ctx.primary))
    f1 = ctx.removal_end(g.to_file(c["t1"], ctx.primary))
    if c.get("cover"):                # a filler goes whole: reach outward a frame where the neighbours leave room
        i0, i1, lo, hi = (g.to_file(x, ctx.primary) for x in c["cover"])
        out0, out1 = ctx.removal_end(i0), ctx.removal_start(i1)
        if f0 > i0 + 1e-6 and out0 >= lo - 1e-6:
            f0 = out0
        if f1 < i1 - 1e-6 and out1 <= hi + 1e-6:
            f1 = out1
    # a grid under words that are not on it can leave an edge inside one: a filler goes whole, a kept word stays whole
    a, b = float(c["t0"]), float(c["t1"])
    meant = lambda w: bool(w.get("filler")) or a <= 0.5 * (float(w["t0"]) + float(w["t1"])) <= b       # noqa: E731
    f0 = ctx.word_safe(f0, start=True, whole=meant)
    f1 = ctx.word_safe(f1, start=False, whole=meant)
    if f1 - f0 < ctx.frame() - 1e-6:
        return None
    return {**c, "t0": g.to_ref(f0, ctx.primary), "t1": g.to_ref(f1, ctx.primary)}


def _speaker_name(g: Graph, spk: Any) -> str:
    for s in g.speakers:
        if s.get("id") == spk:
            return str(s.get("name") or ("Host" if s.get("role_guess") == "host" else "Guest" if s.get("role_guess") == "guest" else spk))
    return str(spk or "the speaker")


def _island(g: Graph, ctx: Ctx, kept: list[tuple[float, float]], i0: float, i1: float, **more: Any) -> dict | None:
    """The removal dict for a voiced island: `_island_cut`'s span plus
    `cover = (i0, i1, lo, hi)` — the sound that must go WHOLE when the
    edges are put on the frame grid, and how far each edge may reach for
    that (the previous kept word's end, the next onset minus the air)."""
    span = _island_cut(g, ctx, kept, i0, i1)
    if span is None:
        return None
    lo = max((w["t1"] for w in g.words if w["t1"] <= i0 + 1e-6 and not w.get("filler")), default=0.0)
    nxt = min((w["t0"] for w in g.words if w["t0"] >= i1 - 1e-6 and not w.get("filler")), default=g.ref_end)
    return {"t0": span[0], "t1": span[1], "cover": (i0, i1, lo, nxt - E.AIR_MIN_S), **more}


def _island_cut(g: Graph, ctx: Ctx, kept: list[tuple[float, float]], i0: float, i1: float) -> tuple[float, float] | None:
    """A removal for a voiced island [i0, i1] (a filler or a false start): its
    span plus its trailing gap up to the next word's onset trough minus the
    pad, never the preceding word's tail."""
    pad = float(ctx.energy["keep_pad_s"])
    prev = max((w["t1"] for w in g.words if w["t1"] <= i0 + 1e-6 and not w.get("filler")), default=0.0)
    nxt = min((w["t0"] for w in g.words if w["t0"] >= i1 - 1e-6 and not w.get("filler")), default=None)
    t0 = seams.trough_time(g.env, g.hz, i0, kind="out", kept=kept)
    t0 = round(min(i0, max(prev, t0)), 4)
    if nxt is None:
        t1 = round(i1 + pad, 4)
    elif nxt - i1 < float(ctx.energy["min_silence_s"]):
        t1 = min(seams.trough_time(g.env, g.hz, nxt, kind="in", kept=kept) - pad, nxt - E.AIR_MIN_S)
    else:
        t1 = seams.trough_time(g.env, g.hz, i1, kind="out", kept=kept)
    t1 = round(max(t1, i1), 4)
    return (t0, t1) if t1 - t0 >= seams.MIN_CUT_S else None


def _filler_cuts(g: Graph, ctx: Ctx, kept: list[tuple[float, float]]) -> list[dict]:
    out: list[dict] = []
    for w in g.words:
        if not w.get("filler"):
            continue
        i0, i1 = _filler_island(g, w)
        cut = _island(g, ctx, kept, i0, i1, code="filler", facts=[w["id"]],
                      fields={"word": w["text"].strip(",.;:!?"), "tc": R.tc(w["t0"], ctx.fps)})
        if cut:
            out.append(cut)
    conf = ctx.energy["acoustic_filler_conf"]
    if conf is not None:
        for af in g.acoustic_fillers:
            if float(af.get("confidence") or 0.0) < float(conf):
                ctx.defer(f"possible filler at {R.tc(af['t0'], ctx.fps)}", "below the confidence the Energy asks for; listed, not cut")
                continue
            cut = _island(g, ctx, kept, af["t0"], af["t1"], code="filler_acoustic", facts=[af["id"]],
                          fields={"tc": R.tc(af["t0"], ctx.fps), "confidence": f"{float(af['confidence']):.2f}"})
            if cut:
                out.append(cut)
    elif g.acoustic_fillers:
        ctx.defer("acoustic fillers", "listed only below energy 5")
    return out


def _false_start_cuts(g: Graph, ctx: Ctx, kept: list[tuple[float, float]]) -> list[dict]:
    out: list[dict] = []
    for f in g.flags.get("false_starts") or []:
        kept_s = g.sentence(str(f.get("kept") or "")) or {}
        cut = _island(g, ctx, kept, f["t0"], f["t1"], code="false_start", facts=[f["id"]] + ([kept_s["id"]] if kept_s else []),
                      fields={"text": f.get("text", ""), "kept": (kept_s.get("text") or "")[:40], "tc": R.tc(f["t0"], ctx.fps)})
        if cut:
            out.append(cut)
    return out


def _repeat_cuts(g: Graph, ctx: Ctx, kept: list[tuple[float, float]]) -> list[dict]:
    reps = g.flags.get("repeats") or []
    if not reps:
        return []
    if not _repeat_allowed():
        for r in reps:
            dup = g.sentence(r["dup"])
            if dup:
                ctx.defer(f"repeated sentence at {R.tc(dup['t0'], ctx.fps)}", "the `repeat` reason lands next wave; left in place")
        return []
    out: list[dict] = []
    pad = float(ctx.energy["keep_pad_s"])
    for r in reps:
        dup, of = g.sentence(r["dup"]), g.sentence(r["of"])
        if not dup or not of:
            continue
        prev = max((w["t1"] for w in g.words if w["t1"] <= dup["t0"] + 1e-6 and w.get("sent") != dup["id"]), default=0.0)
        t0 = round(max(prev + pad, seams.trough_time(g.env, g.hz, prev, kind="out", kept=kept) + pad), 4) if prev else dup["t0"]
        t1 = round(seams.trough_time(g.env, g.hz, dup["t1"], kind="out", kept=kept), 4)
        if t1 - t0 >= seams.MIN_CUT_S:
            out.append({"t0": min(t0, dup["t0"]), "t1": max(t1, dup["t1"]), "code": "repeat", "facts": [r["id"], dup["id"], of["id"]],
                        "fields": {"tc_of": R.tc(of["t0"], ctx.fps)}})
    return out


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    islands = [_filler_island(g, w) for w in g.words if w.get("filler")]
    ctx.state["unheard"] = _unheard(g, ctx)
    kept = [(a, b) for a, b in seams.kept_voiced(g)                    # a word timed INTO a filler's island is not kept sound
            if not any(i0 - 1e-6 <= a and b <= i1 + 1e-6 for i0, i1 in islands)]
    kept += [(a, b) for a, b, _ in ctx.state["unheard"]]               # speech the transcript missed is kept sound too
    gap_cuts, pauses = _gap_cuts(g, ctx, kept)
    raw = gap_cuts + _filler_cuts(g, ctx, kept) + _false_start_cuts(g, ctx, kept) + _repeat_cuts(g, ctx, kept)
    head = _head_cut(g, ctx)
    raw += [head] if head else []
    merged = _spare_unheard(g, ctx, _switch_edges(g, ctx, seams.merge_ranges(raw, kept), kept))
    cuts = [c for c in (_on_grid(g, ctx, x) for x in merged) if c is not None]
    ctx.state["cuts"] = cuts
    return list(decisions) + pauses + cut_decisions(g, ctx)


def cut_decisions(g: Graph, ctx: Ctx) -> list[dict]:
    """`ctx.state["cuts"]` as `cut_range` decisions (the story pass may have
    pruned them to the kept windows and fitted their air since `run`)."""
    out = []
    for c in ctx.state.get("cuts") or []:
        fields = dict(c.get("fields") or {})
        if c["code"] in ("silence", "dead_air"):
            fields["tc"] = R.tc(c["t0"], ctx.fps)
        params = {"air_given_s": c["given"]} if c.get("given") else {}
        if c.get("kept_sound"):                        # speech the transcript has no word for, left standing inside this removal
            params["kept_sound"] = [[g.to_file(a, ctx.primary), g.to_file(b, ctx.primary)] for a, b in c["kept_sound"]]
            fields["spared"] = str(len(c["kept_sound"]))   # the reason's text says so (brain/reasons.py words it)
        out.append(decision("cut_range", ref=ref(ctx.primary, g.to_file(c["t0"], ctx.primary), g.to_file(c["t1"], ctx.primary)),
                            params=params, reason=R.reason(c["code"], c["facts"], **fields), score=1.0,
                            confidence=0.95 if c["code"] != "filler_acoustic" else 0.8))
    return out


__all__ = ["run", "cut_decisions"]
