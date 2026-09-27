"""Golden tables for the program map (Wave D instant preview, spec §6 R4/§13).

Bar-coded sources are rendered through the REAL compositor and every output
frame is decoded back to ``(source id, source frame)``. The result is the
normative definition of frame selection: ``render/frame_map.py`` (Python) and
``frontend/src/lib/preview/timeline/`` (TypeScript) must both reproduce it
frame for frame.

The bar code: 16 vertical bands 20 px wide across a 320x180 picture, luma 235
for a 1 bit and 16 for a 0 bit, LSB on the left. Bits 0-11 are the frame
index, bits 12-15 the source id (1..15; 0 is black, i.e. a gap). The code is
read twice, in the top and the bottom half, because transition cases render
with a split-screen ``xfade`` expression (top = outgoing side A, bottom =
incoming side B) and a progress stripe in rows 0-15 (luma 16 + 219·P). The
frame SELECTION of xfade does not depend on the transition's look, so the
split-screen expression pins exactly what every transition type does.

Two renders per case: ``render_preview`` (the chunk cache, the segmented
long-clip path, the stream-copy assembly) and the export's single pass
(``_render(preview=False, chunked=False)``). They must agree with each other
and with the model.

Regenerate with ``.venv/bin/python tests/gen_frame_map_goldens.py``.
"""
from __future__ import annotations

import json
import random
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import Canvas, Clip, EDL, Transition, empty_edl
from video_ai_editor.render.frame_map import (  # noqa: F401 — SourceInfo re-exported
    KIND_BLEND, KIND_GAP, SourceInfo, build_program_map, frame_map_json,
)

REPO = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO / "tests" / "goldens" / "frame_map"

W, H = 320, 180
BANDS, BAND_W = 16, 20
FRAME_BITS = 12
#: Rows the codes and the progress stripe are read from.
TOP_ROWS = (24, 84)
BOT_ROWS = (100, 172)
STRIPE_ROWS = (2, 13)
STRIPE_H = 16

#: The eight standard project rates the milestone names, plus 48 as a source.
PROJECT_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
    Fraction(30), Fraction(50), Fraction(60000, 1001), Fraction(60),
)
SOURCE_RATES: tuple[Fraction, ...] = PROJECT_RATES + (Fraction(48),)

#: Split-screen xfade: top half = A, bottom = B, rows < 16 = 16 + 219·P.
#: The source a tail-extending music bed reads (any bar source has sound).
MUSIC_SRC = "bar30"

SPLIT_EXPR = "if(lt(Y,16),if(eq(PLANE,0),16+219*P,128),if(lt(Y,H/2),A,B))"


def rate_label(r: Fraction) -> str:
    return str(r.numerator) if r.denominator == 1 else f"{r.numerator}/{r.denominator}"


def rate_name(r: Fraction) -> str:
    return {Fraction(24000, 1001): "23.976", Fraction(30000, 1001): "29.97",
            Fraction(60000, 1001): "59.94"}.get(r, str(r.numerator))


# ---------------------------------------------------------------- sources

@dataclass(frozen=True)
class SourceSpec:
    key: str                 # the EDL `src` used in goldens
    sid: int                 # bar-code source id 1..15
    rate: Fraction
    seconds: float
    timescale: int | None = None   # force an mp4 video timescale (camera files: 90000)

    @property
    def frames(self) -> int:
        return int(round(self.seconds * self.rate))


def make_bar_source(path: Path, spec: SourceSpec) -> Path:
    """A ``spec.frames``-frame bar-coded source with an AAC tone, encoded the
    way an import is (libx264 with B-frames, so the mp4 carries an edit list)."""
    n = spec.frames
    dur = Fraction(n) / spec.rate
    code = f"(N+{spec.sid * (1 << FRAME_BITS)})"
    lum = f"if(mod(floor({code}/pow(2\\,floor(X/{BAND_W})))\\,2)\\,235\\,16)"
    args = ["ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={rate_label(spec.rate)}",
            "-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:duration={float(dur) + 0.1:.6f}",
            "-vf", f"format=gray,geq=lum='{lum}',format=yuv420p",
            "-frames:v", str(n),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "8", "-g", "48",
            "-c:a", "aac", "-b:a", "96k", "-shortest"]
    if spec.timescale:
        args += ["-video_track_timescale", str(spec.timescale)]
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True)
    return path


def probe_source(path: Path) -> SourceInfo:
    """SourceInfo measured with ffprobe: rate, stream time base, decoded frame
    count, first pts relative to the file start, size."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,time_base,start_pts,nb_read_frames,width,height"
         ":format=start_time", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    s = d["streams"][0]
    tbase = Fraction(s["time_base"])
    file_start = Fraction(d["format"].get("start_time") or "0")
    start_ticks = int(s.get("start_pts") or 0) - int(file_start / tbase)
    return SourceInfo(rate=tb.rate_of(Fraction(s["r_frame_rate"])), time_base=tbase,
                      frames=int(s["nb_read_frames"]), start_ticks=start_ticks,
                      width=int(s["width"]), height=int(s["height"]))


# ---------------------------------------------------------------- decoding

def decode_gray(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-vf", f"scale={W}:{H}:flags=neighbor,format=gray",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
        check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W)


def _read_code(frames: np.ndarray, rows: tuple[int, int]) -> np.ndarray:
    region = frames[:, rows[0]:rows[1], :].astype(np.float32)
    code = np.zeros(frames.shape[0], dtype=np.int64)
    for b in range(BANDS):
        x0, x1 = b * BAND_W + 5, b * BAND_W + BAND_W - 5
        bit = region[:, :, x0:x1].mean(axis=(1, 2)) > 125.5
        code |= bit.astype(np.int64) << b
    return code


def code_of(sid: int, frame: int) -> int:
    """The bar code a frame of source ``sid`` carries (0 = black)."""
    return (sid << FRAME_BITS) | frame


def measure(path: Path) -> dict[str, list]:
    """Per output frame: the top-half and bottom-half codes and the stripe
    progress (luma-derived; meaningful on blend frames only)."""
    fr = decode_gray(path)
    top, bot = _read_code(fr, TOP_ROWS), _read_code(fr, BOT_ROWS)
    stripe = fr[:, STRIPE_ROWS[0]:STRIPE_ROWS[1], 20:300].astype(np.float32).mean(axis=(1, 2))
    return {
        "top": [int(c) for c in top],
        "bot": [int(c) for c in bot],
        # `format=gray` expands limited range: the stripe's 16 + 219·P reads
        # back as 255·P.
        "p": [round(float(s / 255.0), 3) for s in stripe],
    }


# ---------------------------------------------------------------- cases

@dataclass
class ClipSpec:
    src: str
    in_: float
    out: float
    start: float
    speed: Any = None
    reverse: bool = False
    # Varispeed, not atempo: sound does not change frame selection, and
    # varispeed is the sample-exact mode. (With ffmpeg 8.1.1 `adelay,atempo`
    # used to emit NOPTS-based pts that failed the export's concat at 29.97;
    # fixed by audio_mix.ATEMPO_RESTAMP, pinned by test_keep_pitch_export.py.)
    keep_pitch: bool = False
    id: str = ""
    freeze: float | None = None     # a freeze frame (Clip.freeze): hold seconds


@dataclass
class CaseSpec:
    name: str
    group: str
    fps: Fraction
    clips: list[ClipSpec]
    transitions: list[tuple[float, float]] = field(default_factory=list)  # (at, duration)
    tail_to: float | None = None       # a music clip that extends the timeline to here
    live: bool = False                 # re-rendered by the default pytest run


def fps_value(r: Fraction) -> float | int:
    return int(r) if r.denominator == 1 else tb.fps_float(r)


def build_edl(case: CaseSpec, src_path: dict[str, str] | None = None) -> EDL:
    """The case's EDL. ``src_path`` maps golden keys to files (render time);
    without it the golden keys themselves are the ``src``."""
    sp = src_path or {}
    e = empty_edl(Canvas(w=W, h=H, fps=fps_value(case.fps)))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    for i, c in enumerate(case.clips):
        clip = Clip(src=sp.get(c.src, c.src), start=c.start, speed=c.speed,
                    reverse=c.reverse, id=c.id or f"c{i:02d}")
        clip.in_ = c.in_
        clip.out = c.out
        clip.audio.keep_pitch = c.keep_pitch
        if c.freeze is not None:
            clip.freeze = c.freeze
        v1.clips.append(clip)
    for at, d in case.transitions:
        v1.transitions.append(Transition(at=at, type="fade", duration=d))
    if case.tail_to is not None:
        e.get_track("music").clips.append(Clip(
            src=sp.get(MUSIC_SRC, MUSIC_SRC), start=0.0, id="music0",
            **{"in": 0.0, "out": case.tail_to}))
    e.recompute_duration()
    return e


def expected_frames(edl: EDL, sources: dict[str, SourceInfo], sid_of: dict[str, int]) -> dict:
    """What the model says each output frame shows, in the measured shape."""
    pm = build_program_map(edl, sources)
    top, bot, p, kind = [], [], [], []
    for k in range(pm.total):
        kd = pm.kind[k]
        kind.append(kd)
        if kd == KIND_GAP:
            top.append(0); bot.append(0); p.append(None)
            continue
        a = code_of(sid_of[pm.clips[pm.clip[k]].src], pm.frame[k])
        top.append(a)
        if kd == KIND_BLEND:
            bot.append(code_of(sid_of[pm.clips[pm.b_clip[k]].src], pm.b_frame[k]))
            p.append((pm.p_num[k], pm.p_den[k]))
        else:
            bot.append(a); p.append(None)
    return {"kind": kind, "top": top, "bot": bot, "p": p, "pm": pm}


P_TOL = 0.02


def _fmt(code: int) -> str:
    return f"{code >> FRAME_BITS}:{code & ((1 << FRAME_BITS) - 1)}"


def compare(measured: dict, expected: dict, *, check_p: bool = True) -> list[str]:
    """Human-readable mismatches between a decoded render and the model (or
    another render). Codes must be equal; blend progress within ``P_TOL``."""
    errs: list[str] = []
    mt, et = measured["top"], expected["top"]
    mb, eb = measured.get("bot") or mt, expected.get("bot") or et
    if len(mt) != len(et):
        errs.append(f"frame count: rendered {len(mt)}, expected {len(et)}")
    for k in range(min(len(mt), len(et))):
        if mt[k] != et[k] or mb[k] != eb[k]:
            errs.append(f"k={k}: rendered {_fmt(mt[k])}/{_fmt(mb[k])}, "
                        f"expected {_fmt(et[k])}/{_fmt(eb[k])}")
        pe = expected["p"][k] if check_p and expected.get("p") else None
        # A blend whose two sides show the SAME frame (a clip fading into a
        # freeze of its own last frame) is pixel-identical at every progress:
        # the golden records no progress there (-1), and there is none to see.
        if pe is not None and measured["p"][k] < 0 and mt[k] == mb[k]:
            pe = None
        if pe is not None and abs(measured["p"][k] - pe[0] / pe[1]) > P_TOL:
            errs.append(f"k={k}: progress rendered {measured['p'][k]}, model {pe[0]}/{pe[1]}")
    return errs


def compact_measured(m: dict) -> dict:
    """The golden form of a measurement: top codes; bottom codes and stripe
    progress (per mille, -1 where the halves agree) only when some frame's
    halves differ (a blend)."""
    out = {"top": m["top"]}
    if m["bot"] != m["top"]:
        out["bot"] = m["bot"]
        out["p"] = [int(round(p * 1000)) if b != t else -1
                    for t, b, p in zip(m["top"], m["bot"], m["p"])]
    return out


def expand_measured(g: dict) -> dict:
    top = g["top"]
    bot = g.get("bot") or top
    p = [(-1 if x < 0 else x / 1000) for x in g["p"]] if "p" in g else [-1] * len(top)
    return {"top": top, "bot": bot, "p": p}


# ---------------------------------------------------------------- matrix

def _t(frames: Fraction | int | float, r: Fraction) -> float:
    """Seconds for a (possibly fractional) frame count on grid ``r``."""
    return float(Fraction(frames) / r)


def source_specs() -> list[SourceSpec]:
    specs = [SourceSpec(key=f"bar{rate_name(r)}", sid=i + 1, rate=r, seconds=16.0)
             for i, r in enumerate(SOURCE_RATES)]
    specs += [
        # Camera-style time bases (90 kHz) — they move exact rounding ties.
        SourceSpec(key="bar29.97_90k", sid=10, rate=Fraction(30000, 1001), seconds=8.0,
                   timescale=90000),
        SourceSpec(key="bar25_90k", sid=11, rate=Fraction(25), seconds=8.0, timescale=90000),
        # Short sources: a clip whose `out` runs past them freezes (tpad clone).
        SourceSpec(key="short30", sid=12, rate=Fraction(30), seconds=0.5),
        SourceSpec(key="short25", sid=13, rate=Fraction(25), seconds=0.52),
        SourceSpec(key="short59.94", sid=14, rate=Fraction(60000, 1001), seconds=0.4),
    ]
    return specs


def _src_for(rate: Fraction) -> str:
    return f"bar{rate_name(rate)}"


def rate_matrix_case(R: Fraction, S: Fraction) -> CaseSpec:
    """Every per-clip selection rule, back to back, for one (project, source)
    rate pair: in-points on the grid and ±0.3/±0.5 frame off it, in=0 and
    in < half a frame (no ``-ss``), off-grid outs, speeds 0.25-4, a speed
    curve, reverse at 1x and 2x. (Starts are laid out as if every clip ran
    at its scalar speed — the curve row, which rendered at 1x before speed
    curves existed, now fills its integral and leaves a gap after it; every
    other row is where it always was.)"""
    src = _src_for(S)
    rows: list[tuple[float, Fraction, float, Any, bool]] = [
        # (in near this many seconds, + this many project frames, source
        #  seconds consumed, speed, reverse)
        (0.0, Fraction(0), 0.40, None, False),
        (0.0, Fraction(2, 10), 0.37, None, False),
        (1.3, Fraction(0), 0.50, None, False),
        (2.1, Fraction(3, 10), 0.43, None, False),
        (2.9, -Fraction(3, 10), 0.43, None, False),
        (3.7, Fraction(1, 2), 0.46, None, False),
        (4.5, -Fraction(1, 2), 0.46, None, False),
        (5.3, Fraction(0), 0.26, 0.5, False),
        (6.1, Fraction(1, 4), 0.75, 1.5, False),
        (7.0, Fraction(0), 1.00, 2.0, False),
        (8.2, Fraction(0), 1.90, 4.0, False),
        (10.3, Fraction(0), 0.12, 0.25, False),
        (10.8, Fraction(0), 0.45, {"curve": [[0, 1.0], [1, 2.0]]}, False),
        (11.6, Fraction(3, 10), 0.41, None, True),
        (12.4, Fraction(0), 0.82, 2.0, True),
        (13.6, Fraction(0), 0.30, 0.3, False),
        (14.4, -Fraction(7, 10), 0.33, 0.8, False),
    ]
    clips: list[ClipSpec] = []
    cursor = 0
    for i, (sec, off, src_s, speed, rev) in enumerate(rows):
        in_ = float((Fraction(round(sec * R)) + off) / R)
        out = in_ + src_s
        clips.append(ClipSpec(src=src, in_=in_, out=out, start=tb.time_of(cursor, R),
                              speed=speed, reverse=rev, id=f"m{i:02d}"))
        sf = speed if isinstance(speed, (int, float)) and speed else 1.0
        cursor += max(1, tb.frame_of(src_s / sf, R))
    return CaseSpec(name=f"rates_p{rate_name(R)}_s{rate_name(S)}", group="rates",
                    fps=R, clips=clips,
                    live=(R, S) in {(Fraction(30000, 1001), Fraction(25)),
                                    (Fraction(25), Fraction(60000, 1001))})


def structure_cases() -> list[CaseSpec]:
    out: list[CaseSpec] = []
    for R in (Fraction(30), Fraction(30000, 1001), Fraction(25), Fraction(60000, 1001)):
        s = _src_for(R)
        f = lambda n: _t(n, R)  # noqa: E731
        rn = rate_name(R)
        # Gaps: leading, 1 frame, sub-frame (0.4 — no gap on the frame grid),
        # 2.6 frames, sub-_GAP_EPS; a trailing gap from a music bed.
        out.append(CaseSpec(
            name=f"gaps_p{rn}", group="structure", fps=R, tail_to=f(95) + 0.2,
            clips=[
                ClipSpec(s, f(10), f(22), f(5)),
                ClipSpec(s, f(40), f(52), f(18)),            # 1-frame gap
                ClipSpec(s, f(70), f(80), f(30) + f(0.4)),    # 0.4-frame gap
                ClipSpec(s, f(90), f(99), f(40) + f(2.6)),    # 2.6-frame gap
                ClipSpec(s, f(120), f(131), f(52) + 0.0004),  # < _GAP_EPS
                ClipSpec(s, f(150), f(160.5), f(64)),
            ],
            live=R == Fraction(30000, 1001)))
        # Legacy overlaps: the renderer packs them with max(cursor, start).
        out.append(CaseSpec(
            name=f"overlap_p{rn}", group="structure", fps=R,
            clips=[
                ClipSpec(s, f(10), f(30), 0.0),
                ClipSpec(s, f(60), f(75), f(12)),
                ClipSpec(s, f(100), f(110), f(25) + f(0.3)),
            ]))
        # Freeze: `out` past the end of a short source (tpad clones the last
        # frame), at 1x, 0.5x, 2x and reversed; a clip shorter than a frame.
        short = {Fraction(30): "short30", Fraction(25): "short25",
                 Fraction(60000, 1001): "short59.94"}.get(R, "short30")
        out.append(CaseSpec(
            name=f"freeze_p{rn}", group="structure", fps=R,
            clips=[
                ClipSpec(short, 0.0, 0.9, 0.0),
                ClipSpec(short, 0.2, 0.8, f(27), speed=0.5),
                ClipSpec(short, 0.1, 1.3, f(27 + 36), speed=2.0),
                ClipSpec(short, 0.25, 0.75, f(27 + 36 + 18), reverse=True),
                ClipSpec(s, f(33), f(33.3), f(27 + 36 + 18 + 15)),
                ClipSpec(s, f(40), f(40) + 0.0001, f(27 + 36 + 18 + 16)),
            ],
            live=R == Fraction(25)))
    out.append(CaseSpec(name="empty_v1_p30", group="structure", fps=Fraction(30),
                        clips=[], tail_to=1.5))
    # Camera-style 90 kHz time bases at speeds that produce exact ties.
    for R, key in ((Fraction(30000, 1001), "bar29.97_90k"), (Fraction(30), "bar29.97_90k"),
                   (Fraction(25), "bar25_90k"), (Fraction(50), "bar25_90k")):
        f = lambda n: _t(n, R)  # noqa: E731
        out.append(CaseSpec(
            name=f"tb90k_p{rate_name(R)}_{key}", group="structure", fps=R,
            clips=[ClipSpec(key, f(3) + f(0.5), f(40), 0.0),
                   ClipSpec(key, f(50), f(80), f(40), speed=1.5),
                   ClipSpec(key, f(90), f(130), f(60), speed=2.0),
                   ClipSpec(key, f(140), f(170), f(80), speed=0.5, reverse=True)]))
    return out


def transition_cases() -> list[CaseSpec]:
    out: list[CaseSpec] = []
    for R in PROJECT_RATES:
        s = _src_for(R)
        other = _src_for(Fraction(25) if R != Fraction(25) else Fraction(30))
        f = lambda n: _t(n, R)  # noqa: E731
        rn = rate_name(R)
        a_len, b_len, c_len = 40, 30, 36
        # Two plain seams (the second off-grid: 0.37 s), then a gap (cut).
        out.append(CaseSpec(
            name=f"xfade_p{rn}", group="transitions", fps=R,
            clips=[ClipSpec(s, f(10), f(10 + a_len), 0.0),
                   ClipSpec(other, 1.0, 1.0 + f(b_len), f(a_len)),
                   ClipSpec(s, f(200), f(200 + c_len), f(a_len + b_len)),
                   ClipSpec(s, f(300), f(320), f(a_len + b_len + c_len + 4))],
            transitions=[(f(a_len), 0.5), (f(a_len + b_len), 0.37),
                         (f(a_len + b_len + c_len), 0.3)],
            # 30 fps: the seams that started a frame late on the old AVTB clock.
            live=R in (Fraction(30), Fraction(25))))
        # Transition longer than the shorter side (clamped), a nested seam over
        # a short middle clip, speed and reverse clips under a seam.
        out.append(CaseSpec(
            name=f"xfade_edge_p{rn}", group="transitions", fps=R,
            clips=[ClipSpec(s, f(20), f(50), 0.0),
                   ClipSpec(other, 2.0, 2.0 + f(12), f(30)),
                   ClipSpec(s, f(100), f(160), f(42), speed=2.0),
                   ClipSpec(other, 4.0, 4.0 + f(40), f(72), reverse=True),
                   ClipSpec(s, f(250), f(280), f(112))],
            transitions=[(f(30), 0.4), (f(42), 0.4), (f(72), 0.25), (f(112), 2.0)],
            live=R == Fraction(24000, 1001)))
    return out + _retimed_seam_cases()


def _retimed_seam_cases() -> list[CaseSpec]:
    """A transition after a RETIMED clip whose exact end is off the grid
    (review RD3): set_speed ripples the next clip to the frame the footprint
    rounds to, so a clip that rounds UP leaves a sub-frame gap before its
    neighbour — a seam on the frame grid, which the 1 ms rule called a gap
    and never cross-faded (1.5x on 91 frames: +11.1 ms; Hero: +4.2 ms)."""
    from video_ai_editor.edl.speed_curve import CURVE_PRESETS as P
    out: list[CaseSpec] = []
    for R in (Fraction(30), Fraction(25)):
        s = _src_for(R)
        other = _src_for(Fraction(25) if R != Fraction(25) else Fraction(30))
        f = lambda n: _t(n, R)  # noqa: E731
        a = ClipSpec(s, f(10), f(50), 0.0, id="a")
        b = ClipSpec(s, f(100), f(191), 0.0, speed=1.5, id="b")        # 60.67 frames
        c = ClipSpec(other, 1.0, 1.0 + f(30), 0.0, id="c")
        n = 60
        while True:   # a Hero clip whose footprint rounds UP
            d = ClipSpec(s, f(300), f(300 + n), 0.0, speed={"curve": P["hero"], "name": "hero"}, id="d")
            eff = Fraction(_planned_clip(d).effective_duration) * R
            if Fraction(1, 20) < tb.frame_of(float(eff / R), R) - eff < Fraction(1, 2):
                break
            n += 1
        e = ClipSpec(s, f(380), f(410), 0.0, id="e")
        clips = _lay([a, b, c, d, e], R)
        for x, y in ((b, c), (d, e)):
            end = x.start + _planned_clip(x).effective_duration
            assert 0.001 < y.start - end < float(1 / R), (x.id, y.start - end)
        out.append(CaseSpec(name=f"xfade_retimed_p{rate_name(R)}", group="transitions", fps=R,
                            clips=clips, transitions=[(c.start, 0.5), (e.start, 0.4)],
                            live=R == Fraction(30)))
    return out


def segment_cases() -> list[CaseSpec]:
    """Long 1x on-grid clips: ``render_preview`` builds them from cached
    picture SEGMENTS (render/segments.py), each with its own seek — and the
    preview must still show the frames the export shows."""
    out = []
    for R, S in ((Fraction(30000, 1001), Fraction(25)), (Fraction(30), Fraction(24000, 1001)),
                 (Fraction(25), Fraction(60000, 1001)), (Fraction(24), Fraction(30)),
                 (Fraction(30), Fraction(30))):
        f = lambda n: _t(n, R)  # noqa: E731
        n = int(13 * R)
        out.append(CaseSpec(
            name=f"segments_p{rate_name(R)}_s{rate_name(S)}", group="segments", fps=R,
            clips=[ClipSpec(_src_for(S), f(7), f(7 + n), 0.0),
                   ClipSpec(_src_for(S), f(3), f(40), f(n))],
            live=(R, S) == (Fraction(30000, 1001), Fraction(25))))
    return out


def fuzz_cases(count: int = 24, seed: int = 20260926) -> list[CaseSpec]:
    """Random timelines in the spirit of test_b8_fuzz_sweep: mixed source
    rates, speeds, off-grid in/out/start, gaps, overlaps, reverse, seams."""
    rng = random.Random(seed)
    out = []
    speeds = [None, None, None, 0.5, 1.5, 2.0, 0.75, 3.0, 0.25, 1.25]
    for i in range(count):
        R = rng.choice(PROJECT_RATES)
        n_clips = rng.randint(2, 6)
        clips: list[ClipSpec] = []
        trans: list[tuple[float, float]] = []
        t = rng.choice([0.0, 0.0, rng.uniform(0, 0.4)])
        for j in range(n_clips):
            S = rng.choice(SOURCE_RATES)
            in_ = rng.uniform(0, 10)
            if rng.random() < 0.5:
                in_ = tb.quantize(in_, R)
            span = rng.uniform(0.15, 1.6)
            speed = rng.choice(speeds)
            rev = rng.random() < 0.15
            clips.append(ClipSpec(_src_for(S), in_, in_ + span, t, speed=speed,
                                  reverse=rev, id=f"z{j}"))
            eff = span / (speed or 1.0)
            end = t + eff
            roll = rng.random()
            if roll < 0.2:
                t = end + rng.uniform(0.001, 0.3)          # gap
            elif roll < 0.3:
                t = max(0.0, end - rng.uniform(0.0, 0.1))  # legacy overlap
            else:
                t = end
                if rng.random() < 0.45 and j < n_clips - 1:
                    trans.append((end, rng.choice([0.2, 0.33, 0.5, 0.8])))
            if rng.random() < 0.5:
                t = tb.quantize(t, R)
        out.append(CaseSpec(name=f"fuzz_{i:02d}_p{rate_name(R)}", group="fuzz", fps=R,
                            clips=clips, transitions=trans, live=i in (3, 11)))
    return out


#: The rates the speed-curve / freeze group covers (the task's five).
SPEED_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001), Fraction(25), Fraction(30000, 1001), Fraction(30), Fraction(60000, 1001),
)


def _spec_info(key: str) -> SourceInfo:
    """The SourceInfo a generated bar source probes as (CFR, the muxer's
    default time base; `gen_frame_map_goldens.ensure_sources` asserts it)."""
    spec = next(s for s in source_specs() if s.key == key)
    return SourceInfo.cfr(spec.rate, spec.frames, width=W, height=H)


def _planned_clip(c: ClipSpec) -> Clip:
    clip = Clip(src=c.src, start=c.start, speed=c.speed, reverse=c.reverse, id=c.id or "x")
    clip.in_, clip.out = c.in_, c.out
    if c.freeze is not None:
        clip.freeze = c.freeze
    return clip


def _frames_of(c: ClipSpec, R: Fraction) -> list[int]:
    from video_ai_editor.render.frame_map import clip_frame_list
    return clip_frame_list(_planned_clip(c), _spec_info(c.src), R)


def _freeze_holding(src: str, frame: int, R: Fraction, hold: float, start: float, id_: str) -> ClipSpec:
    """A freeze clip that holds ``frame`` of ``src`` (what lane S2's
    freeze-frame op writes: ``freeze_in_for``), ``in`` + one frame as out."""
    from video_ai_editor.render.frame_map import freeze_in_for
    in_ = freeze_in_for(_spec_info(src), frame, R)
    return ClipSpec(src, in_, in_ + tb.frame_duration(R), start, freeze=hold, id=id_)


def _fit(c: ClipSpec, R: Fraction) -> ClipSpec:
    """Trim ``c``'s ``out`` (a freeze: its hold) so its footprint is a WHOLE
    number of frames — for a curve, ``S = n/R · mean``. ``seam_table_for``
    charges a seam only where the next clip starts within 1 ms of the
    footprint's end, so a fitted clip's end is a seam a transition can sit on."""
    pc = _planned_clip(c)
    n = max(1, tb.frame_of(pc.effective_duration, R))
    if c.freeze is not None:
        c.freeze = tb.time_of(n, R)
    else:
        c.out = c.in_ + tb.time_of(n, R) * pc.speed_factor
    return c


def _lay(clips: list[ClipSpec], R: Fraction) -> list[ClipSpec]:
    """Place ``clips`` back to back on the frame grid (each at the frame the
    previous one's footprint ends on)."""
    from video_ai_editor.render.compositor import clip_frames
    cursor = 0
    for c in clips:
        c.start = tb.time_of(cursor, R)
        cursor += clip_frames(_planned_clip(c), R)
    return clips


def speed_cases() -> list[CaseSpec]:
    """Speed CURVES and FREEZE frames (Wave D lane S1) at 23.976, 25,
    29.97, 30 and 59.94: ramp up/down, bullet, hero, montage, flash and jump
    curves on on-grid and off-grid in-points, a mismatched source rate, a
    reversed curve clip and curves under seams; freezes at the start, middle
    and end of a clip, between two seams and on a reversed clip."""
    from video_ai_editor.edl.speed_curve import CURVE_PRESETS as P
    out: list[CaseSpec] = []
    for R in SPEED_RATES:
        s = _src_for(R)
        other = _src_for(Fraction(25) if R != Fraction(25) else Fraction(30))
        f = lambda n: _t(n, R)  # noqa: E731
        rn = rate_name(R)
        cv = lambda name: {"curve": P[name], "name": name}  # noqa: E731
        clips = [
            ClipSpec(s, f(12), f(12) + 1.6, 0, speed=cv("ramp_up"), id="ru"),
            ClipSpec(s, f(80) + f(Fraction(3, 10)), f(80) + 1.5, 0, speed=cv("ramp_down"), id="rd"),
            ClipSpec(other, 3.0, 5.2, 0, speed=cv("bullet"), keep_pitch=True, id="bu"),
            ClipSpec(s, f(200) - f(Fraction(1, 2)), f(200) + 1.2, 0, speed=cv("hero"), id="he"),
            ClipSpec(other, 7.1, 9.0, 0, speed=cv("montage"), id="mo"),
            ClipSpec(s, f(320), f(320) + 1.1, 0, speed=cv("flash_in"), reverse=True, id="fi"),
            ClipSpec(s, 12.0, 13.4, 0, speed=cv("jump_cut"), keep_pitch=True, id="jc"),
            ClipSpec(other, 13.5, 14.3, 0,
                     speed={"curve": [[0, 0.1], [0.5, 10.0], [1, 0.1]]}, id="ex"),
        ]
        # Whole-frame footprints (seams) for all but the last, whose curve
        # footprint stays off the grid.
        clips = _lay([_fit(c, R) for c in clips[:-1]] + clips[-1:], R)
        # Seams: a fade into the bullet clip and one out of the montage.
        bu, mo_end = clips[2], clips[5].start
        out.append(CaseSpec(name=f"speed_curves_p{rn}", group="speed", fps=R, clips=clips,
                            transitions=[(bu.start, 0.3), (mo_end, 0.25)],
                            live=R in (Fraction(30000, 1001), Fraction(25))))

        # Freezes. A: split at its 15th frame with a 0.5 s freeze of the
        # frame there (CapCut's freeze at the playhead); B with its first
        # frame frozen before it and its last after it; C faded into a freeze
        # of its last frame that fades into D; E reversed with its 10th output
        # frame frozen after it.
        a1 = _fit(ClipSpec(s, f(10), f(25), 0, id="a1"), R)
        a2 = _fit(ClipSpec(s, f(25), f(50), 0, id="a2"), R)
        fa = _fit(_freeze_holding(s, _frames_of(a2, R)[0], R, 0.5, 0, "fz_mid"), R)
        b = _fit(ClipSpec(other, 2.0 + f(Fraction(3, 10)), 3.2, 0, id="b"), R)
        bf = _frames_of(b, R)
        fb0 = _fit(_freeze_holding(other, bf[0], R, 0.4, 0, "fz_start"), R)
        fb1 = _fit(_freeze_holding(other, bf[-1], R, 0.6, 0, "fz_end"), R)
        c = _fit(ClipSpec(s, f(150), f(150) + 1.0, 0, id="c"), R)
        fc = _fit(_freeze_holding(s, _frames_of(c, R)[-1], R, 1.0, 0, "fz_seam"), R)
        d = _fit(ClipSpec(other, 6.0, 7.0, 0, id="d"), R)
        e = _fit(ClipSpec(s, f(260), f(260) + 1.2, 0, reverse=True, id="e"), R)
        # The last freeze keeps an off-grid hold.
        fe = _freeze_holding(s, _frames_of(e, R)[10], R, 0.7, 0, "fz_rev")
        clips = _lay([a1, fa, a2, fb0, b, fb1, c, fc, d, e, fe], R)
        end_c = clips[7].start
        end_fc = clips[8].start
        out.append(CaseSpec(name=f"speed_freeze_p{rn}", group="speed", fps=R, clips=clips,
                            transitions=[(end_c, 0.3), (end_fc, 0.4)],
                            live=R in (Fraction(30), Fraction(60000, 1001))))
    return out


def all_cases() -> list[CaseSpec]:
    cases = [rate_matrix_case(R, S) for R in PROJECT_RATES for S in PROJECT_RATES]
    cases = (cases + structure_cases() + transition_cases() + segment_cases() + fuzz_cases()
             + speed_cases())
    # The generator keys renders by name across groups: a clash would write
    # one group's render into the other's golden.
    names = [c.name for c in cases]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})
    return cases


# ---------------------------------------------------------------- golden I/O

def golden_path(case: CaseSpec) -> Path:
    return GOLDEN_DIR / f"{case.group}.json"


def golden_edl_json(edl: EDL) -> dict:
    """The EDL as the client receives it, minus the empty lanes the program
    map never reads (v1 and the music bed are kept)."""
    d = edl.model_dump(by_alias=True, mode="json")
    d["tracks"] = [t for t in d["tracks"] if t["id"] in ("v1", "music")]
    return d


def case_record(case: CaseSpec, edl: EDL, sources: dict[str, SourceInfo],
                sid_of: dict[str, int], measured: dict) -> dict:
    exp = expected_frames(edl, sources, sid_of)
    used = sorted({c.src for t in edl.tracks for c in t.clips if isinstance(c, Clip)})
    return {
        "name": case.name,
        "live": case.live,
        "edl": golden_edl_json(edl),
        "sources": {k: {**sources[k].to_json(), "sid": sid_of[k]} for k in used},
        "total": len(measured["top"]),
        "measured": compact_measured(measured),
        "model": model_json(edl, sources),
    }


def model_json(edl: EDL, sources: dict[str, SourceInfo]) -> dict:
    """``frame_map_json`` minus the render hash (a salt, not frame logic):
    what the TypeScript port must reproduce byte for byte."""
    d = frame_map_json(edl, sources)
    d.pop("render_hash", None)
    return d


def load_goldens() -> list[dict]:
    """Every golden case, with ``group`` set."""
    out = []
    for p in sorted(GOLDEN_DIR.glob("*.json")):
        doc = json.loads(p.read_text())
        for c in doc["cases"]:
            out.append({**c, "group": p.stem})
    return out
