"""The three dispatch additions of spec §4.9 (agent/dispatch.py, tools.py):

  1. `transcribe` — non-mutating: persists a transcript, commits nothing,
     reuses one that exists, refuses a model that is not on disk (no
     download without a yes), honours progress + cancel;
  2. `add_music(loop=true)` — a short bed laid back to back until the video
     extent is covered, only the last piece fading out;
  3. platform-aware overlay defaults — captions and lower thirds sit inside
     the 9:16 safe zone, unchanged on 16:9 — and the renderer's caption
     anchor agrees with the EDL (`render/text_overlay.caption_anchor_y`).
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor.agent.tools import input_schema_for, list_tools  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, TextClip  # noqa: E402
from video_ai_editor.ingest import transcribe as _T  # noqa: E402
from video_ai_editor.render import text_overlay as TO  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")


class _Word:
    def __init__(self, word, start, end):
        self.word, self.start, self.end, self.prob = word, start, end, 1.0


def _fake_transcript(words):
    from video_ai_editor.ingest.transcribe import Transcript
    return Transcript.model_validate(F.transcript(words))


# ---------------------------------------------------------------- transcribe

def test_transcribe_is_advertised_and_dispatchable():
    assert "transcribe" in D.DISPATCH
    schema = input_schema_for("transcribe")
    assert set(schema["properties"]) == {"model", "force"}
    assert schema["properties"]["model"]["enum"] == ["small", "large-v3-turbo", "large-v3"]
    assert next(t for t in list_tools() if t["name"] == "transcribe")["category"] == "ai"
    from video_ai_editor.main import ASYNC_DISPATCH_TOOLS
    assert "transcribe" not in ASYNC_DISPATCH_TOOLS          # the phone's mirror is frozen for 0.7.0


def test_transcribe_reuses_a_persisted_transcript_and_commits_nothing(tmp_path, monkeypatch):
    store = F.make_store(tmp_path)
    ops = len(store.ops.ops)
    monkeypatch.setattr(_T, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not transcribe")))
    r = D.dispatch(store, "transcribe", {})
    assert r["reused"] is True and r["words"] == len(F.WORDS) and r["backend"] == "cached"
    assert len(store.ops.ops) == ops


def test_transcribe_refuses_a_model_that_is_not_on_disk(tmp_path, monkeypatch):
    store = F.make_store(tmp_path)
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: False)
    called: list[str] = []
    monkeypatch.setattr(_T, "transcribe", lambda *a, **k: called.append("x"))
    with pytest.raises(ValueError, match="not downloaded"):
        D.dispatch(store, "transcribe", {"force": True, "model": "large-v3"})
    assert called == []


def test_transcribe_writes_the_transcript_beside_the_upload_with_progress_and_cancel(tmp_path, monkeypatch):
    store = F.make_store(tmp_path)
    ingest = Path(store.edl.get_track("v1").clips[0].src).parent / "ingest.json"
    ops = len(store.ops.ops)
    seen: dict = {}

    def fake(audio_path, language=None, model_size=None, backend=None, task="transcribe",
             on_progress=None, should_cancel=None):
        seen.update(path=Path(audio_path), model=model_size, cancel=should_cancel())
        on_progress(0.5, 6.0, 12.0)
        return _fake_transcript([("new", 0.0, 0.5), ("words", 0.6, 1.0)])

    monkeypatch.setattr(_T, "transcribe", fake)
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: m == "small")
    progress: list[float] = []
    r = D.dispatch(store, "transcribe", {"force": True, "model": "small"},
                   set_progress=progress.append, cancel_event=threading.Event())
    assert r["reused"] is False and r["words"] == 2 and r["model"] == "small" and r["path"] == str(ingest)
    assert seen["path"] == Path(store.edl.get_track("v1").clips[0].src) and seen["cancel"] is False
    assert progress == [0.5, 1.0]
    data = json.loads(ingest.read_text(encoding="utf-8"))
    assert [w["word"] for s in data["transcript"]["segments"] for w in s["words"]] == ["new", "words"]
    assert data["spoken_language"] == "en" and data["src"]                # the upload's own keys survive
    assert len(store.ops.ops) == ops and store.edl.hash() == store.edl.hash()
    tx, _ = D._load_transcript_with_source(store)
    assert [w.word for w in tx.words] == ["new", "words"]                  # every consumer now sees it


def test_transcribe_falls_back_to_the_session_file_and_needs_a_clip(tmp_path, monkeypatch):
    src = F.speech_clip(tmp_path, name="outside")
    store = F.make_store(tmp_path, src=src, with_transcript=False)
    (src.parent / "ingest.json").unlink()                                 # no sidecar: a desktop add_clip
    monkeypatch.setattr(_T, "transcribe", lambda *a, **k: _fake_transcript([("hi", 0.0, 0.5)]))
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: True)
    r = D.dispatch(store, "transcribe", {})
    assert r["path"] == str(Path(store.dir) / "transcript.json") and (Path(store.dir) / "transcript.json").exists()
    from video_ai_editor.edl import EDLStore
    bare = EDLStore(tmp_path / "bare")
    with pytest.raises(ValueError, match="no clip on v1"):
        D.dispatch(bare, "transcribe", {})
    store.edl.get_track("v1").clips[0].src = str(tmp_path / "gone.mp4")
    with pytest.raises(ValueError, match="source not found"):
        D.dispatch(store, "transcribe", {"force": True})


def test_whisper_model_on_disk_probes_the_hf_cache_without_loading(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(_T, "_whisper_cpp_available", lambda: False)
    assert D.whisper_model_on_disk("small") is False
    snap = tmp_path / "hub" / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    assert D.whisper_model_on_disk("small") is True
    assert D.whisper_model_on_disk("large-v3") is False


# ---------------------------------------------------------------- add_music(loop)

def _music(store):
    return [c for c in store.edl.get_track("music").clips if isinstance(c, Clip)]


def test_add_music_loop_covers_the_extent_with_gapless_pieces(tmp_path):
    store = F.make_store(tmp_path)
    bed = F.music_bed(tmp_path, dur=5.0)
    r = D.dispatch(store, "add_music", {"src": str(bed), "start": 0.0, "volume_db": -14, "loop": True})
    clips = _music(store)
    assert r["loops"] == 3 and r["clip_ids"] == [c.id for c in clips] and r["clip_id"] == clips[0].id
    assert [(c.start, c.in_, round(c.out, 3)) for c in clips] == [(0.0, 0.0, 5.0), (5.0, 0.0, 5.0), (10.0, 0.0, 2.0)]
    assert [c.audio.fade_in for c in clips] == [0.5, 0.0, 0.0]
    assert [c.audio.fade_out for c in clips] == [0.0, 0.0, 1.0]
    assert all(c.audio.gain_db == -14 for c in clips)
    assert max(c.start + c.effective_duration for c in clips) == pytest.approx(12.0)
    assert "looped ×3 to 12.0s" in r["summary"]
    assert store.ops.last().tool == "add_music"


def test_add_music_without_loop_and_with_a_long_bed_lays_one_clip(tmp_path):
    store = F.make_store(tmp_path)
    short = F.music_bed(tmp_path, name="short.wav", dur=5.0)
    r = D.dispatch(store, "add_music", {"src": str(short), "start": 0.0})
    assert r["loops"] == 1 and len(_music(store)) == 1 and _music(store)[0].audio.fade_out == 1.0
    store.edl.get_track("music").clips.clear()
    long_bed = F.music_bed(tmp_path, name="long.wav", dur=20.0)
    r = D.dispatch(store, "add_music", {"src": str(long_bed), "start": 0.0, "loop": True})
    assert r["loops"] == 1 and _music(store)[0].out == pytest.approx(12.0)   # already fitted to the extent


def test_add_music_loop_honours_a_start_offset(tmp_path):
    store = F.make_store(tmp_path)
    bed = F.music_bed(tmp_path, dur=4.0)
    D.dispatch(store, "add_music", {"src": str(bed), "start": 2.0, "loop": True})
    clips = _music(store)
    assert [c.start for c in clips] == [2.0, 6.0, 10.0] and clips[-1].out == pytest.approx(2.0)


# ---------------------------------------------------------------- overlay defaults + renderer agreement

@pytest.mark.parametrize("w, h, name", [(1080, 1920, "9:16"), (1920, 1080, "16:9"), (1080, 1080, "1:1"),
                                        (1080, 1350, "4:5"), (1088, 1920, "9:16"), (1000, 1500, "other")])
def test_canvas_aspect_name_with_tolerance(w, h, name):
    assert D.canvas_aspect_name(w, h) == name


def test_overlay_default_y_moves_up_only_on_vertical_canvases():
    v, l = Canvas(w=1080, h=1920, fps=30), Canvas(w=1920, h=1080, fps=30)
    assert D.overlay_default_y(v, "caption") == pytest.approx(1920 * 0.76)
    assert D.overlay_default_y(v, "lower_third") == pytest.approx(1920 * 0.74)
    assert D.overlay_default_y(l, "caption") == pytest.approx(1080 * 0.85)
    assert D.overlay_default_y(l, "lower_third") == pytest.approx(1080 * 0.80)
    assert D.overlay_default_y(v, "caption", "top") == pytest.approx(1920 * 0.15)
    assert D.overlay_default_y(v, "caption", "center") == pytest.approx(1920 * 0.5)
    zone = D._safe_zone("9:16")
    assert zone["y_min"] <= zone["caption_y"] <= zone["y_max"] and zone["x_max"] == 0.85
    from video_ai_editor.agent.prompt.presets import SAFE_ZONES
    assert set(SAFE_ZONES) >= {"9:16", "16:9", "1:1", "4:5"}                 # P's table is the source


def test_caption_and_lower_third_handlers_write_the_safe_zone_y(tmp_path):
    store = F.make_store(tmp_path)
    D.dispatch(store, "set_canvas", {"w": 1080, "h": 1920})
    D.dispatch(store, "add_caption_track", {"style": "ig_chunky", "position": "bottom"})
    cues = [c for c in store.edl.get_track("captions").clips if isinstance(c, TextClip)]
    assert cues and all(c.transform.y == pytest.approx(1920 * 0.76) for c in cues)
    D.dispatch(store, "add_lower_third", {"name": "Priya", "start": 0.0, "end": 3.0})
    lt = next(c for c in store.edl.get_track("tx_lt").clips if c.role == "lower_third")
    assert lt.transform.y == pytest.approx(1920 * 0.74)
    D.dispatch(store, "set_canvas", {"w": 1920, "h": 1080})
    D.dispatch(store, "add_caption_track", {"style": "ig_chunky", "position": "bottom"})
    cues = [c for c in store.edl.get_track("captions").clips if isinstance(c, TextClip)]
    assert all(c.transform.y == pytest.approx(1080 * 0.85) for c in cues)


def test_renderer_caption_anchor_agrees_with_the_edl_on_portrait():
    """Captions ignore per-clip transforms (the captions block owns them), so
    the renderer's own anchor must move: 0.76·h on 9:16, the historic 0.84·h
    everywhere else. `overlay_positions` (the verifier) reads it the same way."""
    assert TO.caption_anchor_y(1080, 1920) == pytest.approx(1920 * 0.76)
    assert TO.caption_anchor_y(1920, 1080) == pytest.approx(1080 * 0.84)
    assert TO.caption_anchor_y(None, 1080) == pytest.approx(1080 * 0.84)
    assert TO._y_for_role("caption", 999.0, 1920, 1080) == pytest.approx(1920 * 0.76)   # explicit y ignored for captions
    assert TO._y_for_role("lower_third", 1420.8, 1920, 1080) == pytest.approx(1420.8)  # honoured for lower thirds
    clip = TextClip(text="hi", start=0, end=1, role="caption")
    assert TO.resolve_anchor_overrides(clip, "caption", 1080, 1920) == (None, None)
    png = TO.render_text_png("HELLO", "caption", 1080, 1920)
    bbox = png.getbbox()
    assert bbox is not None
    centre = (bbox[1] + bbox[3]) / 2
    assert abs(centre - 1920 * 0.76) < 1920 * 0.03                           # pixels land where the EDL says
    landscape = TO.render_text_png("HELLO", "caption", 1920, 1080).getbbox()
    assert abs((landscape[1] + landscape[3]) / 2 - 1080 * 0.84) < 1080 * 0.03
