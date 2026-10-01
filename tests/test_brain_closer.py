"""EB1 closer, planner side: two cameras and no recorder, word edges on the frame grid, slivers, captions, Plan-tab wording.

Each test failed on the tree that reached the closer (the failing output is in the closer's report)."""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import P2_A_KEY, P2_A_PATH, P2_B_KEY, P2_B_PATH, P2_REC_KEY, P2_REC_PATH, p2_graph  # noqa: E402

from video_ai_editor.agent.prompt.facts import ClipFact, TimelineFacts  # noqa: E402
from video_ai_editor.brain import compile as C  # noqa: E402
from video_ai_editor.brain.planner import plan  # noqa: E402
from video_ai_editor.brain.planner.graph_view import Graph  # noqa: E402

PODCAST = {"content_type": "podcast"}



def no_recorder_p2(*, hints: bool = True) -> dict:
    """The P2 graph as the analysis writes it for two cameras and NO recorder: camera A is the reference
    (its own clock), so angles.json lists only camera B as a member."""
    g = copy.deepcopy(p2_graph())
    sources = [s for s in g["graph"]["sources"] if s["key"] != P2_REC_KEY]
    for s in sources:
        if s["key"] == P2_A_KEY:
            s["sync_offset_s"] = 0.0
        if s["key"] == P2_B_KEY:
            s["sync_offset_s"] = -0.55
    g["graph"]["sources"] = sources
    g["graph"]["reference"] = P2_A_KEY
    b = next(m for m in g["angles"]["members"] if m["src_key"] == P2_B_KEY)
    b["sync_offset_s"] = -0.55
    g["angles"] = {"reference": P2_A_KEY, "dialogue": P2_A_KEY, "members": [b]}
    if not hints:
        for sp in g["graph"]["speakers"]:
            sp["angle_hint"] = None
    return g


def _facts_v1(names: list[str], duration: float = 340.0) -> TimelineFacts:
    from video_ai_editor.agent.prompt.presets import music_beds
    beds = {str(b.path) for b in music_beds()}
    base = TimelineFacts.minimal(duration=duration, allowed_paths={P2_A_PATH, P2_B_PATH} | beds, has_transcript=True,
                                 words=200, speech_seconds=duration * 0.8, tools_available=set())
    clips = [ClipFact(id=f"c_{i:08x}", track="v1", start=i * 100.0, duration=100.0, src_in=0.0, src_out=100.0, name=n)
             for i, n in enumerate(names)]
    return base.model_copy(update={"clips": clips})


# --------------------------------------------------------------------------
# NEW HIGH: two cameras and no recorder -> Graph.primary was the guest camera
# --------------------------------------------------------------------------

def test_the_reference_angle_is_a_member_and_the_primary():
    g = Graph(no_recorder_p2())
    assert [m["angle"] for m in g.members] == ["A", "B"]
    assert g.primary == P2_A_KEY
    assert g.offset(P2_A_KEY) == 0.0 and g.offset(P2_B_KEY) == -0.55
    assert g.dialogue_key == P2_A_KEY


def test_the_primary_angle_is_the_one_that_carries_the_dialogue_not_the_first_in_the_list():
    """Decision (2): with no recorder the reference camera is the one whose own file the transcript was read from.
    When THAT is camera B (A sorts first, A is the member listed), the primary is still B."""
    g = no_recorder_p2()
    for s in g["graph"]["sources"]:
        s["sync_offset_s"] = {P2_A_KEY: 0.55, P2_B_KEY: 0.0}[s["key"]]
    a = {"angle": "A", "src_key": P2_A_KEY, "path": P2_A_PATH, "sync_offset_s": 0.55, "sees": []}
    g["graph"]["reference"] = P2_B_KEY
    g["angles"] = {"reference": P2_B_KEY, "dialogue": P2_B_KEY, "members": [a]}
    view = Graph(g)
    assert [m["angle"] for m in view.members] == ["A", "B"]
    assert view.primary == P2_B_KEY and view.dialogue_key == P2_B_KEY


def test_two_cameras_and_no_recorder_get_a_camera_plan_and_camera_b_leaves_the_main_lane():
    graph = no_recorder_p2()
    edp = plan(graph, PODCAST)
    assert [d for d in edp["decisions"] if d["kind"] == "switch_angle"], "no camera plan for two cameras"
    out = C.compile_edp(edp, _facts_v1([Path(P2_A_PATH).stem, Path(P2_B_PATH).stem]), graph)
    tools = [s.tool for s in out.steps]
    assert "apply_camera_plan" in tools
    off = next(s for s in out.steps if isinstance(s.args.get("ranges"), list))
    assert {r["src"] for r in off.args["ranges"]} == {P2_B_PATH}
    assert "Camera B" in off.why
    dialogue = next(s for s in out.steps if s.tool == "sync_dialogue_lane")
    assert dialogue.args["src"] == P2_A_PATH and set(dialogue.args["offsets"]) == {P2_A_PATH, P2_B_PATH}


def test_two_cameras_with_no_way_to_tell_who_is_on_which_say_so_and_keep_camera_a_only():
    graph = no_recorder_p2(hints=False)
    edp = plan(graph, PODCAST)
    assert not [d for d in edp["decisions"] if d["kind"] == "switch_angle"]
    asked = [d["asked"] for d in edp["summary"]["deferred"]]
    assert "camera changes" in asked, asked
    why = next(d["why"] for d in edp["summary"]["deferred"] if d["asked"] == "camera changes")
    assert "which camera" in why and "premium_podcast" not in why
    out = C.compile_edp(edp, _facts_v1([Path(P2_A_PATH).stem, Path(P2_B_PATH).stem]), graph)
    assert "apply_camera_plan" not in [s.tool for s in out.steps]
    off = next(s for s in out.steps if isinstance(s.args.get("ranges"), list))
    assert {r["src"] for r in off.args["ranges"]} == {P2_B_PATH}


def test_the_analysis_lists_the_reference_angle_among_the_members():
    from video_ai_editor.brain import graph_scenes as GS
    ref = {"key": P2_A_KEY, "angle": "A", "path": P2_A_PATH, "sync_offset_s": 0.0, "role": "angle", "has_video": True, "sees": ["S1"]}
    b = {"key": P2_B_KEY, "angle": "B", "path": P2_B_PATH, "sync_offset_s": -0.55, "sees": [],
         "sync": {"confidence": 1.0, "engine": "fft_xcorr"}}
    out = GS.angles_json(ref, [b])
    assert [m["angle"] for m in out["members"]] == ["A", "B"]
    assert out["members"][0]["sync_offset_s"] == 0.0 and out["members"][0]["src_key"] == P2_A_KEY
    # a recorder is the reference: only the cameras are members; one camera alone is not a group
    rec = {"key": P2_REC_KEY, "role": "reference_audio", "has_video": False, "path": P2_REC_PATH}
    assert [m["angle"] for m in GS.angles_json(rec, [b])["members"]] == ["B"]
    assert GS.angles_json(ref, [])["members"] == []


# --------------------------------------------------------------------------
# the last line: no moment plays twice from two cameras
# --------------------------------------------------------------------------

def _store_with_v1(tmp_path, pieces):
    """A store whose v1 holds `(file, in, out)` pieces (in-memory layout)."""
    import prompt_fixtures as F
    from video_ai_editor import config
    from video_ai_editor.edl.schema import Clip
    config.WORKDIR = tmp_path
    store = F.make_store(tmp_path, name="s_closer")
    track = store.edl.get_track("v1")
    track.clips, t = [], 0.0
    for i, (f, a, b) in enumerate(pieces):
        track.clips.append(Clip(id=f"c_t{i:03d}", src=f, in_=a, out=b, start=t))
        t += b - a
    return store


def _brain_plan(offsets):
    import prompt_fixtures as F
    from video_ai_editor.agent.prompt.schema import Step
    return F.plan_of(Step(tool="sync_dialogue_lane", args={"src": P2_A_PATH, "lane": "a1", "offsets": offsets,
                                                           "plan_ref": "d_0badf00d"}, why="x"))


@pytest.mark.parametrize("pieces, bad", [
    ([(P2_A_PATH, 0, 100), (P2_B_PATH, 0, 100)], True),                 # the whole conversation, twice
    ([(P2_A_PATH, 0, 50), (P2_B_PATH, 0, 100)], True),                  # B's first half repeats what A played
    ([(P2_A_PATH, 0, 50), (P2_B_PATH, 49.45, 100), (P2_A_PATH, 100.55, 130)], False),   # cameras taking turns
    ([(P2_A_PATH, 0, 100)], False),
])
def test_a_second_camera_left_on_the_main_lane_blocks_the_edit(tmp_path, pieces, bad):
    from types import SimpleNamespace
    from video_ai_editor.brain import checks as BC
    store = _store_with_v1(tmp_path, pieces)
    plan = _brain_plan({P2_A_PATH: 0.0, P2_B_PATH: -0.55})
    reasons = BC.moment_played_twice(store, plan, SimpleNamespace(edl_before=store.edl))
    assert bool(reasons) is bad, reasons
    if bad:
        assert reasons[0]["clause"] == "one_moment_once" and "play twice" in reasons[0]["message"]


def test_the_check_ignores_a_plan_that_is_not_a_brain_plan(tmp_path):
    import prompt_fixtures as F
    from types import SimpleNamespace
    from video_ai_editor.agent.prompt.schema import Step
    from video_ai_editor.brain import checks as BC
    store = _store_with_v1(tmp_path, [(P2_A_PATH, 0, 100), (P2_B_PATH, 0, 100)])
    plan = F.plan_of(Step(tool="sync_dialogue_lane", args={"src": P2_A_PATH, "lane": "a1",
                                                           "offsets": {P2_A_PATH: 0.0, P2_B_PATH: 0.0}}, why="x"))
    assert BC.moment_played_twice(store, plan, SimpleNamespace(edl_before=store.edl)) == []


def test_the_safety_net_rolls_back_a_brain_run_that_leaves_the_second_camera_on_v1(tmp_path):
    from types import SimpleNamespace
    from video_ai_editor.agent.prompt import executor
    from video_ai_editor.agent.prompt.facts import build_facts
    store = _store_with_v1(tmp_path, [(P2_A_PATH, 0, 100), (P2_B_PATH, 0, 100)])
    before = store.edl.model_copy(deep=True)
    store.edl.get_track("v1").clips[0].out = 90.0            # the run changed something: the net looks at the tree
    plan = _brain_plan({P2_A_PATH: 0.0, P2_B_PATH: -0.55})
    result = SimpleNamespace(edl_before=before, steps=[])
    reasons = executor.safety_net(store, plan, result, build_facts(store, None), "tighten this podcast")
    assert any(r.get("clause") == "one_moment_once" for r in reasons), reasons


# --------------------------------------------------------------------------
# a keep window never opens or closes inside a word
# --------------------------------------------------------------------------

def _filler_led_graph() -> dict:
    """Sentences whose text begins with a filler while the sentence's own span starts at the first REAL word
    (as the analysis stores them: s_0443 'Um, nobody needs…' t0 = 2507.782, the end of 'Um,')."""
    from gen_brain_goldens import Line, simple_graph
    g = simple_graph([
        Line("The first sentence has some plain words in it, and it ends here.", pause_after=1.2),
        Line("Um, the cheapest lens here outperforms the expensive one by a wide margin.", fillers=(0,), claim=True,
             strong_number=True, contrast=1, pause_after=1.2),
        Line("And then we go on for a while with plain words that are fine.", pause_after=1.2),
        Line("Um, this is a second sentence with a filler at its head and more words.", fillers=(0,), pause_after=1.2),
        Line("A last plain sentence closes the recording with a few more words.", pause_after=0.5),
    ])
    words = g["layers"]["speech"]["words"]
    for s in g["layers"]["speech"]["sentences"]:
        own = [w for w in words if w["sent"] == s["id"]]
        real = next(w for w in own if not w.get("filler"))
        if own[0].get("filler"):
            real["t0"] = own[0]["t1"]                   # nothing between the filler and the word after it
        s["t0"] = real["t0"]
    return g


def _edges_inside_words(edp: dict, graph: dict) -> list[tuple[str, float, str]]:
    g = Graph(graph)
    bad = []
    for d in edp["decisions"]:
        if d["kind"] != "keep_window":
            continue
        for t_file in (d["ref"]["t0"], d["ref"]["t1"]):
            t = g.to_ref(t_file, g.primary)
            bad += [(d["id"], t, w["text"]) for w in g.words if w["t0"] + 0.02 < t < w["t1"] - 0.02]
    return bad


@pytest.mark.parametrize("asked", [8.0, 12.0, 16.0, 22.0])
def test_a_reel_window_never_opens_or_closes_inside_a_word(asked):
    graph = _filler_led_graph()
    edp = plan(graph, {"duration_s": asked, "platform": "reels", "ratio": "9:16"})
    assert [d for d in edp["decisions"] if d["kind"] == "keep_window"]
    assert _edges_inside_words(edp, graph) == []


def test_the_window_that_opens_on_a_filler_led_sentence_starts_after_the_filler():
    graph = _filler_led_graph()
    words = {w["id"]: w for w in graph["layers"]["speech"]["words"]}
    filler = next(w for w in words.values() if w.get("filler"))
    edp = plan(graph, {"duration_s": 12.0, "platform": "reels", "ratio": "9:16"})
    starts = [d["ref"]["t0"] for d in edp["decisions"] if d["kind"] == "keep_window"]
    assert not any(filler["t0"] + 0.02 < t < filler["t1"] - 0.02 for t in starts), (starts, filler)


# --------------------------------------------------------------------------
# no sliver of the other camera beside a cut
# --------------------------------------------------------------------------

def _sliver_ctx(fps: float = 30.0):
    from video_ai_editor.brain.planner import camera, make_ctx
    graph = p2_graph()
    ctx = make_ctx(graph, PODCAST)
    ctx.fps = fps
    return camera, Graph(graph), ctx


def _quiet_stretch(g: Graph, ctx, start: float = 30.0) -> float:
    """The start of a silence of the graph that is 1 s long at least (no word under any edge a test puts there)."""
    return next(s["t0"] for s in g.silences if s["t0"] > start and s["t1"] - s["t0"] >= 1.0)


def test_a_camera_change_a_hair_after_a_cut_takes_the_cut_to_it():
    camera, g, ctx = _sliver_ctx()
    t = _quiet_stretch(g, ctx) + 0.1                       # reference seconds, as the planner's cuts and switches are
    cuts = [{"t0": t, "t1": t + 0.35, "code": "silence", "facts": ["sil_1"]}]
    sw = camera._Switch(t=t + 0.4, angle=P2_A_KEY, code="speaker_turn", facts=["t_1"])
    assert camera._close_slivers(g, ctx, cuts, [sw]) is True
    # the kept piece between the cut and the change is gone: the cut now ends ON the change (frame grid, never before it)
    assert t + 0.4 - 1e-3 <= cuts[0]["t1"] <= t + 0.4 + 1 / 30 and cuts[0]["t0"] == t


def test_a_camera_change_a_hair_before_a_cut_takes_the_cut_back_to_it():
    camera, g, ctx = _sliver_ctx()
    t = _quiet_stretch(g, ctx) + 0.1                       # reference seconds, as the planner's cuts and switches are
    cuts = [{"t0": t + 0.05, "t1": t + 0.5, "code": "silence", "facts": ["sil_1"]}]
    sw = camera._Switch(t=t, angle=P2_B_KEY, code="speaker_turn", facts=["t_1"])
    assert camera._close_slivers(g, ctx, cuts, [sw]) is True
    assert cuts[0]["t0"] <= t + 1e-3 and cuts[0]["t1"] == t + 0.5


def test_a_camera_change_inside_a_word_does_not_take_the_cut_into_the_word():
    """The change is 0.1 s into a spoken word: the removal reaches the word's start at the most, never into it."""
    camera, g, ctx = _sliver_ctx()
    word = next(w for w in g.words if not w.get("filler") and w["t0"] > 30 and 0.3 < w["t1"] - w["t0"] < 0.8)
    cuts = [{"t0": word["t0"] - 0.5, "t1": word["t0"] - 0.04, "code": "silence", "facts": ["sil_1"]}]
    sw = camera._Switch(t=word["t0"] + 0.1, angle=P2_A_KEY, code="speaker_turn", facts=["t_1"])
    camera._close_slivers(g, ctx, cuts, [sw])
    assert cuts[0]["t1"] <= word["t0"] + 0.02 and g.word_straddling(cuts[0]["t1"]) is None, cuts


def test_a_change_far_from_every_cut_moves_nothing():
    camera, g, ctx = _sliver_ctx()
    cuts = [{"t0": 20.0, "t1": 20.5, "code": "silence", "facts": ["sil_1"]}]
    sw = camera._Switch(t=30.0, angle=P2_A_KEY, code="speaker_turn", facts=["t_1"])
    assert camera._close_slivers(g, ctx, cuts, [sw]) is False and cuts[0]["t0"] == 20.0 and cuts[0]["t1"] == 20.5


def test_the_planned_programme_has_no_piece_shorter_than_the_minimum_piece():
    """The podcast's kept pieces (the complement of the removals, split at every camera change)."""
    from video_ai_editor.brain import energy as E
    graph = p2_graph()
    for fps in (20.0, 24.0, 25.0, 29.97, 30.0, 60.0):
        g = Graph(graph)
        g.project_fps = fps
        edp = plan(g, PODCAST)
        cuts = sorted((d["ref"]["t0"], d["ref"]["t1"]) for d in edp["decisions"] if d["kind"] == "cut_range")
        sw = sorted({d["ref"][k] for d in edp["decisions"] if d["kind"] == "switch_angle" for k in ("t0", "t1")})
        cur, kept = 0.0, []
        for a, b in cuts:
            if a > cur:
                kept.append((cur, a))
            cur = max(cur, b)
        kept.append((cur, g.duration_of(g.primary)))
        for a, b in kept:
            pts = [a] + [t for t in sw if a + 1e-6 < t < b - 1e-6] + [b]
            assert all(y - x >= E.MIN_PIECE_S for x, y in zip(pts, pts[1:])), (fps, [(x, round(y - x, 3)) for x, y in zip(pts, pts[1:])])


# --------------------------------------------------------------------------
# a reel from a project below any platform's rate is planned on the rate the preset conforms it to
# --------------------------------------------------------------------------

REEL = {"duration_s": 20.0, "platform": "reels", "ratio": "9:16"}


def test_the_preset_says_what_rate_it_leaves_the_project_at():
    from video_ai_editor.agent.dispatch import conformed_fps
    assert conformed_fps(20, "reels") == 30 and conformed_fps(15, "youtube_16x9") == 30 and conformed_fps(120, "tiktok") == 30
    for fps in (23.976, 24, 25, 29.97, 30, 50, 60):
        assert conformed_fps(fps, "reels") == fps
    assert conformed_fps(20, "not_a_preset") == 20


def _cut_edges(edp: dict) -> list[float]:
    return [t for d in edp["decisions"] if d["kind"] in ("cut_range", "keep_window") for t in (d["ref"]["t0"], d["ref"]["t1"])]


def _on_grid(t: float, fps: float) -> bool:
    return abs(t * fps - round(t * fps)) < 0.02


def test_a_20_fps_project_and_the_reels_preset_are_planned_on_the_30_fps_grid():
    from gen_brain_goldens import th_graph
    for controls, fps in ((REEL, 30.0), ({"duration_s": 20.0}, 20.0)):
        g = Graph(th_graph())
        g.project_fps = 20.0
        edp = plan(g, controls)
        edges = _cut_edges(edp)
        assert edges and all(_on_grid(t, fps) for t in edges), (controls, [t for t in edges if not _on_grid(t, fps)][:5])
    # and only the edges of the rate the export will have: a 20 fps grid is NOT a 30 fps grid
    g = Graph(th_graph())
    g.project_fps = 20.0
    assert not all(_on_grid(t, 20.0) for t in _cut_edges(plan(g, REEL)))


@pytest.mark.parametrize("project_fps", [24.0, 25.0, 29.97, 30.0, 60.0])
def test_every_edge_is_on_the_projects_grid_not_the_files_20_fps_grid(project_fps):
    """EX-08: the file is 20 fps (edges on 0.05 s steps would be half-frame ties at 30); the project's rate rules."""
    from gen_brain_goldens import th_graph
    from video_ai_editor.edl.timebase import rate_of
    g = Graph(th_graph())
    g.project_fps = project_fps
    rate = float(rate_of(project_fps))
    edges = _cut_edges(plan(g, {}))
    assert edges and all(abs(t * rate - round(t * rate)) < 0.02 for t in edges), [t for t in edges if abs(t * rate - round(t * rate)) >= 0.02][:5]


def test_the_plan_conforms_the_canvas_before_the_first_cut():
    from gen_brain_goldens import TH_PATH, th_graph
    from video_ai_editor.agent.prompt.schema import STAGE_PREREQ
    graph = th_graph()
    edp = plan(graph, REEL)
    facts = TimelineFacts.minimal(duration=80.0, allowed_paths={TH_PATH}, has_transcript=True, words=200, speech_seconds=60.0,
                                  tools_available=set(), fps=20)
    out = C.compile_edp(edp, facts, graph)
    conform = [s for s in out.steps if s.tool == "set_canvas"]
    assert len(conform) == 1 and conform[0].args == {"fps": 30.0} and conform[0].stage == STAGE_PREREQ
    assert "30 fps" in conform[0].why
    # the validator stage-sorts (stable, an explicit `stage` wins): the conform leads, then the cuts, the preset last
    tools = [s.tool for s in sorted(out.steps, key=lambda s: s.stage)]
    assert tools.index("set_canvas") < tools.index("cut_source_ranges") and tools.index("cut_source_ranges") < tools.index("apply_export_preset")
    # a project that keeps its rate under the preset gets no such step
    for fps in (24, 25, 29.97, 30, 60):
        kept = C.compile_edp(edp, facts.model_copy(update={"fps": fps}), graph)
        assert not [s for s in kept.steps if s.tool == "set_canvas"], fps
    # no export preset, no conform
    edp2 = plan(graph, {"duration_s": 20.0})
    assert not [s for s in C.compile_edp(edp2, facts, graph).steps if s.tool == "set_canvas"]


# --------------------------------------------------------------------------
# captions: every spoken, kept word is captioned (UX-09), and a podcast's cues start with the speaker (UX-03)
# --------------------------------------------------------------------------

def _cues_of(graph: dict, controls: dict) -> list[dict]:
    edp = plan(graph, controls)
    return next(d for d in edp["decisions"] if d["kind"] == "captions")["params"]["cues"]


def _tokens(text: str) -> list[str]:
    import re
    return re.findall(r"[\w'’]+", text.lower())


def _uncaptioned_kept_words(graph: dict, controls: dict) -> list[str]:
    """Words the plan keeps (their midpoint is inside no removal), that a viewer hears, and no cue covers."""
    edp = plan(graph, controls)
    g = Graph(graph)
    cues = next(d for d in edp["decisions"] if d["kind"] == "captions")["params"]["cues"]
    cuts = [(g.to_ref(d["ref"]["t0"], g.primary), g.to_ref(d["ref"]["t1"], g.primary))
            for d in edp["decisions"] if d["kind"] == "cut_range"]
    out = []
    for w in g.words:
        mid = 0.5 * (w["t0"] + w["t1"])
        if w.get("filler") or not str(w["text"]).strip() or any(a <= mid <= b for a, b in cuts):
            continue
        if not any(c["t0"] - 0.05 <= mid <= c["t1"] + 0.05 for c in cues):
            out.append((w["text"], w["t0"]))
    return out


def test_a_single_speakers_word_between_two_turns_is_still_captioned():
    """TH: 'write' (mid 10.91) sat between turns 0.03-10.58 and 11.21-13.78; the 'no turn under it' rule (made for a
    podcast's mishearings) skipped it, and the viewer heard a word the caption never showed."""
    from gen_brain_goldens import th_graph
    graph = th_graph()
    turns = graph["layers"]["speech"]["turns"]
    words = graph["layers"]["speech"]["words"]
    turns[1]["t0"] = round(turns[1]["t0"] + 1.0, 3)                # a gap of 1.5 s: the sentence's first words fall in it
    gap = [w for w in words if not w.get("filler") and turns[0]["t1"] + 0.15 < 0.5 * (w["t0"] + w["t1"]) < turns[1]["t0"] - 0.15]
    assert gap, "the fixture edit must put words in the gap"
    assert _uncaptioned_kept_words(graph, {}) == []


def test_every_kept_word_of_the_talking_head_is_captioned():
    from gen_brain_goldens import th_graph
    assert _uncaptioned_kept_words(th_graph(), {}) == []


def _guest_first_word_timed_early(seconds: float = 1.2) -> tuple[dict, dict, dict]:
    """The P2 graph with the guest's first word of one turn timed `seconds` early, as whisper does it
    ('Salt' 0.5 s and 'Let's' 1.8 s before the speaker starts)."""
    graph = p2_graph()
    speech = graph["layers"]["speech"]
    turn = next(t for t in speech["turns"] if t["spk"] == "S2" and t["t0"] > 30)
    first = next(w for w in speech["words"] if w["spk"] == "S2" and abs(w["t0"] - turn["t0"]) < 0.05)
    dur = first["t1"] - first["t0"]
    first["t0"], first["t1"] = round(turn["t0"] - seconds - dur, 3), round(turn["t0"] - seconds, 3)
    return graph, turn, first


def test_a_first_word_timed_before_its_speaker_starts_is_not_an_orphan_cue():
    graph, turn, first = _guest_first_word_timed_early()
    cues = _cues_of(graph, PODCAST)
    word = first["text"].strip(".,?!").lower()
    mine = [c for c in cues if word in _tokens(c["text"])[:2] and c["spk"] == "S2" and abs(c["t0"] - turn["t0"]) < 2.5]
    assert mine, (word, turn)
    c = mine[0]
    assert len(_tokens(c["text"])) >= 3, f"an orphan one-word cue: {c}"                      # it rides with the words it belongs to
    assert c["t0"] >= turn["t0"] - 0.1, (c, turn)                                          # and starts with the speaker, not before


def test_a_podcasts_cues_read_the_same_for_the_host_and_the_guest():
    graph = p2_graph()
    for w in graph["layers"]["speech"]["words"]:
        if w["spk"] == "S2":                                   # the guest as the ASR gave it: no capitals, no full stops
            w["text"] = w["text"].strip(".,?!").lower()
    cues = _cues_of(graph, PODCAST)
    speech = graph["layers"]["speech"]
    opened = ended = 0
    for s in (s for s in speech["sentences"] if s["spk"] == "S2"):
        starts = [c for c in cues if c["spk"] == "S2" and s["t0"] - 0.05 <= c["t0"] <= s["t0"] + 0.3]
        stops = [c for c in cues if c["spk"] == "S2" and s["t1"] - 0.1 <= c["t1"] <= s["t1"] + 0.3]
        if starts:
            opened += 1
            assert starts[0]["text"].lstrip()[0].isupper(), (s["id"], starts[0])        # a cue that opens a sentence: a capital
        if stops:
            ended += 1
            assert stops[-1]["text"].rstrip()[-1] in ".?!", (s["id"], stops[-1])         # one that ends it: its stop
    assert opened >= 5 and ended >= 5, (opened, ended)
    assert not any(c["text"].startswith(("i ", "i'")) for c in cues)


def test_the_host_keeps_his_own_punctuation():
    graph = p2_graph()
    words = {w["id"]: w for w in graph["layers"]["speech"]["words"]}
    cues = _cues_of(graph, PODCAST)
    host_texts = " ".join(c["text"] for c in cues if c["spk"] == "S1")
    assert "Welcome back to the show," in host_texts.replace("\n", " ")
    assert "??" not in host_texts and ".." not in host_texts.replace("…", "")


# --------------------------------------------------------------------------
# a frame grid under words that are not on it never leaves a cut edge inside a word (N-06 / N-03 on real timings)
# --------------------------------------------------------------------------

def _decision_edges_inside_words(edp: dict, g: Graph) -> list[tuple[str, float, str]]:
    """Every cut / keep edge of the plan against the graph's words, the safety net's own measure (tol 0.02 s)."""
    bad = []
    for d in edp["decisions"]:
        if d["kind"] not in ("keep_window", "cut_range"):
            continue
        for t_file in (d["ref"]["t0"], d["ref"]["t1"]):
            t = g.to_ref(t_file, g.primary)
            bad += [(d["id"], t, w["text"]) for w in g.words if w["t0"] + 0.02 < t < w["t1"] - 0.02]
    return bad


def _filler_that_runs_into_the_next_word(fps: float) -> tuple[dict, dict]:
    """TH with the first 'um,' timed to END 26 ms after a frame boundary of `fps` and the next word starting right
    there (a recogniser times an 'um' into the pause after it): the last frame boundary before its end leaves 26 ms
    of the filler, more than the 20 ms the safety net allows, and the next word leaves no room to reach past it."""
    from gen_brain_goldens import th_graph
    graph = th_graph()
    words = graph["layers"]["speech"]["words"]
    i = next(i for i, w in enumerate(words) if w.get("filler"))
    end = round(int(words[i]["t1"] * fps) / fps + 0.026, 4)
    words[i]["t1"], words[i + 1]["t0"] = end, end
    return graph, words[i]


@pytest.mark.parametrize("fps", [20.0, 25.0, 30.0])
def test_a_filler_the_frame_grid_would_cut_short_goes_whole_and_no_edge_is_inside_a_word(fps):
    graph, filler = _filler_that_runs_into_the_next_word(fps)
    g = Graph(graph)
    g.project_fps = fps
    edp = plan(g, {})
    assert _decision_edges_inside_words(edp, g) == []
    cuts = [(d["ref"]["t0"], d["ref"]["t1"]) for d in edp["decisions"] if d["kind"] == "cut_range"]
    assert any(a - 0.02 <= filler["t0"] and b + 0.02 >= filler["t1"] for a, b in cuts), (filler, cuts)


@pytest.mark.parametrize("extra", [0.011, 0.023, 0.037])
def test_cut_edges_stay_out_of_words_whatever_the_fillers_tails_are(extra):
    from gen_brain_goldens import th_graph
    for graph, controls in ((th_graph(), {}), (th_graph(), {"duration_s": 30.0, "platform": "reels", "ratio": "9:16"}),
                            (p2_graph(), PODCAST)):
        graph = copy.deepcopy(graph)
        for w in graph["layers"]["speech"]["words"]:
            if w.get("filler"):
                w["t1"] = round(w["t1"] + extra, 3)
        for fps in (20.0, 25.0, 30.0):
            g = Graph(graph)
            g.project_fps = fps
            assert _decision_edges_inside_words(plan(g, controls), g) == [], (fps, controls)


def test_a_removal_edge_inside_a_word_takes_the_side_the_removal_means():
    camera, _, ctx = _sliver_ctx(30.0)
    g = ctx.graph
    word = next(w for w in g.words if not w.get("filler") and 0.25 < w["t1"] - w["t0"] < 0.6)
    mid = ctx.removal_start(g.to_file(0.5 * (word["t0"] + word["t1"]), ctx.primary))
    assert g.word_straddling(g.to_ref(mid, ctx.primary)) is not None
    after = ctx.word_safe(mid, start=True, whole=lambda w: False)          # a removal START that keeps the word: after it
    before = ctx.word_safe(mid, start=True, whole=lambda w: True)          # … one that takes it: before it
    assert g.to_ref(after, ctx.primary) >= word["t1"] - 0.02 and g.word_straddling(g.to_ref(after, ctx.primary)) is None
    assert g.to_ref(before, ctx.primary) <= word["t0"] + 0.02 and g.word_straddling(g.to_ref(before, ctx.primary)) is None
    end_keeps = ctx.word_safe(mid, start=False, whole=lambda w: False)    # a removal END that keeps the word: before it
    assert g.to_ref(end_keeps, ctx.primary) <= word["t0"] + 0.02
    assert ctx.word_safe(ctx.removal_start(g.to_file(word["t0"] - 0.5, ctx.primary)), start=True, whole=lambda w: True) \
        == ctx.removal_start(g.to_file(word["t0"] - 0.5, ctx.primary)), "an edge in the clear never moves"


def test_a_removal_edge_with_no_clear_side_stays_for_the_safety_net_to_say_so():
    camera, _, ctx = _sliver_ctx(30.0)
    g = ctx.graph
    words = g.words
    i = next(i for i, w in enumerate(words[:-2]) if not w.get("filler") and w["t1"] - w["t0"] > 0.2)
    word = words[i]
    wide = {**word, "id": "w_wide", "t0": round(word["t0"] - 1.0, 3), "t1": round(word["t1"] + 1.0, 3), "text": "wide"}
    g.__dict__["words"] = sorted([*words, wide], key=lambda w: (w["t0"], w["t1"]))
    g.__dict__.pop("_word_starts", None)
    f = ctx.removal_start(g.to_file(0.5 * (word["t0"] + word["t1"]), ctx.primary))
    assert g.word_straddling(g.to_ref(f, ctx.primary)) is not None
    assert ctx.word_safe(f, start=True, whole=lambda w: True) == f


# --------------------------------------------------------------------------
# the Plan tab's sentences: plain words (UX-05 / N-16)
# --------------------------------------------------------------------------

def _plan_sentences(edp: dict) -> list[str]:
    out = [d["reason"]["text"] for d in edp["decisions"]]
    out += [f"{d['asked']} {d['why']}" for d in edp["summary"]["deferred"]]
    return out


@pytest.mark.parametrize("which", ["th_reel", "th_episode", "p2"])
def test_plan_sentences_carry_no_internal_name_and_no_removed_filler(which):
    import re
    from gen_brain_goldens import th_graph
    graph, controls = {"th_reel": (th_graph(), {"duration_s": 30.0, "platform": "reels", "ratio": "9:16"}),
                       "th_episode": (th_graph(), {}), "p2": (p2_graph(), PODCAST)}[which]
    edp = plan(graph, controls)
    sentences = _plan_sentences(edp)
    assert len(sentences) >= 5
    leak = re.compile(r"premium_podcast|viral_reel|clean_professional|\bsrc_|[wsk]_\d{4}|d_[0-9a-f]{8}|rules? \d|\bsigma\b|arousal|rms_z"
                      r"|\bnp\.|stretch \d", re.I)
    for text in sentences:
        assert not leak.search(text), text
    fillers = {w["text"].strip(".,?!").lower() for w in graph["layers"]["speech"]["words"] if w.get("filler")}
    for text in (d["reason"]["text"] for d in edp["decisions"] if d["kind"] in ("punch_in", "open_on")):
        quoted = re.findall(r"“([^”]*)”", text)
        assert quoted and not any(q.split()[0].strip(".,?!…").lower() in fillers for q in quoted if q.split()), text


def test_the_deferred_camera_rate_names_the_style_in_words():
    edp = plan(p2_graph(), PODCAST)
    rate = next(d["why"] for d in edp["summary"]["deferred"] if d["asked"] == "switch rate")
    assert "premium podcast style" in rate and "premium_podcast" not in rate, rate


# --------------------------------------------------------------------------
# the Not-done list says a repeated kind once (a 45-minute recording left 40 pauses in place)
# --------------------------------------------------------------------------

def test_a_repeated_left_in_place_note_is_one_line_with_its_count_and_span():
    from video_ai_editor.brain.planner import collapse_deferred
    why = "sound the transcript has no word for; left in place"
    items = [{"asked": f"pause at {m}:{s:02d}", "why": why} for m, s in ((0, 57), (1, 56), (2, 1), (2, 24), (44, 1))]
    items.insert(2, {"asked": "cold open", "why": "arrives next wave"})
    items.append({"asked": "possible filler at 1:40", "why": "listed, not cut"})
    items.append({"asked": "possible filler at 3:03", "why": "listed, not cut"})
    out = collapse_deferred(items)
    assert out[0] == {"asked": "5 pauses between 0:57 and 44:01", "why": why}
    assert [d["asked"] for d in out[1:]] == ["cold open", "possible filler at 1:40", "possible filler at 3:03"], out   # under four: as they are
    assert collapse_deferred([]) == [] and collapse_deferred(items[2:3]) == items[2:3]


def test_the_first_word_of_a_sentence_timed_early_rides_with_its_sentence_even_with_one_speaker_label():
    """A conversation the speaker layer labelled as ONE speaker has no turn edge: 'Salt' 95.9 s, the first word of its
    sentence, 'gets everywhere' at 97.1 s (real recording). The word is timed into the pause before the sentence; the
    cue must carry it WITH its sentence, opening where the sentence is spoken, and never be a one-word cue of its own."""
    from gen_brain_goldens import th_graph
    graph = th_graph()
    words = graph["layers"]["speech"]["words"]
    sent = next(s for s in graph["layers"]["speech"]["sentences"] if s["id"] == "s_00003")
    own = [w for w in words if w["sent"] == sent["id"]]
    first, second = own[0], own[1]
    dur = first["t1"] - first["t0"]
    first["t1"] = round(second["t0"] - 0.9, 3)                    # timed 0.9 s before the word after it
    first["t0"] = round(first["t1"] - dur, 3)
    cues = _cues_of(graph, {})
    mine = [c for c in cues if _tokens(c["text"])[:1] == [first["text"].strip(".,?!").lower()] and abs(c["t0"] - second["t0"]) < 0.6]
    assert mine, [c for c in cues if 11 < c["t0"] < 14]
    assert len(_tokens(mine[0]["text"])) >= 3 and mine[0]["t0"] >= second["t0"] - 0.45, mine[0]
    assert not [c for c in cues if len(_tokens(c["text"])) == 1 and 11 < c["t0"] < 13]
