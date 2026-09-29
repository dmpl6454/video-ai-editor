"""Final QA r4: a preview's heavy work is done ONCE (agent/prompt/artefacts.py).

Previewing "add captions" transcribed the clip on the scratch copy for the
card and Apply transcribed it again; reframe, denoise and background removal
did the same. The dry run now writes what it derives to a content-addressed
artefact cache under the preview area — keyed by (tool, input file identity,
parameters, model version) — and Apply reads it back:

  * preview + Apply of a captions plan runs whisper once; the committed tree
    fingerprints the same as the card; the live session is byte-identical
    during the preview (the same `_state` the byte-for-byte test pins);
  * a stale artefact — the source file changed under the same path — is not
    reused: Apply transcribes again;
  * a dropped card leaves its entry to the LRU budget
    (render/cache_budget.enforce_artefacts): whole entries, oldest first,
    and a miss derives again; a torn entry is a miss and is removed, never
    a dangling path;
  * a derived render (noise_reduce) made by the dry run is laid into the
    live cache on Apply, so the helper finds it and does not derive again;
  * the age sweep of scratch copies never touches the artefact area;
  * outside a prompt run the cache is off.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401
from test_prompt_preview import (SID, _card, _frames, _join, _live, _preview, _state, app_env)  # noqa: E402,F401

import video_ai_editor.ingest.transcribe as T  # noqa: E402
from video_ai_editor.agent.prompt import artefacts as A, changes as C, pending, preview as PV  # noqa: E402
from video_ai_editor.render import cache_budget  # noqa: E402

D = importlib.import_module("video_ai_editor.agent.dispatch")

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")


@pytest.fixture
def whisper_spy(monkeypatch):
    """Count whisper passes. The model probe says the model is on disk so the
    guard lets the step through; nothing is loaded."""
    calls: list[dict] = []

    def fake(path, language=None, model_size=None, backend=None, task="transcribe",
             on_progress=None, should_cancel=None):
        calls.append({"path": str(path), "model": model_size, "task": task})
        if on_progress is not None:
            on_progress(1.0, F.CLIP_DUR, F.CLIP_DUR)
        return T.Transcript.model_validate(F.transcript())

    monkeypatch.setattr(T, "transcribe", fake)
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda model: True)
    return calls


def _captions_plan():
    return F.plan_of(F.step("auto_caption", style="ig_chunky", position="bottom", model="small"),
                     title="add captions")


def _apply(client, card):
    r = client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": True})
    frames = _frames(r.text)
    _join()
    return frames


def _drop(client, card):
    client.post(f"/api/sessions/{SID}/prompt/answer", json={"token": card["token"], "apply": False})


def _area(root: Path) -> Path:
    return root / PV.PREVIEW_DIR / PV.ARTEFACT_DIR


# ------------------------------------------------------------- captions once

def test_preview_then_apply_of_captions_transcribes_once(app_env, whisper_spy, monkeypatch):
    client, store, facts, root = app_env
    live = _live(root)
    ingest = Path(live.edl.get_track("v1").clips[0].src).parent / "ingest.json"
    ingest_bytes = ingest.read_bytes()
    before_state = _state(live, Path(live.dir))
    before = live.edl.model_copy(deep=True)
    ops0, depth0 = len(live.ops.ops), live.undo_depth

    card = _card(_preview(client, monkeypatch, plan=_captions_plan(), message="add captions"))
    assert len(whisper_spy) == 1, whisper_spy
    # the live session is byte-identical during the preview: EDL, ops, undo,
    # every file of the session and the ingest.json beside the media
    assert _state(live, Path(live.dir)) == before_state
    assert ingest.read_bytes() == ingest_bytes
    # only the artefact area remains under the preview dir — no scratch copy
    assert [p.name for p in (root / PV.PREVIEW_DIR).iterdir()] == [PV.ARTEFACT_DIR]
    entries = list((_area(root) / A.WHISPER_KIND).iterdir())
    assert len(entries) == 1 and (entries[0] / A.TRANSCRIPT_FILE).is_file()
    assert any("caption" in line.lower() for line in card["preview"]["lines"]), card["preview"]["lines"]
    record = pending.load_pending(Path(live.dir))

    frames = _apply(client, card)
    types = [f["type"] for f in frames]
    assert types.count("op") == 1 and "error" not in types and types[-1] == "done", types
    # THE claim: Apply did not transcribe again
    assert len(whisper_spy) == 1, whisper_spy
    assert len(live.ops.ops) == ops0 + 1 and live.ops.ops[-1].tool == "prompt"
    assert live.undo_depth == depth0 + 1
    # the Apply result IS the card
    assert C.canonical(live.edl, before) == record["preview"]["fingerprint"]
    assert [c.text for c in C.summarize(before, live.edl, session_dir=Path(live.dir))] \
        == record["preview"]["all_lines"]
    cap = live.edl.get_track("captions")
    assert cap is not None and cap.clips
    # the transcript landed beside the media on Apply, as a real pass writes it
    assert json.loads(ingest.read_text(encoding="utf-8"))["transcript"]["segments"]
    assert pending.load_pending(Path(live.dir)) is None


def test_a_changed_source_is_transcribed_again_on_apply(app_env, whisper_spy, monkeypatch):
    """A stale artefact is never reused: the upload was replaced under the
    same path (same EDL, so the card is not stale), and the artefact's input
    identity no longer matches."""
    client, store, facts, root = app_env
    live = _live(root)
    card = _card(_preview(client, monkeypatch, plan=_captions_plan(), message="add captions"))
    assert len(whisper_spy) == 1
    src = Path(live.edl.get_track("v1").clips[0].src)
    before_id = A.file_identity(src)
    time.sleep(0.01)
    F.speech_clip(root, w=352, h=198)          # the same path, a different file
    assert A.file_identity(src) != before_id
    frames = _apply(client, card)
    types = [f["type"] for f in frames]
    assert types.count("op") == 1 and "error" not in types, types
    assert len(whisper_spy) == 2, whisper_spy
    cap = live.edl.get_track("captions")
    assert cap is not None and cap.clips


def test_a_dropped_card_leaves_the_artefact_to_the_lru_budget(app_env, whisper_spy, monkeypatch):
    client, store, facts, root = app_env
    monkeypatch.setattr(cache_budget, "PROTECT_RECENT_S", 0.0)
    card = _card(_preview(client, monkeypatch, plan=_captions_plan(), message="add captions"))
    _drop(client, card)
    units = cache_budget.artefact_units(_area(root))
    assert len(units) == 1 and units[0].size > 0
    assert cache_budget.artefact_usage(_area(root))["entries"] == 1
    # a second preview of the same plan reuses it: still one pass
    card = _card(_preview(client, monkeypatch, plan=_captions_plan(), message="add captions"))
    assert len(whisper_spy) == 1
    _drop(client, card)
    # under a zero budget the whole entry goes, oldest first
    removed = cache_budget.enforce_artefacts(_area(root), budget=0)
    assert removed == [units[0].path] and not units[0].path.exists()
    # and the next preview derives again (a miss is a real pass, never an error)
    _card(_preview(client, monkeypatch, plan=_captions_plan(), message="add captions"))
    assert len(whisper_spy) == 2


def test_the_lru_keeps_the_newest_entries_and_protects_the_one_being_written(tmp_path):
    root = tmp_path / "artefacts"
    units = []
    for i in range(3):
        d = root / A.WHISPER_KIND / f"k{i}"
        d.mkdir(parents=True)
        (d / A.TRANSCRIPT_FILE).write_bytes(b"x" * 1000)
        os.utime(d / A.TRANSCRIPT_FILE, (1_000_000 + i, 1_000_000 + i))
        units.append(d)
    removed = cache_budget.enforce_artefacts(root, budget=2500)
    assert removed == [units[0]] and units[1].exists() and units[2].exists()
    removed = cache_budget.enforce_artefacts(root, budget=0, protect=[units[1]])
    assert removed == [units[2]] and units[1].exists()


# ------------------------------------------------------------ torn entries

def test_a_torn_entry_is_a_miss_and_is_removed(tmp_path):
    root = tmp_path / "artefacts"
    cache_dir = tmp_path / "s_1" / "cache"
    calls = []

    def fake(path, **kw):
        calls.append(1)
        return T.Transcript.model_validate(F.transcript())

    src = tmp_path / "a.mp4"
    src.write_bytes(b"\x00" * 4096)
    with A.active(root, write=True):
        # a derived entry whose manifest names a file that is gone
        d = A.entry_dir(A.current(), A.DERIVED_KIND, "kk")
        (d / "files").mkdir(parents=True)
        (d / A.MANIFEST_FILE).write_text(json.dumps({"files": ["denoise/x.mp4"]}), encoding="utf-8")
        assert A.restore_files(A.current(), "kk", cache_dir) == []
        assert not d.exists() and not cache_dir.exists()
        # a derived entry without its manifest (a writer that died) is nothing
        d2 = A.entry_dir(A.current(), A.DERIVED_KIND, "half")
        (d2 / "files" / "denoise").mkdir(parents=True)
        (d2 / "files" / "denoise" / "y.mp4").write_bytes(b"y")
        assert A.restore_files(A.current(), "half", cache_dir) == []
        # a whisper entry holding garbage is a miss: a real pass, and it is rewritten
        key = A.transcript_key(src, model="small", language=None, task="transcribe", backend=None)
        w = A.entry_dir(A.current(), A.WHISPER_KIND, key)
        w.mkdir(parents=True)
        (w / A.TRANSCRIPT_FILE).write_text("{not json", encoding="utf-8")
        tx = A.cached_transcribe(fake, src, model_size="small")
        assert calls == [1] and tx.segments
        assert json.loads((w / A.TRANSCRIPT_FILE).read_text(encoding="utf-8"))["transcript"]["segments"]
        # and now a hit
        A.cached_transcribe(fake, src, model_size="small")
        assert calls == [1]
    with A.active(root, write=False):
        # an Apply hits too, and never writes a new entry on a miss
        A.cached_transcribe(fake, src, model_size="small")
        assert calls == [1]
        A.cached_transcribe(fake, src, model_size="small", language="hi")
        assert calls == [1, 1]
        assert len(list((root / A.WHISPER_KIND).iterdir())) == 1
    # off outside a prompt run
    A.cached_transcribe(fake, src, model_size="small")
    assert calls == [1, 1, 1]


def test_keys_change_with_input_params_and_version(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"\x01" * 100)
    k = A.make_key("noise_reduce", [src], {"strength": 0.85}, "1")
    assert k == A.make_key("noise_reduce", [src], {"strength": 0.85}, "1")
    assert k != A.make_key("noise_reduce", [src], {"strength": 0.5}, "1")
    assert k != A.make_key("noise_reduce", [src], {"strength": 0.85}, "2")
    assert k != A.make_key("stabilize", [src], {"strength": 0.85}, "1")
    with open(src, "ab") as fh:
        fh.write(b"\x02")
    assert k != A.make_key("noise_reduce", [src], {"strength": 0.85}, "1")
    assert A.file_identity(tmp_path / "missing.mp4") == "missing"


def test_the_scratch_sweep_never_touches_the_artefact_area(tmp_path):
    area = tmp_path / PV.PREVIEW_DIR
    old_run = area / "run_old" / "s_x"
    old_run.mkdir(parents=True)
    entry = area / PV.ARTEFACT_DIR / A.WHISPER_KIND / "k"
    entry.mkdir(parents=True)
    (entry / A.TRANSCRIPT_FILE).write_text("{}", encoding="utf-8")
    stale = time.time() - 2 * PV.STALE_SCRATCH_S
    for p in (old_run.parent, area / PV.ARTEFACT_DIR):
        os.utime(p, (stale, stale))
    PV._sweep(area)
    assert not old_run.parent.exists()
    assert (entry / A.TRANSCRIPT_FILE).is_file()


# --------------------------------------------------------- derived renders

def test_preview_then_apply_of_a_denoise_derives_once(app_env, monkeypatch):
    """The dry run's denoised render is laid into the live cache before
    Apply's handler runs, so the helper finds it (its content-hash name) and
    derives nothing; the card and the Apply agree."""
    from video_ai_editor.ai import denoise as DN
    client, store, facts, root = app_env
    live = _live(root)
    derivations: list[str] = []

    def fake_denoise_clip(src, cache_dir, *, strength=0.85, sample_rate=48000, on_progress=None,
                          cancel_event=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256(f"{src}|{strength}".encode()).hexdigest()[:14]
        dst = cache_dir / f"denoise_{h}.mp4"
        if dst.exists() and dst.stat().st_size > 0:
            return dst
        derivations.append(str(dst))
        shutil.copyfile(src, dst)
        return dst

    monkeypatch.setattr(DN, "available", lambda: True)
    monkeypatch.setattr(DN, "denoise_clip", fake_denoise_clip)
    plan = F.plan_of(F.step("noise_reduce", clip_id="$v1_first", strength=0.85), title="clean up the audio")
    before_state = _state(live, Path(live.dir))
    before = live.edl.model_copy(deep=True)

    card = _card(_preview(client, monkeypatch, plan=plan, message="clean up the audio on clip 1"))
    assert len(derivations) == 1 and derivations[0].startswith(str(root / PV.PREVIEW_DIR)), derivations
    assert _state(live, Path(live.dir)) == before_state
    assert not (Path(live.dir) / "cache" / "denoise").exists()
    entries = list((_area(root) / A.DERIVED_KIND).iterdir())
    assert len(entries) == 1
    manifest = json.loads((entries[0] / A.MANIFEST_FILE).read_text(encoding="utf-8"))
    name = Path(derivations[0]).name
    assert f"denoise/{name}" in manifest["files"] and manifest["tool"] == "noise_reduce"
    record = pending.load_pending(Path(live.dir))

    frames = _apply(client, card)
    types = [f["type"] for f in frames]
    assert types.count("op") == 1 and "error" not in types, types
    assert len(derivations) == 1, derivations
    clip = live.edl.get_track("v1").clips[0]
    assert Path(clip.src) == Path(live.dir) / "cache" / "denoise" / name and Path(clip.src).is_file()
    assert C.canonical(live.edl, before) == record["preview"]["fingerprint"]
    assert [c.text for c in C.summarize(before, live.edl, session_dir=Path(live.dir))] \
        == record["preview"]["all_lines"]


def test_a_plain_run_never_touches_the_artefact_area(app_env, whisper_spy, monkeypatch):
    """Setting OFF: the old immediate run — a real pass, nothing cached."""
    client, store, facts, root = app_env
    monkeypatch.setenv("VAI_PROMPT_CONFIRM", "0")
    frames = _preview(client, monkeypatch, plan=_captions_plan(), message="add captions")
    types = [f["type"] for f in frames]
    assert "clarify" not in types and types.count("op") == 1, types
    assert len(whisper_spy) == 1
    assert not _area(root).exists()
