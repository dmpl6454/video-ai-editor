"""The benchmark's own foundations, in the default suite (UNMARKED, spec
§6.4): the synthesized media and its ground truth, the 200 s loop fixture,
the socket guard, session cloning and the SSE parser. Warm (media cached
under the user cache dir) this file runs in a few seconds; the first run
pays ~15 s of Piper + ffmpeg once per `media_key()`.

What each test pins, and why it is a test rather than a note:
  * the planted fillers are single tokens in `FILLERS_STRICT` and the ONLY
    silences ≥ 0.5 s in the narration are the planted pauses — otherwise
    `remove_silences` would cut something the ground truth does not know
    about and the benchmark would measure the fixture, not the editor;
  * `detect_shots` finds every scene cut — case 18 and the `shorts` case
    depend on the seams being real;
  * librosa's beats sit on the synthesis grid — case 10 scores against the
    DETECTED beats, so a bed the detector cannot follow would fail every
    editor for the detector's fault;
  * the guard trips on a deliberate `urlopen` and leaves loopback alone —
    a guard that silently allowed egress would make "zero network" a claim
    with no evidence.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.agent.prompt.recipes import FILLERS_STRICT
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.ingest.probe import probe
from video_ai_editor.ingest.scenes import detect_shots

from . import media as MEDIA
from .harness import EgressAttempted, EgressGuard, PromptRun, clone_session_dir, parse_sse
from .media import (BED_LUFS, BED_SECONDS, LOOP_FIXTURE_SECONDS, MediaSet, build_media_set,
                    ensure_preset_beds, media_key)
from .narration import CONTENT_LIKE_SENTENCE, GAP_S, PAUSE_S, PLANTED_FILLERS, hindi_backend

_SILENCE = re.compile(r"silence_start: ([\d.]+)|silence_duration: ([\d.]+)")


@pytest.fixture(scope="module")
def media() -> MediaSet:
    return build_media_set()


# --- narration ground truth --------------------------------------------------

def test_script_plants_nine_strict_fillers_and_one_content_like():
    assert len(PLANTED_FILLERS) == 9
    assert set(PLANTED_FILLERS) <= set(FILLERS_STRICT)
    assert "like" not in FILLERS_STRICT and CONTENT_LIKE_SENTENCE.startswith("I like")


def test_ground_truth_is_contiguous_and_voiced_spans_are_inside(media: MediaSet):
    nar = media.narration
    assert 60 <= nar.duration <= 120
    assert len(nar.fillers) == 9 and len(nar.pauses) == 7 and len(nar.sentences) == 12
    for a, b in zip(nar.utterances, nar.utterances[1:]):
        assert abs(a.end - b.start) < 1e-6, (a, b)
    for u in nar.spoken:
        vs, ve = u.voiced
        assert u.start <= vs < ve <= u.end + 1e-6, u
    assert all(abs(p.duration - PAUSE_S) < 1e-3 for p in nar.pauses)
    assert all(g.duration < 0.5 for g in nar.utterances if g.kind == "gap") and GAP_S < 0.5
    assert nar.content_like.text == CONTENT_LIKE_SENTENCE


def test_only_the_planted_pauses_are_detectable_silences(media: MediaSet):
    nar = media.narration
    err = subprocess.run([_pu.FFMPEG, "-hide_banner", "-nostats", "-i", nar.wav, "-af",
                          "silencedetect=noise=-30dB:d=0.5", "-f", "null", "-"],
                         capture_output=True, text=True, **_pu.SUBPROCESS_FLAGS).stderr
    starts = [float(s) for s, _ in _SILENCE.findall(err) if s]
    durations = [float(d) for _, d in _SILENCE.findall(err) if d]
    assert len(starts) == len(nar.pauses) == 7, (starts, [p.start for p in nar.pauses])
    for found, pause in zip(starts, nar.pauses):
        assert abs(found - pause.start) <= 0.15, (found, pause.start)
    assert all(abs(d - PAUSE_S) <= 0.2 for d in durations), durations


# --- picture -------------------------------------------------------------------

def test_scene_video_has_the_planted_hard_cuts(media: MediaSet):
    shots = detect_shots(Path(media.video_16x9))
    seams = [s.start for s in shots[1:]]
    assert len(shots) == 6, shots
    for cut in media.scene_cuts:
        assert any(abs(cut - s) <= 0.1 for s in seams), (cut, seams)
    info = probe(Path(media.video_16x9))
    assert info.video is not None and (info.video.width, info.video.height) == (1920, 1080)
    assert abs(info.duration - media.duration) < 0.2
    v9 = probe(Path(media.video_9x16)).video
    assert v9 is not None and (v9.width, v9.height) == (1080, 1920)


def test_broll_and_loop_fixture_shapes(media: MediaSet):
    assert abs(probe(Path(media.broll)).duration - MEDIA.BROLL_SECONDS) < 0.2
    assert abs(probe(Path(media.loop_fixture)).duration - LOOP_FIXTURE_SECONDS) < 0.2


# --- beds ------------------------------------------------------------------------

def test_bench_bed_grid_matches_librosas_beats(media: MediaSet):
    bed = media.bench_bed
    assert abs(probe(Path(bed.path)).duration - BED_SECONDS) <= 0.1
    assert abs(bed.lufs - BED_LUFS) <= 0.5
    assert abs(bed.period - 0.6) < 1e-6
    expected = int(BED_SECONDS / bed.period)
    assert abs(len(bed.grid) - expected) <= 1
    on_grid = sum(1 for b in bed.detected if any(abs(b - g) <= 0.06 for g in bed.grid))
    assert on_grid / max(1, len(bed.detected)) >= 0.9, (on_grid, len(bed.detected))
    assert abs(len(bed.detected) - len(bed.grid)) <= 0.1 * len(bed.grid)


def test_ensure_preset_beds_writes_the_four_moods_into_a_temp_presets_dir(tmp_path: Path):
    paths = ensure_preset_beds(tmp_path, use_script=False, seconds=6.0)
    assert sorted(p.name for p in paths) == sorted(f"{n}.wav" for n in MEDIA.PRESET_BEDS)
    for p in paths:
        side = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        assert side["source"] == "procedural" and side["mood"] in {m for m, _ in MEDIA.PRESET_BEDS.values()}
        assert abs(side["bpm"] - MEDIA.PRESET_BEDS[p.stem][1]) < 1e-6
    # idempotent: a second call touches nothing
    mtimes = {p: p.stat().st_mtime_ns for p in paths}
    ensure_preset_beds(tmp_path, use_script=False, seconds=6.0)
    assert {p: p.stat().st_mtime_ns for p in paths} == mtimes


def test_add_music_loop_covers_the_200s_fixture(media: MediaSet, tmp_path: Path):
    """`add_music(loop=true)` (§4.9) on a 200 s video with a 180 s bed lays
    back-to-back clips until the extent is covered."""
    store = EDLStore(tmp_path / "loop")
    dur = probe(Path(media.loop_fixture)).duration
    dispatch(store, "add_clip", {"track": "v1", "src": media.loop_fixture, "in": 0, "out": dur, "start": 0})
    dispatch(store, "add_music", {"src": media.bench_bed.path, "start": 0, "volume_db": -14,
                                  "duck": True, "loop": True})
    music = store.edl.get_track("music")
    assert music is not None
    clips = sorted(music.clips, key=lambda c: c.start)
    assert len(clips) >= 2, "one 180 s bed cannot cover 200 s without looping"
    covered = sum(c.effective_duration for c in clips)
    assert covered >= 0.95 * store.edl.video_extent()
    for a, b in zip(clips, clips[1:]):
        assert abs((a.start + a.effective_duration) - b.start) < 0.05
    assert clips[-1].start + clips[-1].effective_duration <= store.edl.video_extent() + 0.05


# --- harness plumbing -------------------------------------------------------------

def test_socket_guard_trips_on_urlopen_and_allows_loopback():
    with EgressGuard() as guard:
        with pytest.raises(EgressAttempted):
            urllib.request.urlopen("http://example.com/", timeout=2)
        assert guard.attempts and "example.com" in guard.attempts[0]
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            client = socket.create_connection(server.getsockname(), timeout=2)
            client.close()
        finally:
            server.close()
    # restored: the patched functions are gone
    assert socket.create_connection.__name__ == "create_connection"
    assert socket.getaddrinfo.__module__ in ("socket", "_socket")


def test_clone_session_dir_rewrites_every_absolute_path(tmp_path: Path):
    src = tmp_path / "s_source"
    (src / "uploads" / "clip").mkdir(parents=True)
    (src / "snapshots").mkdir()
    (src / "cache" / "prompt_snap").mkdir(parents=True)
    media_path = src / "uploads" / "clip" / "clip.normalized.mp4"
    media_path.write_bytes(b"\x00")
    (src / "uploads" / "clip" / "ingest.json").write_text(json.dumps({"src": str(media_path), "transcript": {"segments": []}}))
    (src / "edl.json").write_text(json.dumps({"tracks": [{"id": "v1", "clips": [{"src": str(media_path)}]}]}))
    (src / "snapshots" / "00001_abc.json").write_text(json.dumps({"src": str(media_path)}))
    (src / "prompt_run.json").write_text("{}")
    (src / "chat.json").write_text("[]")
    (src / "cache" / "prompt_snap" / "x").write_text("x")
    dst = tmp_path / "s_clone"
    clone_session_dir(src, dst)
    for p in dst.rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        assert str(src) not in text, p
    assert json.loads((dst / "edl.json").read_text())["tracks"][0]["clips"][0]["src"] == str(dst / "uploads" / "clip" / "clip.normalized.mp4")
    assert (dst / "uploads" / "clip" / "clip.normalized.mp4").exists()
    assert not (dst / "prompt_run.json").exists() and not (dst / "chat.json").exists()
    assert not (dst / "cache" / "prompt_snap").exists()
    with pytest.raises(FileExistsError):
        clone_session_dir(src, dst)


def test_parse_sse_and_prompt_run_views():
    text = ('data: {"type": "brain", "status": "trying", "brain": "recipes", "label": "Recipes"}\n\n'
            'data: {"type": "brain", "status": "answered", "brain": "recipes", "label": "Recipes"}\n\n'
            'data: {"type": "plan", "plan": {"id": "p_1", "content_brain": null, "downloads_needed": []}}\n\n'
            'data: {"type": "text_delta", "text": "via Recipes — working"}\n\n'
            'data: {"type": "step", "index": 0, "total": 1, "tool": "add_caption_track", "status": "running"}\n\n'
            'data: {"type": "step", "index": 0, "total": 1, "tool": "add_caption_track", "status": "ok"}\n\n'
            'data: {"type": "verify", "plan_id": "p_1", "checks": [], "passed": 1, "total": 1}\n\n'
            'data: {"type": "op", "op": {"seq": 3}}\n\n'
            'data: not json\n\n'
            'data: {"type": "done"}\n\n')
    events = parse_sse(text)
    assert [e["type"] for e in events][:3] == ["brain", "brain", "plan"]
    assert events[-2]["type"] == "_unparseable"
    run = PromptRun(sid="s", prompt="add captions", turns=[events])
    assert run.brain == "recipes" and run.plan["id"] == "p_1"
    assert run.first_text.startswith("via ") and run.done_count == 1
    assert run.steps("ok")[0]["tool"] == "add_caption_track" and not run.steps("running")
    assert run.ran("add_caption_track") and not run.ran("auto_caption")
    assert len(run.ops) == 1 and run.verify["passed"] == 1 and run.errors == []


def test_manifest_round_trips_and_key_is_content_addressed(media: MediaSet):
    again = MediaSet.from_json(media.to_json())
    assert again == media
    assert Path(media.root).name == media_key()
    assert len(media_key()) == 12


@pytest.mark.tts
def test_hindi_variant_builds_when_a_voice_exists(media: MediaSet):
    backend = hindi_backend()
    if backend is None:
        pytest.skip("no Hindi TTS voice on this machine (Piper hi_IN not cached, `say -v Lekha` absent)")
    path, nar = media.hindi()
    assert path.exists() and nar.lang == "hi"
    assert len(nar.fillers) == 9 and "पसंद" in nar.content_like.text
    assert 40 <= nar.duration <= 160
