"""Wave E, lane F2: CapCut Canvas backgrounds and overlay blend modes.

Every render assertion DECODES the exported (or previewed) file — BT.709
limited, the project's colour domain — and compares it with an oracle built
independently of the filter graph:

* canvas: the letterbox of a contain-fit clip shows the colour asked for, a
  blurred cover copy of the clip (Pillow's own cover + Gaussian), or the
  picture cover-fitted — and the picture itself is untouched — on a 9:16
  and a 16:9 canvas, through the export, the chunked preview and a split;
* blend: for each of CapCut's 14 modes, B(Cb, Cs) per the W3C formula
  (`canvas_blend.blend_reference`) from two OTHER renders of the same EDL
  (the overlay drawn Normal gives Cs, the overlay lane muted gives Cb), at
  full opacity and at 0.5, on both canvases — and everything outside the
  overlay stays byte-identical to the base.

Plus the schema defaults (an EDL without the fields hashes as before),
.vae save/open, the dispatch ops (one undo step, "Apply to all",
validation), the path guard, the chunk-cache key, and the prompt.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import sys

import numpy as np
import pytest
from PIL import Image, ImageFilter, ImageOps

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import canvas_blend as CB
from video_ai_editor.edl.schema import Canvas, CanvasBackground, Clip, EDL, Transform, empty_edl
from video_ai_editor.render import render_export, render_preview
from video_ai_editor.render import canvas_bg as CBG

sys.path.insert(0, str(Path(__file__).parent))

ASPECTS = {"9x16": (360, 640), "16x9": (640, 360)}
#: The fitted picture next to a new background vs next to black bars: the
#: same pixels through a lossy (q18) encode.
PICTURE_MIN_PSNR = 42.0


# --------------------------------------------------------------------------- media

def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("f2_media")
    # a still-ish 16:9 and 4:3 source with big colour areas (testsrc2), lossless
    _ff("-f", "lavfi", "-i", "testsrc2=s=480x270:r=30:d=2", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "wide.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=2", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "tall43.mp4"))
    # a 2-D RGB ramp base (every channel pair meets) and a different overlay ramp
    ramp = ("nullsrc=s=640x640:r=30:d=2,format=gbrp,"
            "geq=r='X*255/639':g='Y*255/639':b='128+127*sin(X/40)',"
            "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
    _ff("-f", "lavfi", "-i", ramp, "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
        str(d / "base.mp4"))
    # the same base with NO colour tags (a screen recording, most web video):
    # ffmpeg 8 negotiates colour tags per link, so the blend must not convert
    _ff("-f", "lavfi", "-i", ramp + ",setparams=colorspace=unknown:range=unknown:color_primaries=unknown"
        ":color_trc=unknown", "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "base_untagged.mp4"))
    top = ("nullsrc=s=320x320:r=30:d=2,format=gbrp,"
           "geq=r='255-Y*255/319':g='128+120*cos(X/25)':b='X*255/319',"
           "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
    _ff("-f", "lavfi", "-i", top, "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "top.mp4"))
    _ff("-f", "lavfi", "-i", top + ",setparams=colorspace=unknown:range=unknown:color_primaries=unknown"
        ":color_trc=unknown", "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(d / "top_untagged.mp4"))
    im = Image.linear_gradient("L").resize((400, 300))
    rgb = Image.merge("RGB", (im, ImageOps.mirror(im), im.transpose(Image.Transpose.ROTATE_90).resize((400, 300))))
    rgb.save(d / "pic.png")
    return {"wide": d / "wide.mp4", "tall43": d / "tall43.mp4", "base": d / "base.mp4",
            "base_untagged": d / "base_untagged.mp4", "top_untagged": d / "top_untagged.mp4",
            "top": d / "top.mp4", "pic": d / "pic.png"}


def decode(path: Path, t: float, w: int, h: int) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
         "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.float64)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def _store(sd: Path, e: EDL) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    e.canvas.loudness_lufs = None
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json(by_alias=True))
    return EDLStore(sd)


def _export(sd: Path, e: EDL) -> Path:
    s = _store(sd, e)
    return render_export(s.edl, s.dir).path


# --------------------------------------------------------------------------- schema

def test_defaults_are_omitted_so_old_edls_hash_the_same():
    c = Clip(src="a.mp4", in_=0, out=1)
    d = json.loads(c.model_dump_json(by_alias=True))
    assert "canvas_bg" not in d and "blend" not in d
    e = empty_edl()
    e.get_track("v1").clips.append(Clip(src="a.mp4", in_=0, out=1, id="c"))
    h0 = e.hash()
    e.get_track("v1").clips[0].blend = "normal"
    assert e.hash() == h0
    e.get_track("v1").clips[0].canvas_bg = CanvasBackground(type="color", color="#FF0000")
    assert e.hash() != h0


def test_the_model_clamps_and_never_makes_an_edl_unloadable():
    bg = CanvasBackground(type="blur", blur=9, color="not a colour")
    assert bg.blur == 4 and bg.color == "#000000"
    assert CanvasBackground(color="white").color == "#FFFFFF"
    c = Clip.model_validate({"src": "a", "in": 0, "out": 1, "canvas_bg": {"type": "color", "color": "e53935"},
                             "blend": "screen"})
    assert c.canvas_bg.color == "#E53935" and c.blend == "screen"
    # review RE: ONE forward-compatibility policy for the wave's fields — a
    # mode a newer build wrote loads as Normal (logged), never an unloadable
    # EDL; set_blend_mode still refuses it with a 400 at the tool boundary
    c.blend = "sparkle"
    assert c.blend == "normal"


def test_the_one_table_names_every_capcut_blend_mode():
    assert [b.label for b in CB.BLENDS] == [
        "Normal", "Darken", "Multiply", "Color Burn", "Linear Burn", "Lighten", "Screen", "Color Dodge",
        "Add", "Overlay", "Soft Light", "Hard Light", "Difference", "Exclusion"]
    assert CB.resolve_blend("Linear Dodge") == "add"
    assert CB.resolve_blend("color-burn") == "color_burn"
    with pytest.raises(ValueError):
        CB.resolve_blend("sparkle")
    assert [x.level for x in CB.CANVAS_BLUR_LEVELS] == [1, 2, 3, 4]


def test_the_engine_blur_fixture_is_current():
    """lib/preview/render/canvasBg.ts reproduces these numbers (its test reads
    the same file); regenerate with tests/gen_canvas_bg_fixture.py."""
    import gen_canvas_bg_fixture as G
    assert json.loads(G.OUT.read_text(encoding="utf-8")) == json.loads(json.dumps(G.cases()))


def test_the_browser_fixture_matches_the_table():
    """The client's operator map (lib/canvasBlend/blendModes.json) is this
    table's payload, byte for byte: pinned on both sides."""
    fx = Path(__file__).parent.parent / "frontend/src/lib/canvasBlend/blendModes.json"
    assert json.loads(fx.read_text(encoding="utf-8")) == CB.presets_payload()


# --------------------------------------------------------------------------- canvas renders

def _canvas_edl(src: Path, wh: tuple[int, int], bg: CanvasBackground | None, **clip) -> EDL:
    e = empty_edl()
    e.canvas = Canvas(w=wh[0], h=wh[1], fps=30)
    c = Clip(src=str(src), in_=0, out=2, start=0, id="c1", **clip)
    c.canvas_bg = bg
    e.get_track("v1").clips.append(c)
    return e


def _picture_box(src_wh: tuple[int, int], wh: tuple[int, int]) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the contain-fitted picture — the pad's own integer rule."""
    from video_ai_editor.render.sar import fit_dims
    fw, fh = fit_dims(src_wh[0], src_wh[1], wh[0], wh[1], "decrease")
    x0 = ((wh[0] - fw) // 2) & ~1
    y0 = ((wh[1] - fh) // 2) & ~1
    return x0, y0, x0 + fw, y0 + fh


def _bars_mask(box, wh) -> np.ndarray:
    m = np.ones((wh[1], wh[0]), bool)
    x0, y0, x1, y1 = box
    # the band a lossy macroblock straddling the picture's edge can touch
    m[max(0, y0 - 8):y1 + 8, max(0, x0 - 8):x1 + 8] = False
    return m


#: The export's blur vs an independent one (Pillow's resampling + scipy's
#: exact Gaussian): gblur(steps=4) alone is ≥ 43 dB from a true Gaussian
#: (measured); the rest is resampler rounding and the lossy encode.
BLUR_MIN_PSNR = 33.0


def _blur_reference(frame: Image.Image, wh: tuple[int, int], level: int) -> np.ndarray:
    """The canvas blur built independently: cover the 1/4-size canvas
    (ffmpeg's size rule, crop offset down to the chroma grid), a true
    Gaussian (edges clamped), bilinear back up."""
    from scipy.ndimage import gaussian_filter
    from video_ai_editor.render.sar import fit_dims
    bw, bh = CBG.blur_dims(*wh)
    cw, ch = fit_dims(frame.width, frame.height, bw, bh, "increase")
    big = frame.resize((cw, ch), Image.Resampling.BICUBIC)
    ox, oy = ((cw - bw) // 2) & ~1, ((ch - bh) // 2) & ~1
    small = np.asarray(big.crop((ox, oy, ox + bw, oy + bh)), dtype=np.float64)
    sig = CBG.blur_sigma_small(level, *wh)
    blurred = np.stack([gaussian_filter(small[..., k], sig, mode="nearest", truncate=4) for k in range(3)], -1)
    up = Image.fromarray(np.clip(blurred + 0.5, 0, 255).astype(np.uint8)).resize(wh, Image.Resampling.BILINEAR)
    return np.asarray(up, dtype=np.float64)


def _source_frame(src: Path, t: float, w: int, h: int) -> Image.Image:
    return Image.fromarray(decode(src, t, w, h).astype(np.uint8))


@pytest.mark.parametrize("aspect", sorted(ASPECTS))
def test_canvas_colour_blur_and_image_fill_the_letterbox(media, tmp_path, aspect):
    wh = ASPECTS[aspect]
    src, src_wh = (media["wide"], (480, 270)) if aspect == "9x16" else (media["tall43"], (320, 240))
    box = _picture_box(src_wh, wh)
    bars = _bars_mask(box, wh)
    x0, y0, x1, y1 = box
    # 8 px in from the picture's edge: the export is lossy H.264, and a
    # macroblock straddling the edge codes differently next to a new colour
    inner = (slice(y0 + 8, y1 - 8), slice(x0 + 8, x1 - 8))
    base = decode(_export(tmp_path / "none", _canvas_edl(src, wh, None)), 1.0, *wh)
    assert base[bars].max() <= 2, "no background renders black bars"

    # colour: the bars ARE the colour (BT.709, ±3 levels)
    col = decode(_export(tmp_path / "col", _canvas_edl(src, wh, CanvasBackground(type="color", color="#E53935"))),
                 1.0, *wh)
    assert np.abs(col[bars] - np.array([229, 57, 53])).max() <= 3, col[bars].mean(axis=0)
    assert psnr(col[inner], base[inner]) >= PICTURE_MIN_PSNR, "the picture itself is untouched"

    # blur: a blurred COVER copy of the clip — Pillow's cover + Gaussian at the
    # same 1/4 size (an independent implementation) within 30 dB, and smooth
    for level in (1, 4):
        out = decode(_export(tmp_path / f"blur{level}",
                             _canvas_edl(src, wh, CanvasBackground(type="blur", blur=level))), 1.0, *wh)
        assert psnr(out[inner], base[inner]) >= PICTURE_MIN_PSNR
        ref = _blur_reference(_source_frame(src, 1.0, *src_wh), wh, level)
        p = psnr(out[bars], ref[bars])
        assert p >= BLUR_MIN_PSNR, (level, p)
        # a blur: neighbouring pixels differ little (testsrc2's hard bars do not)
        grad = np.abs(np.diff(out, axis=1))[bars[:, 1:]].mean()
        assert grad < 3.0, grad

    # image: the picture cover-fitted to the canvas (the route's own file)
    img = decode(_export(tmp_path / "img", _canvas_edl(src, wh, CanvasBackground(type="image",
                                                                                image=str(media["pic"])))),
                 1.0, *wh)
    ref = np.asarray(Image.open(CBG.image_file(media["pic"], *wh)).convert("RGB"), dtype=np.float64)
    assert psnr(img[bars], ref[bars]) >= 36.0, psnr(img[bars], ref[bars])
    assert psnr(img[inner], base[inner]) >= PICTURE_MIN_PSNR


def test_canvas_background_stays_still_under_the_clip_transform_and_opacity(media, tmp_path):
    """Review RE (CapCut's Canvas): the background is a STILL canvas-sized
    layer — the opacity fades the PICTURE over it (it used to dim the
    background too, as it dimmed the black bars), and a scale < 1 shows the
    background where the picture shrank away (it used to shrink the
    background and bring the black bars back)."""
    wh = ASPECTS["9x16"]
    bg = CanvasBackground(type="color", color="#FFFFFF")
    out = decode(_export(tmp_path / "o", _canvas_edl(media["wide"], wh, bg, transform=Transform(opacity=0.5))),
                 1.0, *wh)
    assert out[20, 180].min() >= 250, out[20, 180]                       # the letterbox: undimmed
    base = decode(_export(tmp_path / "b", _canvas_edl(media["wide"], wh, None)), 1.0, *wh)
    mid = out[320, 60:300]                                                # the picture, over white at 50 %
    want = base[320, 60:300] * 0.5 + 255 * 0.5
    assert np.abs(mid - want).mean() <= 6, np.abs(mid - want).mean()
    small = decode(_export(tmp_path / "s", _canvas_edl(media["wide"], wh, bg, transform=Transform(scale=0.6))),
                   1.0, *wh)
    assert small[20, 180].min() >= 250 and small[320, 10].min() >= 250, (small[20, 180], small[320, 10])


def _chain(c: Clip, wh) -> str:
    from video_ai_editor.render.compositor import _build_clip_video_chain
    return _build_clip_video_chain(c, input_label="[0:v]", label_out="[v0]", canvas_w=wh[0], canvas_h=wh[1], fps=30)


def test_cover_fit_and_no_background_keep_their_filter_text(media):
    """A cover clip has no bars (its filter text ignores the field), and a
    clip without a background emits the same text as before the field
    existed — so every existing render is byte-identical."""
    wh = ASPECTS["9x16"]
    plain = Clip(src=str(media["wide"]), in_=0, out=2, id="c")
    assert "pad=360:640:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1" in _chain(plain, wh)
    cover = Clip(src=str(media["wide"]), in_=0, out=2, id="c", fit="cover")
    cover_bg = Clip(src=str(media["wide"]), in_=0, out=2, id="c", fit="cover",
                    canvas_bg=CanvasBackground(type="blur", blur=3))
    assert _chain(cover, wh) == _chain(cover_bg, wh)
    with_bg = Clip(src=str(media["wide"]), in_=0, out=2, id="c", canvas_bg=CanvasBackground(type="blur"))
    text = _chain(with_bg, wh)
    # review RE: the background is its own stream, the picture laid on it by
    # a matte of its own geometry (the picture branch's black pad is masked)
    assert "gblur" in text and "alphamerge" in text and "lutyuv=y=235" in text
    bg_branch = text.split("[cvb")[0]
    assert "color=black" not in bg_branch


def test_a_missing_picture_renders_black_bars_not_a_failed_render(media, tmp_path):
    wh = ASPECTS["9x16"]
    out = decode(_export(tmp_path / "m", _canvas_edl(media["wide"], wh, CanvasBackground(
        type="image", image=str(tmp_path / "gone.png")))), 1.0, *wh)
    assert out[:20].max() <= 2


def test_preview_chunks_key_on_the_background_and_the_fit(media, tmp_path):
    """The chunk cache (render/chunks.py) must not serve a clip's old pixels
    after its background — or its fit, which the key used to omit — changes."""
    from video_ai_editor.render.chunks import fingerprint_clip
    c = Clip(src=str(media["wide"]), in_=0, out=2, id="c")
    k = lambda: fingerprint_clip(c, canvas_w=360, canvas_h=640, fps=30, encoder_args=["x"])  # noqa: E731
    k0 = k()
    c.canvas_bg = CanvasBackground(type="blur", blur=2)
    k1 = k()
    c.canvas_bg = CanvasBackground(type="blur", blur=3)
    k2 = k()
    c.canvas_bg = None
    assert k() == k0 and len({k0, k1, k2}) == 3
    c.fit = "cover"
    assert k() != k0
    # the preview itself (chunked path) shows the background
    wh = ASPECTS["9x16"]
    s = _store(tmp_path / "pv", _canvas_edl(media["wide"], wh, CanvasBackground(type="color", color="#00FF00")))
    pv = render_preview(s.edl, s.dir, height=360)
    fr = decode(pv.path, 1.0, 360, 640)
    assert abs(fr[20, 180, 1] - 255) <= 4 and fr[20, 180, 0] <= 4, fr[20, 180]


@pytest.mark.parametrize("variant", ["speed2", "reverse", "freeze", "keyframed", "curve"])
@pytest.mark.parametrize("kind", ["color", "blur", "image"])
def test_canvas_background_on_retimed_and_keyframed_clips(media, tmp_path, variant, kind):
    """The retime branches (constant, reverse intermediate, freeze, a speed
    curve's file clock) and the keyframed branch (geometry after the grid)
    all carry the background — the split/overlay text splices into each."""
    from video_ai_editor.edl.schema import Keyframe
    wh = ASPECTS["9x16"]
    # (not a primary: the untagged opacity path converts through BT.601
    # RGB, where a BT.709 pure green is out of gamut and clips — R12)
    bg = {"color": CanvasBackground(type="color", color="#40A060"),
          "blur": CanvasBackground(type="blur", blur=2),
          "image": CanvasBackground(type="image", image=str(media["pic"]))}[kind]
    extra: dict = {}
    if variant == "speed2":
        extra["speed"] = 2.0
    elif variant == "reverse":
        extra["reverse"] = True
    elif variant == "freeze":
        extra["freeze"] = 1.0
    elif variant == "curve":
        extra["speed"] = {"curve": [[0, 1.0], [0.5, 0.5], [1, 1.0]]}
    else:
        extra["transform"] = Transform(opacity=Keyframe(keyframes=[(0.0, 1.0), (2.0, 0.5)]))
    out = decode(_export(tmp_path / "r", _canvas_edl(media["wide"], wh, bg, **extra)), 0.5, *wh)
    top = out[10:60, 20:340]
    if kind == "color":
        # (review RE: a keyed opacity fades the picture over the still
        # background; the letterbox stays the full colour)
        want = np.array([0x40, 0xA0, 0x60])
        assert np.abs(top.mean(axis=(0, 1)) - want).max() <= 6, top.mean(axis=(0, 1))
    else:
        assert top.mean() > 20, "the bars are filled, not black"


def test_long_clips_preview_from_segments_with_the_background(media, tmp_path, monkeypatch):
    """A long time-invariant clip previews from picture segments
    (render/segments.py); each segment runs the same chain."""
    from video_ai_editor.render import segments
    monkeypatch.setattr(segments, "SEGMENT_S", 0.5)
    wh = ASPECTS["9x16"]
    c = Clip(src=str(media["wide"]), in_=0, out=2, id="c")
    assert segments.segment_bounds(c, 30) is not None
    s = _store(tmp_path / "seg", _canvas_edl(media["wide"], wh, CanvasBackground(type="color", color="#0000FF")))
    fr = decode(render_preview(s.edl, s.dir, height=360).path, 1.2, 360, 640)
    assert np.abs(fr[20:60, 40:320].mean(axis=(0, 1)) - [0, 0, 255]).max() <= 8


def test_a_split_keeps_the_background_on_both_halves(media, tmp_path):
    wh = ASPECTS["9x16"]
    s = _store(tmp_path / "sp", _canvas_edl(media["wide"], wh, CanvasBackground(type="blur", blur=2)))
    dispatch(s, "split_at", {"track": "v1", "time": 1.0})
    a, b = s.edl.get_track("v1").clips
    assert a.canvas_bg == b.canvas_bg and a.canvas_bg is not b.canvas_bg


# --------------------------------------------------------------------------- blend renders

def _blend_edl(media, wh, mode: str, opacity: float = 1.0, muted: bool = False, base: str = "base",
               top: str = "top") -> EDL:
    e = empty_edl()
    e.canvas = Canvas(w=wh[0], h=wh[1], fps=30)
    e.get_track("v1").clips.append(Clip(src=str(media[base]), in_=0, out=2, start=0, id="b", fit="cover"))
    top = Clip(src=str(media[top]), in_=0, out=2, start=0, id="t",
               transform=Transform(x=wh[0] * 0.45, y=wh[1] * 0.55, scale=1.0, opacity=opacity))
    top.blend = mode
    e.get_track("v2").clips.append(top)
    e.get_track("v2").muted = muted
    return e


def yuv420(rgb: np.ndarray) -> np.ndarray:
    """`rgb` through the export's own 4:2:0 (BT.709 limited) round trip: a
    blend result's chroma is subsampled like every exported pixel, which an
    RGB oracle has to be too (difference/exclusion flip chroma per pixel)."""
    h, w = rgb.shape[:2]
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-",
         "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
         "-f", "rawvideo", "-"],
        input=np.clip(rgb + 0.5, 0, 255).astype(np.uint8).tobytes(), capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.float64)


def _pip_box(wh) -> tuple[int, int, int, int]:
    side = max(40, int(max(wh) * 0.35))           # pip.py: 35 % of the long edge, square source
    cx, cy = wh[0] * 0.45, wh[1] * 0.55
    x0, y0 = int(cx - side / 2), int(cy - side / 2)
    return x0, y0, x0 + side, y0 + side


@pytest.fixture(scope="module")
def blend_refs(media, tmp_path_factory):
    """Cb (overlay lane muted) and Cs (overlay drawn Normal) per aspect."""
    root = tmp_path_factory.mktemp("f2_blend_refs")
    out = {}
    for aspect, wh in ASPECTS.items():
        cb = decode(_export(root / f"cb_{aspect}", _blend_edl(media, wh, "normal", muted=True)), 1.0, *wh)
        cs = decode(_export(root / f"cs_{aspect}", _blend_edl(media, wh, "normal")), 1.0, *wh)
        out[aspect] = (cb, cs)
    return out


#: Export vs the W3C oracle, per mode (measured, wave E F2: 36.4-46.2 dB
#: over 14 modes × 2 canvases × 2 opacities; Difference at full opacity is
#: the floor — its chroma flips sign pixel to pixel and 4:2:0 cannot carry
#: it). The nearest WRONG formula scores at most 27.1 dB on the same frames,
#: so 35 dB passes every right mode and fails every swapped one.
BLEND_MIN_PSNR = 35.0
#: Outside the overlay: the base's own pixels through two lossy encodes.
OUTSIDE_MIN_PSNR = 46.0


@pytest.mark.parametrize("aspect", sorted(ASPECTS))
@pytest.mark.parametrize("mode", [b for b in CB.BLEND_IDS if b != "normal"])
def test_every_blend_mode_renders_the_w3c_formula(media, tmp_path, blend_refs, aspect, mode):
    wh = ASPECTS[aspect]
    cb, cs = blend_refs[aspect]
    x0, y0, x1, y1 = _pip_box(wh)
    inner = (slice(y0 + 3, y1 - 3), slice(x0 + 3, x1 - 3))
    outside = np.ones((wh[1], wh[0]), bool)
    outside[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3] = False
    f = np.vectorize(lambda a, b, al: CB.blend_reference(mode, a / 255.0, b / 255.0, al) * 255.0)
    for opacity in (1.0, 0.5):
        out = decode(_export(tmp_path / f"{mode}_{opacity}", _blend_edl(media, wh, mode, opacity)), 1.0, *wh)
        ref = f(cb[inner], cs[inner], opacity)
        p = psnr(out[inner], ref)
        assert p >= BLEND_MIN_PSNR, (mode, opacity, round(p, 2))
        # untouched: as close to the base as a Normal overlay's render is
        # (two lossy encodes of the same pixels: ~51 dB, max 6 levels)
        assert psnr(out[outside], cb[outside]) >= OUTSIDE_MIN_PSNR, "the base outside the overlay is untouched"


@pytest.mark.parametrize("mode", ["multiply", "screen", "add", "linear_burn"])
def test_a_blend_over_an_untagged_base_is_not_colour_converted(media, tmp_path, mode):
    """An untagged base (most screen recordings): the blended layer is
    converted through RGB as BT.709 and must go back with the BASE's tags,
    or ffmpeg converts one side of the final overlay (601 vs 709)."""
    wh = ASPECTS["9x16"]
    cb = decode(_export(tmp_path / "cb", _blend_edl(media, wh, "normal", muted=True, base="base_untagged", top="top_untagged")), 1.0, *wh)
    cs = decode(_export(tmp_path / "cs", _blend_edl(media, wh, "normal", base="base_untagged", top="top_untagged")), 1.0, *wh)
    out = decode(_export(tmp_path / "bl", _blend_edl(media, wh, mode, base="base_untagged", top="top_untagged")), 1.0, *wh)
    x0, y0, x1, y1 = _pip_box(wh)
    inner = (slice(y0 + 3, y1 - 3), slice(x0 + 3, x1 - 3))
    f = np.vectorize(lambda a, b: CB.blend_reference(mode, a / 255.0, b / 255.0, 1.0) * 255.0)
    assert psnr(out[inner], f(cb[inner], cs[inner])) >= BLEND_MIN_PSNR


def test_a_normal_overlay_keeps_its_filter_text(media):
    """blend='normal' (the default) emits the plain `overlay` — every existing
    PiP renders byte-identically."""
    from video_ai_editor.render.pip import build_pip_overlay_chain
    e = _blend_edl(media, ASPECTS["9x16"], "normal")
    fc, *_ = build_pip_overlay_chain(e, source_label="[base]", out_label="[out]", first_input_index=1,
                                     out_w=360, out_h=640, fps=30)
    assert "lut2" not in fc and "alphamerge" not in fc and "overlay=x='" in fc
    e.get_track("v2").clips[0].blend = "screen"
    fc2, *_ = build_pip_overlay_chain(e, source_label="[base]", out_label="[out]", first_input_index=1,
                                      out_w=360, out_h=640, fps=30)
    assert "lut2" in fc2


def test_preview_leaves_a_blended_overlay_to_the_browser(media):
    """The browser draws a blended PiP (CSS mix-blend-mode, lib/pipBlendLayers)
    exactly as it draws a Normal one: pip.py's preview branch bakes neither."""
    from video_ai_editor.render.pip import build_pip_overlay_chain
    e = _blend_edl(media, ASPECTS["9x16"], "multiply")
    fc, _inputs, label, audio = build_pip_overlay_chain(
        e, source_label="[base]", out_label="[out]", first_input_index=1, out_w=360, out_h=640, fps=30,
        preview=True)
    assert fc == "" or "lut2" not in fc
    assert label == "[base]" and len(audio) == 1


# --------------------------------------------------------------------------- dispatch

def _seed(tmp: Path, media) -> EDLStore:
    e = empty_edl()
    e.canvas = Canvas(w=360, h=640, fps=30)
    for i in range(3):
        e.get_track("v1").clips.append(Clip(src=str(media["wide"]), in_=0, out=2, start=2 * i, id=f"c{i}"))
    e.get_track("v2").clips.append(Clip(src=str(media["top"]), in_=0, out=2, start=0, id="p1",
                                        transform=Transform(x=180, y=320, scale=0.6)))
    return _store(tmp, e)


def test_set_canvas_background_on_one_clip_then_apply_to_all_in_one_undo_step(media, tmp_path):
    s = _seed(tmp_path / "d", media)
    dispatch(s, "set_canvas_background", {"clip_id": "c1", "type": "blur", "blur": 3})
    v1 = s.edl.get_track("v1").clips
    assert v1[1].canvas_bg.type == "blur" and v1[1].canvas_bg.blur == 3
    assert v1[0].canvas_bg is None and v1[2].canvas_bg is None
    dispatch(s, "set_canvas_background", {"clip_id": "c1", "all": True})      # the Inspector's Apply to all
    v1 = s.edl.get_track("v1").clips
    assert all(c.canvas_bg.type == "blur" and c.canvas_bg.blur == 3 for c in v1)
    assert len({id(c.canvas_bg) for c in v1}) == 3, "never one shared model"
    assert s.edl.get_track("v2").clips[0].canvas_bg is None
    dispatch(s, "undo", {})
    v1 = s.edl.get_track("v1").clips
    assert [c.canvas_bg is not None for c in v1] == [False, True, False]
    r = dispatch(s, "set_canvas_background", {"all": True, "type": "colour", "color": "white"})
    assert all(c.canvas_bg.color == "#FFFFFF" for c in s.edl.get_track("v1").clips)
    assert "clip_ids" in r
    dispatch(s, "set_canvas_background", {"all": True, "type": "none"})
    assert all(c.canvas_bg is None for c in s.edl.get_track("v1").clips)


@pytest.mark.parametrize("args,msg", [
    ({"clip_id": "p1", "type": "blur"}, "main-track"),
    ({"clip_id": "c0", "type": "blur", "blur": 7}, "between 1 and 4"),
    ({"clip_id": "c0", "type": "blur", "blur": 2.5}, "integer"),
    ({"clip_id": "c0", "type": "color", "color": "sparkly"}, "colour"),
    ({"clip_id": "c0", "type": "image"}, "image"),
    ({"clip_id": "c0", "type": "image", "image": "/nope/x.png"}, "not found"),
    ({"clip_id": "c0", "type": "glitter"}, "unknown canvas background"),
    ({"type": "blur"}, "clip_id"),
])
def test_set_canvas_background_refuses_what_it_cannot_render(media, tmp_path, args, msg):
    s = _seed(tmp_path / "r", media)
    h = s.edl.hash()
    with pytest.raises(ValueError, match=msg):
        dispatch(s, "set_canvas_background", args)
    assert s.edl.hash() == h


def test_a_text_file_named_png_is_not_a_picture(media, tmp_path):
    s = _seed(tmp_path / "t", media)
    fake = tmp_path / "fake.png"
    fake.write_text("not a picture")
    with pytest.raises(ValueError, match="could not be read"):
        dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "image", "image": str(fake)})


def test_a_photo_imported_as_a_still_uses_its_original_picture(media, tmp_path):
    s = _seed(tmp_path / "st", media)
    up = s.dir / "uploads" / "pic"
    up.mkdir(parents=True)
    pic = up / "pic.png"
    pic.write_bytes(media["pic"].read_bytes())
    norm = up / "pic.normalized.mp4"
    norm.write_bytes(media["wide"].read_bytes())
    (up / "ingest.json").write_text(json.dumps({"src": str(pic), "normalized": str(norm), "still": True}))
    dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "image", "image": str(norm)})
    assert Path(s.edl.get_track("v1").clips[0].canvas_bg.image) == pic.resolve()


def test_set_blend_mode_on_overlays_only(media, tmp_path):
    s = _seed(tmp_path / "b", media)
    dispatch(s, "set_blend_mode", {"clip_id": "p1", "mode": "Linear Dodge"})
    assert s.edl.get_track("v2").clips[0].blend == "add"
    with pytest.raises(ValueError, match="base layer"):
        dispatch(s, "set_blend_mode", {"clip_id": "c0", "mode": "screen"})
    with pytest.raises(ValueError, match="unknown blend mode"):
        dispatch(s, "set_blend_mode", {"clip_id": "p1", "mode": "sparkle"})
    dispatch(s, "undo", {})
    assert s.edl.get_track("v2").clips[0].blend == "normal"


def test_set_property_cannot_point_a_canvas_at_an_unreadable_path(media, tmp_path):
    s = _seed(tmp_path / "sp", media)
    with pytest.raises(ValueError):
        dispatch(s, "set_property", {"clip_id": "c0", "path": "canvas_bg",
                                     "value": {"type": "image", "image": "/etc/hosts"}})
    dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "image", "image": str(media["pic"])})
    with pytest.raises(ValueError):
        dispatch(s, "set_property", {"clip_id": "c0", "path": "canvas_bg.image", "value": "/etc/hosts"})


def test_the_image_arg_is_path_guarded_when_restriction_is_on(media, tmp_path, monkeypatch):
    from video_ai_editor import config
    s = _seed(tmp_path / "g", media)
    monkeypatch.setattr(config, "WORKDIR", tmp_path / "wd")
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(True)
    try:
        with pytest.raises(ValueError):
            dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "image", "image": str(media["pic"])})
    finally:
        config.enable_path_restriction(before)


# --------------------------------------------------------------------------- .vae

def test_vae_round_trip_bundles_the_background_picture(media, tmp_path, monkeypatch):
    from video_ai_editor import storage as _storage, storage_project as _sp
    from video_ai_editor.storage_project import load_project, save_project
    wd = tmp_path / "wd"
    monkeypatch.setattr(_storage, "WORKDIR", wd)
    monkeypatch.setattr(_sp, "session_dir", lambda sid: wd / sid)
    s = _seed(wd / "s1", media)
    dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "image", "image": str(media["pic"])})
    dispatch(s, "set_canvas_background", {"clip_id": "c1", "type": "blur", "blur": 4})
    dispatch(s, "set_blend_mode", {"clip_id": "p1", "mode": "screen"})
    dst = tmp_path / "p.vae"
    save_project("s1", dst)
    new = EDLStore(wd / load_project(dst))
    v1 = new.edl.get_track("v1").clips
    img = Path(v1[0].canvas_bg.image)
    assert img.is_file() and img != media["pic"] and str(wd) in str(img)
    assert img.read_bytes() == media["pic"].read_bytes()
    assert v1[1].canvas_bg == CanvasBackground(type="blur", blur=4)
    assert new.edl.get_track("v2").clips[0].blend == "screen"


# --------------------------------------------------------------------------- routes

def test_presets_route_and_the_picture_route(media, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main
    from video_ai_editor.api import canvas_blend_routes as R
    s = _seed(tmp_path / "rt", media)
    dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "image", "image": str(media["pic"])})
    monkeypatch.setattr(R, "_RESOLVE_STORE", lambda sid: s)
    cl = TestClient(main.app, base_url="http://127.0.0.1")
    body = cl.get("/api/canvas-blend/presets").json()
    assert body == CB.presets_payload()
    r = cl.get("/api/sessions/s/canvas-bg/c0.png", params={"w": 360, "h": 640})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    served = tmp_path / "served.png"
    served.write_bytes(r.content)
    assert np.array_equal(np.asarray(Image.open(served)), np.asarray(Image.open(CBG.image_file(media["pic"], 360, 640))))
    assert cl.get("/api/sessions/s/canvas-bg/c1.png", params={"w": 360, "h": 640}).status_code == 404
    # the Inspector's "Choose picture…": into uploads/images, pictures only
    up = cl.post("/api/sessions/s/canvas-bg/upload", files={"file": ("my pic.png", media["pic"].read_bytes(), "image/png")})
    assert up.status_code == 200, up.text
    dst = Path(up.json()["src"])
    assert dst.parent == s.dir / "uploads" / "images" and dst.read_bytes() == media["pic"].read_bytes()
    again = cl.post("/api/sessions/s/canvas-bg/upload", files={"file": ("my pic.png", media["pic"].read_bytes(), "image/png")})
    assert Path(again.json()["src"]) != dst, "never overwrites"
    bad = cl.post("/api/sessions/s/canvas-bg/upload", files={"file": ("fake.png", b"not a picture", "image/png")})
    assert bad.status_code == 400 and not any(p.name.startswith("fake") for p in dst.parent.iterdir())
    assert cl.post("/api/sessions/s/canvas-bg/upload", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400


# --------------------------------------------------------------------------- the prompt

def test_the_grammar_reads_canvas_and_blend_and_nothing_else():
    from video_ai_editor.agent.prompt import grammar as G
    from video_ai_editor.agent.prompt.canvas_expanders import blend_mode_in
    for phrase in ("blur the background", "make the background black", "use this image as the background",
                   "fill the black bars with white", "heavy blur on the background", "remove the background blur"):
        assert [h.intent for h in G.detect(phrase).hits] == ["canvas"], phrase
    for phrase in ("set the overlay to screen", "multiply blend the top clip", "change the blend mode to add"):
        assert [h.intent for h in G.detect(phrase).hits] == ["blend"], phrase
    # the AI cut-out, the music bed, a brightness request and a text overlay are not these
    for phrase in ("remove the background", "blur the background music", "make the overlay darker",
                   "add a text overlay"):
        assert not {"canvas", "blend"} & {h.intent for h in G.detect(phrase).hits}, phrase
    assert blend_mode_in("use linear dodge on the overlay") == "add"          # not the NOUN "overlay"
    assert blend_mode_in("blend the pip with overlay") == "overlay"
    assert blend_mode_in("make the top clip soft light") == "soft_light"


def test_the_plan_validator_holds_canvas_and_blend_to_the_table(tmp_path):
    from video_ai_editor.agent.prompt import schema as Sc
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    from video_ai_editor.agent.prompt.validate import PlanRejected, validate_plan
    pic = tmp_path / "s" / "uploads" / "images" / "p.png"
    pic.parent.mkdir(parents=True)
    pic.write_bytes(b"\x89PNG")
    f = TimelineFacts.minimal(allowed_paths={str(pic.resolve())}, uploads_images=[str(pic.resolve())])

    def run(tool, **args):
        return validate_plan(Sc.Plan.new(intent="t", brain="claude",
                                         steps=[Sc.Step(tool=tool, args=args, why="t")]), f)

    assert run("set_canvas_background", all=True, type="blur", blur=3).steps
    assert run("set_canvas_background", all=True, type="color", color="white").steps
    assert run("set_canvas_background", all=True, type="image", image=str(pic)).steps
    assert run("set_blend_mode", clip_id="c_v1", mode="screen").steps
    for bad in ({"type": "glitter"}, {"type": "color", "color": "sparkly"}, {"type": "blur", "blur": 9},
                {"type": "blur", "blur": 2.5}, {"type": "image"}):
        with pytest.raises(PlanRejected):
            run("set_canvas_background", all=True, **bad)
    with pytest.raises(PlanRejected):
        run("set_blend_mode", clip_id="c_v1", mode="sparkle")
    # a path a model made up is never taken (asked for or refused)
    out = None
    try:
        out = run("set_canvas_background", all=True, type="image", image="/etc/hosts")
    except PlanRejected:
        pass
    assert out is None or all(s.args.get("image") != "/etc/hosts" for s in out.steps)


def test_a_locked_main_track_refuses_apply_to_all_and_a_locked_overlay_its_blend(media, tmp_path):
    s = _seed(tmp_path / "lk", media)
    dispatch(s, "set_track_locked", {"track": "v1", "locked": True})
    h = s.edl.hash()
    with pytest.raises(ValueError):
        dispatch(s, "set_canvas_background", {"all": True, "type": "blur"})
    with pytest.raises(ValueError):
        dispatch(s, "set_canvas_background", {"clip_id": "c0", "type": "color", "color": "red"})
    assert s.edl.hash() == h
    dispatch(s, "set_track_locked", {"track": "v2", "locked": True})
    with pytest.raises(ValueError):
        dispatch(s, "set_blend_mode", {"clip_id": "p1", "mode": "screen"})
    assert s.edl.get_track("v2").clips[0].blend == "normal"
