"""QA-068: "make 3 shorts" picks distinct, complete, strong moments.

It returned three back-to-back windows (11.1-23.83, 23.83-38.3, 38.3-51.03)
cut mid-sentence, with the fillers and 2 s pauses kept and fragment hooks.
The source here is real media (ffmpeg-made, its loudness following the speech)
with a transcript whose sentences, fillers, pauses and questions are known, so
every claim is checked against ground truth.
"""
from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu

# (start, text) — each word 0.3 s, 0.1 s apart; sentences separated by 0.4 s
# unless a gap is given. Section B is weak: fillers, dead air, "and/so" starts.
_SCRIPT: list[tuple[str, float]] = [
    ("Have you ever wondered why cameras still matter?", 0.4),
    ("The sensor decides almost everything about the picture.", 0.4),
    ("Light is the raw material of every single frame.", 0.4),
    ("Bigger pixels catch more of it in the dark.", 0.4),
    ("That is why phones struggle at night.", 2.0),
    ("And um so the uh thing is the grip.", 2.2),
    ("So um it has uh a jacket pocket and the grip.", 2.5),
    ("And uh yeah um the strap is fine I guess.", 2.0),
    ("What makes a lens worth the money?", 0.4),
    ("Sharp glass keeps detail right into the corners.", 0.4),
    ("Fast apertures let you shoot handheld after sunset.", 0.4),
    ("Good coatings stop flare from washing out colour.", 0.4),
    ("Those three things are what you pay for.", 0.9),
    ("Here is the honest verdict on this camera.", 0.4),
    ("It is the best travel camera I have used.", 0.4),
    ("The battery lasts a full day of shooting.", 0.4),
    ("The autofocus locks on eyes instantly.", 0.4),
    ("I would buy it again tomorrow.", 0.4),
]


def _transcript() -> tuple[dict, list[tuple[float, float, str]]]:
    t = 0.5
    segs, sentences = [], []
    for i, (text, gap_after) in enumerate(_SCRIPT):
        words = []
        for tok in text.split():
            words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "word": tok})
            t += 0.4
        s0, s1 = words[0]["start"], words[-1]["end"]
        segs.append({"id": i, "start": s0, "end": s1, "text": text, "words": words})
        sentences.append((s0, s1, text))
        t = s1 + gap_after
    return {"language": "en", "duration": t + 1.0, "segments": segs}, sentences


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> tuple[Path, dict, list]:
    tx, sentences = _transcript()
    d = tmp_path_factory.mktemp("shorts")
    # Tone only while "speaking", silence in the pauses — so loudness really
    # follows the speech the transcript describes.
    expr = "+".join(f"between(t,{a},{b})" for a, b, _ in sentences)
    dur = tx["duration"]
    src = d / "talk.mp4"
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}",
                    "-filter_complex", f"[1:a]volume='if({expr},1,0)':eval=frame[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    return src, tx, sentences


def _inside(sentences, a: float, b: float):
    return [s for s in sentences if s[0] >= a - 1e-6 and s[1] <= b + 1e-6]


def test_shorts_are_whole_sentences_distinct_and_hooked_by_a_sentence(source, tmp_path):
    from video_ai_editor.ai.shorts import make_shorts
    src, tx, sentences = source
    shorts = make_shorts(src, tx, tmp_path, target_count=3, max_dur=30.0, min_dur=8.0)
    assert len(shorts) == 3
    for r in shorts:
        a, b = r["start"], r["end"]
        assert 8.0 <= b - a <= 30.5
        # Never mid-sentence: no sentence straddles either cut.
        for s0, s1, text in sentences:
            assert not (s0 < a < s1), f"starts inside {text!r}"
            assert not (s0 < b < s1), f"ends inside {text!r}"
        inside = _inside(sentences, a, b)
        assert inside, r
        # The hook is one of the short's own sentences, whole, filler-free.
        texts = [t for _, _, t in inside]
        assert r["hook"] in texts, (r["hook"], texts)
        assert r["hook"].split()[0].lower() not in {"and", "so", "but", "um", "uh"}
    # Distinct: at least one whole sentence between any two shorts.
    for x, y in zip(shorts, shorts[1:]):
        between = _inside(sentences, x["end"], y["start"])
        assert between, f"{x['start']}-{x['end']} touches {y['start']}-{y['end']}"
    # The mumbled section (fillers + dead air) is never chosen.
    weak = [s for s in sentences if " um " in f" {s[2]} " or " uh " in f" {s[2]} "]
    for r in shorts:
        assert not _inside(weak, r["start"], r["end"]), r
    # Questions make the strongest openers.
    assert {"Have you ever wondered why cameras still matter?",
            "What makes a lens worth the money?"} <= {r["hook"] for r in shorts}


def test_make_shorts_sessions_keep_the_rate_and_carry_the_hook(source, tmp_path, monkeypatch):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    from fastapi.testclient import TestClient
    client = TestClient(_main.app)
    src, tx, _ = source
    sid = client.post("/api/sessions").json()["id"]
    with src.open("rb") as f:
        assert client.post(f"/api/sessions/{sid}/upload", files={"file": ("talk.mp4", f, "video/mp4")},
                           data={"transcribe": "false"}).status_code == 200
    (_main.session_dir(sid) / "transcript.json").write_text(json.dumps(tx), encoding="utf-8")
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "make_shorts", "args": {
        "target_count": 3, "max_dur": 30, "min_dur": 8, "save_as_sessions": True}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert len(res["new_sessions"]) == 3
    fps = client.get(f"/api/sessions/{sid}/edl").json()["canvas"]["fps"]
    for short, child in zip(res["shorts"], res["new_sessions"]):
        meta = json.loads((_main.session_dir(child) / "meta.json").read_text(encoding="utf-8"))
        assert meta["hook"] == short["hook"] and meta["hook"]
        edl = json.loads((_main.session_dir(child) / "edl.json").read_text(encoding="utf-8"))
        assert edl["canvas"]["fps"] == fps
        [clip] = [c for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"]]
        assert clip["in"] == pytest.approx(short["source_start"], abs=1 / fps)


def test_each_short_is_tightened_and_hooked_with_its_own_line(source, tmp_path, monkeypatch):
    """The finishing pass (executor._finish_children) plans tighten → reframe
    → captions → hook, with the hook text the short carries."""
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor.agent.prompt import executor as ex
    from video_ai_editor.agent.prompt.schema import Plan
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import Clip, empty_edl
    src, tx, _ = source
    child = _storage.new_session_id()
    sd = _storage.session_dir(child)
    edl = empty_edl()
    edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=10.0, start=0.0))
    edl.recompute_duration()
    (sd / "edl.json").write_text(edl.to_json(), encoding="utf-8")
    (sd / "transcript.json").write_text(json.dumps(tx), encoding="utf-8")
    (sd / "meta.json").write_text(json.dumps({"name": "talk short 1",
                                              "hook": "What makes a lens worth the money?"}))
    captured = []

    def fake_run_plan(store, plan, facts, **kw):
        captured.append(plan)
        return ex.ExecResult(plan=plan, steps=[], edl_before=store.edl, duration_before=0.0)
    monkeypatch.setattr(ex, "run_plan", fake_run_plan)
    parent_plan = Plan(version=1, intent="shorts", steps=[], needs_input=[], postconditions=[],
                       confidence=1.0, brain="recipes")
    parent = ex.ExecResult(plan=parent_plan, steps=[], edl_before=edl, duration_before=0.0,
                           new_sessions=[child])
    import threading
    ex._finish_children(lambda s: EDLStore(_storage.session_dir(s)), parent, None,
                        emit=lambda e: None, cancel_event=threading.Event(), prompt="make shorts")
    assert parent.child_runs and parent.child_runs[0]["status"] == "ok", parent.child_runs
    [plan] = captured
    tools = [s.tool for s in plan.steps]
    assert "remove_fillers" in tools or "remove_silences" in tools, tools
    hook = [s for s in plan.steps if s.tool == "apply_hook_stack"]
    assert hook and hook[0].args["text"] == "What makes a lens worth the money?"


def _seg(i, start, text, pause_before_last=0.0):
    t, words = start, []
    toks = text.split()
    for k, tok in enumerate(toks):
        if k == len(toks) - 1:
            t += pause_before_last
        words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "word": tok})
        t += 0.4
    return {"id": i, "start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words}


def test_hooks_from_real_whisper_shapes_are_whole_lines():
    """Shapes taken from the real 85 s narration's whisper transcript: a
    sentence whisper split across two segments with a pause before its last
    word ("…an external" / "one."), fillers and connectives up front, and a
    short with no sentence short enough, whose opening clause is a question."""
    from video_ai_editor.ai.shorts import _hook_line, _sentences
    tx = {"segments": [
        _seg(0, 49.0, "The microphone is fine for a quick clip, but for anything serious you "
                      "will want an external one.", pause_before_last=1.0),
        _seg(1, 60.0, "Um, that is the review, let me know what you think and thanks for watching."),
    ]}
    sents = _sentences(tx)
    assert [len(s.tokens) for s in sents] == [18, 15], "a mid-sentence pause is not a sentence end"
    # "that" is the subject here, not a connective to trim; the line is its
    # opening clause, since the whole sentence is too long for a hook.
    assert _hook_line(sents[1:]) == "That is the review"
    tx2 = {"segments": [_seg(0, 70.0, "Um, so who is this for, travel vloggers, and anyone who wants "
                                       "a real camera without carrying a bag.")]}
    assert _hook_line(_sentences(tx2)) == "Who is this for?"
