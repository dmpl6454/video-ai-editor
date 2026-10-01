"""Test helpers for lane EB1-D (analysis) over lane A's fixtures.

The truth is A's (`brain_fixtures.build_brain_fixtures()`: TH and P2, truth
by concatenation offsets, see `brain_fixture_truth.py`); this module only adds
what analysis needs and A's builders do not carry:

  * `ensure_transcript(media)` — whisper.cpp `small` over `media` (the backend
    the upload path uses; the ggml already cached on this Mac, never a
    download), persisted as an `ingest.json` in a D-owned cache directory
    keyed by the media's content (`CACHE_ROOT/eb1d-transcripts/<key>/`) — A's
    fixture folders are never written to;
  * `th_sources` / `p2_sources` — the `sources` list `graph.analyse` gets,
    in the order a session lists them (v1 first, then the audio lanes), with
    the transcript of record given explicitly;
  * `speaker_mask` — the talking mask of a truth speaker at 100 Hz.

Every helper skips cleanly (`pytest.skip`) when the Piper voice, macOS `say`
or the whisper.cpp ggml is missing.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark.media import CACHE_ROOT  # noqa: E402

TRANSCRIPT_VERSION = 1
TRANSCRIPT_ROOT = CACHE_ROOT / "eb1d-transcripts"


def whisper_small_available() -> bool:
    from video_ai_editor.ingest import transcribe as _t
    try:
        return bool(_t._whisper_cpp_available() and _t._whisper_cpp_model_path("small").exists())
    except Exception:
        return False


def _content_key(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha256(f"{TRANSCRIPT_VERSION}|{st.st_size}|".encode())
    with open(path, "rb") as fh:
        h.update(fh.read(1 << 20))
        if st.st_size > 2 << 20:
            fh.seek(-(1 << 20), os.SEEK_END)
            h.update(fh.read(1 << 20))
    return h.hexdigest()[:16]


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def transcript_path(media: str | os.PathLike) -> Path:
    """The `ingest.json` of `media`'s transcript (built on first use)."""
    media = Path(media)
    p = TRANSCRIPT_ROOT / _content_key(media) / "ingest.json"
    if not p.exists():
        from video_ai_editor.ingest.transcribe import transcribe
        tr = transcribe(media, language="en", model_size="small", backend="whisper_cpp")
        _write_json(p, {"src": media.name, "normalized": media.name, "transcript": tr.model_dump()})
    return p


def ensure_transcript(media: str | os.PathLike) -> dict:
    return json.loads(transcript_path(media).read_text(encoding="utf-8"))["transcript"]


def th_sources(fx) -> list[dict]:
    """TH: one file, v1, with its own transcript."""
    return [{"path": fx.th.video_16x9, "transcript": str(transcript_path(fx.th.video_16x9))}]


def p2_sources(fx) -> list[dict]:
    """P2 as a session lists it: cam A on v1, cam B, then the recorder on an
    audio lane — no roles given, so analysis must find them by measurement."""
    return [{"path": fx.p2.cam_a}, {"path": fx.p2.cam_b},
            {"path": fx.p2.recorder_wav, "transcript": str(transcript_path(fx.p2.recorder_wav))}]


def _fixtures():
    import pytest
    from brain_fixtures import build_brain_fixtures
    try:
        return build_brain_fixtures()
    except FileNotFoundError as e:
        pytest.skip(str(e))


def th_or_skip():
    import pytest
    if not whisper_small_available():
        pytest.skip("whisper.cpp small ggml is not on this machine")
    return _fixtures()


def p2_or_skip():
    import pytest
    fx = th_or_skip()
    if fx.p2 is None:
        pytest.skip(fx.p2_skip_reason or "the two-camera podcast fixture is unavailable")
    return fx


def speaker_mask(truth, speaker: str, hz: int = 100) -> np.ndarray:
    """Talking mask of a truth speaker (S1/S2) at `hz`, reference seconds,
    over the voiced spans of its own utterances."""
    m = np.zeros(int(truth.duration * hz) + 2, dtype=bool)
    for u in truth.utts:
        if u.speaker == speaker and u.kind not in ("pause", "gap", "silence") and u.voiced_start is not None:
            m[int(u.voiced_start * hz): int(u.voiced_end * hz) + 1] = True
    return m


def sentence_at(sentences: list[dict], truth_sentence, slack: float = 0.05) -> dict | None:
    """The layer sentence covering the middle of a truth sentence."""
    mid = (truth_sentence.t0 + truth_sentence.t1) / 2
    hits = [s for s in sentences if s["t0"] - slack <= mid <= s["t1"] + slack]
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# what a graph must say about each fixture (shared by the golden and the
# packaged-import tests: both build the graphs, both must read the truth)
# --------------------------------------------------------------------------

#: whisper.cpp's word edges are repaired toward the voiced runs
#: (analysis/word_timing.py); worst measured on TH: −0.22 s start, +0.21 s end.
SENTENCE_START_TOL_S = 0.25
SENTENCE_END_TOL_S = 0.30
OFFSET_TOL_S = 0.010
ACOUSTIC_TOL_S = 0.06


def _assert_all_files_validate(session_dir, g: dict) -> None:
    """Every file the analysis wrote is valid against lane C's models: the
    header, scenes.json, angles.json and each of the source's layer files
    (`store.layer_status` — `ok`, never `failed:*`)."""
    from video_ai_editor.brain import schema as S
    from video_ai_editor.brain import store
    S.Graph.model_validate(g)
    S.Scenes.model_validate(json.loads((store.brain_dir(session_dir) / "scenes.json").read_text(encoding="utf-8")))
    assert store.read_angles(session_dir) is not None
    for layer, rel in g["layers"].items():
        name = Path(rel).stem
        assert store.layer_status(g["reference"], layer, name) == "ok", (layer, name)


def check_th_graph(fx, session_dir, gid: str) -> dict:
    """TH's graph against the truth: sentences (ids exact, edges within the
    repaired-whisper tolerance), both filler kinds by time, the retake, the
    emphasis (rms_z/stretch), hook top-1, the five planted pauses as scenes,
    one Host on angle A, v1 recorded. Returns the loaded pieces."""
    from video_ai_editor.brain import graph, store
    from video_ai_editor.brain.analysis.delivery import sentence_inputs
    from video_ai_editor.brain.analysis import pcm as _pcm
    t = fx.th.truth
    g = graph.load_graph(session_dir, gid)
    assert store.current_graph_id(session_dir) == gid and store.read_graph(session_dir, gid) is not None
    _assert_all_files_validate(session_dir, g)
    (src,) = g["sources"]
    assert (src["role"], src["angle"], src["dialogue"], src["path"]) == ("angle", "A", True, str(fx.th.video_16x9))
    assert "v1" in src["tags"] and src["layers"] == {"speech": "ok", "speakers": "ok", "audio": "ok", "semantic": "ok"}
    speech = graph.load_layer(g, g["reference"], "speech")
    audio = graph.load_layer(g, g["reference"], "audio")
    sem = graph.load_layer(g, g["reference"], "semantic")
    assert [s["id"] for s in speech["sentences"]] == [x.id for x in t.sentences]
    for ts, s in zip(t.sentences, speech["sentences"]):
        assert -SENTENCE_START_TOL_S <= s["t0"] - ts.t0 <= SENTENCE_START_TOL_S, (ts.id, s["t0"], ts.t0)
        assert -SENTENCE_END_TOL_S <= s["t1"] - ts.t1 <= SENTENCE_END_TOL_S, (ts.id, s["t1"], ts.t1)
    fill = [w for w in speech["words"] if w.get("filler")]
    for uid in t.fillers_lexical:
        a, b = t.utt(uid).voiced
        assert any(a - 0.1 <= (w["t0"] + w["t1"]) / 2 <= b + 0.1 for w in fill), uid
    assert len(fill) == len(t.fillers_lexical)
    planted = [t.utt(u).voiced for u in t.fillers_acoustic]
    found = speech["acoustic_fillers"]
    assert len(found) == len(planted) == 3
    for (a, b), f in zip(planted, found):
        assert abs(f["t0"] - a) <= ACOUSTIC_TOL_S and abs(f["t1"] - b) <= ACOUSTIC_TOL_S, (a, b, f)
    assert [(r["dup"], r["of"]) for r in speech["flags"]["repeats"]] == [(t.retake.dup, t.retake.of)]
    top = max(sem["scores"], key=lambda k: sem["scores"][k]["hook"])
    assert top == t.quotable and sem["scores"][t.quotable]["hook"] - sem["scores"][t.throwaway]["hook"] >= 0.2
    pcm = _pcm.read_pcm(fx.th.video_16x9)
    inp = sentence_inputs(speech, audio, pcm)
    assert inp[t.emphasis.sent]["rms_z"] >= 1.5 and inp[t.emphasis.sent]["stretch"] >= 1.1
    scenes = graph.load_scenes(session_dir, gid)
    planted_pauses = [p for p in t.pauses if p.kind == "planted"]
    assert sum(1 for s in scenes if s["kind"] == "pause") == len(planted_pauses) == 5
    for p in planted_pauses:     # a pause scene holds the planted pause (a filler may sit at its far end)
        assert any(s["kind"] == "pause" and s["t0"] <= p.t0 + 0.25 and s["t1"] >= p.t1 - 0.25
                   and s["t1"] - s["t0"] <= (p.t1 - p.t0) + 1.0 for s in scenes), p
    (spk,) = g["speakers"]
    assert (spk["id"], spk["role_guess"], spk["name"], spk["angle_hint"]["angle"]) == ("S1", "host", "Host", "A")
    assert g["content_type"]["guess"] == "talking_head"
    return {"graph": g, "speech": speech, "audio": audio, "semantic": sem, "scenes": scenes}


def check_p2_graph(fx, session_dir, gid: str) -> dict:
    """P2's graph against the truth: the recorder found as the reference and
    dialogue, per-file offsets within 10 ms, two speakers with roles and
    own-mic angle hints, the false start, cam A on v1."""
    from video_ai_editor.brain import graph, store
    t = fx.p2.truth
    g = graph.load_graph(session_dir, gid)
    assert store.current_graph_id(session_dir) == gid and store.read_graph(session_dir, gid) is not None
    _assert_all_files_validate(session_dir, g)
    ref, *cams = g["sources"]
    assert (ref["role"], ref["dialogue"], ref["has_video"], ref["path"]) == ("reference_audio", True, False,
                                                                           str(fx.p2.recorder_wav))
    assert g["reference"] == ref["key"]
    by_leaf = {s["leaf"]: s for s in cams}
    for name in ("cam_a", "cam_b"):
        s = by_leaf[Path(getattr(fx.p2, name)).name]
        assert s["role"] == "angle" and s["path"] == str(getattr(fx.p2, name)) and s["dialogue"] is False
        assert abs(s["sync_offset_s"] - t.offsets[name]) <= OFFSET_TOL_S, (name, s["sync_offset_s"], t.offsets[name])
        assert s["angle"] == {"cam_a": "A", "cam_b": "B"}[name]
    assert "v1" in by_leaf[Path(fx.p2.cam_a).name]["tags"] and "sync_unverified" not in by_leaf[Path(fx.p2.cam_a).name]["tags"]
    angles = json.loads((store.brain_dir(session_dir) / "angles.json").read_text(encoding="utf-8"))
    assert angles["reference"] == ref["key"] == angles["dialogue"]
    assert [m["angle"] for m in angles["members"]] == ["A", "B"]
    for m, name in zip(angles["members"], ("cam_a", "cam_b")):
        assert m["by"] == "fft_xcorr" and m["confidence"] >= 0.5 and abs(m["sync_offset_s"] - t.offsets[name]) <= OFFSET_TOL_S
        assert m["sees"] == [t.own_mic[name]] and m["path"] == str(getattr(fx.p2, name))
    speakers = {sp["id"]: sp for sp in g["speakers"]}
    assert set(speakers) == {"S1", "S2"}
    for sid, sp in speakers.items():
        cam = next(k for k, v in t.own_mic.items() if v == sid)
        assert sp["role_guess"] == t.speakers[sid]["role"] and sp["name"] == {"host": "Host", "guest": "Guest"}[sp["role_guess"]]
        assert sp["angle_hint"]["angle"] == {"cam_a": "A", "cam_b": "B"}[cam]
        assert sp["angle_hint"]["by"] == "own_mic" and sp["angle_hint"]["confidence"] >= 0.3
    speech = graph.load_layer(g, ref["key"], "speech")
    audio = graph.load_layer(g, ref["key"], "audio")
    assert set(audio["own_mic_energy"]) == {by_leaf[Path(fx.p2.cam_a).name]["key"], by_leaf[Path(fx.p2.cam_b).name]["key"]}
    assert len(speech["flags"]["false_starts"]) == 1 and len(speech["turns"]) >= 30
    assert {w["spk"] for w in speech["words"]} <= {"S1", "S2"}
    assert g["content_type"]["guess"] in ("podcast", "interview")
    return {"graph": g, "speech": speech, "audio": audio}


__all__ = ["check_th_graph", "check_p2_graph", "SENTENCE_START_TOL_S", "SENTENCE_END_TOL_S", "OFFSET_TOL_S",
           "ACOUSTIC_TOL_S", "whisper_small_available", "transcript_path", "ensure_transcript", "th_sources", "p2_sources",
           "th_or_skip", "p2_or_skip", "speaker_mask", "sentence_at"]
