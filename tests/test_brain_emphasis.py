"""Smart punch-ins (spec §4.4): in at the clause start ≥ 0.8 s before the
peak, hold through the sentence, release as a STEP at the next seam (no
out-keys) or, with no seam within 8 s, a timed ease ending on a sentence
boundary; one `interp` per keyed property."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import Line, simple_graph, th_graph, th_truth  # noqa: E402

from video_ai_editor.brain import energy as E  # noqa: E402
from video_ai_editor.brain.planner import plan  # noqa: E402
from video_ai_editor.edl.schema import Interp  # noqa: E402

REEL = {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"}
INTERPS = set(Interp.__args__)  # type: ignore[attr-defined]


def _punches(edp: dict) -> list[dict]:
    return [d for d in edp["decisions"] if d["kind"] == "punch_in"]


def test_in_point_at_clause_start_geq_0_8s_before_peak():
    graph = th_graph()
    truth = th_truth(graph)
    words = {w["id"]: w for w in graph["layers"]["speech"]["words"]}
    edp = plan(graph, REEL)
    peaks = [d for d in _punches(edp) if d["reason"]["code"] == "emphasis_peak"]
    assert len(peaks) == 1, [(d["reason"]["code"], d["reason"]["facts"]) for d in _punches(edp)]
    d = peaks[0]
    assert truth["emphasis"] in d["reason"]["facts"]
    peak = words[d["params"]["peak_word"]]
    assert peak["sent"] == truth["emphasis"]
    clause = words[truth["clause_start_word"]]
    assert abs(d["ref"]["t0"] - clause["t0"]) < 1e-6, (d["ref"], clause)
    assert peak["t0"] - d["ref"]["t0"] >= E.LEAD_S - 1e-6
    sent = next(s for s in graph["layers"]["speech"]["sentences"] if s["id"] == truth["emphasis"])
    assert abs(d["ref"]["t1"] - sent["t1"]) < 1e-6, "held through the sentence"
    assert d["params"]["scale"] == E.knob("punch_scale", 5)
    assert d["reason"]["code"] == "emphasis_peak" and d["reason"]["text"], d["reason"]   # the words are brain/reasons.py's
    # the retake gets nothing, the hook gets its own punch
    assert not any(truth["retake_dup"] in p["reason"]["facts"] for p in _punches(edp))
    hooks = [p for p in _punches(edp) if p["reason"]["code"] == "hook_emphasis"]
    assert len(hooks) == 1 and truth["quotable"] in hooks[0]["reason"]["facts"]
    assert hooks[0]["params"]["scale"] == E.PUNCH_HOOK_SCALE


def test_step_release_at_next_seam_has_no_out_keys():
    graph = th_graph()
    edp = plan(graph, REEL)
    for d in _punches(edp):
        p = d["params"]
        assert p["release"].startswith("step@"), p["release"]
        assert len(p["keys"]) == 2, p["keys"]
        assert p["interp"] == "ease-out"
        assert p["keys"][0]["values"]["scale"] == 1.0 and p["keys"][1]["values"]["scale"] == p["scale"]
        assert abs((p["keys"][1]["t"] - p["keys"][0]["t"]) - E.knob("punch_push_s", 5)) < 1e-6
        assert p["props"] == ["scale", "x", "y"]
        seam = float(p["release"].split("@", 1)[1])
        assert d["ref"]["t1"] <= seam <= d["ref"]["t1"] + E.RELEASE_WINDOW_S + 1e-6


def test_timed_release_when_no_seam_within_8s():
    # an emphasised sentence followed by ten seconds of continuous speech: no pause, no filler, no seam
    g = simple_graph([
        Line("Now listen, because this is the part that matters.", claim=True, emphasis=True, stress=7, topic_peak=True, pause_after=0.3),
        Line("The first customer paid before the product existed and kept paying.", pause_after=0.3),
        Line("The second one told the third and the third told everyone else.", pause_after=0.3),
        Line("By the end of that year we had never spent a cent on marketing.", pause_after=0.3),
        Line("And that is still true today.", pause_after=0.3),
    ])
    edp = plan(g, {"content_type": "podcast"})
    peaks = [d for d in _punches(edp) if d["reason"]["code"] == "emphasis_peak"]
    assert len(peaks) == 1, [d["reason"] for d in edp["decisions"] if d["kind"] == "punch_in"]
    p = peaks[0]["params"]
    assert p["release"].startswith("ease@"), p
    assert len(p["keys"]) == 4 and p["interp"] == "ease-in-out"
    t_rel, t_end = p["keys"][2]["t"], p["keys"][3]["t"]
    assert t_end - t_rel >= 1.2 - 1e-6
    ends = {round(s["t1"], 3) for s in g["layers"]["speech"]["sentences"]}
    assert round(t_end, 3) in ends, (t_end, sorted(ends))
    assert p["keys"][3]["values"]["scale"] == 1.0 and p["keys"][2]["values"]["scale"] == p["scale"]


def test_one_interp_per_key():
    for graph, controls in ((th_graph(), REEL), (simple_graph([
            Line("A claim with, um, a pause.", claim=True, emphasis=True, stress=0, fillers=(3,), pause_after=1.5),
            Line("Then a second thought.", pause_after=1.5), Line("Then a third one.")]), {"content_type": "talking_head"})):
        edp = plan(graph, controls)
        keyed = [d for d in edp["decisions"] if d["kind"] in ("punch_in", "jump_cut_hide")]
        by_piece: dict[tuple, set[str]] = {}
        for d in keyed:
            assert d["params"]["interp"] in INTERPS
            by_piece.setdefault(tuple(d["params"]["piece"]), set()).add(d["params"]["interp"])
        assert all(len(v) == 1 for v in by_piece.values()), by_piece


# --------------------------------------------------------------------------
# EB1 integration (measured on the real fixtures)
# --------------------------------------------------------------------------

def test_a_line_both_raised_and_coloured_is_emphasis_by_delivery_alone():
    """On fixture TH §3.4's importance tops out at 0.30, so §4.4's 0.5 floor
    alone never punched in on real footage: a line whose level AND emotion
    both clear their thresholds is emphasis by the speaker's own choice."""
    from video_ai_editor.brain.planner import emphasis, make_ctx
    from video_ai_editor.brain.planner.graph_view import Graph
    graph = th_graph()
    truth = th_truth(graph)
    sem = graph["layers"]["semantic"]["scores"]
    sem[truth["emphasis"]]["importance"] = 0.13                       # what lane D's scorer measured on the real clip
    for feat in (s for s in graph["layers"]["speech"]["sentences"] if s["id"] == truth["emphasis"]):
        feat["features"]["conclusion_marker"] = False
    g = Graph(graph)
    ctx = make_ctx(g, REEL)
    s = g.sentence(truth["emphasis"])
    loud, moved = g.rms_z(s) >= emphasis.EMPHASIS_Z, g.scores(s["id"])["emotion"] >= emphasis.EMOTION_MIN
    picked = {x["id"] for x, code in emphasis._candidates(g, ctx) if code == "emphasis_peak"}
    assert (truth["emphasis"] in picked) == (loud and moved), (loud, moved, picked)
    # one signal alone is not enough without weight
    sem[truth["emphasis"]]["emotion"] = 0.2
    g2 = Graph(graph)
    assert (truth["emphasis"] in {x["id"] for x, c in emphasis._candidates(g2, make_ctx(g2, REEL)) if c == "emphasis_peak"}) is False


def test_the_peak_is_a_word_that_carries_meaning():
    from video_ai_editor.brain.planner import emphasis
    from video_ai_editor.brain.planner.graph_view import Graph, content_tokens
    from gen_brain_goldens import encode_env
    graph = simple_graph([Line("When the card is full, the important decisions are already finished.", claim=True, pause_after=0.5)])
    words = graph["layers"]["speech"]["words"]
    env = [-40] * int(100 * (words[-1]["t1"] + 1))
    the = next(w for w in words if w["text"].lower() == "the" and w["t0"] > words[4]["t0"])
    for f in range(int(the["t0"] * 100), int(the["t1"] * 100)):
        env[f] = -18                                                  # "the", loud and short, before the stressed word
    already = next(w for w in words if w["text"].lower() == "already")
    for f in range(int(already["t0"] * 100), int(already["t1"] * 100)):
        env[f] = -22
    graph["layers"]["audio"]["env_10ms"] = encode_env(env)
    g = Graph(graph)
    peak = emphasis._peak_word(g, g.sentences[0])
    assert peak["id"] == already["id"] and content_tokens(peak["text"])
    clause = emphasis._clause_start(g, g.sentences[0], peak)
    assert clause["id"] == the["id"], "in at the clause after the comma, ≥ 0.8 s before the peak"
    assert peak["t0"] - clause["t0"] >= E.LEAD_S


def test_the_cadence_is_counted_where_the_viewer_sees_it():
    """On a reel the hook plays FIRST wherever it was said: a punch-in 5 s
    before it in the source is tens of seconds after it in the programme."""
    from video_ai_editor.brain.planner import select
    graph = th_graph()
    truth = th_truth(graph)
    edp = plan(graph, REEL)
    codes = sorted(d["reason"]["code"] for d in _punches(edp))
    assert codes == ["emphasis_peak", "hook_emphasis"], codes
    from video_ai_editor.brain.planner import PASSES, make_ctx, story
    ctx = make_ctx(graph, REEL)
    decisions: list[dict] = []
    for p in PASSES:
        decisions = p.run(ctx.graph, ctx, decisions)
        if p is story:
            break
    q0 = truth["sentences"][truth["quotable"]][0]
    assert select.programme_time(ctx, q0) is not None and select.programme_time(ctx, q0) < 1.0
    pieces = select.pieces(ctx)
    assert abs(sum(b - a for a, b in pieces) - select.programme_length(ctx)) < 1e-6
    assert pieces[0][0] <= q0 <= pieces[0][1], "the hook's span is the programme's first piece"
    removed = next(c for c in ctx.state["cuts"])
    assert select.programme_time(ctx, 0.5 * (removed["t0"] + removed["t1"])) is None


# --------------------------------------------------------------------------
# FX-C1: what the viewer SEES at a hidden seam (EX-01, EX-04, UX-11)
# --------------------------------------------------------------------------

from video_ai_editor.edl.keyframes import is_keyframed, sample as _kf_sample  # noqa: E402
from video_ai_editor.edl.schema import Keyframe  # noqa: E402
from gen_brain_goldens import p2_graph  # noqa: E402

HIDE_KINDS = ("punch_in", "jump_cut_hide")


def _spans_minus(windows: list[tuple[float, float]], cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in windows:
        cur = a
        for c0, c1 in sorted(cuts):
            if c1 <= cur or c0 >= b:
                continue
            if c0 > cur:
                out.append((cur, min(c0, b)))
            cur = max(cur, c1)
        if cur < b:
            out.append((cur, b))
    return out


def _programme(edp: dict) -> list[tuple[float, float]]:
    """The kept pieces in programme order, from the DECISIONS alone (the
    resolver's own arithmetic, restated): windows minus removals, the
    opening moment first when the reel opens on it. Source seconds of the
    primary file."""
    dec = edp["decisions"]
    wins = sorted((d["ref"]["t0"], d["ref"]["t1"]) for d in dec if d["kind"] == "keep_window")
    cuts = [(d["ref"]["t0"], d["ref"]["t1"]) for d in dec if d["kind"] == "cut_range"]
    end = max((d["ref"]["t1"] for d in dec if d["ref"]), default=0.0)
    pieces = _spans_minus(wins or [(0.0, end)], cuts)
    changes = sorted({d["ref"][k] for d in dec if d["kind"] == "switch_angle" for k in ("t0", "t1")})    # a new angle is a new piece
    pieces = [(a, b) for lo, hi in pieces for a, b in zip([lo, *[c for c in changes if lo + 1e-6 < c < hi - 1e-6]],
                                                            [*[c for c in changes if lo + 1e-6 < c < hi - 1e-6], hi])]
    hook = next((d for d in dec if d["kind"] == "open_on"), None)
    if hook is None:
        return pieces
    h0, h1 = hook["ref"]["t0"], hook["ref"]["t1"]
    front = [(max(a, h0), min(b, h1)) for a, b in pieces if min(b, h1) - max(a, h0) > 1e-6]
    rest = [x for a, b in pieces for x in ((a, min(b, h0)), (max(a, h1), b)) if x[1] - x[0] > 1e-6]
    return front + rest


def _decoded(edp: dict, piece: tuple[float, float], local: float, *, export: bool) -> float:
    """The scale a piece shows `local` seconds in: every keyed decision that opens it, together, as ONE Keyframe —
    with the export's rule (a Keyframe needs >= 2 keys, else the scale is 1.0) or the browser preview's
    (lib/overlay.ts sampleKF: any key counts)."""
    ds = [d for d in edp["decisions"] if d["kind"] in HIDE_KINDS and piece[0] - 1e-6 <= d["params"]["piece"][0] < piece[1] - 1e-6]
    if not ds:
        return 1.0
    assert len({d["params"]["interp"] for d in ds}) == 1, [d["params"]["interp"] for d in ds]     # ONE mode per property
    pts = sorted({(round(float(k["t"]) - piece[0], 4), float(k["values"]["scale"])) for d in ds for k in d["params"]["keys"]})
    kf = Keyframe(keyframes=pts, interp=ds[0]["params"]["interp"])
    if export and not is_keyframed(kf):
        return 1.0
    return float(_kf_sample(kf, local))


def _scale_at(edp: dict, piece: tuple[float, float], local: float, *, export: bool) -> float:
    return _decoded(edp, piece, local, export=export)


def _seam_sides(edp: dict, *, export: bool) -> list[tuple[float, float, float, float]]:
    """(seam start in the programme's source seconds, removed s before it, scale just before, scale just after)."""
    pieces = _programme(edp)
    out = []
    for prev, nxt in zip(pieces, pieces[1:]):
        before = _scale_at(edp, prev, prev[1] - prev[0], export=export)
        after = _scale_at(edp, nxt, 0.0, export=export)
        out.append((nxt[0], nxt[0] - prev[1], before, after))
    return out


def test_every_hide_is_a_key_the_export_draws():
    """EX-01: a one-key Keyframe is ignored by the export (`is_keyframed` needs 2) while the preview
    shows it — the hide needs two keys, holding the same value, so both renderers agree."""
    for graph, controls in ((th_graph(), REEL), (p2_graph(), {"content_type": "podcast"})):
        hides = [d for d in plan(graph, controls)["decisions"] if d["kind"] == "jump_cut_hide"]
        assert hides
        for d in hides:
            keys = d["params"]["keys"]
            assert len(keys) == 2 and keys[0]["values"]["scale"] == keys[1]["values"]["scale"] == d["params"]["scale"], keys
            assert keys[1]["t"] > keys[0]["t"]
            assert d["params"]["interp"] == "step" and {k["interp"] for k in keys} == {"step"}


def test_export_and_preview_show_the_same_scale_at_every_seam():
    for graph, controls in ((th_graph(), REEL), (p2_graph(), {"content_type": "podcast"})):
        edp = plan(graph, controls)
        assert _seam_sides(edp, export=True) == _seam_sides(edp, export=False)


def test_a_scale_step_is_claimed_only_where_the_two_sides_differ_by_six_percent():
    """EX-04 + UX-11: as DECODED (the export's rule), the piece before a claimed hide and the piece it opens
    differ by at least 6 %, and the decision says both numbers."""
    for graph, controls in ((th_graph(), REEL), (p2_graph(), {"content_type": "podcast"})):
        edp = plan(graph, controls)
        hides = [d for d in edp["decisions"] if d["kind"] == "jump_cut_hide"]
        assert hides
        sides = [(s, b, a) for s, _gap, b, a in _seam_sides(edp, export=True)]
        for d in hides:
            _s, before, after = min(sides, key=lambda x: abs(x[0] - d["params"]["piece"][0]))     # (a hide sits on its picture's grid)
            assert abs(after - before) >= E.HIDE_STEP_MIN - 1e-6, (d["params"]["piece"], before, after)
            assert abs(after - d["params"]["scale"]) < 1e-6 and abs(before - d["params"]["from_scale"]) < 1e-6
            assert abs(d["params"]["step"] - abs(after - before)) < 1e-4


def test_the_hold_never_outlasts_its_piece_and_never_claims_a_frame_it_lacks():
    edp = plan(th_graph(), REEL)
    for d in (x for x in edp["decisions"] if x["kind"] == "jump_cut_hide"):
        k0, k1 = d["params"]["keys"][0]["t"], d["params"]["keys"][1]["t"]
        assert d["params"]["piece"][0] <= k0 < k1 < d["params"]["piece"][1]


def test_a_punch_that_starts_late_opens_on_the_hold_and_still_pushes_a_visible_amount():
    """EX-04: the seam into a punched piece whose push starts 1.5 s later was left as a plain cut (a punched piece
    'takes no hide'). It opens on the hold instead and the push continues from it: one curve, one mode. Finalize: at the
    shipped table that push was 1.08 -> 1.10, 2 % (slice `test_th_punch_in_is_a_push_the_viewer_can_see`, the review's
    bar is 5 %) — so the punch is raised to the hold + HIDE_PUNCH_ROOM."""
    g = simple_graph([
        Line("Some quiet setup that comes first.", pause_after=1.6),
        Line("Now listen, because this is the part that truly matters, and I mean it.", claim=True, emphasis=True, stress=9,
             topic_peak=True, pause_after=1.6),
        Line("And that is all of it.", pause_after=0.5)])
    edp = plan(g, {"content_type": "talking_head", "energy": 5})
    punch = next(d for d in edp["decisions"] if d["kind"] == "punch_in")
    hold = next(d for d in edp["decisions"] if d["kind"] == "jump_cut_hide" and d["params"]["piece"][0] <= punch["params"]["keys"][0]["t"])
    assert punch["params"]["keys"][0]["t"] - hold["params"]["keys"][0]["t"] >= 0.5, "the push starts well after the seam"
    assert hold["params"]["interp"] == punch["params"]["interp"] and hold["params"]["scale"] == E.knob("jump_cut_scale", 5)
    assert punch["params"]["keys"][0]["values"]["scale"] == hold["params"]["scale"] == punch["params"]["entry_scale"]
    assert punch["params"]["scale"] - hold["params"]["scale"] >= E.HIDE_PUNCH_ROOM - 1e-9
    assert punch["params"]["keys"][-1]["values"]["scale"] == punch["params"]["scale"], "the raised peak is the one the keys hold"
    assert punch["params"]["scale"] > E.knob("punch_scale", 5), "raised: its own 1.10 left a 2 % push"
    pieces = _programme(edp)
    piece = next(p for p in pieces if p[0] <= hold["params"]["piece"][0] < p[1])
    assert abs(_scale_at(edp, piece, 0.0, export=True) - hold["params"]["scale"]) < 1e-6
    before = _scale_at(edp, pieces[pieces.index(piece) - 1], pieces[pieces.index(piece) - 1][1] - pieces[pieces.index(piece) - 1][0], export=True)
    assert abs(_scale_at(edp, piece, 0.0, export=True) - before) >= E.HIDE_STEP_MIN - 1e-6, "the seam shows a step"
    assert _scale_at(edp, piece, piece[1] - piece[0], export=True) == punch["params"]["scale"], "and the punch holds to its release"
    for energy in (1, 3, 5, 7, 9):        # whatever the table, no punch pushes less than the bar from where its piece opens
        for d in plan(g, {"content_type": "talking_head", "energy": energy})["decisions"]:
            if d["kind"] == "punch_in":
                assert d["params"]["scale"] - d["params"]["keys"][0]["values"]["scale"] >= E.HIDE_PUNCH_ROOM - 1e-9, (energy, d["params"]["keys"])


def test_the_levels_are_chosen_over_the_whole_chain_not_seam_by_seam():
    from video_ai_editor.brain.planner import emphasis as em, make_ctx
    ctx = make_ctx(simple_graph([Line("One.")]), {"energy": 5})
    hide = float(ctx.energy["jump_cut_scale"])

    def piece(opens: str, *, punched: bool = False, exit_: float = 1.0, jump: float = 1.0, peak: float = 1.0):
        return em._Piece(0.0, 5.0, opens, 1.0, exit_, 0 if punched else None, peak, jump)
    # free, free, free: hold, back, hold — every seam a step
    assert em._choose_entries([piece("first"), piece("hide"), piece("hide"), piece("hide")], hide, ctx) == [1.0, hide, 1.0, hide]
    # a punch that ends on 1.10 (a step release) needs no hold after it: the next piece opens at 1.0 — and the punch, whose
    # seam would otherwise be plain, opens on the hold
    assert em._choose_entries([piece("first"), piece("hide", punched=True, exit_=1.1, peak=1.1), piece("hide")], hide, ctx) == [1.0, hide, 1.0]
    # … its push raised to hold + HIDE_PUNCH_ROOM (1.08 -> 1.10 is 2 %), and the exit the next seam reads with it
    assert em._options(piece("hide", punched=True, exit_=1.1, peak=1.1), hide, 0.2) == [(1.0, 1.1), (hide, round(hide + E.HIDE_PUNCH_ROOM, 4))]
    assert em._options(piece("hide", punched=True, exit_=1.0, peak=1.1), hide, 0.2) == [(1.0, 1.0), (hide, 1.0)]     # an ease release ends at 1.0
    assert em._choose_entries([piece("first"), piece("hide", punched=True, exit_=1.1, peak=hide), piece("hide")], hide, ctx) == [1.0, hide, 1.0]
    # free, free, punched: hold, back, and the punch opens on a hold — the three seams all show a step (seam by seam,
    # the third — into a piece that opens at 1.0 after a piece that ended at 1.0 — would have stayed plain)
    got = em._choose_entries([piece("first"), piece("hide", jump=0.4), piece("hide", jump=0.4),
                              piece("hide", punched=True, peak=1.1, jump=1.7)], hide, ctx)
    assert got == [1.0, hide, 1.0, hide], got
    # a punch SMALLER than the hold would have to be inflated past its own size to push from it: the seam into it stays plain,
    # and is not claimed
    got = em._choose_entries([piece("first"), piece("hide", punched=True, peak=1.02, jump=1.7)], hide, ctx)
    assert got == [1.0, 1.0]
    # a piece that is too short to hold two keys takes no hold
    short = em._Piece(0.0, 0.05, "hide", 1.0, 1.0, None, 1.0, 1.0)
    assert em._choose_entries([piece("first"), short], hide, ctx) == [1.0, 1.0]
