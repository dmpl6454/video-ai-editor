"""Wave-B review (QA-064 remainders): cancelling a prompt run mid-step.

1. Esc during Transcribe ended as a red FAILED step ("transcription cancelled
   after 0s of 85s — failed"): transcribe raises TranscriptionCancelled, which
   is not a JobCancelled, so the per-step `except Exception` recorded a step
   failure instead of the "Cancelled — timeline unchanged" path.
2. After a cancel, GET /prompt/run kept the executing step at status
   'running' with ended=null, so a reloaded client spun on it forever.
"""
from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.prompt import executor  # noqa: E402
from video_ai_editor.agent.prompt.runlog import RunLog, RunRecord, read_record  # noqa: E402
from video_ai_editor.ingest.transcribe import TranscriptionCancelled  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")
pytestmark = pytest.mark.usefixtures("desktop_posture")


def test_esc_during_a_step_whose_handler_raises_its_own_cancel_is_cancelled_not_failed(tmp_path, monkeypatch):
    store = F.make_store(tmp_path)
    facts = F.facts_for(store)
    cancel = threading.Event()
    hash_before = store.edl.hash()

    def fake_transcribe(s, a, **kw):
        cancel.set()                       # the user pressed Esc mid-transcribe…
        raise TranscriptionCancelled("transcription cancelled after 0s of 85s")

    monkeypatch.setitem(D.DISPATCH, "add_text", fake_transcribe)
    plan = F.plan_of(F.step("add_text", text="x", start=0, end=1))
    events: list = []
    res = executor.run_plan(store, plan, facts, emit=events.append, cancel_event=cancel,
                            prompt="p", validator=F.identity_validator)
    assert res.cancelled is True
    assert res.error == "Cancelled — timeline unchanged."
    assert not any(s.status == "failed" for s in res.steps)
    assert not any(e.get("type") == "step" and e.get("status") == "failed" for e in events)
    assert store.edl.hash() == hash_before


def test_a_cancelled_run_record_has_no_step_left_running(tmp_path):
    log = RunLog(tmp_path, RunRecord(run_id="r_1", plan_id="p", prompt="x"))
    log.emit({"type": "step", "index": 0, "total": 3, "tool": "transcribe", "status": "running"})
    log.set_status("cancelled", error="Cancelled — timeline unchanged.")
    # a late progress tick must not bring the spinner back
    log.emit({"type": "step", "index": 0, "total": 3, "tool": "transcribe", "status": "running"})
    rec = read_record(tmp_path)
    assert rec["status"] == "cancelled"
    [s] = rec["steps"]
    assert s["status"] == "cancelled" and s["ended"] is not None
