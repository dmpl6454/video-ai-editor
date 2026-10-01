"""The Camera Director, this wave's rules (spec §4.3 rules 1, 1b, 2, 8) for
an angle group with two closes and no wide; one camera gets scale steps.

The planner is OFFLINE with the whole turn table, so every switch is placed
with lookahead on the reference clock:

  1.  follow the speaker, anticipating — the switch lands at the new turn's
      first-word onset minus `LEAD_FRAMES` (≤ 0.15 s), on the frame grid;
      a backchannel (< 0.6 s or all lexicon words) never switches; a true
      overlap switches at the onset;
  1b. hide the jump cuts — a tighten seam that removed ≥ 0.4 s gets an angle
      change AT the seam when a speaker switch belongs there: a switch whose
      lead falls inside the removed span is moved onto the seam (`at_cut`,
      one seam, two reasons). Every other such seam — on one camera or
      several — becomes a scale step (emphasis.py's `jump_cut_hide`);
  2.  min-shot: no shot under 1.2 s — and the Energy's minimum shot yields to
      a WHOLE turn: a turn of 0.6 s or more that is not a backchannel and not
      a true overlap (≥ half of it spoken over another voice) is shown on its
      speaker's close whatever its length. Two turns closer than 1.2 s: the
      longer one keeps the camera;
  8.  switches per minute against the style's cap: REPORTED, never enforced
      by leaving a speaker off camera — with the share of the talking time
      the viewer sees on the speaker's own close (`ctx.state["camera_stats"]`).

Every `switch_angle` decision is a span on the PRIMARY angle's file clock
whose picture comes from another angle; the resolver hands it to
`apply_camera_plan`. Rules 3-7 (wide resets, reactions, overlap → wide,
face-lost, composition) are EB2 and the summary says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import energy as E
from .. import reasons as R
from . import tighten
from .graph_view import Graph
from .types import Ctx, decision, r4, ref


@dataclass
class _Switch:
    t: float                   # reference seconds
    angle: str                 # source key of the angle the picture cuts TO
    code: str                  # speaker_turn | at_cut
    facts: list[str]
    turn: dict | None = None
    turn_len: float = 0.0
    fields: dict = field(default_factory=dict)


def _lead(ctx: Ctx) -> float:
    return E.lead_s(ctx.fps)


def _cut_containing(cuts: list[dict], t: float) -> dict | None:
    return next((c for c in cuts if c["t0"] - 1e-6 <= t <= c["t1"] + 1e-6), None)


def _seam_before(cuts: list[dict], onset: float) -> dict | None:
    """The tighten seam that ends within `SNAP_S` before `onset` — the word
    gap a switch snaps into (rule 1) is the kept air after that cut."""
    hits = [c for c in cuts if 0.0 <= onset - c["t1"] <= E.SNAP_S]
    return max(hits, key=lambda c: c["t1"]) if hits else None


def _in_word_of_other(g: Graph, t: float, spk: str | None) -> bool:
    return any(w["t0"] < t < w["t1"] and w.get("spk") != spk for w in g.words)


def _true_overlap(g: Graph, turn: dict) -> bool:
    """Is at least `OVERLAP_TURN_SHARE` of this turn spoken over another voice's turn?"""
    spk = g.turn_spk(turn)
    over = sum(max(0.0, min(o["t1"], turn["t1"]) - max(o["t0"], turn["t0"])) for o in g.turns
               if o.get("id") != turn.get("id") and g.turn_spk(o) != spk)
    return over >= E.OVERLAP_TURN_SHARE * (turn["t1"] - turn["t0"])


def _takes_camera(g: Graph, turn: dict, min_shot: float | None = None) -> bool:
    """Rule 1's backchannel test, and rule 2 as it applies to a WHOLE turn:
    a turn of `FULL_TURN_MIN_S` or more that is not a backchannel and not a
    true overlap is shown on its speaker's close whatever its length — the
    minimum shot yields to a full turn (EB1 fix UX-10: five whole lines, the
    topic change and the closing line among them, played on the wrong face
    because 2.5 s outranked them). `min_shot` is kept for callers; the shot
    the switch ends is still guarded by `MIN_SHOT_AT_CUT_S` in the caller."""
    length = turn["t1"] - turn["t0"]
    if length < E.FULL_TURN_MIN_S or g.is_backchannel(turn):
        return False
    return not _true_overlap(g, turn)


def _lead_time(g: Graph, ctx: Ctx, onset: float, spk: object) -> float:
    """The switch for a speaker whose sound starts at `onset`: the
    anticipation lead before it, on the primary file's frame grid and never
    MORE than the lead (the grid rounds toward the onset); at the onset's
    own frame when the other speaker is still mid-word there (an overlap)."""
    t = g.to_ref(ctx.removal_start(g.to_file(onset - _lead(ctx), ctx.primary)), ctx.primary)
    if _in_word_of_other(g, t, spk):
        t = g.to_ref(ctx.removal_end(g.to_file(onset, ctx.primary)), ctx.primary)
    return r4(t)


def _turn_switch(g: Graph, ctx: Ctx, cuts: list[dict], turn: dict, angle: str) -> _Switch:
    """The switch that shows `turn` on `angle`: the anticipation lead before
    its onset, or the tighten seam it belongs on (one seam, two reasons)."""
    spk = g.turn_spk(turn)
    onset = g.speaker_onset(turn) if spk == turn.get("spk") else g.onset(float(turn["t0"]))
    t = _lead_time(g, ctx, onset, spk)
    code, facts, fields = "speaker_turn", [str(turn.get("id")), str(spk)], {"speaker": _name(g, spk)}
    cut = _cut_containing(cuts, t) or _cut_containing(cuts, onset) or _seam_before(cuts, onset)
    # a seam takes the switch only if it ends no earlier than the anticipation lead before the onset: a seam 0.25 s ahead
    # of it (SNAP_S) would lead by more than LEAD_MAX_S, and the rule is 0..0.15 s (slice: a switch led by 0.1558 s)
    if cut is not None and onset - cut["t1"] <= min(E.SNAP_S, _lead(ctx) + 1e-6):
        t, code = r4(cut["t1"]), ("at_cut" if cut["t1"] - cut["t0"] >= E.HIDE_CUT_MIN_S - E.HIDE_TOL_S else code)
        if code == "at_cut":
            facts = facts + list(cut["facts"])
            fields = {"tc": R.tc(cut["t1"], ctx.fps)}
    return _Switch(t=t, angle=angle, code=code, facts=facts, turn=turn, turn_len=turn["t1"] - turn["t0"], fields=fields)


def _turn_switches(g: Graph, ctx: Ctx, cuts: list[dict]) -> list[_Switch]:
    """Rule 1 (+ the at_cut snap of 1b, + rule 2's short-turn hold). No shot
    is under `MIN_SHOT_AT_CUT_S`: when two turns are that close the LONGER
    keeps the camera (the shorter's switch is dropped, or never made)."""
    min_shot = E.MIN_SHOT_REEL_S if ctx.reel else float(ctx.energy["camera_min_shot_s"])
    out: list[_Switch] = []
    for turn in g.turns:
        if not _takes_camera(g, turn, min_shot):
            continue
        angle = g.key_of_angle(g.speaker_angle(g.turn_spk(turn)) or "")
        current = out[-1].angle if out else ctx.primary
        if angle is None or angle == current:
            continue
        sw = _turn_switch(g, ctx, cuts, turn, angle)
        if out and sw.t - out[-1].t < E.MIN_SHOT_AT_CUT_S:          # the shot this would end is under 1.2 s
            if sw.turn_len <= out[-1].turn_len:
                continue
            out.pop()
            before = out[-1].angle if out else ctx.primary
            if before == angle or (out and sw.t - out[-1].t < E.MIN_SHOT_AT_CUT_S):
                continue
        out.append(sw)
    return out


def _uncovered(ctx: Ctx, cuts: list[dict], switches: list[_Switch]) -> list[tuple[float, list[str]]]:
    """Rule 1b's remainder: the seams of ≥ 0.4 s no camera change sits on.
    Between two closes with no wide, a cover by the listener's close would
    have to cut BACK mid-turn, away from every onset; this wave hides such a
    seam by a scale step, exactly as on one camera (reaction cutaways are
    rule 4, next wave)."""
    half = ctx.frame() / 2.0
    out = []
    for c in sorted(cuts, key=lambda c: c["t0"]):
        seam = r4(c["t1"])
        if c.get("head"):                                    # nothing plays before the opening: no seam
            continue
        if c["t1"] - c["t0"] >= E.HIDE_CUT_MIN_S - E.HIDE_TOL_S and not any(abs(s.t - seam) <= half for s in switches):
            out.append((seam, list(c["facts"])))
    return out


def _kept_parts(t0: float, t1: float, cuts: list[dict]) -> list[tuple[float, float]]:
    """`[t0, t1]` minus what tighten removes: the part of a turn a viewer sees."""
    out, cur = [], t0
    for c in sorted(cuts, key=lambda c: c["t0"]):
        if c["t1"] <= cur or c["t0"] >= t1:
            continue
        if c["t0"] > cur:
            out.append((cur, c["t0"]))
        cur = max(cur, c["t1"])
    if cur < t1:
        out.append((cur, t1))
    return out


def _talk_share(g: Graph, ctx: Ctx, switches: list[_Switch], cuts: list[dict]) -> tuple[float, float]:
    """(share, seconds off) of the talking time the viewer sees on the
    SPEAKER'S OWN close: every turn counts, a backchannel and a true overlap
    (which never switch) included; what tighten removes does not."""
    line = [(0.0, ctx.primary)] + [(s.t, s.angle) for s in sorted(switches, key=lambda s: s.t)]
    on = total = 0.0
    for turn in g.turns:
        want = g.key_of_angle(g.speaker_angle(g.turn_spk(turn)) or "")
        if want is None:
            continue
        for a, b in _kept_parts(float(turn["t0"]), float(turn["t1"]), cuts):
            total += b - a
            for i, (t, angle) in enumerate(line):
                end = line[i + 1][0] if i + 1 < len(line) else float("inf")
                if angle == want:
                    on += max(0.0, min(b, end) - max(a, t))
    return (round(on / total, 4) if total > 0 else 1.0), round(total - on, 3)


def _close_slivers(g: Graph, ctx: Ctx, cuts: list[dict], switches: list[_Switch]) -> bool:
    """No piece of the programme is under `MIN_PIECE_S` because of a camera change: a change that lands a hair
    AFTER a removal's end (or a hair BEFORE its start) leaves a one-frame piece of the other camera, with a hidden
    jump cut in front of it and no hide (closer review: a 0.05 s piece of camera B at 126.55 s). The removal is
    extended to the change, on the frame grid, so the piece is not made. True when a removal moved."""
    moved = False
    keep = lambda _w: False                                   # noqa: E731 — no word is taken to close a sliver
    for c in cuts:
        for sw in switches:
            if 0.0 < sw.t - c["t1"] < E.MIN_PIECE_S:
                edge = g.to_ref(ctx.word_safe(ctx.removal_start(g.to_file(sw.t, ctx.primary)), start=False, whole=keep), ctx.primary)
                if edge > c["t1"] + 1e-6:                     # a change that falls inside a word is not worth a split word
                    c["t1"], moved = edge, True
            elif 0.0 < c["t0"] - sw.t < E.MIN_PIECE_S and not c.get("head"):
                edge = g.to_ref(ctx.word_safe(ctx.removal_end(g.to_file(sw.t, ctx.primary)), start=True, whole=keep), ctx.primary)
                if edge < c["t0"] - 1e-6:
                    c["t0"], moved = edge, True
    return moved


def _rate(g: Graph, ctx: Ctx, switches: list[_Switch], cuts: list[dict] | None = None) -> None:
    """Rule 8, reported: the changes follow who is speaking, so a fast
    conversation exceeds the style's cap rather than leaving its speakers
    off camera (measured on fixture P2: 40 turns in 2.8 min is 14 a minute;
    no subset under the premium cap of 6 keeps 90 % of the talk on the
    speaker's close). The numbers go to `ctx.state["camera_stats"]` — the
    switch count, the rate, the share of talking time on the speaker's own
    close — and, above the cap, into what the card says it did not enforce."""
    cap = E.rate_cap(ctx.style)
    rate = round(len(switches) / max(1e-6, g.ref_end / 60.0), 2)
    share, off = _talk_share(g, ctx, switches, cuts or [])
    ctx.state["switch_rate"] = rate
    ctx.state["camera_stats"] = {"switches": len(switches), "rate": rate, "cap": cap, "talk_share": share, "off_close_s": off}
    if rate > cap:
        ctx.defer("switch rate", f"{rate:.1f} camera changes a minute follow the speakers, more than the {cap} the "
                                 f"{E.style_name(ctx.style)} style aims for; no speaker is left off camera to lower it "
                                 f"({share:.0%} of the talk is on the speaker's own close)")


def _spans(switches: list[_Switch], end: float, primary: str) -> list[tuple[float, float, str, _Switch]]:
    """Consecutive spans (t0, t1, angle, opener) from the ordered switch list; same-angle neighbours merge."""
    spans: list[tuple[float, float, str, _Switch]] = []
    ordered = sorted(switches, key=lambda s: s.t)
    for i, s in enumerate(ordered):
        t1 = ordered[i + 1].t if i + 1 < len(ordered) else end
        if spans and spans[-1][2] == s.angle:
            a0, _, a, opener = spans[-1]
            spans[-1] = (a0, t1, a, opener)
        else:
            spans.append((s.t, t1, s.angle, s))
    return spans


def _name(g: Graph, spk: object) -> str:
    for s in g.speakers:
        if s.get("id") == spk:
            role = s.get("role_guess")
            return str(s.get("name") or ("Host" if role == "host" else "Guest" if role == "guest" else spk))
    return str(spk or "the speaker")


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    cuts = ctx.state.get("cuts") or []
    if len(g.members) < 2:
        ctx.state["hide_seams"] = _uncovered(ctx, cuts, [])
        ctx.state["switch_times"] = []
        ctx.state["angle_line"] = [(0.0, ctx.primary)]
        ctx.state["camera"] = {"angles": max(1, len(g.members)), "switches": 0, "at_cut": 0}
        return decisions
    switches = _turn_switches(g, ctx, cuts)
    if _close_slivers(g, ctx, cuts, switches):
        decisions = [d for d in decisions if d["kind"] != "cut_range"] + tighten.cut_decisions(g, ctx)
    if not any(g.key_of_angle(g.speaker_angle(sp.get("id")) or "") for sp in g.speakers):
        # two cameras and nothing that says who is on which (one voice found, or no camera hears its own speaker):
        # no switch can be placed, so the programme stays on the first camera and the card says so
        ctx.defer("camera changes", f"I could not tell which camera shows which speaker, so the whole edit stays on "
                                    f"camera {g.angle_of_key(ctx.primary) or 'A'}")
    _rate(g, ctx, switches, cuts)
    spans = _spans(switches, g.ref_end, ctx.primary)
    ctx.state["hide_seams"] = _uncovered(ctx, cuts, switches)
    ctx.state["switch_times"] = sorted({s.t for s in switches})
    ctx.state["angle_line"] = [(0.0, ctx.primary)] + [(s.t, s.angle) for s in sorted(switches, key=lambda s: s.t)]
    at_cut = 0
    n = 0
    for t0, t1, angle, opener in spans:
        if angle == ctx.primary or t1 - t0 <= 0:
            continue
        n += 1
        at_cut += opener.code == "at_cut"
        decisions.append(decision(
            "switch_angle", ref=ref(ctx.primary, g.to_file(t0, ctx.primary), g.to_file(t1, ctx.primary)),
            params={"angle": angle, "angle_src": angle, "letter": g.angle_of_key(angle) or "",
                    "speaker": str(g.turn_spk(opener.turn) if opener.turn else ""), "ref": [r4(t0), r4(t1)]},
            reason=R.reason(opener.code, opener.facts, **opener.fields), score=0.9,
            confidence=0.85 if opener.code == "speaker_turn" else 0.8))
    ctx.state["camera"] = {"angles": len(g.members), "switches": n, "at_cut": at_cut}
    ctx.defer("wide resets and reactions", "not used yet; the picture only follows who is speaking")
    return decisions


__all__ = ["run"]
