"""CapCut Canvas backgrounds in the v1 chain (wave E, lane F2).

A `contain`-fit main-track clip is scaled down to fit and padded out to the
canvas. `Clip.canvas_bg` decides what that pad shows (`edl/canvas_blend.py`):

  color  `pad=…:color=0xRRGGBB` — the same single filter as the black bars;
  blur   the clip itself, scaled to COVER a 1/4-size canvas, Gaussian-blurred
         there and scaled back up (bilinear), with the fitted picture laid
         over its centre by `overlay` (the offset rounds exactly like `pad`'s:
         truncated, then down to the chroma grid);
  image  a picture cover-fitted to the canvas ONCE, in Python (`image_file`,
         cached by content), read into the graph with `movie=` (a filter
         SOURCE, so the graph's input indices are untouched) and held behind
         the fitted picture.

Review RE: the background is a STATIC canvas-sized layer under the moving
picture (`composite_block`): the flip, rotation, scale / pan, keys, the clip
animation, the effects and the opacity apply to the fitted picture ONLY,
which is laid over the background by a matte built with the same geometry.
(Wave E first baked it into the fitted frame, so a Zoom In or a scale < 1
shrank the background and black bars came back.) A clip without
`canvas_bg`, or with `fit='cover'`, never reaches this module: its filter
text is unchanged byte for byte.

The engine's shader (`frontend/src/lib/preview/render`) mirrors each kind;
`GET /api/sessions/{sid}/canvas-bg/{clip_id}.png` serves the browser the SAME
cover-fitted image this module builds.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path

from .. import platformutil as _pu
from ..edl import canvas_blend as _cb

#: Bump when `image_file`'s pixels change for the same inputs. 2: a JPEG
#: larger than twice the canvas is decoded at a reduced DCT scale (review RE).
IMAGE_VERSION = 2
_LOCK = threading.Lock()

#: The largest picture a canvas background decodes (review RE: a 13000²
#: flat PNG of a few KB passed Pillow's bomb check with a warning and was
#: decoded in full — several hundred MB — on every render and every size the
#: engine asked for). 50 MP is a 100 MP phone's binned output with room.
MAX_PICTURE_PIXELS = 50_000_000
#: Picture kinds Pillow cannot decode in this build (no pillow-heif): ffmpeg
#: decodes them to a PNG once (a tiled iPhone HEIC included — ingest/still.py).
FFMPEG_PICTURES = (".heic", ".heif")


def check_picture(path: str | os.PathLike) -> tuple[int, int]:
    """(width, height) of a picture a canvas may use, reading the header
    only; ValueError (an editor-worded reason) when it is not a picture
    Pillow can read or is larger than `MAX_PICTURE_PIXELS`."""
    p = Path(path)
    try:
        from PIL import Image
        with Image.open(p) as im:
            w, h = im.size
            if w * h > MAX_PICTURE_PIXELS:
                raise ValueError(f"{p.name} is {w}×{h} — larger than the {MAX_PICTURE_PIXELS // 1_000_000} MP a "
                                 f"background picture may be; export a smaller copy")
            im.verify()
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 — Pillow's bomb error, a bad header, anything: same answer
        raise ValueError(f"{p.name} could not be read as a picture") from e
    return w, h


def pillow_readable(path: str | os.PathLike) -> Path:
    """`path` itself, or for a HEIC / HEIF a PNG next to it that ffmpeg
    decoded (once: reused while newer than the original). ValueError when
    ffmpeg cannot read it."""
    src = Path(path)
    if src.suffix.lower() not in FFMPEG_PICTURES:
        return src
    dst = src.with_name(src.name + ".png")
    try:
        if dst.is_file() and dst.stat().st_size > 0 and dst.stat().st_mtime_ns >= src.stat().st_mtime_ns:
            return dst
    except OSError:
        pass
    import subprocess
    part = _pu.part_path(dst)
    try:
        proc = subprocess.run([_pu.FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
                               "-update", "1", "-f", "image2", "-c:v", "png", str(part)],
                              capture_output=True, timeout=120, **_pu.SUBPROCESS_FLAGS)
        if proc.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            raise ValueError(f"{src.name} could not be read as a picture")
        _pu.replace_with_retry(part, dst)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"{src.name} could not be read as a picture") from e
    finally:
        _pu.unlink_with_retry(part)
    return dst


def _default_dir() -> Path:
    return Path(tempfile.gettempdir()) / "vae-canvas-bg"


def active(c) -> bool:
    """Does this clip render a canvas background (contain fit + canvas_bg)?"""
    bg = getattr(c, "canvas_bg", None)
    return bg is not None and getattr(c, "fit", "contain") == "contain"


def _image_key(src: Path, w: int, h: int) -> str:
    st = src.stat()
    raw = f"{IMAGE_VERSION}|{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{w}x{h}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def image_file(path: str | os.PathLike | None, w: int, h: int,
               cache_dir: Path | None = None) -> Path | None:
    """`path` cover-fitted to exactly w×h (centre crop, EXIF orientation
    applied, alpha over black), as a cached RGB PNG; None when the picture
    is missing or unreadable (the render then shows black bars, the same as
    no background)."""
    if not path:
        return None
    src = Path(path)
    try:
        if not src.is_file():
            return None
        key = _image_key(src, int(w), int(h))
    except OSError:
        return None
    root = Path(cache_dir) if cache_dir is not None else _default_dir()
    dst = root / f"cbg_{key}.png"
    if dst.is_file() and dst.stat().st_size > 0:
        try:
            from .cache_budget import touch
            touch(dst)
        except Exception:
            pass
        return dst
    with _LOCK:
        if dst.is_file() and dst.stat().st_size > 0:
            return dst
        try:
            from PIL import Image, ImageOps
            with Image.open(src) as im:
                if im.size[0] * im.size[1] > MAX_PICTURE_PIXELS:
                    return None                  # refused at upload; a hostile .vae lands here
                if im.format == "JPEG" and min(im.size[0] / max(1, w), im.size[1] / max(1, h)) >= 2:
                    im.draft("RGB", (int(w), int(h)))    # DCT-scaled decode, still ≥ the cover size
                im = ImageOps.exif_transpose(im)
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    base = Image.new("RGBA", im.size, (0, 0, 0, 255))
                    base.alpha_composite(im)
                    im = base
                im = im.convert("RGB")
                fitted = ImageOps.fit(im, (int(w), int(h)), method=Image.Resampling.BILINEAR,
                                      centering=(0.5, 0.5))
            root.mkdir(parents=True, exist_ok=True)
            tmp = _pu.part_path(dst)
            fitted.save(tmp, format="PNG")
            _pu.replace_with_retry(tmp, dst)
        except Exception:
            return None
    return dst


_TAGS: dict[tuple[str, int, int], tuple[str, str]] = {}
_TAGS_LOCK = threading.Lock()


def source_color_tags(src) -> tuple[str, str]:
    """(colorspace, range) ffprobe reports for `src`'s first video stream —
    "unknown" when untagged or unprobeable. Cached on path+mtime+size."""
    try:
        st = os.stat(src)
    except (OSError, TypeError):
        return "unknown", "unknown"
    key = (os.fspath(src), st.st_mtime_ns, st.st_size)
    with _TAGS_LOCK:
        hit = _TAGS.get(key)
    if hit is not None:
        return hit
    try:
        import json
        from . import cancel as _cancel
        out = _cancel.run_prioritised(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_space,color_range", "-of", "json", os.fspath(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        streams = json.loads(out.stdout or "{}").get("streams") or []
        s0 = streams[0] if streams else {}
        ans = (str(s0.get("color_space") or "unknown"), str(s0.get("color_range") or "unknown"))
    except Exception:  # noqa: BLE001 — unknown this time; asked again next render
        return "unknown", "unknown"
    with _TAGS_LOCK:
        if len(_TAGS) > 512:
            _TAGS.clear()
        _TAGS[key] = ans
    return ans


def pad_colour(fit_decrease: str, W: int, H: int, hexv: str, src) -> str:
    """`pad` in colour `hexv` so a BT.709 decode shows exactly that colour
    (±2 levels). `pad` converts its colour with the LINK's colorspace, and an
    untagged source's link is "unknown", which it treats as BT.601 (measured:
    #E53935 read back 243,72,49 under BT.709). The project's colour domain is
    BT.709 limited (INSTANT_PREVIEW_SPEC R12, what the engine's proxies and
    WebKit use), so an untagged link is labelled BT.709 for the pad alone and
    the source's own tags are put back after it: the exported file's tags do
    not change. A tagged source keeps its tag (the pad follows it, as the
    viewer's decode does)."""
    space, rng = source_color_tags(src)
    pre = post = ""
    if space in ("unknown", "unspecified", "reserved"):
        pre = "setparams=colorspace=bt709" + (":range=tv" if rng == "unknown" else "") + ","
        post = ",setparams=colorspace=unknown" + (":range=unknown" if rng == "unknown" else "")
    # pinned to 4:2:0: a later gbrp (opacity) otherwise pulls the format
    # negotiation up through `pad`, which then paints the colour in RGB and
    # the stream's own (BT.601 when untagged) matrix converts it — measured
    # (47, 126, 81) for #40A060 at opacity 0.875 where (56, 140, 84) is due.
    return (f"{fit_decrease},format=yuv420p,{pre}pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x{hexv}{post},"
            f"setsar=1")


#: ffprobe color_space -> the swscale matrix it was encoded with; anything
#: else (bt709, unknown, untagged) is read as BT.709 — the proxies' table
#: (`ingest/proxy._SWS_MATRIX`, INSTANT_PREVIEW_SPEC R12).
_SWS_MATRIX = {"smpte170m": "bt601", "bt470bg": "bt601", "bt2020nc": "bt2020",
               "bt2020c": "bt2020", "smpte240m": "smpte240m", "fcc": "fcc"}

#: What every main-track segment is labelled before assembly.
BASE_709 = "setparams=colorspace=bt709:range=tv"


def segment_to_709(src) -> str:
    """The filter fragment that makes one main-track SEGMENT (a clip whose
    source is `src`, or a black filler for None) BT.709 limited before
    `concat` / `xfade` (final QA, round 3).

    ffmpeg 8 negotiates colour tags across those inputs, so the FIRST
    clip's tag decided the lane and every other clip was CONVERTED to it:
    an untagged clip after a BT.709 one went BT.601 -> BT.709 (red 81,90,240
    exported 62,102,239), a tagged one after an untagged one the other way —
    in the export only (the preview and the proxies read untagged as BT.709).
    Now nothing is left to negotiate: an untagged or BT.709-limited segment
    is only LABELLED (its bytes pass as they are), and any other matrix or
    full range is converted as the proxies convert it
    (`ingest/proxy.scale_filter`). The composed base, and so the exported
    file, is BT.709 limited."""
    space, rng = source_color_tags(src) if src else ("unknown", "unknown")
    mtx = _SWS_MATRIX.get(space, "bt709")
    r = "pc" if rng == "pc" else "tv"
    if mtx == "bt709" and r == "tv":
        return BASE_709
    return (f"scale=in_range={r}:out_range=tv:in_color_matrix={mtx}:out_color_matrix=bt709,"
            f"{BASE_709}")


def tags_filter(src) -> str:
    """`setparams` putting back `src`'s own colour tags on a stream this
    module converted through RGB. ffmpeg 8 negotiates colorspace/range per
    link: an `overlay` of a BT.709-labelled background under an untagged
    picture CONVERTED the picture (601 → 709, measured 24 dB off), so the
    converted branch must carry exactly the tags the picture carries."""
    space, rng = source_color_tags(src)
    space = space if space not in ("", "unspecified", "reserved") else "unknown"
    rng = rng if rng in ("tv", "pc") else "unknown"
    return f"setparams=colorspace={space}:range={rng}"


def picture_overlay_tags(base_src) -> tuple[str, str]:
    """(pre, post) `setparams` put around an `overlay` that composites an
    RGB(A) PICTURE — a sticker or text PNG, a PNG / JPEG overlay clip — onto
    the composed base whose first main-track clip is `base_src` (None: an
    empty v1, the untagged black filler).

    Review RE (INSTANT_PREVIEW_SPEC R12, the `pad_colour` rule): `overlay`
    converts the picture to the base's YUV with the base LINK's colorspace,
    and an untagged link is "unknown" — swscale's BT.601. So a #FF2828
    sticker exported at luma 106 where BT.709 (what the engine, WebKit and
    the blend path use) gives 90: Normal and Lighten-over-black of the same
    PNG differed by 16 levels. An untagged base is labelled BT.709 for the
    overlay alone and its own tags are put back after it (the file's tags do
    not change). A tagged base keeps its tag — the viewer decodes it so."""
    # Final QA (round 3): the composed base is ALWAYS BT.709 limited now
    # (`segment_to_709` labels or converts every main-track segment before
    # assembly), so `overlay` already converts with BT.709 and nothing needs
    # relabelling around it. Kept as the one place that says so.
    del base_src
    return "", ""


def base_src_of(edl) -> str | None:
    """The first main-track clip's source — whose tags the composed base
    carries (concat negotiates the lane to its first input)."""
    from ..edl.schema import Clip
    v1 = edl.get_track("v1")
    first = next((c for c in sorted(v1.clips if v1 else [], key=lambda c: c.start) if isinstance(c, Clip)),
                 None)
    return str(first.src) if first is not None else None


_PIX: dict[tuple[str, int, int], str] = {}


def is_picture_source(src) -> bool:
    """Does `src` decode to RGB(A) (a PNG / BMP / WebP / GIF…) or JPEG-range
    YUV — a picture whose colours `overlay` must convert with the project's
    matrix (`picture_overlay_tags`)? A video stream (yuv420p…) is not: it is
    passed through as its own YUV. Probed once per file."""
    try:
        st = os.stat(src)
    except (OSError, TypeError):
        return False
    key = (os.fspath(src), st.st_mtime_ns, st.st_size)
    fmt = _PIX.get(key)
    if fmt is None:
        try:
            import json
            from . import cancel as _cancel
            out = _cancel.run_prioritised(
                [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                 "-of", "json", os.fspath(src)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            streams = json.loads(out.stdout or "{}").get("streams") or []
            fmt = str((streams[0] if streams else {}).get("pix_fmt") or "")
        except Exception:  # noqa: BLE001 — unknown: treat as video (the old behaviour)
            return False
        if len(_PIX) > 512:
            _PIX.clear()
        _PIX[key] = fmt
    return fmt.startswith(("rgb", "bgr", "argb", "abgr", "rgba", "bgra", "pal8", "ya8", "yuvj", "gbr"))


def _rgb_to_yuv_matrix(src) -> str:
    """The matrix an RGB picture joins the clip's YUV with: BT.709 unless the
    source is tagged BT.601 (see `pad_colour`)."""
    space, _rng = source_color_tags(src)
    return "bt601" if space in ("bt470bg", "smpte170m") else "bt709"


#: The blur runs in RGB (BT.709, full range) — the domain the engine's
#: shader blurs its decoded texture in. In YUV, gblur blurs the half-size
#: chroma planes with the luma sigma (twice as wide on screen) and before the
#: gamut clip the browser's RGB has already taken (measured on testsrc2:
#: 27.6 dB against an RGB Gaussian, 31.1 in yuv444p).
_TO_RGB = "scale=in_color_matrix=bt709:in_range=tv:out_range=pc"
_TO_YUV_OPTS = "out_color_matrix=bt709:out_range=tv"


def _label_uid(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", uid) or "x"


def blur_dims(canvas_w: int, canvas_h: int) -> tuple[int, int]:
    """The size the blurred copy is built at: 1/CANVAS_BLUR_DOWNSCALE of the
    canvas, even, at least 2×2. geometry.ts `canvasBlurDims` mirrors it."""
    d = _cb.CANVAS_BLUR_DOWNSCALE
    bw = max(2, int(round(canvas_w / d)) // 2 * 2)
    bh = max(2, int(round(canvas_h / d)) // 2 * 2)
    return bw, bh


def blur_sigma_small(level, canvas_w: int, canvas_h: int) -> float:
    """The sigma applied at `blur_dims` (the canvas sigma over the downscale)."""
    bw, _bh = blur_dims(canvas_w, canvas_h)
    return _cb.blur_sigma(level, canvas_w, canvas_h) * bw / canvas_w


def fit_block(c, *, canvas_w: int, canvas_h: int, fit_decrease: str, cover_scale_small: str,
              uid: str) -> str:
    """The filter text that REPLACES the contain branch's
    `{fit_decrease},pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1`.

    `fit_decrease` is the chain's own fitted-scale filter (anamorphic-aware);
    `cover_scale_small` scales the SOURCE to cover `blur_dims` (the same
    rules, `increase`). `uid` makes the internal labels unique in the graph.
    The text starts where the replaced filter started (it may open with a
    `split`), and ends on a canvas-sized frame with square pixels."""
    bg = c.canvas_bg
    W, H = int(canvas_w), int(canvas_h)
    pad_black = f"{fit_decrease},pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    centre = "x=trunc((main_w-overlay_w)/2):y=trunc((main_h-overlay_h)/2)"
    u = _label_uid(uid)
    if bg.type == "color":
        hexv = _cb.normalize_color(bg.color)[1:]
        return pad_colour(fit_decrease, W, H, hexv, getattr(c, "src", None))
    if bg.type == "blur":
        bw, bh = blur_dims(W, H)
        sig = blur_sigma_small(bg.blur, W, H)
        return (
            f"split=2[cbs{u}][cbf{u}];"
            f"[cbs{u}]{cover_scale_small},crop={bw}:{bh},setsar=1,"
            f"{_TO_RGB},format=gbrp,gblur=sigma={sig:.4f}:steps=4,"
            f"scale={W}:{H}:flags=bilinear:{_TO_YUV_OPTS},format=yuv420p,"
            f"{tags_filter(getattr(c, 'src', None))},setsar=1[cbb{u}];"
            f"[cbf{u}]{fit_decrease},setsar=1[cbp{u}];"
            f"[cbb{u}][cbp{u}]overlay={centre},setsar=1"
        )
    # image
    img = image_file(bg.image, W, H)
    if img is None:
        return pad_black
    return (
        f"{fit_decrease},setsar=1,split=2[cif{u}][cip{u}];"
        f"[cip{u}]pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[cib{u}];"
        f"movie=filename={_pu.ffmpeg_filter_path(img)},"
        f"scale=out_color_matrix={_rgb_to_yuv_matrix(getattr(c, 'src', None))}:out_range=tv,"
        f"format=yuv420p,{tags_filter(getattr(c, 'src', None))},setsar=1[cii{u}];"
        f"[cib{u}][cii{u}]overlay=x=0:y=0:eof_action=repeat[cig{u}];"
        f"[cig{u}][cif{u}]overlay={centre},setsar=1"
    )


def _colour_frame(W: int, H: int, hexv: str, src) -> str:
    """A canvas-sized frame of solid `hexv` from any input frame (its
    timestamps kept): a 2×2 copy padded in the colour, the copy cropped away.
    The colour is converted as `pad_colour` converts it (BT.709 on an
    untagged link, the source's tags put back)."""
    space, rng = source_color_tags(src)
    pre = post = ""
    if space in ("unknown", "unspecified", "reserved"):
        pre = "setparams=colorspace=bt709" + (":range=tv" if rng == "unknown" else "") + ","
        post = ",setparams=colorspace=unknown" + (":range=unknown" if rng == "unknown" else "")
    return (f"scale=2:2,format=yuv420p,{pre}pad={W + 2}:{H}:0:0:color=0x{hexv}{post},"
            f"crop={W}:{H}:2:0,setsar=1")


def background_chain(c, *, canvas_w: int, canvas_h: int, cover_scale_small: str, uid: str) -> str:
    """The clip's canvas background as its OWN canvas-sized stream, from the
    clip's source frames (no labels; it ends on the frame): colour, the
    cover-scaled Gaussian blur, or the cover-fitted picture (black when the
    picture is missing, like no background)."""
    bg = c.canvas_bg
    W, H = int(canvas_w), int(canvas_h)
    src = getattr(c, "src", None)
    u = _label_uid(uid)
    if bg.type == "color":
        return _colour_frame(W, H, _cb.normalize_color(bg.color)[1:], src)
    if bg.type == "blur":
        bw, bh = blur_dims(W, H)
        sig = blur_sigma_small(bg.blur, W, H)
        # a copy of the picture: mirrored with it (Transform.flip_h / flip_v)
        tx = getattr(c, "transform", None)
        flips = ("".join(f",{f}" for f, on in (("hflip", getattr(tx, "flip_h", False)),
                                               ("vflip", getattr(tx, "flip_v", False))) if on))
        return (f"{cover_scale_small},crop={bw}:{bh},setsar=1,"
                f"{_TO_RGB},format=gbrp,gblur=sigma={sig:.4f}:steps=4,"
                f"scale={W}:{H}:flags=bilinear:{_TO_YUV_OPTS},format=yuv420p,"
                f"{tags_filter(src)},setsar=1{flips}")
    img = image_file(bg.image, W, H)
    if img is None:
        return _colour_frame(W, H, "000000", src)
    return (f"scale={W}:{H},format=yuv420p,setsar=1[cvi0{u}];"
            f"movie=filename={_pu.ffmpeg_filter_path(img)},"
            f"scale=out_color_matrix={_rgb_to_yuv_matrix(src)}:out_range=tv,"
            f"format=yuv420p,{tags_filter(src)},setsar=1[cvi1{u}];"
            f"[cvi0{u}][cvi1{u}]overlay=x=0:y=0:eof_action=repeat,setsar=1")


def composite_block(c, *, canvas_w: int, canvas_h: int, fit_decrease: str, cover_scale_small: str,
                    geom: str, pic_extra: str = "", mask_extra: str = "", alpha_stage: str = "",
                    uid: str) -> str:
    """The v1 geometry of a clip WITH a canvas background (review RE): the
    background is a STATIC, canvas-sized layer, and only the fitted picture
    moves — CapCut's Canvas. It used to be baked into the fitted frame before
    the transform and the animation, so Zoom In / Slide / a scale < 1 / a
    rotation moved or shrank the background too and black bars came back
    (scale 0.7 on a 9:16 blur: 51 % black; rotation 15°: 12 %).

    `geom` is the chain's own geometry from the fitted picture on
    (`format=yuv420p,pad=W:H:…:black` then flip, rotation, scale / pan, keys
    and animation); it runs twice with the SAME filters and rounding: over
    the picture (+ `pic_extra`, the clip's effects) and over a white matte of
    the fitted picture (+ `mask_extra`, the legacy flip effects), whose luma
    becomes the picture's alpha. `alpha_stage` then multiplies in the
    opacity and the animation's alpha ramps (so the picture fades over the
    background, as the black bars always showed it), and the picture is laid
    on the background. Starts where the replaced fit started (with a
    `split`), ends on a canvas-sized frame, no label."""
    u = _label_uid(uid)
    bgc = background_chain(c, canvas_w=canvas_w, canvas_h=canvas_h, cover_scale_small=cover_scale_small,
                           uid=uid)
    return (
        f"split=2[cvs{u}][cvp{u}];"
        f"[cvs{u}]{bgc}[cvb{u}];"
        f"[cvp{u}]{fit_decrease},setsar=1,split=2[cvf{u}][cvm{u}];"
        f"[cvf{u}]{geom}{pic_extra}[cvq{u}];"
        f"[cvm{u}]format=yuv420p,lutyuv=y=235:u=128:v=128,{geom}{mask_extra},format=gray[cva{u}];"
        f"[cvq{u}][cva{u}]alphamerge{alpha_stage}[cvo{u}];"
        f"[cvb{u}][cvo{u}]overlay=x=0:y=0,setsar=1"
    )


__all__ = ["IMAGE_VERSION", "active", "source_color_tags", "pad_colour", "tags_filter",
           "segment_to_709", "BASE_709", "image_file", "blur_dims",
           "blur_sigma_small", "fit_block", "background_chain", "composite_block"]
