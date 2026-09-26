"""QA-037 (grammar slice): "reverse the clip" in the Prompt bar.

Rendering, the Properties toggle and chat/MCP `set_property` already played a
clip backwards; the prompt answered "I did not catch that" because the
grammar had no reverse intent — and `set_property` is on the plan deny list
(its `src` path bypasses the upload guard), so a plan could not reach the
flag at all. The recipe now plans the narrow `set_clip_reverse` tool, the
verifier reads the flag back, and the end-to-end test decodes a real render:
the first frame of a reversed luma ramp is the source's LAST frame.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import grammar as G  # noqa: E402
from video_ai_editor.agent.prompt import planner as P  # noqa: E402
from video_ai_editor.agent.prompt import schema as Sc  # noqa: E402
from video_ai_editor.agent.prompt import service  # noqa: E402
from video_ai_editor.agent.prompt import validate as V  # noqa: E402
from video_ai_editor.agent.prompt.facts import TimelineFacts  # noqa: E402

FM = TimelineFacts.minimal(session_id="s_rev", v1_clip_ids=["c_a", "c_b"], clip_ids=["c_a", "c_b"],
                           track_ids=["v1"], v1_boundaries=[6.0])


def _rev_steps(p: Sc.Plan) -> list[dict]:
    return [s.args for s in p.steps if s.tool == "set_clip_reverse"]


@pytest.mark.parametrize("prompt,clip,rev", [
    ("reverse the clip", "$v1_all", True),
    ("play it backwards", "$v1_all", True),
    ("play the last clip backwards", "$v1_last", True),
    ("reverse the first clip", "$v1_first", True),
    ("make a rewind effect", "$v1_all", True),
    ("ulta chala do", "$v1_all", True),
    ("play it forwards again", "$v1_all", False),
    ("unreverse the clip", "$v1_all", False),
    ("don't reverse it", "$v1_all", False),
])
def test_reverse_prompts_plan_the_reverse_tool(prompt, clip, rev):
    det = G.detect(prompt)
    assert det.intents == ["reverse"] and det.confidence >= G.RUN_THRESHOLD, (det.intents, det.confidence)
    p = P.plan(prompt, FM)
    assert p.intent == "reverse" and not p.blocking_questions, (p.intent, p.reply)
    assert _rev_steps(p) == [{"clip_id": clip, "reverse": rev}]
    out = V.validate_plan(p, FM)                 # the security boundary accepts it
    assert [pc.check for pc in out.postconditions] == ["clip_reversed"]


def test_the_selected_clip_is_the_default_target():
    p = P.plan("reverse the clip", FM.with_(selection="c_b"))
    assert _rev_steps(p) == [{"clip_id": "c_b", "reverse": True}]
    p = P.plan("reverse this clip", FM)          # names a selection, none exists
    assert _rev_steps(p) == [] and "select a clip" in (p.reply or "").lower()


@pytest.mark.parametrize("prompt", ["reverse that", "reverse the last edit", "reverse the order of the clips"])
def test_undo_and_reorder_wording_is_not_a_reverse(prompt):
    assert "reverse" not in G.detect(prompt).intents


def _ramp_clip(root: Path) -> Path:
    """12 s whose picture brightens linearly with time (luma ≈ 16 + 18·t)."""
    d = root / "uploads" / "talk"
    d.mkdir(parents=True, exist_ok=True)
    src = d / "talk.normalized.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c=black:s=320x180:d={F.CLIP_DUR}:r=30",
                    "-f", "lavfi", "-i", f"sine=f=440:d={F.CLIP_DUR}:r=48000",
                    "-vf", "geq=lum='16+18*T':cb=128:cr=128",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True, capture_output=True)
    return src


def _luma(path: Path, t: float) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", "crop=iw/4:ih/8", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    assert raw
    return sum(raw) / len(raw)


@pytest.mark.usefixtures("no_downloads")
def test_reverse_the_clip_end_to_end_plays_backwards_in_a_real_render(tmp_path, desktop_posture, monkeypatch):
    from video_ai_editor import storage as _storage
    from video_ai_editor.render.compositor import render_preview
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    src = _ramp_clip(tmp_path)
    store = F.make_store(tmp_path, src=src)
    assert _luma(src, 0.1) < 40 and _luma(src, F.CLIP_DUR - 0.2) > 200

    events = F.collect(service.prompt_turn(store, "reverse the clip", [], brain="recipes"))
    assert events[-1]["type"] == "done", events[-3:]
    assert not [e for e in events if e["type"] == "error"]
    v = next(e for e in events if e["type"] == "verify")
    assert v["passed"] == v["total"] >= 1, v
    clip = store.edl.get_track("v1").clips[0]
    assert clip.reverse is True
    assert store.ops.last().tool == "prompt"      # one undo step

    out = Path(render_preview(store.edl, Path(store.dir), height=180).path)
    first, last = _luma(out, 0.05), _luma(out, F.CLIP_DUR - 0.3)
    assert first > 200 and last < 50, (first, last)   # starts on the source's last frame

    events = F.collect(service.prompt_turn(store, "play it forwards again", [], brain="recipes"))
    assert next(e for e in events if e["type"] == "verify")["passed"] >= 1
    assert store.edl.get_track("v1").clips[0].reverse is False


def test_set_clip_reverse_toggles_and_records_one_step(tmp_path, desktop_posture):
    store = F.make_store(tmp_path)
    cid = store.edl.get_track("v1").clips[0].id
    assert dispatch(store, "set_clip_reverse", {"clip_id": cid})["reverse"] is True
    assert dispatch(store, "set_clip_reverse", {"clip_id": cid, "reverse": False})["reverse"] is False
    with pytest.raises(ValueError):
        dispatch(store, "set_clip_reverse", {"clip_id": "c_nope"})
