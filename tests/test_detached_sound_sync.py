"""A detached sound follows its OWN picture across main-track transitions
(final QA, run 2).

THE DEFECT: sound lanes group abutting clips into RUNS pulled by the overlap
before the run's first clip (`schema.sound_pulls`), so a split voiceover
stays back to back. Detaching the sound of two adjacent v1 clips with a
transition at their cut put both sounds on one lane, back to back — one run —
so the second sound took the FIRST sound's pull and missed the pull of the
transition between them: it played late by the transition's length and lost
its tail at the programme end, while its picture was on time.

THE RULE NOW: a sound that carries `linked_to` starts a run of its own, so it
is pulled by `overlap_before(start)` — exactly where its picture starts. The
two sounds overlap during the crossfade, like a J/L cut.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, sound_pulls, sound_render_windows
from video_ai_editor.render import clock, render_export

W, H, FPS = 320, 180, 30
SR = 48000
#: Source-time beeps (start, length) per source; c0 is silent.
BEEPS = {"c0": [], "c1": [(1.0, 0.1)], "c2": [(0.25, 0.1), (1.6, 0.1)]}


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("dsfx")
    out = {}
    for name, colour in (("c0", "red"), ("c1", "blue"), ("c2", "green")):
        gate = "+".join(f"between(t\\,{s}\\,{s + n})" for s, n in BEEPS[name]) or "0"
        _run(["-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=2:r={FPS}",
              "-f", "lavfi", "-i", f"aevalsrc=0.5*sin(2*PI*1000*t)*({gate}):s={SR}:c=stereo:d=2",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / f"{name}.mp4")])
        out[name] = d / f"{name}.mp4"
    return out


def _store(tmp_path: Path, fx, detach: list[str]) -> EDLStore:
    """v1 c0|c1|c2 (2 s each), fades of 0.5 at 2.0 and 4.0, then the
    Timeline's 'Detach audio' on each clip of `detach`, in order."""
    store = EDLStore(tmp_path / "s")
    store.edl.canvas = Canvas(w=W, h=H, fps=FPS, loudness_lufs=None)
    store.edl.get_track("v1").clips = [
        Clip(id=n, src=str(fx[n]), in_=0.0, out=2.0, start=2.0 * i) for i, n in enumerate(("c0", "c1", "c2"))]
    store.edl.recompute_duration()
    store.commit("init", {}, "init")
    for at in (2.0, 4.0):
        dispatch(store, "add_transition", {"at": at, "type": "fade", "duration": 0.5})
    for cid in detach:
        dispatch(store, "detach_audio", {"clip_id": cid})
    return store


def _sounds(store: EDLStore) -> tuple[list[Clip], dict[str, Clip]]:
    lanes = [t for t in store.edl.tracks if t.type == "audio" and t.clips]
    assert len(lanes) == 1, "both sounds land on one lane, back to back"
    clips = lanes[0].clips
    return clips, {c.linked_to: c for c in clips}


def test_two_detached_sounds_back_to_back_each_follow_their_picture(tmp_path, fx):
    store = _store(tmp_path, fx, ["c1", "c2"])
    edl = store.edl
    clips, by_pic = _sounds(store)
    seams = edl.v1_seam_table()
    wins = sound_render_windows(clips, seams, edl.video_extent())
    for pic in ("c1", "c2"):
        rs = clock.render_time(edl, 2.0 if pic == "c1" else 4.0)
        assert wins[by_pic[pic].id] == pytest.approx((rs, rs + 2.0)), pic
    pulls = sound_pulls(clips, seams)
    assert pulls[by_pic["c1"].id] == pytest.approx(0.5)
    assert pulls[by_pic["c2"].id] == pytest.approx(1.0)


def test_unlinked_abutting_pieces_still_move_as_one_run(tmp_path, fx):
    """The run rule for a split voiceover or a looped bed is unchanged: the
    same two clips WITHOUT `linked_to` stay back to back."""
    store = _store(tmp_path, fx, ["c1", "c2"])
    clips, _ = _sounds(store)
    plain = [c.model_copy(update={"linked_to": None}) for c in clips]
    pulls = sound_pulls(plain, store.edl.v1_seam_table())
    assert sorted(pulls.values()) == pytest.approx([0.5, 0.5])


def _bursts(path: Path) -> list[float]:
    """Onsets (seconds) of the 1 kHz beeps in the export's audio."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"], check=True, capture_output=True).stdout
    a = np.abs(np.frombuffer(raw, dtype=np.float32))
    hop = SR // 200                                   # 5 ms blocks
    env = np.array([a[i:i + hop].max() for i in range(0, len(a) - hop, hop)])
    on = env > 0.15
    return [i * hop / SR for i in range(len(on)) if on[i] and (i == 0 or not on[i - 1])]


@pytest.mark.parametrize("detach", [[], ["c2"], ["c1", "c2"]])
def test_every_beep_is_heard_with_its_picture(tmp_path, fx, detach):
    """Decoded: each source-time beep is heard where the picture shows that
    source time — c1's at 1.5 + 1.0, c2's at 3.0 + 0.25 and 3.0 + 1.6 —
    whether or not the sounds are detached (the last beep was lost when both
    were, and the others played 0.5 s late)."""
    store = _store(tmp_path, fx, detach)
    out = render_export(store.edl, tmp_path / "out", height=H).path
    expected = [clock.render_time(store.edl, 2.0) + 1.0,
                clock.render_time(store.edl, 4.0) + 0.25,
                clock.render_time(store.edl, 4.0) + 1.6]
    assert expected == pytest.approx([2.5, 3.25, 4.6])
    heard = _bursts(out)
    assert len(heard) == len(expected), (detach, heard)
    assert heard == pytest.approx(expected, abs=0.025), (detach, heard)
