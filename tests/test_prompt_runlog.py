"""The run bus and the run record (agent/prompt/runlog.py, spec §4.2).

Two guarantees the desktop's reconnect and the phone's "recovery is history,
never a replay" both depend on:

  * a subscriber that reads from an index sees every frame in order and
    `done` exactly once, no matter how late it attaches — so a reload can
    replay the `plan` frame that the UI needs to draw anything;
  * the record on disk (`prompt_run.json`) is rewritten on every event,
    atomically, and folds steps / verify / op / plan / error so a process
    restart can still say what the last run did.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from video_ai_editor.agent.prompt import runlog, service
from video_ai_editor.agent.prompt.runlog import RunBus, RunLog, RunRecord, StepRecord


def _plan_event(plan_id: str = "p_00000001", brain: str = "recipes") -> dict:
    return {"type": "plan", "plan": {"id": plan_id, "brain": brain, "version": 1}}


# ---------------------------------------------------------------- RunBus

def test_bus_publishes_in_order_and_done_closes_it():
    bus = RunBus()
    assert bus.publish({"type": "brain"}) == 0
    assert bus.publish({"type": "step", "index": 0}) == 1
    assert not bus.closed
    assert bus.publish({"type": "done"}) == 2
    assert bus.closed and len(bus) == 3
    with pytest.raises(RuntimeError):
        bus.publish({"type": "text_delta", "text": "too late"})
    assert [e["type"] for e in bus.snapshot()] == ["brain", "step", "done"]
    assert [e["type"] for e in bus.snapshot(start=2)] == ["done"]


def test_wait_next_returns_none_only_on_timeout_or_past_the_end():
    bus = RunBus()
    assert bus.wait_next(0, timeout=0.01) is None            # nothing yet
    bus.publish({"type": "brain"})
    assert bus.wait_next(0, timeout=0.01) == {"type": "brain"}
    bus.publish({"type": "done"})
    assert bus.wait_next(1) == {"type": "done"}
    assert bus.wait_next(2, timeout=5.0) is None             # closed + past the end: immediate


def test_late_subscriber_replays_everything_and_live_subscriber_sees_new_frames():
    bus = RunBus()
    bus.publish(_plan_event())
    bus.publish({"type": "step", "index": 0, "status": "running"})
    seen: list[dict] = []
    done = threading.Event()

    def _reader():
        for evt in bus.iter_from(0):
            seen.append(evt)
        done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    time.sleep(0.05)
    bus.publish({"type": "step", "index": 0, "status": "ok"})
    bus.publish({"type": "done"})
    assert done.wait(2.0)
    assert [e["type"] for e in seen] == ["plan", "step", "step", "done"]
    assert sum(1 for e in seen if e["type"] == "done") == 1
    # A second, late reader gets the identical sequence from the closed bus.
    assert list(bus.iter_from(0)) == seen
    # And one that already saw the first two frames gets only the rest.
    assert [e["type"] for e in bus.iter_from(2)] == ["step", "done"]


def test_unsubscribing_via_should_stop_does_not_touch_the_bus():
    """The decoupling in one assertion: a reader leaving early is invisible
    to the producer — the bus stays open and keeps accepting frames."""
    bus = RunBus()
    bus.publish({"type": "brain"})
    stop = threading.Event()
    got: list[dict] = []
    t = threading.Thread(target=lambda: got.extend(bus.iter_from(0, should_stop=stop.is_set)), daemon=True)
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(3.0)
    assert not t.is_alive()
    assert got == [{"type": "brain"}]
    assert not bus.closed
    bus.publish({"type": "done"})                            # the run finishes regardless
    assert bus.closed


# ---------------------------------------------------------------- RunLog / RunRecord

def test_record_shape_matches_the_frozen_contract():
    rec = RunRecord(run_id="r_1", plan_id="p_1", prompt="add captions")
    d = rec.as_dict()
    assert {"run_id", "plan_id", "status", "started", "steps", "verify", "reply", "op"} <= set(d)
    assert d["status"] == "planning" and d["steps"] == [] and d["events"] == 0
    step = StepRecord(index=0, total=2, tool="add_caption_track")
    assert step.as_dict()["status"] == "running" and step.as_dict()["ended"] is None


def test_runlog_folds_events_into_the_record_and_rewrites_the_file(tmp_path: Path):
    log = RunLog(tmp_path, RunRecord(run_id="r_2", plan_id=None, prompt="x"))
    log.emit({"type": "brain", "status": "answered", "brain": "recipes", "label": "Recipes"})
    log.emit(_plan_event("p_abcdef12", "local_model"))
    assert log.record.plan_id == "p_abcdef12" and log.record.brain == "local_model"
    log.emit({"type": "step", "index": 0, "total": 2, "tool": "remove_fillers", "status": "running"})
    log.emit({"type": "step", "index": 0, "total": 2, "tool": "remove_fillers", "status": "ok",
              "summary": "cut 3", "effect": None})
    log.emit({"type": "step", "index": 1, "total": 2, "tool": "apply_lut", "status": "running"})
    log.emit({"type": "step", "index": 1, "total": 2, "tool": "apply_lut", "status": "skipped",
              "error": "unknown LUT"})
    log.emit({"type": "verify", "plan_id": "p_abcdef12", "checks": [], "passed": 1, "total": 1, "rendered": False})
    log.emit({"type": "op", "op": {"seq": 7, "tool": "prompt"}})
    log.set_reply("via Recipes — done")
    log.set_status("done")
    log.emit({"type": "done"})

    on_disk = runlog.read_record(tmp_path)
    assert on_disk is not None
    assert on_disk["run_id"] == "r_2" and on_disk["status"] == "done" and on_disk["ended"] is not None
    assert [s["status"] for s in on_disk["steps"]] == ["ok", "skipped"]
    assert on_disk["steps"][0]["summary"] == "cut 3" and on_disk["steps"][0]["ended"] is not None
    assert on_disk["steps"][1]["error"] == "unknown LUT"
    assert on_disk["verify"]["passed"] == 1 and "type" not in on_disk["verify"]
    assert on_disk["op"] == {"seq": 7, "tool": "prompt"}
    assert on_disk["reply"] == "via Recipes — done"
    assert on_disk["events"] == len(log.bus) == 9
    assert runlog.runlog_path(tmp_path).name == service.RUNLOG_FILE
    assert not runlog.runlog_path(tmp_path).with_suffix(".json.tmp").exists()   # atomic rewrite left no tmp


def test_error_event_and_status_are_recorded_and_bad_file_reads_as_none(tmp_path: Path):
    log = RunLog(tmp_path, RunRecord(run_id="r_3", plan_id="p_3", prompt="x"))
    log.emit({"type": "error", "message": "Step 1/2 set_speed failed: boom. Timeline unchanged."})
    log.set_status("failed", error="Step 1/2 set_speed failed: boom. Timeline unchanged.")
    d = runlog.read_record(tmp_path)
    assert d["status"] == "failed" and d["error"].startswith("Step 1/2")
    runlog.runlog_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert runlog.read_record(tmp_path) is None
    assert runlog.read_record(tmp_path / "nowhere") is None


def test_new_run_ids_are_distinct_and_prefixed():
    ids = {runlog.new_run_id() for _ in range(50)}
    assert len(ids) == 50 and all(i.startswith("r_") and len(i) == 10 for i in ids)


def test_record_file_is_valid_json_after_every_event(tmp_path: Path):
    """The desktop polls this file mid-run; it must never see a torn write."""
    log = RunLog(tmp_path, RunRecord(run_id="r_4", plan_id="p_4", prompt="x"))
    for i in range(20):
        log.emit({"type": "step", "index": i, "total": 20, "tool": "split_at", "status": "ok"})
        json.loads(runlog.runlog_path(tmp_path).read_text(encoding="utf-8"))
    assert len(runlog.read_record(tmp_path)["steps"]) == 20


def test_a_late_running_tick_never_reopens_a_finished_step(tmp_path: Path):
    """A synthetic-progress tick can race the terminal frame by a millisecond;
    the record must keep `ok` (the desktop row used to spin until reload)."""
    log = RunLog(tmp_path, RunRecord(run_id="r_race", plan_id=None, prompt="x"))
    log.emit({"type": "step", "index": 0, "total": 1, "tool": "auto_reframe", "status": "running", "progress": 0.2})
    log.emit({"type": "step", "index": 0, "total": 1, "tool": "auto_reframe", "status": "ok", "summary": "reframed"})
    log.emit({"type": "step", "index": 0, "total": 1, "tool": "auto_reframe", "status": "running", "progress": 0.3})
    assert log.record.steps[0]["status"] == "ok" and log.record.steps[0]["summary"] == "reframed"
    assert log.record.steps[0]["ended"] is not None
