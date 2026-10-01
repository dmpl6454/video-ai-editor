"""`brain/versions.py` + the one snapshot-pin exemption in `edl/snapshot.py`
(EB1 lane C).

A Version is a named label on a snapshot `commit()` already wrote; restore
is ONE op (`commit("restore_version", …)` of that tree, one ⌘Z, a History
row); a pinned version's snapshot survives pruning (moved aside, never
counted as an undo step) and stays restorable.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.brain import versions as V  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")
pytestmark = pytest.mark.usefixtures("desktop_posture")


def test_record_after_apply_and_restore_is_one_op(tmp_path: Path):
    store = F.make_store(tmp_path)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 4.0, "end": 6.0})   # "the applied brain run"
    applied_hash = store.edl.hash()
    ops_after_apply = len(store.ops.ops)
    v = V.record(store, label="V1 Reel", decisions_id="d_0badf00d", kind="brain")
    assert v.id == "v_1" and v.label == "V1 Reel" and v.edl_hash == applied_hash and v.pinned is True
    assert v.op_seq == store.ops.last().seq and v.decisions_id == "d_0badf00d" and v.kind == "brain"
    rows = V.list_versions(store.dir)
    assert [r.id for r in rows] == ["v_1"] and rows[0].restorable is True
    assert json.loads((Path(store.dir) / "brain" / "versions.json").read_text())["versions"][0]["label"] == "V1 Reel"
    assert V.next_label(store.dir, "Reel") == "V2 Reel"
    # hand edits after the version
    D.dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 2.0})
    first_id = [c.id for c in store.edl.get_track("v1").clips][0]
    D.dispatch(store, "set_volume", {"target": first_id, "db": -6})
    assert store.edl.hash() != applied_hash
    depth_before = store.undo_depth
    ops_before = len(store.ops.ops)
    op = V.restore(store, "v_1")
    assert store.edl.hash() == applied_hash                       # the tree is back
    assert len(store.ops.ops) == ops_before + 1 and op.tool == "restore_version"   # ONE op, a History row
    assert op.args == {"id": "v_1", "label": "V1 Reel"} and "V1 Reel" in op.summary
    assert store.undo_depth == depth_before + 1
    assert store.undo() and store.edl.hash() != applied_hash      # one ⌘Z takes it back
    assert store.redo() and store.edl.hash() == applied_hash
    # a second version after a manual save; restoring a missing id is refused
    v2 = V.record(store, label="Save 1", decisions_id=None, kind="manual", pinned=False)
    assert v2.id == "v_2" and v2.pinned is False
    with pytest.raises(V.UnknownVersion, match="v_9"):
        V.restore(store, "v_9")
    # the LATEST version whose hash equals the live tree is "current" (v_1 and Save 1 label the same tree)
    cur = V.current_version(store)
    assert cur is not None and cur.id == "v_2" and cur.edl_hash == store.edl.hash()
    assert V.record_after_run(store, decisions_id="d_0badf00d", title="Reel").label == "V3 Reel"
    assert ops_after_apply <= len(store.ops.ops)


def test_pinned_snapshot_survives_pruning(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(EDLStore, "MAX_UNDO", 6)
    store = F.make_store(tmp_path)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 10.0, "end": 11.0})
    pinned_hash = store.edl.hash()
    v = V.record(store, label="V1 Reel", decisions_id="d_0badf00d", kind="brain")
    snap_name = V.snapshot_name(v)
    assert (Path(store.dir) / "snapshots" / snap_name).exists()
    cid = [c.id for c in store.edl.get_track("v1").clips][0]
    D.dispatch(store, "set_volume", {"target": cid, "db": -0.5})
    unpinned = V.record(store, label="Save", decisions_id=None, kind="manual", pinned=False)
    assert V.snapshot_name(unpinned) != snap_name
    for i in range(12):
        D.dispatch(store, "set_volume", {"target": cid, "db": -1.0 - i})
    live = sorted(p.name for p in (Path(store.dir) / "snapshots").glob("*.json"))
    assert len(live) <= 6 and snap_name not in live                     # pruned from the undo sequence…
    assert (Path(store.dir) / "snapshots" / "pinned" / snap_name).exists()   # …kept aside for restore
    assert store.undo_depth <= 5                                         # never counted as an undo step
    rows = {r.id: r for r in V.list_versions(store.dir)}
    assert rows["v_1"].restorable is True and rows[unpinned.id].restorable is False
    with pytest.raises(V.NotRestorable, match="no longer restorable"):
        V.restore(store, unpinned.id)
    V.restore(store, "v_1")
    assert store.edl.hash() == pinned_hash
    # a second store on the same directory sees the same rows and can restore too
    again = EDLStore(Path(store.dir))
    D.dispatch(again, "set_volume", {"target": cid, "db": -20.0})
    V.restore(again, "v_1")
    assert again.edl.hash() == pinned_hash


import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

# ---------------------------------------------------------------- FX-A: SC-06 / EX-05, distinct types

def test_the_same_plan_applied_twice_records_two_distinct_labels(tmp_path, monkeypatch):
    """Make a reel, undo, make it again: the EDP id is a content hash, so both runs name the SAME `did`."""
    import brain_card_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import storage
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import brain_card, service
    from video_ai_editor.agent.prompt.schema import Plan, Step
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    live = F.make_store(tmp_path, name="s_twice")
    src = str(live.edl.get_track("v1").clips[0].src)
    BF.write_edp(Path(live.dir), BF.edp(src))
    plan = Plan.new(intent="edit", brain="recipes", title="Reel", postconditions=[], steps=[
        Step(tool="cut_source_ranges", args={"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID},
             why="cuts", stage=2)])
    handle = SimpleNamespace(result=SimpleNamespace(committed=True, op={"tool": "prompt"}), mode="apply")
    labels, cards = [], []
    for i in range(3):
        brain_card.register_plan(plan, Path(live.dir))
        service._refresh_version_label(plan, Path(live.dir))         # what `_run_and_stream` does right after
        cards.append(brain_card.version_label(BF.DID))
        dispatch(live, "add_text", {"text": f"run {i}", "start": 0, "end": 1})
        row = service.record_brain_version(live, plan, handle)
        labels.append(row["label"])
    assert labels == ["V1 Reel", "V2 Reel", "V3 Reel"], labels
    assert [v.label for v in V.list_versions(live.dir)] == labels
    assert len({v.id for v in V.list_versions(live.dir)}) == 3
    # the card of the next run names the version that run WILL record, not the last one's label
    assert cards == ["V1 Reel", "V2 Reel", "V3 Reel"], cards


def test_unknown_and_pruned_versions_raise_distinct_types(tmp_path):
    import prompt_fixtures as F
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import brain_seams
    assert not issubclass(V.UnknownVersion, V.NotRestorable) and not issubclass(V.NotRestorable, V.UnknownVersion)
    st = F.make_store(tmp_path, name="s_types")
    dispatch(st, "add_text", {"text": "one", "start": 0, "end": 1})
    row = V.record(st, label="V1 Edit", decisions_id=None, kind="manual", pinned=False)
    dispatch(st, "add_text", {"text": "two", "start": 1, "end": 2})
    with pytest.raises(V.UnknownVersion):
        V.restore(st, "v_404")
    V.snapshot_file(st.dir, row).unlink()
    with pytest.raises(V.NotRestorable):
        V.restore(st, row.id)
    api = brain_seams.versions()
    with pytest.raises(brain_seams.UnknownVersionError):
        api.restore(st, "v_404")
    with pytest.raises(brain_seams.VersionNotRestorable):
        api.restore(st, row.id)
    assert not issubclass(brain_seams.UnknownVersionError, brain_seams.VersionNotRestorable)
    assert not issubclass(brain_seams.VersionNotRestorable, brain_seams.UnknownVersionError)


def test_the_seam_maps_by_type_not_by_message(monkeypatch):
    """A message that says nothing of 'restorable' or 'exist' still lands on the right type."""
    from video_ai_editor.agent.prompt import brain_seams

    def restore(store, vid):
        raise (V.NotRestorable("snapshot gone") if vid == "a" else V.UnknownVersion("no such row"))
    monkeypatch.setattr(V, "restore", restore)
    api = brain_seams.versions()
    with pytest.raises(brain_seams.VersionNotRestorable):
        api.restore(SimpleNamespace(dir="."), "a")
    with pytest.raises(brain_seams.UnknownVersionError):
        api.restore(SimpleNamespace(dir="."), "b")


def test_concurrent_records_get_distinct_ids(tmp_path):
    import prompt_fixtures as F
    from video_ai_editor.agent.dispatch import dispatch
    st = F.make_store(tmp_path, name="s_conc")
    dispatch(st, "add_text", {"text": "one", "start": 0, "end": 1})
    out, errs = [], []

    def rec() -> None:
        try:
            out.append(V.record(st, label="x", decisions_id=None, kind="manual").id)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=rec) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(set(out)) == 6 and len(V.list_versions(st.dir)) == 6


# --------------------------------------------------------------------------
# closer: a malformed versions.json (a hand-made or imported file) never breaks Undo, listing or the route
# --------------------------------------------------------------------------

GARBAGE_ROWS = [
    [{"op_seq": None, "edl_hash": "abc", "pinned": True}],
    [{"op_seq": "x", "edl_hash": "abc", "pinned": True}],
    [{"op_seq": 3, "edl_hash": None, "pinned": True}],
    [{"op_seq": True, "edl_hash": "abc", "pinned": True}],
    [{"op_seq": 1.5, "edl_hash": "../../etc/passwd", "pinned": True}],
    [1, "x", None],
    [{"id": "v_1"}],
    [{"id": "v_1", "label": "L", "op_seq": [1], "edl_hash": {"a": 1}, "pinned": True, "kind": "brain", "created": 1.0,
      "decisions_id": None}],
]


@pytest.mark.parametrize("rows", GARBAGE_ROWS)
def test_garbage_rows_in_versions_json_never_break_undo_or_listing(tmp_path: Path, rows):
    store = F.make_store(tmp_path)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 4.0, "end": 6.0})
    D.dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 2.0})
    (Path(store.dir) / "brain").mkdir(exist_ok=True)
    (Path(store.dir) / "brain" / "versions.json").write_text(json.dumps({"versions": rows}), encoding="utf-8")
    assert V.list_versions(store.dir) == []                          # nothing valid in it
    assert store.undo() and store.redo()                              # Undo (which retires a snapshot) and Redo run
    D.dispatch(store, "cut_range", {"track": "v1", "start": 0.2, "end": 0.6})    # a commit prunes: never raises either


def test_a_good_row_beside_garbage_rows_still_pins_its_snapshot(tmp_path: Path):
    store = F.make_store(tmp_path)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 4.0, "end": 6.0})
    v = V.record(store, label="V1 Reel", decisions_id="d_0badf00d", kind="brain")
    path = Path(store.dir) / "brain" / "versions.json"
    body = json.loads(path.read_text())
    body["versions"] = [None, {"op_seq": "x"}] + body["versions"]
    path.write_text(json.dumps(body), encoding="utf-8")
    assert [r.id for r in V.list_versions(store.dir)] == [v.id]
    assert store._pinned_snapshots() == {V.snapshot_name(v)}


# ---------------------------------------------------------------- closer: N-18 (two surviving mutants)

def _twice_applied_world(tmp_path, monkeypatch):
    import brain_card_fixtures as BF
    import prompt_fixtures as F
    from video_ai_editor import storage
    from video_ai_editor.agent.prompt import service
    from video_ai_editor.agent.prompt.schema import Plan, Step
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    live = F.make_store(tmp_path, name="s_twice_wired")
    src = str(live.edl.get_track("v1").clips[0].src)
    BF.write_edp(Path(live.dir), BF.edp(src))
    plan = Plan.new(intent="edit", brain="recipes", title="Reel", postconditions=[], steps=[
        Step(tool="cut_source_ranges", args={"track": "v1", "ranges": "$brain:cuts", "plan_ref": BF.DID}, why="cuts", stage=2)])
    return live, plan, BF.DID


def test_a_stale_remembered_label_never_names_the_version_that_is_recorded(tmp_path, monkeypatch):
    """Mutant: `record_brain_version` read the memo before the versions file. The memo is only ever the reply's wording."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import brain_card, service
    live, plan, did = _twice_applied_world(tmp_path, monkeypatch)
    handle = SimpleNamespace(result=SimpleNamespace(committed=True, op={"tool": "prompt"}), mode="apply")
    dispatch(live, "add_text", {"text": "one", "start": 0, "end": 1})
    assert service.record_brain_version(live, plan, handle)["label"] == "V1 Reel"
    brain_card.remember_version(did, "V1 Reel")                       # stale: the next run records V2
    dispatch(live, "add_text", {"text": "two", "start": 1, "end": 2})
    assert service.record_brain_version(live, plan, handle)["label"] == "V2 Reel"
    brain_card.remember_version(did, "V9 Whatever")
    dispatch(live, "add_text", {"text": "three", "start": 2, "end": 3})
    assert service.record_brain_version(live, plan, handle)["label"] == "V3 Reel"


def test_the_run_the_prompt_bar_starts_names_the_version_it_will_record(tmp_path, monkeypatch):
    """Mutant: the `_refresh_version_label` call in `_run_and_stream` removed. Drive the real wiring with a stubbed
    executor: what the card and the reply read (`brain_card.version_label`) at the moment the run starts is V<n>."""
    import asyncio
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.agent.prompt import brain_card, executor, service
    from video_ai_editor.agent.prompt.facts import build_facts
    live, plan, did = _twice_applied_world(tmp_path, monkeypatch)
    seen: list[str | None] = []

    def fake_start_run(resolver, sid, p, facts, *, bus, **kw):
        seen.append(brain_card.version_label(did))                      # what the card / reply will say for THIS run
        bus.publish({"type": "done"})
        return SimpleNamespace(run_id="r_fake", final_text=None, stale=False, mode="apply",
                               result=SimpleNamespace(committed=True, op={"tool": "prompt"}))

    monkeypatch.setattr(executor, "start_run", fake_start_run)

    async def one_run() -> list[dict]:
        out = []
        async for evt in service._run_and_stream(live, plan, build_facts(live, None), prompt="make a reel", history=[],
                                                 pre_events=[], mode="apply"):
            out.append(evt)
        return out

    for i, want in enumerate(["V1 Reel", "V2 Reel", "V3 Reel"]):
        dispatch(live, "add_text", {"text": f"run {i}", "start": i, "end": i + 1})
        events = asyncio.run(one_run())
        assert events and events[-1]["type"] == "done"
    assert seen == ["V1 Reel", "V2 Reel", "V3 Reel"], seen
    assert [v.label for v in V.list_versions(live.dir)] == ["V1 Reel", "V2 Reel", "V3 Reel"]
