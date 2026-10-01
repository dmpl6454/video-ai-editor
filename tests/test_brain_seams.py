"""Where a cut edge lands (spec §4.6.2) and which pauses survive (§4.6.3):
the 10 ms envelope's trough within ±80 ms of the word boundary, outside
every kept word's voiced span, ties toward the later trough for an
out-point and the earlier for an in-point; ≥ 40 ms of air before a kept
onset; protected pauses kept as `keep_pause` decisions with their why;
turn-boundary floors from the Energy table."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import Line, encode_env, simple_graph  # noqa: E402

from video_ai_editor.brain import energy as E  # noqa: E402
from video_ai_editor.brain.planner import make_ctx, tighten  # noqa: E402
from video_ai_editor.brain.planner import seams  # noqa: E402
from video_ai_editor.brain.planner.graph_view import Graph  # noqa: E402

HZ = 100


def _env(n: int, level: int = -58) -> list[int]:
    return [level] * n


def test_edges_on_troughs_outside_words_with_tie_rules():
    env = _env(500, -40)
    # word A ends at 1.50 s; two equal minima 40 ms and 70 ms later (within 1 dB) → the out-point takes the LATER
    env[154] = -55
    env[157] = -55
    # word B starts at 3.00 s; equal minima 70 ms and 40 ms before → the in-point takes the EARLIER
    env[293] = -55
    env[296] = -55
    # a deeper dip INSIDE word A's voiced span must never be chosen
    env[148] = -70
    kept = [(1.0, 1.5), (3.0, 3.5)]
    assert seams.trough_time(env, HZ, 1.5, kind="out", kept=kept) == 1.57
    assert seams.trough_time(env, HZ, 3.0, kind="in", kept=kept) == 2.93
    # a tie that is NOT within 1 dB is not a tie: the real minimum wins
    env[154] = -57
    assert seams.trough_time(env, HZ, 1.5, kind="out", kept=kept) == 1.54


def test_air_floor_40ms():
    # energy 10: keep-pad 0.06 s; the envelope's minimum hugs the onset at 3.00 s
    env = _env(500, -40)
    for f in range(290, 300):
        env[f] = -60 + 2 * (299 - f)      # quietest right before the onset (2.99 s), no 1 dB ties
    for f in range(150, 160):
        env[f] = -60 + 2 * (f - 150)      # quietest right after the decay (1.50 s)
    t0, t1 = seams.place_cut(env, HZ, prev_end=1.5, next_start=3.0, pad=E.knob("keep_pad_s", 10), kept=[(1.0, 1.5), (3.0, 3.5)])
    assert t1 <= 3.0 - E.AIR_MIN_S + 1e-9, (t0, t1)
    assert t0 >= 1.5 + E.knob("keep_pad_s", 10) - 1e-9
    # a pad measured from the trough, not the token: at energy 5 the cut starts pad after the 1.50 s trough
    t0, t1 = seams.place_cut(env, HZ, prev_end=1.5, next_start=3.0, pad=0.15, kept=[(1.0, 1.5), (3.0, 3.5)])
    assert abs(t0 - 1.65) < 1e-9 and abs(t1 - 2.84) < 1e-9


def _protection_graph() -> dict:
    return simple_graph([
        Line("We nearly lost the company that spring.", emotion=0.8, pause_after=1.6),          # (a) after emotion
        Line("What was the hardest month?", hook_floor=0.7, pause_after=1.6),                   # (b) before the answer
        Line("March, when the factory asked for the deposit.", answer_of=1, pause_after=0.5),
        Line("So in the end, the proof is what raised the money, all twelve customers of it.", conclusion=True, claim=True,
             topic_peak=True, strong_number=True, answer_of=1, pause_after=1.6),               # (c) conclusion, importance ≥ 0.7
        Line("And that is the whole story.", pause_after=0.5),
    ])


def test_protected_pause_classes_and_turn_floor():
    g = Graph(_protection_graph())
    ctx = make_ctx(g, {"content_type": "podcast", "energy": 5}, "premium_podcast")
    decisions = tighten.run(g, ctx, [])
    kept = {d["reason"]["code"]: d for d in decisions if d["kind"] == "keep_pause"}
    assert set(kept) >= {"pause_kept:emotion", "pause_kept:hard_question", "pause_kept:conclusion"}, sorted(kept)
    for code, d in kept.items():
        pause_len = d["params"]["pause_s"]
        want = min(E.KEEP_PAUSE_MAX_S, max(E.knob("keep_pad_s", 5), E.KEEP_PAUSE_FRAC * pause_len))
        assert d["params"]["kept_s"] >= want - 1e-6, (code, d["params"])
        assert d["reason"]["facts"], code
        assert d["reason"]["text"].lower().startswith("kept "), d["reason"]["text"]
    # every protected pause's cut leaves at least the kept air
    cuts = [d for d in decisions if d["kind"] == "cut_range"]
    for d in kept.values():
        p0, p1 = d["params"]["pause"]
        removed = sum(min(c["ref"]["t1"], p1) - max(c["ref"]["t0"], p0) for c in cuts
                      if c["ref"]["t1"] > p0 and c["ref"]["t0"] < p1)
        assert (p1 - p0) - removed >= d["params"]["kept_s"] - 1e-6


def test_turn_boundary_floor_and_in_turn_table_value():
    two = simple_graph([
        Line("So tell me how it started.", spk="S1", pause_after=1.8),
        Line("It started in a garage with one customer.", spk="S2", answer_of=0, dead_air=(4, 2.4), pause_after=0.5),
        Line("And it grew from there.", spk="S2", turn_start=False, pause_after=0.5),
    ])
    g = Graph(two)
    ctx = make_ctx(g, {"content_type": "podcast", "energy": 4}, "premium_podcast")
    decisions = tighten.run(g, ctx, [])
    sents = {s["id"]: s for s in g.sentences}
    gap0, gap1 = sents["s_00001"]["t1"], sents["s_00002"]["t0"]
    boundary = [d for d in decisions if d["kind"] == "cut_range" and d["ref"]["t0"] >= gap0 - 1e-6 and d["ref"]["t1"] <= gap1 + 1e-6]
    assert len(boundary) == 1, decisions
    left = (gap1 - gap0) - (boundary[0]["ref"]["t1"] - boundary[0]["ref"]["t0"])
    assert left >= E.knob("turn_floor_s", 4) - 1e-6, left
    dead = g.flags["dead_air"][0]
    inside = [d for d in decisions if d["kind"] == "cut_range" and d["reason"]["code"] == "dead_air"]
    assert len(inside) == 1 and dead["id"] in inside[0]["reason"]["facts"]
    removed = inside[0]["ref"]["t1"] - inside[0]["ref"]["t0"]
    pad = E.knob("keep_pad_s", 4)
    # the table value: the pause minus a pad each side (the trough snap may widen the kept air by ≤ 80 ms a side)
    assert 2.4 - 2 * pad - 0.16 - 1e-6 <= removed <= 2.4 - 2 * pad + 1e-6, removed


def test_edges_never_inside_a_kept_word_and_fillers_keep_the_neighbours_tail():
    g = Graph(simple_graph([Line("So the, um, lens is the thing that matters.", fillers=(2,), pause_after=0.5)]))
    ctx = make_ctx(g, {"content_type": "talking_head", "energy": 5}, "viral_reel")
    decisions = tighten.run(g, ctx, [])
    fill = [d for d in decisions if d["reason"]["code"] == "filler"]
    assert len(fill) == 1
    um = next(w for w in g.words if w.get("filler"))
    prev = next(w for w in g.words if w["t1"] <= um["t0"] and w["sent"] == um["sent"] and w["text"] == "the,")
    nxt = next(w for w in g.words if w["t0"] >= um["t1"])
    d = fill[0]
    assert d["ref"]["t0"] >= prev["t1"] - 1e-9, "the preceding word's tail belongs to the neighbour"
    assert d["ref"]["t0"] <= um["t0"] + 1e-9
    assert d["ref"]["t1"] <= nxt["t0"] - E.AIR_MIN_S + 1e-9
    assert um["id"] in d["reason"]["facts"]
    env = encode_env([-58] * 10)
    assert isinstance(env, str)


# --------------------------------------------------------------------------
# EB1 integration: removals on the frame grid, merged by union, never over
# sound no word names
# --------------------------------------------------------------------------

def test_two_removals_that_touch_are_one_with_the_more_specific_reason():
    got = seams.merge_ranges([
        {"t0": 1.0, "t1": 2.0, "code": "silence", "facts": ["sil_0001"], "fields": {"dur": "1.0 s"}},
        {"t0": 1.4, "t1": 1.7, "code": "filler_acoustic", "facts": ["af_0001"], "fields": {"tc": "x"},
         "cover": (1.4, 1.7, 0.9, 2.3)},
    ])
    assert len(got) == 1
    assert (got[0]["t0"], got[0]["t1"], got[0]["code"]) == (1.0, 2.0, "filler_acoustic")
    assert got[0]["facts"] == ["af_0001", "sil_0001"] and got[0]["fields"] == {"tc": "x"}
    assert got[0]["cover"] == (1.4, 1.7, 0.9, 2.3)
    assert seams.CODE_PRIORITY.index("false_start") < seams.CODE_PRIORITY.index("filler") < seams.CODE_PRIORITY.index("silence")


def test_a_sliver_of_air_between_two_removals_is_one_cut():
    ranges = [{"t0": 1.0, "t1": 2.0, "code": "silence", "facts": ["sil_0001"]},
              {"t0": 2.2, "t1": 2.6, "code": "filler", "facts": ["w_1"]}]
    one = seams.merge_ranges(ranges, kept=[(0.0, 0.9), (2.8, 3.5)])
    assert [(r["t0"], r["t1"], r["code"]) for r in one] == [(1.0, 2.6, "filler")]
    # a kept word in the sliver keeps them apart; so does a piece of half a second or more
    two = seams.merge_ranges(ranges, kept=[(0.0, 0.9), (2.05, 2.15), (2.8, 3.5)])
    assert [(r["t0"], r["t1"]) for r in two] == [(1.0, 2.0), (2.2, 2.6)]
    far = seams.merge_ranges([ranges[0], {**ranges[1], "t0": 2.0 + seams.MIN_KEPT_PIECE_S, "t1": 3.0}], kept=[])
    assert len(far) == 2
    assert len(seams.merge_ranges(ranges)) == 2                       # without the kept words nothing is assumed


def test_the_unvoiced_part_of_a_gap():
    assert seams.unvoiced_part(10.0, 13.0, [(11.8, 12.4)], 0.15) == (10.0, 11.65)
    assert seams.unvoiced_part(10.0, 13.0, [(10.2, 10.5)], 0.15) == (10.65, 13.0)
    assert seams.unvoiced_part(10.0, 11.0, [(9.9, 11.2)], 0.15) is None


def test_removal_edges_are_on_the_grid_and_the_tools_rounding_is_a_no_op():
    from video_ai_editor.edl import timebase as tb
    g = Graph(simple_graph([Line("One."), Line("Two.")]))
    for fps in (30.0, 20.0, 24.0, 29.97):
        ctx = make_ctx(g, {"energy": 5})
        ctx.fps = fps
        for t in (1.0, 14.01, 37.7025, 49.64, 61.4001, 100.0 / 3.0):
            a, b = ctx.removal_start(t), ctx.removal_end(t)
            assert a >= t - 1e-4 and b <= t + 1e-4 and 0.0 <= a - b <= ctx.frame() + 2e-4, (fps, t, a, b)
            # the tool floors a removal's start and ceils its end (dispatch._cut_source_ranges)
            assert abs(tb.floor_to_frame(a, fps) - a) < 1.01e-4, (fps, t, a, tb.floor_to_frame(a, fps))
            assert abs(tb.ceil_to_frame(b, fps) - b) < 1.01e-4, (fps, t, b, tb.ceil_to_frame(b, fps))
            # and a time already on the grid stays where it is, whichever way it is rounded
            assert ctx.removal_end(a) == ctx.removal_end(ctx.removal_end(a)) and abs(ctx.removal_end(a) - a) <= 1.01e-4
            assert abs(ctx.removal_start(b) - b) <= 1.01e-4


def test_every_cut_of_a_plan_is_on_the_frame_grid_and_a_filler_goes_whole():
    g = Graph(simple_graph([Line("So the, um, lens is the thing that matters.", fillers=(2,), pause_after=1.5),
                            Line("It is the glass.", pause_after=0.5)]))
    ctx = make_ctx(g, {"content_type": "talking_head", "energy": 5}, "viral_reel")
    decisions = tighten.run(g, ctx, [])
    cuts = [d for d in decisions if d["kind"] == "cut_range"]
    assert len(cuts) >= 2
    for d in cuts:
        for t in (d["ref"]["t0"], d["ref"]["t1"]):
            assert abs(t * ctx.fps - round(t * ctx.fps)) < 0.01, (d["reason"]["code"], t)
    um = next(w for w in g.words if w.get("filler"))
    fill = next(d for d in cuts if d["reason"]["code"] == "filler")
    assert fill["ref"]["t0"] <= um["t0"] + 1e-6 and fill["ref"]["t1"] >= um["t1"] - 1e-6, (fill["ref"], um)


def test_sound_no_word_names_is_never_cut_as_a_silence():
    """Lane D's request: a gap between two WORDS is not yet a silence —
    whisper drops whole phrases. The audio layer's voiced runs and the
    speech layer's `unheard_voice` flags bound every gap cut."""
    graph = simple_graph([Line("We shipped it in the spring.", pause_after=3.0), Line("Then the orders came.", pause_after=0.5)])
    g0 = Graph(graph)
    a, b = g0.sentences[0]["t1"], g0.sentences[1]["t0"]
    heard = (round(a + 1.2, 2), round(a + 1.9, 2))                      # a phrase the transcript has no word for
    graph["layers"]["audio"]["vad"] = [*graph["layers"]["audio"].get("vad", []), list(heard)]
    graph["layers"]["speech"]["flags"]["technical"] = [{"id": "x_0001", "t0": heard[0], "t1": heard[1], "why": "unheard_voice"}]
    g = Graph(graph)
    ctx = make_ctx(g, {"content_type": "talking_head", "energy": 5}, "clean_professional")
    cuts = [d for d in tighten.run(g, ctx, []) if d["kind"] == "cut_range"]
    pad = E.knob("keep_pad_s", 5)
    for d in cuts:
        assert d["ref"]["t1"] <= heard[0] - pad + 1e-6 or d["ref"]["t0"] >= heard[1] + pad - 1e-6, (d["ref"], heard)
    assert any(a < d["ref"]["t0"] < b for d in cuts), "the silent part of the gap is still tightened"
    # without the sound, the whole gap is one silence
    plain = Graph(simple_graph([Line("We shipped it in the spring.", pause_after=3.0), Line("Then the orders came.", pause_after=0.5)]))
    whole = [d for d in tighten.run(plain, make_ctx(plain, {"energy": 5}, "clean_professional"), []) if d["kind"] == "cut_range"]
    assert max(d["ref"]["t1"] - d["ref"]["t0"] for d in whole) > max(d["ref"]["t1"] - d["ref"]["t0"] for d in cuts)


def test_a_filler_island_is_the_voiced_run_not_the_token():
    """Whisper times the NEXT word into the tail of an "um": the voiced run
    (≤ 0.6 s) the filler sits in is what goes."""
    graph = simple_graph([Line("Um, we threw away the case.", fillers=(0,), pause_after=0.5)])
    g0 = Graph(graph)
    um = next(w for w in g0.words if w.get("filler"))
    nxt = next(w for w in g0.words if w["t0"] >= um["t1"] - 1e-6 and not w.get("filler"))
    island = [round(um["t0"], 2), round(um["t1"] + 0.1, 2)]             # the um rings on 0.1 s under the next token
    graph["layers"]["audio"]["vad"] = [island, [round(nxt["t0"] + 0.27, 2), round(g0.sentences[0]["t1"], 2)]]
    for w in graph["layers"]["speech"]["words"]:
        if w["id"] == nxt["id"]:
            w["t0"], w["t1"] = um["t1"], round(um["t1"] + 0.1, 4)       # "we", timed into the island
    g = Graph(graph)
    assert tighten._filler_island(g, next(w for w in g.words if w.get("filler"))) == (island[0], island[1])
    ctx = make_ctx(g, {"energy": 5}, "viral_reel")
    fill = next(d for d in tighten.run(g, ctx, []) if d["reason"]["code"] == "filler")
    assert fill["ref"]["t1"] >= island[1] - 1e-6, (fill["ref"], island)


# --------------------------------------------------------------------------
# FX-C1: speech the transcript dropped is speech, on every path to a cut
# --------------------------------------------------------------------------

import copy  # noqa: E402
import json  # noqa: E402

from gen_brain_goldens import p2_graph  # noqa: E402

GOLDEN_P2 = Path(__file__).parent / "goldens" / "brain" / "graphs" / "two_cam_podcast.json"
#: what whisper dropped on the podcast fixture (lane D's report): "Why the sensor first?", "The lens, the heat,", "Yeah."
P2_UNHEARD = ((33.70, 35.10), (38.14, 39.46), (71.05, 71.51))
PODCAST = {"content_type": "podcast"}


def _drop_words(graph: dict, inside) -> dict:
    """The graph as if whisper had dropped the words `inside(w)` names: the words, and the sentences they leave empty,
    are gone; the audio layer (voiced runs, envelope) is untouched."""
    out = copy.deepcopy(graph)
    sp = out["layers"]["speech"]
    sp["words"] = [w for w in sp["words"] if not inside(w)]
    left: dict[str, list[dict]] = {}
    for w in sp["words"]:
        left.setdefault(w["sent"], []).append(w)
    sents = []
    for st in sp["sentences"]:
        if st["id"] in left:
            st["t0"], st["t1"] = left[st["id"]][0]["t0"], left[st["id"]][-1]["t1"]
            sents.append(st)
    sp["sentences"] = sents
    for t in sp["turns"]:
        t["sents"] = [x for x in t["sents"] if x in left]
    sp["turns"] = [t for t in sp["turns"] if t["sents"]]
    return out


def _cuts_over(edp: dict, g: Graph, spans) -> list[tuple]:
    off = g.offset(g.primary)
    cuts = [(d["ref"]["t0"] - off, d["ref"]["t1"] - off, d) for d in edp["decisions"] if d["kind"] == "cut_range"]
    return [(s, round(a, 3), round(b, 3), d["reason"]["code"]) for s in spans for a, b, d in cuts if min(b, s[1]) - max(a, s[0]) > 0.005]


def test_the_p2_stretches_whisper_dropped_are_never_cut():
    """On the podcast fixture three stretches (33.70-35.10, 38.14-39.46, 71.05-71.51 s) lie under no word. With the words
    gone and the flags (or only the voiced runs) saying so, none of them is cut — and the ones cut around them still say so."""
    from video_ai_editor.brain.planner import plan
    base = json.loads(GOLDEN_P2.read_text(encoding="utf-8"))
    for flagged in (False, True):
        graph = _drop_words(base, lambda w: any(w["t0"] >= a - 0.05 and w["t1"] <= b + 0.05 for a, b in P2_UNHEARD))
        if flagged:
            graph["layers"]["speech"]["flags"]["technical"] = [
                {"id": f"x_{i + 1:04d}", "t0": a, "t1": b, "why": "unheard_voice"} for i, (a, b) in enumerate(P2_UNHEARD)]
        g = Graph(graph)
        assert not [w for w in g.words if 33.75 < w["t0"] < 35.0], "the stretch has no word"
        edp = plan(g, PODCAST)
        assert not _cuts_over(edp, g, P2_UNHEARD), _cuts_over(edp, g, P2_UNHEARD)


def test_speech_dropped_by_the_transcript_is_not_cut_through_a_merge_a_filler_or_a_switch_edge():
    """A voiced run is split at every breath: a dropped SENTENCE is several short runs with a few
    hundredths of air between them. Each used to look like 'air between two removals' and the silences around a
    dropped sentence merged across it (41.1-45.5 s of the podcast graph, 3 s of speech, one 'silence')."""
    from video_ai_editor.brain.planner import plan
    base = p2_graph()
    words = {}
    for w in base["layers"]["speech"]["words"]:
        words.setdefault(w["sent"], []).append((w["t0"], w["t1"]))
    for sid in ("s_00010", "s_00013", "s_00022", "s_00035", "s_00036"):
        graph = _drop_words(base, lambda w, sid=sid: w["sent"] == sid)
        g = Graph(graph)
        edp = plan(g, PODCAST)
        assert not _cuts_over(edp, g, words[sid]), (sid, _cuts_over(edp, g, words[sid]))


def test_a_removal_that_spared_speech_says_so():
    """A 2 s silence (28.35-30.35 s of the podcast) with a phrase the transcript missed in the middle of it: two removals,
    one either side, each citing the flag and naming the sound it left standing."""
    from video_ai_editor.brain.planner import plan
    graph = json.loads(GOLDEN_P2.read_text(encoding="utf-8"))
    graph["layers"]["audio"]["vad"] = sorted([*graph["layers"]["audio"]["vad"], [29.1, 29.7]])
    graph["layers"]["speech"]["flags"]["technical"] = [{"id": "x_0001", "t0": 29.1, "t1": 29.7, "why": "unheard_voice"}]
    g = Graph(graph)
    edp = plan(g, PODCAST)
    off = g.offset(g.primary)
    near = [d for d in edp["decisions"] if d["kind"] == "cut_range" and "x_0001" in d["reason"]["facts"]]
    assert len(near) == 2, [(d["ref"], d["reason"]["facts"]) for d in edp["decisions"] if d["kind"] == "cut_range"]
    pad = E.knob("keep_pad_s", 5)
    for d in near:
        (a, b), = [tuple(x) for x in d["params"]["kept_sound"]]
        assert abs(a - (29.1 + off)) < 1e-3 and abs(b - (29.7 + off)) < 1e-3
        assert d["ref"]["t1"] <= a - pad + 1e-3 or d["ref"]["t0"] >= b + pad - 1e-3, d["ref"]
    plain = plan(Graph(json.loads(GOLDEN_P2.read_text(encoding="utf-8"))), PODCAST)
    assert not [d for d in plain["decisions"] if d["kind"] == "cut_range" and d["params"].get("kept_sound")]


def test_unvoiced_parts_are_every_stretch_clear_of_the_sound():
    assert seams.unvoiced_parts(10.0, 13.0, [(11.0, 11.5)], 0.15) == [(10.0, 10.85), (11.65, 13.0)]
    assert seams.unvoiced_parts(10.0, 13.0, [(9.0, 13.5)], 0.15) == []


# --------------------------------------------------------------------------
# FX-C1: removals sit on the PROJECT's frame grid (EX-08)
# --------------------------------------------------------------------------

def _on_grid(t: float, rate: float, tol: float = 0.011) -> bool:
    return abs(t * rate - round(t * rate)) <= tol


def test_removals_and_hide_keys_are_on_the_project_grid_at_every_rate():
    """The podcast is 20 fps on disk; a 25, 29.97 or 30 fps project used to get removal edges on the FILE's
    grid — between the output's frames, so the picture landed up to half an output frame late — and at 29.97 two
    hide keys missed their seam (`their moments were cut away earlier in this plan`)."""
    from video_ai_editor.brain.planner import plan
    from video_ai_editor.edl import timebase as tb
    seen = set()
    for fps in (20.0, 25.0, 29.97, 30.0):
        g = Graph(json.loads(GOLDEN_P2.read_text(encoding="utf-8")))
        assert g.fps == 20.0                                     # what the analysis recorded: the file's rate
        g.project_fps = fps
        assert g.fps == fps
        edp = plan(g, PODCAST)
        rate = float(tb.rate_of(fps))
        cuts = [d for d in edp["decisions"] if d["kind"] == "cut_range"]
        assert len(cuts) >= 8
        for d in cuts:
            for t in (d["ref"]["t0"], d["ref"]["t1"]):
                assert _on_grid(t, rate), (fps, d["reason"]["code"], t, t * rate)
        ends = {d["ref"]["t1"] for d in cuts}
        for d in edp["decisions"]:
            if d["kind"] == "jump_cut_hide":
                one = 1.0 / rate                                       # (stated on the grid of the picture that plays there: ≤ 1 frame)
                assert any(0.0 <= d["ref"]["t0"] - e <= one + 2e-4 for e in ends), (fps, d["ref"])       # the key opens the piece the removal leaves
                assert all(k["t"] > d["ref"]["t0"] - 1e-9 for k in d["params"]["keys"])
                k0, k1 = (k["t"] for k in d["params"]["keys"])
                assert abs((k1 - k0) - E.HIDE_KEY_LAG_FRAMES / rate) < 1e-3, (fps, k0, k1)       # the hold's second key: two frames on
        seen.add(json.dumps([[d["ref"]["t0"], d["ref"]["t1"]] for d in cuts]))
    assert len(seen) >= 3, "the removals really move with the rate"


def test_the_plan_reads_the_projects_rate_from_the_session(tmp_path):
    from video_ai_editor.brain.planner.graph_view import session_project_fps
    assert session_project_fps(tmp_path) is None
    (tmp_path / "edl.json").write_text(json.dumps({"canvas": {"w": 1920, "h": 1080, "fps": 29.97002997}}), encoding="utf-8")
    assert abs(session_project_fps(tmp_path) - 29.97002997) < 1e-9
    (tmp_path / "edl.json").write_text("{not json", encoding="utf-8")
    assert session_project_fps(tmp_path) is None


def test_an_episode_opens_at_most_0_3s_before_its_first_sound():
    """UX-10: 0.87 s of dead air before the first word of the podcast. The head is a removal from the primary
    file's own first frame (before reference 0 when the camera rolled first) to 0.3 s ahead of the first sound;
    it is no seam (nothing plays before it); a reel opens where its window does."""
    from video_ai_editor.brain.planner import plan
    g = Graph(json.loads(GOLDEN_P2.read_text(encoding="utf-8")))
    edp = plan(g, PODCAST)
    head = [d for d in edp["decisions"] if d["kind"] == "cut_range" and d["ref"]["t0"] == 0.0]
    assert len(head) == 1 and head[0]["reason"]["code"] == "silence", head
    first = g.to_file(g.onset(min(w["t0"] for w in g.words)), g.primary)
    kept = first - head[0]["ref"]["t1"]
    assert E.HEAD_KEEP_S - 1.05 / 20 <= kept <= E.HEAD_KEEP_S + 2e-4, kept                # on the grid, never more than 0.3 s
    assert head[0]["params"] == {}
    from gen_brain_goldens import th_graph
    reel = plan(th_graph(), {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"})
    assert not [d for d in reel["decisions"] if d["kind"] == "cut_range" and d["ref"]["t0"] == 0.0]


# --------------------------------------------------------------------------
# FX-C1: the reel meets its length in AIR without leaving a hole (UX-11)
# --------------------------------------------------------------------------

def _holes(g: Graph, ctx) -> list[tuple[float, float, float]]:
    """(cut t0, hole, given) for every pause the reel gives air back at: the sound the viewer hears ends
    `hole` seconds before the next sound begins — what is left of the pause after the removal."""
    from video_ai_editor.brain.planner import story
    runs = g.voiced_runs
    out = []
    for c in ctx.state["cuts"]:
        if c["code"] not in story.AIR_CODES:
            continue
        prev_end = max((b for _a, b in runs if b <= c["t0"] + 1e-6), default=c["t0"])
        next_start = min((a for a, _b in runs if a >= c["t1"] - 1e-6), default=c["t1"])
        out.append((c["t0"], (c["t0"] - prev_end) + (next_start - c["t1"]), c.get("given", 0.0)))
    return out


def _reel_ctx_after_story():
    from gen_brain_goldens import th_graph
    from video_ai_editor.brain.planner import PASSES, story
    graph = th_graph()
    ctx = make_ctx(Graph(graph), {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"})
    decisions: list = []
    for p in PASSES:
        decisions = p.run(ctx.graph, ctx, decisions)
        if p is story:
            break
    return ctx.graph, ctx


def test_the_reels_air_is_spread_over_its_pauses_and_no_hole_outgrows_a_silence():
    """UX-11: the asked length was met by giving the longest pauses 0.45 s each — ~1 s holes between two
    sentences in a reel of 0.15 s pads. The air is spread over every pause that can take it, and a pause
    that gives air is never left longer than a reel's own dead-air line (what tighten itself would cut)."""
    from video_ai_editor.brain.planner import select, story
    g, ctx = _reel_ctx_after_story()
    ctx.state["cuts"] = [{k: v for k, v in c.items() if k != "given"} for c in ctx.state["cuts"]]
    story._regrid(g, ctx)
    before = select.programme_length(ctx)
    total = story._air_fit(g, ctx, round(before + 0.6, 4))
    frame = ctx.frame()
    limit = min(float(ctx.energy["min_silence_s"]), float(ctx.energy["dead_air_s"]) / 2.0)
    holes = _holes(g, ctx)
    given = [x for _t, _h, x in holes]
    assert total - before >= 0.2, (before, total)
    assert sum(1 for x in given if x > 0) >= 3, f"the air is on one or two pauses: {given}"
    assert all(h <= limit + 2 * frame for _t, h, x in holes if x > 0), [(round(t, 2), round(h, 3), x) for t, h, x in holes]
    assert all(x <= story.AIR_GIVE_MAX_S + 1e-6 for x in given), given
    # spread: a pause that gave clearly less than another had nothing more it could give
    for _t, h, x in holes:
        assert x >= max(given) - 2 * frame - 1e-6 or h + frame > limit - 1e-6 or x + frame > story.AIR_GIVE_MAX_S, (given, holes)


def test_air_is_never_bought_with_a_hole_and_the_reel_is_left_short_instead():
    """Asked for far more length than the pauses can give: every pause that gave stays under the dead-air
    line and the reel is short of it — the fit reports that (a note beyond the ±1 s), it does not open a hole."""
    from video_ai_editor.brain.planner import select, story
    g, ctx = _reel_ctx_after_story()
    ctx.state["cuts"] = [{k: v for k, v in c.items() if k != "given"} for c in ctx.state["cuts"]]
    story._regrid(g, ctx)
    before = select.programme_length(ctx)
    story._air_fit(g, ctx, before + 30.0)
    holes = _holes(g, ctx)
    assert any(x > 0 for _t, _h, x in holes)
    assert all(h <= min(float(ctx.energy["min_silence_s"]), float(ctx.energy["dead_air_s"]) / 2.0) + 2 * ctx.frame() for _t, h, x in holes if x > 0), holes
    assert select.programme_length(ctx) < before + 30.0


def test_a_removal_that_spared_speech_hands_the_reason_that_fact(monkeypatch):
    """The reason TEXT is brain/reasons.py's; the planner's part is choosing the code and handing over the fact:
    a removal that left speech standing carries `spared` (how many stretches), and a plain one carries none."""
    from video_ai_editor.brain import reasons as R
    from video_ai_editor.brain.planner import plan
    seen: list[tuple[str, dict]] = []
    real = R.reason

    def spy(code, facts, **fields):
        seen.append((code, dict(fields)))
        return real(code, facts, **fields)
    monkeypatch.setattr(R, "reason", spy)
    graph = json.loads(GOLDEN_P2.read_text(encoding="utf-8"))
    graph["layers"]["audio"]["vad"] = sorted([*graph["layers"]["audio"]["vad"], [29.1, 29.7]])
    graph["layers"]["speech"]["flags"]["technical"] = [{"id": "x_0001", "t0": 29.1, "t1": 29.7, "why": "unheard_voice"}]
    edp = plan(Graph(graph), PODCAST)
    cuts = [d for d in edp["decisions"] if d["kind"] == "cut_range"]
    spared = [d for d in cuts if d["params"].get("kept_sound")]
    assert len(spared) == 2
    marked = [f for c, f in seen if c in ("silence", "dead_air") and f.get("spared")]
    assert len(marked) == 2 and all(f["spared"] == "1" for f in marked), marked
    assert not any(f.get("spared") for c, f in seen if c in ("filler", "filler_acoustic", "false_start"))
