"""Final sweep 4 (export truth): Generate captions on a project that ALREADY
has a main-track transition kept every voiceover / picture-in-picture word
heard during the cross-fade window.

THE DEFECT. A(0-4) B(4-8) C(8-12) with a 1.0 s fade at 4.0 has the render
window [3.0, 4.0) for that dissolve. `caption_sources.sound_clock` mapped a
sound-lane cue's start AND end through `layout_time`, which maps every
instant inside the window onto the seam (4.0) — so a cue that started and
ended inside the window (a 2x voiceover's word at render 3.25 held to 3.67;
a short phrase at 3.3 held to 3.82 at 1x) became the zero-length span
(4.0, 4.0), and `merge_tiers` — reached whenever v1 has media, because the
main track is itself a speech source — dropped it. The word was heard and
had no caption; captioning first and adding the transition afterwards
(tests/test_final_sweep3_r2_vo_caption_link.py's order) never showed it.

THE RULE NOW. The tiers are compared on the RENDER clock: a sound / PIP cue
where it is heard (`sound_clock`), a main-track cue where it is shown
(`dispatch._v1_render_clock`). Every cue keeps its own clock when stored.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, Transform, sound_render_windows
from video_ai_editor.render import clock
from video_ai_editor.render.compositor import render_export

W, H, FPS = 320, 180, 30
WORDS = 6

#: source word times: "spread": word k at k+0.5 (the r2 test's voiceover);
#: "phrase": a short word at 1.3 and the next at 1.9, so at 1x the cue for
#: w1 (heard at 3.3, held to 3.82) starts AND ends inside the fade's window.
TIMINGS = {
    "spread": [(k + 0.5, k + 0.8) for k in range(WORDS)],
    "phrase": [(0.5, 0.8), (1.3, 1.5), (1.9, 2.1), (2.6, 2.9), (3.5, 3.8), (4.5, 4.8)],
}


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("caption_xfade")
    out: dict[str, Path] = {}
    for name, colour in (("A", "red"), ("B", "blue"), ("C", "green")):
        out[name] = d / f"{name}.mp4"
        _ff("-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=4:r={FPS}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=4",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out[name]))
    out["vo"] = d / "vo.wav"
    _ff("-f", "lavfi", "-i", "sine=f=1500:r=48000:d=6", "-ac", "2", "-c:a", "pcm_s16le", str(out["vo"]))
    out["pip"] = d / "pip.mp4"
    _ff("-f", "lavfi", "-i", f"color=c=0x202020:s=160x90:d=6:r={FPS}",
        "-f", "lavfi", "-i", "sine=f=900:r=48000:d=6", "-pix_fmt", "yuv420p", "-c:v", "libx264",
        "-c:a", "aac", "-shortest", str(out["pip"]))
    return out


class _FakeTx:
    def __init__(self, segments: list[dict]):
        self._d = {"language": "en", "duration": 6.0, "segments": segments}
        self.language = "en"
        self.duration = 6.0

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self._d))


@pytest.fixture
def fake_tx(monkeypatch, request):
    """The voiceover / PIP speaks TIMINGS[param]; the main track is silent."""
    from video_ai_editor.ingest import transcribe as T
    timing = getattr(request, "param", "spread")
    segs = [{"start": a, "end": b, "text": f"w{k}", "words": [{"word": f"w{k}", "start": a, "end": b}]}
            for k, (a, b) in enumerate(TIMINGS[timing])]

    def fake(path, language=None, model_size=None, backend=None, task="transcribe",
             on_progress=None, should_cancel=None):
        p = str(path)
        return _FakeTx(segs if (p.endswith("vo.wav") or p.endswith("pip.mp4")) else [])

    monkeypatch.setattr(T, "transcribe", fake)
    return SimpleNamespace(words=TIMINGS[timing])


def _store(tmp: Path, media, *, source: str) -> EDLStore:
    tracks = [Track(id="v1", type="video", clips=[
        Clip(id=n, src=str(media[n]), in_=0, out=4, start=4.0 * i) for i, n in enumerate("ABC")])]
    if source == "vo":
        tracks.append(Track(id="vo", type="vo", clips=[
            Clip(id="vo1", src=str(media["vo"]), in_=0, out=6, start=2.0)]))
    else:
        tracks.append(Track(id="v2", type="video", z=1, clips=[
            Clip(id="pip1", src=str(media["pip"]), in_=0, out=6, start=2.0,
                 transform=Transform(x=W * 0.8, y=H * 0.25, scale=0.4))]))
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=tracks)
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json(by_alias=True))
    return EDLStore(tmp)


def _heard(edl: EDL, source: str, words) -> list[float]:
    """Where each word is heard in the export (the renderer's window rules)."""
    if source == "vo":
        vo = edl.get_track("vo")
        c = next(x for x in vo.clips if x.id == "vo1")
        rs, re = sound_render_windows(list(vo.clips), list(edl.v1_seam_table()), edl.video_extent())["vo1"]
    else:
        c = edl.get_clip("pip1")[1]
        rs, re = clock.render_window(clock.seam_table(edl), c.start, c.start + c.effective_duration)
    sp = c.speed_factor
    return [round(rs + (a - c.in_) / sp, 3) for a, _b in words if rs + (a - c.in_) / sp < re]


def _shown(edl: EDL) -> list[float]:
    """Where each cue starts in the export: the renderer's window rule."""
    linked = clock.linked_text_windows(edl)
    out = []
    for c in edl.get_track("captions").clips:
        win = linked[c.id] if c.id in linked else clock.render_window(clock.seam_table(edl), c.start, c.end)
        if win is not None:
            out.append(round(win[0], 3))
    return sorted(out)


def _frames(path: Path) -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.uint8).reshape(-1, H, W, 3)


def _caption_onsets(path: Path) -> list[float]:
    """Frames where a caption APPEARS (white text in the lower third)."""
    on = [(f[H * 2 // 3:, :, :].min(axis=2) > 200).sum() > 30 for f in _frames(path)]
    return [round(i / FPS, 3) for i, v in enumerate(on) if v and (i == 0 or not on[i - 1])]


@pytest.mark.parametrize("source,speed,fake_tx,missing", [
    ("vo", 2.0, "spread", "w2"),      # a 2x voiceover: w2 heard at 3.25, its cue held to 3.67
    ("vo", 1.0, "phrase", "w1"),      # 1x: a short word at 3.3, its cue held to 3.82
    ("pip", 2.0, "spread", "w2"),     # the same words spoken on a picture-in-picture lane
], indirect=["fake_tx"])
def test_generate_captions_after_a_transition_keeps_the_word_heard_during_the_crossfade(
        tmp_path, media, fake_tx, source, speed, missing):
    st = _store(tmp_path / "s", media, source=source)
    dispatch(st, "add_transition", {"at": 4.0, "type": "fade", "duration": 1.0})
    if speed != 1.0:
        dispatch(st, "set_speed", {"clip_id": "vo1" if source == "vo" else "pip1",
                                   "factor": speed, "keep_pitch": False})
    dispatch(st, "auto_caption", {"style": "default"})
    heard = _heard(st.edl, source, fake_tx.words)
    cues = st.edl.get_track("captions").clips
    spoken = " ".join(c.text for c in cues).split()
    # every word that is heard has a caption — the one inside the window too
    assert missing in spoken, (missing, [c.text for c in cues])
    assert len(spoken) == len(heard), (spoken, heard)
    # every cue starts on one of its words, where the word is heard
    shown = _shown(st.edl)
    assert all(any(abs(s - h) < 1e-3 for h in heard) for s in shown), (shown, heard)
    assert shown[0] == heard[0]
    # and the export shows each cue where the model says
    res = render_export(st.edl, st.dir, height=H)
    onsets = _caption_onsets(Path(res.path))
    assert len(onsets) == len(shown), (onsets, shown)
    assert all(abs(a - b) <= 1.5 / FPS for a, b in zip(onsets, shown)), (onsets, shown)


def test_the_same_project_captioned_before_the_transition_keeps_every_word(tmp_path, media, fake_tx):
    """The control (r2's order): caption first, then add the transition."""
    st = _store(tmp_path / "s", media, source="vo")
    dispatch(st, "auto_caption", {"style": "default"})
    dispatch(st, "add_transition", {"at": 4.0, "type": "fade", "duration": 1.0})
    texts = [c.text for c in st.edl.get_track("captions").clips]
    assert texts == [f"w{k}" for k in range(WORDS)], texts
    assert _shown(st.edl) == _heard(st.edl, "vo", fake_tx.words)


def test_a_main_track_cue_over_a_voiceover_word_still_yields_to_it(tmp_path, media, monkeypatch):
    """The tiers are now compared where they are heard: a main-track word
    spoken while the voiceover speaks loses to the voiceover; one spoken
    when it does not is kept — measured across the seam."""
    from video_ai_editor.ingest import transcribe as T
    vo_segs = [{"start": 1.3, "end": 1.6, "text": "vo", "words": [{"word": "vo", "start": 1.3, "end": 1.6}]}]
    # B's word at source 0.1 plays at render 3.1 (B starts on screen at 3.0),
    # under the voiceover's "vo" heard at 3.3-3.82; B's word at 1.5 plays at
    # render 4.5, clear of it.
    b_segs = [{"start": 0.1, "end": 0.3, "text": "under", "words": [{"word": "under", "start": 0.1, "end": 0.3}]},
              {"start": 1.5, "end": 1.8, "text": "clear", "words": [{"word": "clear", "start": 1.5, "end": 1.8}]}]

    def fake(path, **_k):
        p = str(path)
        return _FakeTx(vo_segs if p.endswith("vo.wav") else b_segs if p.endswith("B.mp4") else [])

    monkeypatch.setattr(T, "transcribe", fake)
    st = _store(tmp_path / "s", media, source="vo")
    dispatch(st, "add_transition", {"at": 4.0, "type": "fade", "duration": 1.0})
    dispatch(st, "auto_caption", {"style": "default", "max_cps": 100})
    cues = {c.text: c for c in st.edl.get_track("captions").clips}
    assert "vo" in cues and "clear" in cues and "under" not in cues, sorted(cues)
    assert cues["vo"].linked_to == "vo1" and cues["clear"].linked_to is None
    # the main-track cue keeps the layout clock: B's source 1.5 s is layout 5.5
    assert cues["clear"].start == pytest.approx(5.5)
