"""Preview, then apply (0.8.0): the key-free Prompt bar changes nothing until
the person presses Apply (agent/prompt/preview.py).

Over the real FastAPI app (the prompt routes a desktop uses), with "Ask
before applying Prompt bar edits" ON (`VAI_PROMPT_CONFIRM=1`; the shared
harness runs the older suites with it OFF — tests/conftest.py):

  * a preview is a DRY RUN: the live EDL, ops, undo depth, redo stack and
    every file of the session are byte-identical afterwards (only the
    prompt feature's own bookkeeping — the pending card, the run record,
    chat.json — is written), no project is created and no scratch is left;
  * the card's lines come from the EDL diff and the frame says "Nothing has
    changed yet";
  * Apply commits the SAME plan as ONE op / one undo step, and the tree it
    commits is the dry run's (same fingerprint); a stale base re-plans and
    shows a fresh card (in the service AND, raced, inside the run lock); a
    result that differs from its card is rolled back and re-drawn;
  * Change / Cancel / a new prompt drop the card with nothing committed;
  * the setting OFF, or the Claude brain, runs at once (the old behaviour);
  * the K3 net still turns a wrong dry run into the same question;
  * the setting's route is loopback, same-origin and JSON only.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.agent.prompt import executor, pending, preview as PV, service  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")

SID = "s_preview"
#: The prompt feature's own files — the only ones a preview may write.
BOOKKEEPING = {"prompt_pending.json", "prompt_run.json", "chat.json"}


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setenv("VAI_PROMPT_CONFIRM", "1")
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    monkeypatch.setattr(executor, "_validate_plan", F.identity_validator)
    _main._STORES.clear()
    F.make_store(tmp_path, name=SID)
    store = _main._store(SID)          # the app's own cached store for the session
    D.dispatch(store, "split_at", {"track": "v1", "time": 6.0})
    facts = F.facts_for(store)
    monkeypatch.setattr(service, "build_facts_for", lambda st, ui: F.facts_for(st))
    yield TestClient(_main.app), store, facts, tmp_path
    _main._STORES.clear()


def _frames(text: str) -> list[dict]:
    return [json.loads(f[len("data: "):]) for f in text.split("\n\n") if f.startswith("data: ")]


def _join():
    h = executor.get_run(SID)
    if h is not None and h.thread is not None:
        h.thread.join(20.0)


def _live(tmp_path: Path):
    from video_ai_editor import main as _main
    return _main._store(SID)


def _files(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in BOOKKEEPING:
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _state(store, root: Path) -> dict:
    return {"edl": store.edl.to_json(), "ops": store.ops.model_dump_json(), "undo": store.undo_depth,
            "redo": store.redo_available, "files": _files(root)}


def _plan(title: str = "slow and warm"):
    return F.plan_of(F.step("set_speed", clip_id="$v1_first", factor=0.5),
                     F.step("apply_lut", clip_id="$v1_all", src="warm.cube"),
                     F.step("set_aspect_ratio", ratio="9:16"), title=title)


def _preview(client, monkeypatch, plan=None, message="slow clip 1 to half speed, give all clips a warm look and make it 9:16") -> list[dict]:
    F.route_with(monkeypatch, F.FakeRouted(plan or _plan()))
    r = client.post(f"/api/sessions/{SID}/prompt", json={"message": message})
    assert r.status_code == 200, r.text
    _join()
    return _frames(r.text)


def _card(frames: list[dict]) -> dict:
    cards = [f for f in frames if f["type"] == "clarify" and f.get("preview")]
    assert cards, "\n".join(str((f["type"], f.get("text") or f.get("message") or f.get("error")
                                  or f.get("status"))) for f in frames)
    return cards[-1]


# ------------------------------------------------------------------ the setting

@pytest.fixture
def clean_setting():
    """The suite shares one sandboxed settings.json: start and end without
    the prompt section."""
    from video_ai_editor.api import pairing

    def drop(data: dict) -> dict:
        return {k: v for k, v in data.items() if k != "prompt"}

    pairing._mutate(drop)
    yield
    pairing._mutate(drop)


@pytest.mark.usefixtures("clean_setting")
def test_setting_defaults_on_and_env_overrides(monkeypatch):
    from video_ai_editor import prompt_setting
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    assert prompt_setting.confirm_before_apply() == (True, "default")
    prompt_setting.set_confirm_before_apply(False)
    try:
        assert prompt_setting.confirm_before_apply() == (False, "settings")
        monkeypatch.setenv("VAI_PROMPT_CONFIRM", "1")
        assert prompt_setting.confirm_before_apply() == (True, "env")
    finally:
        prompt_setting.set_confirm_before_apply(True)
    monkeypatch.delenv("VAI_PROMPT_CONFIRM")
    assert prompt_setting.confirm_before_apply() == (True, "settings")
    with pytest.raises(ValueError):
        prompt_setting.set_confirm_before_apply("no")


@pytest.mark.usefixtures("clean_setting")
def test_setting_route_is_loopback_same_origin_json(app_env, monkeypatch):
    client, *_ = app_env
    monkeypatch.delenv("VAI_PROMPT_CONFIRM", raising=False)
    from video_ai_editor import prompt_setting
    assert client.get("/api/settings/prompt").json() == {"confirm_before_apply": True, "source": "default",
                                                        "default": True}
    r = client.put("/api/settings/prompt", content="confirm_before_apply=false",
                   headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert client.put("/api/settings/prompt", json={"confirm_before_apply": "no"}).status_code == 400
    assert client.put("/api/settings/prompt", json={"confirm_before_apply": False, "x": 1}).status_code == 400
    r = client.put("/api/settings/prompt", json={"confirm_before_apply": False},
                   headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    try:
        r = client.put("/api/settings/prompt", json={"confirm_before_apply": False})
        assert r.status_code == 200 and r.json()["confirm_before_apply"] is False
        assert prompt_setting.confirm_before_apply() == (False, "settings")
    finally:
        prompt_setting.set_confirm_before_apply(True)


# ------------------------------------------------------------------ the dry run

def test_preview_changes_nothing_byte_for_byte(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    before = _state(live, Path(live.dir))
    sessions_before = sorted(p.name for p in root.glob("s_*"))
    frames = _preview(client, monkeypatch)
    assert "op" not in [f["type"] for f in frames]
    assert _state(live, Path(live.dir)) == before
    assert sorted(p.name for p in root.glob("s_*")) == sessions_before
    scratch = root / PV.PREVIEW_DIR
    assert not scratch.exists() or not any(scratch.iterdir()), list(scratch.rglob("*"))
    # a fresh store read from disk agrees (nothing reached edl.json / ops.json)
    from video_ai_editor.edl import EDLStore
    assert EDLStore(Path(live.dir)).edl.to_json() == before["edl"]


def test_preview_card_lists_the_diff_and_says_nothing_changed(app_env, monkeypatch):
    client, *_ = app_env
    frames = _preview(client, monkeypatch)
    card = _card(frames)
    p = card["preview"]
    assert p["summary"] == "slow and warm: 3 changes" and p["total"] == 3
    assert "Clip 1 'talk.mp4': speed 1x -> 0.5x (6.0 s -> 12.0 s)" in p["lines"]
    assert "All 2 clips of the video: added look 'warm'" in p["lines"]
    # 9:16 on a 9:16 canvas changes nothing, so the card does not claim it
    assert p["lines"] == ["Clip 1 'talk.mp4': speed 1x -> 0.5x (6.0 s -> 12.0 s)",
                          "All 2 clips of the video: added look 'warm'",
                          "1 later clip moves to keep the video continuous"]
    assert p["nothing_changed"] == "Nothing has changed yet." and p["more"] == 0
    assert [q["key"] for q in card["questions"]] == ["apply"] and card["questions"][0]["kind"] == "confirm"
    reply = "".join(f["text"] for f in frames if f["type"] == "text_delta")
    assert "Nothing has changed yet." in reply and "- Clip 1 'talk.mp4': speed 1x -> 0.5x" in reply
    assert reply.startswith("via Recipes — Preview — slow and warm:")
    got = client.get(f"/api/sessions/{SID}/prompt/pending").json()["pending"]
    assert got["token"] == card["token"] and got["preview"]["lines"] == p["lines"]
    assert got["questions"][0]["key"] == "apply"
    assert executor.get_run(SID).log.record.status == "clarify"


def test_apply_commits_exactly_the_previewed_tree_as_one_undo_step(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    hash0, ops0, depth0 = live.edl.hash(), len(live.ops.ops), live.undo_depth
    captured: dict = {}
    real = PV.pause_for_confirm

    def spy(*a, **k):
        captured["after"] = k["after"].model_copy(deep=True)
        captured["before"] = k["before"].model_copy(deep=True)
        return real(*a, **k)

    monkeypatch.setattr(PV, "pause_for_confirm", spy)
    card = _card(_preview(client, monkeypatch))
    record = pending.load_pending(Path(live.dir))
    r = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": True})
    frames = _frames(r.text)
    _join()
    types = [f["type"] for f in frames]
    assert types.count("op") == 1 and "error" not in types and types[-1] == "done", types
    assert len(live.ops.ops) == ops0 + 1 and live.ops.ops[-1].tool == "prompt"
    assert live.undo_depth == depth0 + 1
    # the committed tree IS the dry run's (fresh ids renamed, scratch paths mapped)
    assert C.canonical(live.edl, captured["before"]) == record["preview"]["fingerprint"]
    assert C.canonical(live.edl, captured["before"]) == C.canonical(
        captured["after"], captured["before"], {str(Path(root / PV.PREVIEW_DIR)): ""}) or \
        [c.text for c in C.summarize(captured["before"], live.edl)] == record["preview"]["all_lines"]
    assert [c.text for c in C.summarize(captured["before"], live.edl, session_dir=Path(live.dir))] \
        == record["preview"]["all_lines"]
    assert pending.load_pending(Path(live.dir)) is None
    # one ⌘Z takes the whole prompt back
    D.dispatch(live, "undo", {})
    assert live.edl.hash() == hash0


def test_change_and_cancel_leave_nothing(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    before = _state(live, Path(live.dir))
    card = _card(_preview(client, monkeypatch))
    r = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": False})
    frames = _frames(r.text)
    assert [f["type"] for f in frames] == ["text_delta", "done"]
    assert "nothing was changed" in frames[0]["text"]
    assert pending.load_pending(Path(live.dir)) is None
    assert _state(live, Path(live.dir)) == before
    card = _card(_preview(client, monkeypatch))
    assert client.post(f"/api/sessions/{SID}/prompt/cancel", json={"token": card["token"]}).json()[
        "cancelled"]["pending"] == card["token"]
    assert _state(live, Path(live.dir)) == before
    stale = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": True})
    assert _frames(stale.text)[0]["type"] == "error"
    assert _state(live, Path(live.dir)) == before


def test_stale_base_replans_and_never_applies_the_old_preview(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    card = _card(_preview(client, monkeypatch))
    # someone edits the timeline under the card
    r = client.post(f"/api/sessions/{SID}/dispatch", json={"tool": "set_clip_muted", "args": {
        "clip_id": live.edl.get_track("v1").clips[0].id, "muted": True}})
    assert r.status_code == 200, r.text
    ops_after_edit = len(live.ops.ops)
    hash_after_edit = live.edl.hash()
    r = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": True})
    frames = _frames(r.text)
    _join()
    assert "op" not in [f["type"] for f in frames]
    fresh = _card(frames)
    assert fresh["token"] != card["token"]
    assert fresh["preview"]["note"].startswith("The timeline changed since the preview, so nothing was applied.")
    assert len(live.ops.ops) == ops_after_edit and live.edl.hash() == hash_after_edit
    assert pending.load_pending(Path(live.dir))["preview"]["base_hash"] == hash_after_edit


def test_stale_inside_the_run_lock_runs_nothing(app_env, monkeypatch):
    """The race: the service saw the base hash, then an edit landed before
    the run thread took the session lock. The thread re-checks and runs
    nothing."""
    client, store, facts, root = app_env
    live = _live(root)
    hash0, ops0 = live.edl.hash(), len(live.ops.ops)
    plan = _plan()
    handle = executor.start_run(lambda sid: live, SID, plan, F.facts_for(live), prompt="x",
                                mode="apply", preview={"base_hash": "not-the-live-hash", "fingerprint": "x",
                                                       "all_lines": []})
    handle.thread.join(20.0)
    assert handle.stale and handle.result is None
    assert live.edl.hash() == hash0 and len(live.ops.ops) == ops0
    assert handle.final_text == executor.STALE_PREVIEW_TEXT


def test_apply_that_comes_out_differently_is_rolled_back_and_redrawn(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    hash0, ops0 = live.edl.hash(), len(live.ops.ops)
    card = _card(_preview(client, monkeypatch))
    rec = pending.load_pending(Path(live.dir))
    rec["preview"]["fingerprint"] = "0" * 24
    rec["preview"]["all_lines"] = ["something the card never said"]
    pending.pending_path(Path(live.dir)).write_text(json.dumps(rec), encoding="utf-8")
    r = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": True})
    frames = _frames(r.text)
    _join()
    assert "op" not in [f["type"] for f in frames]
    again = _card(frames)
    assert again["preview"]["note"].startswith("Applying it came out differently from the preview")
    assert live.edl.hash() == hash0 and len(live.ops.ops) == ops0


def test_a_new_prompt_drops_the_open_card(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    first = _card(_preview(client, monkeypatch))
    plan2 = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="mute clip 1")
    frames = _preview(client, monkeypatch, plan=plan2, message="mute the first clip")
    second = _card(frames)
    assert second["token"] != first["token"]
    assert second["preview"]["lines"] == ["Clip 1 'talk.mp4': muted"]
    assert pending.load_pending(Path(live.dir))["token"] == second["token"]
    stale = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": first["token"], "apply": True})
    assert _frames(stale.text)[0]["type"] == "error"
    assert not any(c.audio.mute for c in live.edl.get_track("v1").clips)


def test_typed_yes_applies_like_the_button(app_env, monkeypatch):
    """The chat pane and the phone read the card as text: a whole-message
    'yes' answers it."""
    client, store, facts, root = app_env
    live = _live(root)
    ops0 = len(live.ops.ops)
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="mute clip 1")
    _card(_preview(client, monkeypatch, plan=plan, message="mute the first clip"))
    r = client.post(f"/api/sessions/{SID}/prompt", json={"message": "yes"})
    frames = _frames(r.text)
    _join()
    assert [f["type"] for f in frames].count("op") == 1
    assert len(live.ops.ops) == ops0 + 1 and live.edl.get_track("v1").clips[0].audio.mute


def test_setting_off_runs_at_once(app_env, monkeypatch):
    client, store, facts, root = app_env
    monkeypatch.setenv("VAI_PROMPT_CONFIRM", "0")
    live = _live(root)
    ops0 = len(live.ops.ops)
    frames = _preview(client, monkeypatch)
    types = [f["type"] for f in frames]
    assert "clarify" not in types and types.count("op") == 1
    assert len(live.ops.ops) == ops0 + 1


def test_the_claude_brain_is_never_previewed(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    ops0 = len(live.ops.ops)
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), brain="claude", title="mute")
    frames = _preview(client, monkeypatch, plan=plan, message="mute the first clip")
    assert "clarify" not in [f["type"] for f in frames]
    assert len(live.ops.ops) == ops0 + 1


def test_the_net_still_turns_a_wrong_dry_run_into_a_question(app_env, monkeypatch):
    """"make the first clip slower" planned as 2x: the K3 contract refuses
    it in the dry run exactly as in a real run — a question, saved in the
    LIVE session, not a preview card."""
    client, store, facts, root = app_env
    live = _live(root)
    before = _state(live, Path(live.dir))
    plan = F.plan_of(F.step("set_speed", clip_id="$v1_first", factor=2.0), title="slower")
    frames = _preview(client, monkeypatch, plan=plan, message="make the first clip slower")
    asked = [f for f in frames if f["type"] == "clarify"]
    assert asked and not asked[-1].get("preview"), frames
    # nothing was ever applied in a preview: no "I undid that" (final sweep 3)
    q = asked[-1]["questions"][0]["question"]
    assert q.startswith("I did not offer that plan:") and "undid" not in q, q
    rec = pending.load_pending(Path(live.dir))
    assert rec is not None and not pending.is_preview(rec) and rec.get("rollback")
    after = _state(live, Path(live.dir))
    assert after == before


def test_a_plan_that_changes_nothing_gets_no_card(app_env, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=False), title="unmute")
    frames = _preview(client, monkeypatch, plan=plan, message="unmute the first clip")
    assert "clarify" not in [f["type"] for f in frames]
    text = "".join(f.get("text", "") for f in frames if f["type"] == "text_delta")
    assert "would not change anything" in text and pending.load_pending(Path(live.dir)) is None


def test_dry_run_makes_no_short_projects(app_env, monkeypatch):
    """make_shorts saves new projects on Apply only: the dry run reports the
    picks and creates nothing."""
    client, store, facts, root = app_env
    live = _live(root)
    before = sorted(p.name for p in root.glob("s_*"))
    calls = []

    def fake_shorts(src, transcript, cache, **kw):
        calls.append(kw)
        return [{"source_start": 0.0, "source_end": 5.0, "hook": "hi"},
                {"source_start": 6.0, "source_end": 11.0, "hook": "bye"}]

    import video_ai_editor.ai.shorts as shorts_mod
    monkeypatch.setattr(shorts_mod, "make_shorts", fake_shorts)
    plan = F.plan_of(F.step("make_shorts", target_count=2, max_dur=6.0, min_dur=3.0, save_as_sessions=True),
                     title="2 shorts")
    card = _card(_preview(client, monkeypatch, plan=plan, message="make 2 shorts"))
    assert calls, "the dry run ran the picker"
    assert sorted(p.name for p in root.glob("s_*")) == before
    assert card["preview"]["lines"][0].startswith("Create 2 new short projects (00:00:00:00-00:00:05:00")


def test_files_a_step_rewrites_beside_the_media_are_restored(app_env, monkeypatch):
    """auto_caption / transcribe rewrite the upload's ingest.json, which sits
    beside the media — outside any session copy. The dry run's side-effect
    snapshot puts it back byte for byte; Apply writes it for real."""
    client, store, facts, root = app_env
    live = _live(root)
    ingest = Path(live.edl.get_track("v1").clips[0].src).parent / "ingest.json"
    original = ingest.read_bytes()
    real = D.DISPATCH["set_clip_muted"]

    def muting_and_rewriting(st, args, **kw):
        ingest.write_text(json.dumps({"rewritten": True}), encoding="utf-8")
        return real(st, args, **kw)

    monkeypatch.setitem(D.DISPATCH, "set_clip_muted", muting_and_rewriting)
    before = _state(live, Path(live.dir))
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="mute clip 1")
    card = _card(_preview(client, monkeypatch, plan=plan, message="mute clip 1"))
    assert ingest.read_bytes() == original
    assert _state(live, Path(live.dir)) == before
    client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": True})
    _join()
    assert json.loads(ingest.read_text(encoding="utf-8")) == {"rewritten": True}
    assert live.edl.get_track("v1").clips[0].audio.mute


def test_the_key_free_chat_pane_gets_the_card_as_a_yes_no(app_env, monkeypatch):
    """The chat pane without a key delegates to the same turn: it receives the
    card as a yes/no question with the list in the reply text, and a typed
    "yes" applies it."""
    client, store, facts, root = app_env
    from video_ai_editor import config
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    live = _live(root)
    ops0 = len(live.ops.ops)
    plan = F.plan_of(F.step("set_clip_muted", clip_id="$v1_first", muted=True), title="mute clip 1")
    F.route_with(monkeypatch, F.FakeRouted(plan))
    frames = _frames(client.post(f"/api/sessions/{SID}/chat", json={"message": "mute clip 1"}).text)
    _join()
    card = _card(frames)
    assert card["questions"][0]["kind"] == "confirm"
    text = "".join(f["text"] for f in frames if f["type"] == "text_delta")
    assert "- Clip 1 'talk.mp4': muted" in text and "Reply **yes** to apply" in text
    assert len(live.ops.ops) == ops0
    frames = _frames(client.post(f"/api/sessions/{SID}/chat", json={"message": "yes"}).text)
    _join()
    assert [f["type"] for f in frames].count("op") == 1
    assert len(live.ops.ops) == ops0 + 1
