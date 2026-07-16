"""Chat history reset + live-timeline grounding (R3).

Regression coverage for the "Claude describes the previous video" bug
(docs/superpowers/plans/2026-07-10-editor-issues-verification-and-fixes.md):
a new upload into an EMPTY timeline starts a brand-new project, but the whole
prior conversation used to be replayed to Claude verbatim on every turn, so it
kept answering from stale history about footage that's no longer present.

Two independent fixes are covered here:
  1. main.py's upload() clears chat.json when the upload lands on an empty v1
     (a genuinely new project) but PRESERVES history for a mid-project upload
     (e.g. adding b-roll to existing footage, where the old context is still
     relevant).
  2. agent/loop.py's `_live_context_block` computes a fresh, ground-truth
     timeline summary and folds it into the `system` prompt on every API
     call — so even if a model skips calling get_timeline, the current state
     is structurally present rather than purely a function of chat memory.
"""
from __future__ import annotations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.main import app, _history_path, _load_history, _save_history
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip, TextClip, Transform
from video_ai_editor.agent.loop import _live_context_block


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    return TestClient(app)


def _make_video(p: Path, *, dur: float = 1.0):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c=blue:s=320x180:d={dur}:r=30",
                    "-pix_fmt", "yuv420p", str(p)],
                   check=True, capture_output=True)


def _upload(client, sid, path, name="v.mp4"):
    with path.open("rb") as f:
        return client.post(
            f"/api/sessions/{sid}/upload",
            files={"file": (name, f, "video/mp4")},
            data={"add_to_timeline": "true", "transcribe": "false"},
        )


def test_upload_into_empty_timeline_clears_prior_chat_history(client, tmp_path: Path):
    sid = client.post("/api/sessions").json()["id"]
    _save_history(sid, [
        {"role": "user", "content": "describe this video"},
        {"role": "assistant", "content": "this is a screen recording with no people"},
    ])
    assert _load_history(sid) != []

    v = tmp_path / "v.mp4"
    _make_video(v, dur=1.0)
    r = _upload(client, sid, v)
    assert r.status_code == 200

    assert _load_history(sid) == [], (
        "uploading into a fresh, empty timeline must reset stale chat "
        "history — otherwise Claude keeps answering from a prior video"
    )


def test_upload_into_nonempty_timeline_preserves_chat_history(client, tmp_path: Path):
    """A second upload into a project that ALREADY has footage (e.g. adding
    b-roll) must not wipe out context the user is still relying on."""
    sid = client.post("/api/sessions").json()["id"]
    v1 = tmp_path / "v1.mp4"
    _make_video(v1, dur=1.0)
    r = _upload(client, sid, v1, name="v1.mp4")
    assert r.status_code == 200

    _save_history(sid, [
        {"role": "user", "content": "make the intro punchier"},
        {"role": "assistant", "content": "added a hook overlay"},
    ])
    assert _load_history(sid) != []

    v2 = tmp_path / "v2.mp4"
    _make_video(v2, dur=1.0)
    r2 = _upload(client, sid, v2, name="v2.mp4")
    assert r2.status_code == 200

    assert _load_history(sid) != [], (
        "a mid-project upload (timeline already has clips) must NOT wipe "
        "chat history — that context is still relevant to the user"
    )


def test_live_context_block_reports_empty_timeline(tmp_path: Path):
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[Track(id="v1", type="video", clips=[])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    block = _live_context_block(store)
    assert "EMPTY" in block


def test_live_context_block_reflects_actual_current_clips(tmp_path: Path):
    src = tmp_path / "yoga.normalized.mp4"
    src.write_bytes(b"placeholder")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[
                  Track(id="v1", type="video", clips=[
                      Clip(src=str(src), in_=0, out=10, start=0, id="c1"),
                  ]),
                  Track(id="text", type="text", clips=[
                      TextClip(id="t1", text="HELLO", start=0.0, end=2.0,
                               transform=Transform(x=160, y=40), role="super"),
                  ]),
              ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    block = _live_context_block(store)
    assert "video" in block
    assert "1 clip" in block or "clip(s)" in block
    assert "EMPTY" not in block


def test_live_context_block_updates_after_a_mutation(tmp_path: Path):
    """The block must be recomputed, not cached — a tool call that adds/
    removes a clip mid-turn must be visible on the NEXT call in the same
    chat turn's tool-use loop."""
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30),
              tracks=[Track(id="v1", type="video", clips=[])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    before = _live_context_block(store)
    assert "EMPTY" in before

    from video_ai_editor.agent.dispatch import dispatch
    src = tmp_path / "clip.normalized.mp4"
    src.write_bytes(b"placeholder")
    dispatch(store, "add_clip", {
        "track": "v1", "src": str(src), "in": 0.0, "out": 5.0, "start": 0.0,
    })

    after = _live_context_block(store)
    assert "EMPTY" not in after
    assert "video" in after
