"""Follow-up round: sticker portability/selectability, crop-to-fill, overlay order.

Each of these was silent — nothing errored, the user just found something
missing, unreachable or letterboxed.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, Sticker, empty_edl
from video_ai_editor.render import render_preview
from video_ai_editor.storage import new_session_id, session_dir
from video_ai_editor.storage_project import _media_srcs, load_project, save_project


def _mk_video(path: Path, w: int = 640, h: int = 360, dur: float = 2.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={dur}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)], check=True, capture_output=True)
    return path


def _mk_png(path: Path, color: str = "red", size: int = 64) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={color}:s={size}x{size}:d=1", "-frames:v", "1", str(path)],
        check=True, capture_output=True)
    return path


# ------------------------------------------------- C8-X1: .vae sticker media

def test_media_srcs_includes_sticker_images(tmp_path):
    """`_media_srcs` collected only Clip, so no sticker PNG was ever bundled."""
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    edl.get_track("v1").clips.append(Clip(src="/m/v.mp4", in_=0.0, out=1.0, start=0.0))
    edl.get_track("stickers").clips.append(
        Sticker(src="/m/logo.png", start=0.0, end=1.0))
    assert _media_srcs(edl) == {"/m/v.mp4", "/m/logo.png"}


def test_vae_roundtrip_preserves_stickers_after_source_is_gone(tmp_path):
    """The real failure mode: the .vae opened on another machine (or after the
    source session was deleted) had every sticker's file missing — and the
    renderer skips a missing sticker SILENTLY, so they just weren't there."""
    vid = _mk_video(tmp_path / "v.mp4")
    png = _mk_png(tmp_path / "logo.png")

    sid = new_session_id()
    sd = session_dir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    edl.get_track("v1").clips.append(Clip(src=str(vid), in_=0.0, out=2.0, start=0.0))
    edl.get_track("stickers").clips.append(Sticker(src=str(png), start=0.0, end=2.0))
    edl.recompute_duration()
    (sd / "edl.json").write_text(edl.model_dump_json())
    EDLStore(sd)

    vae = save_project(sid, tmp_path / "proj.vae")
    bundled = [n for n in zipfile.ZipFile(vae).namelist() if n.startswith("media/")]
    assert any(n.endswith("logo.png") for n in bundled), bundled

    # Simulate moving the .vae elsewhere: destroy the session AND the original.
    shutil.rmtree(sd, ignore_errors=True)
    png.unlink()

    new_sid = load_project(vae)
    try:
        restored = EDLStore(session_dir(new_sid))
        sticker = restored.edl.get_track("stickers").clips[0]
        assert Path(sticker.src).exists(), "sticker image lost on .vae round-trip"
    finally:
        shutil.rmtree(session_dir(new_sid), ignore_errors=True)


# ------------------------------------------- A2: unselectable stacked stickers

def test_add_sticker_cascades_off_an_exact_position_collision(tmp_path):
    """All three insert paths hard-code the same canvas point, and the client
    hit-box derives from x/y/scale — so back-to-back stickers had pixel-identical
    hit boxes and only the top one could ever be clicked or deleted."""
    png = _mk_png(tmp_path / "s.png")
    edl = empty_edl(Canvas(w=1080, h=1920, fps=30))
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)

    pos = [540.0, 1056.0]  # what StickerPanel / Timeline / main.py all send
    for _ in range(3):
        dispatch(store, "add_sticker",
                 {"src": str(png), "start": 0.0, "end": 3.0, "position": list(pos)})

    pts = [(s.transform.x, s.transform.y)
           for s in store.edl.get_track("stickers").clips]
    assert len(pts) == 3
    assert len(set(pts)) == 3, f"stickers share a hit box: {pts}"


def test_explicit_position_is_still_honoured_when_free(tmp_path):
    """The cascade must only fire on a collision — an explicit free position is
    a deliberate placement."""
    png = _mk_png(tmp_path / "s.png")
    edl = empty_edl(Canvas(w=1080, h=1920, fps=30))
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    dispatch(store, "add_sticker",
             {"src": str(png), "start": 0.0, "end": 3.0, "position": [100.0, 200.0]})
    s = store.edl.get_track("stickers").clips[0]
    assert (s.transform.x, s.transform.y) == (100.0, 200.0)


# ------------------------------------------------------- P13-a: crop to fill

def _fully_black_rows(path: Path, w: int, h: int) -> int:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-"],
        capture_output=True, check=True).stdout
    return sum(1 for y in range(h) if max(raw[y * w:(y + 1) * w]) < 12)


@pytest.mark.parametrize("fit,expect_bars", [("contain", True), ("cover", False)])
def test_fit_cover_fills_the_frame(tmp_path, fit, expect_bars):
    """A landscape source on a vertical canvas could ONLY be letterboxed — the
    toolbar's aspect buttons resize the canvas and nothing cropped to fill."""
    src = _mk_video(tmp_path / "land.mp4", w=640, h=360)
    sess = tmp_path / f"s_{fit}"
    sess.mkdir()
    edl = empty_edl(Canvas(w=360, h=640, fps=30))
    c = Clip(src=str(src), in_=0.0, out=2.0, start=0.0)
    c.fit = fit
    edl.get_track("v1").clips.append(c)
    edl.recompute_duration()
    res = render_preview(edl, sess, height=360)
    bars = _fully_black_rows(res.path, 360, 640)
    if expect_bars:
        assert bars > 50, f"contain should letterbox, got {bars} black rows"
    else:
        assert bars == 0, f"cover should fill the frame, got {bars} black rows"


def test_fit_defaults_to_contain_so_existing_edls_are_unchanged():
    assert Clip(src="/x.mp4", in_=0.0, out=1.0).fit == "contain"


def test_set_clip_fit_rejects_an_unknown_mode(tmp_path):
    src = _mk_video(tmp_path / "v.mp4")
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=2.0, start=0.0))
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    cid = store.edl.get_track("v1").clips[0].id
    with pytest.raises(ValueError, match="contain"):
        dispatch(store, "set_clip_fit", {"clip_id": cid, "fit": "stretch"})


# --------------------------------------- P6 residual: deterministic overlay order

def test_overlay_order_follows_start_not_static_vs_animated(tmp_path):
    """Behavioural, not source-text: statics and animated stickers are appended
    in two SEPARATE blocks, so at equal (track_z, clip_z) every static used to
    sort ahead of every animated one regardless of start — a static starting at
    5s composited UNDER an animated one starting at 1s. The old key relied on
    sort() stability for a tie-break the append order did not provide.

    `extra_inputs` is built by enumerating the sorted items, so its order IS the
    composite order: later entries draw on top.
    """
    from video_ai_editor.render.text_overlay import build_overlay_chain

    early = _mk_png(tmp_path / "early.png", "red")
    late = _mk_png(tmp_path / "late.png", "blue")

    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    track = edl.get_track("stickers")
    # ANIMATED (keyframed opacity) and EARLY.
    anim = Sticker(src=str(early), start=1.0, end=4.0)
    anim.transform.opacity = {"keyframes": [[0.0, 0.0], [0.5, 1.0]]}
    # STATIC and LATE.
    static = Sticker(src=str(late), start=5.0, end=8.0)
    track.clips.extend([anim, static])
    edl.recompute_duration()

    _fc, extra_inputs, _label = build_overlay_chain(
        edl, tmp_path / "cache", source_label="[v]", out_label="[vo]",
        first_input_index=1, out_w=320, out_h=180)

    # Both are re-cached under content-hash names whose PREFIX encodes the kind:
    # `sa_` = sticker-animated, `st_` = sticker-static (text_overlay's cache
    # naming). Compare BASENAMES only — matching those prefixes anywhere in the
    # joined argv also hits the pytest tmpdir path, which is derived from this
    # test's own name ("te-st_-overlay…").
    order = [Path(a).name.split("_")[0] for a in extra_inputs if a.endswith(".png")]
    assert order == ["sa", "st"], (
        "expected the EARLY (animated) sticker first and the LATE (static) one "
        f"composited on top; got {order} from {extra_inputs}")
