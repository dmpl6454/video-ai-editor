"""QA-018: the Prompt Editor's everyday one-liners — fades, music level, mute,
fitting the bed to the video — route to the right tools with the right args,
the result is VERIFIED on the EDL and MEASURED on a real render; nonsense
gets a "did you mean" list; an on-device brain may not answer "fade in the
first clip" with a whole-timeline re-cut; and a ripple edit no longer leaves
the music bed playing over black.

Before the fix: 'fade in the first clip' → Apple Intelligence make_shorts;
'add a fade in at the start' → "I did not catch that"; every music level /
fade / mute / fit prompt → intent=music, steps=[], "music is already on the
timeline"; after remove_silences the video was 71.67 s and the music 85.03 s.
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.prompt import grammar as G  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import schema as Sc  # noqa: E402
from video_ai_editor.agent.prompt import service  # noqa: E402
from video_ai_editor.agent.prompt import validate as V  # noqa: E402
from video_ai_editor.agent.prompt.brains import router  # noqa: E402
from video_ai_editor.agent.prompt.brains.base import BrainRequest, BrainResult, available  # noqa: E402
from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402
from video_ai_editor.agent.prompt.recipes import cards  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

#: A 30 s 16:9 timeline with two clips, a transcript and a ducked bed at -14 dB.
FM = TimelineFacts.minimal(
    session_id="s_bed", v1_clip_ids=["c_a", "c_b"], clip_ids=["c_a", "c_b", "c_bed"],
    track_ids=["v1", "music"], v1_boundaries=[15.0], has_transcript=True, language="en", words=40,
    has_music=True, music_ducked=True, music_clip_ids=["c_bed"], music_gain_db=-14.0)


def _tools(p: Sc.Plan) -> list[str]:
    return [s.tool for s in p.steps]


def _args(p: Sc.Plan, tool: str) -> list[dict]:
    return [s.args for s in p.steps if s.tool == tool]


# --------------------------------------------------------------------------- grammar + planner

ONE_LINERS = [
    ("fade in the first clip", "fade",
     [("set_video_fade", {"clip_id": "$v1_first", "in_s": 1.0}), ("add_fade", {"clip_id": "$v1_first", "in_s": 1.0})]),
    ("add a fade in at the start", "fade",
     [("set_video_fade", {"clip_id": "$v1_first", "in_s": 1.0}), ("add_fade", {"clip_id": "$v1_first", "in_s": 1.0})]),
    ("fade out the last clip over 2 seconds", "fade",
     [("set_video_fade", {"clip_id": "$v1_last", "out_s": 2.0}), ("add_fade", {"clip_id": "$v1_last", "out_s": 2.0})]),
    ("turn the music down", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("turn the music up", "volume", [("set_volume", {"target": "music", "db": -8.0})]),
    ("lower the music to -20 dB", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("set the music to -25 dB", "volume", [("set_volume", {"target": "music", "db": -25.0})]),
    ("the music is too loud", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("fade out the music over the last 3 seconds", "fade", [("fit_music_to_video", {"fade_out": 3.0})]),
    ("mute the music", "mute", [("set_track_muted", {"track": "music", "muted": True})]),
    ("mute the original audio", "mute", [("set_clip_muted", {"clip_id": "$v1_all", "muted": True})]),
    ("trim the music to the video length", "fit_music", [("fit_music_to_video", {})]),
    # second sweep: the same misroutes in other words
    ("music at -18db", "volume", [("set_volume", {"target": "music", "db": -18.0})]),
    ("set music volume to 50%", "volume", [("set_volume", {"target": "music", "db": -6.0})]),
    ("make my voice louder", "volume", [("set_volume", {"target": "v1", "db": 6.0})]),
    ("turn up the voice", "volume", [("set_volume", {"target": "v1", "db": 6.0})]),
    ("music kam karo", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("music band karo", "mute", [("set_track_muted", {"track": "music", "muted": True})]),
    ("the music keeps playing after the video ends", "fit_music", [("fit_music_to_video", {})]),
    ("remove the music", "remove_music", [("bulk_delete", {"clip_ids": ["c_bed"]})]),
    ("music hatao", "remove_music", [("bulk_delete", {"clip_ids": ["c_bed"]})]),
    ("the music is way too loud", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("i can't hear my voice over the music", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("quieter music please", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("volume down on the music", "volume", [("set_volume", {"target": "music", "db": -20.0})]),
    ("mute the video", "mute", [("set_clip_muted", {"clip_id": "$v1_all", "muted": True})]),
    ("end the music when the video ends", "fit_music", [("fit_music_to_video", {})]),
    ("music should end with the video", "fit_music", [("fit_music_to_video", {})]),
    ("fade to black at the end", "fade",
     [("set_video_fade", {"clip_id": "$v1_last", "out_s": 1.0}), ("add_fade", {"clip_id": "$v1_last", "out_s": 1.0})]),
]


@pytest.mark.parametrize("prompt,intent,expected", ONE_LINERS, ids=[p for p, _, _ in ONE_LINERS])
def test_one_liners_route_to_the_right_tool_with_the_right_args(prompt, intent, expected):
    det = G.detect(prompt)
    assert det.intents == [intent] and det.confidence >= G.RUN_THRESHOLD, (det.intents, det.confidence)
    p = P.plan(prompt, FM)
    assert p.intent == intent and not p.blocking_questions, (p.intent, p.reply)
    for tool, args in expected:
        got = _args(p, tool)
        assert got, (tool, _tools(p))
        assert any(all(a.get(k) == v for k, v in args.items()) for a in got), (tool, got)
    assert "already on the timeline" not in (p.reply or "")
    out = V.validate_plan(p, FM)          # the security boundary accepts every one
    assert _tools(out) and all(pc.check in Sc.CHECK_SPECS for pc in out.postconditions)


def test_music_level_is_relative_to_the_current_bed_and_bounded():
    p = P.plan("turn the music down", FM.with_(music_gain_db=-30.0))
    assert _args(p, "set_volume") == [{"target": "music", "db": -36.0}]
    p = P.plan("turn the music down by 3 dB", FM)
    assert _args(p, "set_volume") == [{"target": "music", "db": -17.0}]
    p = P.plan("turn the music down", FM.with_(music_gain_db=-38.0))
    assert _args(p, "set_volume") == [{"target": "music", "db": -40.0}]


def test_two_fade_clauses_add_up_instead_of_the_last_one_winning():
    p = P.plan("add a fade in and a fade out", FM)
    assert {"in_s": 1.0} in [{k: v for k, v in a.items() if k != "clip_id"} for a in _args(p, "set_video_fade")]
    assert [a["clip_id"] for a in _args(p, "set_video_fade")] == ["$v1_first", "$v1_last"]
    p = P.plan("fade in the video and fade out the music", FM)
    assert _args(p, "set_video_fade") == [{"clip_id": "$v1_first", "in_s": 1.0}]
    assert _args(p, "fit_music_to_video") == [{"fade_out": 2.0}]


def test_the_fade_edge_is_the_direction_word_after_fade_not_any_in():
    p = P.plan("fade out the music in the last 2 seconds", FM)
    assert _args(p, "fit_music_to_video") == [{"fade_out": 2.0}]           # was fade_in 2 AND fade_out 2
    assert _args(P.plan("fade the music in", FM), "fit_music_to_video") == [{"fade_in": 1.0}]
    assert _args(P.plan("fade the music out in the final 3 seconds", FM), "fit_music_to_video") == [{"fade_out": 3.0}]
    assert _args(P.plan("fade up from black", FM), "set_video_fade") == [{"clip_id": "$v1_first", "in_s": 1.0}]


def test_fade_to_black_at_the_end_fades_a_one_clip_video_out():
    one = TimelineFacts.minimal(v1_clip_ids=["c_a"], clip_ids=["c_a"], track_ids=["v1"], duration=30.0)
    p = P.plan("fade to black at the end", one)
    assert p.steps and _args(p, "set_video_fade") == [{"clip_id": "$v1_last", "out_s": 1.0}], p.reply
    assert G.detect("add a fade to black transition at the last cut").intents == ["transitions"]


def test_volume_without_an_object_moves_the_loudness_target_the_way_asked():
    f = FM.with_(loudness_lufs=-16.0)
    assert _args(P.plan("turn the volume down", f), "set_loudness_target") == [{"lufs": -19.0}]
    assert _args(P.plan("turn the volume up", f), "set_loudness_target") == [{"lufs": -13.0}]
    assert _args(P.plan("normalize the audio", f), "set_loudness_target") == [{"lufs": -16.0}]


def test_remove_the_music_and_add_another_is_a_replace():
    f = FM.with_(uploads_audio=["/u/bed.wav"], allowed_paths={"/u/bed.wav"})
    p = P.plan("remove the music and add chill music", f)
    tools = _tools(p)
    assert tools.index("bulk_delete") < tools.index("add_music") and "already on the timeline" not in (p.reply or "")
    assert not any(c.check == "music_present" and c.args.get("count") == 0 for c in p.postconditions)


def test_turn_the_music_back_on_unmutes_it():
    p = P.plan("turn the music back on", FM.with_(music_muted=True))
    assert _args(p, "set_track_muted") == [{"track": "music", "muted": False}]
    assert _args(P.plan("music chalu karo", FM.with_(music_muted=True)), "set_track_muted") == [
        {"track": "music", "muted": False}]


def test_the_word_set_no_longer_asks_for_a_second_bed():
    det = G.detect("set the music to -20 dB")
    assert det.intents == ["volume"]
    assert "add_music" not in _tools(P.plan("set the music to -20 dB", FM))


def test_existing_readings_are_unchanged():
    assert G.detect("fade to black between the clips").intents == ["transitions"]
    assert G.detect("add chill background music and duck it under my voice").intents == ["music", "duck"]
    assert G.detect("quieter music when I talk").intents == ["duck"]
    assert G.detect("lower the music under my voice").intents == ["duck"]
    assert G.detect("add a crossfade between the clips").intents == ["transitions"]


def test_nonsense_gets_a_did_you_mean_list():
    p = P.plan("banana wobble zebra", FM)
    assert p.intent == "clarify" and not p.steps
    q = p.blocking_questions[0]
    assert q.key == "intent" and len(q.options or []) >= 2


def test_no_music_is_said_honestly():
    bare = TimelineFacts.minimal()
    for prompt in ("turn the music down", "mute the music", "trim the music to the video length",
                   "fade out the music"):
        p = P.plan(prompt, bare)
        assert not p.steps and "no music" in (p.reply or ""), (prompt, p.reply)


# --------------------------------------------------------------------------- the ladder

class _Fake:
    def __init__(self, bid, plan=None):
        self.id = bid
        self._plan = plan
        self.plans = 0

    def availability(self):
        return available("ready", model="m")

    def plan(self, req, *, timeout_s):
        self.plans += 1
        if self._plan is None:
            return BrainResult.failure(self.id, "decode", model="m")
        p = self._plan(req) if callable(self._plan) else self._plan
        return BrainResult(plan=p, brain=self.id, ok=True, model="m", latency_ms=3)

    def text(self, task, *, timeout_s):
        return None


def _shorts_plan(_req=None) -> Sc.Plan:
    step = Sc.Step(tool="make_shorts", args={"target_count": 3, "max_dur": 60.0, "min_dur": 12.0,
                                             "save_as_sessions": True}, why="shorts")
    return Sc.Plan.new(intent="shorts", brain="apple_intelligence", steps=[step], confidence=0.8,
                       postconditions=Sc.bind_postconditions("make_shorts", step.args))


def _route(prompt: str, fm_plan) -> router.RoutedPlan:
    from video_ai_editor.agent.prompt.brains.recipes_brain import RecipesBrain
    brains = {"recipes": RecipesBrain(), "apple_intelligence": _Fake("apple_intelligence", fm_plan)}
    req = BrainRequest(prompt=prompt, facts=FM, recipes=cards())
    return router.plan(req, order=("recipes", "apple_intelligence"), brains=brains,
                       validate=lambda p, f: p, prefetch=False)


def test_an_on_device_recut_nobody_asked_for_falls_through_to_did_you_mean():
    # A prompt with editing words but none that name a re-cut. (A prompt with
    # no editing words at all never reaches the model: test_c6_prompt_paraphrase.)
    routed = _route("polish the vibe", _shorts_plan)
    assert routed.plan is None and routed.clarify is not None and routed.clarify.key == "intent"
    fm = [a for a in routed.attempts if a.brain == "apple_intelligence"]
    assert fm and fm[-1].status == "failed" and "ungrounded" in fm[-1].reason and "make_shorts" in fm[-1].reason


def test_fade_in_the_first_clip_never_reaches_the_model():
    fm_plan = _Fake("apple_intelligence", _shorts_plan)
    from video_ai_editor.agent.prompt.brains.recipes_brain import RecipesBrain
    req = BrainRequest(prompt="fade in the first clip", facts=FM, recipes=cards())
    routed = router.plan(req, order=("recipes", "apple_intelligence"),
                         brains={"recipes": RecipesBrain(), "apple_intelligence": fm_plan},
                         validate=lambda p, f: p, prefetch=False)
    assert routed.brain == "recipes" and fm_plan.plans == 0
    assert "make_shorts" not in _tools(routed.plan) and "set_video_fade" in _tools(routed.plan)


def test_a_grounded_restructure_is_still_the_models_to_make():
    assert router.ungrounded_restructure(_shorts_plan(), "give me the viral moments as clips") == []
    assert router.ungrounded_restructure(_shorts_plan(), "fade in the first clip") == ["make_shorts"]


# --------------------------------------------------------------------------- executed: dispatch

def _probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _rms_db(path: Path, start: float, dur: float) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}", "-t", f"{dur}", "-i", str(path),
                          "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    vals = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", err)
    assert vals, err[-800:]
    v = vals[-1]                       # the "Overall" block comes last
    return -120.0 if v == "-inf" else float(v)


def _luma(path: Path, t: float) -> float:
    # The centre of the frame: the canvas may letterbox the clip (black bars).
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", "crop=iw/4:ih/8", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    assert raw
    return sum(raw) / len(raw)


def _render(store) -> Path:
    from video_ai_editor.render.compositor import render_preview
    # Raw mix levels are measured here; with a target the preview is matched
    # to the export's loudness (QA-082), which moves every absolute level.
    edl = store.edl.model_copy(deep=True)
    edl.canvas.loudness_lufs = None
    res = render_preview(edl, Path(store.dir), height=180)
    return Path(res.path)


def _bright_clip(root: Path) -> Path:
    """`prompt_fixtures.speech_clip` in WHITE, so a fade from black is visible."""
    d = root / "uploads" / "talk"
    d.mkdir(parents=True, exist_ok=True)
    src = d / "talk.normalized.mp4"
    gate = "+".join(f"between(t\\,{a}\\,{b})" for a, b in F.TONE_WINDOWS)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c=white:s=320x180:d={F.CLIP_DUR}:r=30",
                    "-f", "lavfi", "-i", f"aevalsrc='0.6*sin(440*2*PI*t)*({gate})':s=48000:d={F.CLIP_DUR}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True, capture_output=True)
    return src


@pytest.fixture
def bed_session(tmp_path: Path, desktop_posture, monkeypatch):
    """12 s of white picture with tone in 0-3 / 5-8 / 10-12 s, and a 200 Hz bed
    laid UNDUCKED at -14 dB, 16 s long (so it overhangs the video)."""
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)        # chat.json lands in the test's own dir
    # The run commits to THIS store object (an app resolver configured by an
    # earlier test would hand back a second EDLStore on the same directory).
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    store = F.make_store(tmp_path, src=_bright_clip(tmp_path))
    bed = F.music_bed(tmp_path, dur=16.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "out": 16.0, "volume_db": -14.0, "duck": False})
    return store


def test_a_ripple_cut_refits_the_music_bed_and_the_render_has_no_music_tail(tmp_path, desktop_posture):
    store = F.make_store(tmp_path)
    bed = F.music_bed(tmp_path, dur=4.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14.0, "duck": False, "loop": True})
    assert store.edl.video_extent() == pytest.approx(12.0, abs=0.05)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 5.0})
    extent = store.edl.video_extent()
    music = [c for c in store.edl.get_track("music").clips]
    music_end = max(c.start + c.effective_duration for c in music)
    assert extent == pytest.approx(10.0, abs=0.05)
    assert music_end == pytest.approx(extent, abs=0.02), (music_end, extent)    # was 12.0: 2 s over black
    assert max(music, key=lambda c: c.start).audio.fade_out >= 1.0 - 1e-6
    out = _render(store)
    assert _probe_duration(out) == pytest.approx(extent, abs=0.15)


def test_fit_music_to_video_trims_the_bed_and_fades_it_out(bed_session):
    store = bed_session
    before = max(c.start + c.effective_duration for c in store.edl.get_track("music").clips)
    assert before == pytest.approx(16.0, abs=0.05)
    res = D.dispatch(store, "fit_music_to_video", {"fade_out": 4.0})
    clips = store.edl.get_track("music").clips
    assert res["music_end"] == pytest.approx(12.0, abs=0.02) and clips[-1].audio.fade_out == 4.0
    out = _render(store)
    assert _probe_duration(out) == pytest.approx(12.0, abs=0.15)
    # 3.3–4.7 s and 8.2–9.8 s are speech gaps (bed only); the 4 s fade-out
    # runs 8–12 s, so the second gap is measurably quieter than the first.
    body, tail = _rms_db(out, 3.3, 1.4), _rms_db(out, 9.2, 0.6)
    assert body > -40.0 and tail < body - 3.0, (body, tail)


def test_a_locked_music_lane_is_not_refit(bed_session):
    store = bed_session
    store.edl.get_track("music").locked = True
    with pytest.raises(Exception):
        D.dispatch(store, "fit_music_to_video", {})


# --------------------------------------------------------------------------- end to end through the service

def _turn(store, message: str) -> list[dict]:
    events = F.collect(service.prompt_turn(store, message, [], brain="recipes"))
    assert events[-1]["type"] == "done", events[-3:]
    errors = [e for e in events if e["type"] == "error"]
    assert not errors, errors
    return events


def _verify(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "verify")


@pytest.mark.usefixtures("no_downloads")
def test_one_liners_end_to_end_through_the_real_service_and_a_real_render(bed_session):
    """Real grammar → real planner → real validate_plan → executor → dispatch →
    verifier, then a real render measured with ffmpeg."""
    store = bed_session
    out0 = _render(store)
    gap0 = _rms_db(out0, 3.3, 1.4)            # bed only (the speech tone is off 3–5 s)

    ev = _turn(store, "turn the music down")
    v = _verify(ev)
    assert v["passed"] == v["total"] >= 1, v
    assert {c.audio.gain_db for c in store.edl.get_track("music").clips} == {-20.0}
    gap1 = _rms_db(_render(store), 3.3, 1.4)
    assert gap1 - gap0 == pytest.approx(-6.0, abs=1.0), (gap0, gap1)

    ev = _turn(store, "fade in the first clip")
    v = _verify(ev)
    assert v["passed"] == v["total"] >= 2, v
    first = min(store.edl.get_track("v1").clips, key=lambda c: c.start)
    assert first.video_fade_in == 1.0 and first.audio.fade_in == 1.0
    out = _render(store)
    assert _luma(out, 0.0) < 40 and _luma(out, 2.0) > 200     # starts from black, is white by 2 s

    ev = _turn(store, "trim the music to the video length")
    assert _verify(ev)["passed"] == _verify(ev)["total"] >= 1
    assert _probe_duration(_render(store)) == pytest.approx(store.edl.video_extent(), abs=0.15)

    ev = _turn(store, "music at -18 dB")
    assert _verify(ev)["passed"] == _verify(ev)["total"] >= 1
    assert {c.audio.gain_db for c in store.edl.get_track("music").clips} == {-18.0}

    ev = _turn(store, "mute the music")
    v = _verify(ev)
    assert v["passed"] == v["total"] >= 1 and store.edl.get_track("music").muted is True
    assert _rms_db(_render(store), 3.3, 1.4) < -60.0          # the bed is gone from the gap


@pytest.mark.usefixtures("no_downloads")
def test_fade_in_and_out_and_remove_the_music_end_to_end(bed_session):
    """Two fade clauses → both ends dark on a real render; 'fade to black at
    the end' on a ONE-clip video fades it out (it used to do nothing: "no
    seam"); 'remove the music' empties the lane and the bed is gone from the
    render."""
    store = bed_session
    ev = _turn(store, "add a fade in and a fade out")
    v = _verify(ev)
    assert v["passed"] == v["total"] >= 4, v
    out = _render(store)
    end = store.edl.video_extent()
    assert _luma(out, 0.0) < 40 and _luma(out, 6.0) > 200 and _luma(out, end - 0.08) < 60

    D.dispatch(store, "set_video_fade", {"clip_id": store.edl.get_track("v1").clips[0].id, "in_s": 0.0, "out_s": 0.0})
    ev = _turn(store, "fade to black at the end")
    assert _verify(ev)["passed"] == _verify(ev)["total"] >= 1
    out = _render(store)
    assert _luma(out, 6.0) > 200 and _luma(out, end - 0.08) < 60

    gap0 = _rms_db(out, 3.3, 1.4)
    ev = _turn(store, "remove the music")
    v = _verify(ev)
    assert v["passed"] == v["total"] >= 1, v
    assert not [c for c in store.edl.get_track("music").clips]
    assert _rms_db(_render(store), 3.3, 1.4) < gap0 - 30.0


def test_a_cut_inside_a_faded_clip_keeps_the_sound_fade_on_its_outer_edges(tmp_path, desktop_posture):
    """'fade in the first clip' then a cut (remove_silences / cut_range /
    split): the picture fades were partitioned across the seam but the SOUND
    fades were copied to every piece, so speech faded in again after every
    removed silence (live: 7 cuts, 8 pieces each with a 1 s audio fade-in)."""
    store = F.make_store(tmp_path)
    cid = store.edl.get_track("v1").clips[0].id
    D.dispatch(store, "add_fade", {"clip_id": cid, "in_s": 1.0, "out_s": 1.0})
    D.dispatch(store, "cut_range", {"track": "v1", "start": 3.0, "end": 5.0})
    D.dispatch(store, "split_at", {"track": "v1", "time": 6.5})
    pieces = sorted(store.edl.get_track("v1").clips, key=lambda c: c.start)
    assert len(pieces) == 3
    assert [(c.audio.fade_in, c.audio.fade_out) for c in pieces] == [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0)]
    # Measured: the tone that resumes right after the cut (timeline 3.0 =
    # source 5.0) plays at full level, like the steady tone later on.
    out = _render(store)
    head, steady = _rms_db(out, 3.02, 0.3), _rms_db(out, 4.5, 0.3)
    assert head > steady - 1.5, (head, steady)
