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
from video_ai_editor.edl.schema import Canvas, Clip, Sticker, Transform, empty_edl
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


def _mk_split_video(path: Path, w: int = 640, h: int = 360, dur: float = 1.0) -> Path:
    """Left half red, right half blue — lets a test tell which part of the
    source is actually on screen, not just whether it's black or not."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=red:s={w // 2}x{h}:d={dur}",
         "-f", "lavfi", "-i", f"color=c=blue:s={w // 2}x{h}:d={dur}",
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}",
         "-filter_complex", "[0:v][1:v]hstack[v]",
         "-map", "[v]", "-map", "2:a", "-r", "30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True)
    return path


def test_cover_fit_pan_reveals_real_footage_not_black(tmp_path):
    """'Fill frame' + a Transform X pan used to crop to dead-centre in the fit
    stage BEFORE the pan ever ran, so panning had nothing left to reveal and
    just padded in black ("this function crops the video by its own, I have
    no freedom to choose which part is kept"). A pan should show a different
    real part of the (wide) source, with the frame still fully filled."""
    src = _mk_split_video(tmp_path / "split.mp4", w=640, h=360)
    edl = empty_edl(Canvas(w=360, h=640, fps=30))
    c = Clip(src=str(src), in_=0.0, out=1.0, start=0.0)
    c.fit = "cover"
    edl.get_track("v1").clips.append(c)
    edl.recompute_duration()

    def render_and_sample(transform: Transform) -> tuple[int, int, int]:
        c.transform = transform
        sess = tmp_path / f"s_{transform.x}"
        sess.mkdir()
        res = render_preview(edl, sess, height=640)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "0.3", "-i", str(res.path), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "360x640", "-"],
            capture_output=True, check=True).stdout
        black_rows = sum(1 for y in range(640) if max(raw[y * 360 * 3:(y + 1) * 360 * 3]) < 12)
        # The red/blue split lands at the source's own centre, which a centred
        # cover crop also puts at the output's centre column — so the centre
        # column is the point most sensitive to a modest pan in EITHER
        # direction (an edge column only "sees" one direction of pan before
        # it's already saturated one colour).
        px = raw[(320 * 360 + 180) * 3: (320 * 360 + 180) * 3 + 3]
        return px[0], px[2], black_rows  # (red channel, blue channel, black rows)

    _, _, blk0 = render_and_sample(Transform())                 # centred cover crop
    r_pos, b_pos, blk_pos = render_and_sample(Transform(x=200))   # +x moves the picture
    r_neg, b_neg, blk_neg = render_and_sample(Transform(x=-200))  # right -> reveals more
                                                                   # of the source's LEFT
                                                                   # (red) side; -x more blue.

    assert blk0 == blk_pos == blk_neg == 0, (
        f"panning a cover-fit clip must never reveal black: {blk0} {blk_pos} {blk_neg}")
    # Proves the pan reveals genuinely different real footage (not a no-op or
    # manufactured black fill): +x shifts toward red, -x shifts toward blue.
    assert r_pos > r_neg, f"+x didn't shift toward red vs -x: {r_pos} vs {r_neg}"
    assert b_neg > b_pos, f"-x didn't shift toward blue vs +x: {b_neg} vs {b_pos}"


def test_cover_fit_pan_with_scale_below_one_is_not_black(tmp_path):
    """A cover-fit pan combined with Transform scale < 1 (reachable via the
    CropReposition scroll-to-zoom-out gesture) used to render a solid BLACK
    frame: the "extra zoom" step multiplied the already-covering frame by
    `sc_static` unclamped, so scale=0.1 shrunk it to 10% — SMALLER than the
    canvas — and the subsequent crop then asked for more pixels than existed.
    Found live via a real user session (scale=0.1 x=342 y=130); ffmpeg
    produced a valid-looking mp4 with no error, just a black one. The extra
    zoom must never shrink the frame below its cover-fit baseline size."""
    src = _mk_split_video(tmp_path / "split2.mp4", w=640, h=360)
    edl = empty_edl(Canvas(w=360, h=640, fps=30))
    c = Clip(src=str(src), in_=0.0, out=1.0, start=0.0)
    c.fit = "cover"
    c.transform = Transform(x=100.0, y=0.0, scale=0.1)
    edl.get_track("v1").clips.append(c)
    edl.recompute_duration()
    res = render_preview(edl, tmp_path / "sess", height=640)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "0.3", "-i", str(res.path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "360x640", "-"],
        capture_output=True, check=True).stdout
    max_channel = max(raw)
    assert max_channel > 12, f"scale<1 cover-fit pan rendered (near-)black: max byte {max_channel}"


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


def test_preview_skips_stickers_but_export_still_bakes_them(tmp_path):
    """Preview must NOT bake stickers; export must.

    StickerLayer draws every sticker client-side each frame so a drag is
    smooth. While the preview ALSO baked them, the baked copy sat frozen at
    the pre-drag position through the whole gesture and the commit→re-render
    gap: two stickers on screen mid-drag, then the live one vanished and the
    stale one lingered at the old spot ("it disappears and leaves a trail").
    A client cannot erase a baked pixel, so the preview has to leave them
    alone — but export has no StickerLayer, so it must keep baking.
    """
    from video_ai_editor.render.text_overlay import build_overlay_chain

    png = _mk_png(tmp_path / "sticker.png", "red")
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    track = edl.get_track("stickers")
    static = Sticker(src=str(png), start=0.0, end=4.0)
    animated = Sticker(src=str(png), start=0.0, end=4.0)
    animated.transform.opacity = {"keyframes": [[0.0, 0.0], [0.5, 1.0]]}
    track.clips.extend([static, animated])
    edl.recompute_duration()

    def chain(preview: bool):
        return build_overlay_chain(
            edl, tmp_path / f"cache_{preview}", source_label="[v]", out_label="[vo]",
            first_input_index=1, out_w=320, out_h=180, preview=preview)

    pv_filter, pv_inputs, pv_label = chain(True)
    ex_filter, ex_inputs, _ = chain(False)

    # Both kinds (static `st_`, animated `sa_`) must be gone from preview.
    assert [a for a in pv_inputs if a.endswith(".png")] == [], (
        f"preview must bake no sticker PNGs, got {pv_inputs}")
    # No overlay work at all here → the chain is a pass-through, so callers
    # keep using the untouched source label.
    assert pv_filter == "" and pv_label == "[v]"

    ex_pngs = [Path(a).name.split("_")[0] for a in ex_inputs if a.endswith(".png")]
    assert sorted(ex_pngs) == ["sa", "st"], (
        f"export must still bake both sticker kinds, got {ex_inputs}")


def test_split_at_reports_the_right_half_so_selection_can_follow(tmp_path):
    """The LEFT half keeps the original clip id, so whatever was selected
    before the split ends up pointing at the piece that now ENDS exactly at
    the cut — the one the playhead has just left. Properties then opened with
    "Not visible at the playhead (28.90s) — this clip runs 0.00–28.90s" the
    instant you pressed split, which reads as the split having broken
    something. `halves` maps original id → right-half id so the caller can
    move the selection onto the piece under the playhead.
    """
    sid = new_session_id()
    sd = session_dir(sid)
    for sub in ("uploads", "previews", "exports", "cache", "snapshots"):
        (sd / sub).mkdir(parents=True, exist_ok=True)
    store = EDLStore(sd)
    src = _mk_video(tmp_path / "v.mp4", dur=4.0)
    dispatch(store, "add_clip", {"src": str(src), "track": "v1",
                                 "in": 0.0, "out": 4.0, "start": 0.0})
    orig = store.edl.get_track("v1").clips[0].id

    res = dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    assert res["split"] == 1
    halves = res["halves"]
    assert list(halves) == [orig], f"expected the original id as the key, got {halves}"

    right_id = halves[orig]
    clips = {c.id: c for c in store.edl.get_track("v1").clips}
    assert right_id in clips and right_id != orig
    left, right = clips[orig], clips[right_id]
    # The left half ends AT the cut (playhead >= end → "not visible"); the
    # right half is the one that actually contains the playhead.
    assert left.start + left.effective_duration == pytest.approx(2.0, abs=1e-6)
    assert right.start == pytest.approx(2.0, abs=1e-6)
    assert right.start + right.effective_duration > 2.0


def test_sticker_image_route_resolves_paths_outside_uploads(tmp_path):
    """StickerLayer draws sticker pixels client-side now, so it must be able
    to FETCH every sticker's artwork — including the ones that legitimately
    live outside <session>/uploads/ (a brand kit's end-card, an emoji from the
    shared cache, any absolute path). /files/{kind}/{name} refuses those by
    design, so those stickers would have drawn as an empty box in preview
    while exporting perfectly. Resolution goes through the session's OWN EDL,
    so the untrusted URL input is a clip id, never a path.
    """
    from fastapi.testclient import TestClient
    from video_ai_editor import main as m

    outside = tmp_path / "brand" / "endcard.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    _mk_png(outside, "blue")

    sid = new_session_id()
    sd = session_dir(sid)
    for sub in ("uploads", "previews", "exports", "cache", "snapshots"):
        (sd / sub).mkdir(parents=True, exist_ok=True)
    store = EDLStore(sd)
    sk = Sticker(src=str(outside), start=0.0, end=2.0)
    store.edl.get_track("stickers").clips.append(sk)
    store.commit("test", {}, "add sticker")

    client = TestClient(m.app)
    r = client.get(f"/api/sessions/{sid}/sticker/{sk.id}")
    assert r.status_code == 200, r.text
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    # Unknown clip id → 404, not a 500 and not a path probe.
    assert client.get(f"/api/sessions/{sid}/sticker/nope").status_code == 404
    # A malformed session id is rejected before any lookup.
    assert client.get("/api/sessions/..%2F..%2Fetc/sticker/x").status_code in (400, 404)
