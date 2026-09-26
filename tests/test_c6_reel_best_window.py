"""QA-069 (wave C remainder): "make this a 30s reel" keeps the BEST 30 s.

Wave B made the reel end on the last sentence that fits, but it still kept
the FIRST 30 s whatever they held. The target-length trim is now a
`$fit_best:<s>` sentinel resolved on the live timeline (after the plan's own
silence/filler cuts): every run of whole sentences that fits is scored with
ai/shorts' own window score — speech density, a strong opening line, a
finished last sentence, length fit, dead air and fillers penalised — and the
best one is kept by a tail cut and a head cut inside ONE step (one undo).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.dispatch import _ripple_close_gap  # noqa: E402
from video_ai_editor.agent.prompt import live  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl.schema import Clip  # noqa: E402

#: 60 s: a rambling, filler-heavy, gappy first half and a clean second half
#: that opens on a question.
WEAK_THEN_STRONG = [
    (0.4, 6.0, "so um and uh I was thinking maybe we could"),
    (8.5, 13.0, "and like uh you know it is um kind of"),
    (16.0, 21.0, "but so anyway um where was I uh yes"),
    (24.0, 28.5, "and then um it sort of just uh worked"),
    (31.0, 36.0, "Why does this little camera beat my phone every single time?"),
    (36.3, 42.0, "It focuses in a blink and holds a face across the whole frame."),
    (42.3, 48.0, "The battery lasts a full day of shooting without a spare."),
    (48.3, 54.0, "The screen flips out so you can frame yourself while you record."),
    (54.3, 59.5, "That is why it lives in my jacket pocket now."),
]


def _store(root: Path, dur: float) -> EDLStore:
    s = EDLStore(root)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_0", src="/x/0.mp4", in_=0.0, out=dur, start=0.0))
    _ripple_close_gap(v1, s.edl.canvas.fps)
    s.commit("seed", {}, "seed")
    return s


def _transcript(root: Path, segs, duration: float) -> None:
    out = []
    for i, (st, en, text) in enumerate(segs):
        toks = text.split()
        step = (en - st) / len(toks)
        words = [{"start": round(st + k * step, 3), "end": round(st + (k + 1) * step, 3), "word": " " + w}
                 for k, w in enumerate(toks)]
        out.append({"id": i, "start": st, "end": en, "text": text, "words": words})
    (root / "transcript.json").write_text(json.dumps({"segments": out, "language": "en",
                                                      "duration": duration}), encoding="utf-8")


def _run_reel(store: EDLStore, max_s: float):
    from video_ai_editor.agent.prompt import executor, planner
    from video_ai_editor.agent.prompt import slots as S
    from video_ai_editor.agent.prompt.facts import build_facts
    from video_ai_editor.agent.prompt.recipes import Intent
    f = build_facts(store, {}, feature_report={})
    it = Intent("trim", {"range": S.TimeRange(kind="abs", start=max_s, end=None),
                         "_optional": True, "_max_s": max_s})
    plan = planner.compose([it], f)
    events: list[dict] = []
    res = executor.run_plan(store, plan, f, emit=events.append, cancel_event=None,
                            prompt="make this a 30s reel", wait_transcript=False)
    assert res.error is None, res.error
    return res, events


def test_the_best_30s_is_kept_not_the_first(tmp_path):
    s = _store(tmp_path, 60.0)
    _transcript(tmp_path, WEAK_THEN_STRONG, 60.0)
    ops = len(s.ops.ops)
    res, events = _run_reel(s, 30.0)
    clips = sorted(s.edl.get_track("v1").clips, key=lambda c: c.start)
    assert len(clips) == 1
    kept_in, kept_out = clips[0].in_, clips[0].out
    # The question at 31.0 s opens the reel; the last sentence (ends 59.5 s) closes it.
    assert 30.6 <= kept_in <= 31.0, kept_in
    assert 59.5 <= kept_out <= 59.8, kept_out
    assert s.edl.video_extent() <= 30.0 + 1e-6
    assert len(s.ops.ops) == ops + 1                  # tail + head cut = one undo step
    notes = [n for st in res.steps for n in st.notices]
    assert any("best" in n and "opening" in n for n in notes), notes
    tool_use = next(e for e in events if e["type"] == "tool_use" and e["name"] == "cut_range")
    assert len(tool_use["args"]["ranges"]) == 2
    assert s.undo() and s.edl.video_extent() == pytest.approx(60.0)


def test_best_window_scores_the_strong_half_above_the_opening(tmp_path):
    s = _store(tmp_path, 60.0)
    _transcript(tmp_path, WEAK_THEN_STRONG, 60.0)
    cands = live._scored_windows(live._timeline_words(s), 30.0)
    opening = min(cands, key=lambda c: (c[1], -c[2]))
    best = max(cands, key=lambda c: c[0])
    assert best[0] > opening[0] + 0.2, (best, opening)
    assert all(e - st <= 30.0 + 1e-6 for _, st, e, _ in cands)


def test_without_a_transcript_the_opening_is_kept_and_said(tmp_path):
    s = _store(tmp_path, 60.0)
    bw = live.best_window(s, 30.0)
    assert isinstance(bw, live.FitCut) and bw.start == pytest.approx(30.0, abs=0.04)
    assert any("no transcript" in n for n in bw.notices)


def test_a_timeline_already_within_the_target_is_left_alone(tmp_path):
    s = _store(tmp_path, 20.0)
    out, notices = live.resolve_live_args(s, "cut_range", {"track": "v1", "start": "$fit_best:30", "end": 21})
    assert out is None and "within" in notices[0]


# ------------------------------------------------------------------ the 85 s narration

BENCH = Path(os.path.expanduser("~/Library/Caches/Video AI Editor/bench/8515fa4411c9"))


def _narration_transcript() -> tuple[dict, list[dict]]:
    """Word timing for the bench's Piper narration from its own manifest:
    words spread evenly over each utterance's voiced span (the fillers are
    their own one-word utterances, so remove_fillers finds them)."""
    man = json.loads((BENCH / "narration_en.json").read_text(encoding="utf-8"))
    segs, utts = [], []
    for u in man["utterances"]:
        if u["kind"] not in ("speech", "filler"):
            continue
        a, b = float(u["voiced_start"]), float(u["voiced_end"])
        toks = u["text"].split()
        step = (b - a) / len(toks)
        words = [{"start": round(a + k * step, 3), "end": round(a + (k + 1) * step, 3), "word": " " + w,
                  "prob": 1.0} for k, w in enumerate(toks)]
        segs.append({"id": len(segs), "start": a, "end": b, "text": u["text"], "words": words})
        if u["kind"] == "speech":
            utts.append(u)
    return {"language": "en", "duration": man["duration"], "segments": segs}, utts


@pytest.mark.skipif(not (BENCH / "scene_16x9.mp4").exists(), reason="bench media not on this machine")
@pytest.mark.usefixtures("no_downloads")
def test_make_this_a_30s_reel_on_the_85s_narration(tmp_path, desktop_posture, monkeypatch):
    """The QA repro, end to end through the real service: tighten, then the
    best 30 s. Measured: ≤ 30 s, starts on a sentence start and ends on a
    sentence end of the narration (source time), and the kept window scores
    at least as well as the opening one."""
    from video_ai_editor import storage as _storage
    from video_ai_editor.agent.prompt import service
    from video_ai_editor.render.compositor import render_preview
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    d = tmp_path / "uploads" / "scene"
    d.mkdir(parents=True)
    src = d / "scene.normalized.mp4"
    os.symlink(BENCH / "scene_16x9.mp4", src)
    tx, utts = _narration_transcript()
    F.write_ingest(src, tx)
    store = F.make_store(tmp_path, src=src)
    for c in store.edl.get_track("v1").clips:
        c.out = tx["duration"]
    store.commit("full", {}, "full length")

    events = F.collect(service.prompt_turn(store, "make this a 30s reel", [], brain="recipes"))
    assert events[-1]["type"] == "done", events[-3:]
    errors = [e for e in events if e["type"] == "error"]
    assert not errors, errors
    clips = sorted((c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)), key=lambda c: c.start)
    first_in, last_out = clips[0].in_, clips[-1].out
    extent = store.edl.video_extent()
    assert 18.0 <= extent <= 30.0 + 1e-6, extent
    # Sentence starts / ends of the narration, in source seconds.
    starts, ends = [], []
    for u in utts:
        a, b = float(u["voiced_start"]), float(u["voiced_end"])
        parts = [p for p in u["text"].replace("?", "?|").replace(". ", ".|").split("|") if p.strip()]
        n = sum(len(p.split()) for p in parts)
        t = a
        for p in parts:
            starts.append(t)
            t += (b - a) * len(p.split()) / n
            ends.append(t)
    # No word is cut: the reel starts in the gap before a sentence and ends in
    # the gap after one (source seconds, word timing ±50 ms).
    gaps = [(0.0, starts[0])] + list(zip(ends, starts[1:])) + [(ends[-1], tx["duration"])]
    assert any(a - 0.05 <= first_in <= b + 0.05 for a, b in gaps), (first_in, gaps)
    assert any(a - 0.05 <= last_out <= b + 0.05 for a, b in gaps), (last_out, gaps)
    step = next(e for e in events if e["type"] == "step" and e.get("tool") == "cut_range" and e.get("status") == "ok")
    assert "best" in step["summary"], step
    out = Path(render_preview(store.edl, Path(store.dir), height=180).path)
    got = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True, check=True).stdout)
    assert got == pytest.approx(extent, abs=0.15)
