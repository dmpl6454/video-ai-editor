"""Wave-B lane B6 regressions: edit operations and the Prompt Editor.

QA-046 undo horizon, QA-057 Cmd+D order on the frame grid, QA-021-TAIL no stub
past the end, QA-067 Find moments in timeline time, QA-069 reel length on a
sentence boundary, QA-070 every seam gets a transition, QA-071 short caption
chunks, QA-072 hook text resolved after the transcript, QA-073 title case and
stacking, QA-074 restyle keeps cue edits, QA-018 Apple Intelligence slots.
Every test drives the real `dispatch()`, planner or FastAPI app.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip, TextClip


def _store(tmp_path: Path, durs=(10.0, 10.0, 20.0), fps: float = 30) -> EDLStore:
    s = EDLStore(tmp_path)
    s.edl.canvas.fps = fps
    v1 = s.edl.get_track("v1")
    t = 0.0
    for i, d in enumerate(durs):
        v1.clips.append(Clip(id=f"c_{i}", src=f"/x/{i}.mp4", in_=0.0, out=d, start=t))
        t += d
    from video_ai_editor.agent.dispatch import _ripple_close_gap
    _ripple_close_gap(v1, fps)
    s.commit("seed", {}, "seed")
    return s


def _v1_ids(s: EDLStore) -> list[str]:
    return [c.id for c in s.edl.get_track("v1").clips]


# ---------------------------------------------------------------- QA-057

@pytest.mark.parametrize("fps,durs", [
    (29.97, (10.02, 9.987, 20.0)),     # the grid pulls the neighbour in before start+footprint
    (29.97, (10.017, 10.0, 20.0)),
    (30, (10.0, 10.0, 20.0)),
])
def test_duplicate_lands_right_after_its_original_on_a_fractional_grid(tmp_path, fps, durs):
    s = _store(tmp_path, durs, fps)
    new = dispatch(s, "duplicate_clip", {"clip_id": "c_0"})["new_clip_id"]
    assert _v1_ids(s) == ["c_0", new, "c_1", "c_2"]
    new2 = dispatch(s, "bulk_duplicate", {"clip_ids": ["c_1"]})["new_ids"][0]
    assert _v1_ids(s) == ["c_0", new, "c_1", new2, "c_2"]


# ---------------------------------------------------------------- QA-021-TAIL

def _title(s: EDLStore, start: float, end: float, text: str = "T") -> str:
    return dispatch(s, "add_text", {"text": text, "start": start, "end": end})["id"]


def _texts(s: EDLStore) -> list[TextClip]:
    return [c for t in s.edl.tracks for c in t.clips if isinstance(c, TextClip)]


def test_deleting_the_last_clip_under_a_title_leaves_no_stub_past_the_end(tmp_path):
    s = _store(tmp_path)
    _title(s, 22.0, 38.0)
    dispatch(s, "ripple_delete", {"clip_id": "c_2"})
    v1_end = max(c.start + c.effective_duration for c in s.edl.get_track("v1").clips)
    assert _texts(s) == []
    assert s.edl.duration == pytest.approx(v1_end) == pytest.approx(20.0)


def test_bulk_deleting_the_tail_leaves_no_stub_but_middle_orphans_keep_one(tmp_path):
    s = _store(tmp_path, (10.0, 10.0, 10.0, 10.0))
    _title(s, 12.0, 18.0, "middle")     # over c_1 (deleted, footage follows)
    _title(s, 32.0, 38.0, "tail")       # over c_3 (deleted, nothing follows)
    dispatch(s, "bulk_delete", {"clip_ids": ["c_1", "c_3"]})
    texts = _texts(s)
    assert [t.text for t in texts] == ["middle"]
    assert s.edl.duration == pytest.approx(20.0)


def test_cutting_the_tail_off_drops_orphaned_captions(tmp_path):
    s = _store(tmp_path, (40.0,))
    _title(s, 35.0, 38.0)
    dispatch(s, "cut_range", {"track": "v1", "start": 30.0, "end": 40.0})
    assert _texts(s) == [] and s.edl.duration == pytest.approx(30.0)


# ---------------------------------------------------------------- QA-046

def test_forty_edits_are_all_undoable_and_the_depth_is_honest(tmp_path):
    s = _store(tmp_path)
    for i in range(40):
        dispatch(s, "add_marker", {"time": i * 0.1, "label": f"m{i}"})
    assert s.undo_depth == 41                      # 40 markers + the seed
    for i in range(40):
        r = dispatch(s, "undo", {})
        assert r["ok"], f"undo {i + 1} of 40 refused"
        assert r["undo_depth"] == 41 - (i + 1)
    assert s.edl.markers == []


def test_undo_stops_exactly_at_the_budget_and_says_so(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(EDLStore, "MAX_UNDO", 6)
    for i in range(10):
        dispatch(s, "add_marker", {"time": i * 0.1, "label": f"m{i}"})
    depth = s.undo_depth
    assert depth == 5
    for _ in range(depth):
        assert dispatch(s, "undo", {})["ok"]
    r = dispatch(s, "undo", {})
    assert r["ok"] is False and r["undo_depth"] == 0
    assert len(s.edl.markers) == 5


def test_byte_budget_keeps_at_least_one_step(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(EDLStore, "UNDO_DISK_BUDGET_BYTES", 1)
    for i in range(5):
        dispatch(s, "add_marker", {"time": i * 0.1, "label": f"m{i}"})
    assert s.undo_depth == 1
    assert len(list(s.snapshots_dir.glob("*.json"))) == 2


def test_a_fresh_project_has_nothing_to_undo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    client = TestClient(_main.app)
    sid = client.post("/api/sessions", json={"name": "fresh"}).json()["id"]
    info = client.get(f"/api/sessions/{sid}").json()
    assert info["undo_depth"] == 0 and len(info["ops"]) == 1
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "undo", "args": {}}).json()
    assert r["result"]["ok"] is False
    assert len(client.get(f"/api/sessions/{sid}").json()["ops"]) == 1, "init must stay in History"
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "add_marker", "args": {"time": 1.0}}).json()
    assert r["undo_depth"] == 1
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "undo", "args": {}}).json()
    assert r["result"]["ok"] is True and r["result"]["undo_depth"] == 0
    _main._STORES.clear()


# ---------------------------------------------------------------- QA-067

def _write_transcript(d: Path, segs: list[tuple[float, float, str]], duration: float) -> None:
    """An imported transcript (the import_srt file) with evenly spread words."""
    import json
    out = []
    for i, (st, en, text) in enumerate(segs):
        toks = text.split()
        step = (en - st) / len(toks)
        words = [{"start": round(st + k * step, 3), "end": round(st + (k + 1) * step, 3),
                  "word": " " + w} for k, w in enumerate(toks)]
        out.append({"id": i, "start": st, "end": en, "text": text, "words": words})
    (d / "transcript.json").write_text(
        json.dumps({"segments": out, "language": "en", "duration": duration}), encoding="utf-8")


def _transcribed(tmp_path: Path) -> EDLStore:
    s = _store(tmp_path, (60.0,))
    _write_transcript(tmp_path, [
        (2.0, 8.0, "hello and welcome to the review"),
        (12.0, 15.0, "the battery life of the old model was poor"),
        (39.08, 49.04, "battery life is the best part of this camera"),
    ], 60.0)
    return s


@pytest.fixture
def no_vision(monkeypatch):
    from video_ai_editor.ai import vision
    monkeypatch.setattr(vision, "shot_index_for", lambda src, cache: [])
    monkeypatch.setattr(vision, "describe_shot", lambda *a, **k: "")


def test_find_moments_marks_where_the_moment_plays_after_a_ripple_cut(tmp_path, no_vision):
    s = _transcribed(tmp_path)
    dispatch(s, "cut_range", {"track": "v1", "start": 10.0, "end": 16.5})   # a removed stretch
    r = dispatch(s, "find_moments", {"query": "battery life", "top_k": 3})
    by_src = {m["source_start"]: m for m in r["matches"]}
    assert 12.0 not in by_src, "a match the edit cut away must not be offered"
    assert r["cut_away"] == 1
    m = by_src[39.08]
    assert (m["start"], m["end"]) == pytest.approx((32.58, 42.54), abs=1 / 30)


def test_make_shorts_rows_are_in_timeline_time(tmp_path, monkeypatch):
    s = _transcribed(tmp_path)
    dispatch(s, "cut_range", {"track": "v1", "start": 10.0, "end": 16.5})
    from video_ai_editor.ai import shorts
    monkeypatch.setattr(shorts, "make_shorts", lambda *a, **k: [
        {"start": 30.0, "end": 50.0, "score": 0.9, "why": "energy"}])
    r = dispatch(s, "make_shorts", {"target_count": 1})
    (sh,) = r["shorts"]
    assert (sh["start"], sh["end"]) == pytest.approx((23.5, 43.5), abs=1 / 30)
    assert (sh["source_start"], sh["source_end"]) == (30.0, 50.0)


# ---------------------------------------------------------------- prompt helpers

def _facts(store: EDLStore, **over):
    from video_ai_editor.agent.prompt.facts import build_facts
    f = build_facts(store, {}, feature_report={})
    return f.with_(**over) if over else f


def _run(store: EDLStore, plan, facts):
    from video_ai_editor.agent.prompt import executor
    events: list[dict] = []
    res = executor.run_plan(store, plan, facts, emit=events.append, cancel_event=None,
                            prompt="test", wait_transcript=False)
    assert res.error is None, res.error
    return res, events


def _plan(prompt: str, facts):
    from video_ai_editor.agent.prompt import planner
    return planner.plan(prompt, facts, allow_downloads=False)


# ---------------------------------------------------------------- QA-069

_REEL_SENTENCES = [
    (0.5, 7.5, "This camera is small enough to carry anywhere."),
    (8.0, 15.0, "The screen flips all the way around for selfies."),
    (15.5, 24.4, "Battery life got me through a full day of shooting."),
    (25.0, 33.0, "It tracks faces across the whole frame, even when I walk out."),
    (33.5, 40.0, "That is my review of this little camera."),
]


def test_a_30s_reel_ends_on_the_last_sentence_that_fits(tmp_path):
    from video_ai_editor.agent.prompt import planner
    from video_ai_editor.agent.prompt.recipes import Intent
    from video_ai_editor.agent.prompt import slots as S
    s = _store(tmp_path, (45.0,))
    _write_transcript(tmp_path, _REEL_SENTENCES, 45.0)
    f = _facts(s)
    it = Intent("trim", {"range": S.TimeRange(kind="abs", start=30.0, end=None),
                         "_optional": True, "_max_s": 30.0})
    plan = planner.compose([it], f)
    res, _ = _run(s, plan, f)
    extent = s.edl.video_extent()
    # "It tracks faces …" straddles 30 s: the reel ends after "… shooting." (24.4 s + pad).
    # Wave C (best window): this opening IS the best-scoring window, and the
    # 0.5 s of silence before its first word is trimmed as dead air.
    assert 23.9 <= extent <= 24.6, extent
    notices = [n for st in res.steps for n in st.notices]
    assert any("sentence" in n for n in notices), notices


def test_a_reel_already_short_enough_is_left_alone(tmp_path):
    from video_ai_editor.agent.prompt import live
    s = _store(tmp_path, (20.0,))
    fc = live.fit_cut(s, 30.0)
    assert fc.start is None and "within" in fc.notices[0]


# ---------------------------------------------------------------- QA-070

def _chopped(tmp_path: Path, n: int, sliver_every: int = 0) -> EDLStore:
    durs = []
    for i in range(n):
        durs.append(0.1 if sliver_every and i % sliver_every == sliver_every - 1 else 2.0)
    return _store(tmp_path, tuple(durs))


def test_transition_between_every_clip_covers_all_16_seams(tmp_path):
    s = _chopped(tmp_path, 17)
    f = _facts(s)
    plan = _plan("add a smooth zoom transition between every clip", f)
    res, _ = _run(s, plan, f)
    assert len(s.edl.get_track("v1").transitions) == 16


def test_per_seam_steps_name_the_seam_in_timecode(tmp_path):
    s = _store(tmp_path, (13.7279, 5.0, 5.0))
    plan = _plan("add a smooth zoom transition between every clip", _facts(s))
    whys = [st.why for st in plan.steps if st.tool == "add_transition"]
    assert whys and all("s seam" not in w for w in whys), whys
    assert any("00:00:13:22 seam" in w for w in whys), whys


def test_sliver_seams_are_skipped_and_said(tmp_path):
    from video_ai_editor.agent.prompt import summary
    s = _chopped(tmp_path, 9, sliver_every=3)          # clips 2, 5, 8 are 0.1 s slivers
    f = _facts(s)
    plan = _plan("add a smooth zoom transition between every clip", f)
    res, _ = _run(s, plan, f)
    trs = s.edl.get_track("v1").transitions
    assert trs and all(t.duration >= 0.3 for t in trs)
    assert len(trs) == 3                               # only seams between two 2 s clips
    assert "shorter than 0.3 s" in (plan.reply or "")
    assert "shorter than 0.3 s" in summary.compose_reply(plan, res, None)


def test_live_seam_fanout_is_not_capped_at_12_and_reports_a_cap(tmp_path, monkeypatch):
    from video_ai_editor.agent.prompt import executor, live
    s = _chopped(tmp_path, 30)
    fan = live.seam_fanout(s.edl)
    assert len(fan.seams) == 29 and not fan.capped
    monkeypatch.setattr(live, "MAX_SEAM_FANOUT", 10)
    fan = live.seam_fanout(s.edl)
    assert len(fan.seams) == 10 and fan.capped == 19
    assert "capped at 10 of 29 seams" in fan.notices(30)


# ---------------------------------------------------------------- QA-071

_LONG_SEGMENT = [(0.0, 5.64, "Hey everyone, today I am taking a close look at this little camera, "
                  "and I have three things"),
                 (5.8, 9.0, "to tell you about it before you buy one.")]


def test_ig_chunky_captions_are_short_phrases_not_whole_segments(tmp_path):
    s = _store(tmp_path, (10.0,))
    _write_transcript(tmp_path, _LONG_SEGMENT, 10.0)
    dispatch(s, "add_caption_track", {"style": "ig_chunky"})
    cues = [c for c in s.edl.get_track("captions").clips if isinstance(c, TextClip)]
    words = [len(c.text.split()) for c in cues]
    assert len(cues) >= 4, [c.text for c in cues]
    assert max(words) <= 7 and all(c.text.count("\n") <= 1 for c in cues), [c.text for c in cues]
    assert all(c.end - c.start <= 3.0 + 1e-6 for c in cues)
    # Every word survives, in order.
    joined = " ".join(c.text.replace("\n", " ") for c in cues).split()
    assert joined == " ".join(t for _, _, t in _LONG_SEGMENT).split()


def test_default_style_keeps_one_cue_per_segment(tmp_path):
    s = _store(tmp_path, (10.0,))
    _write_transcript(tmp_path, _LONG_SEGMENT, 10.0)
    dispatch(s, "add_caption_track", {"style": "default"})
    assert len(s.edl.get_track("captions").clips) == 2


# ---------------------------------------------------------------- QA-074

def test_restyling_hand_edited_captions_keeps_the_edits(tmp_path):
    s = _store(tmp_path, (10.0,))
    _write_transcript(tmp_path, _LONG_SEGMENT, 10.0)
    dispatch(s, "add_caption_track", {"style": "default"})
    first = s.edl.get_track("captions").clips[0]
    dispatch(s, "set_property", {"clip_id": first.id, "path": "text", "value": "MY EDITED CUE"})
    dispatch(s, "set_clip_timing", {"clip_id": first.id, "start": 0.5, "end": 5.64})
    r = dispatch(s, "add_caption_track", {"style": "word_emphasis"})
    cap = s.edl.get_track("captions")
    assert cap.config.style == "word_emphasis"
    kept = s.edl.get_clip(first.id)[1]
    assert (kept.text, kept.start) == ("MY EDITED CUE", 0.5)
    assert len(cap.clips) == 2 and r["kept_edits"] == 1
    assert "kept" in r["notice"]
    # A second style change still keeps them (the restyle is not a rebuild).
    dispatch(s, "add_caption_track", {"style": "ig_chunky"})
    assert s.edl.get_clip(first.id)[1].text == "MY EDITED CUE"
    # An explicit rebuild re-lays from the transcript and says what it replaced.
    r = dispatch(s, "add_caption_track", {"style": "ig_chunky", "rebuild": True})
    assert s.edl.get_clip(first.id) is None and "replaced" in r["notice"]


def test_restyling_untouched_captions_rebuilds_them_for_the_new_style(tmp_path):
    s = _store(tmp_path, (10.0,))
    _write_transcript(tmp_path, _LONG_SEGMENT, 10.0)
    dispatch(s, "add_caption_track", {"style": "default"})
    dispatch(s, "add_caption_track", {"style": "word_emphasis"})
    cues = s.edl.get_track("captions").clips
    assert len(cues) > 2 and all(len(c.text.split()) <= 2 for c in cues)


# ---------------------------------------------------------------- QA-072

def test_hook_is_written_from_the_transcript_that_lands_after_planning(tmp_path):
    s = _store(tmp_path, (45.0,))
    f = _facts(s, transcript_pending=True)
    assert not f.transcript_head
    plan = _plan("add a hook", f)
    assert "generic" not in (plan.reply or "")
    _write_transcript(tmp_path, _REEL_SENTENCES, 45.0)           # the upload transcript lands
    res, _ = _run(s, plan, f)
    hook = [c for c in s.edl.tracks for c in c.clips
            if isinstance(c, TextClip) and c.role == "hook"]
    assert hook and hook[0].text != "WATCH THIS BEFORE YOU SCROLL"
    assert hook[0].text != "$hook_from_transcript"
    words = {w.strip(",.").upper() for _, _, t in _REEL_SENTENCES for w in t.split()}
    assert set(hook[0].text.split()) <= words, hook[0].text
    assert any("written once the transcript landed" in n for st in res.steps for n in st.notices)


def test_hook_with_no_speech_at_run_time_says_generic(tmp_path):
    s = _store(tmp_path, (45.0,))
    f = _facts(s, transcript_pending=True)
    res, _ = _run(s, _plan("add a hook", f), f)
    hook = [c for c in s.edl.tracks for c in c.clips if isinstance(c, TextClip) and c.role == "hook"]
    assert hook[0].text == "WATCH THIS BEFORE YOU SCROLL"
    assert any("generic" in n for st in res.steps for n in st.notices)


# ---------------------------------------------------------------- QA-073

def test_title_keeps_the_typed_case_and_a_second_title_is_reported(tmp_path):
    from video_ai_editor.agent.prompt import summary
    s = _store(tmp_path, (20.0,))
    f = _facts(s)
    p1 = _plan("add a title that says BIG SALE at the top for 3 seconds", f)
    _run(s, p1, f)
    assert [t.text for t in _texts(s)] == ["BIG SALE"]
    f = _facts(s)
    p2 = _plan('add a title "Grand Opening" at the top', f)
    res, events = _run(s, p2, f)
    reply = summary.compose_reply(p2, res, None)
    # Same place, same time: the new title replaces the old one — and SAYS so
    # in the run log and the reply instead of "Added".
    assert [t.text for t in _texts(s)] == ["Grand Opening"]
    assert "replaced 'BIG SALE'" in reply, reply
    assert any("Replaced text 'BIG SALE'" in (e.get("summary") or "") for e in events)


# ---------------------------------------------------------------- QA-018 remainder

def _fm_brain(bodies):
    """The real FMBrain with a runner that replays recorded helper answers
    (recorded from the real macOS 27 Apple Intelligence model on this Mac)."""
    import json as _json
    import sys as _sys
    from video_ai_editor.agent.prompt.brains import fm
    probe = {"ok": True, "available": True, "state": "available", "fix": None, "os": "27.0.0",
             "languages": ["en"]}
    queue = list(bodies)

    def runner(argv, stdin, timeout_s):
        if argv[-1] == "probe":
            return 0, _json.dumps(probe).encode(), b""
        return 0, _json.dumps(queue.pop(0)).encode(), b""

    return fm.FMBrain(runner=runner, helper=_sys.executable, darwin=True, macos=(27, 0), frozen=False)


def _fm_plan(prompt: str, draft: dict):
    from video_ai_editor.agent.prompt import recipes
    from video_ai_editor.agent.prompt.brains.base import BrainRequest
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    facts = TimelineFacts.minimal(has_music=True, music_ducked=True, music_clip_ids=["m_1"])
    body = {"ok": True, "model": "apple-fm", "latency_ms": 2900, "draft": draft}
    res = _fm_brain([body]).plan(BrainRequest(prompt=prompt, facts=facts, recipes=recipes.cards()),
                                 timeout_s=8)
    assert res.ok, res.reason
    return res.plan


def test_fm_slots_for_fade_volume_mute_duck_reach_the_recipes():
    from video_ai_editor.agent.prompt.brains.prompt_text import flatten_fm_item
    item = {"recipe": "volume", "change": "down", "db": -20.0, "edge": "out", "muted": True,
            "enabled": False, "target": "music"}
    assert flatten_fm_item(item)["slots"] == {"target": "music", "change": "down", "db": -20.0,
                                              "edge": "out", "muted": True, "enabled": False}
    plan = _fm_plan("bring the backing track down to -20", {
        "intents": [{"recipe": "volume", "target": "music", "db": -20.0}],
        "exclusions": [], "needs_input": [], "confidence": 0.9, "reply": "ok"})
    vol = [s for s in plan.steps if s.tool == "set_volume"]
    assert vol and vol[0].args.get("db") == -20.0, [(s.tool, s.args) for s in plan.steps]


def test_fm_duck_off_is_grounded_when_the_model_leaves_enabled_unset():
    # Recorded: "stop ducking the music" → {"intents":[{"recipe":"duck"}]}.
    plan = _fm_plan("stop ducking the music", {
        "intents": [{"recipe": "duck"}], "exclusions": ["duck the music"], "needs_input": [],
        "confidence": 0.9, "reply": "I've stopped ducking the music."})
    duck = [s for s in plan.steps if s.tool == "set_duck"]
    assert duck and duck[0].args["enabled"] is False


def test_fm_extra_recipes_the_prompt_never_named_are_dropped():
    # Recorded: "mute the music" → mute + voiceover; "fade out at the end" →
    # fade + trim + remove_music (a destructive bed delete nobody asked for).
    plan = _fm_plan("mute the music", {
        "intents": [{"recipe": "mute"}, {"recipe": "voiceover"}], "exclusions": ["do not mute the music"],
        "needs_input": [], "confidence": 0.9, "reply": "I will mute the music for you."})
    assert {s.tool for s in plan.steps} == {"set_track_muted"}, [s.tool for s in plan.steps]
    plan = _fm_plan("fade out at the end", {
        "intents": [{"recipe": "fade"}, {"recipe": "trim"}, {"recipe": "remove_music"}],
        "exclusions": ["add music"], "needs_input": [], "confidence": 0.9, "reply": "ok"})
    tools = {s.tool for s in plan.steps}
    assert not tools & {"cut_range", "bulk_delete", "tts_voiceover"}, tools
    assert tools & {"set_video_fade", "add_fade"}, tools


@pytest.mark.parametrize("prompt,intent,off", [
    ("could you make the song a touch softer", "volume", False),
    ("quiet the music a little", "volume", False),
    ("make the background song less loud", "volume", False),
    ("please silence the backing track", "mute", False),
    ("kill the audio on the soundtrack", "mute", False),
    ("have the song dip under speech", "duck", False),
    ("stop the music from lowering when I talk", "duck", True),
    ("keep the soundtrack steady under my voice", "duck", True),
    ("let the tune stop dipping when I talk", "duck", True),
    ("trim the song so it matches the clip", "fit_music", False),
    # found by the live Apple Intelligence pass (each was misread by the model)
    ("the music should not dip when I speak", "duck", True),
    ("make the music silent", "mute", False),
    ("hush the soundtrack completely", "mute", False),
    ("clip the tune when the picture stops", "fit_music", False),
    ("the tune keeps going after the video ends", "fit_music", False),
    ("make it fit a phone screen", "reframe", False),
])
def test_everyday_audio_paraphrases_route_to_their_edit_not_add_music(prompt, intent, off):
    """QA-018: every one of these read as `music` at 0.85 — above the run bar,
    so the recipes answered "music is already on the timeline"."""
    from video_ai_editor.agent.prompt import grammar as G
    d = G.detect(prompt)
    assert [h.intent for h in d.hits] == [intent] and d.confidence >= G.RUN_THRESHOLD
    assert G.duck_off(prompt) is off


def test_fm_invented_questions_and_reply_are_dropped_and_misreadings_grounded():
    # Recorded (live, macOS 27): "ease the picture in from black" came back
    # as a fade plus "Which platform?" (options: the music file's name).
    plan = _fm_plan("ease the picture in from black", {
        "intents": [{"recipe": "fade"}], "exclusions": [],
        "needs_input": [{"key": "platform", "question": "Which platform should I apply this to?",
                         "options": ["tiktok", "reels"]}],
        "confidence": 0.9, "reply": "I'll ease the picture in from black using the fade recipe."})
    assert not plan.blocking_questions and "recipe" not in (plan.reply or "")
    assert {s.tool for s in plan.steps} >= {"set_video_fade"}
    # "I want the backing track to sit lower" came back as ducking OFF. It
    # names no speaker, so (wave C, QA-018) it is a level: the bed goes down,
    # never ducking off (and no longer ducking on either).
    plan = _fm_plan("I want the backing track to sit lower", {
        "intents": [{"recipe": "duck", "enabled": False}], "exclusions": [], "needs_input": [],
        "confidence": 0.9, "reply": ""})
    assert [s.args.get("enabled") for s in plan.steps if s.tool == "set_duck"] == []
    assert [s.args.get("target") for s in plan.steps if s.tool == "set_volume"] == ["music"]
    # "slap LAUNCH DAY across the top" came back as a name card.
    plan = _fm_plan("slap LAUNCH DAY across the top", {
        "intents": [{"recipe": "title", "name": "LAUNCH DAY"}], "exclusions": [], "needs_input": [],
        "confidence": 0.9, "reply": ""})
    texts = [s.args.get("text") for s in plan.steps if s.tool == "add_text"]
    assert texts == ["LAUNCH DAY"], [(s.tool, s.args) for s in plan.steps]


def test_fm_draft_with_no_edit_falls_through():
    from video_ai_editor.agent.prompt import recipes
    from video_ai_editor.agent.prompt.brains.base import BrainRequest
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    body = {"ok": True, "model": "apple-fm", "latency_ms": 4757, "draft": {
        "intents": [], "exclusions": [], "confidence": 0.9, "reply": "",
        "needs_input": [{"key": "voice", "question": "What voice?", "options": ["voice_1"]}]}}
    res = _fm_brain([body]).plan(BrainRequest(prompt="let the video begin softly",
                                              facts=TimelineFacts.minimal(), recipes=recipes.cards()),
                                 timeout_s=8)
    assert not res.ok and res.reason == "rejected:no edit in the draft"


def test_fm_fade_edge_and_invented_numbers_are_grounded():
    # Recorded live: "let the ending melt to black" → fade with duration_s 85
    # (the timeline length) and no edge; "ease the picture in from black" → no edge.
    plan = _fm_plan("let the ending melt to black", {
        "intents": [{"recipe": "fade", "duration_s": 85.0}], "exclusions": [], "needs_input": [],
        "confidence": 0.9, "reply": ""})
    fades = [s for s in plan.steps if s.tool == "set_video_fade"]
    assert [s.args["clip_id"] for s in fades] == ["$v1_last"]
    assert all(s.args.get("out_s", 1.0) <= 1.0 for s in fades), [s.args for s in fades]
    plan = _fm_plan("ease the picture in from black", {
        "intents": [{"recipe": "fade"}], "exclusions": [], "needs_input": [], "confidence": 0.9, "reply": ""})
    assert [s.args["clip_id"] for s in plan.steps if s.tool == "set_video_fade"] == ["$v1_first"]
    # A number the user DID say is kept.
    plan = _fm_plan("let the ending melt to black over 3 seconds", {
        "intents": [{"recipe": "fade", "duration_s": 3.0}], "exclusions": [], "needs_input": [],
        "confidence": 0.9, "reply": ""})
    assert [s.args.get("out_s") for s in plan.steps if s.tool == "set_video_fade"] == [3.0]


def test_a_reel_trim_is_not_reported_as_lost_speech(tmp_path):
    from video_ai_editor.agent.prompt import planner, verify
    from video_ai_editor.agent.prompt import slots as S
    from video_ai_editor.agent.prompt.recipes import Intent, pc
    s = _store(tmp_path, (45.0,))
    _write_transcript(tmp_path, _REEL_SENTENCES, 45.0)
    f = _facts(s)
    it = Intent("trim", {"range": S.TimeRange(kind="abs", start=30.0, end=None),
                         "_optional": True, "_max_s": 30.0})
    plan = planner.compose([it], f)
    res, _ = _run(s, plan, f)
    ctx = verify.VerifyCtx(store=s, plan=plan, exec_result=res, facts_before=f)
    r = verify.c_speech_preserved(ctx, pc("speech_preserved", "no kept word was cut"))
    assert r.passed is True, r
    assert "trimmed as asked" in (r.detail or "")


def test_a_fit_trim_gets_no_unmeasurable_cut_range_check():
    from video_ai_editor.agent.prompt.schema import bind_postconditions
    assert bind_postconditions("cut_range", {"track": "v1", "start": "$fit_to:30", "end": 90.0}) == []
    assert [p.check for p in bind_postconditions("cut_range", {"track": "v1", "start": 3.0, "end": 5.0})] \
        == ["duration_between"]


def test_export_after_deleting_the_last_clip_under_a_title_has_no_black_tail(tmp_path):
    """QA-021-TAIL, measured on a real export: the file ends with the picture."""
    import subprocess
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from overlay_render_helpers import run_ffmpeg
    from video_ai_editor.render.compositor import render_export

    def gray_clip(path, w, h, dur, fps, color):
        run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={dur}:r={fps}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={dur}",
                    "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", str(path)])
        return path
    s = EDLStore(tmp_path / "sess")
    v1 = s.edl.get_track("v1")
    s.edl.canvas.w, s.edl.canvas.h = 320, 180
    for i, color in enumerate(("red", "blue", "green")):
        src = gray_clip(tmp_path / f"{color}.mp4", 320, 180, 1.0, 30, color)
        v1.clips.append(Clip(id=f"c_{i}", src=str(src), in_=0.0, out=1.0, start=float(i)))
    s.commit("seed", {}, "seed")
    dispatch(s, "add_text", {"text": "END TITLE", "start": 2.2, "end": 2.8})
    dispatch(s, "ripple_delete", {"clip_id": "c_2"})
    out = render_export(s.edl, s.dir).path
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True,
                               check=True).stdout)
    assert dur == pytest.approx(2.0, abs=0.05), dur        # was 2.1: a 0.1 s stub over black
