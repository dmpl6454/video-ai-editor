"""The verifier (agent/prompt/verify.py, spec §4.4) measures the EDL, the
transcript (through timemap), ffprobe and — for two checks — a 360p render.
Never assumes: every check here is driven to pass AND fail on a constructed
timeline, and the two-clock rule is proven with a cut.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture  # noqa: E402,F401

from video_ai_editor.agent.prompt import verify as V  # noqa: E402
from video_ai_editor.agent.prompt.schema import CHECK_SPECS, Postcondition  # noqa: E402
from video_ai_editor.agent.timemap import source_to_timeline  # noqa: E402
from video_ai_editor.edl.schema import Clip, Keyframe, TextClip, Track, Transform  # noqa: E402
from video_ai_editor.render import verify_render as R  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture")


def _pc(check: str, **args) -> Postcondition:
    return Postcondition(check=check, args=args, human=f"test {check}",
                         needs_render=CHECK_SPECS[check].needs_render, headline=CHECK_SPECS[check].headline)


def _ctx(store, *, plan=None, before=None, results=None, new_sessions=(), render_path=None, facts=None):
    exec_result = SimpleNamespace(
        edl_before=before if before is not None else store.edl.model_copy(deep=True),
        duration_before=(before.duration if before is not None else store.edl.duration),
        steps=[], new_sessions=list(new_sessions),
        results_for=lambda tool: (results or {}).get(tool, []),
        outcomes_for=lambda tool: [],
    )
    return V.VerifyCtx(store=store, plan=plan or F.plan_of(), exec_result=exec_result,
                       facts_before=facts or F.facts_for(store), render_path=render_path)


def _check(ctx, check, **args):
    return V.run_check(ctx, _pc(check, **args))


def _captions(store, cues):
    t = store.edl.get_track("captions")
    if t is None:
        t = Track(id="captions", type="captions")
        store.edl.tracks.append(t)
    t.config.enabled = True
    for s, e, text in cues:
        t.clips.append(TextClip(text=text, start=s, end=e, role="caption"))
    store.edl.recompute_duration()
    return t


@pytest.fixture
def store(tmp_path: Path):
    return F.make_store(tmp_path)


# ---------------------------------------------------------------- coverage

def test_every_check_spec_has_an_implementation():
    missing = set(CHECK_SPECS) - set(V.CHECKS)
    assert not missing, f"CHECK_SPECS names checks verify.py lacks: {sorted(missing)}"
    assert "tool_ok" in V.CHECKS


def test_unknown_or_crashing_checks_are_reported_never_fatal(store):
    ctx = _ctx(store)
    r = V.run_check(ctx, Postcondition(check="no_such_check", args={}, human="?"))
    assert r.passed is None and r.detail == "unknown check"
    r = _check(ctx, "duration_between")                      # no target/range/factor → unmeasured
    assert r.passed is None and "no target" in r.detail
    d = r.as_dict()
    assert {"check", "human", "pass", "measured", "expected", "headline", "detail"} <= set(d)


# ---------------------------------------------------------------- transcript + captions (two clocks)

def test_captions_cover_uses_timeline_seconds_after_a_cut(store):
    """Cut [5,10) on v1: the word at source 10.5 s ('um') is at timeline
    5.5 s. Cues laid at SOURCE time would miss; cues laid at timeline time
    cover. The check must use the mapped spans."""
    before = store.edl.model_copy(deep=True)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.0, "end": 10.0})
    assert store.edl.duration == pytest.approx(7.0)
    assert source_to_timeline(store.edl, "v1", 12.0) == pytest.approx(7.0)
    ctx = _ctx(store, before=before)
    spans = ctx.speech_spans(store.edl)
    assert spans and all(e <= 7.0 + 1e-6 for _, e in spans)
    # source words: [0.2..2.8] survive at the same place; [5.5..7.6] were cut;
    # [10.5..11.6] now sit at [5.5..6.6].
    _captions(store, [(0.2, 2.8, "so um hello there friends"), (5.5, 6.6, "um goodbye")])
    r = _check(ctx, "captions_cover", min_ratio=0.9)
    assert r.passed is True and r.measured >= 0.9
    store.edl.get_track("captions").clips.clear()
    _captions(store, [(10.5, 11.6, "um goodbye")])           # SOURCE-timed cue: past the extent, covers nothing
    r = _check(ctx, "captions_cover", min_ratio=0.9)
    assert r.passed is False and r.measured < 0.5
    assert _check(ctx, "captions_within_extent").passed is False
    assert _check(ctx, "captions_nonempty").passed is True


def test_captions_checks_on_an_empty_and_a_good_track(store):
    ctx = _ctx(store)
    assert _check(ctx, "captions_nonempty").passed is False
    assert _check(ctx, "captions_within_extent").passed is None
    assert _check(ctx, "transcript_present").passed is True and _check(ctx, "transcript_present").measured == len(F.WORDS)
    t = _captions(store, [(0.2, 2.8, "so um hello there friends"), (5.5, 7.6, "uh today we start"),
                          (10.5, 11.6, "um goodbye")])
    t.config.style = "ig_chunky"
    assert _check(ctx, "captions_cover", min_ratio=0.9).passed is True
    assert _check(ctx, "captions_within_extent").passed is True
    assert _check(ctx, "captions_style", style="ig_chunky").passed is True
    assert _check(ctx, "captions_style", style="word_emphasis").passed is False
    assert _check(ctx, "captions_language", target="en").passed is True
    assert _check(ctx, "captions_language", target="hi").passed is False
    r = _check(ctx, "captions_language", target="hinglish")
    assert r.passed is None                                 # source is English: romanisation not judgeable
    t.clips.clear()
    t.clips.append(TextClip(text="नमस्ते दोस्तों", start=0.2, end=2.8, role="caption"))
    assert _check(ctx, "captions_language", target="hi").passed is True


def test_captions_sync_after_a_seam(store):
    before = store.edl.model_copy(deep=True)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.0, "end": 10.0})
    ctx = _ctx(store, before=before)
    _captions(store, [(0.2, 2.8, "hello"), (5.5, 6.6, "um goodbye")])   # first cue after the 5.0 seam: 5.5 = word 'um'
    assert _check(ctx, "captions_sync", tol=0.1).passed is True
    store.edl.get_track("captions").clips[1].start = 5.9
    assert _check(ctx, "captions_sync", tol=0.1).passed is False
    fresh = _ctx(F.make_store(store.dir.parent, src=Path(store.edl.get_track("v1").clips[0].src), name="one"))
    assert _check(fresh, "captions_sync").passed is None    # no seam


def test_captions_sync_measures_drift_not_whispers_cue_padding(store):
    """A whisper cue may START before its first word (the segment opens in
    the preceding pause: 1.6 s early on the benchmark fixture). That offset
    is the same before and after an edit and no editor can change it, so
    the check must report how far the cue moved RELATIVE to its own word,
    not the raw offset. Here the cue for 'um goodbye' leads 'um' by 0.4 s
    (> tol) on both sides of a cut."""
    _captions(store, [(0.2, 2.8, "so um hello there friends"), (10.1, 11.6, "Um, goodbye")])  # 'um' voiced at 10.5
    before = store.edl.model_copy(deep=True)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 5.0, "end": 10.0})
    ctx = _ctx(store, before=before)
    cues = store.edl.get_track("captions").clips
    cues[1].start, cues[1].end = 5.1, 6.6                       # re-laid: 'um' now at 5.5, cue still 0.4 s early
    r = _check(ctx, "captions_sync", tol=0.1)
    assert r.passed is True and r.measured == pytest.approx(0.0, abs=1e-6)
    assert "unchanged" in (r.detail or "")
    cues[1].start, cues[1].end = 5.6, 7.1                       # the run moved the cue 0.5 s off its word
    r = _check(ctx, "captions_sync", tol=0.1)
    assert r.passed is False and r.measured == pytest.approx(0.5, abs=1e-6)
    assert "was -0.40 before the run" in (r.detail or "")
    # No cue before the run → nothing to drift from: the spec's literal claim
    # (cue start within tol of its own first word) is what is measured.
    cues[1].start, cues[1].end = 5.1, 6.6
    fresh = _ctx(store, before=F.make_store(store.dir.parent, src=Path(before.get_track("v1").clips[0].src),
                                            name="fresh").edl)
    r = _check(fresh, "captions_sync", tol=0.1)
    assert r.passed is False and r.measured == pytest.approx(0.4, abs=1e-6)
    assert "no cue before the run" in (r.detail or "")


def test_speech_preserved_and_fillers_remaining(store):
    before = store.edl.model_copy(deep=True)
    D.dispatch(store, "remove_fillers", {"words": ["um", "uh"], "pad": 0.05, "track": "v1"})
    ctx = _ctx(store, before=before)
    assert _check(ctx, "fillers_remaining_leq", words=["um", "uh"], max=0).passed is True
    assert _check(ctx, "speech_preserved").passed is True
    assert _check(ctx, "duration_shrank", min_seconds=0.1).passed is True
    # Now cut a real word ("hello", source 1.0–1.5, which the filler cut has
    # shifted on the timeline) and the preservation check names it.
    t0, t1 = source_to_timeline(store.edl, "v1", 1.0), source_to_timeline(store.edl, "v1", 1.5)
    D.dispatch(store, "cut_range", {"track": "v1", "start": t0 - 0.02, "end": t1 + 0.02})
    r = _check(ctx, "speech_preserved")
    assert r.passed is False and "hello" in r.detail and r.measured == 1
    plain = _ctx(F.make_store(store.dir.parent, src=Path(before.get_track("v1").clips[0].src), name="two"))
    r = _check(plain, "fillers_remaining_leq", words=["um", "uh"], max=0)
    assert r.passed is False and r.measured == 3          # um ×2 + uh ×1 in the fixture transcript


# ---------------------------------------------------------------- durations

def test_duration_checks(store):
    before = store.edl.model_copy(deep=True)
    D.dispatch(store, "cut_range", {"track": "v1", "start": 4.0, "end": 6.0})
    ctx = _ctx(store, before=before)
    assert _check(ctx, "duration_shrank", min_ratio=0.1).passed is True
    assert _check(ctx, "duration_shrank", min_ratio=0.5).passed is False
    assert _check(ctx, "duration_between", start=4.0, end=6.0, tol=0.1).passed is True
    assert _check(ctx, "duration_between", target=10.0, tol=0.1).passed is True
    assert _check(ctx, "duration_between", target=12.0, tol=0.1).passed is False
    assert _check(ctx, "duration_between", factor=1.2, tol_ratio=0.05).passed is True
    assert _check(ctx, "duration_leq", max=10.0).passed is True
    assert _check(ctx, "duration_leq", max=9.0).passed is False


def test_speed_equals_and_clip_src_changed(store):
    cid = store.edl.get_track("v1").clips[0].id
    before = store.edl.model_copy(deep=True)
    D.dispatch(store, "set_speed", {"clip_id": cid, "factor": 1.5})
    ctx = _ctx(store, before=before)
    assert _check(ctx, "speed_equals", clip_id=cid, factor=1.5).passed is True
    assert _check(ctx, "speed_equals", clip_id="$v1_all", factor=2.0).passed is False
    assert _check(ctx, "speed_equals", clip_id="c_missing", factor=1.5).passed is False
    assert _check(ctx, "duration_between", factor=1.5, tol_ratio=0.05).passed is True
    assert _check(ctx, "clip_src_changed", clip_id=cid).passed is False
    store.edl.get_track("v1").clips[0].src = str(Path(store.dir) / "cache" / "cleaned.mp4")
    assert _check(ctx, "clip_src_changed", clip_id="$v1_all").passed is True


# ---------------------------------------------------------------- canvas / reframe / safe zone

def test_canvas_aspect_reframe_effective_and_no_letterbox(store):
    ctx = _ctx(store)
    # The fixture's canvas is the 9:16 default; the clip itself is 16:9.
    assert _check(ctx, "canvas_aspect", ratio="9:16").passed is True
    assert _check(ctx, "canvas_aspect", ratio="16:9").passed is False
    assert _check(ctx, "canvas_aspect").passed is None
    # A 16:9 source on a 9:16 canvas with fit=contain letterboxes …
    r = _check(ctx, "no_letterbox")
    assert r.passed is False and r.measured == 1
    assert _check(ctx, "reframe_effective").passed is False
    # … and fit=cover is the guaranteed fallback (§2.4 reframe).
    D.dispatch(store, "set_clip_fit", {"clip_id": store.edl.get_track("v1").clips[0].id, "fit": "cover"})
    assert _check(ctx, "no_letterbox").passed is True
    assert _check(ctx, "reframe_effective").passed is True
    # A real reframe: src changed and nothing skipped.
    store.edl.get_track("v1").clips[0].fit = "contain"
    ctx2 = _ctx(store, before=ctx.edl_before, results={"auto_reframe": [{"reframed": ["c1 → 540x960"]}]})
    store.edl.get_track("v1").clips[0].src = str(Path(store.dir) / "cache" / "reframed.mp4")
    assert _check(ctx2, "reframe_effective").passed is True
    ctx3 = _ctx(store, before=ctx.edl_before, results={"auto_reframe": [{"reframed": ["c1 (skipped: no cv2)"]}]})
    assert _check(ctx3, "reframe_effective").passed is False
    unknown = _check(ctx, "no_letterbox")                     # src no longer probeable, fit contain
    assert unknown.passed is None
    D.dispatch(store, "set_canvas", {"w": 1920, "h": 1080})
    assert _check(ctx, "canvas_aspect", ratio="16:9").passed is True


def test_overlays_inside_safe_zone_follows_the_renderer(store):
    ctx = _ctx(store)
    assert _check(ctx, "overlays_inside_safe_zone").passed is None        # vacuous: no overlays
    D.dispatch(store, "set_canvas", {"w": 1080, "h": 1920})
    D.dispatch(store, "add_caption_track", {"style": "ig_chunky", "position": "bottom"})
    positions = {role: (x, y) for _, role, x, y in V.overlay_positions(store.edl)}
    assert positions["caption"][1] == pytest.approx(0.76, abs=0.005)      # §4.9 portrait default
    r = _check(ctx, "overlays_inside_safe_zone", ratio="9:16")
    assert r.passed is True
    D.dispatch(store, "add_text", {"text": "too low", "start": 0, "end": 2, "x": 540, "y": 1900})
    r = _check(ctx, "overlays_inside_safe_zone", ratio="9:16")
    assert r.passed is False and r.measured == 1 and "y=0.99" in r.detail
    # A watermark lives in the margin by design and is exempt.
    store.edl.get_track("text").clips.clear()
    store.edl.get_track("text").clips.append(TextClip(text="@me", start=0, end=2, role="watermark",
                                                       transform=Transform(x=1000, y=1880)))
    assert _check(ctx, "overlays_inside_safe_zone", ratio="9:16").passed is True


def test_text_lower_third_brand_and_hook_checks(store):
    ctx = _ctx(store)
    assert _check(ctx, "hook_text_starts_leq", t=0.5).passed is False
    assert _check(ctx, "text_present", contains="Priya").passed is False
    D.dispatch(store, "add_lower_third", {"name": "Priya Sharma", "handle": "@priya.codes", "start": 0.5, "end": 4.0})
    assert _check(ctx, "text_present", contains="priya sharma", role="lower_third").passed is True
    assert _check(ctx, "text_present", contains="Priya", start_geq=1.0).passed is False
    assert _check(ctx, "brand_watermark_present").passed is False
    assert _check(ctx, "brand_kit_set").passed is False
    D.dispatch(store, "apply_brand_kit", {"handle": "@quicksolutions.in"})
    assert _check(ctx, "brand_watermark_present").passed is True
    assert _check(ctx, "brand_kit_set").measured == "@quicksolutions.in"
    D.dispatch(store, "apply_hook_stack", {"text": "WATCH THIS", "duration": 3.0})
    r = _check(ctx, "hook_text_starts_leq", t=0.5)
    assert r.passed is True and r.measured <= 0.5
    axes = _check(ctx, "hook_axes_geq", n=3)
    assert axes.headline is False and axes.passed is True
    audit = _check(ctx, "audit_ok")
    assert audit.measured["hook_score"] == 3 and "score" in audit.measured


# ---------------------------------------------------------------- music / beats

def test_music_checks(store, tmp_path):
    ctx = _ctx(store)
    assert _check(ctx, "music_present").passed is False
    assert _check(ctx, "music_ducked").passed is False
    assert _check(ctx, "music_covers").passed is False
    bed = F.music_bed(tmp_path, dur=4.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "duck": True})
    assert _check(ctx, "music_present", ducked=True).passed is True
    assert _check(ctx, "music_within_video_extent").passed is True
    r = _check(ctx, "music_covers", min_ratio=0.95)
    assert r.passed is False and r.measured == pytest.approx(4 / 12, abs=0.01)   # a 4 s bed under 12 s
    assert _check(ctx, "music_ducked", to_db=-12).passed is True                  # default duck −18 ≤ −12
    D.dispatch(store, "set_duck", {"track": "music", "enabled": True, "to_db": -6})
    assert _check(ctx, "music_ducked", to_db=-12).passed is False
    store.edl.get_track("music").clips.clear()
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "duck": True, "loop": True})
    assert _check(ctx, "music_covers", min_ratio=0.95).passed is True
    assert _check(ctx, "music_within_video_extent").passed is True
    store.edl.get_track("music").duck = None
    assert _check(ctx, "music_present", ducked=True).passed is False


def test_beat_and_shot_checks(store):
    before = store.edl.model_copy(deep=True)
    for t in (3.0, 6.0, 9.0):
        D.dispatch(store, "split_at", {"track": "v1", "time": t})
    ctx = _ctx(store, before=before)
    assert _check(ctx, "beat_splits_geq", n=3).passed is True
    assert _check(ctx, "beat_splits_geq", n=4).passed is False
    assert _check(ctx, "min_shot_geq", seconds=0.8).passed is True
    D.dispatch(store, "split_at", {"track": "v1", "time": 0.3})
    assert _check(ctx, "min_shot_geq", seconds=0.8).passed is False
    assert _check(ctx, "beat_pulse_present", n=2).passed is None            # no music bed
    D.dispatch(store, "add_transition", {"at": 3.0, "type": "crossdissolve"})
    assert _check(ctx, "transitions_count_geq", n=1, type="crossdissolve").passed is True
    assert _check(ctx, "transitions_count_geq", n=2).passed is False


def test_beat_pulse_present_reads_keyframes_against_detected_beats(store, tmp_path, monkeypatch):
    bed = F.music_bed(tmp_path, dur=6.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0})
    from video_ai_editor.ingest import beats as _beats
    monkeypatch.setattr(_beats, "detect_beats", lambda p: [1.0, 2.0, 3.0, 4.0])
    D.dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    D.dispatch(store, "split_at", {"track": "v1", "time": 4.0})
    for c in store.edl.get_track("v1").clips[1:]:
        c.transform.scale = Keyframe(keyframes=[[0.0, 1.0], [0.5, 1.05]])
    ctx = _ctx(store)
    assert _check(ctx, "beat_pulse_present", n=2).passed is True
    assert _check(ctx, "beat_pulse_present", n=3).passed is False


# ---------------------------------------------------------------- effects / presets / loudness / vo

def test_effect_export_preset_loudness_target_and_vo(store, tmp_path):
    ctx = _ctx(store)
    assert _check(ctx, "effect_present", type="lut").passed is False
    D.dispatch(store, "apply_lut", {"clip_id": store.edl.get_track("v1").clips[0].id, "src": "teal_orange.cube"})
    assert _check(ctx, "effect_present", type="lut", all=True).passed is True
    assert _check(ctx, "export_preset_applied", name="tiktok").passed is False
    D.dispatch(store, "apply_export_preset", {"name": "tiktok"})
    r = _check(ctx, "export_preset_applied", name="tiktok")
    assert r.passed is True and r.measured["lufs"] == -16
    assert _check(ctx, "loudness_target_set", lufs=-16).passed is True
    assert _check(ctx, "loudness_target_set", lufs=-14).passed is False
    assert _check(ctx, "export_preset_applied", name="nope").passed is None
    assert _check(ctx, "vo_present").passed is False
    wav = F.music_bed(tmp_path, name="vo.wav", dur=1.0)
    vo = store.edl.get_track("vo") or Track(id="vo", type="vo")
    if store.edl.get_track("vo") is None:
        store.edl.tracks.append(vo)
    vo.clips.append(Clip(src=str(wav), in_=0.0, out=1.0, start=8.0))
    assert _check(ctx, "vo_present").passed is True


def test_tool_ok_reads_step_outcomes(store):
    ctx = _ctx(store)
    ctx.exec_result.outcomes_for = lambda tool: [SimpleNamespace(status="ok"), SimpleNamespace(status="skipped")]
    assert _check(ctx, "tool_ok", tool="x").passed is False
    ctx.exec_result.outcomes_for = lambda tool: [SimpleNamespace(status="ok")]
    assert _check(ctx, "tool_ok", tool="x").passed is True
    ctx.exec_result.outcomes_for = lambda tool: []
    assert _check(ctx, "tool_ok", tool="x").passed is None


def test_shorts_checks_read_the_child_sessions(store):
    root = Path(store.dir).parent
    src = Path(store.edl.get_track("v1").clips[0].src)
    a = F.make_store(root, src=src, name="short_a")
    b = F.make_store(root, src=src, name="short_b")
    children = {"short_a": a, "short_b": b}
    ctx = _ctx(store, new_sessions=["short_a", "short_b"])
    ctx.store_resolver = lambda sid: children[sid]
    r = _check(ctx, "shorts_created", count=2, max_dur=30, min_dur=5)
    assert r.passed is True and r.measured["durations"] == [12.0, 12.0]
    assert _check(ctx, "shorts_created", count=3, max_dur=30, min_dur=5).passed is False
    assert _check(ctx, "shorts_created", count=2, max_dur=10, min_dur=5).passed is False
    r = _check(ctx, "shorts_finished")
    assert r.passed is False and "captions=False" in r.detail
    for child in children.values():
        D.dispatch(child, "set_canvas", {"w": 1080, "h": 1920})
        D.dispatch(child, "add_caption_track", {"style": "ig_chunky"})
        D.dispatch(child, "apply_hook_stack", {"text": "HEY", "duration": 2.0})
    assert _check(ctx, "shorts_finished").passed is True
    none = _ctx(store)
    assert _check(none, "shorts_created", count=1).passed is False


# ---------------------------------------------------------------- render-based checks

def _lavfi_wav(path: Path, *, gain: str, dur: float = 3.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=f=440:duration={dur}", "-af", f"volume={gain}", "-ar", "48000", str(path)],
                   check=True, capture_output=True)
    return path


def test_loudness_and_silence_readers_on_lavfi_audio(tmp_path):
    loud = _lavfi_wav(tmp_path / "loud.wav", gain="0dB")
    quiet = _lavfi_wav(tmp_path / "quiet.wav", gain="-30dB")
    l1, l2 = R.integrated_loudness(loud), R.integrated_loudness(quiet)
    assert l1 is not None and l2 is not None and l1 > l2 + 20
    assert R.total_silence(loud) == 0.0
    gap = tmp_path / "gap.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "aevalsrc='0.5*sin(440*2*PI*t)*between(t\\,0\\,1)':s=48000:d=3", str(gap)],
                   check=True, capture_output=True)
    assert R.total_silence(gap, min_dur=0.5) >= 1.8


def test_render_checks_are_unmeasured_without_a_render_and_skipped_when_too_long(store):
    ctx = _ctx(store)
    ctx.render_skip_reason = "verify render disabled"
    r = _check(ctx, "silence_total_leq", max_total_s=1.0)
    assert r.passed is None and r.detail == "verify render disabled"
    assert _check(ctx, "loudness_within", tol=1.0).passed is None
    assert R.render_for_verify(store.edl, Path(store.dir), max_duration_s=5.0) is None   # 12 s > 5 s cap


def test_verify_plan_renders_once_and_measures_silence_and_loudness(store):
    """One real 360p verify render of the 12 s fixture (tone in three windows,
    so silencedetect finds the two 2 s gaps — both long pauses under the
    0.85 s floor) drives both render checks."""
    D.dispatch(store, "set_loudness_target", {"lufs": -16})
    plan = F.plan_of(F.step("set_loudness_target", lufs=-16),
                     postconditions=[_pc("silence_total_leq", max_total_s=1.0),
                                     _pc("loudness_within", tol=1.0),
                                     _pc("loudness_target_set", lufs=-16)])
    ctx_result = SimpleNamespace(edl_before=store.edl.model_copy(deep=True), duration_before=12.0, steps=[],
                                 new_sessions=[], results_for=lambda t: [], outcomes_for=lambda t: [])
    events: list[dict] = []
    out = V.verify_plan(store, plan, ctx_result, F.facts_for(store), emit=events.append)
    assert out["rendered"] is True and out["plan_id"] == plan.id
    by = {c["check"]: c for c in out["checks"]}
    assert by["silence_total_leq"]["pass"] is False and by["silence_total_leq"]["measured"] >= 3.5
    assert by["loudness_within"]["pass"] is not None and by["loudness_within"]["unit"] == "LUFS"
    assert by["loudness_target_set"]["pass"] is True
    assert out["total"] == 3 and out["passed"] >= 1
    render_steps = [e for e in events if e["type"] == "step" and e["tool"] == "verify_render"]
    assert render_steps[0]["status"] == "running" and render_steps[-1]["status"] == "ok"
    cached = R.render_for_verify(store.edl, Path(store.dir), max_duration_s=600)
    assert cached is not None and cached.exists()          # second call hits the EDL-hash cache


def test_verify_plan_falls_back_to_default_postconditions_for_raw_plans(store):
    plan = F.plan_of(F.step("apply_lut", clip_id="$v1_all", src="warm.cube"))
    D.dispatch(store, "apply_lut", {"src": "warm.cube"})
    res = SimpleNamespace(edl_before=store.edl, duration_before=12.0, steps=[], new_sessions=[],
                          results_for=lambda t: [], outcomes_for=lambda t: [])
    out = V.verify_plan(store, plan, res, F.facts_for(store), emit=lambda e: None, render=False)
    assert [c["check"] for c in out["checks"]] == ["effect_present"] and out["passed"] == 1
    assert out["rendered"] is False


# ---------------------------------------------------------------- findings: honest measurements

def test_speech_preserved_forgives_whispers_pre_onset_padding_but_not_a_cut_word(store, tmp_path):
    """Whisper starts a word after a pause up to ~0.3 s before the voice
    (' The' 15.91–16.37 s, onset 16.27 s). remove_silences trims that air:
    the word's END plays and 44% of its span — a kept word. A cut through
    the tail is still a lost word."""
    from prompt_fixtures import speech_clip, transcript, write_ingest
    src = speech_clip(tmp_path / "onset", name="onset")
    words = [("hello", 0.30, 0.50), ("the", 4.60, 5.30), ("camera", 5.40, 5.90), ("wins", 6.00, 6.40)]
    write_ingest(src, transcript(words))
    st = F.make_store(tmp_path / "onset", src=src, name="s_onset")
    before = st.edl.model_copy(deep=True)
    D.dispatch(st, "cut_range", {"track": "v1", "start": 3.0, "end": 5.0})       # trims 0.4 of 'the' (57%), its end plays
    ctx = _ctx(st, before=before)
    r = _check(ctx, "speech_preserved")
    assert r.passed is True and r.measured == 0, r
    D.dispatch(st, "cut_range", {"track": "v1", "start": 3.7, "end": 3.9})       # 'camera' was 5.4–5.9 → now 3.4–3.9: its tail is cut
    r = _check(ctx, "speech_preserved")
    assert r.passed is False and "camera" in (r.detail or "")


def test_captions_style_is_unmeasured_on_an_empty_track(store):
    ctx = _ctx(store)
    t = _captions(store, [])
    t.config.style = "ig_chunky"
    r = _check(ctx, "captions_style", style="ig_chunky")
    assert r.passed is None and "no captions" in (r.detail or "")
    _captions(store, [(0.2, 1.0, "hi")])
    assert _check(ctx, "captions_style", style="ig_chunky").passed is True


def test_music_present_count_catches_a_bed_laid_on_top_of_another(store, tmp_path):
    bed = F.music_bed(tmp_path)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "duck": True})
    ctx = _ctx(store)
    assert _check(ctx, "music_present", ducked=True, count=1).passed is True
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "duck": True})
    r = _check(ctx, "music_present", ducked=True, count=1)
    assert r.passed is False and r.measured["clips"] == 2 and r.expected["clips"] == "= 1"


def test_silence_is_measured_with_the_music_muted(store, tmp_path, monkeypatch):
    """Under a −14 dB bed silencedetect finds nothing, so the check was
    vacuously true whenever music was on the timeline; it now reads the
    speech-only render and says so."""
    from video_ai_editor.render import verify_render as VR
    bed = F.music_bed(tmp_path, dur=12.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "duck": True})
    calls: list = []

    def fake_render(edl, session_dir, *, max_duration_s, on_progress=None, cancel_event=None, speech_only=False):
        calls.append(speech_only)
        out = Path(session_dir) / "cache" / "verify" / f"{'speech' if speech_only else 'mix'}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return out

    monkeypatch.setattr(VR, "render_for_verify", fake_render)
    monkeypatch.setattr(V, "silence_runs", lambda p, **k: [] if p.name == "mix.mp4" else [(1.0, 5.5)])
    ctx = _ctx(store, render_path=Path(store.dir) / "cache" / "verify" / "mix.mp4")
    r = _check(ctx, "silence_total_leq", max_total_s=1.0)
    assert r.passed is False and r.measured == 4.5 and "muted" in (r.detail or "")
    assert calls == [True]
    stripped = VR.speech_only(store.edl)
    assert not stripped.get_track("music").clips and store.edl.get_track("music").clips   # a copy, never the store


def test_silence_without_a_speech_render_is_unmeasured_not_passed(store, tmp_path, monkeypatch):
    from video_ai_editor.render import verify_render as VR
    bed = F.music_bed(tmp_path)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14})

    def boom(*a, **k):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(VR, "render_for_verify", boom)
    ctx = _ctx(store, render_path=tmp_path / "mix.mp4")
    r = _check(ctx, "silence_total_leq", max_total_s=1.0)
    assert r.passed is None and "ffmpeg died" in (r.detail or "")


# ---------------------------------------------------------------- findings: energy is ground truth, timestamps are estimates

#: A transcript for the 12 s fixture clip (tone 0–3, 5–8, 10–12 s) that
#: puts two words INSIDE the 3–5 s silence — whisper's uniform-spacing
#: fallback did exactly this on the TikTok run (14.29–16.28 s: five
#: back-to-back 0.40 s "words" over a stretch silencedetect measured silent).
_MISALIGNED_WORDS = [("hello", 0.30, 0.50), ("than", 3.40, 3.80), ("it", 3.90, 4.30),
                     ("camera", 5.40, 5.90), ("wins", 6.00, 6.40), ("bye", 10.50, 11.00)]


def test_speech_preserved_does_not_count_transcript_words_inside_measured_silence(tmp_path):
    """remove_silences cut 3.1–4.9 s and with it "than" and "it" — words the
    transcript placed where the audio holds no energy. Nothing voiced was
    lost, so the check passes and says so; a word whose tone really is cut
    ("camera") still fails exactly as before."""
    from prompt_fixtures import speech_clip, transcript, write_ingest
    src = speech_clip(tmp_path / "mis", name="mis")
    write_ingest(src, transcript(_MISALIGNED_WORDS))
    st = F.make_store(tmp_path / "mis", src=src, name="s_mis")
    before = st.edl.model_copy(deep=True)
    plan = F.plan_of(F.step("remove_silences", track="v1", threshold_db=-30.0, min_dur=0.5, keep_pad=0.1))
    D.dispatch(st, "remove_silences", {"track": "v1", "threshold_db": -30.0, "min_dur": 0.5, "keep_pad": 0.1})
    assert st.edl.duration < 9.0                                              # both 2 s gaps went
    ctx = _ctx(st, before=before, plan=plan)
    r = _check(ctx, "speech_preserved")
    assert r.passed is True and r.measured == 0, r
    assert "2 transcript words sat inside measured silence" in (r.detail or ""), r.detail
    assert "than" in r.detail and "it" in r.detail and "not counted" in r.detail
    assert ctx.source_silences() and any(s <= 3.4 and e >= 4.3 for s, e in ctx.source_silences())
    # "camera" (source 5.4–5.9) now plays at timeline 3.6–4.1: cut it and the
    # check fails on that one word while still reporting the two it excused.
    t0, t1 = source_to_timeline(st.edl, "v1", 5.4), source_to_timeline(st.edl, "v1", 5.9)
    D.dispatch(st, "cut_range", {"track": "v1", "start": t0 - 0.02, "end": t1 + 0.02})
    r = _check(ctx, "speech_preserved")
    assert r.passed is False and r.measured == 1 and "camera" in (r.detail or ""), r
    assert "not counted" in r.detail and "than" in r.detail


def test_speech_preserved_uses_the_recipe_defaults_when_no_plan_step_names_the_silence_params(tmp_path):
    from prompt_fixtures import speech_clip, transcript, write_ingest
    src = speech_clip(tmp_path / "dflt", name="dflt")
    write_ingest(src, transcript(_MISALIGNED_WORDS))
    st = F.make_store(tmp_path / "dflt", src=src, name="s_dflt")
    before = st.edl.model_copy(deep=True)
    D.dispatch(st, "remove_silences", {"track": "v1"})
    ctx = _ctx(st, before=before)                                             # plan: no steps at all
    assert V._silence_params(ctx) == (-30.0, 0.5, 0.1)
    r = _check(ctx, "speech_preserved")
    assert r.passed is True and r.measured == 0 and "not counted" in (r.detail or ""), r


def test_word_inside_silence_needs_seventy_percent_of_its_span_in_a_silent_run():
    runs = [(3.0, 5.0)]
    assert V._inside_silence({"start": 3.4, "end": 3.8}, runs) is True
    assert V._inside_silence({"start": 4.7, "end": 5.1}, runs) is True         # 75% inside
    assert V._inside_silence({"start": 4.6, "end": 5.3}, runs) is False        # 57%: a real onset pad, not a misalignment
    assert V._inside_silence({"start": 4.0, "end": 4.0}, runs) is True         # zero-length: the instant is silent
    assert V._inside_silence({"start": 6.0, "end": 6.0}, runs) is False


def test_silence_runs_reads_start_end_pairs_on_lavfi_audio(tmp_path):
    gap = tmp_path / "gap.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "aevalsrc='0.5*sin(440*2*PI*t)*(between(t\\,0\\,1)+between(t\\,3\\,4))':s=48000:d=4",
                    str(gap)], check=True, capture_output=True)
    runs = V.silence_runs(gap, noise_db=-30.0, min_dur=0.5)
    assert len(runs) == 1 and runs[0][0] == pytest.approx(1.0, abs=0.05) and runs[0][1] == pytest.approx(3.0, abs=0.05)
    assert V.silence_runs(gap, noise_db=-30.0, min_dur=2.5) == []


def test_silence_check_counts_only_long_pauses_and_names_the_longest(store):
    """Two real 360p renders of the fixture: uncut, the two 2 s gaps are long
    pauses (≥ 0.85 s = min_dur 0.5 + 2×keep_pad 0.1 + 0.15 tolerance) and
    the check fails naming 2.0 s; after remove_silences only the 2×0.1 s of
    deliberately kept air remains per pause and the check passes. The old
    "≤ 1.0 s of any silence" read the TikTok run's seven kept-air pairs plus
    natural sub-0.5 s pauses as 3.79 s of failure after every dead-air
    stretch was gone."""
    import re
    plan = F.plan_of(F.step("remove_silences", track="v1", threshold_db=-30.0, min_dur=0.5, keep_pad=0.1))
    before = store.edl.model_copy(deep=True)
    ctx = _ctx(store, before=before, plan=plan,
               render_path=R.render_for_verify(store.edl, Path(store.dir), max_duration_s=60.0))
    r = _check(ctx, "silence_total_leq", max_total_s=1.0)
    assert r.passed is False and 3.6 <= r.measured <= 4.4 and r.unit == "s", r
    m = re.search(r"longest remaining pause ([\d.]+) s", r.detail or "")
    assert m and abs(float(m.group(1)) - 2.0) < 0.15, r.detail
    assert "≥ 0.85 s" in r.detail and "2 pause(s)" in r.detail
    D.dispatch(store, "remove_silences", {"track": "v1", "threshold_db": -30.0, "min_dur": 0.5, "keep_pad": 0.1})
    ctx = _ctx(store, before=before, plan=plan,
               render_path=R.render_for_verify(store.edl, Path(store.dir), max_duration_s=60.0))
    r = _check(ctx, "silence_total_leq", max_total_s=1.0)
    assert r.passed is True and r.measured == 0.0 and "0 pause(s) ≥ 0.85 s" in (r.detail or ""), r


def test_long_pause_floor_follows_the_plans_remove_silences_args():
    plan = F.plan_of(F.step("remove_silences", track="v1", threshold_db=-40.0, min_dur=1.0, keep_pad=0.2))
    ctx = SimpleNamespace(plan=plan)
    assert V._silence_params(ctx) == (-40.0, 1.0, 0.2)
    assert V._long_pause_floor(ctx) == pytest.approx(1.0 + 0.4 + 0.15)
