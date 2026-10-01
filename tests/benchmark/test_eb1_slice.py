"""The EB1 slice (EB1_BRIEF "The slice this wave must demonstrate"; lane F owns).

    VAI_BRAIN=recipes uv run pytest -m "benchmark and eb1" tests/benchmark/test_eb1_slice.py

The two demo prompts through the REAL `/prompt` route — preview card, Apply,
one op — on lane A's fixtures, with no cloud key, `HF_HUB_OFFLINE=1`,
librosa/torch/mlx import-blocked, `brain.enabled` on:

  1. talking head → "make a 45-second reel": the card opens on its Plan tab;
     Apply is ONE op with the EDP in its args and one ⌘Z; V1 Reel is listed
     and restorable; measured — the first kept sentence is the planted
     quotable line, 45 ± 1 s, every planted filler gone by TIME (the three
     acoustic "uh"s included), every cut edge on an energy trough, 0 clicks at
     the seams of the render, the punch-in in at the clause start and released
     as a step, captions cover ≥ 0.9, the dialogue lane `a1` in step at
     every seam (EDL and the render's clicks vs flashes);
  2. two-camera podcast (+ recorder) → "tighten this podcast like a premium
     podcast": ONE op, V1 Premium Podcast; ≥ 90 % of talking time on the
     speaker's close, every switch 0-0.15 s BEFORE the truth onset, the
     backchannel turns never switch, every tighten seam ≥ 0.4 s hidden by an
     angle change or a scale step, the bar codes prove the source per span,
     the recorder's clicks meet the picture's flashes within half a frame,
     camera mics muted, no v1 transition;
  3. the rungs: with the FM helper live (`VAI_BRAIN=fm`) the EDP differs
     from the recipes EDP only in `content_brain` and the hook re-rank's
     provenance; with FM stubbed to time out it is byte-equal.

Beyond the demos (EB1 fix wave, lane FX-F): the review's findings as measured
assertions — seams that hide a jump cut change the picture's zoom in the
DECODED EXPORT; captions cover the speech on both cameras and never show a
removed filler; every full turn is on its speaker's close; the recorder meets
the picture at 20/25/29.97/30 fps with no hide dropped; a second run over an
edited timeline commits nothing wrong; a saved project keeps its versions; a
prompt typed before the transcript exists never offers a cut-less edit; a
cancelled read leaves no card; one plan applied twice gets two labels; the
Prompt bar with the flag OFF equals 0.8.0 for the 24 `edit` phrasings.

Owner data is untouched (the real settings.json sha before/after). The
module SKIPS, naming the lane, while a lane's module is not on the tree
(A's fixtures, C's store/versions, D's analyse, E's `edit` card); once the
tree is integrated every assertion below is measured, never skipped.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from . import eb1_slice_lib as L  # noqa: E402
from . import measure as M  # noqa: E402
from .harness import clone_session_dir, open_bench, parse_sse, requirement_missing  # noqa: E402

pytestmark = [pytest.mark.benchmark, pytest.mark.eb1, pytest.mark.slow]

#: (lane, module, attribute) the slice needs on the tree.
_LANES = (("A fixtures", "brain_fixtures", "build_brain_fixtures"),
          ("C store", "video_ai_editor.brain.store", "read_edp"),
          ("C versions", "video_ai_editor.brain.versions", "restore"),
          ("D analyse", "video_ai_editor.brain.graph", "analyse"),
          ("E edit recipe", "video_ai_editor.agent.prompt.brain_expanders", None))
_BLOCKED = ("librosa", "torch", "mlx", "mlx_lm")


def _missing_lane() -> str | None:
    for lane, mod, attr in _LANES:
        try:
            m = importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            return f"{lane} not on this tree ({mod}: {type(e).__name__})"
        if attr and not hasattr(m, attr):
            return f"{lane} lacks {mod}.{attr}"
    return None


def _owner_settings_sha() -> str:
    from video_ai_editor import platformutil
    p = platformutil.user_data_dir("Video AI Editor") / "settings.json"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "absent"


@pytest.fixture(scope="module")
def slice_env(tmp_path_factory):
    why = _missing_lane() or requirement_missing("whisper_small") or L.whisper_cpp_missing()
    if why:
        pytest.skip(why)
    owner_before = _owner_settings_sha()
    mp = pytest.MonkeyPatch()
    mp.setenv("VAI_BRAIN_ENABLED", "1")
    mp.setenv("VAI_PROMPT_CONFIRM", "1")          # the card path: preview, then Apply
    for name in _BLOCKED:
        mp.setitem(sys.modules, name, None)
    try:
        with open_bench(tmp_path_factory.mktemp("eb1_slice"), brain="recipes") as env:
            # the bench pins faster-whisper; an upload on this Mac transcribes with
            # whisper.cpp (ingest/transcribe.py "auto"), so the slice does too
            inner = pytest.MonkeyPatch()
            inner.setenv("WHISPER_BACKEND", "whisper_cpp")
            try:
                yield env
            finally:
                inner.undo()
    finally:
        mp.undo()
    assert _owner_settings_sha() == owner_before, "the slice changed the owner's settings.json"


@pytest.fixture(scope="module")
def fixtures():
    from brain_fixtures import build_brain_fixtures
    return build_brain_fixtures()


def _say(line: str) -> None:
    """A measured number for the gate's report (`-s`, or `VAI_EB1_MEASURES=<file>`)."""
    print(f"EB1: {line}")
    out = os.environ.get("VAI_EB1_MEASURES")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _run_edit(env: Any, sid: str, prompt: str) -> Any:
    run = env.run_prompt(sid, prompt, answers={"apply": "yes", "go": "yes"})
    assert not run.errors, run.errors
    assert run.done_count >= 1 and run.first_text.startswith("via ")
    return run


def _one_op(env: Any, sid: str, before_ops: int) -> dict[str, Any]:
    ops = M.load_ops(env.session_dir(sid))
    assert len(ops) == before_ops + 1, [o["tool"] for o in ops[before_ops:]]
    assert ops[-1]["tool"] == "prompt"
    return ops[-1]


def _edp(env: Any, sid: str, op: dict[str, Any]) -> dict[str, Any]:
    from video_ai_editor.brain import store as B
    did = L.decisions_id_of(op)
    assert did, f"the prompt op carries no decisions id: {op.get('args')}"
    edp = B.read_edp(env.session_dir(sid), did)
    assert edp is not None
    return json.loads(edp.model_dump_json())


# --------------------------------------------------------------------------
# 1. talking head → 45 s reel
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def th_result(slice_env, fixtures):
    env, th = slice_env, fixtures.th
    sid = env.new_session("EB1 TH reel")
    env.upload_video(sid, Path(th.video_16x9))
    L.transcribe(env, sid)
    L.analyse(env, sid)
    store = env.store(sid)
    before = {"hash": store.edl.hash(), "ops": len(M.load_ops(env.session_dir(sid))), "depth": store.undo_depth,
              "edl": M.load_edl(env.session_dir(sid))}
    run = _run_edit(env, sid, L.TH_PROMPT)
    op = _one_op(env, sid, before["ops"])
    return {"sid": sid, "run": run, "op": op, "before": before, "edl": M.load_edl(env.session_dir(sid)),
            "edp": _edp(env, sid, op), "src": str(L.v1(before["edl"])[0].src)}


def test_th_card_opens_on_the_plan_tab_and_apply_is_one_op(slice_env, th_result):
    env, r = slice_env, th_result
    preview = L.preview_of(r["run"])
    assert preview and isinstance(preview.get("brain"), dict), "no brain payload on the card"
    assert preview["brain"]["tab_default"] == "plan"
    assert preview["brain"]["decisions_id"] == L.decisions_id_of(r["op"])
    assert all(preview["brain"]["whys"]), "a Changes line without a why"
    store = env.store(r["sid"])
    assert store.undo_depth == r["before"]["depth"] + 1
    assert store.undo() and store.edl.hash() == r["before"]["hash"], "one ⌘Z did not restore the original"
    assert store.redo()
    rows = L.versions(env, r["sid"])
    assert [v["label"] for v in rows] == ["V1 Reel"] and rows[0]["restorable"] and rows[0]["current"]
    _say(f"TH one op ({r['op']['summary']}), one undo, {rows[0]['label']} restorable; card tab {preview['brain']['tab_default']}, "
         f"{len(preview['brain']['whys'])} Changes lines each with a why")


def test_th_opens_on_the_quotable_line_at_45s(th_result, fixtures):
    r, truth = th_result, fixtures.th.truth
    edl = r["edl"]
    assert abs(edl.duration - 45.0) <= 1.0, edl.duration
    assert r["edp"]["summary"]["hook"]["sent"] == truth.quotable
    s = truth.sentence(truth.quotable)
    assert L.first_kept_sentence_ok(edl, r["src"], s.t0, s.t1), (L.v1(edl)[0].in_, s.t0, s.t1)
    _say(f"TH duration {edl.duration:.3f} s (asked 45); opens at source {float(L.v1(edl)[0].in_):.3f} s, "
         f"{s.t0 - float(L.v1(edl)[0].in_):.3f} s before the quotable line ({s.t0:.3f}-{s.t1:.3f})")


def test_th_every_planted_filler_is_gone_by_time(th_result, fixtures):
    r, truth = th_result, fixtures.th.truth
    spans = [((truth.utt(u).voiced_start or truth.utt(u).start), (truth.utt(u).voiced_end or truth.utt(u).end))
             for u in (*truth.fillers_lexical, *truth.fillers_acoustic)]
    assert len(truth.fillers_acoustic) == 3
    left = L.source_seconds_removed(r["edl"], r["src"], spans)
    assert not left, f"fillers still on the timeline: {left}"
    _say(f"TH fillers gone by time: {len(truth.fillers_lexical)} lexical + {len(truth.fillers_acoustic)} acoustic of "
         f"{len(spans)} planted, 0 left")


def test_th_cut_edges_sit_on_troughs_and_seams_do_not_click(slice_env, th_result, fixtures):
    from timing_fixtures import audio_samples
    env, r, th = slice_env, th_result, fixtures.th
    edl = r["edl"]
    rate = 16000
    src_audio = np.asarray(audio_samples(Path(th.audio_wav), rate=rate), dtype=np.float32)
    edges = [float(a.out) for a, _ in L.seams(edl)] + [float(b.in_) for _, b in L.seams(edl)]
    bad = L.trough_violations(src_audio, rate, edges)
    assert not bad, f"cut edges off a trough (t, dBFS, local min): {bad}"
    render = M.render(env.session_dir(r["sid"]))
    assert render is not None
    out = np.asarray(audio_samples(render, rate=48000), dtype=np.float32)
    # a planted click AT a pause boundary that survives inside ±2 ms of a
    # seam is the fixture's, not the edit's: measured separately below
    clicks = set(round(t, 3) for t in th.truth.clicks)
    seam_ts = []
    for a, b in L.seams(edl):
        near = any(abs(float(a.out) - c) < 0.003 or abs(float(b.in_) - c) < 0.003 for c in clicks)
        if not near:
            seam_ts.append(float(b.start))
    peaks = [(t, p) for t, p in L.seam_click_peaks(out, 48000, seam_ts) if p >= L.SEAM_CLICK_MAX]
    assert not peaks, f"clicks at seams (t, peak): {peaks}"
    worst = max((p for _, p in L.seam_click_peaks(out, 48000, seam_ts)), default=0.0)
    levels = [L.rms_db(src_audio, rate, t) for t in edges]
    _say(f"TH {len(edges)} cut edges, loudest {max(levels):.1f} dBFS, 0 off a trough; {len(seam_ts)} seams in the render, "
         f"largest first-difference peak {worst:.4f} (limit {L.SEAM_CLICK_MAX})")


def test_th_punch_in_starts_at_the_clause_and_releases_as_a_step(th_result, fixtures):
    r, truth = th_result, fixtures.th.truth
    edl = r["edl"]
    tl = L.ref_to_timeline(edl, r["src"], truth.emphasis.clause_start)
    assert tl is not None, "the emphasised clause was cut away"
    hit = timeline_to_source_clip(edl, tl)
    keys = L.scale_keys(hit)
    assert keys is not None, "no scale keyframes on the emphasised clip"
    push = L.punch_push(keys)
    assert push is not None, keys.keyframes
    start_local, from_scale, reached, held = push
    # in at the clause start (to the frame), a push of at most half a second, to >= 1.08 ...
    assert abs((float(hit.start) + start_local) - tl) <= L.frame_s(edl) + 1e-6, (start_local, tl - hit.start)
    assert 0.0 < reached - start_local <= 0.5 and held >= 1.08, keys.keyframes
    # ONE mode per property, and it is the one the EDP decision names (SC-17: the old line accepted every valid mode)
    decision = _emphasis_decision(r["edp"], truth)
    assert not L.punch_keys_mismatch(hit, decision), L.punch_keys_mismatch(hit, decision)
    # held through the sentence: no key lowers the scale on this piece ...
    after_peak = [k for k in keys.keyframes if float(k[0]) > reached and float(k[1]) < held - 1e-6]
    assert not after_peak, f"the punch-in eases out instead of releasing as a step: {after_peak}"
    sent_end = L.ref_to_timeline(edl, r["src"], truth.sentence(truth.emphasis.sent).t1 - 0.05)
    assert sent_end is not None and sent_end <= float(hit.start) + float(hit.effective_duration) + 1e-6, \
        "the emphasised sentence runs past the punched piece"
    # … and the release is the seam: the next piece does not open at the held scale
    nxt = next((c for c in L.v1(edl) if abs(float(c.start) - (float(hit.start) + float(hit.effective_duration))) < 1e-3), None)
    if nxt is not None:
        opens = L.scale_at(nxt, 0.0)
        assert abs(opens - held) >= L.HIDE_MIN_STEP, (opens, held)      # a 1.10 -> 1.08 "step" is 2 %, not a release (EX-04)
    _say(f"TH punch-in in at {float(hit.start) + start_local:.3f} s on the timeline, clause start at {tl:.3f} "
         f"(Δ {abs(float(hit.start) + start_local - tl) * 1000:.1f} ms); push {reached - start_local:.2f} s from {from_scale:.2f} to {held:.2f}; "
         f"keys {[list(map(float, k)) for k in keys.keyframes]} ({keys.interp}); released as a step at the seam")


def _emphasis_decision(edp: dict[str, Any], truth: Any) -> dict[str, Any]:
    return next(d for d in edp["decisions"] if d["kind"] == "punch_in" and d["reason"]["code"] == "emphasis_peak"
                and truth.emphasis.sent in d["reason"]["facts"])


def test_th_punch_in_is_a_push_the_viewer_can_see(th_result, fixtures):
    """Lane E (planner/emphasis.py): a piece that opens on a hidden seam is already at the hide's 1.08 — the punch-in on
    its emphasised clause must still PUSH: 1.08 -> 1.10 is 2 % (the review's own bar for 'not a step' is 5 %)."""
    r, truth = th_result, fixtures.th.truth
    tl = L.ref_to_timeline(r["edl"], r["src"], truth.emphasis.clause_start)
    assert tl is not None, "the emphasised clause was cut away"
    keys = L.scale_keys(timeline_to_source_clip(r["edl"], tl))
    push = L.punch_push(keys) if keys is not None else None
    assert push is not None, "no punch-in on the emphasised clip"
    _t0, from_scale, _t1, held = push
    assert held - from_scale >= L.HIDE_MIN_STEP - 1e-9, f"the punch-in pushes {from_scale} -> {held}: {keys.keyframes}"   # (1.13 - 1.08 is 0.0499999… in floats)


def timeline_to_source_clip(edl, t: float):
    from video_ai_editor.agent.timemap import timeline_to_source
    hit = timeline_to_source(edl, "v1", t)
    assert hit is not None
    return hit[0]


def test_th_captions_cover_and_dialogue_lane_is_in_step(slice_env, th_result, fixtures):
    from timing_fixtures import av_offsets_ms
    env, r = slice_env, th_result
    edl = r["edl"]
    speech = [u.voiced for u in fixtures.th.truth.utts if u.kind == "speech"]
    cover = L.caption_coverage(edl, r["src"], speech)
    assert cover >= 0.9, f"captions cover {cover:.1%} of the speech that plays"
    a1 = edl.get_track("a1")
    assert a1 is not None and a1.type == "audio", "no dialogue lane a1"
    problems = L.a1_matches_v1(edl, r["src"], {r["src"]: 0.0}, tol=L.frame_s(edl) / 2)
    assert not problems, problems
    assert not (edl.get_track("v1").transitions or []), "a brain plan added a v1 transition over a1"
    render = M.render(env.session_dir(r["sid"]))
    offs = av_offsets_ms(render)
    half_frame_ms = 1000.0 * L.frame_s(edl) / 2
    assert offs and all(abs(o) <= half_frame_ms for o in offs), offs
    _say(f"TH captions cover {cover:.1%} of the speech; a1 matches v1 at {len(L.v1(edl))} pieces; "
         f"{len(offs)} click/flash pairs in the render, worst {max(abs(o) for o in offs):.2f} ms (half a frame {half_frame_ms:.1f} ms)")


def test_th_plan_tab_says_what_the_slice_promises(th_result):
    """Type, target, the hook quote with a seek time, the cuts grouped by
    reason, the punch-ins, captions, the bed, "Not done this time"."""
    edp, card = th_result["edp"], L.preview_of(th_result["run"])["brain"]
    s = edp["summary"]
    assert (s["project_type"], s["target"]) == ("talking_head", "reel")
    assert s["hook"]["quote"].startswith("I now finish") and isinstance(card["summary"]["hook"].get("timeline_t"), (int, float))
    codes = [d["reason"]["code"] for d in edp["decisions"] if d["kind"] == "cut_range"]
    assert {"silence", "filler", "filler_acoustic"} <= set(codes), codes
    assert sorted(d["reason"]["code"] for d in edp["decisions"] if d["kind"] == "punch_in") == ["emphasis_peak", "hook_emphasis"]
    assert s["captions"]["mode"] == "dynamic" and s["captions"]["style"] == "ig_chunky"
    assert s["music"]["shape"] == "bed" and s["music"]["rel_lu"] == 24.0 and s["music"]["duck_lu"] == 6.0
    assert s["deferred"] and all(d["asked"] and d["why"] for d in s["deferred"])
    assert all(d["reason"]["text"] and d["reason"]["facts"] for d in edp["decisions"] if d["ref"] is not None)
    grouped = [ln for ln in L.preview_of(th_result["run"])["lines"] if ln.startswith("Removed ")]
    assert len(grouped) == 1 and "filler" in grouped[0] and "silence" in grouped[0], grouped
    _say(f"TH plan: {len(codes)} cuts by reason {sorted(set(codes))}; card line “{grouped[0]}”; "
         f"not done this time: {[d['asked'] for d in s['deferred']]}")


def test_th_restore_v1_after_a_hand_edit_is_one_op(slice_env, th_result):
    env, r = slice_env, th_result
    sid = r["sid"]
    env.dispatch(sid, "add_text", {"text": "later", "start": 0, "end": 1})
    ops0 = len(M.load_ops(env.session_dir(sid)))
    out = L.restore(env, sid, L.versions(env, sid)[0]["id"])
    ops = M.load_ops(env.session_dir(sid))
    assert len(ops) == ops0 + 1 and ops[-1]["tool"] == "restore_version" and out["op"]["tool"] == "restore_version"
    assert M.load_edl(env.session_dir(sid)).hash() == r["edl"].hash()


# --------------------------------------------------------------------------
# 2. two-camera podcast (+ recorder) → premium podcast
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def p2_result(slice_env, fixtures):
    env = slice_env
    if fixtures.p2 is None:
        pytest.skip(fixtures.p2_skip_reason or "P2 fixture unavailable")
    p2 = fixtures.p2
    sid = env.new_session("EB1 P2 podcast")
    env.upload_video(sid, Path(p2.cam_a))
    env.upload_video(sid, Path(p2.cam_b))
    env.upload_audio(sid, Path(p2.recorder_wav), add_to_music=True)      # today's audio-only landing spot
    L.transcribe(env, sid)
    L.analyse(env, sid)
    before_ops = len(M.load_ops(env.session_dir(sid)))
    run = _run_edit(env, sid, L.P2_PROMPT)
    op = _one_op(env, sid, before_ops)
    edl = M.load_edl(env.session_dir(sid))
    srcs = sorted({str(c.src) for t in edl.tracks for c in t.clips if getattr(c, "src", None)})

    def ours(path: str) -> str:
        """The session's own copy of an uploaded file (`<stem>.normalized.mp4`, `<stem>_<hash>.wav`)."""
        hits = [x for x in srcs if Path(x).name.startswith(Path(path).stem)]
        assert len(hits) == 1, (path, srcs)
        return hits[0]
    return {"sid": sid, "run": run, "op": op, "edl": edl, "edp": _edp(env, sid, op),
            "cam_a": ours(p2.cam_a), "cam_b": ours(p2.cam_b), "recorder": ours(p2.recorder_wav)}


def _angle_of(r: dict[str, Any]) -> dict[str, str]:
    return {r["cam_a"]: "cam_a", r["cam_b"]: "cam_b"}          # the truth's `expected_angle` names


def test_p2_one_op_muted_mics_no_transition_and_v1_premium_podcast(slice_env, p2_result):
    env, r = slice_env, p2_result
    edl = r["edl"]
    assert all(c.audio and c.audio.mute for c in L.v1(edl)), "a camera mic is not muted"
    assert not (edl.get_track("v1").transitions or [])
    assert edl.get_track("a1") is not None
    music = edl.get_track("music")
    assert not music or not any(str(c.src) == r["recorder"] for c in music.clips), "the recorder is still a bed"
    assert [v["label"] for v in L.versions(env, r["sid"])] == ["V1 Premium Podcast"]
    store = env.store(r["sid"])
    depth, now = store.undo_depth, store.edl.hash()
    assert store.undo() and store.undo_depth == depth - 1 and store.redo() and store.edl.hash() == now
    _say(f"P2 one op ({r['op']['summary']}), one undo; {len(L.v1(edl))} v1 pieces all muted, 0 transitions, "
         f"recorder off the music lane, V1 Premium Podcast")


def test_p2_plan_tab_says_what_the_slice_promises(p2_result, fixtures):
    """Cuts by reason (silences, fillers, one false start), camera switches
    by reason, the dialogue line — and the planted fillers and the false
    start are gone by TIME."""
    r, truth = p2_result, fixtures.p2.truth
    edp = r["edp"]
    cuts = [d["reason"]["code"] for d in edp["decisions"] if d["kind"] == "cut_range"]
    assert {"silence", "filler"} <= set(cuts) and cuts.count("false_start") == 1, cuts
    sw = [d["reason"]["code"] for d in edp["decisions"] if d["kind"] == "switch_angle"]
    assert {"speaker_turn", "at_cut"} == set(sw), sw
    said = next(d["reason"]["text"] for d in edp["decisions"] if d["kind"] == "dialogue")
    # (was "dialogue from p2_recorder… on lane a1; camera microphones muted": UX-05 asked for words, not a file name and a lane id)
    assert "recorder" in said and "camera microphones muted" in said and not re.search(r"\.wav|\.mp4|src_|\ba1\b", said), said
    assert any(d["asked"] == "wide resets and reactions" for d in edp["summary"]["deferred"])
    gone = [u.voiced for u in truth.utts if u.kind in ("filler", "false_start") and u.voiced_start is not None]
    left = L.source_seconds_removed(r["edl"], r["recorder"], gone, track="a1")
    assert len(gone) == 5 and not left, f"still heard: {left}"
    _say(f"P2 plan: cuts {sorted((c, cuts.count(c)) for c in set(cuts))}; switches "
         f"{sorted((c, sw.count(c)) for c in set(sw))}; {len(gone)} planted fillers/false start gone by time; “{said}”")


@pytest.mark.xfail(strict=True, reason="EB1: the pause after the planted emotional line (reference 88.15-89.15 s) is cut as a "
                   "1.0 s silence. The line is emotional in its WORDS only — the two synthetic voices carry no acoustic "
                   "emotion (lane D's score: 0.30, floor 0.6) and whisper merged it with the next sentence, so §4.6.3 has "
                   "nothing to protect. A lexical emotion cue or the model's label is EB2.")
def test_p2_the_pause_after_the_emotional_line_is_kept(p2_result, fixtures):
    r, truth = p2_result, fixtures.p2.truth
    kept = [d for d in r["edp"]["decisions"] if d["kind"] == "keep_pause"]
    assert kept and any(d["reason"]["code"] == "pause_kept:emotion" for d in kept), [d["reason"]["code"] for d in kept]
    pause = next(p for p in truth.pauses if p.protection == "emotion")
    still = sum(b - a for a, b in L.on_timeline(r["edl"], r["recorder"], pause.t0, pause.t1, track="a1"))
    assert still >= 0.45 * (pause.t1 - pause.t0), still


def test_p2_talking_time_on_the_speakers_close_and_switches_anticipate(p2_result, fixtures):
    r, truth = p2_result, fixtures.p2.truth
    edl, angles = r["edl"], _angle_of(r)
    spoken = [(t.t0, t.t1, t.expected_angle) for t in truth.turns
              if t.expected_angle and t.id not in truth.backchannels]
    share = L.angle_share(edl, r["recorder"], spoken, angles)
    assert share >= 0.9, f"{share:.2%} of talking time on the speaker's close"
    # a turn's onset is its first sound that still PLAYS (a removed "um" at
    # the head of a turn is not what the viewer hears the speaker start with)
    onsets = L.heard_onsets(edl, r["recorder"], truth)
    assert len(onsets) >= 0.8 * sum(1 for t in truth.turns if t.id not in truth.backchannels)
    switches = L.angle_switches(edl, r["recorder"])
    assert len(switches) >= 20, f"only {len(switches)} camera changes over {len(onsets)} turns"
    late = []
    for seam_t, _a, _b in L.angle_switches(edl, r["recorder"]):
        nxt = min((o for o in onsets if o >= seam_t - 1e-3), default=None)
        lead = (nxt - seam_t) if nxt is not None else None
        if lead is None or not (0.0 <= lead <= 0.15 + 1e-3):
            late.append((round(seam_t, 3), lead))
    assert not late, f"switches not 0-0.15 s before an onset (seam ref s, lead): {late}"
    leads = [min(o for o in onsets if o >= t - 1e-3) - t for t, _a, _b in switches]
    _say(f"P2 {share:.1%} of talking time on the speaker's close; {len(switches)} camera changes, leads "
         f"{min(leads):.3f}-{max(leads):.3f} s before the heard onset ({len(onsets)} turns heard)")


def test_p2_backchannels_never_switch(p2_result, fixtures):
    r, truth = p2_result, fixtures.p2.truth
    switches = [t for t, _a, _b in L.angle_switches(r["edl"], r["recorder"])]
    offenders = []
    for bid in truth.backchannels:
        t = truth.turn(bid)
        if any(t.t0 - 0.2 <= s <= t.t1 for s in switches):
            offenders.append(bid)
    assert not offenders, offenders
    _say(f"P2 {len(truth.backchannels)} backchannel turns, 0 with a camera change")


def test_p2_every_tighten_seam_is_hidden(p2_result):
    r = p2_result
    exposed = [(round(gap, 2), float(b.start)) for a, b, gap in L.tighten_seams(r["edl"], r["recorder"])
               if L.hidden_by(a, b) is None]
    assert not exposed, f"tighten seams ≥ 0.4 s without an angle change or scale step: {exposed}"
    how = [(round(gap, 2), L.hidden_by(a, b)) for a, b, gap in L.tighten_seams(r["edl"], r["recorder"])]
    assert len(how) >= 4, how
    _say(f"P2 {len(how)} tighten seams ≥ 0.4 s, hidden by (removed s, how): {how}")


def test_p2_barcodes_and_clicks_prove_sync_at_every_seam(slice_env, p2_result, fixtures):
    from timing_fixtures import av_offsets_ms
    env, r, truth = slice_env, p2_result, fixtures.p2.truth
    render = M.render(env.session_dir(r["sid"]))
    assert render is not None
    sids = {r["cam_a"]: truth.sids["cam_a"], r["cam_b"]: truth.sids["cam_b"]}
    offsets = {r["cam_a"]: truth.offsets["cam_a"], r["cam_b"]: truth.offsets["cam_b"], r["recorder"]: 0.0}
    bad = L.barcode_mismatches(render, r["edl"], sids)
    assert not bad, bad
    offs = av_offsets_ms(render)
    half_frame_ms = 1000.0 / truth.fps / 2
    assert offs and all(abs(o) <= half_frame_ms for o in offs), offs
    problems = L.a1_matches_v1(r["edl"], r["recorder"], offsets, tol=L.frame_s(r["edl"]) / 2)
    assert not problems, problems
    _say(f"P2 bar codes: sid and frame right at 3 samples in each of {len(L.v1(r['edl']))} pieces; {len(offs)} click/flash "
         f"pairs, worst {max(abs(o) for o in offs):.2f} ms (half a frame {half_frame_ms:.1f} ms); a1 matches v1 everywhere")


# --------------------------------------------------------------------------
# 2b. the review's findings (EB1 fix wave), measured on the demos' own output
# --------------------------------------------------------------------------

def _clone(env: Any, sid: str, name: str) -> str:
    """A self-contained copy of session `sid` (the harness's clone, paths re-pointed) under a fresh id."""
    from video_ai_editor import main as _main
    from video_ai_editor.storage import new_session_id
    new = new_session_id()
    clone_session_dir(env.session_dir(sid), env.session_dir(new))
    meta = env.session_dir(new) / "meta.json"
    data = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    meta.write_text(json.dumps({**data, "name": name}), encoding="utf-8")
    _main._STORES.pop(new, None)
    return new


@pytest.fixture(scope="module")
def th_pristine(slice_env, fixtures):
    """A transcribed talking-head upload the brain has NOT read: what a session is right after import."""
    env = slice_env
    sid = env.new_session("EB1 TH pristine")
    env.upload_video(sid, Path(fixtures.th.video_16x9))
    L.transcribe(env, sid)
    return sid


@pytest.fixture(scope="module")
def p2_pristine(slice_env, fixtures):
    env = slice_env
    if fixtures.p2 is None:
        pytest.skip(fixtures.p2_skip_reason or "P2 fixture unavailable")
    sid = env.new_session("EB1 P2 pristine")
    for f in (fixtures.p2.cam_a, fixtures.p2.cam_b):
        env.upload_video(sid, Path(f))
    env.upload_audio(sid, Path(fixtures.p2.recorder_wav), add_to_music=True)
    L.transcribe(env, sid)
    return sid


@pytest.fixture(scope="module")
def th_export(slice_env, th_result):
    return L.export(slice_env, th_result["sid"])


@pytest.fixture(scope="module")
def p2_export(slice_env, p2_result):
    return L.export(slice_env, p2_result["sid"])


# --- seams: the zoom the viewer sees (EX-01, EX-04, UX-11) -------------------------------------------------------

def test_th_every_tighten_seam_is_hidden_in_the_edl(th_result):
    """Lane E (planner/emphasis.py `_hides`, EX-04/UX-11): the piece after every jump cut OPENS at a zoom that
    differs from where the last one ENDED by >= 0.05 — not a key that repeats the outgoing scale."""
    edl, src = th_result["edl"], th_result["src"]
    rows = [(round(gap, 2), round(float(b.start), 3), round(L.scale_at(a, float(a.effective_duration)), 3), round(L.scale_at(b, 0.0), 3))
            for a, b, gap in L.tighten_seams(edl, src) if L.hidden_by(a, b) is None]
    assert not rows, f"jump cuts with no zoom change (removed s, timeline s, zoom out, zoom in): {rows}"


def _assert_seams_change_the_zoom(video: Path, edl: Any, ref_src: str) -> tuple[int, int]:
    scales, fps = L.bar_scales(video)
    base = L.zoom_calibration(scales, edl, fps)
    assert abs(base - 1.0) <= L.ZOOM_CALIBRATION_TOL, f"the bar-code geometry is off: an unzoomed piece reads {base:.3f}"
    seams = L.tighten_seams(edl, ref_src)
    flat = L.unseen_seams(scales, fps, seams)
    assert not flat, (f"jump cuts that look the same before and after in the DECODED export (timeline s, removed s, "
                      f"zoom before, zoom after; an unzoomed piece reads {base:.3f}): {flat}")
    return len(seams), sum(1 for a, b, _ in seams if str(a.src) != str(b.src))


def test_th_hidden_seams_change_the_zoom_in_the_decoded_export(th_result, th_export):
    """Lane E (EX-01/EX-04/UX-11, planner emits two keys per hide; a 1-key Keyframe renders at 1.0 — RENDER_BEHAVIOR_VERSION
    is frozen this wave). The export the viewer would get, decoded: every reel seam >= 0.4 s changes zoom by >= 0.05."""
    n, angles = _assert_seams_change_the_zoom(th_export, th_result["edl"], th_result["src"])
    assert n >= 5 and angles == 0, (n, angles)          # a reel has no second camera: every seam hides by zoom
    _say(f"TH decoded export: {n} tighten seams, every one changes the zoom by >= {L.HIDE_MIN_STEP} (bar-code cell width)")


def test_p2_hidden_seams_change_the_zoom_or_the_angle_in_the_decoded_export(p2_result, p2_export):
    n, angles = _assert_seams_change_the_zoom(p2_export, p2_result["edl"], p2_result["recorder"])
    assert n >= 4 and angles >= 2, (n, angles)          # the angle changes are proven by the bar codes' sid (barcodes test)
    _say(f"P2 decoded export: {n} tighten seams, {angles} hidden by an angle change, {n - angles} by a zoom step >= {L.HIDE_MIN_STEP}")


# --- captions (UX-03, UX-09) -------------------------------------------------------------------------------------

def test_p2_captions_cover_the_speech_on_both_cameras(p2_result, fixtures):
    """Lane E (planner/captions.py + the caption source, UX-03): the podcast's captions came from cam A's transcript
    only — the guest's turns had none. Covered share of the speech that plays, per camera AND per speaker, >= 0.9."""
    r, truth = p2_result, fixtures.p2.truth
    edl, angles = r["edl"], _angle_of(r)
    utts = [(float(u.voiced_start), float(u.voiced_end), u.speaker) for u in truth.utts if u.kind == "speech" and u.voiced_start is not None]
    by_cam = L.speech_cover_by(edl, r["recorder"], utts, lambda tl, _g: angles.get(str(timeline_to_source_clip(edl, tl).src), "?"))
    by_who = L.speech_cover_by(edl, r["recorder"], utts, lambda _tl, g: g)
    assert sorted(by_cam) == ["cam_a", "cam_b"] and sorted(by_who) == ["S1", "S2"], (sorted(by_cam), sorted(by_who))
    low = {f"{kind}:{k}": round(c / n, 3) for kind, groups in (("camera", by_cam), ("speaker", by_who)) for k, (c, n) in groups.items() if c < 0.9 * n}
    assert not low, f"captions cover < 0.9 of the speech that plays: {low}"
    _say("P2 captions cover " + ", ".join(f"{k} {c / n:.1%}" for k, (c, n) in sorted({**by_cam, **by_who}.items())) + " of the speech that plays")


def _caption_faults(edl: Any, ref_src: str, spans: list[tuple[float, float]]) -> dict[str, Any]:
    playing = L.speech_on_timeline(edl, ref_src, spans)
    return {"a removed filler on screen (s, text)": L.caption_filler_cues(edl),
            "a cue up across a cut (start, end, text, cut)": L.cues_across(edl, L.hard_seams(edl)),
            "a cue held where no speech plays (start, end, text)": L.floating_cues(edl, playing)}


def test_th_captions_never_show_a_removed_filler_or_run_past_a_seam(th_result, fixtures):
    """Lane E (brain/compile.py + planner/captions.py, UX-09): captions used the raw whisper timings — 'Um,' flashed for
    0.1 s and 'small work?' hung on across the seam into the next sentence."""
    r = th_result
    spans = [u.voiced for u in fixtures.th.truth.utts if u.kind == "speech"]
    faults = {k: v for k, v in _caption_faults(r["edl"], r["src"], spans).items() if v}
    assert not faults, faults


def test_p2_captions_show_no_removed_filler_and_stay_over_speech(p2_result, fixtures):
    r = p2_result
    spans = [(float(u.voiced_start), float(u.voiced_end)) for u in fixtures.p2.truth.utts
             if u.kind in ("speech", "backchannel") and u.voiced_start is not None]
    faults = {k: v for k, v in _caption_faults(r["edl"], r["recorder"], spans).items() if v}
    assert not faults, faults


# --- the camera rule and the speech whisper dropped (UX-10, D's flag) --------------------------------------------

def test_p2_every_full_turn_is_on_its_speakers_close(p2_result, fixtures):
    """Lane E (planner/camera.py, UX-10/EX-07): 92.7 % share hid five whole turns on the wrong face because the 2.5 s
    minimum shot outranked them. Per turn: not one full host/guest turn (backchannels and the overlap excepted) may
    spend less than 0.9 of its playing time on its speaker's close."""
    r, truth = p2_result, fixtures.p2.truth
    rows = L.turn_shares(r["edl"], r["recorder"], truth, _angle_of(r))
    assert len(rows) >= 30, len(rows)
    off = [(i, spk, round(t0, 2), round(t1, 2), f"{k}/{n}") for i, spk, t0, t1, n, k in rows if n and k < 0.9 * n]
    assert not off, f"full turns not on their speaker's close (turn, speaker, ref s, ref s, samples on close): {off}"
    _say(f"P2 {len(rows)} full turns, each on its speaker's close for >= 90 % of the time it plays")


#: Reference-second stretches whisper gave NO word (lane D's flag; truth utterances u_0012 "Why the sensor first?",
#: u_0014 "The lens, the heat…", u_0025 the guest's "Yeah."): the planner must treat them as sound and keep them.
_WHISPER_DROPPED = ((33.70, 35.10), (38.14, 39.46), (71.05, 71.51))


def test_p2_speech_whisper_dropped_is_not_cut(p2_result):
    r = p2_result
    lost = {(a, b): round(L.played_fraction(r["edl"], r["recorder"], a, b), 3) for a, b in _WHISPER_DROPPED}
    assert all(v >= 0.95 for v in lost.values()), f"speech the transcript has no word for was cut (stretch: share that plays): {lost}"


def _drop_words(ingest: Path, spans: list[tuple[float, float]]) -> int:
    """Take the words whose midpoint lies in `spans` (the upload's own seconds) out of the upload's transcript — what
    the ASR did on lane D's run and whisper.cpp does not do on this Mac's."""
    data = json.loads(ingest.read_text(encoding="utf-8"))
    tx = data.get("transcript") or {}
    mid = lambda w: 0.5 * (float(w["start"]) + float(w["end"]))  # noqa: E731
    gone = 0
    for seg in tx.get("segments") or []:
        words = seg.get("words") or []
        kept = [w for w in words if not any(a <= mid(w) <= b for a, b in spans)]
        if len(kept) != len(words):
            gone += len(words) - len(kept)
            seg["words"], seg["text"] = kept, " ".join(str(w["word"]).strip() for w in kept)
    tx["segments"] = [sg for sg in tx.get("segments") or [] if sg.get("words") or not sg.get("text", "").strip()]
    if isinstance(tx.get("words"), list):
        tx["words"] = [w for w in tx["words"] if not any(a <= mid(w) <= b for a, b in spans)]
    ingest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return gone


@pytest.fixture(scope="module")
def p2_dropped(slice_env, p2_pristine, fixtures):
    """The podcast demo over a transcript that HAS the three stretches missing (whisper.cpp small hears them on this Mac,
    so the demo's own transcript cannot exercise the rule): the read must still find the sound, the edit must keep it."""
    from video_ai_editor.brain.planner.graph_view import load_graph
    env, truth = slice_env, fixtures.p2.truth
    sid = _clone(env, p2_pristine, "EB1 P2 whisper dropped")
    gone = 0
    for ij in sorted(env.session_dir(sid).glob("uploads/*/ingest.json")):
        off = next((truth.offsets[c] for c in ("cam_a", "cam_b") if ij.parent.name.startswith(f"p2_{c}")), None)
        if off is not None:
            gone += _drop_words(ij, [(a - 0.05 + off, b + 0.05 + off) for a, b in _WHISPER_DROPPED])
    assert gone >= 3, f"only {gone} words were taken out of the transcripts"
    gid = L.analyse(env, sid)
    g = load_graph(env.session_dir(sid), gid)
    heard = [(a, b) for a, b in _WHISPER_DROPPED if any(not w.get("filler") and a <= 0.5 * (w["t0"] + w["t1"]) <= b for w in g.words)]
    assert not heard, f"the simulated drop did not reach the graph's words: {heard}"
    before = len(M.load_ops(env.session_dir(sid)))
    _run_edit(env, sid, L.P2_PROMPT)
    _one_op(env, sid, before)
    edl = M.load_edl(env.session_dir(sid))
    return {"sid": sid, "edl": edl, "recorder": next(str(c.src) for c in L.media_clips(edl, "a1")), "gone": gone}


def test_p2_speech_the_transcript_has_no_word_for_is_not_cut(p2_dropped):
    """Lane D/E (planner/tighten.py `_unheard`): with whisper's words gone from the three stretches the audio layer still
    hears voice there; a gap with sound in it is not a silence, and no removal may take it."""
    r = p2_dropped
    lost = {(a, b): round(L.played_fraction(r["edl"], r["recorder"], a, b), 3) for a, b in _WHISPER_DROPPED}
    assert all(v >= 0.95 for v in lost.values()), f"speech the transcript has no word for was cut (stretch: share that plays): {lost}"
    _say(f"P2 with {r['gone']} words taken out of the transcripts: the three stretches still play {sorted(lost.values())}")


# --- the project's rate (EX-08) ---------------------------------------------------------------------------------

@pytest.fixture(scope="module", params=(20, 25, 29.97, 30), ids=lambda f: f"{f}fps")
def p2_at_rate(request, slice_env, p2_pristine, fixtures):
    """The podcast demo with the PROJECT set to `fps` before the prompt (20 is the recorded demo itself)."""
    env, fps = slice_env, request.param
    if fps == 20:
        r = request.getfixturevalue("p2_result")
        return {"fps": fps, "sid": r["sid"], "edl": r["edl"], "edp": r["edp"], "recorder": r["recorder"]}
    sid = _clone(env, p2_pristine, f"EB1 P2 {fps} fps")
    env.dispatch(sid, "set_canvas", {"fps": fps})
    L.analyse(env, sid)
    before = len(M.load_ops(env.session_dir(sid)))
    _run_edit(env, sid, L.P2_PROMPT)
    op = _one_op(env, sid, before)
    edl = M.load_edl(env.session_dir(sid))
    rec = next(str(c.src) for c in L.media_clips(edl, "a1"))
    return {"fps": fps, "sid": sid, "edl": edl, "edp": _edp(env, sid, op), "recorder": rec}


def test_p2_at_every_project_rate_clicks_meet_flashes_and_no_hide_is_dropped(slice_env, p2_at_rate):
    """Lane E (planner/types.py grid, EX-08): the planner cut on the FILE's 20 fps grid, so at a 30 fps project the
    picture landed half an output frame late on 10 of 35 clicks, and at 29.97 both hide keys were dropped."""
    from timing_fixtures import av_offsets_ms
    from video_ai_editor.edl.timebase import fps_float
    env, r = slice_env, p2_at_rate
    edl = r["edl"]
    assert abs(fps_float(edl.canvas.fps) - r["fps"]) < 0.01, (edl.canvas.fps, r["fps"])
    render = M.render(env.session_dir(r["sid"]))
    assert render is not None
    offs = av_offsets_ms(render)
    half = 500.0 / fps_float(edl.canvas.fps)
    tol = L.click_tolerance_ms(fps_float(edl.canvas.fps))
    late = [(i, round(o, 2)) for i, o in enumerate(offs) if abs(o) > half + tol]
    assert len(offs) >= 30 and not late, f"{len(offs)} click/flash pairs; beyond half a frame ({half:.1f} ms): {late}"
    hides = L.hide_status(r["edp"], edl, r["recorder"])
    assert len(hides) >= 2 and all(st == "ok" for _, st in hides), f"jump-cut hides the plan decided but the timeline lacks: {hides}"
    _say(f"P2 at {r['fps']} fps: {len(offs)} click/flash pairs, worst {max(abs(o) for o in offs):.2f} ms (half a frame {half:.1f}); "
         f"{len(hides)} hide keys all on the timeline")


# --- a second run over an edited timeline (EX-02, UX-04) ----------------------------------------------------------

def _nothing_committed(env: Any, sid: str, before_hash: str, before_ops: int) -> None:
    assert M.load_edl(env.session_dir(sid)).hash() == before_hash, "the timeline changed"
    assert len(M.load_ops(env.session_dir(sid))) == before_ops, [o["tool"] for o in M.load_ops(env.session_dir(sid))[before_ops:]]


def test_p2_rerun_on_its_own_output_commits_nothing(slice_env, p2_result):
    """Lane E (brain/compile.py `_angles_off_v1`, brain/resolve.py, EX-02/UX-04): the second 'premium podcast' over the
    already tightened podcast cut 107.55 s more (160 s -> 47 s) and was applied. Answer every question it lets
    through with yes: the timeline must not move."""
    env = slice_env
    cid = _clone(env, p2_result["sid"], "EB1 P2 rerun")
    before, ops0 = M.load_edl(env.session_dir(cid)).hash(), len(M.load_ops(env.session_dir(cid)))
    out = L.rerun(env, cid, L.P2_PROMPT, apply="yes")
    assert not out["run"].errors, out["run"].errors
    _nothing_committed(env, cid, before, ops0)
    assert out["asked"] or out["text"].strip(), "the second run said nothing at all"
    _say(f"P2 second run: nothing committed; {'asked ' + repr(out['asked'][0][:60]) if out['asked'] else 'said ' + repr(out['text'][:80])}")


def test_th_rerun_after_a_hand_edit_asks_and_offers_no_card(slice_env, th_result):
    """Lane E/C (UX-04): 'make a 45-second reel' again over a hand-edited reel used to offer a 1-change card (or,
    with the source imported again, commit a 114 s 'V2 Reel'). With the timeline no longer the brain's own it
    asks ONE question or says why it will not — never a card of edits, and nothing is committed."""
    env = slice_env
    cid = _clone(env, th_result["sid"], "EB1 TH hand edit")
    edl = M.load_edl(env.session_dir(cid))
    env.dispatch(cid, "ripple_delete", {"clip_id": L.v1(edl)[3].id})
    before, ops0 = M.load_edl(env.session_dir(cid)).hash(), len(M.load_ops(env.session_dir(cid)))
    out = L.rerun(env, cid, L.TH_PROMPT, apply="no")
    assert not out["run"].errors, out["run"].errors
    assert out["card"] is None, f"a card of edits was offered over a hand-edited timeline: {out['card']['lines'][:4]}"
    assert out["asked"] or out["text"].strip(), "neither a question nor a sentence"
    _nothing_committed(env, cid, before, ops0)


# --- a saved and reopened project (EX-03) -----------------------------------------------------------------------

def test_a_saved_and_reopened_project_keeps_its_versions_decisions_and_restore(slice_env, th_result):
    """Lane C (storage_project.py `_write_archive`, EX-03): the .vae carried no brain/ — a reopened reel had no
    versions, a prompt op pointing at a missing EDP, and no way back to V1."""
    env, r = slice_env, th_result
    before = [(v["label"], v["restorable"]) for v in L.versions(env, r["sid"])]
    assert before == [("V1 Reel", True)], before
    nsid = L.save_and_reopen(env, r["sid"])
    rows = L.versions(env, nsid)
    assert [(v["label"], v["restorable"]) for v in rows] == before, rows
    did = L.decisions_id_of(r["op"])
    got = env.client.get(f"/api/sessions/{nsid}/brain/decisions/{did}", headers=L.SAME_ORIGIN)
    assert got.status_code == 200, (got.status_code, got.text[:200])
    env.dispatch(nsid, "add_text", {"text": "later", "start": 0, "end": 1})
    out = L.restore(env, nsid, rows[0]["id"])
    assert out["op"]["tool"] == "restore_version"
    assert L.pieces(M.load_edl(env.session_dir(nsid))) == L.pieces(r["edl"]), "restore V1 after reopening is not the reel"
    _say(f"reopened project {nsid}: versions {before}, decisions {did} readable, restore V1 = the reel")


# --- before the transcript exists, and cancelling the read (UX-01, UX-07) ---------------------------------------

def _offers_a_cutting_reel(card: dict[str, Any] | None) -> bool:
    return bool(card) and any(ln.startswith("Removed ") for ln in card["lines"]) and bool((card.get("brain") or {}).get("summary", {}).get("hook"))


def test_a_prompt_typed_before_the_transcript_exists_never_offers_a_cutless_edit(slice_env, th_pristine):
    """Lane D (brain/graph.py `analyse`, UX-01): the read raced the upload's transcription, pinned a speech-less
    graph and offered a 'reel' of the whole 69.9 s with no cuts. Whatever it offers must cut; a re-run once the
    transcript has landed must not reuse the speech-less graph."""
    env = slice_env
    cid = _clone(env, th_pristine, "EB1 TH race")
    landing = env.simulate_upload_transcript(cid, delay_s=6.0)            # the words land 6 s after the prompt
    first = L.rerun(env, cid, L.TH_PROMPT, apply="no")
    landing.join()
    assert not first["run"].errors, first["run"].errors
    assert first["card"] is None or _offers_a_cutting_reel(first["card"]), f"a cut-less card: {first['card']['lines']}"
    again = L.rerun(env, cid, L.TH_PROMPT, apply="no")
    assert _offers_a_cutting_reel(again["card"]), (again["asked"], again["text"][:200])


def test_cancelling_the_read_leaves_no_pending_card(slice_env, th_pristine):
    """Lane F (api/prompt_routes.py cancel + brain_seams, UX-07): 'Cancel run' while the footage was being read did
    nothing — the analysis and the re-plan went on and the card popped up afterwards."""
    from video_ai_editor.agent.prompt import brain_seams
    env = slice_env
    cid = _clone(env, th_pristine, "EB1 TH cancel")
    ops0 = len(M.load_ops(env.session_dir(cid)))
    first = env.client.post(f"/api/sessions/{cid}/prompt", json={"message": L.TH_PROMPT, "brain": "recipes"})
    gate = next(e for e in parse_sse(first.text) if e.get("type") == "clarify")
    q = gate["questions"][0]
    assert q["key"] == "gate_analysis", q
    box: dict[str, Any] = {}

    def _answer() -> None:
        box["r"] = env.client.post(f"/api/sessions/{cid}/prompt/answer",
                                   json={"token": gate["token"], "answers": {q["key"]: env.answer_for(q, allow_downloads=False, answers=None)}})
    worker = threading.Thread(target=_answer, name="eb1-answer")
    worker.start()
    job = None
    deadline = time.time() + 30.0
    while time.time() < deadline:
        job = brain_seams.latest_analysis_job(cid)
        if job is not None and job.status in ("queued", "running"):
            break
        time.sleep(0.02)
    assert job is not None and job.status in ("queued", "running"), "the read never started"
    assert env.client.post(f"/api/sessions/{cid}/prompt/cancel", json={}).status_code == 200
    worker.join(120.0)
    assert not worker.is_alive(), "the run did not end after Cancel"
    events = parse_sse(box["r"].text)
    cards = [e for e in events if e.get("type") == "clarify" and isinstance(e.get("preview"), dict)]
    assert not cards, f"a card appeared after the cancel: {cards[0]['preview']['lines'][:3]}"
    assert not (env.session_dir(cid) / "prompt_pending.json").exists(), "a question is still pending"
    assert env.client.get(f"/api/jobs/{job.id}").json()["status"] == "cancelled"
    assert len(M.load_ops(env.session_dir(cid))) == ops0


# --- versions (SC-06) -------------------------------------------------------------------------------------------

def test_applying_the_same_plan_twice_gives_two_version_labels(slice_env, th_pristine):
    """Lane F (agent/prompt/service.py `record_brain_version` + brain_card, SC-06/EX-05): the EDP id is a content hash,
    so 'make a 45-second reel', ⌘Z, 'make a 45-second reel' recorded 'V1 Reel' twice — and the reply's title too."""
    env = slice_env
    cid = _clone(env, th_pristine, "EB1 TH twice")
    L.analyse(env, cid)
    first = _run_edit(env, cid, L.TH_PROMPT)
    assert env.store(cid).undo()
    second = _run_edit(env, cid, L.TH_PROMPT)
    rows = [v["label"] for v in L.versions(env, cid)]
    assert rows == ["V1 Reel", "V2 Reel"], rows
    assert "V2 Reel" in second.reply and "V1 Reel" not in second.reply, second.reply[:160]
    assert "V1 Reel" in first.reply, first.reply[:160]


# --- the flag off (SC-04) ---------------------------------------------------------------------------------------

def test_flag_off_the_prompt_bar_is_0_8_0_for_the_24_edit_phrasings(slice_env, th_pristine, monkeypatch):
    """Lane E (agent/prompt/grammar.py, SC-04): with brain.enabled OFF the `edit` grammar rows changed what phrases did
    and the replies named the hidden feature. Each phrasing through the real /prompt route must route, ask and open
    its reply exactly as FLAG_OFF_080 — recorded from `git archive HEAD` — says, and never say 'Brain'."""
    env = slice_env
    cid = _clone(env, th_pristine, "EB1 TH flag off")
    monkeypatch.setenv("VAI_BRAIN_ENABLED", "0")
    sd = env.session_dir(cid)
    before = M.load_edl(sd).hash()
    diffs, brain_talk = {}, []
    for phrase, want in L.FLAG_OFF_080.items():
        r = env.client.post(f"/api/sessions/{cid}/prompt", json={"message": phrase, "brain": "recipes"})
        events = parse_sse(r.text)
        got = L.phrase_signature(events)
        if got != want:
            diffs[phrase] = {"0.8.0": want, "now": got}
        if "brain" in json.dumps([e for e in events if e.get("type") in ("text_delta", "clarify")], default=str).lower():
            brain_talk.append(phrase)
        for name in ("prompt_pending.json", "prompt_run.json", "chat.json"):        # each phrase starts from a clean bar
            (sd / name).unlink(missing_ok=True)
    assert M.load_edl(sd).hash() == before, "a phrasing changed the timeline"
    assert not diffs, f"{len(diffs)} of {len(L.FLAG_OFF_080)} phrasings differ from 0.8.0: {json.dumps(diffs, indent=1, default=str)[:2400]}"
    assert not brain_talk, f"the reply names the hidden feature: {brain_talk}"


# --- the analysis takes what a transcript is made of (SC-01) ----------------------------------------------------

def test_analysis_survives_a_quote_in_the_hook_sentence(slice_env, th_pristine, fixtures, monkeypatch):
    """Lane D (brain/digest.py `assert_path_free`, SC-01): a double quote in a top-quartile hook sentence made the
    payload guard read the JSON escape as a path and the whole analysis failed with PathLeak. The gateway only
    exists when the brain is not pinned to `recipes` (`VAI_BRAIN=fm`, the production default), so the read runs
    under it — with or without the model on this Mac, the semantic layer must come out `ok`, not failed or degraded."""
    env = slice_env
    cid = _clone(env, th_pristine, "EB1 TH quote")
    ingest = Path(env.ingest_json(cid))
    data = json.loads(ingest.read_text(encoding="utf-8"))
    sent = fixtures.th.truth.sentence(fixtures.th.truth.quotable)
    hit = 0
    for seg in data["transcript"]["segments"]:
        words = [w for w in seg.get("words", []) if sent.t0 - 0.05 <= float(w["start"]) <= sent.t1]
        for w in words[:3]:
            w["word"] = f'"{w["word"]}"'
            hit += 1
        if words:
            seg["text"] = " ".join(w["word"] for w in seg["words"])
    assert hit >= 3, "the quotable sentence's words were not found"
    ingest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("VAI_BRAIN", "fm")
    gid = L.analyse(env, cid)                               # asserts the job COMPLETED
    graph = env.client.get(f"/api/sessions/{cid}/brain/graph", headers=L.SAME_ORIGIN).json()
    assert graph["layer_status"].get("semantic") == "ok", graph["layer_status"]
    monkeypatch.setenv("VAI_BRAIN", "recipes")
    run = env.run_prompt(cid, L.TH_PROMPT, answers={"apply": "no", "go": "yes"})
    assert not run.errors, run.errors
    card = L.preview_of(run)
    assert _offers_a_cutting_reel(card), "no reel came out of a transcript with quotes in it"
    if _fm_available() is None:              # the model is here: its re-rank must have reached the graph, not been skipped
        assert card["brain"]["rungs"]["content_brain"] == "apple_intelligence", card["brain"]["rungs"]
    _say(f"analysis {gid} over a hook sentence with double quotes under VAI_BRAIN=fm: {graph['layer_status']}, "
         f"reel planned, moments by {card['brain']['rungs']['content_brain']}")


# --- what the Plan tab says (UX-05, UX-06) ----------------------------------------------------------------------

_TELEMETRY = re.compile(r"src_[0-9a-f]{6,}|\.normalized|σ|\barousal\b|\benergy \d|\(0\.\d\d\)|topic .{0,3}the footage|\bsigma\b", re.I)
_TIMECODE = re.compile(r"\d+:\d\d")


def _card_texts(card: dict[str, Any]) -> list[str]:
    return [d["text"] for d in card["decisions"]] + list(card["whys"])


def test_th_plan_tab_reads_like_an_editor_and_marks_only_what_happened(th_result):
    """Lane F/E (brain/reasons.py, brain_card.py, UX-05/UX-06): no source keys, file internals, σ / arousal / energy /
    raw confidences; every punch-in and hide names a time; nothing that was applied is struck through; every 'why'
    is joined with a space and says a thing once."""
    card = L.preview_of(th_result["run"])["brain"]
    telemetry = [t for t in _card_texts(card) if _TELEMETRY.search(t)]
    assert not telemetry, f"Plan-tab lines that are telemetry, not reasons: {telemetry}"
    untimed = [d["text"] for d in card["decisions"] if d["kind"] in ("punch_in", "jump_cut_hide") and not _TIMECODE.search(d["text"])]
    assert not untimed, f"punch-ins / hides without a time: {untimed}"
    struck = [(d["id"], d["kind"], d["text"][:50]) for d in card["decisions"] if d["applied"] is False and not d["optional"]]
    assert not struck and not card["overclaims"], f"decisions the card strikes through although the edit was applied: {struck} {card['overclaims']}"
    joined = [w for w in card["whys"] if re.search(r"\w—|—\w", w) or len(set(w.split("; "))) != len(w.split("; "))]
    assert not joined, f"a 'why' with a missing space before the dash or a clause said twice: {joined}"


def test_p2_plan_tab_names_who_and_when_for_every_camera_change(p2_result):
    card = L.preview_of(p2_result["run"])["brain"]
    telemetry = [t for t in _card_texts(card) if _TELEMETRY.search(t)]
    assert not telemetry, telemetry
    sw = [d["text"] for d in card["decisions"] if d["kind"] == "switch_angle"]
    assert len(sw) >= 12 and all(_TIMECODE.search(t) for t in sw) and len(set(sw)) == len(sw), sw[:6]
    assert not [d["id"] for d in card["decisions"] if d["applied"] is False and not d["optional"]]


# --------------------------------------------------------------------------
# 3. Apple Intelligence as an upgrade, never a requirement
# --------------------------------------------------------------------------

#: What a re-rank may change and nothing else: the graph it froze into (the
#: semantic layer holds the model's answer), who ranked, and — on the
#: decisions that rest on the hook's rank — `by`, the score and the score in
#: the reason's words.
_HOOK_CODES = ("hook_strongest_opening", "hook_emphasis")


def _edit_of(edp: dict[str, Any]) -> dict[str, Any]:
    """The EDIT an EDP describes, without what a re-rank is allowed to touch."""
    core = {k: v for k, v in edp.items() if k not in ("id", "created", "compiled", "content_brain", "score", "graph")}
    out = []
    for d in core.get("decisions", []):
        d = {k: v for k, v in d.items() if k != "by"}
        if d["reason"]["code"] in _HOOK_CODES:
            d = {**d, "score": None, "reason": {**d["reason"], "text": None}}
        out.append(d)
    return {**core, "decisions": out}


def _exact(edp: dict[str, Any]) -> str:
    """Everything but the clock and the file's own name (`created`, and `compiled`/`score` filled later)."""
    return json.dumps({k: v for k, v in edp.items() if k not in ("created", "compiled", "score")}, sort_keys=True)


def _fm_available() -> str | None:
    try:
        from video_ai_editor.agent.prompt.brains import fm
        rep = fm.FMBrain().availability()
    except Exception as e:  # noqa: BLE001
        return f"FM helper not importable: {e}"
    return None if rep.get("available") else f"FM helper unavailable: {rep.get('detail')}"


@pytest.fixture(scope="module")
def rung_session(slice_env, fixtures):
    """ONE session for the three rungs: source keys name the uploaded FILE,
    so only plans over the same upload can be compared byte for byte. The
    plans are previewed, never applied — the timeline stays the upload's."""
    why = _fm_available()
    if why:
        pytest.skip(why)
    env = slice_env
    sid = env.new_session("EB1 TH rungs")
    env.upload_video(sid, Path(fixtures.th.video_16x9))
    L.transcribe(env, sid)
    return sid


def _planned(env: Any, sid: str, brain: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Analyse and plan the TH demo under `VAI_BRAIN=<brain>` (the analysis
    reads the pin: `fm` lets the Gateway ask Apple Intelligence), decline
    the card, and return (the frozen EDP, the card's brain payload)."""
    from video_ai_editor.brain import store as B
    mp = pytest.MonkeyPatch()
    mp.setenv("VAI_BRAIN", brain)
    try:
        L.analyse(env, sid)
        before = M.load_edl(env.session_dir(sid)).hash()
        run = env.run_prompt(sid, L.TH_PROMPT, brain=brain, answers={"apply": "no"})
        assert not run.errors, run.errors
        assert M.load_edl(env.session_dir(sid)).hash() == before, "a declined card changed the timeline"
        card = (L.preview_of(run) or {}).get("brain")
        assert isinstance(card, dict), "no brain card under this rung"
        edp = B.read_edp(env.session_dir(sid), card["decisions_id"])
        return json.loads(edp.model_dump_json()), card
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def recipes_plan(slice_env, rung_session):
    edp, _card = _planned(slice_env, rung_session, "recipes")
    assert edp["content_brain"] is None and all(d["by"] == "recipes" for d in edp["decisions"])
    return edp


def test_a_timed_out_fm_is_byte_equal_to_recipes(slice_env, rung_session, recipes_plan, monkeypatch):
    # runs BEFORE the live rung: a model's answer is frozen in the semantic
    # layer and never re-asked, so after a live answer nothing would time out
    from video_ai_editor.agent.prompt.brains import fm
    asked: list[str] = []

    def _silent(self, task, timeout_s=None):
        asked.append(str(getattr(task, "kind", "")))
        return None                                        # what a timed-out helper hands back
    monkeypatch.setattr(fm.FMBrain, "text", _silent)
    stubbed, _card = _planned(slice_env, rung_session, "fm")
    assert "rank_windows" in asked, f"the Gateway never asked the model: {asked}"
    assert _exact(stubbed) == _exact(recipes_plan)
    _say(f"fm timed out after {len(asked)} call(s) — EDP {stubbed['id']} is the recipes EDP {recipes_plan['id']}")


def test_fm_rerank_changes_only_provenance(slice_env, fixtures, rung_session, recipes_plan):
    live, card = _planned(slice_env, rung_session, "fm")
    assert live["content_brain"] == "apple_intelligence", live["content_brain"]
    ranked = [d for d in live["decisions"] if d["by"] == "apple_intelligence"]
    assert ranked and all(d["reason"]["code"] in _HOOK_CODES for d in ranked), [d["reason"]["code"] for d in ranked]
    assert live["summary"]["hook"]["sent"] == recipes_plan["summary"]["hook"]["sent"] == fixtures.th.truth.quotable
    assert live["graph"]["id"] != recipes_plan["graph"]["id"], "the model's answer is not frozen in the graph"
    assert _edit_of(live) == _edit_of(recipes_plan), "Apple Intelligence changed more than the provenance"
    assert "Apple Intelligence" in json.dumps(card), "the card does not say who ranked the moments"
    _say(f"fm live — content_brain {live['content_brain']}; decisions by it "
          f"{[(d['id'], d['reason']['code'], d['score']) for d in ranked]}; recipes scores "
          f"{[(d['id'], d['score']) for d in recipes_plan['decisions'] if d['reason']['code'] in _HOOK_CODES]}")


# --------------------------------------------------------------------------
# 4. the measurements can FAIL (SC-17): each helper against a case built to trip it
# --------------------------------------------------------------------------

def _clip(start: float, dur: float, scale: Any = 1.0, src: str = "/a.mp4", in_: float = 0.0):
    from video_ai_editor.edl.schema import Clip, Keyframe, Transform
    if isinstance(scale, list):
        scale = Keyframe(keyframes=scale, interp="step")
    return Clip(src=src, **{"in": in_}, out=in_ + dur, start=start, transform=Transform(scale=scale))


def test_hidden_by_needs_the_zoom_to_change_not_merely_a_key():
    from video_ai_editor.edl.schema import Keyframe
    a = _clip(0, 4)
    assert L.hidden_by(a, _clip(4, 4, [(0.0, 1.0)])) is None                 # a key at 0 that repeats the outgoing scale
    assert L.hidden_by(a, _clip(4, 4, [(0.0, 1.08)])) == "scale_step"
    punched = _clip(0, 4, [(0.0, 1.0), (1.0, 1.10)])
    assert L.hidden_by(punched, _clip(4, 4, [(0.0, 1.08)])) is None          # 1.10 -> 1.08 is 2 %
    assert L.hidden_by(punched, _clip(4, 4)) == "scale_step"
    assert L.hidden_by(a, _clip(4, 4, src="/b.mp4")) == "angle"
    assert isinstance(_clip(0, 1, [(0.0, 1.0), (1.0, 1.1)]).transform.scale, Keyframe)


def test_punch_keys_mismatch_catches_a_wrong_mode_and_a_missing_key():
    from video_ai_editor.edl.schema import Clip, Keyframe, Transform
    decision = {"params": {"interp": "ease-out", "keys": [{"t": 2.0, "values": {"scale": 1.0}}, {"t": 2.4, "values": {"scale": 1.1}}]}}
    good = Clip(src="/a.mp4", **{"in": 1.0}, out=6.0, start=0.0, transform=Transform(
        scale=Keyframe(keyframes=[(1.0, 1.0), (1.4, 1.1)], interp="ease-out")))
    assert L.punch_keys_mismatch(good, decision) == []
    wrong_mode = good.model_copy(update={"transform": Transform(scale=Keyframe(keyframes=[(1.0, 1.0), (1.4, 1.1)], interp="linear"))})
    assert any("interp" in m for m in L.punch_keys_mismatch(wrong_mode, decision))       # the old line accepted this
    late = good.model_copy(update={"transform": Transform(scale=Keyframe(keyframes=[(1.5, 1.0), (1.9, 1.1)], interp="ease-out"))})
    assert L.punch_keys_mismatch(late, decision), "a push that starts half a second late passed"
    assert L.punch_keys_mismatch(_clip(0, 5), decision) == ["no scale keyframes on the clip"]


def test_punch_push_reads_the_push_not_the_hide_key_in_front_of_it():
    from video_ai_editor.edl.schema import Keyframe
    kf = lambda rows: Keyframe(keyframes=rows, interp="ease-out")  # noqa: E731
    assert L.punch_push(kf([(0.23, 1.0), (0.63, 1.12)])) == (0.23, 1.0, 0.63, 1.12)                # the hook: a plain push
    hide_then_punch = kf([(0.0, 1.08), (0.0334, 1.08), (1.7633, 1.08), (2.1633, 1.10)])            # the reel's piece 4
    assert L.punch_push(hide_then_punch) == (1.7633, 1.08, 2.1633, 1.10)                            # not (0.0, ..): the flat hide keys are no push
    assert L.punch_in_point(hide_then_punch) == (1.7633, 1.10)
    assert L.punch_push(kf([(0.0, 1.08), (0.0667, 1.08)])) is None                                 # a hide alone is not a punch-in
    assert L.punch_push(kf([(0.5, 1.0), (0.9, 1.1), (3.0, 1.1), (3.4, 1.0)])) == (0.5, 1.0, 0.9, 1.1)   # a timed release still reads its push
    assert L.punch_push(hide_then_punch)[3] - L.punch_push(hide_then_punch)[1] < L.HIDE_MIN_STEP       # 2 %: the slice's "visible push" bar rejects it


def _strip(width: int, k: float, zoom: float, cells: int = 5) -> np.ndarray:
    """One decoded bar-code row: `cells` lit cells (band pitch 20 units) about the centre at `zoom`, edges anti-aliased
    the way an encoded frame's are (a pixel holds the share of it the cell covers)."""
    row = np.zeros(width, dtype=np.float32)
    px = np.arange(width, dtype=np.float64)
    for b in range(cells):
        cx = width / 2 + (b - cells // 2) * 20.0 * k * zoom
        x0, x1 = cx - 5.0 * k * zoom, cx + 5.0 * k * zoom
        row = np.maximum(row, np.clip(np.minimum(px + 1, x1) - np.maximum(px, x0), 0.0, 1.0).astype(np.float32))
    return row


def test_the_bar_code_reader_tells_1_00_from_1_08_and_says_nothing_when_it_cannot_read():
    for k in (2.0, 3.5556, 6.0):
        w = int(320 * k)
        z1, z8 = L.zoom_of_row(_strip(w, k, 1.0), k), L.zoom_of_row(_strip(w, k, 1.08), k)
        assert abs(z1 - 1.0) < 0.02 and abs(z8 - 1.08) < 0.02, (k, z1, z8)
        assert z8 - z1 >= L.HIDE_MIN_STEP, (k, z1, z8)
    assert L.zoom_of_row(np.ones(640, dtype=np.float32), 2.0) is None              # a flash frame
    assert L.zoom_of_row(np.zeros(640, dtype=np.float32), 2.0) is None            # a run of zero bits


def test_a_seam_with_no_zoom_change_in_the_decoded_export_is_reported():
    a, b, c = _clip(0, 2), _clip(2, 2), _clip(4, 2, src="/b.mp4")
    scales = [1.0] * 4 + [1.08] * 4 + [1.08] * 4                  # 2 fps: a|b steps 1.0 -> 1.08, b|c changes angle
    assert L.unseen_seams(scales, 2.0, [(a, b, 1.0), (b, c, 1.0)]) == []
    flat = [1.0] * 12
    assert [r[0] for r in L.unseen_seams(flat, 2.0, [(a, b, 1.0), (b, c, 1.0)])] == [2.0]      # the angle change is not measured here
    assert L.unseen_seams([1.0] * 4 + [None] * 4 + [1.0] * 4, 2.0, [(a, b, 1.0)])[0][3] is None   # unreadable is a failure, not a pass


def _edl_with_captions(rows: list[tuple[float, float, str]], a1: list[tuple[float, float, float]] | None = None):
    from video_ai_editor.edl.schema import TextClip, empty_edl
    edl = empty_edl()
    edl.get_track("captions").clips.extend(TextClip(text=t, start=s, end=e, role="caption") for s, e, t in rows)
    for start, in_, dur in a1 or []:
        edl.get_track("a1").clips.append(_clip(start, dur, in_=in_, src="/ref.wav"))
    return edl


def test_the_caption_checks_flag_a_removed_filler_a_cue_over_a_cut_and_a_cue_over_silence():
    edl = _edl_with_captions([(0.0, 1.0, "So we start"), (1.0, 1.1, "Um,"), (1.1, 2.0, "small work?"), (5.0, 6.0, "floats over a pause")],
                             a1=[(0.0, 10.0, 1.5), (1.5, 20.0, 2.5)])          # a1 jumps 10.0 s of the reference to 20.0 s at 1.5
    assert [t for _s, t in L.caption_filler_cues(edl)] == ["Um,"]
    assert L.hard_seams(edl) == [1.5]
    assert [c[2] for c in L.cues_across(edl, L.hard_seams(edl))] == ["small work?"]
    assert [c[2] for c in L.floating_cues(edl, [(0.0, 4.0)])] == ["floats over a pause"]
    clean = _edl_with_captions([(0.0, 1.5, "So we start"), (1.5, 3.0, "a new sentence")], a1=[(0.0, 10.0, 1.5), (1.5, 20.0, 2.5)])
    assert L.caption_filler_cues(clean) == [] and L.cues_across(clean, L.hard_seams(clean)) == [] and L.floating_cues(clean, [(0.0, 4.0)]) == []


def test_a_dropped_hide_key_is_reported():
    edl = _edl_with_captions([], a1=[(0.0, 10.0, 8.0)])
    edl.get_track("v1").clips.extend([_clip(0.0, 4.0, src="/a.mp4"), _clip(4.0, 4.0, [(0.0, 1.08)], src="/a.mp4", in_=6.0)])
    edp = {"summary": {"dialogue": {"offsets": {"src_x": 0.0}}},
           "decisions": [{"id": "k_1", "kind": "jump_cut_hide", "ref": {"src": "src_x"}, "params": {"piece": [14.0, 18.0]}},   # ref 14 -> timeline 4
                         {"id": "k_2", "kind": "jump_cut_hide", "ref": {"src": "src_x"}, "params": {"piece": [90.0, 94.0]}}]}
    # a1 plays reference 10-18 from timeline 0-8; the piece at timeline 4 carries a key, the second decision's moment was cut away
    assert L.hide_status(edp, edl, "/ref.wav") == [("k_1", "ok"), ("k_2", "no piece")]
    edl.get_track("v1").clips[1].transform = edl.get_track("v1").clips[1].transform.model_copy(update={"scale": 1.0})
    assert L.hide_status(edp, edl, "/ref.wav")[0] == ("k_1", "no keys")


def test_the_flag_off_signature_reads_routing_and_words_not_numbers():
    ev = [{"type": "plan", "plan": {"intent": "auto_edit", "steps": [{"tool": t} for t in L.FLAG_OFF_080["make a 45-second reel"][1].split(",")]}},
          {"type": "clarify", "questions": [{"key": "apply"}]},
          {"type": "text_delta", "text": "via Recipes — Preview — Auto edit: 21 changes. Nothing has changed yet.\n- Deleted"}]
    assert L.phrase_signature(ev) == L.FLAG_OFF_080["make a 45-second reel"]
    ev[0]["plan"]["intent"] = "tighten+edit"
    assert L.phrase_signature(ev) != L.FLAG_OFF_080["make a 45-second reel"]
    ev[0]["plan"]["intent"] = "auto_edit"
    ev[2]["text"] = "via Recipes — the Editor Brain is off; did the standard edit"
    assert L.phrase_signature(ev) != L.FLAG_OFF_080["make a 45-second reel"]
    assert len(L.FLAG_OFF_080) == 24
