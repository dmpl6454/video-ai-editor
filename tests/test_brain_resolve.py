"""`$brain:<kind>` — the validator rule, the live branch, the resolver and
the executor's last line (EB1 lane C), plus Milestone A brought forward:
a hand-written EDP compiled BY HAND into a Plan that validates, dry-runs on
`preview.scratch_store`, applies as ONE op with `decisions` in the commit
args and a version row written.

The talking-head stand-in is `brain_contract_fixtures` (a 12 s lavfi clip
with a hand-written transcript and a hand-built graph) until lane A's
fixture lands. Tests that need lane B's tools to EXECUTE skip until
`cut_source_ranges` is in `agent.tools.ALL_TOOLS`; everything the contract
owns (validation, resolution, footprint, caps, staleness, the round trip
through the existing tools) runs now.
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor import config  # noqa: E402
from video_ai_editor.agent import tools as _tools  # noqa: E402
from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.agent.prompt import executor, live, preview, validate  # noqa: E402
from video_ai_editor.agent.prompt import verify as _verify  # noqa: E402
from video_ai_editor.agent.prompt.schema import Postcondition, Step  # noqa: E402
from video_ai_editor.brain import resolve as R  # noqa: E402
from video_ai_editor.brain import store as BS  # noqa: E402
from video_ai_editor.brain import versions as V  # noqa: E402
from video_ai_editor.edl.schema import Clip  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")
pytestmark = pytest.mark.usefixtures("desktop_posture")



@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    """A store under a WORKDIR (so the validator and the preview scratch
    store find `<WORKDIR>/<sid>/brain/`), the hand graph + EDP written."""
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store = F.make_store(tmp_path, name="s_brain")
    bed = F.music_bed(tmp_path)
    src = _v1(store)[0].src
    doc = BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, bed=str(bed))
    facts = F.facts_for(store)
    return store, facts, src, doc


def _v1(store) -> list[Clip]:
    return [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]


def _sent(tool: str, arg: str, kind: str, **more) -> Step:
    return Step(tool=tool, args={arg: f"$brain:{kind}", "plan_ref": BF.DID, **more}, why=f"{kind} by the brain")


# ---------------------------------------------------------------- the table

def test_sentinel_pairs_and_stage2_order_are_the_frozen_ones():
    assert R.BRAIN_SENTINELS == {"cuts": ("cut_source_ranges", "ranges"), "keep": ("cut_source_ranges", "ranges"),
                                 "story_splits": ("split_at", "time"), "story_order": ("reorder_clips", "order"),
                                 "camera": ("apply_camera_plan", "switches"), "punch_ins": ("add_keyframe", "clip_id"),
                                 # FX-C2 (UX-03/UX-09): the captions decision lays ready-made cues
                                 "captions": ("add_caption_track", "cues")}
    assert R.STAGE2_ORDER == ("keep", "cuts", "story_splits", "story_order", "camera", "sync_dialogue_lane")
    assert R.MAX_BRAIN_FANOUT == 4096 and R.BRAIN_PREFIX == "$brain:"
    assert R.PLAN_REF_RE.fullmatch("d_0badf00d") and not R.PLAN_REF_RE.fullmatch("p_0badf00d")
    ranks = [R.stage2_rank(Step(tool=t, args=a, why="x")) for t, a in (
        ("sync_dialogue_lane", {"src": "/x.wav"}), ("apply_camera_plan", {"switches": "$brain:camera"}),
        ("reorder_clips", {"order": "$brain:story_order"}), ("split_at", {"time": "$brain:story_splits"}),
        ("cut_source_ranges", {"ranges": "$brain:cuts"}), ("cut_source_ranges", {"ranges": "$brain:keep"}))]
    assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == 6


# ---------------------------------------------------------------- the validator rule

def test_validator_accepts_declared_pairs_only(session):
    store, facts, src, _ = session
    ok = F.plan_of(_sent("split_at", "time", "story_splits", track="v1"),
                   _sent("reorder_clips", "order", "story_order", track="v1"),
                   _sent("add_keyframe", "clip_id", "punch_ins"))
    plan = validate.validate_plan(ok, facts)
    assert [s.tool for s in plan.steps] == ["split_at", "reorder_clips", "add_keyframe"]
    assert plan.steps[0].args["time"] == "$brain:story_splits" and plan.steps[0].args["plan_ref"] == BF.DID
    # an undeclared (tool, arg) pair, a kind on the wrong tool, a free string on a non-brain tool
    for bad in (F.plan_of(Step(tool="split_at", args={"track": "v1", "time": "$brain:cuts", "plan_ref": BF.DID}, why="x")),
                F.plan_of(Step(tool="cut_range", args={"track": "v1", "start": "$brain:cuts", "end": 3, "plan_ref": BF.DID}, why="x")),
                F.plan_of(Step(tool="add_text", args={"text": "$brain:cuts", "plan_ref": BF.DID}, why="x")),
                F.plan_of(Step(tool="reorder_clips", args={"track": "v1", "order": "$brain:nonsense", "plan_ref": BF.DID}, why="x"))):
        with pytest.raises(validate.PlanRejected, match=r"\$brain"):
            validate.validate_plan(bad, facts)


def test_validator_requires_a_real_plan_ref_and_a_current_graph(session, tmp_path):
    store, facts, src, _ = session
    with pytest.raises(validate.PlanRejected, match="plan_ref"):
        validate.validate_plan(F.plan_of(Step(tool="split_at", args={"track": "v1", "time": "$brain:story_splits"}, why="x")), facts)
    with pytest.raises(validate.PlanRejected, match="plan_ref"):
        validate.validate_plan(F.plan_of(Step(tool="split_at", args={"track": "v1", "time": "$brain:story_splits",
                                                                    "plan_ref": "d_zzzzzzzz"}, why="x")), facts)
    with pytest.raises(validate.PlanRejected, match="d_00000000"):
        validate.validate_plan(F.plan_of(Step(tool="split_at", args={"track": "v1", "time": "$brain:story_splits",
                                                                    "plan_ref": "d_00000000"}, why="x")), facts)
    # the same EDP under another session id is not this session's
    other = F.facts_for(store, session_id="s_other")
    with pytest.raises(validate.PlanRejected, match="plan_ref"):
        validate.validate_plan(F.plan_of(_sent("split_at", "time", "story_splits", track="v1")), other)


def test_stale_graph_refused_with_replan_question(session):
    store, facts, src, _ = session
    BS.set_current_graph(Path(store.dir), "g_ffffffffffff")
    with pytest.raises(validate.PlanRejected, match="replan"):
        validate.validate_plan(F.plan_of(_sent("split_at", "time", "story_splits", track="v1")), facts)
    with pytest.raises(R.StaleGraph) as ei:
        R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    q = ei.value.question
    assert q.key == "replan" and q.kind == "confirm" and q.required and q.default is None
    assert BF.GID in str(ei.value) and "g_ffffffffffff" in str(ei.value)
    BS.set_current_graph(Path(store.dir), BF.GID)
    args, _ = R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    assert isinstance(args, list)


def test_validator_path_rule_for_literal_brain_tool_args(session, tmp_path):
    store, facts, src, _ = session
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(b"RIFF")
    with pytest.raises(validate.PlanRejected, match="not offered"):
        validate.validate_plan(F.plan_of(Step(tool="sync_dialogue_lane", args={
            "src": str(outside), "lane": "a1", "offsets": {str(outside): 0.0}}, why="x")), facts)
    with pytest.raises(validate.PlanRejected, match="not offered"):
        validate.validate_plan(F.plan_of(Step(tool="cut_source_ranges", args={
            "track": "v1", "ranges": [{"src": str(outside), "start": 0.0, "end": 1.0}]}, why="x")), facts)
    # EB1 integration: a file already ON the timeline may be re-used by the brain's tools even when it is
    # not under the session's uploads (a clip added from ~/Movies by path) — and only such a file
    bare = facts.with_(allowed_paths=set())
    step = Step(tool="sync_dialogue_lane", args={"src": src, "lane": "a1", "offsets": {src: 0.0}}, why="x")
    with pytest.raises(validate.PlanRejected, match="not offered"):
        validate.validate_plan(F.plan_of(step), bare)
    on_timeline = bare.with_(timeline_paths={str(Path(src).resolve())})
    assert validate.validate_plan(F.plan_of(step), on_timeline).steps[0].tool == "sync_dialogue_lane"
    with pytest.raises(validate.PlanRejected, match="not offered"):
        validate.validate_plan(F.plan_of(Step(tool="sync_dialogue_lane", args={
            "src": str(outside), "lane": "a1", "offsets": {str(outside): 0.0}}, why="x")), on_timeline)
    with pytest.raises(validate.PlanRejected, match="2000"):
        validate.validate_plan(F.plan_of(Step(tool="cut_source_ranges", args={
            "track": "v1", "ranges": [{"src": src, "start": i, "end": i + 0.1} for i in range(2001)]}, why="x")), facts)
    with pytest.raises(validate.PlanRejected, match="16"):
        validate.validate_plan(F.plan_of(Step(tool="sync_dialogue_lane", args={
            "src": src, "lane": "a1", "offsets": {f"{src}{i}": 0.0 for i in range(17)}}, why="x")), facts)


# ---------------------------------------------------------------- the resolver

def test_cuts_resolve_through_timemap_after_prior_cuts(session):
    store, facts, src, doc = session
    args, notices = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID})
    assert isinstance(args, dict) and "plan_ref" not in args
    assert args["ranges"] == [{"src": src, "start": 0.6, "end": 0.9}, {"src": src, "start": 5.5, "end": 5.8},
                              {"src": src, "start": 3.0, "end": 5.3}, {"src": src, "start": 7.8, "end": 10.3}]
    assert notices == []
    # a prior cut in the same run removed the "uh" (5.4-5.9): that decision is dropped and SAID
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.4, "end": 5.9})
    args, notices = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID})
    assert [r["start"] for r in args["ranges"]] == [0.6, 3.0, 7.8]
    assert len(notices) == 1 and "1 cut" in notices[0] and "k_0002" in notices[0]
    # a decision straddling a cut edge keeps its full SOURCE span (the tool re-maps and clips it)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 3.5})   # timeline 3.0-3.5 = source ~3.0-3.5
    args, _ = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID})
    assert {"src": src, "start": 3.0, "end": 5.3} in args["ranges"]


def test_keep_resolves_to_the_complement_of_the_kept_window(session):
    store, facts, src, doc = session
    edp = json.loads(json.dumps(doc))
    edp["id"] = "d_00000001"
    edp["decisions"] = [BF._decision(1, "keep_window", "best_window", ["s_0002"], "keep the hook", ref=(5.0, 8.0)),
                        BF._decision(2, "keep_window", "best_window", ["s_0003"], "and the end", ref=(10.0, 11.8))]
    from video_ai_editor.brain.schema import EDP
    BS.write_edp(Path(store.dir), EDP.model_validate(edp))
    args, notices = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:keep", "plan_ref": "d_00000001"})
    assert args["ranges"] == [{"src": src, "start": 0.0, "end": 5.0}, {"src": src, "start": 8.0, "end": 10.0},
                              {"src": src, "start": 11.8, "end": 12.0}]


def test_story_order_resolves_against_post_split_tree(session):
    store, facts, src, _ = session
    before = [c.id for c in _v1(store)]
    splits, notices = R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    assert [a["time"] for a in splits] == [6.0, 7.6] and all(a["track"] == "v1" and "plan_ref" not in a for a in splits)
    for a in splits:
        D.dispatch(store, "split_at", a)
    pieces = _v1(store)
    assert len(pieces) == 3 and pieces[0].id == before[0]
    order, notices = R.resolve(store, "reorder_clips", {"track": "v1", "order": "$brain:story_order", "plan_ref": BF.DID})
    hook = next(c for c in pieces if abs(c.in_ - 6.0) < 1e-6 and abs(c.out - 7.6) < 1e-6)
    assert order["order"] == [hook.id] + [c.id for c in pieces if c is not hook]
    assert notices == []
    D.dispatch(store, "reorder_clips", order)
    assert _v1(store)[0].id == hook.id and _v1(store)[0].start == 0.0
    # resolved again on the reordered tree: already first, so the order is the identity (and said)
    order2, notices2 = R.resolve(store, "reorder_clips", {"track": "v1", "order": "$brain:story_order", "plan_ref": BF.DID})
    assert order2["order"] == [c.id for c in _v1(store)]
    # splits on a tree where the hook edges already are seams: nothing to split → None
    none, notices3 = R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    assert none is None and notices3


def test_punch_ins_resolve_to_clip_local_keys_and_a_dropped_decision_becomes_notice(session):
    store, facts, src, _ = session
    fan, notices = R.resolve(store, "add_keyframe", {"clip_id": "$brain:punch_ins", "plan_ref": BF.DID})
    cid = _v1(store)[0].id
    assert fan == [{"clip_id": cid, "props": ["scale"], "values": {"scale": 1.1}, "time": 6.0, "interp": "ease-out"}]
    # after the hook is split out and moved to the front, the key is clip-local 0 on the hook piece
    for t in (6.0, 7.6):
        D.dispatch(store, "split_at", {"track": "v1", "time": t})
    hook = next(c for c in _v1(store) if abs(c.in_ - 6.0) < 1e-6)
    D.dispatch(store, "reorder_clips", {"track": "v1", "order": [hook.id] + [c.id for c in _v1(store) if c is not hook]})
    fan, notices = R.resolve(store, "add_keyframe", {"clip_id": "$brain:punch_ins", "plan_ref": BF.DID})
    assert fan[0]["clip_id"] == hook.id and fan[0]["time"] == 0.0 and notices == []
    # the moment cut away earlier in the plan: dropped, with a notice naming the count
    D.dispatch(store, "ripple_delete", {"clip_id": hook.id})
    none, notices = R.resolve(store, "add_keyframe", {"clip_id": "$brain:punch_ins", "plan_ref": BF.DID})
    assert none is None
    assert len(notices) == 1 and "1 punch-in" in notices[0] and "cut away" in notices[0]


def test_dropped_decision_becomes_notice(session):
    store, facts, src, _ = session
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.9, "end": 7.7})   # the hook is gone
    none, notices = R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    assert none is None and any("k_0005" in n for n in notices)
    same, notices = R.resolve(store, "reorder_clips", {"track": "v1", "order": "$brain:story_order", "plan_ref": BF.DID})
    assert same is None and any("k_0005" in n for n in notices)


def test_fanout_cap_is_said(session, monkeypatch):
    store, facts, src, doc = session
    edp = json.loads(json.dumps(doc))
    edp["id"] = "d_00000002"
    edp["decisions"] = [BF._decision(i, "punch_in", "emphasis_peak", ["s_0001"], "peak", ref=(0.2 + i * 0.05, 0.5 + i * 0.05),
                                     params={"scale": 1.1, "interp": "ease-out"}) for i in range(1, 8)]
    from video_ai_editor.brain.schema import EDP
    BS.write_edp(Path(store.dir), EDP.model_validate(edp))
    monkeypatch.setattr(R, "MAX_BRAIN_FANOUT", 3)
    fan, notices = R.resolve(store, "add_keyframe", {"clip_id": "$brain:punch_ins", "plan_ref": "d_00000002"})
    assert len(fan) == 3
    assert len(notices) == 1 and "3" in notices[0] and "4" in notices[0] and "cap" in notices[0].lower()


def test_footprint_written(session):
    store, facts, src, _ = session
    R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID})
    R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    fp = json.loads((Path(store.dir) / "brain" / "footprint.json").read_text(encoding="utf-8"))
    assert fp["plan_ref"] == BF.DID
    cuts = fp["steps"]["cuts"]
    assert [d["decision"] for d in cuts] == ["k_0001", "k_0002", "k_0003", "k_0004"]
    assert cuts[0]["code"] == "filler" and cuts[0]["text"].startswith("filler") and cuts[0]["src_range"] == [0.6, 0.9]
    assert cuts[0]["timeline"] == [[0.6, 0.9]]
    # EB1 integration (lane F's request): a cut names the v1 clips it hit
    assert all(d["clip_ids"] == [_v1(store)[0].id] for d in cuts)
    assert R._lineage("c_627c3ffb_6b1f58_0a1b") == ["c_627c3ffb_6b1f58_0a1b", "c_627c3ffb_6b1f58", "c_627c3ffb"]
    splits = fp["steps"]["story_splits"]
    assert splits[0]["decision"] == "k_0005" and splits[0]["times"] == [6.0, 7.6]
    assert fp["dropped"] == []
    # the footprint is per run directory (a scratch store writes its own) and a second step merges in
    R.resolve(store, "add_keyframe", {"clip_id": "$brain:punch_ins", "plan_ref": BF.DID})
    fp = json.loads((Path(store.dir) / "brain" / "footprint.json").read_text(encoding="utf-8"))
    assert set(fp["steps"]) == {"cuts", "story_splits", "punch_ins"}
    assert fp["steps"]["punch_ins"][0]["clip_ids"] == [_v1(store)[0].id]


def test_live_branch_and_guard_last_line(session):
    store, facts, src, _ = session
    out, notices = live.resolve_live_args(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})
    assert isinstance(out, list) and [a["time"] for a in out] == [6.0, 7.6]
    # an ordinary step is untouched by the branch
    same, _ = live.resolve_live_args(store, "split_at", {"track": "v1", "time": 3.0})
    assert same == {"track": "v1", "time": 3.0}
    # a plan_ref with no sentinel is stripped, never handed to a handler
    stripped, _ = live.resolve_live_args(store, "split_at", {"track": "v1", "time": 3.0, "plan_ref": BF.DID})
    assert stripped == {"track": "v1", "time": 3.0}
    # the executor's last line refuses an unresolved sentinel and a malformed plan_ref
    with pytest.raises(executor.StepRefused, match=r"\$brain"):
        executor.guard_step("split_at", {"track": "v1", "time": "$brain:story_splits"}, facts)
    with pytest.raises(executor.StepRefused, match=r"\$brain"):
        executor.guard_step("add_keyframe", {"clip_id": "c_x", "values": {"scale": "$brain:punch_ins"}, "time": 0}, facts)
    with pytest.raises(executor.StepRefused, match="plan_ref"):
        executor.guard_step("split_at", {"track": "v1", "time": 3.0, "plan_ref": "nope"}, facts)
    executor.guard_step("split_at", {"track": "v1", "time": 3.0, "plan_ref": BF.DID}, facts)


# ---------------------------------------------------------------- Milestone A (brought forward)

def _hand_plan(src: str, bed: str, *, with_b: bool) -> tuple:
    """The hand compilation of the EDP: keep → cuts → story → dialogue (B) → captions → music."""
    steps = []
    if with_b:
        steps.append(_sent("cut_source_ranges", "ranges", "cuts", track="v1"))
    steps += [_sent("split_at", "time", "story_splits", track="v1"),
              _sent("reorder_clips", "order", "story_order", track="v1")]
    if with_b:
        steps.append(Step(tool="sync_dialogue_lane", args={"src": src, "lane": "a1", "offsets": {src: 0.0},
                                                            "seam_fade_s": 0.005, "mute_camera_mics": True},
                          why="dialogue on its own lane"))
    steps += [Step(tool="add_keyframe", args={"clip_id": "$brain:punch_ins", "plan_ref": BF.DID}, why="1 punch-in",
                   optional=True),
              Step(tool="add_caption_track", args={"style": "ig_chunky", "position": "bottom"}, why="Captions: Dynamic"),
              Step(tool="add_music", args={"src": bed, "volume_db": -20.0, "loop": True}, why="subtle bed")]
    pcs = [Postcondition(check="no_cut_mid_word", args={"tol": 0.02}, human="no cut lands inside a word"),
           Postcondition(check="dialogue_in_sync", args={}, human="the dialogue lane is in sync"),
           Postcondition(check="captions_cover", args={"min_ratio": 0.9}, human="captions cover the speech")]
    return F.plan_of(*steps, intent="edit", title="Reel: hand-compiled", postconditions=pcs)


def _run(store, plan, facts, **kw):
    return executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=threading.Event(),
                             prompt="make a 45-second reel", **kw)


def _round_trip(session, *, with_b: bool):
    store, facts, src, _ = session
    bed = next(iter(sorted(p for p in facts.allowed_paths if p.endswith("bed.wav"))))
    plan = validate.validate_plan(_hand_plan(src, bed, with_b=with_b), facts)
    assert plan.steps[0].stage == 2 and [s.tool for s in plan.steps][-1] == "add_music"
    hash_before, ops_before, depth_before = store.edl.hash(), len(store.ops.ops), store.undo_depth
    # dry run on the preview's scratch store: the live session is byte-identical afterwards
    live_bytes = (Path(store.dir) / "edl.json").read_bytes()
    scratch = preview.scratch_store(store, "r_test0001")
    try:
        dry = _run(scratch, plan, facts, dry_run=True)
        pmap = preview.path_map(scratch, store)
    finally:
        preview.discard_scratch(scratch)
    assert dry.error is None and dry.rollback is None and dry.dry_run and dry.after is not None
    assert (Path(store.dir) / "edl.json").read_bytes() == live_bytes and store.edl.hash() == hash_before
    assert len(store.ops.ops) == ops_before
    # apply on the live store: ONE op, decisions in the commit args, one undo step
    res = _run(store, plan, facts)
    assert res.error is None and res.rollback is None and res.committed
    assert len(store.ops.ops) == ops_before + 1 and store.undo_depth == depth_before + 1
    op = store.ops.last()
    assert op.tool == "prompt" and op.args["decisions"] == BF.DID and op.args["plan_id"] == plan.id
    # the dry run and Apply agree — `preview.apply_check`'s own measure (new ids renamed in document order)
    assert C.canonical(dry.after, res.edl_before, pmap) == C.canonical(store.edl, res.edl_before)
    # the two blocking checks held in-batch (else the run would have rolled back) and measure green after
    _verify_checks = _verify.verify_plan(store, plan, res, facts, emit=lambda e: None, render=False)["checks"]
    by = {c["check"]: c for c in _verify_checks}
    assert by["no_cut_mid_word"]["pass"] is not False, by["no_cut_mid_word"]
    assert by["dialogue_in_sync"]["pass"] is not False, by["dialogue_in_sync"]
    # the picture: the hook sentence opens the reel, keyed at its clause start
    first = _v1(store)[0]
    assert first.start == 0.0 and abs(first.in_ - BF.HOOK[0]) < 1e-6
    assert not isinstance(first.transform.scale, float) and first.transform.scale.keyframes[0][0] == 0.0
    assert store.edl.get_track("captions").clips and store.edl.get_track("music").clips
    # a version row, restorable, naming the EDP
    v = V.record_after_run(store, decisions_id=op.args["decisions"], title="Reel")
    assert v.label == "V1 Reel" and v.decisions_id == BF.DID and V.list_versions(store.dir)[0].restorable
    after = store.edl.model_copy(deep=True)
    assert store.undo() and store.edl.hash() == hash_before     # one ⌘Z restores the original
    return after, res, by


def test_round_trip_through_existing_tools(session):
    """The contract end to end with the tools main already has (split /
    reorder / keyframes / captions / music): what does not need lane B."""
    after, res, by = _round_trip(session, with_b=False)
    assert [s.tool for s in res.plan.steps if s.tool != "add_music"] == \
        ["split_at", "reorder_clips", "add_keyframe", "add_caption_track"]
    assert by["dialogue_in_sync"]["pass"] is None          # no dialogue step in this plan: unmeasured
    assert not [c for c in after.get_track("a1").clips if isinstance(c, Clip)]


def test_hand_compiled_edp_round_trip(session):
    """Milestone A: keep → cuts → sync_dialogue_lane → add_caption_track →
    add_music from the hand EDP, one op, decisions committed, a1 in sync."""
    after, res, by = _round_trip(session, with_b=True)
    assert by["no_cut_mid_word"]["pass"] is True and by["dialogue_in_sync"]["pass"] is True
    v1 = [c for c in after.get_track("v1").clips if isinstance(c, Clip)]
    a1 = [c for c in after.get_track("a1").clips if isinstance(c, Clip)]
    assert len(a1) == len(v1) >= 4 and all(p.audio.mute for p in v1)          # one a1 clip per v1 piece, mics muted
    assert [round(a.start, 4) for a in a1] == [round(p.start, 4) for p in v1]
    assert a1[0].audio.fade_in == 0.0 and a1[-1].audio.fade_out == 0.0
    assert all(a.audio.fade_out == 0.005 for a in a1[:-1]) and all(a.audio.fade_in == 0.005 for a in a1[1:])
    # the four cuts landed by SOURCE time: every planted filler and silence is gone, the hook opens
    kept = [(round(p.in_, 2), round(p.out, 2)) for p in v1]
    assert kept[0] == (6.0, 7.6)
    assert not any(a <= 0.7 < b or a <= 5.6 < b or a <= 4.0 < b or a <= 9.0 < b for a, b in kept)
    assert abs(after.duration - (12.0 - 0.3 - 0.3 - 2.3 - 2.5)) < 0.2


# ---------------------------------------------------------------- EB1 integration

def test_a_hook_in_several_pieces_moves_whole(session):
    """A removal inside the opening moment leaves it in two pieces; they go to the front together, in order."""
    store, facts, src, _ = session
    for a in R.resolve(store, "split_at", {"track": "v1", "time": "$brain:story_splits", "plan_ref": BF.DID})[0]:
        D.dispatch(store, "split_at", a)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 6.5, "end": 6.8})          # inside the hook (6.0-7.6)
    hook = [c for c in _v1(store) if c.in_ >= 6.0 - 1e-6 and c.out <= 7.6 + 1e-6]
    assert len(hook) == 2
    order, notices = R.resolve(store, "reorder_clips", {"track": "v1", "order": "$brain:story_order", "plan_ref": BF.DID})
    assert order["order"][:2] == [c.id for c in sorted(hook, key=lambda c: c.in_)] and notices == []
    assert sorted(order["order"]) == sorted(c.id for c in _v1(store))
    fp = R.read_footprint(store.dir)
    assert fp["steps"]["story_order"][0]["clip_ids"] == order["order"][:2]
    # a hook whose head was cut away is not "the opening moment": the order stays, and it is said
    D.dispatch(store, "ripple_delete", {"clip_id": min(hook, key=lambda c: c.in_).id})
    none, notices = R.resolve(store, "reorder_clips", {"track": "v1", "order": "$brain:story_order", "plan_ref": BF.DID})
    assert none is None and notices and "order unchanged" in notices[0]


def _cx(store, src: str, offsets: dict[str, float]) -> "R._Ctx":
    edp = R.load_edp(R.brain_dir_for(store), BF.DID)
    return R._Ctx(store, edp, {}, {}, offsets)


def test_a_key_that_opens_a_piece_lands_on_it_at_local_zero(session):
    """A jump-cut hide sits exactly ON the seam; the piece's in-point is the
    frame-quantised seam (measured on TH: 7 of 8 hides were dropped as "cut
    away" because 15.0 is not inside a piece that starts at 15.0)."""
    store, facts, src, _ = session
    D.dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 4.0})
    incoming = _v1(store)[1]
    cx = _cx(store, src, {})
    for t in (incoming.in_, incoming.in_ - 0.01, incoming.in_ - 1 / 30):
        hit = R._key_clip(cx, src, t, opens=True)
        assert hit is not None and hit[0].id == incoming.id and hit[1] == 0.0, t
    assert R._key_clip(cx, src, 3.5, opens=False) is None                     # a moment inside the removal
    assert R._key_clip(cx, src, 3.5, opens=True) is None
    mid = R._key_clip(cx, src, incoming.in_ + 1.0, opens=False)
    assert mid[0].id == incoming.id and mid[1] == pytest.approx(1.0, abs=1e-4)


def test_a_key_follows_its_moment_onto_the_other_angle(session, tmp_path):
    """After a camera plan the piece that shows a moment may play another
    angle's file: `file_t = ref_t + offset[file]`."""
    store, facts, src, _ = session
    other = str(tmp_path / "cam_b.mp4")
    shutil.copyfile(src, other)
    offsets = {src: 0.35, other: -0.20}
    D.dispatch(store, "apply_camera_plan", {"switches": [{"src": src, "at_src": 4.0, "until_src": 8.0, "angle_src": other}],
                                            "offsets": offsets})
    piece = next(c for c in _v1(store) if str(c.src) == other)
    assert piece.in_ == pytest.approx(4.0 - 0.55, abs=1 / 30)
    cx = _cx(store, src, offsets)
    assert cx.same_moment(src, 5.0) == [(src, 5.0), (other, 4.45)]
    hit = R._key_clip(cx, src, 5.0, opens=False)                               # primary second 5.0 = reference 4.65
    assert hit is not None and hit[0].id == piece.id
    assert hit[1] == pytest.approx(4.45 - piece.in_, abs=1e-3)
    assert R._key_clip(_cx(store, src, {}), src, 5.0, opens=False) is None     # without the offsets it is simply gone
    # a moment still on the first angle is found there
    first = R._key_clip(cx, src, 2.0, opens=False)
    assert str(first[0].src) == src and first[1] == pytest.approx(2.0, abs=1e-3)


# ---------------------------------------------------------------- FX-B: SC-02, SC-16, SC-05, UX-13

def _write_edp(store, doc: dict, did: str, decisions: list[dict]) -> str:
    from video_ai_editor.brain.schema import EDP
    edp = json.loads(json.dumps(doc))
    edp["id"], edp["decisions"] = did, decisions
    BS.write_edp(Path(store.dir), EDP.model_validate(edp))
    return did


def _long_timeline(store, seconds: float) -> None:
    """The 12 s stand-in stretched to `seconds` on the EDL only (no render, no media read)."""
    clip = _v1(store)[0]
    clip.out = seconds
    store.edl.recompute_duration()
    store.commit("init", {}, "stretch")


def _covered(ranges: list[dict]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for r in sorted(ranges, key=lambda r: r["start"]):
        if out and abs(out[-1][1] - r["start"]) < 1e-6:
            out[-1] = (out[-1][0], r["end"])
        else:
            out.append((r["start"], r["end"]))
    return out


def test_keep_over_45_minutes_resolves_in_pieces_the_tool_accepts(session):
    """SC-02: `$brain:keep` used to emit the complement as-is (0-1000 s and 1045-2700 s); the tool refuses
    a range over 600 s, so a reel from a long recording failed at dispatch."""
    store, facts, src, doc = session
    _long_timeline(store, 2700.0)
    did = _write_edp(store, doc, "d_00000045", [
        BF._decision(1, "keep_window", "best_window", ["s_0002"], "the best 45 s", ref=(1000.0, 1045.0))])
    dispatches, notices = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:keep", "plan_ref": did})
    dispatches = dispatches if isinstance(dispatches, list) else [dispatches]
    rows = [r for d in dispatches for r in d["ranges"]]
    assert rows and all(r["end"] - r["start"] <= _tools.CUT_RANGE_MAX_S for r in rows), [r for r in rows if r["end"] - r["start"] > 600]
    assert _covered(rows) == [(0.0, 1000.0), (1045.0, 2700.0)]          # the union is unchanged, in order
    assert [r["start"] for r in rows] == sorted(r["start"] for r in rows)
    for d in dispatches:                                               # what the executor's guard and the tool see
        executor.guard_step("cut_source_ranges", d, facts)
        D.dispatch(store, "cut_source_ranges", d)
    assert store.edl.duration == pytest.approx(45.0, abs=0.1)
    kept = _v1(store)
    assert len(kept) == 1 and (kept[0].in_, kept[0].out) == pytest.approx((1000.0, 1045.0), abs=0.05)


def test_cuts_longer_than_the_cap_are_split_too_and_the_fanout_cap_still_chunks(session, monkeypatch):
    store, facts, src, doc = session
    _long_timeline(store, 2700.0)
    did = _write_edp(store, doc, "d_00000046", [
        BF._decision(1, "cut_range", "dead_air", ["sil_0001"], "a 25 minute silence", ref=(100.0, 1600.0))])
    args, _ = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": did})
    rows = args["ranges"]
    assert len(rows) == 3 and all(r["end"] - r["start"] <= 600 for r in rows) and _covered(rows) == [(100.0, 1600.0)]
    monkeypatch.setattr(R, "MAX_RANGES_PER_DISPATCH", 2)                  # the count cap chunks AFTER the split
    fan, _ = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": did})
    assert [len(d["ranges"]) for d in fan] == [2, 1] and _covered([r for d in fan for r in d["ranges"]]) == [(100.0, 1600.0)]
    for d in fan:
        D.dispatch(store, "cut_source_ranges", d)
    assert store.edl.duration == pytest.approx(2700.0 - 1500.0, abs=0.1)


def test_a_piece_never_exceeds_the_cap_on_awkward_floats(session):
    for a, b in ((0.1, 600.1), (3.3333, 1803.3333), (0.0, 600.0), (12.0, 12.5), (0.0, 1e-3)):
        pieces = R.split_range(a, b)
        assert pieces[0][0] == a and pieces[-1][1] == b and all(q - p <= _tools.CUT_RANGE_MAX_S for p, q in pieces)
        assert all(abs(pieces[i][1] - pieces[i + 1][0]) < 1e-9 for i in range(len(pieces) - 1))


def test_the_reel_plan_resolves_and_dispatches_on_a_45_minute_timeline(session):
    """SC-02, end to end on the executor: an EDL of 45 minutes and a graph dict; the compiled reel steps
    (keep, cuts, splits, order, dialogue) validate and run — nothing renders."""
    store, facts, src, doc = session
    _long_timeline(store, 2700.0)
    facts = F.facts_for(store)
    did = _write_edp(store, doc, "d_00000047", [
        BF._decision(1, "keep_window", "best_window", ["s_0002"], "the hook and what follows", ref=(1000.0, 1045.0)),
        BF._decision(2, "cut_range", "silence", ["sil_0001"], "silence of 2.0 s", ref=(1010.0, 1012.0)),
        BF._decision(3, "open_on", "hook_strongest_opening", ["s_0002"], "strongest opening", ref=(1020.0, 1030.0))])
    plan = F.plan_of(_sent("cut_source_ranges", "ranges", "keep", track="v1"),
                     _sent("cut_source_ranges", "ranges", "cuts", track="v1"),
                     _sent("split_at", "time", "story_splits", track="v1"),
                     _sent("reorder_clips", "order", "story_order", track="v1"), intent="edit", title="Reel: 45 minutes")
    for s in plan.steps:
        s.args["plan_ref"] = did
    plan = validate.validate_plan(plan, facts)
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=threading.Event(), prompt="make a 45-second reel")
    assert res.error is None and res.rollback is None and res.committed, res.error
    assert store.edl.duration == pytest.approx(45.0 - 2.0, abs=0.2)
    assert _v1(store)[0].in_ == pytest.approx(1020.0, abs=0.05)             # the hook opens


GHOST = "src_" + "b" * 24            # a source key the graph does not know: not on this timeline


def test_a_dropped_decision_is_reported_with_its_own_reason(session):
    """SC-16: every drop used to read 'already removed earlier in this plan', a wrong reason for a file
    that was never on the timeline."""
    store, facts, src, doc = session
    d = [BF._decision(1, "cut_range", "filler", ["w_0002"], "filler", ref=(0.6, 0.9)),
         BF._decision(2, "cut_range", "filler", ["w_0006"], "filler", ref=(5.5, 5.8)),
         BF._decision(3, "cut_range", "silence", ["sil_0001"], "silence", ref=(3.0, 5.3)),
         BF._decision(4, "punch_in", "emphasis_peak", ["s_0002"], "peak", ref=(6.0, 7.0), params={"scale": 1.1}),
         BF._decision(5, "punch_in", "emphasis_peak", ["s_0002"], "peak", ref=(6.0, 7.0), params={"scale": 1.1})]
    d[2]["ref"]["src"] = GHOST
    d[4]["ref"]["src"] = GHOST
    did = _write_edp(store, doc, "d_00000048", d)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.4, "end": 5.9})          # k_0002 is already gone
    args, notices = R.resolve(store, "cut_source_ranges", {"track": "v1", "ranges": "$brain:cuts", "plan_ref": did})
    assert [r["start"] for r in args["ranges"]] == [0.6]
    said = {n for n in notices}
    gone = next(n for n in said if "k_0002" in n)
    ghost = next(n for n in said if "k_0003" in n)
    assert "already removed" in gone and "k_0003" not in gone and "not on this timeline" not in gone
    assert "not on this timeline" in ghost and "already removed" not in ghost and "k_0002" not in ghost
    fp = R.read_footprint(store.dir)
    assert {x["decision"]: x["why"] for x in fp["dropped"]}["k_0003"].endswith("is not on this timeline")
    # the punch-ins say the same: one moment cut away, one file missing
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.4, "end": 7.9})       # source 5.9-8.4: the hook is gone
    none, notices = R.resolve(store, "add_keyframe", {"clip_id": "$brain:punch_ins", "plan_ref": did})
    assert none is None
    text = " | ".join(notices)
    assert "k_0004" in text and "cut away" in text and "k_0005" in text and "not on this timeline" in text
    assert not any("k_0005" in n and "cut away" in n for n in notices)


# ---------------------------------------------------------------- SC-05: the path rule on what a sentinel RESOLVES

def _outside(tmp_path: Path) -> str:
    p = tmp_path / "outside_private.mp4"
    p.write_bytes(b"\0" * 32)
    return str(p)


def test_the_last_line_guard_refuses_an_unoffered_path_in_resolved_args(session, tmp_path):
    """SC-05: `guard_step` walked only top-level path args; the nested `ranges[].src`,
    `switches[].src/angle_src` and `offsets` keys of the three brain tools were never checked at run time."""
    store, facts, src, _ = session
    out = _outside(tmp_path)
    cut = {"track": "v1", "ranges": [{"src": out, "start": 0.0, "end": 1.0}]}
    cam = {"switches": [{"src": src, "at_src": 1.0, "until_src": 3.0, "angle_src": out}], "offsets": {src: 0.0, out: 0.1}}
    dlg = {"src": src, "lane": "a1", "offsets": {out: 0.0}}
    for tool, args, what in (("cut_source_ranges", cut, "ranges[0].src"), ("apply_camera_plan", cam, "angle_src"),
                             ("sync_dialogue_lane", dlg, "offsets")):
        with pytest.raises(executor.StepRefused, match=r"not offered \(outside_private\.mp4\)") as ei:
            executor.guard_step(tool, args, facts)
        assert what in str(ei.value)
    # the same tools with offered files, and a file already on the timeline, pass
    executor.guard_step("cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 0.0, "end": 1.0}]}, facts)
    executor.guard_step("sync_dialogue_lane", {"src": src, "lane": "a1", "offsets": {src: 0.0}}, facts)
    # bounds too: a range over the tool's cap and a reversed switch are refused before a handler sees them
    with pytest.raises(executor.StepRefused, match="may not exceed"):
        executor.guard_step("cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 0.0, "end": 900.0}]}, facts)
    with pytest.raises(executor.StepRefused, match="until_src must be after at_src"):
        executor.guard_step("apply_camera_plan", {"switches": [{"src": src, "at_src": 3.0, "until_src": 1.0, "angle_src": src}]}, facts)


def _tampered(session, tmp_path, *, kind: str, target: str, did: str = "d_0000baad") -> tuple:
    """The session's own EDP, rewritten so one decision names `target` (a file the user never offered)."""
    store, facts, src, doc = session
    if kind == "camera":
        d = BF._decision(1, "switch_angle", "speaker_turn", ["s_0002"], "cut to the guest", ref=(6.0, 7.0), params={"angle": target})
    else:
        d = BF._decision(1, "cut_range", "silence", ["sil_0001"], "silence", ref=(3.0, 5.3))
        d["ref"]["src"] = target
    did = _write_edp(store, doc, did, [d])
    return did, store, facts, src


def test_a_tampered_edp_naming_an_unoffered_file_is_refused_at_both_ends(session, tmp_path):
    """SC-05: validate's rule covers what a sentinel DECLARES, the guard what it RESOLVES."""
    store, facts, src, _ = session
    out = _outside(tmp_path)
    did, store, facts, src = _tampered(session, tmp_path, kind="camera", target=out)
    step = _sent("apply_camera_plan", "switches", "camera", offsets={src: 0.0})
    step.args["plan_ref"] = did
    with pytest.raises(validate.PlanRejected, match=r"not offered \(outside_private\.mp4\)"):
        validate.validate_plan(F.plan_of(step), facts)                    # 1. the plan never validates
    args, _ = R.resolve(store, "apply_camera_plan", {"switches": "$brain:camera", "plan_ref": did, "offsets": {src: 0.0}})
    assert args["switches"][0]["angle_src"] == out                        # 2. the resolver alone would have passed it on ...
    with pytest.raises(executor.StepRefused, match="not offered"):
        executor.guard_step("apply_camera_plan", args, facts)             #    ... the last line does not
    with pytest.raises(validate.PlanRejected, match="not offered"):       # 3. and a tampered GRAPH file: the source key maps to /etc/passwd
        did2, store, facts, src = _tampered(session, tmp_path, kind="cut", target=GHOST, did="d_0000baae")
        graph = json.loads((Path(store.dir) / "brain" / "graph" / f"{BF.GID}.json").read_text(encoding="utf-8"))
        graph["sources"].append({**graph["sources"][0], "key": GHOST, "path": out})
        (Path(store.dir) / "brain" / "graph" / f"{BF.GID}.json").write_text(json.dumps(graph), encoding="utf-8")
        cut = _sent("cut_source_ranges", "ranges", "cuts", track="v1")
        cut.args["plan_ref"] = did2
        validate.validate_plan(F.plan_of(cut), facts)


def test_a_run_with_a_tampered_edp_commits_nothing(session, tmp_path):
    store, facts, src, _ = session
    out = _outside(tmp_path)
    did, store, facts, src = _tampered(session, tmp_path, kind="camera", target=out)
    step = _sent("apply_camera_plan", "switches", "camera", offsets={src: 0.0})
    step.args["plan_ref"] = did
    plan = F.plan_of(step)
    before = (store.edl.hash(), len(store.ops.ops))
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=threading.Event(), prompt="x",
                            validator=lambda p, f: p)                     # even a validator that said yes
    assert res.error and "not offered" in res.error and not res.committed
    assert (store.edl.hash(), len(store.ops.ops)) == before


# ---------------------------------------------------------------- UX-13: what the op text says

def test_reorder_clips_says_what_moved(session, monkeypatch):
    """UX-13: 'Reorder V1:,,,,,,,,,' — the summary joined every clip id (the details view drops ids and
    left the commas). With the brain on it counts what moved and names the new opener instead."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    store, facts, src, _ = session
    for t in (4.0, 8.0):
        D.dispatch(store, "split_at", {"track": "v1", "time": t})
    a, b, c = (x.id for x in _v1(store))
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [c, a, b]})
    assert res["summary"] == "Reordered 1 clip on v1: what was clip 3 now plays first"
    assert store.ops.ops[-1].summary == res["summary"] and "c_" not in res["summary"] and ",," not in res["summary"]
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [a, c, b]})       # the opener returns; c moved behind it
    assert res["summary"] == "Reordered 1 clip on v1: what was clip 2 now plays first"
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [a, b, c]})       # the opener is unchanged: nothing is said about it
    assert res["summary"] == "Reordered 1 clip on v1"
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [c, b, a]})
    assert res["summary"] == "Reordered 2 clips on v1: what was clip 3 now plays first"
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [c, b, a]})
    assert res["summary"] == "Reorder v1: the order did not change"


def test_reorder_clips_op_text_is_the_0_8_0_text_with_the_brain_off(session, monkeypatch):
    """Closer N-24 (1): with `brain.enabled` off the History line is byte-identical to 0.8.0's: the ids, in order."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    store, facts, src, _ = session
    for t in (4.0, 8.0):
        D.dispatch(store, "split_at", {"track": "v1", "time": t})
    a, b, c = (x.id for x in _v1(store))
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [c, a, b]})
    assert res["summary"] == f"Reorder v1: {c}, {a}, {b}" and store.ops.ops[-1].summary == res["summary"]
    res = D.dispatch(store, "reorder_clips", {"track": "v1", "order": [c, a, b]})        # unchanged order: still the 0.8.0 line
    assert res["summary"] == f"Reorder v1: {c}, {a}, {b}"
