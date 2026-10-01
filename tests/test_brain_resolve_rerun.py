"""A re-run on the brain's own output (EX-02 / UX-04), split out of test_brain_resolve.py to keep that file under
800 lines: the resolver's refusal, the removal check as the second line, and the first run inside its own estimate."""
from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import brain_contract_fixtures as BF  # noqa: E402
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401
from test_brain_resolve import _v1, session  # noqa: E402,F401

from video_ai_editor import config  # noqa: E402
from video_ai_editor.agent.prompt import executor  # noqa: E402
from video_ai_editor.brain import resolve as R  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")
pytestmark = pytest.mark.usefixtures("desktop_posture")


# ---------------------------------------------------------------- EX-02 / UX-04: a re-run on the brain's own output

def _v1_layout(store, files: list[str], spans: list[float]) -> None:
    """v1 rebuilt from `(file, seconds)` pieces, edited in memory only (a layout for the predicate)."""
    from video_ai_editor.edl.schema import Clip as _Clip
    track = store.edl.get_track("v1")
    track.clips, t = [], 0.0
    for i, (f, d) in enumerate(zip(files, spans)):
        track.clips.append(_Clip(id=f"c_t{i:03d}", src=f, in_=0.0, out=d, start=t))
        t += d


def test_camera_plan_present_reads_the_live_layout(session):
    store, facts, src, _ = session
    a, b, roll = "/m/cam_a.mp4", "/m/cam_b.mp4", "/m/broll.mp4"
    for files, expect in ((["a", "b"], False),                # two uploads dropped one after the other
                          (["b", "a"], False),
                          (["a", "b", "a"], True),            # a camera plan: A B A
                          (["a", "b", "a", "b", "a"], True),
                          (["a", "roll", "a"], False),        # B-roll between two stretches of one camera is not a switch
                          (["a", "a", "a"], False),
                          (["a", "roll", "b", "roll", "a"], True)):
        _v1_layout(store, [{"a": a, "b": b, "roll": roll}[f] for f in files], [5.0] * len(files))
        assert R.camera_plan_present(store.edl, [a, b]) is expect, files
    assert R.camera_plan_present(store.edl, [a]) is False       # one camera: never a camera plan
    assert R.camera_plan_present(store.edl, []) is False


def _pod(tmp_path):
    from brain_fixtures import build_brain_fixtures
    fx = build_brain_fixtures()
    if fx.p2 is None:
        pytest.skip(f"P2 skipped by its builder: {fx.p2_skip_reason}")
    return BF.podcast_session(tmp_path, fx.p2)


def _edit_podcast(store, again=None, *, replan: bool = False):
    """One run of 'tighten this podcast like a premium podcast' as the service composes it: expander → plan →
    validate → executor. With `again` (an earlier run's result) the SAME plan is applied once more on the
    live tree — a stale card. With `replan` the expander plans again with its own re-run rule (lane FX-D's:
    a note and no steps) switched off, as it was when the tester saw 107 s cut. Returns `(result, notes)`."""
    from video_ai_editor.agent.prompt import brain_expanders as BX
    from video_ai_editor.agent.prompt.facts import build_facts
    from video_ai_editor.agent.prompt.recipes import Context, Intent
    facts = build_facts(store, None)
    if replan:
        facts = facts.with_(brain_edit=None)
    if again is not None:
        plan, notes = again.plan, ()
    else:
        x = BX.x_edit(Intent("edit", {}, clause="tighten this podcast like a premium podcast"), facts,
                      Context(recipes=frozenset({"edit"}), exclusions=frozenset(), has_cut_steps=True))
        plan = F.plan_of(*x.steps, intent="edit", title="Premium podcast", postconditions=list(x.postconditions))
        notes = x.notes
    res = executor.run_plan(store, plan, facts, emit=lambda e: None, cancel_event=threading.Event(),
                            prompt="tighten this podcast like a premium podcast")
    return res, notes


def test_rerunning_the_podcast_on_its_own_output_commits_nothing(tmp_path, monkeypatch):
    """EX-02: the second run removed 107.55 s of a 160 s episode (the literal 'second angle off v1' step
    cut every cam B piece the first camera plan had placed) and the run was applied."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store, sd = _pod(tmp_path)
    first, _ = _edit_podcast(store)
    assert first.error is None and first.rollback is None and first.committed, first.error
    layout = (store.edl.hash(), len(store.ops.ops), round(store.edl.video_extent(), 3), len(_v1(store)))
    assert layout[3] > 20 and first.duration_before - layout[2] < 200          # ONE programme, tightened
    members = list(R.angle_offsets(sd / "brain"))
    assert R.camera_plan_present(store.edl, members)                            # the live v1 says: already edited
    second, _ = _edit_podcast(store, replan=True)
    assert second.error is not None and not second.committed and second.rollback is None
    assert "already has camera changes" in second.error and "Timeline unchanged" in second.error, second.error
    assert (store.edl.hash(), len(store.ops.ops), round(store.edl.video_extent(), 3), len(_v1(store))) == layout


def test_the_removal_check_blocks_when_the_resolver_guard_is_off(tmp_path, monkeypatch):
    """EX-02, the second line: even with the resolver's refusal switched off, a run that removes more
    picture than its decisions name (by more than a frame per cut) is rolled back by the safety net."""
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store, sd = _pod(tmp_path)
    first, _ = _edit_podcast(store)
    assert first.committed
    layout = (store.edl.hash(), len(store.ops.ops))
    monkeypatch.setattr(R, "refuse_if_already_edited", lambda s: None)
    second, _ = _edit_podcast(store, replan=True)
    assert second.rollback and any(r["clause"] == "removal_within_plan" for r in second.rollback), (second.error, second.rollback)
    assert not second.committed and (store.edl.hash(), len(store.ops.ops)) == layout


def test_the_first_run_is_within_its_own_estimate(tmp_path, monkeypatch):
    """The check never fires on a correct run: measured removed vs named on the first podcast run."""
    from video_ai_editor.brain import checks as BCH
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "1")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    store, sd = _pod(tmp_path)
    first, _ = _edit_podcast(store)
    assert first.committed
    assert BCH.removal_overrun(store, first.plan, first) == []
    edp = R.load_edp(sd / "brain", next(s.args["plan_ref"] for s in first.plan.steps if "plan_ref" in s.args))
    named, cuts = BCH.planned_removal(first.edl_before, edp, sd / "brain", list(first.plan.steps))
    removed = first.edl_before.video_extent() - store.edl.video_extent()
    assert cuts >= 2 and abs(named - removed) < 0.5 and removed > 100, (named, removed, cuts)
    # one piece deleted on top of the run: the check alone (no resolver, no other net) says so
    piece = max(_v1(store), key=lambda c: c.effective_duration)
    D.dispatch(store, "ripple_delete", {"clip_id": piece.id})
    reasons = BCH.removal_overrun(store, first.plan, first)
    assert len(reasons) == 1 and reasons[0]["clause"] == "removal_within_plan" and "decisions name" in reasons[0]["message"], reasons
