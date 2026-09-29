"""Final sweep 3, timeline backend (r1): four main-lane edits that left the
rest of the timeline behind.

* smooth_slow_motion lengthens a v1 clip and ripples v1, but no layer and no
  detached sound followed: a PIP / title over the NEXT clip ended up over the
  slowed footage, and that clip's detached sound played 2 s early. Layers
  over the slowed clip itself (its captions) now retime with its footage.
* set_property speed / in / out on a v1 clip set the field and nothing else:
  a speed-up or trim left a black hole in the export, a slow-down stored two
  overlapping v1 clips that the export played one after the other.
* remove_silences read a failed or killed ffmpeg as "no silences" (or as
  the few it printed before dying) and reported success.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, TextClip, Track


def _ff(*a: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    out = {}
    for name, color in (("red", "red"), ("blue", "blue")):
        p = d / f"{name}.mp4"
        _ff("-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=30:d=2",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(p))
        out[name] = p
    p = d / "bars.mp4"                      # no dark frame anywhere
    _ff("-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=6", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", str(p))
    out["bars"] = p
    return out


def _store(edl: EDL) -> EDLStore:
    tmp = Path(tempfile.mkdtemp())
    edl.recompute_duration()
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


def _span(store: EDLStore, cid: str) -> tuple[float, float]:
    c = store.edl.get_clip(cid)[1]
    end = c.end if hasattr(c, "end") else c.start + c.effective_duration
    return round(float(c.start), 6), round(float(end), 6)


# ------------------------------------------------------------ smooth slow-mo

def _stub_rife(monkeypatch) -> None:
    """RIFE's output shape without RIFE: the same picture at `factor`x the
    frames, played at the original fps (so `factor`x as long)."""
    from video_ai_editor.ai import rife

    def fake(src: Path, cache_dir: Path, *, factor: int = 2, **_kw) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{src.stem}_x{factor}.mp4"
        # The fixtures are 2 s at 30 fps: exactly 60 * factor frames out.
        _ff("-i", str(src), "-vf", f"setpts={factor}*PTS,fps=30,tpad=stop_mode=clone:stop=30",
            "-frames:v", str(60 * factor), "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out))
        return out

    monkeypatch.setattr(rife, "smooth_slow_motion", fake)


def _slowmo_store(media) -> EDLStore:
    a, b = str(media["red"]), str(media["blue"])
    return _store(EDL(canvas=Canvas(w=160, h=90, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="m0", src=a, in_=0, out=2, start=0),
                                            Clip(id="m1", src=b, in_=0, out=2, start=2)]),
        Track(id="v2", type="video", z=5, clips=[Clip(id="pip", src=a, in_=0, out=0.5, start=2.5)]),
        Track(id="tx", type="text", z=6, clips=[TextClip(id="t_m1", text="over blue", start=2.5, end=3.5)]),
        Track(id="cap", type="captions", z=8, clips=[
            TextClip(id="c_m0", text="said over red", start=1.0, end=1.5, role="caption")]),
        Track(id="a1", type="audio", clips=[Clip(id="snd", src=b, in_=0, out=2, start=2.0, linked_to="m1")]),
    ]))


def test_smooth_slow_motion_carries_later_layers_and_linked_sound(media, monkeypatch):
    _stub_rife(monkeypatch)
    store = _slowmo_store(media)
    dispatch(store, "smooth_slow_motion", {"clip_id": "m0", "factor": 2})
    assert [_span(store, c) for c in ("m0", "m1")] == [(0.0, 4.0), (4.0, 6.0)]
    assert _span(store, "pip")[0] == 4.5                 # still over m1 (blue)
    assert _span(store, "t_m1") == (4.5, 5.5)
    assert _span(store, "snd")[0] == 4.0                 # in sync with m1 again


def test_smooth_slow_motion_retimes_captions_over_the_slowed_clip(media, monkeypatch):
    """RIFE's file plays m0's speech 2x slower: its caption at [1.0, 1.5]
    is now said at [2.0, 3.0]."""
    _stub_rife(monkeypatch)
    store = _slowmo_store(media)
    dispatch(store, "smooth_slow_motion", {"clip_id": "m0", "factor": 2})
    assert _span(store, "c_m0") == (2.0, 3.0)


def test_smooth_slow_motion_and_what_follows_are_one_undo_step(media, monkeypatch):
    _stub_rife(monkeypatch)
    store = _slowmo_store(media)
    before = {c: _span(store, c) for c in ("m0", "m1", "pip", "t_m1", "c_m0", "snd")}
    dispatch(store, "smooth_slow_motion", {"clip_id": "m0", "factor": 2})
    dispatch(store, "undo", {})
    assert {c: _span(store, c) for c in before} == before


# ----------------------------------------------- set_property on the main lane

def _pair_store(media) -> EDLStore:
    src = str(media["bars"])
    return _store(EDL(canvas=Canvas(w=160, h=90, fps=30, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="a", src=src, in_=0, out=2, start=0),
                                            Clip(id="b", src=src, in_=2, out=4, start=2)]),
    ]))


def _v1_contiguous(store: EDLStore) -> None:
    a, b = store.edl.get_clip("a")[1], store.edl.get_clip("b")[1]
    assert b.start == pytest.approx(a.start + a.effective_duration, abs=1e-6)


@pytest.mark.parametrize("path,value,a_len", [
    ("speed", 2.0, 1.0), ("out", 1.0, 1.0), ("in", 1.0, 1.0), ("speed", 0.5, 4.0),
])
def test_set_property_timing_on_v1_keeps_the_main_lane_magnetic(media, path, value, a_len):
    store = _pair_store(media)
    dispatch(store, "set_property", {"clip_id": "a", "path": path, "value": value})
    a = store.edl.get_clip("a")[1]
    assert a.effective_duration == pytest.approx(a_len)
    _v1_contiguous(store)
    store.edl.recompute_duration()
    assert store.edl.duration == pytest.approx(a_len + 2.0)


def test_set_property_start_refuses_an_overlap_on_the_main_lane(media):
    store = _pair_store(media)
    with pytest.raises(ValueError, match="overlap"):
        dispatch(store, "set_property", {"clip_id": "b", "path": "start", "value": 1.0})
    assert _span(store, "b") == (2.0, 4.0)


def test_set_property_speed_up_on_v1_exports_no_black_hole(media, tmp_path):
    from video_ai_editor.render import render_export
    store = _pair_store(media)
    dispatch(store, "set_property", {"clip_id": "a", "path": "speed", "value": 2})
    r = render_export(store.edl, tmp_path / "out", height=90)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(r.path), "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, 90, 160)
    assert len(fr) == 90                                  # 1 s + 2 s at 30 fps
    assert not [k for k in range(len(fr)) if fr[k].mean() < 3]


# ------------------------------------------------------------ remove_silences

def test_remove_silences_raises_when_silencedetect_fails(media, monkeypatch):
    store = _pair_store(media)
    n_ops = len(store.ops.ops)
    before = store.edl.hash()

    class _Killed:
        returncode, stdout = -9, ""
        # What a killed run printed before it died still reads as silences.
        stderr = "silence_start: 0.2\nsilence_end: 1.5\n"

    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: _Killed()
                        if "silencedetect" in " ".join(map(str, argv)) else real_run(argv, *a, **kw))
    with pytest.raises(RuntimeError, match="silence detection failed"):
        dispatch(store, "remove_silences", {"min_dur": 0.5})
    assert len(store.ops.ops) == n_ops
    assert store.edl.hash() == before
