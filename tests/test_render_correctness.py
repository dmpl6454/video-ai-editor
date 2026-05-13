"""Render-correctness tests: each effect / mask / transition / PiP combo
must produce a valid mp4 with the expected dimensions, duration, and
non-empty streams."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (
    EDL, Track, Clip, Canvas, Mask, ChromaKey, Effect, Transition, Transform,
)
from video_ai_editor.render import render_preview


def _mk_video(path: Path, *, color: str = "blue", duration: float = 2.0,
              w: int = 320, h: int = 180):
    keyed = path.with_suffix(".keyed.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c={color}:s={w}x{h}:d={duration}:r=30",
         "-pix_fmt", "yuv420p", str(keyed)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(keyed),
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def _render(tmp_path: Path, edl: EDL) -> Path:
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    return render_preview(store.edl, tmp_path, height=180).path


@pytest.mark.parametrize("effect_type,params", [
    ("color", {"brightness": 0.1, "contrast": 1.2, "sat": 1.3, "temp": 0.2}),
    ("blur", {"radius": 6}),
    ("sharpen", {"amount": 1.0}),
    ("vignette", {"angle": 0.785}),
    ("grain", {"strength": 15}),
    ("vintage", {}),
    ("vhs", {}),
    ("glow", {"strength": 0.4}),
    ("hflip", {}),
    ("vflip", {}),
    ("rgb_split", {"offset": 4}),
])
def test_effect_renders_valid_mp4(tmp_path: Path, effect_type: str, params: dict):
    src = tmp_path / "src.mp4"; _mk_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1",
                 effects=[Effect(type=effect_type, params=params)]),
        ]),
    ])
    out = _render(tmp_path, edl)
    info = _probe(out)
    streams = {s["codec_type"] for s in info["streams"]}
    assert "video" in streams, f"effect={effect_type}: no video stream"
    assert "audio" in streams, f"effect={effect_type}: no audio stream"
    assert float(info["format"]["duration"]) > 1.5


@pytest.mark.parametrize("mask_type", ["circle", "rectangle", "linear"])
def test_mask_renders_valid_mp4(tmp_path: Path, mask_type: str):
    src = tmp_path / "src.mp4"; _mk_video(src)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1",
                 mask=Mask(type=mask_type, feather=8.0)),
        ]),
    ])
    out = _render(tmp_path, edl)
    assert out.stat().st_size > 1024


def test_chroma_key_renders_valid_mp4(tmp_path: Path):
    src = tmp_path / "src.mp4"; _mk_video(src, color="green")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1",
                 chromakey=ChromaKey(color="#00FF00", similarity=0.4,
                                     smoothness=0.1, spill_suppress=0.4)),
        ]),
    ])
    out = _render(tmp_path, edl)
    assert _probe(out)["streams"]


@pytest.mark.parametrize("ttype", ["fade", "dissolve"])
def test_transition_between_two_clips(tmp_path: Path, ttype: str):
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"
    _mk_video(a, color="blue"); _mk_video(b, color="red")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(a), in_=0, out=2, start=0, id="c1"),
            Clip(src=str(b), in_=0, out=2, start=2, id="c2"),
        ], transitions=[Transition(at=2.0, type=ttype, duration=0.5)]),
    ])
    out = _render(tmp_path, edl)
    info = _probe(out)
    assert float(info["format"]["duration"]) > 3.0


def test_pip_overlay_renders_with_correct_dims(tmp_path: Path):
    """V2 clip with transform should overlay on V1 base."""
    base = tmp_path / "base.mp4"; pip = tmp_path / "pip.mp4"
    _mk_video(base, color="blue", w=320, h=180)
    _mk_video(pip, color="red", w=160, h=90)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(base), in_=0, out=2, start=0, id="b1"),
        ]),
        Track(id="v2", type="video", z=1, clips=[
            Clip(src=str(pip), in_=0, out=2, start=0, id="p1",
                 transform=Transform(x=240, y=45, scale=0.6)),
        ]),
    ])
    out = _render(tmp_path, edl)
    info = _probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert v["width"] == 320 and v["height"] == 180


def test_keyframed_scale_renders(tmp_path: Path):
    src = tmp_path / "src.mp4"; _mk_video(src)
    from video_ai_editor.edl.schema import Keyframe
    tx = Transform(scale=Keyframe(keyframes=[(0.0, 1.0), (2.0, 1.5)],
                                   interp="linear"))
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=2, start=0, id="c1", transform=tx),
        ]),
    ])
    out = _render(tmp_path, edl)
    assert out.stat().st_size > 1024


def test_speed_change_changes_duration(tmp_path: Path):
    src = tmp_path / "src.mp4"; _mk_video(src, duration=3.0)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(src), in_=0, out=3, start=0, id="c1", speed=2.0),
        ]),
    ])
    out = _render(tmp_path, edl)
    dur = float(_probe(out)["format"]["duration"])
    # 3s @ 2× = 1.5s ± codec rounding
    assert 1.2 < dur < 1.8, f"expected ~1.5s, got {dur}"
