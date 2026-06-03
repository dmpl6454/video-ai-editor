"""Transition catalog: resolver unit tests + real 2-clip xfade renders.

The headline guarantee: every name the catalog advertises actually renders.
The old schema shipped `slide`/`zoom`/`glitch`/`whip`/`spin` but passed them
raw to ffmpeg's xfade, which only accepts `fade`/`dissolve` among those —
the other five crashed. These tests lock that door shut.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas, Transition
from video_ai_editor.render import render_preview
from video_ai_editor.render import transitions as T


# ---------- resolver unit tests ----------

def test_catalog_has_breadth():
    names = T.all_names()
    assert len(names) >= 45, f"expected a broad catalog, got {len(names)}"
    # The viral staples must all be present.
    for must in ("fade", "dissolve", "slideleft", "zoomin", "circleopen",
                 "radial", "pixelize", "glitch", "whip", "spin"):
        assert must in names, f"{must} missing from catalog"


def test_legacy_names_resolve_not_crash():
    # The five names the old schema advertised must all resolve to something
    # ffmpeg accepts (native name, or custom+expr).
    for legacy in ("slide", "zoom", "glitch", "whip", "spin"):
        name, expr = T.resolve_transition(legacy)
        if name == "custom":
            assert expr, f"{legacy} resolved to custom with no expr"
        else:
            assert name in T.NATIVE.values(), f"{legacy} → {name} not a native xfade name"


def test_unknown_resolves_to_fade():
    name, expr = T.resolve_transition("definitely-not-a-transition")
    assert name == "fade" and expr is None


def test_glitch_is_custom_expr():
    name, expr = T.resolve_transition("glitch")
    assert name == "custom"
    assert expr and "P" in expr  # references progress


def test_is_valid():
    assert T.is_valid("slideleft")
    assert T.is_valid("whip")        # alias
    assert T.is_valid("glitch")      # custom
    assert not T.is_valid("nope")


# ---------- real render tests ----------

def _mk_video(path: Path, *, color: str, duration: float = 2.0):
    keyed = path.with_suffix(".keyed.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c={color}:s=320x180:d={duration}:r=30",
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


# A representative slice across every category, plus the three stylized ones.
_SAMPLE = [
    "fade", "dissolve", "fadeblack", "fadewhite",
    "slideleft", "slideright", "smoothup",
    "wipeleft", "wipedown", "diagtl",
    "coverleft", "revealright",
    "circleopen", "circleclose", "radial", "rectcrop",
    "hrslice", "vuslice",
    "squeezeh", "zoomin",
    "pixelize", "hblur",
    "hrwind",
    # formerly-broken — these are the whole point:
    "slide", "zoom", "glitch", "whip", "spin",
]


@pytest.mark.parametrize("ttype", _SAMPLE)
def test_transition_renders_valid_mp4(tmp_path: Path, ttype: str):
    a = tmp_path / "a.mp4"; _mk_video(a, color="red")
    b = tmp_path / "b.mp4"; _mk_video(b, color="blue")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(a), in_=0, out=2, start=0, id="c1"),
            Clip(src=str(b), in_=0, out=2, start=2, id="c2"),
        ], transitions=[Transition(at=2.0, type=ttype, duration=0.5)]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    out = render_preview(store.edl, tmp_path, height=180).path

    info = _probe(out)
    streams = {s["codec_type"] for s in info["streams"]}
    assert "video" in streams, f"{ttype}: no video stream"
    # Two 2s clips with a 0.5s overlap → ~3.5s.
    dur = float(info["format"]["duration"])
    assert dur > 3.0, f"{ttype}: duration {dur:.2f}s too short (xfade likely failed)"


def test_add_transition_rejects_unknown(tmp_path: Path):
    from video_ai_editor.agent.dispatch import dispatch
    a = tmp_path / "a.mp4"; _mk_video(a, color="red")
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src=str(a), in_=0, out=2, start=0, id="c1"),
        ]),
    ])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    with pytest.raises(ValueError, match="unknown transition"):
        dispatch(store, "add_transition", {"at": 2.0, "type": "kapow"})


def test_list_transitions_returns_catalog(tmp_path: Path):
    from video_ai_editor.agent.dispatch import dispatch
    (tmp_path / "edl.json").write_text(
        EDL(canvas=Canvas(w=320, h=180, fps=30)).model_dump_json())
    store = EDLStore(tmp_path)
    out = dispatch(store, "list_transitions", {})
    assert out["count"] >= 45
    assert "categories" in out["catalog"]
    assert "stylized" in out["catalog"]["categories"]
