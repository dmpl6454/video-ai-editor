"""The reel story (spec §4.1.1): opens on the Hook Engine's top scene when it
stands alone, fits the asked duration with whole sentences, joins only where
the antecedent guard passes, and skips `open_on` when the guard fails."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import Line, simple_graph, th_graph, th_truth  # noqa: E402

from video_ai_editor.brain.planner import make_ctx, plan  # noqa: E402
from video_ai_editor.brain.planner import story  # noqa: E402
from video_ai_editor.brain.planner.graph_view import Graph  # noqa: E402

REEL = {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"}


def test_th_45s_reel_opens_on_quotable_line_within_1s():
    graph = th_graph()
    truth = th_truth(graph)
    edp = plan(graph, REEL)
    s = edp["summary"]
    assert s["project_type"] == "talking_head" and s["target"] == "reel"
    assert s["hook"] and s["hook"]["sent"] == truth["quotable"]
    assert s["story"][0]["beat"] == "hook" and s["story"][0]["sents"][0] == truth["quotable"]
    assert abs(s["duration_s"] - 45.0) <= 1.0, s["duration_s"]
    q0, q1 = truth["sentences"][truth["quotable"]]
    open_on = [d for d in edp["decisions"] if d["kind"] == "open_on"]
    assert len(open_on) == 1 and open_on[0]["reason"]["code"] == "hook_strongest_opening"
    ref = open_on[0]["ref"]
    assert q0 - 1.0 <= ref["t0"] <= q0 and ref["t1"] >= q1
    assert truth["quotable"] in open_on[0]["reason"]["facts"]
    keeps = [d for d in edp["decisions"] if d["kind"] == "keep_window"]
    assert keeps and all(d["reason"]["code"] in ("best_window", "duration_fit") for d in keeps)
    # an editor keeps the LAST take: the earlier take of the retake never survives into a reel, the retake does
    first0, first1 = truth["sentences"][truth["retake_of"]]
    later0, later1 = truth["sentences"][truth["retake_dup"]]
    assert not any(k["ref"]["t0"] <= first0 + 0.05 and k["ref"]["t1"] >= first1 - 0.05 for k in keeps)
    assert any(k["ref"]["t0"] <= later0 + 0.05 and k["ref"]["t1"] >= later1 - 0.05 for k in keeps)
    # the last kept sentence is complete
    last = s["story"][-1]["sents"][-1]
    assert next(x for x in graph["layers"]["speech"]["sentences"] if x["id"] == last)["complete"]


def test_join_ok_rejects_anaphora_after_a_dropped_scene():
    g = Graph(simple_graph([
        Line("The lens costs a lot of money."),
        Line("That is why we tested it for a year."),                    # anaphora on the line before
        Line("We shot for a week in the rain."),
        Line("And the coating never flared once."),                       # conjunction start
    ]))
    a, b, c, d = g.sentences
    assert story.join_ok(a, b, g) is True                # source-adjacent: the antecedent is there
    assert story.join_ok(c, b, g) is False               # the scene before was dropped: "that" points at nothing
    assert story.join_ok(a, c, g) is True                # a standalone sentence joins anything
    assert story.join_ok(b, d, g) is False               # "And …" after a non-adjacent predecessor
    assert story.join_ok(c, d, g) is True
    assert story.join_ok(None, b, g) is False            # nothing can precede an anaphora at the very front


def test_open_on_skipped_when_guard_fails():
    # The sentence AFTER the hook's original position leans on the hook ("That …");
    # moving the hook would leave it next to a scene it does not follow.
    g_dict = simple_graph([
        Line("Hey everyone, welcome back to the channel."),
        Line("Most of that year we spent returning gear that did not survive."),
        Line("The cheapest lens here beats the ten thousand dollar one.", claim=True, strong_number=True, contrast=1, emotion=0.4),
        Line("That single fact changed how we buy."),
        Line("We shot the whole season on it and nothing broke.", claim=True, topic_peak=True),
        Line("So buy the glass, not the body.", conclusion=True, claim=True),
    ])
    edp = plan(g_dict, {"duration_s": 20.0, "platform": "reels"})
    assert not [d for d in edp["decisions"] if d["kind"] == "open_on"]
    assert any("open_on" in n["asked"] or "chronolog" in n["why"] for n in edp["summary"]["deferred"]) or \
        edp["summary"]["story"][0]["beat"] != "hook"


def test_duration_fit_reports_the_shortfall_honestly():
    g_dict = simple_graph([Line("One long sentence about lenses that runs for a while and then stops.", claim=True)])
    edp = plan(g_dict, {"duration_s": 45.0, "platform": "reels"})
    assert edp["summary"]["duration_s"] < 45.0
    assert any(n["asked"].startswith("45") for n in edp["summary"]["deferred"]), edp["summary"]["deferred"]


# --------------------------------------------------------------------------
# EB1 integration: the asked length to the frame, in air
# --------------------------------------------------------------------------

def _programme(edp: dict) -> float:
    """The programme's length from the decisions alone: kept windows minus the removals inside them."""
    keeps = [(d["ref"]["t0"], d["ref"]["t1"]) for d in edp["decisions"] if d["kind"] == "keep_window"]
    cuts = [(d["ref"]["t0"], d["ref"]["t1"]) for d in edp["decisions"] if d["kind"] == "cut_range"]
    total = sum(b - a for a, b in keeps)
    for c0, c1 in cuts:
        total -= sum(max(0.0, min(c1, b) - max(c0, a)) for a, b in keeps)
    return total


def test_the_reel_is_fitted_to_the_asked_length_in_air():
    from video_ai_editor.brain.planner import story as S
    graph = th_graph()
    for asked in (45.0, 40.0, 30.0):
        edp = plan(graph, {**REEL, "duration_s": asked})
        length = edp["summary"]["duration_s"]
        assert abs(length - asked) <= 1.0 or any(d["asked"] == f"{asked:g} s" for d in edp["summary"]["deferred"])
        air = [d for d in edp["decisions"] if d["kind"] == "cut_range" and d["reason"]["code"] in S.AIR_CODES]
        if abs(length - asked) > S.AIR_FIT_TOL_S + 1e-6:
            # short of it only when every pause has given all it may
            assert length < asked and air, (asked, length)
            # (a pause also stops giving when what is left of it would exceed S.hole_limit: FX-C1's UX-11 cap,
            # measured here on the reference clock exactly as the planner measures it)
            gv = Graph(graph)
            limit = S.hole_limit(make_ctx(gv, {**REEL, "duration_s": asked}))
            for d in air:
                cut = {"t0": gv.to_ref(d["ref"]["t0"], gv.primary), "t1": gv.to_ref(d["ref"]["t1"], gv.primary)}
                spent = d["params"].get("air_given_s", 0.0) + 1 / 30 > S.AIR_GIVE_MAX_S
                full = S._hole(gv.voiced_runs, cut) + 1 / 30 > limit + 1e-6
                assert spent or full or d["ref"]["t1"] - d["ref"]["t0"] - 1 / 30 < S.AIR_CUT_MIN_S, (asked, length, d["ref"], d["params"])
            reached_45 = False
        else:
            reached_45 = asked == 45.0
        assert reached_45 or asked != 45.0, "the demo's 45 s must be met"
        # the summary's length IS what the decisions add up to: nothing is left to the tools' rounding
        assert abs(_programme(edp) - length) < 2e-3, (_programme(edp), length)
        for d in edp["decisions"]:
            if d["kind"] in ("keep_window", "cut_range", "open_on"):
                for t in (d["ref"]["t0"], d["ref"]["t1"]):
                    assert abs(t * 30 - round(t * 30)) < 0.01, (d["kind"], t)
        given = [d["params"]["air_given_s"] for d in edp["decisions"] if d["kind"] == "cut_range" and d["params"].get("air_given_s")]
        assert all(-1 / 30 - 1e-6 <= x <= S.AIR_GIVE_MAX_S + 1e-6 for x in given), given
        for d in edp["decisions"]:
            if d["kind"] == "cut_range" and d["params"].get("air_given_s", 0) > 0:
                assert d["reason"]["code"] in S.AIR_CODES and d["ref"]["t1"] - d["ref"]["t0"] >= S.AIR_CUT_MIN_S - 1e-6


def test_the_hook_moves_whole_between_its_neighbouring_seams():
    """No sliver of air is left where the hook was: its span opens where the
    removal before it ends and closes where the next begins."""
    edp = plan(th_graph(), REEL)
    hook = next(d for d in edp["decisions"] if d["kind"] == "open_on")["ref"]
    edges = sorted(t for d in edp["decisions"] if d["kind"] in ("cut_range", "keep_window") for t in (d["ref"]["t0"], d["ref"]["t1"]))
    for t in (hook["t0"], hook["t1"]):
        near = [e for e in edges if 1e-3 < abs(e - t) < 0.5]
        assert not near, (t, near)
    assert edp["summary"]["hook"]["t0"] == hook["t0"] and edp["summary"]["hook"]["t1"] == hook["t1"]


def test_who_ranked_the_moments_is_on_the_edp_and_nothing_else_moves():
    graph = th_graph()
    plain = plan(graph, REEL)
    assert plain["content_brain"] is None and {d["by"] for d in plain["decisions"]} == {"recipes"}
    hook = plain["summary"]["hook"]["sent"]
    ranked = th_graph()
    ranked["layers"]["semantic"]["annotations"] = [
        {"sent": hook, "by": "apple_intelligence", "model": "apple-fm", "task": "rank_moments",
         "prompt_hash": "sha256:0123456789abcdef", "at": 1, "hook": 1.0}]
    live = plan(ranked, REEL)
    assert live["content_brain"] == "apple_intelligence"
    by = {d["reason"]["code"]: d["by"] for d in live["decisions"]}
    assert by["hook_strongest_opening"] == by["hook_emphasis"] == "apple_intelligence"
    assert {d["by"] for d in live["decisions"] if not d["reason"]["code"].startswith("hook_")} == {"recipes"}
    strip = lambda e: [{k: v for k, v in d.items() if k != "by"} for d in e["decisions"]]          # noqa: E731
    assert strip(live) == strip(plain) and live["summary"] == plain["summary"]
    # an annotation that carries no answer (a model that timed out) changes nothing at all
    silent = th_graph()
    silent["layers"]["semantic"]["annotations"] = [{"sent": hook, "by": "apple_intelligence", "task": "rank_moments",
                                                    "prompt_hash": "sha256:0123456789abcdef", "at": 1, "hook": None}]
    assert plan(silent, REEL)["decisions"] == plain["decisions"] and plan(silent, REEL)["content_brain"] is None


# --------------------------------------------------------------------------
# UX-15: an editor keeps the LAST take
# --------------------------------------------------------------------------

EPISODE = {"content_type": "talking_head"}


def _span(edp: dict, sid: str, graph: dict) -> tuple[float, float]:
    s = next(x for x in graph["layers"]["speech"]["sentences"] if x["id"] == sid)
    return s["t0"], s["t1"]


def _covers(d: dict, a: float, b: float, tol: float = 0.4) -> bool:
    return d["ref"]["t0"] <= a + tol and d["ref"]["t1"] >= b - tol


def test_the_episode_drops_the_earlier_take_and_names_the_retake_that_stays():
    graph = th_graph()
    truth = th_truth(graph)
    edp = plan(graph, EPISODE)
    first, later = _span(edp, truth["retake_of"], graph), _span(edp, truth["retake_dup"], graph)
    repeats = [d for d in edp["decisions"] if d["kind"] == "cut_range" and d["reason"]["code"] == "repeat"]
    assert len(repeats) == 1
    assert _covers(repeats[0], *first), (repeats[0]["ref"], first)
    assert not (repeats[0]["ref"]["t1"] > later[0] + 0.5), "the retake is not cut"
    text = repeats[0]["reason"]["text"]
    assert "earlier take" in text and f"the retake at {int(later[0] // 60)}:{int(later[0] % 60):02d} stays" in text, text


def test_the_reel_keeps_the_retake_and_says_the_earlier_take_went():
    graph = th_graph()
    truth = th_truth(graph)
    edp = plan(graph, REEL)
    first, later = _span(edp, truth["retake_of"], graph), _span(edp, truth["retake_dup"], graph)
    keeps = [d for d in edp["decisions"] if d["kind"] == "keep_window"]
    assert not any(_covers(k, *first, tol=0.05) for k in keeps)
    holder = next(k for k in keeps if _covers(k, *later, tol=0.05))
    assert "earlier take" in holder["reason"]["text"] and "retake" in holder["reason"]["text"], holder["reason"]["text"]
    assert not any(d["kind"] == "cut_range" and d["reason"]["code"] == "repeat" for d in edp["decisions"])


def test_a_flatter_retake_does_not_replace_a_better_first_take():
    graph = th_graph()
    truth = th_truth(graph)
    graph["layers"]["semantic"]["scores"][truth["retake_of"]]["emotion"] = 0.95
    graph["layers"]["semantic"]["scores"][truth["retake_dup"]]["emotion"] = 0.0
    edp = plan(graph, EPISODE)
    first, later = _span(edp, truth["retake_of"], graph), _span(edp, truth["retake_dup"], graph)
    repeats = [d for d in edp["decisions"] if d["kind"] == "cut_range" and d["reason"]["code"] == "repeat"]
    assert len(repeats) == 1 and _covers(repeats[0], *later), (repeats[0]["ref"], later)
    assert not (repeats[0]["ref"]["t0"] < first[1] - 0.5), "the better first take is not cut"


def test_the_callers_graph_is_left_as_it_was():
    graph = th_graph()
    before = [dict(r) for r in graph["layers"]["speech"]["flags"]["repeats"]]
    plan(graph, EPISODE)
    plan(graph, REEL)
    assert graph["layers"]["speech"]["flags"]["repeats"] == before


# --------------------------------------------------------------------------
# UX-09 / UX-03: the captions are the words the plan keeps, every speaker's, each once
# --------------------------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402

GRAPHS_A = Path(__file__).parent / "goldens" / "brain" / "graphs"
FILLER_TOKENS = {"um", "umm", "uh", "er", "erm"}


def _cues_of(edp: dict) -> list[dict]:
    return next(d for d in edp["decisions"] if d["kind"] == "captions")["params"]["cues"]


def _kept_spans(edp: dict, offset: float) -> list[tuple[float, float]]:
    """The reference-clock spans the plan plays, from the DECISIONS alone (a
    reel's kept windows, else everything, minus every removal)."""
    keeps = [(d["ref"]["t0"] - offset, d["ref"]["t1"] - offset) for d in edp["decisions"] if d["kind"] == "keep_window"]
    cuts = sorted((d["ref"]["t0"] - offset, d["ref"]["t1"] - offset) for d in edp["decisions"] if d["kind"] == "cut_range")
    spans = keeps or [(0.0, 1e9)]
    out = []
    for a, b in spans:
        cur = a
        for c0, c1 in cuts:
            if c1 <= cur or c0 >= b:
                continue
            if c0 > cur:
                out.append((cur, c0))
            cur = max(cur, c1)
        if cur < b:
            out.append((cur, b))
    return out


def _mid(w: dict) -> float:
    return 0.5 * (w["t0"] + w["t1"])


def _unread(graph: dict, w: dict) -> bool:
    """Nobody captions a backchannel (a turn under 0.6 s — what ASR mishears as "An image, em." — the camera rule's
    own test) nor a word under no turn at all; with no turns nothing is skipped."""
    turns = graph["layers"]["speech"]["turns"]
    if not turns:
        return False
    mid = _mid(w)
    under = [t["t1"] - t["t0"] < 0.6 for t in turns if t["t0"] - 0.15 <= mid <= t["t1"] + 0.15]
    return not under or all(under)


def _kept_words(graph: dict, spans: list[tuple[float, float]]) -> list[dict]:
    words = sorted(graph["layers"]["speech"]["words"], key=lambda w: (w["t0"], w["t1"]))
    return [w for w in words if not w.get("filler") and any(a - 1e-6 <= _mid(w) <= b + 1e-6 for a, b in spans)
            and not _unread(graph, w)]


def _th_a_graph() -> dict:
    p = GRAPHS_A / "talking_head.json"
    if not p.exists():
        pytest.skip("lane A's talking-head golden graph is not built")
    return json.loads(p.read_text(encoding="utf-8"))


def test_the_reel_captions_are_the_kept_words_each_once_and_no_filler():
    graph = _th_a_graph()
    edp = plan(graph, REEL)
    cues = _cues_of(edp)
    spans = _kept_spans(edp, 0.0)
    words = _kept_words(graph, spans)
    got = " ".join(c["text"] for c in cues).split()
    assert got == [w["text"].strip() for w in words], "every kept word captioned once, nothing else"
    assert not {t.strip(".,?!—").lower() for t in got} & FILLER_TOKENS, "no removed filler on screen"
    # nothing removed is ever shown: a cue never overlaps a removal and never crosses a seam
    cuts = [(d["ref"]["t0"], d["ref"]["t1"]) for d in edp["decisions"] if d["kind"] == "cut_range"]
    for c in cues:
        assert not any(min(c["t1"], b) - max(c["t0"], a) > 1e-3 for a, b in cuts), c
        assert sum(1 for a, b in spans if a - 1e-3 <= c["t0"] and c["t1"] <= b + 1e-3) == 1, ("crosses a seam", c)


def test_a_cue_starts_and_ends_with_its_own_words():
    graph = _th_a_graph()
    edp = plan(graph, REEL)
    cues = _cues_of(edp)
    words = _kept_words(graph, _kept_spans(edp, 0.0))
    i = 0
    for c in cues:
        n = len(c["text"].split())
        first, last = words[i], words[i + n - 1]
        i += n
        assert c["t0"] >= first["t0"] - 1e-3 and c["t0"] <= first["t0"] + 0.15, (c, first)     # its first word starts it
        short = max(0.0, 0.4 - (last["t1"] - first["t0"]))
        assert last["t1"] - 1e-3 <= c["t1"] <= last["t1"] + short + 1e-3, ("runs late", c, last)  # only a too-short cue lingers
    assert i == len(words)
    assert all(a["t1"] <= b["t0"] + 1e-3 for a, b in zip(cues, cues[1:]) if a["spk"] == b["spk"] or True)


def test_the_sentence_after_the_cut_is_captioned_from_its_first_word():
    """The reel's 'Because it forces a choice…' was never captioned and the
    line before it ran on: truth from the fixture, cues from the plan."""
    graph = _th_a_graph()
    try:
        from brain_fixtures import build_brain_fixtures
        truth = build_brain_fixtures().th.truth
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"the brain fixtures are not built: {e}")
    edp = plan(graph, REEL)
    cues = _cues_of(edp)
    spans = _kept_spans(edp, 0.0)
    for s in truth.sentences:
        inside = [(a, b) for a, b in spans if a - 1e-6 <= 0.5 * (s.t0 + s.t1) <= b + 1e-6]
        if not inside:
            continue
        mine = [c for c in cues if s.t0 - 0.35 <= c["t0"] <= s.t1 + 0.35]
        assert mine, f"{s.id} plays but is never captioned"
        assert mine[0]["text"].split()[0].lower().strip(",.?!") == s.text.split()[0].lower().strip(",.?!"), (s.id, mine[0])
        assert mine[-1]["t1"] <= s.t1 + 0.4 + 1e-3, ("a cue outlives its sentence", s.id, mine[-1])


def _speaker_coverage(graph: dict, edp: dict) -> dict[str, float]:
    spans = _kept_spans(edp, 0.0)
    cues = _cues_of(edp)
    out: dict[str, float] = {}
    for spk in sorted({w["spk"] for w in graph["layers"]["speech"]["words"]}):
        words = [w for w in _kept_words(graph, spans) if w["spk"] == spk]
        total = sum(w["t1"] - w["t0"] for w in words)
        under = sum(max(0.0, min(w["t1"], c["t1"]) - max(w["t0"], c["t0"])) for w in words for c in cues)
        out[spk] = under / total if total else 0.0
    return out


@pytest.mark.parametrize("golden", ["two_cam_podcast"])
def test_the_podcast_captions_cover_every_speaker(golden):
    from gen_brain_goldens import p2_graph
    graphs = [("synthetic", p2_graph())]
    p = GRAPHS_A / f"{golden}.json"
    if p.exists():
        graphs.append(("lane A", json.loads(p.read_text(encoding="utf-8"))))
    for name, graph in graphs:
        edp = plan(graph, {"content_type": "podcast"})
        cover = _speaker_coverage(graph, edp)
        assert set(cover) >= {"S1", "S2"}, (name, cover)
        assert all(v >= 0.9 for v in cover.values()), (name, cover)
        cues = _cues_of(edp)
        assert {c["spk"] for c in cues} >= {"S1", "S2"}, "the guest is captioned as well as the host"
        assert max(len(line) for c in cues for line in c["text"].split("\n")) <= 32, "podcast lines are at most 32 characters"
