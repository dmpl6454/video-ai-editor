"""C2-panels lane regressions (wave C of the pro-editor QA sweep).

Backend halves of panel defects; the UI halves are in tests/test_c2_panels_ui.py
(real browser) and the frontend vitest suites named in each docstring.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient pinned at a tmp WORKDIR — never the owner's real projects."""
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", tmp_path / "wd", raising=False)
    (tmp_path / "wd").mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return TestClient(_main.app)


def _png(path: Path) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:size=64x64:duration=0.04", "-frames:v", "1", str(path)], check=True)
    return path


def test_sticker_upload_answers_the_new_stickers_id(client, tmp_path):
    """QA-128: an uploaded PNG dropped at the playhead comes back with the id of
    the sticker it created, so the picker can select it (add_sticker already
    answers `sticker_id`; the upload route threw it away)."""
    c = client
    sid = c.post("/api/sessions", json={"name": "c2"}).json()["id"]
    png = _png(tmp_path / "badge.png")
    with png.open("rb") as fh:
        r = c.post(f"/api/sessions/{sid}/sticker_upload", files={"file": ("badge.png", fh, "image/png")},
                   data={"add_at_playhead": "true", "playhead": "0"})
    assert r.status_code == 200, r.text
    body = r.json()
    edl = c.get(f"/api/sessions/{sid}/edl").json()
    ids = [cl["id"] for t in edl["tracks"] if t["id"] == "stickers" for cl in t["clips"]]
    assert body.get("sticker_id") in ids and len(ids) == 1, (body, ids)
    # Upload only (no drop): nothing created, no id.
    with png.open("rb") as fh:
        r2 = c.post(f"/api/sessions/{sid}/sticker_upload", files={"file": ("badge.png", fh, "image/png")},
                    data={"add_at_playhead": "false"})
    assert r2.status_code == 200 and "sticker_id" not in r2.json()


def test_an_over_long_prompt_is_refused_on_the_message_field(client):
    """QA-124: the Prompt bar (lib/promptLimit.ts) mirrors this limit and reads
    this refusal — pin both halves of that contract: 4000 is the limit, and a
    longer prompt is a 422 naming `message` as `string_too_long` with the limit
    in ctx.max_length."""
    c = client
    sid = c.post("/api/sessions", json={"name": "c2"}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/prompt", json={"message": "a" * 4001})
    assert r.status_code == 422
    [d] = r.json()["error"]["details"]
    assert d["loc"] == ["body", "message"] and d["type"] == "string_too_long"
    assert d["ctx"]["max_length"] == 4000
    ts = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "promptLimit.ts").read_text()
    assert re.search(r"PROMPT_MAX_CHARS = 4000\b", ts)


# ---- QA-068: shorts are named, and each gets an Open action -----------------

_LINES = [
    "Have you ever wondered why cameras still matter?",
    "The sensor decides almost everything about the picture.",
    "Light is the raw material of every single frame.",
    "Bigger pixels catch more of it in the dark.",
    "That is why phones struggle at night.",
    "What makes a lens worth the money?",
    "Sharp glass keeps detail right into the corners.",
    "Fast apertures let you shoot handheld after sunset.",
    "Good coatings stop flare from washing out colour.",
    "Those three things are what you pay for.",
    "Here is the honest verdict on this camera.",
    "It is the best travel camera I have used.",
    "The battery lasts a full day of shooting.",
    "I would buy it again tomorrow.",
]


def _talk(tmp_path: Path) -> tuple[Path, dict]:
    t, segs = 0.5, []
    for i, text in enumerate(_LINES):
        words = []
        for tok in text.split():
            words.append({"start": round(t, 3), "end": round(t + 0.3, 3), "word": tok})
            t += 0.4
        segs.append({"id": i, "start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words})
        t = words[-1]["end"] + 1.2
    dur = t + 1.0
    src = tmp_path / "talk.mp4"
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
    return src, {"language": "en", "duration": dur, "segments": segs}


def test_created_shorts_are_named_in_the_reply_and_carry_their_name_for_open(tmp_path, monkeypatch):
    """QA-068 (remainder): the finished run listed raw 'Created sessions:
    s_…' ids. The finishing pass now records each short's NAME with its
    session id (the run log's Open buttons read those `finish_short` records),
    and the reply names the shorts — no `s_` id anywhere in it."""
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    from fastapi.testclient import TestClient
    client = TestClient(_main.app)
    src, tx = _talk(tmp_path)
    sid = client.post("/api/sessions").json()["id"]
    with src.open("rb") as f:
        assert client.post(f"/api/sessions/{sid}/upload", files={"file": ("talk.mp4", f, "video/mp4")},
                           data={"transcribe": "false"}).status_code == 200
    (_main.session_dir(sid) / "transcript.json").write_text(json.dumps(tx), encoding="utf-8")
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "make_shorts", "args": {
        "target_count": 2, "max_dur": 20, "min_dur": 6, "save_as_sessions": True}})
    assert r.status_code == 200, r.text
    children = r.json()["result"]["new_sessions"]
    assert len(children) == 2

    from video_ai_editor.agent.prompt import executor as ex
    from video_ai_editor.agent.prompt.schema import Plan
    from video_ai_editor.agent.prompt.summary import compose_reply
    from video_ai_editor.edl import EDLStore

    def fake_run_plan(store, plan, facts, **kw):   # the finish itself is B8's test
        return ex.ExecResult(plan=plan, steps=[], edl_before=store.edl, duration_before=0.0)
    monkeypatch.setattr(ex, "run_plan", fake_run_plan)
    plan = Plan(version=1, intent="shorts", title="3 shorts", steps=[], needs_input=[], postconditions=[],
                confidence=1.0, brain="recipes")
    store = EDLStore(_storage.session_dir(sid))
    parent = ex.ExecResult(plan=plan, steps=[], edl_before=store.edl, duration_before=0.0, new_sessions=children)
    events: list[dict] = []
    ex._finish_children(lambda s: EDLStore(_storage.session_dir(s)), parent, None,
                        emit=events.append, cancel_event=threading.Event(), prompt="make 2 shorts")
    results = [e for e in events if e["type"] == "tool_result" and e["name"] == "finish_short"]
    names = [json.loads((_storage.session_dir(c) / "meta.json").read_text())["name"] for c in children]
    assert [(e["result"]["session"], e["result"]["name"], e["result"]["status"]) for e in results] == \
        [(c, n, "ok") for c, n in zip(children, names)]

    # The reply as the run ends (compose_reply over a finished parent whose
    # one step was the make_shorts dispatch above).
    parent.steps = [ex.StepOutcome(index=0, tool="make_shorts", args=[{}], status="ok",
                                   results=[r.json()["result"]])]
    reply = compose_reply(plan, parent, None)
    assert "2 shorts ready: " + " · ".join(names) + "." in reply, reply
    assert not re.search(r"\bs_[0-9a-f]{6,}", reply), reply
    # Without the finishing records (a shorts run nobody finished) the names
    # are read from the projects themselves.
    parent.child_runs = []
    assert " · ".join(names) in compose_reply(plan, parent, None)
    # The failure path's "kept" note names them too.
    kept = ex._with_kept_sessions("Cancelled — timeline unchanged.", children)
    assert " · ".join(names) in kept and not re.search(r"\bs_[0-9a-f]{6,}", kept), kept
