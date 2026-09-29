"""Final sweep 3, round 2 (timeline backend): captions made from a
voiceover stay on its words after a later main-track edit.

`auto_caption` placed a voiceover's cues on the layout clock
(`caption_sources.layout_time`) with nothing tying them to the voiceover.
The voiceover is one sound run pulled by the overlap before its start, while
each cue went through `render_time(cue.start)`: after a 1.0 s fade at 4.0
every caption past the seam showed a word (1.0 s) early and the last word had
none; after trimming a main-track clip before the voiceover the captions
followed the picture (`_follow_main_lane`) while the unlinked voiceover did
not, so every caption was 2 s early.

A cue made from a sound-lane or PIP source now records that clip
(`TextClip.linked_to`), is stored on that clip's own clock, is placed in the
export on the clip's render window (`clock.linked_text_windows`), and moves
only with that clip (`dispatch._follow_sounds`).

No model runs: `transcribe` is replaced by a fixed word list.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, sound_render_windows

W, H, FPS = 320, 180, 30
WORDS = 6


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("vo_caption_link")
    out: dict[str, Path] = {}
    for name, colour in (("A", "red"), ("B", "blue"), ("C", "green")):
        out[name] = d / f"{name}.mp4"
        _ff("-f", "lavfi", "-i", f"color=c={colour}:s={W}x{H}:d=4:r={FPS}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=4",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out[name]))
    out["vo"] = d / "vo.wav"
    _ff("-f", "lavfi", "-i", "sine=f=1500:r=48000:d=6", "-ac", "2", "-c:a", "pcm_s16le", str(out["vo"]))
    return out


class _FakeTx:
    def __init__(self, segments: list[dict]):
        self._d = {"language": "en", "duration": 6.0, "segments": segments}
        self.language = "en"
        self.duration = 6.0

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self._d))


@pytest.fixture
def fake_tx(monkeypatch):
    """Word k of the voiceover is spoken at source [k+0.5, k+0.8]; the main
    track is silent."""
    from video_ai_editor.ingest import transcribe as T
    segs = [{"start": k + 0.5, "end": k + 0.8, "text": f"w{k}",
             "words": [{"word": f"w{k}", "start": k + 0.5, "end": k + 0.8}]} for k in range(WORDS)]

    def fake(path, language=None, model_size=None, backend=None, task="transcribe",
             on_progress=None, should_cancel=None):
        return _FakeTx(segs if str(path).endswith(".wav") else [])

    monkeypatch.setattr(T, "transcribe", fake)


def _store(tmp: Path, media) -> EDLStore:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(id=n, src=str(media[n]), in_=0, out=4, start=4.0 * i)
            for i, n in enumerate("ABC")]),
        Track(id="vo", type="vo", clips=[Clip(id="vo1", src=str(media["vo"]), in_=0, out=6, start=2.0)]),
    ])
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json(by_alias=True))
    return EDLStore(tmp)


def _heard(edl: EDL) -> list[float]:
    """Where word k is heard in the export (the mix's own window rule)."""
    vo = edl.get_track("vo")
    c = next(x for x in vo.clips if x.id == "vo1")
    rs = sound_render_windows(list(vo.clips), list(edl.v1_seam_table()), edl.video_extent())["vo1"][0]
    return [round(rs + (k + 0.5 - c.in_), 3) for k in range(WORDS)]


def _shown(edl: EDL) -> list[float]:
    """Where each cue starts in the export: the renderer's window rule."""
    from video_ai_editor.render import clock
    linked = getattr(clock, "linked_text_windows", lambda _e: {})(edl)
    out = []
    for c in edl.get_track("captions").clips:
        win = linked[c.id] if c.id in linked else clock.render_window(clock.seam_table(edl), c.start, c.end)
        if win is not None:
            out.append(round(win[0], 3))
    return sorted(out)


def _captioned(tmp: Path, media) -> EDLStore:
    st = _store(tmp, media)
    dispatch(st, "auto_caption", {"style": "default"})
    assert _shown(st.edl) == _heard(st.edl) == [2.5, 3.5, 4.5, 5.5, 6.5, 7.5]
    return st


def test_voiceover_cues_are_linked_to_the_voiceover(tmp_path, media, fake_tx):
    st = _captioned(tmp_path / "s", media)
    assert {c.linked_to for c in st.edl.get_track("captions").clips} == {"vo1"}


def test_a_transition_before_the_words_keeps_them_captioned_on_time(tmp_path, media, fake_tx):
    st = _captioned(tmp_path / "s", media)
    dispatch(st, "add_transition", {"at": 4.0, "type": "fade", "duration": 1.0})
    assert _shown(st.edl) == _heard(st.edl) == [2.5, 3.5, 4.5, 5.5, 6.5, 7.5]


def test_trimming_a_main_clip_before_the_voiceover_keeps_its_captions(tmp_path, media, fake_tx):
    st = _captioned(tmp_path / "s", media)
    dispatch(st, "trim_clip", {"clip_id": "A", "in": 0, "out": 2})
    assert st.edl.get_clip("vo1")[1].start == pytest.approx(2.0)
    assert _shown(st.edl) == _heard(st.edl) == [2.5, 3.5, 4.5, 5.5, 6.5, 7.5]


def test_deleting_the_main_clip_under_the_voiceover_keeps_every_caption(tmp_path, media, fake_tx):
    """B lay under w1..w4; ripple-deleting it dropped/moved their cues with
    the picture while the voiceover still speaks them."""
    st = _captioned(tmp_path / "s", media)
    dispatch(st, "ripple_delete", {"clip_id": "B"})
    assert _shown(st.edl) == _heard(st.edl)
    assert len(st.edl.get_track("captions").clips) == WORDS


def test_moving_the_voiceover_moves_its_captions(tmp_path, media, fake_tx):
    st = _captioned(tmp_path / "s", media)
    dispatch(st, "move_clip", {"clip_id": "vo1", "new_start": 3.0})
    assert _heard(st.edl)[0] == pytest.approx(3.5)
    assert _shown(st.edl) == _heard(st.edl)


def test_a_split_voiceover_keeps_each_half_captioned(tmp_path, media, fake_tx):
    st = _captioned(tmp_path / "s", media)
    dispatch(st, "split_at", {"track": "vo", "time": 5.0})
    right = next(c for c in st.edl.get_track("vo").clips if c.id != "vo1")
    cues = st.edl.get_track("captions").clips
    assert {c.linked_to for c in cues if c.start >= 5.0} == {right.id}
    dispatch(st, "move_clip", {"clip_id": right.id, "new_start": 6.0})
    moved = sorted(round(c.start, 3) for c in st.edl.get_track("captions").clips if c.linked_to == right.id)
    assert moved == [6.5, 7.5, 8.5]


# ------------------------------------------------ the export itself

def _frames(path: Path) -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.uint8).reshape(-1, H, W, 3)


def _caption_onsets(path: Path) -> list[float]:
    """Frames where a caption APPEARS (white text in the lower third)."""
    on = [(f[H * 2 // 3:, :, :].min(axis=2) > 200).sum() > 30 for f in _frames(path)]
    return [round(i / FPS, 3) for i, v in enumerate(on) if v and (i == 0 or not on[i - 1])]


@pytest.mark.parametrize("edit", ["transition", "trim"])
def test_the_export_shows_each_caption_with_its_word(tmp_path, media, fake_tx, edit):
    from video_ai_editor.render.compositor import render_export
    st = _captioned(tmp_path / "s", media)
    if edit == "transition":
        dispatch(st, "add_transition", {"at": 4.0, "type": "fade", "duration": 1.0})
    else:
        dispatch(st, "trim_clip", {"clip_id": "A", "in": 0, "out": 2})
    res = render_export(st.edl, st.dir, height=H)
    onsets = _caption_onsets(Path(res.path))
    heard = _heard(st.edl)
    # One caption per word, each appearing on the frame its word starts.
    assert len(onsets) == len(heard), (onsets, heard)
    assert all(abs(a - b) <= 1.5 / FPS for a, b in zip(onsets, heard)), (onsets, heard)


def test_trimming_the_voiceovers_head_drops_the_cut_word_and_keeps_the_rest(tmp_path, media, fake_tx):
    st = _captioned(tmp_path / "s", media)
    dispatch(st, "trim_clip", {"clip_id": "vo1", "in": 1.0, "out": 6.0})
    texts = [c.text.lower() for c in st.edl.get_track("captions").clips]
    assert "w0" not in texts and len(texts) == WORDS - 1, texts
    assert _shown(st.edl) == _heard(st.edl)[1:]      # w1..w5, wherever the trim put them


def test_a_main_track_cue_is_not_linked(tmp_path, media, fake_tx, monkeypatch):
    """Only sound / PIP sources are linked: v1 captions keep the layout clock."""
    from video_ai_editor.ingest import transcribe as T
    seg = [{"start": 1.0, "end": 1.4, "text": "hi", "words": [{"word": "hi", "start": 1.0, "end": 1.4}]}]
    monkeypatch.setattr(T, "transcribe", lambda path, **_k: _FakeTx(seg if str(path).endswith("A.mp4") else []))
    st = _store(tmp_path / "s", media)
    st.edl.get_track("vo").clips = []
    st.commit("seed", {}, "seed")
    dispatch(st, "auto_caption", {"style": "default"})
    cues = st.edl.get_track("captions").clips
    assert cues and all(c.linked_to is None for c in cues)
    assert "linked_to" not in cues[0].model_dump(by_alias=True)
