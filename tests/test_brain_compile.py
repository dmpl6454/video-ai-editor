"""EDP → Plan (spec §5.6): every decision kind maps to its tool, the stage-2
sentinel order ends with `sync_dialogue_lane`, ≤ 24 steps for the podcast,
and the compiled plan validates on `TimelineFacts.minimal()` once lane C's
contract (the three tools in `TOOL_STAGE`, the `$brain:*` rule) is in the
tree — until then the literal steps validate and the sentinel steps are
checked against the frozen shapes."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import P2_A_PATH, P2_B_PATH, P2_REC_PATH, TH_PATH, p2_graph, th_graph  # noqa: E402

from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402
from video_ai_editor.agent.prompt.schema import CHECK_SPECS, TOOL_STAGE, Plan  # noqa: E402
from video_ai_editor.brain import compile as C  # noqa: E402
from video_ai_editor.brain.planner import plan  # noqa: E402
from video_ai_editor.brain.planner.graph_view import Graph  # noqa: E402

REEL = {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"}
PODCAST = {"content_type": "podcast"}
KIND_TOOLS = {
    "keep_window": {"cut_source_ranges"}, "cut_range": {"cut_source_ranges"}, "keep_pause": set(),
    "open_on": {"split_at", "reorder_clips"}, "switch_angle": {"apply_camera_plan"},
    "punch_in": {"add_keyframe"}, "jump_cut_hide": {"add_keyframe"},
    "captions": {"add_caption_track", "set_caption_style"}, "music": {"add_music", "set_duck", "fit_music_to_video"},
    "reframe": {"auto_reframe", "set_clip_fit"}, "dialogue": {"sync_dialogue_lane"}, "export_preset": {"apply_export_preset"},
}
SENTINEL_OF = {"keep_window": "$brain:keep", "cut_range": "$brain:cuts", "switch_angle": "$brain:camera",
               "punch_in": "$brain:punch_ins", "jump_cut_hide": "$brain:punch_ins", "captions": "$brain:captions"}


def _facts(paths: list[str], duration: float) -> TimelineFacts:
    from video_ai_editor.agent.prompt.presets import music_beds
    beds = {str(b.path) for b in music_beds()}
    return TimelineFacts.minimal(duration=duration, allowed_paths=set(paths) | beds, has_transcript=True, words=200,
                                 speech_seconds=duration * 0.8, tools_available=set())


def test_every_kind_maps_to_its_tool():
    for graph, controls in ((th_graph(), REEL), (p2_graph(), PODCAST)):
        edp = plan(graph, controls)
        out = C.compile_edp(edp, _facts([TH_PATH, P2_A_PATH, P2_B_PATH, P2_REC_PATH], 180.0), graph)
        tools = [s.tool for s in out.steps]
        kinds = {d["kind"] for d in edp["decisions"]}
        for kind in kinds:
            assert KIND_TOOLS[kind] <= set(tools), (kind, tools)
        for s in out.steps:
            for k, v in s.args.items():
                if isinstance(v, str) and v.startswith("$brain:"):
                    assert s.args.get("plan_ref") == edp["id"]
                    assert re.fullmatch(r"d_[0-9a-f]{8}", s.args["plan_ref"])
        for kind, sentinel in SENTINEL_OF.items():
            if kind in kinds:
                assert any(sentinel in s.args.values() for s in out.steps), (kind, sentinel)
        # the aesthetic audit rates a HOOK: a reel closes on it, an episode has none and is not audited (closer review)
        if edp["summary"]["target"] == "reel":
            assert tools[-1] == "audit_aesthetic"
        else:
            assert "audit_aesthetic" not in tools and "audit_ok" not in [p.check for p in out.postconditions]
        assert edp["compiled"] is None or edp["compiled"]["steps"] == len(out.steps)
        assert set(out.sentinels) == {SENTINEL_OF[k].split(":", 1)[1] for k in kinds if k in SENTINEL_OF} | \
            ({"story_splits", "story_order"} if "open_on" in kinds else set())


def test_stage2_order_ends_with_sync_dialogue_lane():
    for graph, controls in ((th_graph(), REEL), (p2_graph(), PODCAST)):
        edp = plan(graph, controls)
        out = C.compile_edp(edp, _facts([TH_PATH, P2_A_PATH, P2_B_PATH, P2_REC_PATH], 180.0), graph)
        stage2 = [s for s in out.steps if s.stage == 2]
        assert stage2[-1].tool == "sync_dialogue_lane"
        order = [C.stage2_rank(s) for s in stage2]
        assert order == sorted(order), [s.tool for s in stage2]
        # the validator's stage sort is stable (validate.py `# stable`), so a stage-sorted copy keeps the order
        resorted = sorted(out.steps, key=lambda s: s.stage if s.stage is not None else 12)
        assert [s.tool for s in resorted if s.stage == 2] == [s.tool for s in stage2]
        sync = stage2[-1]
        assert sync.args["lane"] == "a1" and sync.args["seam_fade_s"] == 0.005 and sync.args["mute_camera_mics"] is True
        assert sync.args["src"] in sync.args["offsets"]
        if len(graph["graph"]["sources"]) > 1:
            assert sync.args["src"] == P2_REC_PATH and set(sync.args["offsets"]) == {P2_REC_PATH, P2_A_PATH, P2_B_PATH}


def test_leq_24_steps_for_p2():
    graph = p2_graph()
    edp = plan(graph, PODCAST)
    out = C.compile_edp(edp, _facts([P2_A_PATH, P2_B_PATH, P2_REC_PATH], 180.0), graph)
    assert len(out.steps) <= 24, [s.tool for s in out.steps]
    assert len(out.postconditions) <= 20
    assert all(len(s.why) <= 160 for s in out.steps)
    cuts = next(s for s in out.steps if s.tool == "cut_source_ranges" and s.args["ranges"] == "$brain:cuts")
    assert re.match(r"Removed \d+ stretch", cuts.why), cuts.why


def test_compiled_plan_validates_on_minimal_facts(tmp_path):
    from video_ai_editor.agent.prompt.validate import validate_plan
    src = tmp_path / "talking_head.mp4"
    src.write_bytes(b"\0" * 64)
    graph = th_graph()
    for s in graph["graph"]["sources"]:
        s["path"] = str(src)
    graph["angles"]["members"][0]["path"] = str(src)
    edp = plan(graph, REEL)
    facts = _facts([str(src)], 74.0)
    out = C.compile_edp(edp, facts, graph)
    literal = [s for s in out.steps if not any(isinstance(v, str) and v.startswith("$brain:") for v in s.args.values())
               and s.tool in TOOL_STAGE]
    assert literal, "the literal steps exist"
    # the EDP goes through lane C's store and models exactly as `_x_edit` writes it
    from video_ai_editor import config
    from video_ai_editor.brain import schema as S, store as ST
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    try:
        sdir = tmp_path / facts.session_id
        ST.write_graph(sdir, S.Graph.model_validate(graph["graph"]))
        ST.write_json(ST.brain_dir(sdir) / "angles.json", graph["angles"])
        ST.write_json(ST.brain_dir(sdir) / "scenes.json", graph["scenes"])
        ST.set_current_graph(sdir, graph["graph"]["id"])
        model = S.EDP.model_validate(edp)
        S.check_facts(model, S.graph_ids(S.Graph.model_validate(graph["graph"]),
                                         {k: ST.LAYER_MODELS[k].model_validate(v) for k, v in graph["layers"].items() if k in ST.LAYER_MODELS},
                                         scenes=S.Scenes.model_validate(graph["scenes"]), angles=S.Angles.model_validate(graph["angles"])))
        ST.write_edp(sdir, model)
        p = Plan.new(intent="edit", brain="recipes", steps=out.steps, needs_input=[], postconditions=out.postconditions,
                     confidence=0.9, estimated_seconds=out.estimated_seconds)
        checked = validate_plan(p, facts)
    finally:
        monkeypatch.undo()
    assert [s.tool for s in checked.steps if s.stage == 2][-1] == "sync_dialogue_lane"
    assert any(s.tool == "add_music" for s in checked.steps)


# --------------------------------------------------------------------------
# EB1 integration
# --------------------------------------------------------------------------

def _facts_with_v1(paths: list[str], names: list[str], duration: float) -> TimelineFacts:
    from video_ai_editor.agent.prompt.facts import ClipFact
    base = _facts(paths, duration)
    clips = [ClipFact(id=f"c_{i:08x}", track="v1", start=i * 100.0, duration=100.0, src_in=0.0, src_out=100.0, name=n)
             for i, n in enumerate(names)]
    return base.model_copy(update={"clips": clips})


def test_the_other_angles_leave_the_main_lane_first():
    """Two cameras dropped on the timeline land one after the other on v1;
    the edit plays ONE programme, so the second angle's own clip goes before
    anything else — a literal step, no sentinel."""
    graph = p2_graph()
    edp = plan(graph, PODCAST)
    paths = [P2_A_PATH, P2_B_PATH, P2_REC_PATH]
    both = C.compile_edp(edp, _facts_with_v1(paths, [Path(P2_A_PATH).stem, Path(P2_B_PATH).stem], 360.0), graph)
    first = both.steps[0]
    assert first.tool == "cut_source_ranges" and first.stage == TOOL_STAGE["cut_source_ranges"]
    assert [r["src"] for r in first.args["ranges"]] == [P2_B_PATH] and "plan_ref" not in first.args
    # the WHOLE of the angle's file (a truncated range would leave the rest of it on the main lane: SC-03)
    b_key = next(str(m["src_key"]) for m in Graph(graph).members if m.get("path") == P2_B_PATH)
    assert first.args["ranges"][0]["start"] == 0.0 and first.args["ranges"][-1]["end"] == Graph(graph).duration_of(b_key)
    assert all(0.0 < r["end"] - r["start"] <= C.MAX_RANGE_S for r in first.args["ranges"])
    assert C.stage2_rank(first) < min(C.stage2_rank(s) for s in both.steps[1:] if s.stage == first.stage)
    assert [s.tool for s in both.steps if s.stage == first.stage][-1] == "sync_dialogue_lane"
    assert any("clip was moved off the main lane" in n and "comes back wherever the edit cuts to it" in n for n in both.notes)
    assert len(both.steps) <= C.MAX_STEPS
    # only the first camera on the main lane: nothing to take off
    one = C.compile_edp(edp, _facts_with_v1(paths, [Path(P2_A_PATH).stem], 180.0), graph)
    assert not any(isinstance(s.args.get("ranges"), list) for s in one.steps)
    assert len(one.steps) == len(both.steps) - 1
    # a single camera never gets the step
    th = C.compile_edp(plan(th_graph(), REEL), _facts_with_v1([TH_PATH], [Path(TH_PATH).stem], 80.0), th_graph())
    assert not any(isinstance(s.args.get("ranges"), list) for s in th.steps)


def test_the_cuts_line_reads_as_english():
    assert C._plural(8, "stretch") == "8 stretches" and C._plural(1, "stretch") == "1 stretch"
    assert C._plural(3, "silence") == "3 silences" and C._plural(2, "stretch of dead air") == "2 stretches of dead air"
    out = C.compile_edp(plan(p2_graph(), PODCAST), _facts([P2_A_PATH, P2_B_PATH, P2_REC_PATH], 180.0), p2_graph())
    why = next(s.why for s in out.steps if s.args.get("ranges") == "$brain:cuts")
    assert why.startswith("Removed ") and "stretchs" not in why


# --------------------------------------------------------------------------
# SC-03: a long second angle leaves the main lane WHOLE
# --------------------------------------------------------------------------

LONG_S = 2700.0                                       # a 45-minute podcast


def _long_p2():
    graph = p2_graph()
    for s in graph["graph"]["sources"]:
        if s.get("role") == "angle":
            s["duration"] = LONG_S
    for m in graph["angles"]["members"]:
        m["duration"] = LONG_S
    return graph


def test_a_45_minute_second_angle_is_taken_off_the_main_lane_whole(tmp_path):
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl.schema import Clip
    from video_ai_editor.edl.snapshot import EDLStore
    graph = _long_p2()
    edp = plan(graph, PODCAST)
    paths = [P2_A_PATH, P2_B_PATH, P2_REC_PATH]
    out = C.compile_edp(edp, _facts_with_v1(paths, [Path(P2_A_PATH).stem, Path(P2_B_PATH).stem], 2 * LONG_S), graph)
    first = out.steps[0]
    ranges = first.args["ranges"]
    b = sorted((r for r in ranges if r["src"] == P2_B_PATH), key=lambda r: r["start"])
    assert all(r["src"] == P2_B_PATH for r in ranges), "only the second angle leaves"
    assert all(r["end"] - r["start"] <= C.MAX_RANGE_S + 1e-9 for r in b), "one range is at most 600 s"
    assert b[0]["start"] == 0.0 and b[-1]["end"] == LONG_S, "the ranges reach the end of the angle"
    assert all(x["end"] == y["start"] for x, y in zip(b, b[1:])), "no hole between two ranges"
    assert len(out.steps) <= C.MAX_STEPS, len(out.steps)
    # executed on the timeline it describes: v1 = camera A's 45 min then camera B's 45 min
    store = EDLStore(tmp_path / "long")
    v1 = store.edl.get_track("v1")
    v1.clips = [Clip(id="c_a", src=P2_A_PATH, in_=0.0, out=LONG_S, start=0.0),
                Clip(id="c_b", src=P2_B_PATH, in_=0.0, out=LONG_S, start=LONG_S)]
    store.edl.recompute_duration()
    store.commit("init", {}, "init")
    from video_ai_editor import config
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    try:
        dispatch(store, first.tool, {k: v for k, v in first.args.items() if k != "plan_ref"})
    finally:
        config.enable_path_restriction(before)
    left = [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]
    assert {c.src for c in left} == {P2_A_PATH}, [(c.src, c.in_, c.out) for c in left]
    assert abs(sum(c.out - c.in_ for c in left) - LONG_S) < 0.1


# --------------------------------------------------------------------------
# UX-03: the two-camera edit is captioned for EVERY speaker
# --------------------------------------------------------------------------

DURATION = {P2_A_PATH: 168.9, P2_B_PATH: 168.35}
NEEDS_MEDIA = {"sync_dialogue_lane", "audit_aesthetic", "add_music", "set_duck", "fit_music_to_video", "apply_export_preset",
               "add_keyframe", "auto_reframe", "set_clip_fit"}


def _world(tmp_path, monkeypatch, graph, controls, clips, names):
    """A plan compiled and RUN (every step that needs no real media) on a
    timeline of `clips` (`[(path, seconds)]`, one after the other), the way
    `_x_edit` does: sentinels resolve against the live tree, step by step."""
    import importlib
    from video_ai_editor import config
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.brain import resolve as R, schema as S, store as ST
    from video_ai_editor.edl.schema import Clip
    from video_ai_editor.edl.snapshot import EDLStore
    D = importlib.import_module("video_ai_editor.agent.dispatch")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(D, "_source_video_extent", lambda p, fps: dict(clips).get(str(p)))
    monkeypatch.setattr(config, "_FORCED_RESTRICT", False)
    edp = plan(graph, controls)
    store = EDLStore(tmp_path / "s_world")
    sdir = Path(store.dir)
    for name, rel in graph["graph"]["layers"].items():
        ST.write_layer(graph["graph"]["reference"], name, Path(rel).stem, graph["layers"][name], workdir=tmp_path)
    ST.write_graph(sdir, S.Graph.model_validate(graph["graph"]))
    ST.write_json(ST.brain_dir(sdir) / "angles.json", graph["angles"])
    ST.write_json(ST.brain_dir(sdir) / "scenes.json", graph["scenes"])
    ST.set_current_graph(sdir, graph["graph"]["id"])
    ST.write_edp(sdir, S.EDP.model_validate(edp))
    store.edl.canvas.fps = int(float(graph["graph"]["project"]["fps"]))
    at = 0.0
    pieces = []
    for i, (path, seconds) in enumerate(clips):
        pieces.append(Clip(id=f"c_{i:02d}", src=path, in_=0.0, out=seconds, start=at))
        at += seconds
    store.edl.get_track("v1").clips = pieces
    store.edl.recompute_duration()
    store.commit("init", {}, "init")
    out = C.compile_edp(edp, _facts_with_v1([p for p, _ in clips] + [P2_REC_PATH], names, 2 * at), graph)
    for s in out.steps:
        if s.tool in NEEDS_MEDIA:
            continue
        sentinel = any(isinstance(v, str) and v.startswith("$brain:") for v in s.args.values())
        resolved, _ = R.resolve(store, s.tool, dict(s.args)) if sentinel else (dict(s.args), [])
        for args in ([] if resolved is None else resolved if isinstance(resolved, list) else [resolved]):
            dispatch(store, s.tool, {k: v for k, v in args.items() if k != "plan_ref"})
    return store, graph, edp, out


def _p2_world(tmp_path, monkeypatch):
    return _world(tmp_path, monkeypatch, p2_graph(), PODCAST, list(DURATION.items()),
                  [Path(P2_A_PATH).stem, Path(P2_B_PATH).stem])


def _plays_at(store, path: str, off: float, t_ref: float) -> float | None:
    """Where reference second `t_ref` plays on v1, by the clips alone."""
    from video_ai_editor.edl.schema import Clip
    for c in store.edl.get_track("v1").clips:
        if isinstance(c, Clip) and c.src == path and c.in_ - 1e-6 <= t_ref + off < c.out - 1e-6:
            return c.start + (t_ref + off - c.in_) / (c.speed or 1.0)
    return None


def _coverage_by_speaker(store, graph) -> dict[str, tuple[int, int]]:
    """`{speaker: (words under a caption, words that play)}` measured from the
    v1 clips and the caption clips, independently of the resolver."""
    offsets = {P2_A_PATH: 0.35, P2_B_PATH: -0.20}
    cues = [(c.start, c.end) for c in store.edl.get_track("captions").clips]
    out: dict[str, list[int]] = {}
    turns = graph["layers"]["speech"]["turns"]
    for w in graph["layers"]["speech"]["words"]:
        mid = 0.5 * (w["t0"] + w["t1"])
        under = [t["t1"] - t["t0"] < 0.6 for t in turns if t["t0"] - 0.15 <= mid <= t["t1"] + 0.15]
        if w.get("filler") or not under or all(under):          # a filler, a backchannel, a word under no turn: no caption
            continue
        at = next((t for p, o in offsets.items() if (t := _plays_at(store, p, o, mid)) is not None), None)
        if at is None:
            continue
        row = out.setdefault(w["spk"], [0, 0])
        row[1] += 1
        row[0] += any(a - 1e-3 <= at <= b + 1e-3 for a, b in cues)
    return {k: (v[0], v[1]) for k, v in out.items()}


def test_the_podcast_is_captioned_for_the_host_and_the_guest(tmp_path, monkeypatch):
    store, graph, edp, out = _p2_world(tmp_path, monkeypatch)
    step = next(s for s in out.steps if s.tool == "add_caption_track")
    assert step.args["cues"] == "$brain:captions" and step.args["plan_ref"] == edp["id"]
    assert "captions" in out.sentinels and len(out.steps) <= C.MAX_STEPS
    cover = _coverage_by_speaker(store, graph)
    assert set(cover) == {"S1", "S2"}, cover
    for spk, (under, plays) in cover.items():
        assert plays > 40 and under / plays >= 0.9, (spk, under, plays)
    # both cameras carry cues: the guest's turns are on camera B and are captioned there
    from video_ai_editor.edl.schema import Clip
    on_b = [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip) and c.src == P2_B_PATH]
    caps = store.edl.get_track("captions").clips
    under_b = [k for k in caps if any(p.start <= 0.5 * (k.start + k.end) < p.start + p.effective_duration for p in on_b)]
    assert on_b and len(under_b) >= 10, (len(on_b), len(under_b))
    # the old build — the first file's transcript through that file's clips only — leaves the guest bare
    from video_ai_editor.agent.timemap import map_words_to_timeline
    words_a = [{"start": w["t0"] + 0.35, "end": w["t1"] + 0.35, "word": w["text"], "spk": w["spk"]}
               for w in graph["layers"]["speech"]["words"] if not w.get("filler")]
    old = map_words_to_timeline(store.edl, "v1", words_a, src=P2_A_PATH)
    old_spk = {s: sum(1 for w in old if w["spk"] == s) for s in ("S1", "S2")}
    assert old_spk["S2"] < 0.5 * cover["S2"][1] <= old_spk["S1"], (old_spk, cover)


def _worst(store, graph, tmp_path):
    from types import SimpleNamespace
    from video_ai_editor.agent.prompt import verify as V
    from video_ai_editor.agent.prompt.schema import Postcondition
    ctx = SimpleNamespace(store=store, edl=store.edl)
    return V.c_captions_cover(ctx, Postcondition(check="captions_cover", args={"min_ratio": 0.9}, human="captions cover the speech"))


def test_the_verifier_cannot_report_a_full_cover_when_a_speaker_has_none(tmp_path, monkeypatch):
    store, graph, edp, out = _p2_world(tmp_path, monkeypatch)
    ok = _worst(store, graph, tmp_path)
    assert ok.passed is True and ok.measured >= 0.9 and "Guest" in (ok.detail or "") and "Host" in (ok.detail or ""), ok
    # the guest's cues gone (what the transcript-of-the-first-file build produced): the check must say so
    cap = store.edl.get_track("captions")
    guest = {"S2"}
    offsets = {P2_A_PATH: 0.35, P2_B_PATH: -0.20}
    spans = []
    for w in graph["layers"]["speech"]["words"]:
        if w["spk"] in guest:
            mid = 0.5 * (w["t0"] + w["t1"])
            spans += [t for p, o in offsets.items() if (t := _plays_at(store, p, o, mid)) is not None]
    cap.clips = [k for k in cap.clips if not any(k.start - 1e-3 <= t <= k.end + 1e-3 for t in spans)]
    bad = _worst(store, graph, tmp_path)
    assert bad.passed is False and bad.measured < 0.5, bad
    assert "Guest" in (bad.detail or "")


def test_a_step_never_says_a_graph_key_or_a_file_name():
    """What the card and the run log print for a step (its why, the plan's
    notes) is written for the editor: cameras by their letter, never a file."""
    file_name = re.compile(r"\.(?:mp4|mov|wav|m4a)\b|normalized|src_[0-9a-f]{6}|cam_[ab]_|recorder")
    for graph, controls, on_v1 in ((p2_graph(), PODCAST, [Path(P2_A_PATH).stem, Path(P2_B_PATH).stem]),
                                   (th_graph(), REEL, [Path(TH_PATH).stem])):
        edp = plan(graph, controls)
        paths = [P2_A_PATH, P2_B_PATH, P2_REC_PATH, TH_PATH]
        out = C.compile_edp(edp, _facts_with_v1(paths, on_v1, 360.0), graph)
        for text in [s.why for s in out.steps] + out.notes + [out.title]:
            assert not file_name.search(text), text
    both = C.compile_edp(plan(p2_graph(), PODCAST), _facts_with_v1([P2_A_PATH, P2_B_PATH, P2_REC_PATH],
                                                                   [Path(P2_A_PATH).stem, Path(P2_B_PATH).stem], 360.0), p2_graph())
    assert both.steps[0].why.startswith("Camera B leaves the main lane")
    cap = next(s for s in both.steps if s.tool == "add_caption_track")
    assert re.search(r"Captions: Podcast, \d+ cues from the analysed speech of every speaker", cap.why), cap.why


def test_a_cue_whose_moment_was_cut_away_is_dropped_and_said(tmp_path, monkeypatch):
    """The sentinel maps through the LIVE layout: a stretch removed after the
    plan was made takes its captions with it, and the reply says so."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.brain import resolve as R
    store, graph, edp, out = _p2_world(tmp_path, monkeypatch)
    laid = len(store.edl.get_track("captions").clips)
    dispatch(store, "cut_range", {"track": "v1", "start": 40.0, "end": 70.0})
    args, notices = R.resolve(store, "add_caption_track", {"style": "default", "position": "bottom", "cues": "$brain:captions",
                                                           "plan_ref": edp["id"]})
    assert args is not None and 0 < len(args["cues"]) < laid and "plan_ref" not in args and args["style"] == "default"
    assert any("captions dropped" in n or "caption dropped" in n for n in notices), notices
    assert all(c["end"] <= store.edl.video_extent() + 1e-3 and c["end"] > c["start"] for c in args["cues"])


def test_the_reel_captions_follow_the_hook_to_the_front_and_stop_at_every_seam(tmp_path, monkeypatch):
    """The reel keeps a window, cuts inside it and moves the hook to the
    front: every cue still sits on ITS words, whole, inside one piece."""
    from video_ai_editor.edl.schema import Clip
    graph = th_graph()
    store, _, edp, out = _world(tmp_path, monkeypatch, graph, REEL, [(TH_PATH, 74.0)], [Path(TH_PATH).stem])
    pieces = [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]
    caps = store.edl.get_track("captions").clips
    assert len(pieces) >= 4 and len(caps) >= 15
    for k in caps:
        holder = [p for p in pieces if p.start - 1e-3 <= k.start and k.end <= p.start + p.effective_duration + 1e-3]
        assert len(holder) == 1, ("a cue crosses a seam", k.text, k.start, k.end)
    # the hook plays first and is captioned first
    hook = next(d for d in edp["decisions"] if d["kind"] == "open_on")
    quote = " ".join(next(x for x in graph["layers"]["speech"]["sentences"] if x["id"] == hook["params"]["sent"])["text"].split()[:2]).lower()
    assert caps[0].start < 1.0 and caps[0].text.replace("\n", " ").lower().startswith(quote.split()[0]), (caps[0].text, quote)
    # every kept word that plays is under a cue: the programme has no bare speech
    words = [w for w in graph["layers"]["speech"]["words"] if not w.get("filler")]
    plays = [(w, at) for w in words if (at := _plays_at(store, TH_PATH, 0.0, 0.5 * (w["t0"] + w["t1"]))) is not None]
    under = sum(any(k.start - 1e-3 <= at <= k.end + 1e-3 for k in caps) for _, at in plays)
    assert len(plays) > 60 and under / len(plays) >= 0.97, (under, len(plays))
