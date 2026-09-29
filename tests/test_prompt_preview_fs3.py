"""Preview, then apply — final sweep 3 findings, over the real FastAPI app.

  * a capped card exposes EVERY line before Apply (the payload's `hidden`
    and the chat / phone text), not just "and N more changes";
  * a card's note ("The timeline changed since the preview, so nothing was
    applied", "Dropped the earlier preview …") belongs to the card only —
    the Apply that follows does not say "nothing was applied";
  * the upload's background transcript landing DURING a dry run survives the
    preview (the side-effect snapshot used to be taken before the wait and
    restored after it, erasing it);
  * a Cancel that lands while the dry run finishes shows no card.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401
from test_prompt_preview import (SID, _card, _frames, _join, _live, _preview, app_env)  # noqa: E402,F401

from video_ai_editor.agent.prompt import executor, pending, service  # noqa: E402

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")


def _text(frames: list[dict]) -> str:
    return "".join(f.get("text", "") for f in frames if f["type"] == "text_delta")


def test_a_capped_card_exposes_every_line_before_apply(app_env, monkeypatch):
    """HIGH: 'make it a 10 second reel' — 13 changes, the card showed 11 and
    'and 2 more changes' with no way to see the two (one trimmed the music)."""
    client, store, facts, root = app_env
    plan = F.plan_of(*[F.step("add_marker", time=float(t), label=f"m{t}") for t in range(14)], title="marks")
    card = _card(_preview(client, monkeypatch, plan=plan, message="add markers"))
    p = card["preview"]
    assert p["more"] > 0, p
    everything = list(p["lines"]) + list(p.get("hidden") or [])
    assert len(everything) == p["total"] == 14, p
    assert "Added marker 'm13' at 00:00:13:00" in everything
    got = client.get(f"/api/sessions/{SID}/prompt/pending").json()["pending"]["preview"]
    assert list(got["lines"]) + list(got.get("hidden") or []) == everything
    # the chat pane and the phone read the text: it lists every line too
    text = _text(_preview(client, monkeypatch, plan=plan, message="add markers"))
    assert all(f"- {line}" in text for line in everything), text


def test_apply_after_a_stale_card_does_not_say_nothing_was_applied(app_env, monkeypatch):
    """MEDIUM: the fresh card's note leaked into the Apply's report —
    'done — 1 step applied … The timeline changed since the preview, so
    nothing was applied.'"""
    client, store, facts, root = app_env
    live = _live(root)
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="mute clip 1")
    card = _card(_preview(client, monkeypatch, plan=plan, message="mute clip 1"))
    r = client.post(f"/api/sessions/{SID}/dispatch", json={"tool": "set_clip_muted", "args": {
        "clip_id": live.edl.get_track("v1").clips[-1].id, "muted": True}})
    assert r.status_code == 200, r.text
    fresh = _card(_frames(client.post(f"/api/sessions/{SID}/prompt/answer",
                                      json={"token": card["token"], "apply": True}).text))
    _join()
    assert "nothing was applied" in (fresh["preview"]["note"] or "")
    frames = _frames(client.post(f"/api/sessions/{SID}/prompt/answer",
                                 json={"token": fresh["token"], "apply": True}).text)
    _join()
    assert [f["type"] for f in frames].count("op") == 1
    done = _text(frames)
    assert "nothing was applied" not in done and "done" in done, done


def test_apply_after_a_dropped_card_does_not_say_nothing_from_it_was_applied(app_env, monkeypatch):
    client, store, facts, root = app_env
    _card(_preview(client, monkeypatch))
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="mute clip 1")
    second = _card(_preview(client, monkeypatch, plan=plan, message="mute clip 1"))
    assert "Dropped the earlier preview" in (second["preview"]["note"] or "")
    frames = _frames(client.post(f"/api/sessions/{SID}/prompt/answer",
                                 json={"token": second["token"], "apply": True}).text)
    _join()
    assert [f["type"] for f in frames].count("op") == 1
    assert "nothing from it was applied" not in _text(frames), _text(frames)


def test_a_transcript_landing_during_the_dry_run_survives_it(app_env, monkeypatch):
    """HIGH: 'add captions' right after an import — the dry run waited for the
    upload's transcript, built the card from it, then restored ingest.json
    to its pre-transcript bytes (the snapshot was taken BEFORE the wait)."""
    client, store, facts, root = app_env
    live = _live(root)
    src = Path(live.edl.get_track("v1").clips[0].src)
    F.write_ingest(src, with_transcript=False)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: F.facts_for(st, transcript_pending=True))
    landed = {"n": 0}

    def arrives(st) -> bool:
        if landed["n"] == 0:
            F.write_ingest(src)            # what main._bg_transcribe writes
        landed["n"] += 1
        return True

    monkeypatch.setattr(executor, "_transcript_present", arrives)
    plan = F.plan_of(F.step("transcribe", model="small"),
                     F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="transcribe and mute")
    _card(_preview(client, monkeypatch, plan=plan, message="transcribe and mute clip 1"))
    body = json.loads((src.parent / "ingest.json").read_text(encoding="utf-8"))
    assert body.get("transcript", {}).get("segments"), "the landed transcript was erased by the preview"
    # Change drops the card: the transcript stays
    tok = pending.load_pending(Path(live.dir))["token"]
    client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": tok, "apply": False})
    body = json.loads((src.parent / "ingest.json").read_text(encoding="utf-8"))
    assert body.get("transcript", {}).get("segments")


def test_a_file_the_dry_run_did_not_touch_is_not_restored(app_env, monkeypatch):
    """The no-wait race: the background transcriber writes ingest.json while
    a dry run's steps run; no step touched the file, so the preview must
    not put the old bytes back."""
    client, store, facts, root = app_env
    live = _live(root)
    src = Path(live.edl.get_track("v1").clips[0].src)
    F.write_ingest(src, with_transcript=False)
    ingest = src.parent / "ingest.json"
    real = executor._dispatch_step
    wrote = {"n": 0}

    def step_then_background_write(*a, **k):
        out = real(*a, **k)
        if wrote["n"] == 0:
            wrote["n"] += 1
            F.write_ingest(src)            # lands between two steps
        return out

    monkeypatch.setattr(executor, "_dispatch_step", step_then_background_write)
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True),
                     F.step("set_clip_muted", clip_id="$v1_last", muted=True), title="mute both")
    _card(_preview(client, monkeypatch, plan=plan, message="mute clip 1 and the last clip"))
    assert json.loads(ingest.read_text(encoding="utf-8")).get("transcript", {}).get("segments")


def test_a_cancel_during_the_dry_run_shows_no_card(app_env, monkeypatch):
    """HIGH: a Cancel acknowledged while the diff was being written still
    saved and showed the card."""
    client, store, facts, root = app_env
    live = _live(root)
    real = executor.run_plan

    def cancelled_at_the_end(*a, **k):
        out = real(*a, **k)
        executor.get_run(SID).cancel()
        return out

    monkeypatch.setattr(executor, "run_plan", cancelled_at_the_end)
    frames = _preview(client, monkeypatch)
    assert not [f for f in frames if f["type"] == "clarify"], [f["type"] for f in frames]
    assert pending.load_pending(Path(live.dir)) is None
    assert "Cancelled" in (executor.get_run(SID).final_text or ""), executor.get_run(SID).final_text
