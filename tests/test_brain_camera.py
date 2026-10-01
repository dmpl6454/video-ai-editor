"""The Camera Director this wave (spec §4.3 rules 1, 1b, 2, 8) on the
two-camera podcast graph: anticipatory switches that never land after the
onset, backchannels that never cut, every tighten seam ≥ 0.4 s hidden — by
the speaker's switch when one belongs on it, else by a scale step —,
min-shot, the switch rate REPORTED against the style's cap, and determinism.

As integrated (EB1): every picture change is a speaker's switch 0-0.15 s
before their first sound (the slice's claim), so a seam inside a turn is
hidden by a scale step, not by a listener's-close cover that would have to
cut back mid-turn; and the cap never takes a speaker off camera."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import P2_A_KEY, P2_A_PATH, P2_B_KEY, p2_graph, p2_truth, th_graph  # noqa: E402

from gen_brain_goldens import encode_env  # noqa: E402

from video_ai_editor.brain import energy as E  # noqa: E402
from video_ai_editor.brain.planner import camera, make_ctx, plan  # noqa: E402
from video_ai_editor.brain.planner.graph_view import Graph  # noqa: E402

PODCAST = {"content_type": "podcast"}
FRAME = 1 / 30
HALF_FRAME = FRAME / 2


def _switches(edp: dict) -> list[dict]:
    return [d for d in edp["decisions"] if d["kind"] == "switch_angle"]


def _ref_time(t_file: float, offsets: dict, path: str) -> float:
    return t_file - offsets[path]


def test_p2_switches_land_0_to_0_15s_before_onsets():
    graph = p2_graph()
    truth = p2_truth(graph)
    edp = plan(graph, PODCAST)
    sw = _switches(edp)
    assert len(sw) >= 8, len(sw)
    turns = {t["id"]: t for t in truth["turns"]}
    checked = 0
    for d in sw:
        assert d["ref"]["src"] == P2_A_KEY and d["params"]["angle_src"] == P2_B_KEY
        turn = next((turns[f] for f in d["reason"]["facts"] if f in turns), None)
        if turn is None:
            assert d["reason"]["code"] == "at_cut", d["reason"]     # a seam hide cites the seam, not a turn
            continue
        at_ref = _ref_time(d["ref"]["t0"], truth["offsets"], P2_A_PATH)
        lead = turn["t0"] - at_ref
        if d["reason"]["code"] == "speaker_turn":
            assert 0.0 <= lead <= E.LEAD_MAX_S + 1e-6, (d["id"], lead)
            checked += 1
        elif d["reason"]["code"] == "at_cut":
            # a switch that sits ON a tighten seam lands where the seam is — and the seam's in-edge
            # was put where the switch wants to be: the same lead, never after the onset
            assert -1e-6 <= lead <= E.LEAD_MAX_S + 1e-6, (d["id"], lead)
            checked += 1
    assert checked >= 6
    # the summary says how much of the talking time each speaker's close carries
    assert edp["summary"]["camera"]["angles"] == 2 and edp["summary"]["camera"]["switches"] == len(sw)


def test_backchannels_never_switch():
    graph = p2_graph()
    truth = p2_truth(graph)
    edp = plan(graph, PODCAST)
    turns = {t["id"]: t for t in truth["turns"]}
    bc = truth["backchannel_turns"]
    assert len(bc) == 4
    for d in _switches(edp):
        assert not (set(d["reason"]["facts"]) & set(bc)), d
        at_ref = _ref_time(d["ref"]["t0"], truth["offsets"], P2_A_PATH)
        until_ref = _ref_time(d["ref"]["t1"], truth["offsets"], P2_A_PATH)
        for b in bc:
            t = turns[b]
            # a B span (the guest talking) is never cut away for a host "mm-hm": no switch boundary inside it
            assert not (t["t0"] - 0.3 < at_ref < t["t1"] + 0.05), (d["id"], b)
            assert not (t["t0"] - 0.3 < until_ref < t["t1"] + 0.05), (d["id"], b)


def test_every_tighten_seam_geq_0_4s_is_hidden():
    graph = p2_graph()
    edp = plan(graph, PODCAST)
    sw = _switches(edp)
    edges = {round(d["ref"]["t0"], 3) for d in sw} | {round(d["ref"]["t1"], 3) for d in sw}
    steps = {round(d["ref"]["t0"], 3) for d in edp["decisions"] if d["kind"] == "jump_cut_hide"}
    big = [d for d in edp["decisions"] if d["kind"] == "cut_range" and d["ref"]["t1"] - d["ref"]["t0"] >= E.HIDE_CUT_MIN_S - E.HIDE_TOL_S
           and d["ref"]["t0"] > 0.0]                                      # (the opening's own removal is no seam)
    assert len(big) >= 4, [(d["reason"]["code"], round(d["ref"]["t1"] - d["ref"]["t0"], 2)) for d in big]
    how = []
    for d in big:
        seam = d["ref"]["t1"]
        by_angle = any(abs(e - seam) <= HALF_FRAME + 1e-6 for e in edges)
        by_step = any(abs(e - seam) <= FRAME + 1e-6 for e in steps)     # (a hide sits on the grid of the picture that plays: up to a frame)
        assert by_angle != by_step, (d["reason"], seam, by_angle, by_step)      # hidden, and by ONE means
        how.append("angle" if by_angle else "step")
    assert "angle" in how and "step" in how, how
    at_cut = [d for d in sw if d["reason"]["code"] == "at_cut"]
    assert at_cut and all(d["reason"]["code"] == "at_cut" and d["reason"]["text"] for d in at_cut)   # the words are brain/reasons.py's
    assert edp["summary"]["camera"]["at_cut"] == len(at_cut)


def test_min_shot_and_rate_cap():
    graph = p2_graph()
    truth = p2_truth(graph)
    edp = plan(graph, PODCAST)
    sw = sorted(_switches(edp), key=lambda d: d["ref"]["t0"])
    times = sorted({round(_ref_time(d["ref"][k], truth["offsets"], P2_A_PATH), 4) for d in sw for k in ("t0", "t1")})
    # no shot under 1.2 s, whoever speaks next
    assert all(b - a >= E.MIN_SHOT_AT_CUT_S - 1e-6 for a, b in zip(times, times[1:])), \
        [(a, b) for a, b in zip(times, times[1:]) if b - a < E.MIN_SHOT_AT_CUT_S]
    # rule 2 as it applies to a whole turn (UX-10): the minimum shot yields to a full turn — a turn of 0.6 s or more
    # that is not a backchannel and not a true overlap takes the camera whatever its length
    g = Graph(graph)
    for tu in g.turns:
        length = tu["t1"] - tu["t0"]
        over = sum(max(0.0, min(o["t1"], tu["t1"]) - max(o["t0"], tu["t0"])) for o in g.turns
                   if o["id"] != tu["id"] and o.get("spk") != tu.get("spk"))
        assert camera._takes_camera(g, tu) == (
            length >= E.FULL_TURN_MIN_S and not g.is_backchannel(tu) and over < E.OVERLAP_TURN_SHARE * length), tu["id"]
    # rule 8 is REPORTED: the changes follow the speakers; above the cap the card says so, and only then
    for style, cap in (("premium_podcast", E.SWITCH_RATE_MAX_PREMIUM), ("clean_professional", E.SWITCH_RATE_MAX)):
        ctx = make_ctx(graph, PODCAST, style)
        switches = camera._turn_switches(ctx.graph, ctx, [])
        camera._rate(ctx.graph, ctx, switches, ctx.state.get("cuts") or [])
        rate = ctx.state["switch_rate"]
        assert abs(rate - len(switches) / (ctx.graph.ref_end / 60.0)) < 0.01
        said = [d for d in ctx.deferred if d["asked"] == "switch rate"]
        assert bool(said) == (rate > cap), (style, rate, cap, said)
        if said:
            assert "follow the speakers" in said[0]["why"] and str(cap) in said[0]["why"]
    assert len(camera._turn_switches(make_ctx(graph, PODCAST).graph, make_ctx(graph, PODCAST), [])) >= 2 * len(sw) - 1


def test_onset_skips_a_one_frame_transient_ahead_of_the_voice():
    """A slate click 50 ms before the voice is what a voicing detector
    latches onto; the switch is timed from the sound that SUSTAINS."""
    env = [-120] * 400
    env[100] = -7                                   # the click, one 10 ms frame
    for f in range(105, 300):
        env[f] = -25                                # the voice, from 1.05 s
    graph = p2_graph()
    graph["layers"]["audio"]["env_10ms"] = encode_env(env)
    g = Graph(graph)
    assert g.onset(1.0) == 1.05
    assert g.onset(1.05) == 1.05
    assert g.onset(2.0) == 2.0                      # already inside sustained sound
    assert g.onset(3.5) == 3.5                      # nothing sustains within reach: the layer's own instant


def test_the_lead_is_planned_inside_the_clamp():
    # 3 frames at 30 fps is 0.10 s; at 20 fps it would be 0.15 s — the whole clamp, with nothing left
    # for how well an onset is known (±0.03 s measured on the podcast fixture)
    assert abs(E.lead_s(30.0) - 0.1) < 1e-9
    assert abs(E.lead_s(20.0) - (E.LEAD_MAX_S - E.ONSET_TOL_S)) < 1e-9
    ctx = make_ctx(p2_graph(), PODCAST)
    g = ctx.graph
    for onset in (10.0, 10.013, 10.02, 10.0333):
        t = camera._lead_time(g, ctx, onset, "nobody")
        assert 0.0 < onset - t <= E.lead_s(ctx.fps) + 1e-6, (onset, t)       # the grid rounds TOWARD the onset


def test_single_angle_yields_only_scale_steps():
    edp = plan(th_graph(), {"duration_s": 45.0, "platform": "reels"})
    assert not _switches(edp)
    hides = [d for d in edp["decisions"] if d["kind"] == "jump_cut_hide"]
    assert hides, "a single camera hides its ≥ 0.4 s seams with scale steps"
    scales = [d["params"]["keys"][0]["values"]["scale"] for d in sorted(hides, key=lambda d: d["ref"]["t0"])]
    assert all(s in (1.0, E.knob("jump_cut_scale", 5)) for s in scales), scales
    # a hide is claimed only where the two sides differ (EX-04): the decision says from what to what
    assert all(abs(d["params"]["scale"] - d["params"]["from_scale"]) >= E.HIDE_STEP_MIN - 1e-6 for d in hides), \
        [(d["params"]["from_scale"], d["params"]["scale"]) for d in hides]
    assert all(d["params"]["interp"] == "step" and len(d["params"]["keys"]) == 2 for d in hides)
    assert all(d["reason"]["code"] == "jump_cut_hide" for d in hides)
    assert edp["summary"]["camera"] == {"angles": 1, "switches": 0, "at_cut": 0}


def test_deterministic():
    a = json.dumps(plan(p2_graph(), PODCAST), sort_keys=True)
    b = json.dumps(plan(p2_graph(), PODCAST), sort_keys=True)
    assert a == b


# --------------------------------------------------------------------------
# FX-C1: whole turns on their speaker's close (UX-10, EX-07)
# --------------------------------------------------------------------------

def _angle_timeline(edp: dict, g: Graph) -> list[tuple[float, float, str]]:
    """(t0, t1, angle letter) spans in REFERENCE seconds, from the switch decisions alone."""
    off = g.offset(g.primary)
    spans = sorted((d["ref"]["t0"] - off, d["ref"]["t1"] - off, d["params"]["letter"]) for d in _switches(edp))
    line, cur = [], 0.0
    for a, b, letter in spans:
        line.append((cur, a, g.angle_of_key(g.primary)))
        line.append((a, b, letter))
        cur = b
    line.append((cur, 1e9, g.angle_of_key(g.primary)))
    return [x for x in line if x[1] - x[0] > 1e-9]


def _kept(t0: float, t1: float, edp: dict, g: Graph) -> list[tuple[float, float]]:
    off = g.offset(g.primary)
    cuts = sorted((d["ref"]["t0"] - off, d["ref"]["t1"] - off) for d in edp["decisions"] if d["kind"] == "cut_range")
    out, cur = [], t0
    for c0, c1 in cuts:
        if c1 <= cur or c0 >= t1:
            continue
        if c0 > cur:
            out.append((cur, c0))
        cur = max(cur, c1)
    if cur < t1:
        out.append((cur, t1))
    return out


def _minus(t0: float, t1: float, holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out, cur = [], t0
    for h0, h1 in holes:
        if h1 <= cur or h0 >= t1:
            continue
        if h0 > cur:
            out.append((cur, h0))
        cur = max(cur, h1)
    if cur < t1:
        out.append((cur, t1))
    return out


def _on_screen(edp: dict, g: Graph, turn: dict, *, overlaps: bool = True) -> float:
    """Share of the turn's kept time shown on its speaker's close; `overlaps=False` leaves out the seconds
    another voice's turn already covers (the camera follows the NEW speaker there, by design)."""
    want = g.angle_of_key(g.key_of_angle(g.speaker_angle(turn["spk"]) or "") or "")
    line = _angle_timeline(edp, g)
    on = total = 0.0
    parts = _kept(turn["t0"], turn["t1"], edp, g)
    if not overlaps:
        others = sorted((o["t0"], o["t1"]) for o in g.turns if o["spk"] != turn["spk"])
        parts = [q for a, b in parts for q in _minus(a, b, others)]
    for a, b in parts:
        total += b - a
        on += sum(max(0.0, min(b, y) - max(a, x)) for x, y, letter in line if letter == want)
    return on / total if total else 1.0


def test_every_full_turn_is_on_its_speakers_close():
    """UX-10 / EX-07: a complete turn is shown on its speaker's close whether it is 1.2 s or 12 s; only a backchannel and a
    true overlap stay where they were. The 2.5 s minimum shot used to outrank whole lines of 1.2-2.4 s."""
    graph = p2_graph()
    g = Graph(graph)
    edp = plan(graph, PODCAST)
    full = [t for t in g.turns if camera._takes_camera(g, t)]
    short_full = [t for t in full if t["t1"] - t["t0"] < E.knob("camera_min_shot_s", 5)]
    assert len(short_full) >= 3, [(t["id"], round(t["t1"] - t["t0"], 2)) for t in full]     # the case this exists for
    off = [(t["id"], round(_on_screen(edp, g, t, overlaps=False), 2)) for t in full if _on_screen(edp, g, t, overlaps=False) < 0.98]
    assert not off, f"whole turns not on their speaker's close: {off}"
    for t in g.turns:
        if g.is_backchannel(t) or t["t1"] - t["t0"] < E.BACKCHANNEL_MAX_S:
            assert not camera._takes_camera(g, t), t["id"]


def test_the_talk_share_and_the_switch_count_are_reported():
    graph = p2_graph()
    g = Graph(graph)
    ctx = make_ctx(graph, PODCAST)
    from video_ai_editor.brain.planner import PASSES, camera as _cam
    decisions: list[dict] = []
    for p in PASSES:
        decisions = p.run(ctx.graph, ctx, decisions)
        if p is _cam:
            break
    stats = ctx.state["camera_stats"]
    edp = {"decisions": decisions}
    assert stats["switches"] == ctx.state["camera"]["switches"] + 0 or stats["switches"] >= ctx.state["camera"]["switches"]
    on = total = 0.0
    for t in g.turns:                                       # restated from the decisions alone
        for a, b in _kept(t["t0"], t["t1"], edp, g):
            total += b - a
            on += (b - a) * _on_screen({"decisions": decisions}, g, {**t, "t0": a, "t1": b})
    assert abs(stats["talk_share"] - on / total) < 0.005, (stats, on / total)
    assert stats["talk_share"] >= 0.95 and stats["cap"] == E.SWITCH_RATE_MAX_PREMIUM
    assert stats["rate"] > stats["cap"], "a fast conversation exceeds the cap: reported, not enforced"
    said = next(d for d in ctx.deferred if d["asked"] == "switch rate")
    assert f"{stats['talk_share']:.0%}" in said["why"]


def test_two_close_turns_never_make_a_shot_under_1_2_s_and_the_longer_one_keeps_the_camera():
    graph = p2_graph()
    g = Graph(graph)
    guest, host = "S2", "S1"
    turns = [{"id": "u_a", "spk": guest, "t0": 10.0, "t1": 16.0, "sents": []},
             {"id": "u_b", "spk": host, "t0": 16.3, "t1": 17.2, "sents": []},          # 0.9 s, then the guest again at once
             {"id": "u_c", "spk": guest, "t0": 17.5, "t1": 24.0, "sents": []}]
    g.__dict__["turns"] = turns
    ctx = make_ctx(g, PODCAST)
    sw = camera._turn_switches(g, ctx, [])
    times = [s.t for s in sw]
    assert all(b - a >= E.MIN_SHOT_AT_CUT_S - 1e-6 for a, b in zip(times, times[1:])), times
    assert "u_c" not in [s.turn["id"] for s in sw if s.turn is not None and s.turn["id"] == "u_b"]
    assert not any(s.turn is not None and s.turn["id"] == "u_b" for s in sw), "the 0.9 s line lost to the 6.5 s one it sits between"


def test_a_true_overlap_never_switches_but_a_partial_one_still_does():
    graph = p2_graph()
    g = Graph(graph)
    host, guest = "S1", "S2"
    turns = [{"id": "u_a", "spk": guest, "t0": 10.0, "t1": 20.0, "sents": []},
             {"id": "u_b", "spk": host, "t0": 12.0, "t1": 14.0, "sents": []}]          # spoken entirely over the guest
    g.__dict__["turns"] = turns
    assert not camera._takes_camera(g, turns[1])
    turns2 = [{"id": "u_a", "spk": guest, "t0": 10.0, "t1": 20.0, "sents": []},
              {"id": "u_b", "spk": host, "t0": 19.2, "t1": 22.0, "sents": []}]        # 0.8 of 2.8 s over the guest's tail
    g.__dict__["turns"] = turns2
    assert camera._takes_camera(g, turns2[1])


# --------------------------------------------------------------------------
# FX-C1: who speaks, from the closes' own microphones (the turns the words mislabel)
# --------------------------------------------------------------------------

def _with_mics(graph: dict, talk: list[tuple[str, float, float]] | None = None) -> dict:
    """The podcast graph with `own_mic_energy` laid the way two closes hear it: a speaker's own mic 6 dB louder
    than the other's while they talk (`talk` = (speaker, t0, t1); default the graph's own words)."""
    import copy
    out = copy.deepcopy(graph)
    hz = out["layers"]["audio"]["hz"]
    n = len(__import__("base64").b64decode(out["layers"]["audio"]["env_10ms"]))
    key = {"S1": P2_A_KEY, "S2": P2_B_KEY}
    mic = {k: [-60.0] * n for k in key.values()}
    spans = talk or [(w["spk"], w["t0"], w["t1"]) for w in out["layers"]["speech"]["words"]]
    for spk, t0, t1 in spans:
        other = "S2" if spk == "S1" else "S1"
        for f in range(max(0, int(t0 * hz)), min(n, int(t1 * hz))):
            mic[key[spk]][f] = max(mic[key[spk]][f], -40.0)
            mic[key[other]][f] = max(mic[key[other]][f], -46.0)
    out["layers"]["audio"]["own_mic_energy"] = mic
    return out


def test_a_turn_the_microphones_put_on_the_other_speaker_is_that_speakers():
    """The end of the guest's line ("number.") was diarised as the host's turn: the words say the guest, the
    microphones say the guest (−6 dB), and the camera stayed put."""
    graph = _with_mics(p2_graph())
    g = Graph(graph)
    assert g.mic_signature is not None and min(g.mic_signature.values()) >= 5.9
    real = next(t for t in g.turns if t["spk"] == "S2" and t["t1"] - t["t0"] > 3)
    assert g.turn_spk(real) == "S2"
    wrong = {**real, "id": "u_wrong", "spk": "S1"}                       # the same sound, called the host's
    assert g.turn_spk(wrong) == "S2"
    # without the microphones, or where they do not tell the speakers apart, the label stands
    bare = Graph(p2_graph())
    assert bare.mic_signature is None and bare.turn_spk(wrong) == "S1"
    flat = _with_mics(p2_graph())
    for k in flat["layers"]["audio"]["own_mic_energy"]:
        flat["layers"]["audio"]["own_mic_energy"][k] = [-45.0] * len(flat["layers"]["audio"]["own_mic_energy"][k])
    assert Graph(flat).mic_signature is None


def test_an_overlap_the_words_missed_starts_the_turn_where_the_speaker_starts():
    """Host and guest talk over each other for 0.8 s and the words are labelled by sentence, so the host's turn
    starts when the guest's words end. Their close hears them from the overlap's first frame: the switch goes there."""
    base = p2_graph()
    g0 = Graph(base)
    prev = next(t for t in g0.turns if t["spk"] == "S2" and t["t1"] - t["t0"] > 4)
    nxt = next(t for t in g0.turns if t["spk"] == "S1" and t["t0"] >= prev["t1"])
    over = round(prev["t1"] - 0.9, 2)                                     # the host starts 0.9 s inside the guest's tail
    talk = [(w["spk"], w["t0"], w["t1"]) for w in base["layers"]["speech"]["words"]]
    talk += [("S1", over, nxt["t1"])]
    graph = _with_mics(base, talk)
    g = Graph(graph)
    assert g.speaker_onset(nxt) < nxt["t0"] - 0.5, (g.speaker_onset(nxt), nxt["t0"])
    assert abs(g.speaker_onset(nxt) - over) < 0.06, (g.speaker_onset(nxt), over)
    # every other turn keeps its onset (the guard: only an overlap over the other's line moves one)
    moved = [t["id"] for t in g.turns if t["id"] != nxt["id"] and abs(g.speaker_onset(t) - g.onset(t["t0"])) > 1e-6]
    assert not moved, moved
    # the switch is where the host starts, and never after it
    edp = plan(graph, PODCAST)
    off = g.offset(g.primary)
    edges = [d["ref"][k] - off for d in _switches(edp) for k in ("t0", "t1")]      # every change of picture (to the guest, back to the host)
    assert any(0.0 <= over - s <= E.LEAD_MAX_S for s in edges), (over, edges)


def test_a_seam_far_ahead_of_the_onset_does_not_take_the_switch():
    """Slice (`test_p2_talking_time_on_the_speakers_close_and_switches_anticipate`): a removal ending 0.15 s before the
    host's first kept sound took the switch (rule 1b's 0.25 s snap), so the camera changed 0.1558 s ahead of the
    sound — over the 0..0.15 s rule once the onset estimate's own error (up to +36 ms) is counted. A seam takes the
    switch only where it ends within the anticipation lead of the onset."""
    from video_ai_editor.brain.planner import camera as CAM, make_ctx
    g = Graph(p2_graph())
    ctx = make_ctx(g, PODCAST)
    turn = next(t for t in g.turns if g.turn_spk(t) == "S2" and t["t1"] - t["t0"] > 4)
    angle = g.key_of_angle(g.speaker_angle("S2") or "")
    onset = g.speaker_onset(turn) if g.turn_spk(turn) == turn.get("spk") else g.onset(float(turn["t0"]))

    def cut(end: float) -> dict:
        return {"t0": round(end - 0.35, 4), "t1": round(end, 4), "code": "filler", "facts": ["w_x"]}
    far = CAM._turn_switch(g, ctx, [cut(onset - 0.15)], turn, angle)
    assert 0.0 <= onset - far.t <= E.LEAD_MAX_S - 0.03, (far.t, onset)           # room for the +36 ms
    near = CAM._turn_switch(g, ctx, [cut(onset - 0.05)], turn, angle)
    assert abs(near.t - (onset - 0.05)) < 1e-6, "a seam inside the lead still takes the switch"
    inside = CAM._turn_switch(g, ctx, [cut(onset + 0.4)], turn, angle)
    assert inside.t == round(cut(onset + 0.4)["t1"], 4) or 0.0 <= onset - inside.t <= E.LEAD_MAX_S
