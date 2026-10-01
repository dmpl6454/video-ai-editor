"""The `edit` recipe's expander (agent/prompt/brain_expanders.py) and the
facts it reads: brain off → the old expansion; on without a graph → the
analysis gate; on with a graph → the EDP written through lane C's store and
compiled into steps; `expand_auto_edit` delegates only with a graph."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402
import prompt_fixtures as F  # noqa: E402
from gen_brain_goldens import TH_KEY, th_graph  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor import config  # noqa: E402
from video_ai_editor.agent.prompt import brain_expanders as BX  # noqa: E402
from video_ai_editor.agent.prompt.expanders import expand_auto_edit  # noqa: E402
from video_ai_editor.agent.prompt.facts import TimelineFacts, build_facts  # noqa: E402
from video_ai_editor.agent.prompt.recipes import Context, Intent  # noqa: E402
from video_ai_editor.brain import schema as S  # noqa: E402
from video_ai_editor.brain import store as ST  # noqa: E402

CTX = Context(recipes=frozenset({"edit"}), exclusions=frozenset(), has_cut_steps=True)


def _install_graph(sdir: Path, src_path: str) -> str:
    """Lane C's layout: header + current.json under <session>/brain, layers under WORKDIR/analysis/<key>/."""
    graph = th_graph()
    for s in graph["graph"]["sources"]:
        s["path"] = src_path
    graph["angles"]["members"][0]["path"] = src_path
    ST.write_graph(sdir, S.Graph.model_validate(graph["graph"]))
    ST.write_json(ST.brain_dir(sdir) / "angles.json", graph["angles"])
    ST.write_json(ST.brain_dir(sdir) / "scenes.json", graph["scenes"])
    for name, rel in graph["graph"]["layers"].items():
        layer, params = rel.split("/")
        ST.write_layer(TH_KEY, layer, params.removesuffix(".json"), graph["layers"][name])
    ST.set_current_graph(sdir, graph["graph"]["id"])
    return graph["graph"]["id"]


def test_brain_off_is_the_old_expansion(monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    f = TimelineFacts.minimal(duration=120.0, has_transcript=True, words=100, speech_seconds=80.0)
    x = BX.x_edit(Intent("edit", {}, clause="tighten this podcast"), f, CTX)
    assert not x.steps and not x.questions and [p.recipe for p in x.prerequisites] == ["tighten"]
    x = BX.x_edit(Intent("edit", {}, clause="make a 45-second reel"), f, CTX)
    names = [p.recipe for p in x.prerequisites]
    assert "tighten" in names and "reframe" in names and "captions" in names and names[-1] == "_audit"
    assert any(p.recipe == "trim" and p.get("_max_s") == 45.0 for p in x.prerequisites), names
    # SC-04: flag off, the reply never names the hidden feature
    assert not any("Editor Brain" in n for n in BX.x_edit(Intent("edit", {}, clause="tighten this podcast"), f, CTX).notes)
    assert not any("Editor Brain" in n for n in x.notes)


def test_brain_on_without_a_graph_asks_the_analysis_gate(monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    f = TimelineFacts.minimal(has_transcript=True, words=100, speech_seconds=20.0)
    x = BX.x_edit(Intent("edit", {}, clause="make a 45-second reel"), f, CTX)
    assert not x.steps and len(x.questions) == 1
    q = x.questions[0]
    assert q.key == BX.ANALYSIS_GATE_KEY and q.required and q.default is None
    assert [o.value for o in q.options] == ["read", "abort"]


def test_controls_from_the_clause():
    f = TimelineFacts.minimal()
    c = BX.controls_from_intent(Intent("edit", {}, clause="make an engaging 45-second instagram reel"), f, CTX)
    assert c["duration_s"] == 45.0 and c["platform"] == "reels" and c["ratio"] == "9:16"
    c = BX.controls_from_intent(Intent("edit", {}, clause="tighten this podcast like a premium podcast"), f, CTX)
    assert c["content_type"] == "podcast" and "duration_s" not in c
    no_caps = Context(recipes=frozenset({"edit"}), exclusions=frozenset({"captions"}), has_cut_steps=True)
    assert BX.controls_from_intent(Intent("edit", {}, clause="make a 30 second reel"), f, no_caps)["captions"] == "off"


def test_brain_on_with_a_graph_writes_the_edp_and_compiles(tmp_path, monkeypatch):
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    src = tmp_path / "s_brain" / "uploads" / "talking_head.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\0" * 64)
    gid = _install_graph(tmp_path / "s_brain", str(src))
    from video_ai_editor.agent.prompt.presets import music_beds
    f = TimelineFacts.minimal(session_id="s_brain", duration=74.0, has_transcript=True, words=300, speech_seconds=60.0,
                              allowed_paths={str(src)} | {str(b.path) for b in music_beds()}, brain_graph_id=gid,
                              brain_layers={"speech": "ok"})
    x = BX.x_edit(Intent("edit", {}, clause="make a 45-second reel"), f, CTX)
    tools = [s.tool for s in x.steps]
    assert tools[0] == "cut_source_ranges" and "sync_dialogue_lane" in tools and tools[-1] == "audit_aesthetic"
    ref = next(s.args["plan_ref"] for s in x.steps if "plan_ref" in s.args)
    on_disk = ST.read_edp(tmp_path / "s_brain", ref)
    assert on_disk is not None and on_disk.graph.id == gid and on_disk.summary.target == "reel"
    assert on_disk.summary.hook is not None and on_disk.summary.hook.sent == "s_00005"
    # the same request again names the same decisions file (content-addressed; only `created` may differ)
    again = BX.x_edit(Intent("edit", {}, clause="make a 45-second reel"), f, CTX)
    assert next(s.args["plan_ref"] for s in again.steps if "plan_ref" in s.args) == ref
    later = {**on_disk.model_dump(), "created": "2031-01-01T00:00:00Z"}
    assert BX._write_edp(tmp_path / "s_brain", later) == [f"decisions {ref} already on file; reused"]
    assert ST.read_edp(tmp_path / "s_brain", ref).created == on_disk.created
    assert any(n.startswith("opens on") for n in x.notes)
    assert all(len(s.why) <= 160 for s in x.steps) and len(x.steps) <= 24


def test_expand_auto_edit_delegates_only_with_a_graph():
    it = Intent("auto_edit", {"platform": "reels", "_ratio": "9:16", "_duration_s": 45.0}, clause="make a reel")
    plain = TimelineFacts.minimal(has_transcript=True, words=100, speech_seconds=20.0)
    assert "edit" not in [i.recipe for i in expand_auto_edit(it, plain, frozenset())]
    with_graph = plain.with_(brain_graph_id="g_0123456789ab")
    out = expand_auto_edit(it, with_graph, frozenset())
    assert [i.recipe for i in out] == ["edit"]
    assert out[0].get("platform") == "reels" and out[0].get("_duration_s") == 45.0
    assert "edit" not in [i.recipe for i in expand_auto_edit(it, with_graph, frozenset({"edit"}))]


def test_build_facts_reads_the_current_graph_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    src = F.speech_clip(tmp_path)
    F.write_ingest(src)
    store = F.make_store(tmp_path, src=src, name="s_facts")
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    facts = build_facts(store, {}, feature_report={})
    assert facts.brain_graph_id is None and facts.brain_layers == {} and facts.dialogue_lane is None
    gid = _install_graph(tmp_path / "s_facts", str(src))
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    facts = build_facts(store, {}, feature_report={})
    assert facts.brain_graph_id == gid and facts.brain_layers.get("speech") == "ok"
    # a graph whose source moved is stale: no graph, the gate asks again
    header = json.loads(ST.graph_path(tmp_path / "s_facts", gid).read_text())
    header["sources"][0]["path"] = str(tmp_path / "gone.mp4")
    ST.graph_path(tmp_path / "s_facts", gid).write_text(json.dumps(header))
    assert build_facts(store, {}, feature_report={}).brain_graph_id is None


@pytest.mark.parametrize("phrase", ["make a 45-second reel", "tighten this podcast", "edit this like a premium podcast"])
def test_grammar_routes_the_edit_family(phrase, monkeypatch):
    from video_ai_editor.agent.prompt import grammar as G
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    assert [h.intent for h in G.detect(phrase).hits] == ["edit"]


# --------------------------------------------------------------------------
# FX-D: a timeline the brain already edited (EX-02 / UX-04), the dialogue lane
# fact (SC-12 / EX-06), "sync the dialogue", the stale-lane line, the length
# --------------------------------------------------------------------------

@pytest.fixture
def brain_session(tmp_path, monkeypatch):
    """A one-clip session with the hand graph + EDP on disk, brain ON."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    from video_ai_editor import storage
    from video_ai_editor.agent.prompt import service
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, name="s_bx")
    src = next(c for c in store.edl.get_track("v1").clips).src
    BF.write_brain_files(Path(store.dir), src, workdir=tmp_path, current=False)
    return store, src


def _footage_read(store) -> None:
    """The analysis gate answered: the session's current graph is the hand graph."""
    from video_ai_editor.brain import store as ST
    ST.set_current_graph(Path(store.dir), BF.GID)


def _apply_brain_run(store, src: str) -> None:
    """What an applied brain plan leaves: ONE `prompt` op carrying the decisions id (cuts, then the
    dialogue lane last) and the version row the service records."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.brain import versions as VERS
    with store.batch():
        dispatch(store, "cut_source_ranges", {"track": "v1", "ranges": [{"src": src, "start": 3.0, "end": 5.3}]})
        dispatch(store, "sync_dialogue_lane", {"src": src, "lane": "a1", "offsets": {src: 0.0}, "seam_fade_s": 0.005})
    store.commit("prompt", {"prompt": "make a 10-second reel", "plan_id": "p_fx", "decisions": BF.DID,
                            "steps": ["cut_source_ranges", "sync_dialogue_lane"]}, "Prompt: Edit (2 steps)")
    VERS.record_after_run(store, decisions_id=BF.DID, title="Reel")


def _facts(store):
    from video_ai_editor.agent.prompt.facts import build_facts
    return build_facts(store, {}, feature_report={})


def _plan(store, prompt: str):
    from video_ai_editor.agent.prompt import planner
    return planner.plan(prompt, _facts(store))


def test_facts_report_the_lane_and_whether_it_still_follows_the_picture(brain_session, monkeypatch):
    """SC-12: `in_sync` is measured (brain.checks on the live edl), not always None."""
    from video_ai_editor.agent.dispatch import dispatch
    store, src = brain_session
    _footage_read(store)
    assert _facts(store).dialogue_lane is None and _facts(store).brain_edit is None    # a fresh import
    _apply_brain_run(store, src)
    lane = _facts(store).dialogue_lane
    assert lane is not None and lane.in_sync is True and lane.offsets == {src: 0.0}
    piece = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)[0]
    dispatch(store, "ripple_delete", {"clip_id": piece.id})                                # a hand edit
    assert _facts(store).dialogue_lane.in_sync is False
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")                                           # SC-12: flag off computes nothing
    off = _facts(store)
    assert off.dialogue_lane is None and off.brain_edit is None and off.brain_graph_id is None and not off.timeline_paths


def test_a_detached_sound_on_a1_is_not_a_stale_dialogue_lane(brain_session):
    from video_ai_editor.agent.dispatch import dispatch
    store, src = brain_session
    clip = next(iter(store.edl.get_track("v1").clips))
    dispatch(store, "detach_audio", {"clip_id": clip.id})
    f = _facts(store)
    assert f.dialogue_lane is not None and f.dialogue_lane.in_sync is None and f.brain_edit is None


def test_a_second_run_on_a_timeline_the_brain_edited_is_refused_in_one_sentence(brain_session):
    """EX-02 / UX-04: never a plan that cuts footage that is already cut."""
    store, src = brain_session
    _footage_read(store)
    _apply_brain_run(store, src)
    before = store.edl.hash()
    f = _facts(store)
    assert f.brain_edit is not None and f.brain_edit.state == "brain_edited" and f.brain_edit.label == "V1 Reel"
    for prompt in ("make a 10-second reel", "tighten this podcast", "premium podcast", "edit this"):
        p = _plan(store, prompt)
        assert not p.steps and not p.needs_input, (prompt, p.intent)
        assert p.reply == BX.ALREADY_EDITED.format(label="V1 Reel"), p.reply
        assert "⌘Z" in p.reply and "versions list" in p.reply
    assert store.edl.hash() == before


def test_a_hand_edit_after_a_brain_run_asks_instead_of_planning_over_it(brain_session):
    from video_ai_editor.agent.dispatch import dispatch
    store, src = brain_session
    _footage_read(store)
    _apply_brain_run(store, src)
    piece = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)[0]
    dispatch(store, "ripple_delete", {"clip_id": piece.id})
    f = _facts(store)
    assert f.brain_edit is not None and f.brain_edit.state == "edited_after" and f.brain_edit.label == "V1 Reel"
    p = _plan(store, "make a 10-second reel")
    assert not p.steps and p.reply == BX.EDITED_AFTER.format(label="V1 Reel")


def test_undoing_the_brain_run_or_a_fresh_import_plans_normally(brain_session):
    """The refusal is about what the timeline IS: a fresh import, and a timeline undone back to one, ask the gate / plan."""
    store, src = brain_session
    p = _plan(store, "make a 10-second reel")
    assert p.intent == "edit" and p.needs_input and p.needs_input[0].key == "gate_analysis"      # brain on, no graph read yet
    _footage_read(store)
    fresh = _plan(store, "make a 10-second reel")
    assert fresh.steps and fresh.steps[0].tool == "cut_source_ranges" and not fresh.needs_input
    _apply_brain_run(store, src)
    assert _plan(store, "make a 10-second reel").reply == BX.ALREADY_EDITED.format(label="V1 Reel")
    assert store.undo() is not None                                                             # ⌘Z: the run is gone
    assert _facts(store).brain_edit is None
    assert _plan(store, "make a 10-second reel").steps


def test_the_footprint_without_a_version_row_still_refuses(brain_session):
    """A version row that never got written (it is a courtesy): the a1 lane of an analysed source is the brain's footprint."""
    from video_ai_editor.agent.dispatch import dispatch
    store, src = brain_session
    _footage_read(store)
    dispatch(store, "sync_dialogue_lane", {"src": src, "lane": "a1", "offsets": {src: 0.0}})
    f = _facts(store)
    assert f.brain_edit is not None and f.brain_edit.state == "brain_traces" and f.brain_edit.label is None
    p = _plan(store, "make a 10-second reel")
    assert not p.steps and p.reply == BX.ALREADY_EDITED_NO_VERSION


def test_sync_the_dialogue_relays_a_stale_lane_and_the_stale_line_leads_there(brain_session):
    """EX-06: after a hand edit the lane is stale; any other Prompt-bar edit says so in one line,
    "sync the dialogue" rebuilds the lane (the only way to fix it by hand this wave)."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import planner
    store, src = brain_session
    _footage_read(store)
    _apply_brain_run(store, src)
    assert _plan(store, "sync the dialogue").reply and not _plan(store, "sync the dialogue").steps   # already in step
    piece = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)[0]
    dispatch(store, "ripple_delete", {"clip_id": piece.id})
    other = _plan(store, "add captions")
    assert other.steps and planner.STALE_LANE_NOTE in (other.reply or ""), other.reply
    assert planner.STALE_LANE_NOTE == "The dialogue lane no longer follows the picture — ask me to re-sync it"
    p = _plan(store, "sync the dialogue")
    assert [s.tool for s in p.steps] == ["sync_dialogue_lane"] and p.steps[0].args["offsets"] == {src: 0.0}
    assert planner.STALE_LANE_NOTE not in (p.reply or "")
    from video_ai_editor.agent.prompt import service
    events = F.collect(service.prompt_turn(store, "sync the dialogue", [], brain="recipes"))
    card = next(e for e in events if e["type"] == "clarify")                       # the brain on: a preview card first
    assert "Dialogue" in card["preview"]["lines"][0] and _facts(store).dialogue_lane.in_sync is False
    events = F.collect(service.resume(store, card["token"], {"apply": "yes"}))
    assert events[-1]["type"] == "done" and not [e for e in events if e["type"] == "error"], events[-3:]
    assert _facts(store).dialogue_lane.in_sync is True
    assert planner.STALE_LANE_NOTE not in (_plan(store, "add captions").reply or "")


def test_the_stale_lane_line_reaches_the_person_in_the_turn(brain_session):
    """The line is in the Prompt bar's own text for a non-brain edit (not only in the plan object)."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import planner, service
    store, src = brain_session
    _footage_read(store)
    _apply_brain_run(store, src)
    piece = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)[0]
    dispatch(store, "ripple_delete", {"clip_id": piece.id})
    events = F.collect(service.prompt_turn(store, "add captions", [], brain="recipes"))
    card = next(e for e in events if e["type"] == "clarify")
    events = F.collect(service.resume(store, card["token"], {"apply": "yes"}))
    text = " ".join(e.get("text", "") for e in events if e["type"] == "text_delta")
    assert planner.STALE_LANE_NOTE in text, text


def test_sync_the_dialogue_says_so_when_there_is_no_lane(brain_session):
    store, _src = brain_session
    p = _plan(store, "sync the dialogue")
    assert not p.steps and "no dialogue lane" in (p.reply or "")


def test_a_length_longer_than_the_footage_is_refused_and_the_result_length_is_stated(brain_session):
    """UX-12: 'make a 2 minute reel' over 12 s of footage says so; a fitting ask names the length it makes."""
    store, _src = brain_session
    _footage_read(store)
    p = _plan(store, "make a 2 minute reel")
    assert not p.steps and "0:12" in p.reply and "2:00" in p.reply, p.reply
    ok = _plan(store, "make a 10-second reel")
    assert ok.steps and "the result runs 0:06" in (ok.reply or ""), ok.reply     # the planner's closest fit is 5.7 s


def test_a_punchier_ask_raises_the_energy_control():
    f = TimelineFacts.minimal()
    assert BX.controls_from_intent(Intent("edit", {}, clause="make it punchier"), f, CTX)["energy"] == BX.PUNCHY_ENERGY
    assert BX.controls_from_intent(Intent("edit", {}, clause="make a 45-second reel"), f, CTX)["energy"] == 5


@pytest.mark.parametrize("phrase,expect", [
    ("make a 2 minute reel", 120.0), ("make a 45-second reel", 45.0), ("cut this down to a 60s vertical for tiktok", 60.0),
    ("give me a 90 second short for youtube shorts", 90.0)])
def test_the_length_the_sentence_names_is_read_in_seconds_and_minutes(phrase, expect):
    f = TimelineFacts.minimal()
    assert BX.controls_from_intent(Intent("edit", {}, clause=phrase), f, CTX)["duration_s"] == expect


def test_the_service_never_commits_a_second_brain_run_and_says_why(brain_session):
    """The whole turn, not only the planner: the phrases of the re-testers over V1 Reel (and over V1 plus a
    re-imported copy of the source) commit nothing and answer with the refusal — no card, no step."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import service
    store, src = brain_session
    _footage_read(store)
    _apply_brain_run(store, src)
    for phase in ("as edited", "after a re-import"):
        if phase == "after a re-import":
            dispatch(store, "add_clip", {"track": "v1", "src": src, "in": 0, "out": 12.0, "start": store.edl.video_extent()})
        before, ops = store.edl.hash(), len(store.ops.ops)
        for prompt in ("make a 10-second reel", "tighten this podcast like a premium podcast", "make it punchier"):
            events = F.collect(service.prompt_turn(store, prompt, [], brain="recipes"))
            assert events[-1]["type"] == "done", events[-2:]
            assert not [e for e in events if e["type"] in ("step", "clarify", "error")], (phase, prompt, events)
            text = "".join(e.get("text", "") for e in events if e["type"] == "text_delta")
            assert "already edited by the brain" in text or "edited after the brain" in text, (phase, prompt, text)
            assert store.edl.hash() == before and len(store.ops.ops) == ops


def test_second_angle_pieces_on_v1_are_the_camera_plan_footprint(tmp_path, monkeypatch):
    """EX-02: a two-camera FRESH import has the whole second camera on v1 (the compiler moves it off the
    main lane itself); pieces of it that are not the whole upload are what a camera plan leaves."""
    import shutil
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.brain import store as ST
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store = F.make_store(tmp_path, name="s_two")
    a = next(iter(store.edl.get_track("v1").clips)).src
    b = str(tmp_path / "cam_b.mp4")
    shutil.copyfile(a, b)
    sdir = Path(store.dir)
    BF.write_brain_files(sdir, a, workdir=tmp_path)
    header = ST.read_json(ST.graph_path(sdir, BF.GID))
    header["sources"].append({**header["sources"][0], "key": "src_" + "b" * 24, "angle": "B", "leaf": "cam_b.mp4",
                              "path": b, "dialogue": False})
    ST.write_json(ST.graph_path(sdir, BF.GID), header)
    angles = ST.read_json(ST.brain_dir(sdir) / "angles.json")
    angles["members"].append({"angle": "B", "src_key": "src_" + "b" * 24, "path": b, "sync_offset_s": -0.2,
                              "confidence": 1.0, "sees": [], "by": "single"})
    ST.write_json(ST.brain_dir(sdir) / "angles.json", angles)
    dispatch(store, "add_clip", {"track": "v1", "src": b, "in": 0, "out": F.CLIP_DUR, "start": store.edl.video_extent()})
    assert _facts(store).brain_edit is None                                   # the whole second camera: a fresh import
    dispatch(store, "add_clip", {"track": "v1", "src": b, "in": 2.0, "out": 5.0, "start": store.edl.video_extent()})
    f = _facts(store)
    assert f.brain_edit is not None and f.brain_edit.state == "brain_traces"
    assert BX.already_edited(f) is not None


# --------------------------------------------------------------------------
# FX-D round 2: the gate's wording (FX-A), a speech-less graph, and the UX-12 phrasings run through the WHOLE turn
# --------------------------------------------------------------------------

def test_the_gate_says_the_footage_is_still_being_read_and_estimates_from_the_media_length(monkeypatch):
    """FX-A / UX-07: with the transcript on its way the gate says so (no promise of a read of its own); otherwise
    the estimate is brain_seams.estimate_read_s (about 2 s + 4 % of the media), not the old 6 min per hour."""
    from video_ai_editor.agent.prompt import brain_seams
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    pending = TimelineFacts.minimal(duration=70.0, transcript_pending=True)
    q = BX.x_edit(Intent("edit", {}, clause="make a 30 second reel"), pending, CTX).questions[0]
    assert q.key == "gate_analysis" and "still being read" in q.question and "still being transcribed" in q.question
    assert "Read it first" not in q.question and [o.value for o in q.options] == ["read", "abort"]
    plain = TimelineFacts.minimal(duration=70.0)
    q = BX.x_edit(Intent("edit", {}, clause="make a 30 second reel"), plain, CTX).questions[0]
    assert f"≈ {round(brain_seams.estimate_read_s(70.0))} s" in q.question and "min" not in q.question, q.question
    hour = BX.x_edit(Intent("edit", {}, clause="tighten this podcast"), TimelineFacts.minimal(duration=3600.0), CTX)
    assert "≈ 2 min" in hour.questions[0].question, hour.questions[0].question            # 146 s, not the old "6 min"


def _speechless_graph(store) -> None:
    """The hand graph as the analysis pins it when no transcript ever came: the reference's speech layer is missing."""
    from video_ai_editor.brain import store as ST
    sdir = Path(store.dir)
    header = ST.read_json(ST.graph_path(sdir, BF.GID))
    for s in header["sources"]:
        s.setdefault("layers", {})["speech"] = "missing"
    ST.write_json(ST.graph_path(sdir, BF.GID), header)
    ST.set_current_graph(sdir, BF.GID)


def test_a_speechless_graph_is_refused_in_one_sentence_or_waits_for_the_transcript(brain_session):
    """FX-A -> FX-D (UX-01): never a cut-less 'reel' over a graph with no speech; nothing is planned, nothing changes."""
    store, _src = brain_session
    _speechless_graph(store)
    f = _facts(store).with_(brain_graph_id=BF.GID)
    before = store.edl.hash()
    x = BX.x_edit(Intent("edit", {}, clause="make a 10-second reel"), f, CTX)
    assert not x.steps and not x.questions and len(x.notes) == 1
    assert "nothing to cut or caption on" in x.notes[0] and "Nothing was changed" in x.notes[0], x.notes
    waiting = BX.x_edit(Intent("edit", {}, clause="make a 10-second reel"), f.with_(transcript_pending=True), CTX)
    assert not waiting.steps and waiting.questions[0].key == "gate_analysis" and "still being read" in waiting.questions[0].question
    p = _plan(store, "make a 10-second reel")
    assert not p.steps and (p.reply or "").count("nothing to cut or caption on") <= 1
    assert store.edl.hash() == before


#: the phrasings the re-testers (R-editor) found answered with a wrong or unclear question (review UX-12). Not here:
#: "make a 30-second clip of the best moment for linkedin" — on ONE clip 0.8.0 itself reads "clip … 30" as a clip
#: number ("There are only 1 clips"), flag on or off (the re-testers' two-camera session had the clips it names).
UX12_PHRASINGS = [
    "cut this down to a 60s vertical for tiktok", "clean this up", "edit this", "make it punchier", "make a 2 minute reel",
    "podcast ko tight karo, premium feel", "cut the silences and switch to whoever is speaking",
    "edit this like a podcast, switch cameras when they talk", "make a 45-second reel", "edit this like a talking head",
    "make a 30 second reel", "premium podcast", "filler words hata do aur tight kar do",
    "give me a 45 sec short for youtube shorts",
    "tighten this podcast", "edit this like a premium podcast", "isko 45 second ki reel bana do", "turn this into a youtube video"]


@pytest.mark.usefixtures("desktop_posture", "no_downloads")
@pytest.mark.parametrize("phrase", UX12_PHRASINGS)
def test_ux12_phrasings_are_the_edit_or_one_clear_question_and_state_the_length(brain_session, phrase):
    """The whole turn with the footage read: the edit's preview card (which states the length the edit makes), or one
    honest sentence (a length longer than the footage) — never the contract's 'I did not offer that plan: it added
    captions, which the request did not ask for', and nothing is committed."""
    from video_ai_editor.agent.prompt import service
    store, _src = brain_session
    _footage_read(store)
    before, ops = store.edl.hash(), len(store.ops.ops)
    events = F.collect(service.prompt_turn(store, phrase, [], brain="recipes"))
    text = " ".join(str(e.get("text", "")) for e in events if e["type"] == "text_delta")
    questions = [q["question"] for e in events if e["type"] == "clarify" for q in e["questions"]]
    assert not [e for e in events if e["type"] == "error"], events[-2:]
    assert "did not offer that plan" not in text and not any("did not offer that plan" in q for q in questions), (text, questions)
    assert "did not ask for" not in text and "Which did you mean" not in text, text
    if questions:
        assert questions == ["Apply these changes?"], questions
        assert "s when applied" in text, text                          # the card's line names the resulting length
    else:
        assert "shorter than the" in text and "you asked for" in text, text
    assert store.edl.hash() == before and len(store.ops.ops) == ops


def test_the_contract_reads_the_edit_asks_as_composite_only_with_the_brain_on(monkeypatch):
    """UX-12: 'edit this' / 'make it punchier' license the edit's changes like 'make it a reel' does — and with
    brain.enabled off the contract judges them exactly as 0.8.0 (SC-04)."""
    from video_ai_editor.agent.prompt.contract import families_of
    asks = ("edit this", "make it punchier", "clean this up", "switch to whoever is speaking", "premium podcast")
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    assert all("composite" in families_of(a) for a in asks)
    assert "composite" not in families_of("mute the music") and "composite" not in families_of("make the title red")
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    assert "composite" not in families_of("edit this") and "composite" not in families_of("make it punchier")


def test_the_hook_quote_in_the_plan_note_is_never_cut_mid_word():
    """Finalize (FX-E request, UX-13): 'opens on “… the best stretch of my ”' was cut at 60 characters."""
    from video_ai_editor.agent.prompt.brain_expanders import _quote_cut
    line = "I now finish forty percent more of what I plan, which is the best stretch of my week"
    cut = _quote_cut(line)
    assert cut.endswith("…") and len(cut) <= 61 and line.startswith(cut[:-1]) and not cut[:-1].endswith(" ")
    assert line[len(cut) - 1] == " ", "cut on a word boundary"
    assert _quote_cut("A short line.") == "A short line."


@pytest.mark.usefixtures("desktop_posture", "no_downloads")
@pytest.mark.parametrize("confirm", [True, False])
def test_a_brain_plan_the_safety_net_rolls_back_is_one_honest_sentence_and_no_question(brain_session, monkeypatch, confirm):
    """Closer review (UX-12, the podcast 'make a 45-second reel'): a rolled-back brain plan ended on validator prose
    ('cut at source 127.150s (seam … on a1)') and a Trim / Speed / Title picker with a Run button. It is one honest
    sentence now — what did not hold in plain words, that nothing changed, a next step — and nothing is pending."""
    from video_ai_editor.agent.prompt import executor, pending, service
    store, _src = brain_session
    _footage_read(store)
    monkeypatch.setattr(executor, "safety_net", lambda *a, **k: [
        {"kind": "check", "clause": "no_cut_mid_word",
         "message": "no cut lands inside a word did not hold: “that.” cut at source 127.150s (seam 00:00:04:13 on a1)"}])
    if not confirm:
        monkeypatch.setenv("VAI_PROMPT_CONFIRM", "0")
    before, ops = store.edl.hash(), len(store.ops.ops)
    events = F.collect(service.prompt_turn(store, "make a 3-second reel", [], brain="recipes"))
    text = " ".join(str(e.get("text", "")) for e in events if e["type"] == "text_delta")
    assert not [e for e in events if e["type"] == "clarify"], events
    for leak in ("source 127", "seam", "on a1", "Which did you mean", "I undid that", "I did not offer that plan", "Trim", "Speed"):
        assert leak not in text, (leak, text)
    assert "inside a spoken word" in text and "Nothing was changed." in text and "another length" in text, text
    assert "3-second reel" in text or "rolled back" in text, text
    assert pending.load_pending(Path(store.dir)) is None
    assert store.edl.hash() == before and len(store.ops.ops) == ops
