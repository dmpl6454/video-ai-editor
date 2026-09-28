"""Geometry goldens for the instant-preview compositor (Wave D, spec §3.4, §13).

The client's WebGL compositor must place a clip's picture exactly where the
export's ffmpeg chain (`render/compositor.py::_build_clip_video_chain`) puts
it: fit/cover, rotate in place, crop-zoom/pan, keyframed transforms, flip,
opacity and video fades. Those rules EMERGE from ffmpeg's integer handling
(scale's aspect rounding, pad/crop offsets snapped to the chroma grid, crop
clamping) and from the time the keyframe expressions see, so they are pinned
here by MEASUREMENT: marker sources (flat grey, 5 coloured squares at known
places) are rendered through the real compositor, and every output frame
we keep records

* the canvas centroid of each fully visible marker,
* the picture's bounding box (pixels brighter than half the grey), and
* the gain (median grey / 128), for opacity and fades.

``frontend/src/lib/preview/render/geometry.test.ts`` asserts that
``geometry.ts`` reproduces every number (tests/goldens/geometry_cases.json).
Regenerate with ``.venv/bin/python tests/gen_geometry_goldens.py``.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from video_ai_editor.edl.schema import EDL, Canvas, CanvasBackground, Clip, Effect, Keyframe, empty_edl
from video_ai_editor.render import compositor
from video_ai_editor.render.frame_map import SourceInfo
from video_ai_editor.render.sar import display_width, parse_sar

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "goldens" / "geometry_cases.json"
VERSION = 1
GREY = 128
#: name → (u, v) centre in the source (normalised) and RGB colour.
MARKERS: dict[str, tuple[tuple[float, float], tuple[int, int, int]]] = {
    "red": ((0.25, 0.25), (255, 0, 0)),
    "green": ((0.75, 0.25), (0, 255, 0)),
    "blue": ((0.25, 0.75), (0, 0, 255)),
    "yellow": ((0.75, 0.75), (255, 255, 0)),
    "white": ((0.5, 0.5), (255, 255, 255)),
}


@dataclass(frozen=True)
class Src:
    key: str
    w: int
    h: int
    rate: Fraction = Fraction(30)
    frames: int = 90
    #: sample aspect ratio (anamorphic: stored w x h, DISPLAYED w·sar x h)
    sar: Fraction | None = None


SOURCES: dict[str, Src] = {s.key: s for s in (
    Src("land", 1280, 720),
    Src("port", 720, 1280),
    Src("odd", 702, 1280),
    Src("wide", 1280, 706),
    Src("sq", 1000, 1000),
    Src("land25", 1280, 720, Fraction(25), 75),
    Src("odd25", 962, 540, Fraction(25), 75),
    # anamorphic masters (Wave D3, lane E1a): PAL 4:3 (720x576 SAR 16:15,
    # displayed 768x576), PAL 16:9 (SAR 64:45, 1024x576), HDV (1440x1080
    # SAR 4:3, 1920x1080)
    Src("pal43", 720, 576, Fraction(25), 50, Fraction(16, 15)),
    Src("pal169", 720, 576, Fraction(25), 50, Fraction(64, 45)),
    Src("hdv", 1440, 1080, Fraction(30), 60, Fraction(4, 3)),
)}


def marker_size(s: Src) -> int:
    return max(12, min(s.w, s.h) // 15) // 2 * 2


def make_source(path: Path, s: Src) -> Path:
    """Flat grey with five coloured squares; lossless-ish 4:2:0 H.264."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    m = marker_size(s)
    boxes = ",".join(
        f"drawbox=x={round(u * s.w) - m // 2}:y={round(v * s.h) - m // 2}:w={m}:h={m}"
        f":color=0x{r:02x}{g:02x}{b:02x}:t=fill"
        for (u, v), (r, g, b) in MARKERS.values())
    rate = f"{s.rate.numerator}/{s.rate.denominator}"
    sar = f",setsar={s.sar.numerator}/{s.sar.denominator}" if s.sar else ""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=0x{GREY:02x}{GREY:02x}{GREY:02x}:s={s.w}x{s.h}:r={rate}:d={float(s.frames / s.rate) + 1}",
         "-vf", f"{boxes}{sar},format=yuv420p", "-frames:v", str(s.frames),
         "-c:v", "libx264", "-qp", "0", "-preset", "veryfast", str(path)],
        check=True, capture_output=True)
    return path


def probe(path: Path) -> SourceInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,time_base,start_pts,nb_read_frames,width,height,"
         "sample_aspect_ratio:format=start_time", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    st = d["streams"][0]
    tbase = Fraction(st["time_base"])
    file_start = Fraction(d["format"].get("start_time") or "0")
    return SourceInfo(rate=Fraction(st["r_frame_rate"]), time_base=tbase,
                      frames=int(st["nb_read_frames"]),
                      start_ticks=int(st.get("start_pts") or 0) - int(file_start / tbase),
                      # the DISPLAYED size, as the proxy probe reports it
                      width=display_width(int(st["width"]), parse_sar(st.get("sample_aspect_ratio"))),
                      height=int(st["height"]))


# ------------------------------------------------------------------ cases

@dataclass
class ClipSpec:
    src: str
    start: float = 0.0
    in_: float = 0.0
    out: float = 1.0
    speed: float | dict | None = None
    fit: str = "contain"
    tx: dict[str, Any] = field(default_factory=dict)
    effects: tuple[str, ...] = ()
    fade_in: float = 0.0
    fade_out: float = 0.0
    measure: bool = True
    freeze: float | None = None
    reverse: bool = False
    #: other Clip fields set verbatim (a Canvas background, a clip animation)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Case:
    name: str
    clips: list[ClipSpec]
    ks: list[int]
    canvas: tuple[int, int] = (640, 360)
    fps: int = 30
    #: gain cases keep identity geometry and skip the bbox (it is threshold-based)
    gain_only: bool = False


def kf(*pts: tuple[float, float], interp: str = "linear") -> dict:
    return {"keyframes": [list(p) for p in pts], "interp": interp}


def cases() -> list[Case]:
    L = "land"
    return [
        # ---- fit
        Case("identity", [ClipSpec(L)], [0]),
        Case("contain_portrait", [ClipSpec("port")], [0]),
        Case("cover_portrait", [ClipSpec("port", fit="cover")], [0]),
        Case("contain_odd", [ClipSpec("odd")], [0]),
        Case("cover_odd", [ClipSpec("odd", fit="cover")], [0]),
        Case("contain_wide", [ClipSpec("wide")], [0]),
        Case("cover_wide", [ClipSpec("wide", fit="cover")], [0]),
        Case("contain_square", [ClipSpec("sq")], [0]),
        Case("contain_odd_25fps", [ClipSpec("odd25")], [0, 11]),
        Case("contain_land_in_portrait", [ClipSpec(L)], [0], canvas=(360, 640)),
        Case("cover_land_in_portrait", [ClipSpec(L, fit="cover")], [0], canvas=(360, 640)),
        Case("cover_pan", [ClipSpec("port", fit="cover", tx={"x": 30.0, "y": -500.0})], [0]),
        Case("cover_pan_zoom", [ClipSpec("port", fit="cover", tx={"x": 30.0, "y": -50.0, "scale": 1.3})], [0]),
        Case("cover_pan_land_in_portrait", [ClipSpec(L, fit="cover", tx={"x": -120.0, "y": 3.0, "scale": 1.15})], [0],
             canvas=(360, 640)),
        # ---- rotate
        Case("rotate_20", [ClipSpec(L, tx={"rotation": 20.0})], [0]),
        Case("rotate_neg35_contain_portrait", [ClipSpec("port", tx={"rotation": -35.0})], [0]),
        # ---- static transform (crop-zoom / pan)
        Case("scale_05", [ClipSpec("port", tx={"scale": 0.5})], [0]),
        Case("scale_073_pan", [ClipSpec(L, tx={"scale": 0.73, "x": -33.4, "y": 12.6})], [0]),
        Case("zoom_137_pan", [ClipSpec(L, tx={"scale": 1.37, "x": 21.0})], [0]),
        # review RD3: a static opacity < 1 must not move an odd pan (a gbrp
        # downstream pulled pad/crop out of 4:2:0 and dropped its rounding)
        Case("scale_05_pan1", [ClipSpec(L, tx={"scale": 0.5, "x": 1.0, "y": 1.0})], [0]),
        Case("scale_05_pan1_opacity", [ClipSpec(L, tx={"scale": 0.5, "x": 1.0, "y": 1.0,
                                                       "opacity": 0.9})], [0]),
        Case("pan_only", [ClipSpec(L, tx={"x": 100.0, "y": 40.0})], [0]),
        Case("pan_half_pixels", [ClipSpec(L, tx={"x": 12.5, "y": -7.5})], [0]),
        Case("zoom_cover_rotate", [ClipSpec("odd", fit="cover", tx={"scale": 1.2, "rotation": 7.0})], [0]),
        # ---- keyframed transform
        Case("kf_x_zoom", [ClipSpec(L, tx={"x": kf((0, 0), (1, 200)), "scale": 1.5})], [0, 7, 15, 22, 29]),
        Case("kf_scale", [ClipSpec(L, tx={"scale": kf((0, 1), (1, 2)), "x": 30.0})], [0, 10, 20, 29]),
        Case("kf_scale_below_one", [ClipSpec(L, tx={"scale": kf((0, 0.5), (1, 0.8))})], [0, 15]),
        Case("kf_start_offset", [ClipSpec(L, out=1.0), ClipSpec(L, start=1.0, out=2.0,
                                                                   tx={"x": kf((0, 0), (2, 300)), "scale": 2.0})],
             [30, 45, 60, 75, 89]),
        Case("kf_speed2", [ClipSpec(L, out=2.0, speed=2.0, tx={"x": kf((0, 0), (2, 200)), "scale": 2.0})],
             [0, 10, 20, 29]),
        Case("kf_rotation", [ClipSpec(L, tx={"rotation": kf((0, 0), (1, 90))})], [0, 10, 20, 29]),
        # the keyframe clock is the OUTPUT frame's clip-local time (Wave D3):
        # a 0.5x clip animates every frame, a freeze animates over its still,
        # a curve and a reversed 2x clip key on timeline time
        Case("kf_speed_half", [ClipSpec(L, out=1.0, speed=0.5, tx={"x": kf((0, 0), (2, 200)), "scale": 2.0})],
             [0, 1, 2, 3, 31, 59]),
        Case("kf_freeze", [ClipSpec(L, in_=0.5, out=0.5 + 1 / 30, freeze=1.0,
                                    tx={"rotation": kf((0, 0), (1, 30))})], [0, 1, 15, 29]),
        Case("kf_freeze_opacity", [ClipSpec(L, in_=0.5, out=0.5 + 1 / 30, freeze=1.0,
                                            tx={"opacity": kf((0, 1), (1, 0.3))})], [0, 1, 15, 29], gain_only=True),
        Case("kf_opacity_speed_half", [ClipSpec(L, out=1.0, speed=0.5, tx={"opacity": kf((0, 1), (2, 0.2))},
                                                fade_out=0.5)], [0, 1, 2, 3, 45, 50, 55, 59], gain_only=True),
        Case("kf_curve", [ClipSpec(L, out=2.0, speed={"curve": [[0, 1], [0.5, 0.4], [1, 1]]},
                                   tx={"y": kf((0, -60), (2.5, 60)), "scale": 1.6})], [0, 1, 20, 41, 60]),
        Case("kf_reverse_2x", [ClipSpec(L, in_=0.5, out=2.5, speed=2.0, reverse=True,
                                        tx={"x": kf((0, -100), (1, 100)), "scale": 1.5})], [0, 7, 15, 29]),
        Case("kf_shrink_about_centre", [ClipSpec(L, tx={"scale": kf((0, 0.5), (1, 1.5)), "y": 20.0})],
             [0, 15, 29]),
        Case("kf_single_key_ignored", [ClipSpec(L, tx={"x": kf((0, 300)), "rotation": kf((0, 45))})], [0]),
        Case("kf_ease_in_out", [ClipSpec(L, tx={"y": kf((0, -100), (1, 100), interp="ease-in-out"), "scale": 1.8})],
             [0, 8, 15, 23, 29]),
        Case("kf_25fps_source", [ClipSpec("land25", out=2.0, tx={"x": kf((0, 0), (2, 240)), "scale": 1.6})],
             [0, 13, 31, 59]),
        # ---- flip
        Case("hflip_pan", [ClipSpec(L, tx={"x": 100.0}, effects=("hflip",))], [0]),
        Case("vflip_rotate", [ClipSpec(L, tx={"rotation": 15.0}, effects=("vflip",))], [0]),
        Case("hvflip_contain_portrait", [ClipSpec("port", effects=("hflip", "vflip"))], [0]),
        # ---- opacity and fades (gain)
        Case("opacity_05", [ClipSpec(L, tx={"opacity": 0.5})], [0], gain_only=True),
        Case("opacity_kf", [ClipSpec(L, tx={"opacity": kf((0, 1), (1, 0))})], [0, 10, 20, 29], gain_only=True),
        Case("fades_speed2", [ClipSpec(L, out=2.0, speed=2.0, fade_in=0.5, fade_out=0.5)],
             list(range(0, 30)), gain_only=True),
        Case("fades_25fps_in_30", [ClipSpec("land25", out=2.0, fade_in=0.4, fade_out=0.7)],
             list(range(0, 60, 3)), gain_only=True),
        Case("fade_longer_than_clip", [ClipSpec(L, out=0.5, fade_in=3.0)], list(range(0, 15)), gain_only=True),
        Case("fade_and_opacity", [ClipSpec(L, out=1.0, fade_out=0.5, tx={"opacity": 0.8})], [0, 20, 25, 29],
             gain_only=True),
        # wave E gate (X2): a clip cut at in > 0 runs on the in-anchored clock
        # with NEGATIVE pre-roll pts, and vf_fade (uint64 start) came out
        # unfaded / black. Every fade case at in > 0: the retimed source
        # frame's time T = (pts - in) / speed is the non-keyframed fade clock.
        Case("fades_in_offset", [ClipSpec(L, in_=1.0, out=2.0, fade_in=0.4, fade_out=0.4)],
             list(range(0, 30)), gain_only=True),
        Case("fades_in_offgrid", [ClipSpec(L, in_=0.52, out=1.52, fade_in=0.4, fade_out=0.4)],
             list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_speed2", [ClipSpec(L, in_=0.8, out=2.8, speed=2.0, fade_in=0.5, fade_out=0.5)],
             list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_speed_half", [ClipSpec(L, in_=0.61, out=1.11, speed=0.5, fade_in=0.4,
                                                     fade_out=0.3)], list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_25fps_in_30", [ClipSpec("land25", in_=0.52, out=2.0, fade_in=0.4, fade_out=0.7)],
             list(range(0, 44, 2)), gain_only=True),
        Case("fades_in_offset_curve", [ClipSpec(L, in_=0.9, out=2.4, speed={"curve": [[0, 1], [0.5, 0.5], [1, 1]]},
                                                fade_in=0.4, fade_out=0.4)], list(range(0, 60, 2)), gain_only=True),
        Case("fades_in_offset_freeze", [ClipSpec(L, in_=1.2, out=1.2 + 1 / 30, freeze=1.0, fade_in=0.3,
                                                 fade_out=0.3)], list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_reverse_2x", [ClipSpec(L, in_=0.5, out=2.5, speed=2.0, reverse=True,
                                                     fade_in=0.3, fade_out=0.4)], list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_kf_opacity", [ClipSpec(L, in_=1.0, out=2.0, fade_in=0.3, fade_out=0.3,
                                                     tx={"opacity": kf((0, 1), (1, 0.5))})],
             list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_anim", [ClipSpec(L, in_=1.0, out=2.0, fade_in=0.3, fade_out=0.3,
                                               extra={"anim_in": "fade_in", "anim_dur": 0.5})],
             list(range(0, 30)), gain_only=True),
        Case("fades_in_offset_canvas_bg", [ClipSpec("port", in_=1.0, out=2.0, fade_in=0.4, fade_out=0.4,
                                                    extra={"canvas_bg": CanvasBackground(type="color",
                                                                                         color="#0000FF")})],
             list(range(0, 30)), gain_only=True),
        # ---- anamorphic sources: fitted by their DISPLAYED shape
        Case("contain_pal43", [ClipSpec("pal43")], [0]),
        Case("cover_pal43", [ClipSpec("pal43", fit="cover")], [0]),
        Case("contain_pal169_portrait", [ClipSpec("pal169")], [0], canvas=(360, 640)),
        Case("cover_pal169", [ClipSpec("pal169", fit="cover")], [0]),
        Case("contain_hdv_portrait", [ClipSpec("hdv")], [0], canvas=(360, 640)),
        Case("cover_pan_hdv", [ClipSpec("hdv", fit="cover", tx={"x": 40.0, "y": -30.0, "scale": 1.25})], [0],
             canvas=(360, 640)),
        Case("scale_pan_rotate_pal43", [ClipSpec("pal43", tx={"scale": 0.8, "x": 40.0, "y": -12.0, "rotation": 10.0})],
             [0]),
        Case("kf_pan_pal169", [ClipSpec("pal169", out=1.2, speed=0.5,
                                        tx={"x": kf((0, -80), (2, 80)), "scale": 1.5})], [0, 1, 30, 59]),
        # ---- Transform.flip_h / flip_v (wave E): the fitted frame mirrored
        # BEFORE the rotation, scale and pan (the picture turns as set)
        Case("tflip_h_rotate", [ClipSpec(L, tx={"flip_h": True, "rotation": 20.0})], [0]),
        Case("tflip_v_contain_portrait", [ClipSpec("port", tx={"flip_v": True})], [0]),
        Case("tflip_hv_scale_pan", [ClipSpec(L, tx={"flip_h": True, "flip_v": True, "scale": 0.73,
                                                    "x": -33.4, "y": 12.6})], [0]),
        Case("tflip_h_cover_pan", [ClipSpec("port", fit="cover", tx={"flip_h": True, "x": 30.0, "y": -50.0,
                                                                     "scale": 1.3})], [0]),
        Case("tflip_v_cover_pan", [ClipSpec("port", fit="cover", tx={"flip_v": True, "y": -500.0})], [0]),
        Case("tflip_h_cover_zoom_rotate", [ClipSpec(L, fit="cover", tx={"flip_h": True, "scale": 1.2,
                                                                        "rotation": 7.0})], [0]),
        Case("tflip_h_kf", [ClipSpec(L, tx={"flip_h": True, "x": kf((0, 0), (1, 200)),
                                            "rotation": kf((0, 0), (1, 45)), "scale": 1.5})], [0, 15, 29]),
        Case("tflip_h_and_hflip_effect", [ClipSpec(L, tx={"flip_h": True, "rotation": 15.0},
                                                   effects=("hflip",))], [0]),
        Case("tflip_v_pal43", [ClipSpec("pal43", tx={"flip_v": True, "rotation": -10.0})], [0]),
        # ---- combined
        Case("combined_25fps_odd", [ClipSpec("odd25", fit="cover", out=2.0,
                                             tx={"scale": 1.2, "rotation": -12.0, "x": -40.0, "y": 25.0},
                                             effects=("hflip",))], [0, 30]),
    ]


def build_edl(case: Case, paths: dict[str, str] | None = None) -> EDL:
    paths = paths or {}
    e = empty_edl(Canvas(w=case.canvas[0], h=case.canvas[1], fps=case.fps))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    for i, cs in enumerate(case.clips):
        c = Clip(src=paths.get(cs.src, cs.src), start=cs.start, speed=cs.speed, id=f"c{i}")
        c.in_ = cs.in_
        c.out = cs.out
        c.fit = cs.fit
        for k, v in cs.tx.items():
            setattr(c.transform, k, Keyframe(**v) if isinstance(v, dict) else v)
        c.effects = [Effect(type=t) for t in cs.effects]
        c.video_fade_in = cs.fade_in
        c.video_fade_out = cs.fade_out
        if cs.freeze is not None:
            c.freeze = cs.freeze
        if cs.reverse:
            c.reverse = True
        for k, v in cs.extra.items():
            setattr(c, k, v)
        v1.clips.append(c)
    e.recompute_duration()
    return e


def edl_json(edl: EDL) -> dict:
    d = edl.model_dump(by_alias=True, mode="json")
    d["tracks"] = [t for t in d["tracks"] if t["id"] == "v1"]
    return d


# --------------------------------------------------------------- measuring

def decode_rgb(path: Path, w: int, h: int) -> np.ndarray:
    # RGB with the matrix the SOURCE was encoded with (`make_source`: lavfi's
    # BT.601, untagged): the export is tagged BT.709 since final QA round 3
    # (untagged reads as BT.709, INSTANT_PREVIEW_SPEC R12), and a default
    # decode would convert the markers' colours with another matrix than the
    # one that made them. The geometry measured here does not depend on it.
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                          "-vf", "scale=in_color_matrix=bt601:in_range=tv:out_range=pc,format=rgb24",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def _largest_blob(mask: np.ndarray) -> np.ndarray | None:
    """The largest 4-connected component of `mask` (another marker's blurred
    edge can come close to this marker's colour: yellow's edge to white)."""
    seen = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    h, w = mask.shape
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack, comp = [(int(y0), int(x0))], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(comp) > len(best):
            best = comp
    if not best:
        return None
    out = np.zeros_like(mask, dtype=bool)
    yy, xx = zip(*best)
    out[list(yy), list(xx)] = True
    return out


def decode_y(path: Path, w: int, h: int) -> np.ndarray:
    """The luma plane of every frame (limited range, as encoded)."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True).stdout
    per = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
    n = len(raw) // per
    return np.stack([np.frombuffer(raw[i * per:i * per + w * h], np.uint8).reshape(h, w) for i in range(n)])


#: Limited-range luma of the grey and of each marker colour (BT.601, which
#: is what the untagged masters are encoded with).
def _y601(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 16 + (0.299 * r + 0.587 * g + 0.114 * b) * 219 / 255


def measure_frame(fr: np.ndarray, *, gain_only: bool, luma: np.ndarray | None = None) -> dict:
    f = fr.astype(np.int32)
    lum = f.mean(axis=2)
    greyish = (np.abs(f[..., 0] - f[..., 1]) < 8) & (np.abs(f[..., 1] - f[..., 2]) < 8)
    out: dict[str, Any] = {}
    if gain_only:
        # Identity geometry: the grey covers the frame except the markers.
        out["gain"] = round(float(np.median(lum[greyish])) / GREY, 4)
        return out
    h, w = lum.shape
    grey = np.array([GREY, GREY, GREY])
    markers: dict[str, list[float] | None] = {}
    for name, (_uv, col) in MARKERS.items():
        target = np.array(col)
        core = _largest_blob(np.abs(f - target).sum(axis=2) < 90)
        if core is None or core.sum() < 4:
            markers[name] = None
            continue
        ys, xs = np.nonzero(core)
        x0, x1 = max(0, xs.min() - 4), min(w, xs.max() + 5)
        y0, y1 = max(0, ys.min() - 4), min(h, ys.max() + 5)
        win = f[y0:y1, x0:x1]
        # A marker cut by a crop, the frame edge or the black pad has a biased
        # centroid: only markers wholly surrounded by grey are recorded.
        edge = x0 == 0 or y0 == 0 or x1 == w or y1 == h
        dark = bool((win.mean(axis=2) < GREY / 3).any())
        if edge or dark:
            markers[name] = None
            continue
        # Weighted centroid on LUMA: each pixel's weight is how far its luma
        # has moved from the grey's toward the marker's. Sub-pixel, and free
        # of the chroma plane's half-resolution siting, which biased a colour
        # mask by up to 1.5 px on a 13 px marker.
        if luma is not None:
            yg, ym = _y601((GREY, GREY, GREY)), _y601(col)
            wt = np.clip((luma[y0:y1, x0:x1].astype(np.float64) - yg) / (ym - yg), 0.0, 1.0)
        else:
            span = float(np.abs(target - grey).sum())
            wt = np.clip(1.0 - np.abs(win - target).sum(axis=2) / span, 0.0, 1.0)
        gy, gx = np.mgrid[y0:y1, x0:x1]
        tot = float(wt.sum())
        markers[name] = [round(float((wt * gx).sum()) / tot + 0.5, 3), round(float((wt * gy).sum()) / tot + 0.5, 3)]
    out["markers"] = markers
    pic = lum > GREY / 2
    if pic.any():
        ys, xs = np.nonzero(pic)
        out["bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    else:
        out["bbox"] = None
    return out


def render_case(case: Case, paths: dict[str, str], work: Path, *, reuse: bool = False) -> list[dict]:
    edl = build_edl(case, paths)
    sess = work / case.name
    sess.mkdir(parents=True, exist_ok=True)
    w, h = case.canvas
    out = sess / "out.mp4"
    if not (reuse and out.is_file()):
        out = compositor._render(edl, out, height=h, fps=case.fps, preview=False,
                                 cache_dir=sess / "cache", chunked=False)
    frames = decode_rgb(out, w, h)
    lumas = decode_y(out, w, h)
    return [{"k": k, **measure_frame(frames[k], gain_only=case.gain_only, luma=lumas[k])} for k in case.ks]


def ensure_sources(work: Path) -> tuple[dict[str, str], dict[str, SourceInfo]]:
    paths, infos = {}, {}
    for s in SOURCES.values():
        p = make_source(work / "src" / f"{s.key}.mp4", s)
        paths[s.key] = str(p)
        infos[s.key] = probe(p)
        assert infos[s.key].frames == s.frames, (s.key, infos[s.key].frames)
    return paths, infos


def case_record(case: Case, infos: dict[str, SourceInfo], measured: list[dict]) -> dict:
    used = sorted({c.src for c in case.clips})
    return {
        "name": case.name,
        "canvas": list(case.canvas),
        "gain_only": case.gain_only,
        "edl": edl_json(build_edl(case)),
        "sources": {k: infos[k].to_json() for k in used},
        "frames": measured,
    }


def document(records: list[dict]) -> dict:
    return {
        "version": VERSION,
        "grey": GREY,
        "markers": {n: list(uv) for n, (uv, _c) in MARKERS.items()},
        "marker_size": {k: marker_size(s) for k, s in SOURCES.items()},
        "cases": records,
    }


def load() -> dict:
    return json.loads(GOLDEN.read_text())
