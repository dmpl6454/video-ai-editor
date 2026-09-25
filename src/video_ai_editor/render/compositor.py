"""Renderer — video tracks composited through ffmpeg filter_complex.

M1 capability:  cut/trim/concat/reorder on V1 + A1, scaled to canvas, hash-keyed
                preview cache, preview-vs-export quality split.
M2 addition:    text + captions tracks composited as PNG overlays.

GPU encoding: when VideoToolbox is available (Apple Silicon), preview + export
encode via h264_videotoolbox for ~5–10× the throughput of libx264.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from .. import platformutil as _pu
from ..config import FONTS_DIR
from ..edl import EDL
from ..edl.schema import Clip, Track
from .text_overlay import build_overlay_chain
from .audio_mix import build_audio_mix
from .effects import effect_chain, render_mask_png, build_chromakey_filter, mask_png_is_valid
from .pip import (build_pip_overlay_chain, collect_pip_clips, pip_audio_input_index,
                  pip_audio_chain, pip_frames, pip_input_args)
from . import clock
from . import cancel as _cancel
from . import cache_budget as _cache_budget
from ..edl.keyframes import is_keyframed, to_ffmpeg_expr
from ..edl import timebase as _tb


def _part_path(dst: Path) -> Path:
    """A unique sibling temp path for an in-progress render of `dst`.

    ffmpeg's `-y` truncates its output to 0 bytes and writes progressively, so
    pointing it straight at the served path means a concurrent fetch (the
    <video>/FrameScrubber polling preview.mp4) — or a render killed mid-write —
    sees a 0-byte or torn file, which mp4box reports as "invalid box". Render to
    this temp path, then swap it into place via platformutil.replace_with_retry
    (atomic on one filesystem; retries on Windows if a reader still holds the
    destination open), so readers only ever see a complete file or none at all.
    PID+thread id keep concurrent renders of the same hash from clobbering
    each other's temp file.

    The temp file's extension MUST match `dst`'s (not a hardcoded `.mp4`) —
    ffmpeg infers its output muxer from the argv output path's extension, and
    this temp path is that literal argv output path (see `_render`). A MOV
    export writing to a `.part.mp4` temp name would get muxed as MP4 and then
    simply renamed to `.mov`, producing a file with a `.mov` extension but
    MP4-brand internals.
    """
    return dst.with_name(f".{dst.stem}.{os.getpid()}.{threading.get_ident()}.part{dst.suffix}")


def _run_ffmpeg_progress(args: list[str], total_s: float,
                         on_progress, cancel_event) -> tuple[int, str]:
    """Run ffmpeg streaming `-progress`, reporting 0..1 against `total_s` and
    honouring `cancel_event`. Returns (returncode, stderr). Used by the export
    path; preview keeps the plain blocking `subprocess.run`.

    ffmpeg emits `out_time_us=<microseconds>` lines on the progress pipe; we
    divide by the known timeline duration for a real percentage. stderr is
    drained on a thread so a full pipe can't deadlock the progress reader.
    """
    out = args[-1]
    full = [*args[:-1], "-progress", "pipe:1", "-nostats", out]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", **_pu.SUBPROCESS_FLAGS)
    err_chunks: list[str] = []

    def _drain_err() -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                err_chunks.append(line)
        except Exception:
            pass

    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                from ..api.jobs import JobCancelled
                raise JobCancelled()
            line = raw.strip()
            if on_progress and total_s and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    on_progress(us / 1_000_000 / total_s)
                except (ValueError, ZeroDivisionError):
                    pass
    finally:
        rc = proc.wait()
        t.join(timeout=1)
    return rc, "".join(err_chunks)


@lru_cache(maxsize=None)
def _usable_encoder(name: str) -> bool:
    """True iff ffmpeg can actually ENCODE with `name` on this machine.

    'ffmpeg -encoders' lists h264_nvenc/qsv/amf even with no matching GPU, so a
    listing grep is not enough — we run a tiny null encode. VideoToolbox is the
    exception: it's Apple-only and cheap to trust from the listing, but the null
    encode works for it too, so we use one code path. Cached per process."""
    try:
        out = subprocess.run([_pu.FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, **_pu.SUBPROCESS_FLAGS)
        if f" {name} " not in out.stdout:
            return False
    except Exception:
        return False
    # Functional probe: a ~0.1s black-frame encode to null.
    try:
        r = subprocess.run(
            [_pu.FFMPEG, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
             "-c:v", name, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            **_pu.SUBPROCESS_FLAGS,
        )
        return r.returncode == 0
    except Exception:
        return False


# Probe order: Apple HW first (only lists on Mac), then the three Windows/Linux
# HW encoders, then guaranteed software fallback.
_HW_ENCODER_ORDER = ["h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"]


def _target_bitrate_args(name: str, kbps: int, *, peak_cap: bool = True) -> list[str]:
    """Average-bitrate rate control for a platform delivery target (QA-027).

    `canvas.bitrate_kbps` (written by `apply_export_preset`) used to have NO
    reader anywhere in render/: every export ran in quality mode, so a
    6000 kbps 'story' preset shipped ~8.5 Mbps and a 12000 kbps 'youtube_16x9'
    ~22.5 Mbps while the prompt verifier reported the target as met.

    `-b:v` is the target; the peak is capped at 1.5x over a 2x buffer, which is
    what the platforms' "recommended bitrate" means (an average, not a CBR
    ceiling). Measured on VideoToolbox, 15 s of 1080x1920 scene footage:
    `-b:v 8000k` alone → 8.00 Mbps; with `-maxrate 12000k -bufsize 16000k` →
    8.00 Mbps; with `-maxrate` pinned AT the target → 5.6 Mbps (VT treats a
    tight cap as a hard ceiling and undershoots), hence 1.5x rather than 1x.

    `peak_cap=False` (VideoToolbox only) drops the cap: VT treats ANY -maxrate
    as a data-rate limit and starves compressible footage — the 16:9 bench
    scene measured 7948 kb/s against 12000 capped (-34%) and 11998 uncapped,
    testsrc2 9123 vs ~12000. `render_export` encodes uncapped first and only
    re-encodes with the cap when the file overshoots (pure-noise content ran
    +58% uncapped, on target capped). The other encoders honour the cap as an
    average-with-peak and always take it.
    """
    b, peak, buf = f"{kbps}k", f"{int(kbps * 1.5)}k", f"{kbps * 2}k"
    rate = ["-b:v", b, "-maxrate", peak, "-bufsize", buf]
    if name == "h264_videotoolbox":
        vt_rate = rate if peak_cap else ["-b:v", b]
        return ["-c:v", name, *vt_rate, "-allow_sw", "1", "-realtime", "0",
                "-pix_fmt", "yuv420p"]
    if name == "h264_nvenc":
        return ["-c:v", name, "-preset", "p6", "-tune", "hq", "-rc", "vbr", *rate,
                "-pix_fmt", "yuv420p"]
    if name == "h264_qsv":
        return ["-c:v", name, "-preset", "slower", *rate, "-pix_fmt", "nv12"]
    if name == "h264_amf":
        return ["-c:v", name, "-quality", "quality", "-rc", "vbr_peak", *rate,
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", *rate, "-pix_fmt", "yuv420p"]


def _video_encoder_args(*, preview: bool, crf: int | None = None,
                        bitrate_kbps: int | None = None,
                        bitrate_peak_cap: bool = True) -> list[str]:
    """Pick the fastest usable H.264 encoder; fall back to libx264.

    `bitrate_kbps` (export only) switches the chosen encoder from quality mode
    to the platform's average-bitrate target — see `_target_bitrate_args`.
    Preview never takes it: previews are sized and tuned for scrubbing, not
    for delivery.

    `crf` is an optional caller-supplied override (e.g. from ExportRequest.crf)
    for the export Quality selector. It's threaded into whichever encoder is
    actually selected: libx264 takes it directly as `-crf`; HW encoders
    (nvenc/qsv/amf/videotoolbox) don't have a `-crf` knob, so `_hw_encoder_args`
    maps it onto each encoder's own quality knob (-q:v / -cq / -global_quality
    / -qp) — a best-effort approximation, not a calibrated 1:1 (see
    `_hw_encoder_args`'s docstring). Previously `crf` was silently dropped for
    HW encoders, making the Quality selector a no-op on e.g. Mac
    (VideoToolbox). `crf=None` (the default) preserves the exact prior
    hardcoded values for both branches.
    """
    target = int(bitrate_kbps) if bitrate_kbps and not preview else None
    for name in _HW_ENCODER_ORDER:
        if _usable_encoder(name):
            if target:
                return _target_bitrate_args(name, target, peak_cap=bitrate_peak_cap)
            # The GOP bound must apply to the HARDWARE encoders too, not just
            # the libx264 fallback below. It was only ever set on libx264, on
            # the assumption that "HW encoders already emit ~0.4-1s GOPs" —
            # measured false: h264_qsv produced a 60-frame (2s) GOP, so on
            # every machine WITH a GPU (i.e. most of them, and the fast path
            # this ladder exists to prefer) the mitigation never applied and
            # the scrubber paid a full ~30-frame decode per drag tick.
            return _hw_encoder_args(name, preview=preview, crf=crf) + (
                ["-g", str(_PREVIEW_GOP)] if preview else [])
    if target:
        return _target_bitrate_args("libx264", target)
    default_crf = 30 if preview else 20
    crf_val = crf if crf is not None else default_crf
    preset = "ultrafast" if preview else "medium"
    args = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf_val), "-pix_fmt", "yuv420p"]
    if preview:
        args += ["-g", str(_PREVIEW_GOP)]
    return args


def _crf_to_videotoolbox_qv(crf: int) -> int:
    """Map an x264-style crf (0-51, LOWER=better) onto VideoToolbox's -q:v
    scale (0-100, HIGHER=better) — the two scales run in opposite directions.

    Endpoints chosen so the app's crf presets land in a sensible range
    around the prior hardcoded default (48 export / 60 preview):
    crf=18 (High) -> 90, crf=23 (Medium) -> 78, crf=28 (Small/low quality)
    -> 65. Formula: q = 100 - (crf - 14) * 2.5, clamped to [0, 100].
    """
    q = 100 - (crf - 14) * 2.5
    return int(round(max(0.0, min(100.0, q))))


# Keyframe spacing for PREVIEW renders only, in frames. The frontend scrubber
# decodes from the nearest PRIOR keyframe on every paused playhead drag, so the
# average cost of a drag tick is GOP/2 frames of H.264 decode — the GOP is the
# single number that sets how smooth scrubbing feels.
#
# A/B on a 1080x1920 frame-numbered ramp, playhead dragged across 6s of timeline
# from an in-page rAF loop at 60Hz, counting the scrubber's own drawImage calls:
#   GOP 60 (what h264_qsv emitted with no -g) -> 45.4 fps, p90 gap 32ms
#   GOP 15 (this)                             -> 57.3 fps, p90 gap 23ms
# Measure this with an IN-PAGE driver and a drawImage counter, never with
# playwright's mouse.move() (capped ~12 events/sec by the CDP round-trip) or a
# per-rAF getImageData (~35ms/tick). Both of those measure the harness: the
# first reported 12.4 fps here, which was playwright's event rate, not the app.
# Export is deliberately untouched: long GOPs are the right size trade there,
# and nothing scrubs an exported file inside the app.
_PREVIEW_GOP = 15


def _hw_encoder_args(name: str, *, preview: bool, crf: int | None = None) -> list[str]:
    """Per-encoder quality-mode args. Values are tuned defaults, not mandates.

    `crf` (when given) overrides the default quality knob via a best-effort
    mapping onto each encoder's own scale — HW encoders don't take x264's
    `-crf` directly:
      - videotoolbox: `-q:v` (0-100, higher=better) via `_crf_to_videotoolbox_qv`.
      - nvenc: `-cq` (0-51, lower=better) — same direction/range as crf, used
        near-identically.
      - qsv: `-global_quality` (roughly 1-51, lower=better) — used near-identically.
      - amf: `-qp_i`/`-qp_p` (0-51, lower=better) — crf used directly for `-qp_i`,
        `-qp_p` offset by +2 to preserve the existing I/P quality gap.
    These are documented approximations, not a calibrated cross-encoder parity
    (out of scope per the plan) — the bar is "the knob visibly changes output."
    `crf=None` reproduces the exact prior hardcoded values.
    """
    if name == "h264_videotoolbox":
        q = str(_crf_to_videotoolbox_qv(crf)) if crf is not None else ("60" if preview else "48")
        return ["-c:v", "h264_videotoolbox", "-q:v", q, "-allow_sw", "1",
                "-realtime", "1" if preview else "0", "-pix_fmt", "yuv420p"]
    if name == "h264_nvenc":
        cq = str(crf) if crf is not None else ("33" if preview else "21")
        if preview:
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                    "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"]
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
                "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"]
    if name == "h264_qsv":
        gq = str(crf) if crf is not None else ("30" if preview else "22")
        if preview:
            return ["-c:v", "h264_qsv", "-global_quality", gq,
                    "-preset", "veryfast", "-pix_fmt", "nv12"]
        return ["-c:v", "h264_qsv", "-global_quality", gq,
                "-preset", "slower", "-pix_fmt", "nv12"]
    if name == "h264_amf":
        qp_i = crf if crf is not None else (30 if preview else 20)
        qp_p = qp_i + 2 if crf is not None else (32 if preview else 22)
        if preview:
            return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                    "-qp_i", str(qp_i), "-qp_p", str(qp_p), "-pix_fmt", "yuv420p"]
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(qp_i), "-qp_p", str(qp_p), "-pix_fmt", "yuv420p"]
    # Unreachable: only called for names in _HW_ENCODER_ORDER.
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


# In-flight render dedup: identical EDL hash → one ffmpeg job, shared result.
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()

# Ceiling on ffmpeg jobs running at once. _INFLIGHT only collapses renders of
# the SAME EDL hash; N different hashes (several sessions, or one user editing
# fast enough that each keystroke supersedes the last) still launched N ffmpeg
# processes, each of which saturates every core. Past a couple of concurrent
# renders total throughput does not improve — they just contend — while the
# machine becomes unresponsive, which is the reported VAI-11 symptom.
# Deliberately a plain semaphore, not a queue: waiters proceed in arrival order
# and nothing is dropped.
_RENDER_SLOTS = threading.BoundedSemaphore(
    max(1, int(os.environ.get("VAI_MAX_CONCURRENT_RENDERS", "2")))
)


#: Most v1 clips an EXPORT renders in a single ffmpeg pass (QA-097); above
#: this it builds per-clip chunks first to bound decoder memory. See the
#: chunk stage in `_render_locked`.
_EXPORT_SINGLE_PASS_MAX_CLIPS = max(
    0, int(os.environ.get("VAI_EXPORT_SINGLE_PASS_MAX_CLIPS", "48") or 48))
#: Share of an export's progress bar the chunk stage fills when it runs.
_CHUNK_PHASE_SHARE = 0.5


@dataclass
class RenderResult:
    path: Path
    cached: bool
    edl_hash: str


def _video_clips(edl: EDL) -> list[Clip]:
    """Return the V1 (main video) clips sorted by `start` time."""
    v1 = edl.get_track("v1")
    if not v1:
        return []
    clips: list[Clip] = [c for c in v1.clips if isinstance(c, Clip)]
    clips.sort(key=lambda c: c.start)
    return clips


# A gap shorter than this is a rounding artefact, not a deliberate hole.
_GAP_EPS = 0.001


def _v1_segments(clips: list[Clip], total_duration: float) -> list[tuple[str, object]]:
    """Ordered v1 timeline segments: ("clip", index) | ("gap", seconds).

    v1 used to be assembled as a bare `concat` of the clips, which packed them
    from t=0 and threw `clip.start` away — the timeline drew a gap, the render
    silently closed it, and the output was shorter than `edl.duration` (proven:
    an 84.27s timeline rendering as a 62.31s file). Every other lane already
    honours `start` via `adelay` (music: audio_mix.py; PIP: build_pip_overlay_chain),
    so v1 was the only track whose geometry was a lie.

    Emitting explicit black+silent filler for the leading offset, each interior
    gap and the trailing remainder makes the rendered file exactly
    `total_duration` long. That single property is what lets the playhead, the
    transport denominator, the music extent and the export all agree — and it
    makes `amix=duration=first` correct, since input 0 now spans the whole
    timeline instead of just the video content.

    Overlapping clips (legacy EDLs; `_first_free_gap` prevents new ones) keep the
    old packing behaviour via the `max(cursor, start)` cursor — never a negative
    filler.
    """
    segs: list[tuple[str, object]] = []
    cursor = 0.0
    for i, c in enumerate(clips):
        gap = c.start - cursor
        if gap > _GAP_EPS:
            segs.append(("gap", gap))
            cursor += gap
        segs.append(("clip", i))
        cursor = max(cursor, c.start) + c.effective_duration
    tail = total_duration - cursor
    if tail > _GAP_EPS:
        segs.append(("gap", tail))
    return segs


def _has_v1_gaps(clips: list[Clip], total_duration: float, fps=None) -> bool:
    """True when the v1 base needs filler (so packet-level concat can't be used)."""
    if fps is not None:
        return any(kind == "gap" for kind, _i, _n in _v1_frame_plan(clips, total_duration, fps))
    return any(kind == "gap" for kind, _ in _v1_segments(clips, total_duration))


# ---- Frame-exact clip timing (QA-002 / QA-039) -----------------------------
#
# WHY. Every clip used to be decoded with input-side `-ss %.3f -to %.3f`, and
# ffmpeg rounds each such segment UP to whole frames, so every seam gained a
# frame: a pure split of a 600-frame clip exported 602 frames with frames 151
# and 154 doubled, and after the TikTok recipe's 17 cuts the picture ran 14
# frames behind the EDL clock every other lane is placed on. The fix is to
# stop letting float seconds decide how many frames a clip emits:
#
#   * the input is seeked HALF A FRAME before `in_` (timebase.seek_preroll), so
#     the frame that starts at `in_` is always the first one kept and the one
#     before it never is, and `-to` only bounds decoding (two frames of slack);
#   * the video chain rebases to PTS 0, retimes for speed, resamples onto the
#     project grid with `fps=`, pads by cloning the last frame if the source
#     runs short and cuts with `trim=end_frame=N` — N from `clip_frames`;
#   * the audio chain drops the same pre-roll with `atrim=start`, and ends
#     with `apad`+`atrim=end_sample=M`, M = exactly N frames of samples, so the
#     concat of every clip's audio stays locked to the concat of its picture.
#
# N depends only on the clip's own fields and the rate (never on `start`),
# which is what keeps a cached chunk valid wherever the clip sits. Layout in
# frames (`_v1_frame_plan`) then decides gaps on the same grid, so a clip that
# ends within a frame of its neighbour's start is a seam, never a 1-frame gap.

#: Frames of decode slack past `out` — `trim`/`atrim` make the cut, this only
#: stops ffmpeg decoding to the end of a 12-minute source for a 3 s clip.
_DECODE_SLACK_FRAMES = 2


def clip_frames(c: Clip, fps) -> int:
    """Frames clip `c` occupies on the timeline at rate `fps` (≥ 1)."""
    return max(1, _tb.frame_of(c.effective_duration, fps))


def clip_input_args(c: Clip, fps) -> list[str]:
    """`-ss/-to/-i` for clip `c`: seek half a frame early, decode a little past
    `out`. Precision is µs, not the old `%.3f` (which alone could land a seek
    after the frame it meant to keep)."""
    pre = _tb.seek_preroll(c.in_, fps)
    seek = max(0.0, float(c.in_) - pre)
    end = float(c.out) + _DECODE_SLACK_FRAMES * _tb.frame_duration(fps)
    return ["-ss", f"{seek:.6f}", "-to", f"{end:.6f}", "-i", str(c.src)]


def _clip_preroll(c: Clip, fps) -> float:
    return _tb.seek_preroll(c.in_, fps) if fps is not None else 0.0


@lru_cache(maxsize=512)
def _has_audio_stream_cached(src: str, mtime_ns: int, size: int) -> bool:
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", src],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, **_pu.SUBPROCESS_FLAGS)
        return bool(out.stdout.strip())
    except Exception:
        return True  # unknown: keep the historical assumption


def source_has_audio(src: str) -> bool:
    """Whether `src` carries an audio stream (cached on path+mtime+size).

    QA-040: every v1 chain read `[i:a]`, so a picture-only source — the output
    of smooth slow-mo, or anything else that bypassed ingest's silent-track
    fill — failed the WHOLE timeline's preview and export with "matches no
    streams". Such a clip now gets digital silence of its exact length.
    """
    try:
        st = os.stat(src)
    except OSError:
        return True  # a missing file fails later with its own, clearer error
    return _has_audio_stream_cached(str(src), st.st_mtime_ns, st.st_size)


def _v1_frame_plan(clips: list[Clip], total_duration: float,
                   fps) -> list[tuple[str, int | None, int]]:
    """`_v1_segments` on the frame grid: ("clip", i, frames) | ("gap", None, frames).

    Same packing rule (a gap is emitted only where the next clip starts after
    the cursor; overlaps keep the old `max(cursor, start)` packing), but in
    whole frames — so the plan's total is exactly what the renderer emits and
    a sub-frame difference between a clip's end and its neighbour's start is
    not a sliver of black.
    """
    plan: list[tuple[str, int | None, int]] = []
    cursor = 0
    for i, c in enumerate(clips):
        sf = _tb.frame_of(c.start, fps)
        if sf - cursor >= 1:
            plan.append(("gap", None, sf - cursor))
            cursor = sf
        n = clip_frames(c, fps)
        plan.append(("clip", i, n))
        cursor = max(cursor, sf) + n
    tail = _tb.frame_of(total_duration, fps) - cursor
    if tail >= 1:
        plan.append(("gap", None, tail))
    return plan


def _gap_filler_chains(w: int, h: int, fps, frames: int) -> tuple[str, str]:
    """Black picture and digital silence exactly `frames` frames long (no
    labels). `d=` alone rounds to the source's own tick; `trim`/`atrim` make
    the length exact."""
    m = _tb.samples_for_frames(frames, fps)
    d = _tb.time_of(frames + 1, fps)
    v = (f"color=c=black:s={w}x{h}:r={_tb.ffmpeg_rate(fps)}:d={d:.6f},"
         f"trim=end_frame={frames},format=yuv420p,setsar=1")
    a = (f"anullsrc=channel_layout=stereo:sample_rate=48000:d={d:.6f},"
         f"atrim=end_sample={m},"
         f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")
    return v, a


def _plan_seconds(plan, fps) -> float:
    return _tb.time_of(sum(n for _k, _i, n in plan), fps)


def _build_clip_video_chain(c: Clip, *, input_label: str, label_out: str,
                            canvas_w: int, canvas_h: int, fps=None) -> str:
    """Build the per-clip video filter chain (scale + transform + effects + speed),
    starting from `input_label` (e.g. "[0:v]") and ending at `label_out`.

    Used by both the monolithic renderer (where input_label = [N:v] for the
    Nth input) and the chunk renderer (where input_label = [0:v]).

    With `fps` (every render path passes it) the chain is FRAME-EXACT: it
    starts at PTS 0 and emits exactly `clip_frames(c, fps)` frames on the
    project grid — see the block above `clip_frames`. The input must then be
    opened with `clip_input_args`.
    """
    if fps is not None:
        # Rebase first: the half-frame seek pre-roll leaves the first kept
        # frame at PTS ≈ half a frame, and every retime below assumes 0.
        input_label = f"{input_label}setpts=PTS-STARTPTS,"
    tx = c.transform
    rot_static = float(tx.rotation) if isinstance(tx.rotation, (int, float)) else 0.0
    sc_static = float(tx.scale) if isinstance(tx.scale, (int, float)) else 1.0
    rot_animated = is_keyframed(tx.rotation)
    sc_animated = is_keyframed(tx.scale)
    x_animated = is_keyframed(tx.x)
    y_animated = is_keyframed(tx.y)
    # Scalar pan, in canvas pixels. 0.0 when the axis is keyframed — that case is
    # handled entirely by the animated branch below.
    x_static = 0.0 if x_animated else float(tx.x) if isinstance(tx.x, (int, float)) else 0.0
    y_static = 0.0 if y_animated else float(tx.y) if isinstance(tx.y, (int, float)) else 0.0
    tvar = f"(t-{c.start:.4f})"

    # `fit` decides what happens when the source aspect doesn't match the canvas:
    #   contain (default) — scale DOWN to fit, pad the remainder black. Letterbox.
    #   cover             — scale UP to fill, crop the overflow. No black bars.
    # `decrease`+`pad` IS letterbox by construction, and it was the only mode
    # that existed, which is why switching 9:16 → 16:9 could only ever add bars
    # ("there is no crop option for the video, only the aspect ratio gets
    # changed"). `contain` is the default so every existing EDL renders
    # byte-identically.
    #
    # A static (non-keyframed) pan on a `cover` clip needs its own path. The
    # plain center crop below throws away everything outside the canvas box
    # BEFORE any transform runs, so the generic pan logic further down (which
    # assumes it's panning an already-canvas-sized frame) had no real footage
    # left to reveal and could only pad in manufactured black — defeating the
    # entire point of "fill frame, no bars" ("the user has no freedom of
    # cropping the particular part of video ... this function crops the video
    # by its own"). Scale-only cover zoom and every `contain` clip are
    # untouched by this — see `cover_needs_real_pan` below.
    cover_needs_real_pan = (
        getattr(c, "fit", "contain") == "cover"
        and not (sc_animated or x_animated or y_animated)
        and (x_static != 0 or y_static != 0)
    )
    if getattr(c, "fit", "contain") == "cover":
        if cover_needs_real_pan:
            # Keep the "increase"-scaled frame OVERSIZED (don't crop yet) so
            # there's real surplus footage to pan into. An optional extra
            # zoom (the same Scale slider) widens that margin further, then a
            # single crop picks the window — offset by the same x/y meaning
            # used everywhere else in this function (positive x/y moves the
            # picture right/down; see the static branch below) but evaluated
            # against the true oversized size via ffmpeg's own `in_w`/`in_h`,
            # since the exact "increase" scale-up factor depends on the
            # source's aspect ratio, which isn't known in Python here. ffmpeg
            # clamps an out-of-range crop x/y to the available margin on its
            # own, so panning past the real footage's edge holds on the last
            # real pixel instead of exposing black — cover's whole point.
            v_chain = (f"{input_label}"
                       f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,setsar=1")
            # Clamped to >=1: this multiplier only ever WIDENS the pan margin.
            # sc_static<1 (zooming OUT) would shrink the already-covering
            # frame to SMALLER than the canvas — crop then has less input
            # than its requested output size and produces black (found live:
            # a scale=0.1 pan committed via the wheel-zoom rendered a solid
            # black frame, no ffmpeg error). Mirrors the `max(1, ...)` guard
            # the keyframed branch below already applies for the same reason.
            extra_zoom = max(1.0, sc_static)
            if extra_zoom > 1.001:
                v_chain += f",scale=w='iw*{extra_zoom:.4f}':h='ih*{extra_zoom:.4f}'"
            v_chain += (
                f",crop={canvas_w}:{canvas_h}:"
                f"'(in_w-out_w)/2-{x_static:.2f}':'(in_h-out_h)/2-{y_static:.2f}'"
            )
        else:
            v_chain = (f"{input_label}"
                       f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
                       f"crop={canvas_w}:{canvas_h},setsar=1")
    else:
        v_chain = (f"{input_label}"
                   f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                   f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")

    # Rotation happens IN PLACE: the frame keeps its canvas size and the corners
    # that swing outside it are cut, exactly like the browser's `rotate()` and
    # exactly like every NLE's rotate control.
    #
    # It used to expand the output to the rotated bounding box
    # (`ow=rotw:oh=roth`) and then scale that box back down to fit the canvas
    # with `decrease`+`pad`. Nothing was lost, but the picture SHRANK — and the
    # shrink grows with the angle, so nudging a clip 3° to straighten it visibly
    # zoomed the whole shot out. Worse, `Preview.tsx`'s live preview is a CSS
    # `rotate()`, which rotates in place, so the picture jumped to a different
    # size the instant the value committed and the render landed: "while
    # rotating, the preview I get is correct, but when the changes are made the
    # preview gets changed". Two renderers of the same property have to agree,
    # and in-place is the one users expect. `rotate`'s default ow/oh IS iw/ih,
    # so simply not overriding them gives in-place rotation with black corners.
    if rot_animated:
        re = to_ffmpeg_expr(tx.rotation, time_var=tvar)
        re_rad = f"({re})*PI/180"
        v_chain += f",rotate=a='{re_rad}':c=black"
    elif abs(rot_static) > 0.001:
        rad = rot_static * 3.14159265 / 180.0
        v_chain += f",rotate={rad}:c=black"

    if sc_animated or x_animated or y_animated:
        sexpr = to_ffmpeg_expr(tx.scale, time_var=tvar) if sc_animated else f"{sc_static:.4f}"
        zoom = f"max(1\\,{sexpr})"
        if x_animated:
            xe = to_ffmpeg_expr(tx.x, time_var=tvar)
            cx_expr = f"(iw-{canvas_w})/2 + ({xe})"
        else:
            cx_expr = f"(iw-{canvas_w})/2 + {float(tx.x) if isinstance(tx.x, (int, float)) else 0:.2f}"
        if y_animated:
            ye = to_ffmpeg_expr(tx.y, time_var=tvar)
            cy_expr = f"(ih-{canvas_h})/2 + ({ye})"
        else:
            cy_expr = f"(ih-{canvas_h})/2 + {float(tx.y) if isinstance(tx.y, (int, float)) else 0:.2f}"
        v_chain += (
            f",scale=w='{canvas_w}*{zoom}':h='{canvas_h}*{zoom}':eval=frame"
            f",crop={canvas_w}:{canvas_h}:'{cx_expr}':'{cy_expr}'"
        )
    elif cover_needs_real_pan:
        # Already applied above, in the `cover`-fit block — this branch's
        # pad/crop hack assumes it's starting from an exactly canvas-sized
        # frame, which isn't true here (the cover frame was deliberately left
        # oversized so the pan above had real footage, not black, to reveal).
        pass
    elif (abs(sc_static - 1.0) > 0.001 and sc_static > 0) or x_static or y_static:
        # This branch used to fire ONLY on a scale change, and even then it
        # hardcoded a dead-centre crop — so `Transform.x`/`y` were silently
        # dropped for every v1 clip. The Position inputs in the Properties panel
        # dispatched, committed and re-rendered, and the picture never moved
        # ("I tried to change the position by changing the coordinates, but it
        # didn't flinch"). Only the KEYFRAMED branch above ever emitted x/y.
        #
        # The offset is applied EXACTLY: x=200 moves the picture 200 canvas
        # pixels right, revealing black where nothing covers the canvas. The
        # obvious alternative — zoom in just enough to have material to pan into —
        # avoids the black edge but makes the apparent shift smaller than the
        # number the user typed, which is how a control earns a reputation for
        # being half-broken. There is no client-side translate preview to diverge
        # from (Preview.tsx's liveTransform only does scale + rotate).
        #
        # scale -> pad(oversized, offset) -> crop(canvas) is one chain and is
        # exact for scale <1, ==1 and >1 alike. Note it never asks `pad` for a
        # target SMALLER than its input, which is the error that made 4:5
        # unrenderable ("Padded dimensions cannot be smaller than input").
        sw = max(2, int(canvas_w * sc_static) // 2 * 2)
        sh = max(2, int(canvas_h * sc_static) // 2 * 2)
        dx, dy = int(round(x_static)), int(round(y_static))
        v_chain += f",scale={sw}:{sh}"
        # Intermediate must cover the scaled frame AND the canvas plus the pan
        # margin on both sides; even parity for the same chroma reason as h_out.
        pw = (max(sw, canvas_w) + 2 * abs(dx) + 1) // 2 * 2
        ph = (max(sh, canvas_h) + 2 * abs(dy) + 1) // 2 * 2
        if pw > sw or ph > sh:
            v_chain += (f",pad={pw}:{ph}:"
                        f"({pw}-{sw})/2+{dx}:({ph}-{sh})/2+{dy}:color=black")
        v_chain += (f",crop={canvas_w}:{canvas_h}:"
                    f"({pw}-{canvas_w})/2:({ph}-{canvas_h})/2")

    ec = effect_chain(c.effects or [], uid=c.id)
    if ec:
        v_chain += "," + ec

    # OPACITY. This chain had NO opacity handling at all, so `Transform.opacity`
    # was a dead control on v1 — it committed to the EDL, survived a reload and
    # changed nothing in the picture, in the preview or the export. It was
    # honoured for PIPs (pip.py), text and stickers, which is why it looked
    # implemented.
    #
    # Worse than merely doing nothing, it made the LIVE preview lie in both
    # directions. Preview.tsx applies a CSS opacity RELATIVE to what the visible
    # render already has baked in (liveCssTransform divides by the baked value),
    # and it reads that baked value from the EDL. So the EDL said 0.3 while the
    # frame was still at full brightness: dragging further down showed the wrong
    # amount of dimming, and dragging back UP showed none at all, since CSS
    # opacity cannot exceed 1 and the "restore" it was counting on never existed.
    # Reported from the far end of that: "when i reduce the opacity and then i
    # rotate the main video, the video gets blacked out while rotating."
    #
    # A FADE TOWARD BLACK, not an alpha channel — and that difference is the
    # whole reason this is not simply pip.py's line. A PIP is OVERLAID, so
    # `format=yuva420p,colorchannelmixer=aa=` lets the layer beneath show
    # through. v1 is the BASE: there is nothing beneath it, so alpha here would
    # either be discarded at yuv420p conversion or composited against
    # undefined ground. Multiplying RGB toward black is what the canvas itself
    # does (the letterbox is black) and exactly what the browser does when it
    # fades the element over the black preview pane — so the live preview and
    # the bake finally agree, which is the point.
    #
    # Done in gbrp so the multiply is a true RGB multiply. colorchannelmixer on
    # YUV input would scale Y *and* the chroma offsets, tinting the picture
    # toward green as it dims instead of darkening it. The chain converts back
    # to yuv420p downstream, and this costs a conversion only when opacity is
    # actually below 1.
    opa_animated = is_keyframed(tx.opacity)
    opa_static = float(tx.opacity) if isinstance(tx.opacity, (int, float)) else 1.0
    if opa_animated:
        # Keyframed: per-frame multiply. Same geq shape text_overlay.py uses for
        # an animated text opacity, on RGB rather than on alpha.
        #
        # `T`, NOT the chain's lowercase `tvar`. geq's expression namespace is
        # its own: it exposes X/Y/W/H/N/T, and lowercase `t` is not in it, so a
        # tvar-built expression dies with "Unknown function in 't-0.0000)...'"
        # — a whole-graph failure, i.e. the render fails outright rather than
        # animating wrong. `rotate` above legitimately uses `t`, which is why
        # reusing tvar here looks right and is not.
        oe = to_ffmpeg_expr(tx.opacity, time_var=f"(T-{c.start:.4f})")
        v_chain += (f",format=gbrp,geq=r='r(X\\,Y)*({oe})'"
                    f":g='g(X\\,Y)*({oe})':b='b(X\\,Y)*({oe})',format=yuv420p")
    elif opa_static < 0.999:
        o = max(0.0, min(1.0, opa_static))
        v_chain += (f",format=gbrp,colorchannelmixer=rr={o:.4f}:gg={o:.4f}:bb={o:.4f}"
                    f",format=yuv420p")
    # ...and back to yuv420p EXPLICITLY, so a faded clip leaves this chain in the
    # same pixel format an untouched one does. libavfilter does auto-negotiate a
    # conversion into `concat` (verified: a mixed-opacity timeline renders), so
    # this is not what makes it work — it makes it DETERMINISTIC. Format
    # negotiation is build-dependent, the two shipping platforms use different
    # ffmpeg builds (Homebrew vs Gyan), and "it happens to negotiate" is the kind
    # of thing that holds on the machine it was written on. The neighbouring
    # `setsar=1` exists for the same reason, for the one parameter concat will
    # NOT negotiate.

    # Chroma key (green/blue screen) — produces transparent regions; on V1 they
    # show through to canvas bg colour, on PiP they show through to the layer below.
    if getattr(c, "chromakey", None) is not None:
        v_chain += "," + build_chromakey_filter(c.chromakey)

    if isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0:
        v_chain += f",setpts=PTS/{float(c.speed)}"

    # Visual fade from/to black (clip.video_fade_in/out). st/d are clip-local
    # TIMELINE seconds: this sits AFTER the speed setpts, so a 1 s fade on a
    # 2x clip lasts 1 s on screen and ends exactly at the clip's end. Same
    # convention as the audio side, which runs its afade after atempo against
    # `effective_duration` (QA-038 — it used source `duration`, so a 0.5x clip
    # went silent for its second half and a 2x clip never faded). At 1x the
    # two conventions are identical. d is clamped to the clip's length (and
    # skipped entirely for zero-duration clips, where fade=d=0 would error).
    eff = c.effective_duration
    vfi = min(float(getattr(c, "video_fade_in", 0.0) or 0.0), eff)
    vfo = min(float(getattr(c, "video_fade_out", 0.0) or 0.0), eff)
    if vfi > 0.001:
        v_chain += f",fade=t=in:st=0:d={vfi:.3f}"
    if vfo > 0.001:
        v_chain += f",fade=t=out:st={max(0.0, eff - vfo):.3f}:d={vfo:.3f}"

    if fps is not None:
        # QA-002/QA-039: land on the project grid HERE, inside the clip, and
        # emit an exact frame count. Before, a bare `setpts=PTS/speed` was left
        # to the output `-r`, whose frame picking ran ~2 frames late on a
        # retimed clip, and `-to` decided the length by rounding up.
        # `tpad` clones the last frame when a source is a frame short of what
        # its `out` claims (legacy audio-padded extents), so the count holds.
        n = clip_frames(c, fps)
        v_chain += (f",fps={_tb.ffmpeg_rate(fps)}"
                    f",tpad=stop={n}:stop_mode=clone"
                    f",trim=end_frame={n},setpts=PTS-STARTPTS")

    # Normalize sample aspect ratio at the end. rotate / scale-with-eval=frame
    # can produce SAR like 86519:86488 which makes concat fail with
    # "Input link parameters do not match" when neighbours have SAR 1:1.
    v_chain += ",setsar=1"
    v_chain += label_out
    return v_chain


#: See the atempo loop in `_build_clip_audio_chain` (960 samples = 20 ms @ 48k).
_ATEMPO_LAG = "adelay=delays=960S:all=1"


def _build_clip_audio_chain(c: Clip, *, input_label: str, label_out: str,
                            fps=None) -> str:
    """Per-clip audio chain: resample + atempo for speed + gain/fade/mute.

    Gain/fade/mute were previously only applied to music + vo clips (via
    audio_mix._audio_clip_filter); V1 clips lost those properties silently
    on render. The fade is positioned relative to the clip's LOCAL audio
    (which is `-ss/-to`-trimmed and starts at t=0), since concat then
    sequences these clips into the timeline absolute time.

    With `fps` the chain is SAMPLE-EXACT to the frame-exact picture: it drops
    the input's half-frame seek pre-roll and ends at exactly
    `samples_for_frames(clip_frames(c, fps))` samples (see `clip_frames`).
    A source with no audio stream at all gets silence of that length
    (QA-040) instead of failing the whole graph on `[i:a]`.
    """
    if fps is not None and not source_has_audio(str(c.src)):
        input_label = "anullsrc=channel_layout=stereo:sample_rate=48000,"
    a_chain = (f"{input_label}aresample=async=1:first_pts=0,"
               f"aformat=channel_layouts=stereo:sample_rates=48000")
    pre = _clip_preroll(c, fps)
    if pre > 1e-9:
        a_chain += f",atrim=start={pre:.6f},asetpts=PTS-STARTPTS"
    if (isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0
            and not getattr(c.audio, "keep_pitch", True)):
        # Varispeed (QA-039 residual): re-clocked, sample-exact, pitch moves.
        from .audio_mix import varispeed_filter
        a_chain += "," + varispeed_filter(float(c.speed))
    elif isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0:
        remaining = float(c.speed)
        # Every atempo stage is preceded by `_ATEMPO_LAG` of silence: ffmpeg's
        # WSOLA emits content ~20 ms of ITS INPUT early (measured on a 57-click
        # track: -42.4 ms at 0.5x, -24.0 at 0.75x, -15.6 at 1.25x, -12.4 at
        # 1.5x — a constant 18-21 ms of source time, divided by the tempo).
        # Delaying the stage's input by that much centres transients on the
        # retimed picture at every tempo (QA-039), leaving only WSOLA's own
        # ±12 ms jitter, at the cost of that sliver of silence at the head of
        # a retimed clip. The tail is cut back by the exact-length atrim.
        while remaining > 2.0:
            a_chain += f",{_ATEMPO_LAG},atempo=2.0"
            remaining /= 2.0
        while remaining < 0.5:
            a_chain += f",{_ATEMPO_LAG},atempo=0.5"
            remaining /= 0.5
        if abs(remaining - 1.0) > 0.001:
            a_chain += f",{_ATEMPO_LAG},atempo={remaining:.4f}"
    a_chain += _audio_props_filters(c)
    if fps is not None:
        m = _tb.samples_for_frames(clip_frames(c, fps), fps)
        a_chain += f",apad=whole_len={m},atrim=end_sample={m}"
    a_chain += label_out
    return a_chain


def _audio_props_filters(c: Clip) -> str:
    """`,volume=…,afade=…` fragment for a clip's gain/fade/mute (no labels).

    Fade times are clip-LOCAL (the source is -ss/-to-trimmed and starts at
    t=0), so callers must apply this BEFORE any adelay repositioning.
    """
    frag = ""
    if c.audio:
        if abs(c.audio.gain_db) > 0.01:
            frag += f",volume={c.audio.gain_db:.2f}dB"
        # Volume automation (QA-086), keyed in this same clip-local time.
        from .audio_mix import gain_env_filter
        env = gain_env_filter(c.audio)
        if env:
            frag += "," + env
        if c.audio.fade_in > 0.001:
            # fade-in starts at local t=0 and runs for fade_in seconds.
            frag += f",afade=t=in:st=0:d={c.audio.fade_in:.3f}"
        if c.audio.fade_out > 0.001:
            # fade-out ends at the clip's TIMELINE end. This fragment runs
            # after atempo (`_build_clip_audio_chain`), where local time is
            # timeline seconds, so the end is `effective_duration` — QA-038:
            # it used source `duration`, so a 0.5x clip faded at its midpoint
            # and sat in -180 dB silence for the rest, and a 2x clip's fade
            # started after its audio had already ended (never heard). PIP
            # audio calls this too; a PIP is always 1x, where the two agree.
            fade_out_start = max(0.0, c.effective_duration - c.audio.fade_out)
            frag += (f",afade=t=out:st={fade_out_start:.3f}"
                     f":d={c.audio.fade_out:.3f}")
        if c.audio.mute:
            frag += ",volume=0"
    return frag


def _build_filter_complex(clips: list[Clip], canvas_w: int, canvas_h: int,
                          *, transitions: list | None = None,
                          cache_dir: Path | None = None,
                          chunk_paths: list[Path] | None = None,
                          fps: int = 30, total_duration: float = 0.0,
                          seams: clock.SeamTable | None = None,
                          ) -> tuple[str, list[str], list[str], list[str]]:
    """Build the video+audio filter chain for the V1 timeline.

    `total_duration` is the LAYOUT end of the timeline (`edl.duration +
    edl.transition_overlap()`), NOT `edl.duration`: `_v1_segments` walks a
    layout cursor, and the trailing filler is `total_duration − cursor`. It
    was handed `edl.duration` — already render time — so whenever a music
    bed or sticker outlived v1 on a timeline with transitions the tail came
    out short by the total overlap: A=2 s, B=2 s, fade 0.5, music to layout
    5.0 → edl.duration 4.5, file 4.0 s, the bed's last 0.45 s and a sticker at
    layout 4.5–5.0 (clock 4.0–4.5) simply absent while the transport promised
    them. `seams` is the seam table the xfades follow (see below); when not
    given it is derived from `clips`/`transitions` by the same function.

    Returns (filter_str, input_args, [v_label, a_label], extra_inputs_for_masks).
    Each clip is decoded with input-side seeking, scaled+padded to canvas,
    then runs through any per-clip effect chain, then mask alphamerge if a
    mask is set, then enters the timeline assembly (concat OR xfade).

    The assembly runs over `_v1_segments`, not over the clip list: black+silent
    filler is emitted for the leading offset, interior gaps and the trailing
    remainder so the output spans `total_duration` exactly. Fillers are lavfi
    filter SOURCES (no `-i`), which keeps the per-clip chunk cache 1:1 with
    `clips` and untouched.
    """
    inputs: list[str] = []
    extra_inputs: list[str] = []
    fc_parts: list[str] = []
    v_labels: list[str] = []
    a_labels: list[str] = []

    transitions = transitions or []
    use_chunks = chunk_paths is not None and len(chunk_paths) == len(clips)

    # Pass 1: assign clip indices [0..N-1]; emit clip-side filter chains and
    # remember which clips need masks (their PNGs are added as inputs in
    # pass 2 so their input indices land AFTER all clip inputs — that's the
    # actual ordering ffmpeg sees on the command line).
    pending_masks: list[tuple[int, Path]] = []  # (clip_index, mask_path)
    for i, c in enumerate(clips):
        if use_chunks:
            inputs += ["-i", str(chunk_paths[i])]
            fc_parts.append(f"[{i}:v]null[ve{i}]")
            # A decoded chunk's AAC runs up to one codec frame past its
            # picture (the padded last frame is not trimmed on decode), and
            # concat advances by the LONGER stream — a frame duplicated every
            # couple of seams. Cut it back to exactly the clip's frames.
            m = _tb.samples_for_frames(clip_frames(c, fps), fps)
            fc_parts.append(f"[{i}:a]apad=whole_len={m},atrim=end_sample={m}[a{i}]")
            v_labels.append(f"[ve{i}]")
            a_labels.append(f"[a{i}]")
            continue

        inputs += clip_input_args(c, fps)
        fc_parts.append(_build_clip_video_chain(
            c, input_label=f"[{i}:v]", label_out=f"[ve{i}]",
            canvas_w=canvas_w, canvas_h=canvas_h, fps=fps,
        ))
        if c.mask is not None and cache_dir is not None:
            mask_path = cache_dir / f"mask_{c.id}_{c.mask.type}_{int(c.mask.feather)}_{canvas_w}x{canvas_h}.png"
            if not mask_png_is_valid(mask_path):
                render_mask_png(c.mask, canvas_w, canvas_h, mask_path)
            pending_masks.append((i, mask_path))

        fc_parts.append(_build_clip_audio_chain(
            c, input_label=f"[{i}:a]", label_out=f"[a{i}]", fps=fps,
        ))
        a_labels.append(f"[a{i}]")
        v_labels.append(f"[ve{i}]")  # tentative; rewritten below if mask present

    # Pass 2: append mask inputs (so their ffmpeg indices are N + k) and emit
    # alphamerge filter chunks; rewrite the corresponding v_label to the masked one.
    n_clips = len(clips)
    for k, (clip_i, mask_path) in enumerate(pending_masks):
        extra_inputs += ["-i", str(mask_path)]
        mask_idx = n_clips + k
        v_masked = f"[vm{clip_i}]"
        fc_parts.append(
            f"[ve{clip_i}][{mask_idx}:v]"
            f"alphamerge,format=yuva420p[vmrgba{clip_i}];"
            f"color=c=black:s={canvas_w}x{canvas_h}:r={_tb.ffmpeg_rate(fps)}[bg{clip_i}];"
            f"[bg{clip_i}][vmrgba{clip_i}]overlay=format=auto:shortest=1{v_masked}"
        )
        v_labels[clip_i] = v_masked

    # ---- Segment plan: clips interleaved with black+silent gap filler ----
    # On the FRAME grid (`_v1_frame_plan`): every segment's length is a whole
    # number of frames and its audio exactly that many frames of samples, so
    # the assembled stream is exactly the plan long (QA-002).
    segments = _v1_frame_plan(clips, total_duration, fps)
    if not segments:
        return "", [], [], []

    seg_v: list[str] = []
    seg_a: list[str] = []
    seg_dur: list[float] = []
    seg_of_clip: dict[int, int] = {}
    for kind, ci, nfr in segments:
        if kind == "clip":
            seg_of_clip[ci] = len(seg_v)
            seg_v.append(v_labels[ci])
            seg_a.append(a_labels[ci])
            seg_dur.append(_tb.time_of(nfr, fps))
        else:
            g = _tb.time_of(nfr, fps)
            k = len(seg_v)
            vg, ag = f"[vgap{k}]", f"[agap{k}]"
            vf, af = _gap_filler_chains(canvas_w, canvas_h, fps, nfr)
            # format/setsar/aformat pin the filler to the same parameters the
            # clip chains produce — concat refuses inputs whose link params
            # differ (it does not auto-convert).
            fc_parts.append(vf + vg)
            fc_parts.append(af + ag)
            seg_v.append(vg)
            seg_a.append(ag)
            seg_dur.append(g)

    # Transitions bridge two clips that are ADJACENT segments — a cross-fade
    # across intervening black is meaningless, so a gapped boundary stays a cut.
    #
    # WHICH seam gets WHICH duration is not decided here. `seam_table_for`
    # (edl/schema.py — the body of `EDL.v1_seam_table()`) is the one rule:
    # adjacency, the 0.05 s boundary match, the FIRST record at a stacked
    # cut, the cost clamped to the shorter neighbour. `edl.duration`, the
    # render clock every other lane is placed through, the desktop's
    # `seamTable` and the benchmark all read that table; this loop used to be
    # a second copy that disagreed with it three ways (last record won,
    # `effective_duration` resolved sub-0.1 s records here but not there,
    # nothing clamped to the clip lengths), and every disagreement was the
    # picture running ahead of the overlays by the difference, with the video
    # stream ending before the audio. Measured: A=2 s, B=0.3 s, fade 0.5 →
    # video 1.8 s / audio 2.0 s; fade 5.0 between 2 s clips → video 2.0 s /
    # audio 4.0 s with clip C never shown. So: the COST comes from the table
    # (matched on the boundary it was built from, same 1 ms as
    # `_assemble_v1_audio`), only the LOOK comes from the record — the first
    # match, the same record the table charged — and a boundary the table
    # does not list is a hard cut, whatever records sit near it.
    from ..edl.schema import seam_matching, seam_table_for
    records = list(transitions)
    if seams is None:
        seams = seam_table_for(clips, records, fps=fps)
    seg_trans: dict[int, tuple[str, float]] = {}
    for idx, c in enumerate(clips[:-1]):
        si = seg_of_clip.get(idx)
        if si is None or seg_of_clip.get(idx + 1) != si + 1:
            continue
        boundary = c.start + c.effective_duration
        cost = next((d for seam, d in seams if abs(seam - boundary) < _GAP_EPS), 0.0)
        record = seam_matching(records, boundary)
        if cost > 0.0 and record is not None:
            seg_trans[si] = (record.type, cost)

    # ---- Timeline assembly ----
    if not seg_trans:
        # Plain concat (interleaved [v0][a0][v1][a1]...)
        interleaved = "".join(f"{v}{a}" for v, a in zip(seg_v, seg_a))
        fc_parts.append(f"{interleaved}concat=n={len(seg_v)}:v=1:a=1[vout][aout]")
    else:
        # Chain xfade for video, acrossfade for audio between adjacent segments.
        # cur_dur tracks the accumulated OUTPUT stream length: each clip's
        # per-clip chain applies setpts/atempo for speed (and chunk files
        # bake it), so streams entering xfade are effective_duration long —
        # source `duration` would place every offset after a sped clip at
        # the wrong time. Filler segments contribute their own length.
        cur_v = seg_v[0]
        cur_a = seg_a[0]
        cur_dur = seg_dur[0]
        for i in range(1, len(seg_v)):
            tr_for_left = seg_trans.get(i - 1)
            new_v = f"[xv{i}]"
            new_a = f"[xa{i}]"
            if tr_for_left:
                ttype, tdur = tr_for_left
                offset = max(0.0, cur_dur - tdur)
                # Resolve the friendly name to a real xfade transition (or a
                # custom-expr spec). Keeps glitch/whip/spin/slide/zoom from
                # crashing the render the way the raw passthrough used to.
                from .transitions import resolve_transition
                xf_name, xf_expr = resolve_transition(ttype)
                # Both numbers are whole frames (seam_table_for(fps=...)), printed
                # at µs precision so neither is re-rounded off the grid, and the
                # acrossfade below overlaps the sound by the SAME seconds.
                xf = f"xfade=transition={xf_name}:duration={tdur:.6f}:offset={offset:.6f}"
                if xf_expr:
                    # expr is wrapped in single quotes; it contains no quotes itself.
                    xf += f":expr='{xf_expr}'"
                # xfade's config_output REQUIRES both inputs to share a
                # timebase, and ours routinely don't: a link coming out of
                # `concat` or a `color=` filler is normalised to 1/1000000,
                # while a raw per-clip chain keeps the demuxer's own tbn (and a
                # mask/alphamerge branch keeps yet another). Any mismatch aborts
                # the whole graph with "Input link parameters … do not match",
                # which the UI then reported as "corrupt frames or an unusual
                # codec" — blaming the user's media for a graph bug.
                #
                # It bit hardest when a seam was NOT the first (the left side had
                # been through concat), which is why "add a transition, then
                # split" reproduced so reliably. But it also hit the FIRST seam
                # whenever the two sources had different container timebases
                # (mp4 + mkv) or the left clip carried a Mask — both verified.
                #
                # Pin both inputs at the NODE, not inside _build_clip_video_chain:
                # that function is also handed to the chunk renderer, and
                # chunks.fingerprint_clip keys the cache on clip FIELDS, never on
                # the chain string — so changing the chain there would silently
                # serve every already-cached chunk with the old timebase and no
                # invalidation.
                ltb, rtb = f"[xtbl{i}]", f"[xtbr{i}]"
                fc_parts.append(f"{cur_v}settb=AVTB{ltb}")
                fc_parts.append(f"{seg_v[i]}settb=AVTB{rtb}")
                # A few transitions need a real filter on top of the blend,
                # because an xfade expr can only pick between the two pixels at
                # one coordinate — it cannot smear them. `whip` is a slide plus
                # a directional blur burst; without it the name just produced a
                # soft slide. Gated with enable= to the transition window in
                # OUTPUT time, so it cannot touch the rest of the timeline.
                from .transitions import post_filter
                post = post_filter(ttype, offset, offset + tdur,
                                   canvas_w, canvas_h)
                if post:
                    mid = f"[xpost{i}]"
                    fc_parts.append(f"{ltb}{rtb}{xf}{mid}")
                    fc_parts.append(f"{mid}{post}{new_v}")
                else:
                    fc_parts.append(f"{ltb}{rtb}{xf}{new_v}")
                # acrossfade performs no such timebase check — leave it alone.
                fc_parts.append(
                    f"{cur_a}{seg_a[i]}acrossfade=d={tdur:.6f}{new_a}"
                )
                cur_dur = cur_dur + seg_dur[i] - tdur
            else:
                fc_parts.append(
                    f"{cur_v}{seg_v[i]}concat=n=2:v=1:a=0{new_v}"
                )
                fc_parts.append(
                    f"{cur_a}{seg_a[i]}concat=n=2:v=0:a=1{new_a}"
                )
                cur_dur += seg_dur[i]
            cur_v = new_v
            cur_a = new_a
        # Rename the final accumulators to [vout]/[aout] for downstream code
        fc_parts.append(f"{cur_v}null[vout]")
        fc_parts.append(f"{cur_a}anull[aout]")

    return ";".join(fc_parts), inputs, ["[vout]", "[aout]"], extra_inputs


# AAC output args. Pinning the sample rate (48 kHz) and channel layout
# (stereo) on the *output stream* — not just inside the filtergraph — guards
# against ffmpeg's native AAC encoder rejecting a negotiated PCM format with
# "Task finished with error code: -22 (Invalid argument)". This was observed on
# preview re-renders after an aspect-ratio change (Reels/Shorts/TikTok): the
# aac encoder thread died with EINVAL and the muxer wrote zero packets
# ("Nothing was written into output file, streams received no packets").
# Forcing -ar/-ac makes ffmpeg insert an implicit resampler so the encoder
# always receives a layout it supports. The /vo_record transcode in main.py
# already does this; the render pipeline now matches.
_AAC_OUT = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


@lru_cache(maxsize=1)
def _preview_aac_out() -> list[str]:
    """AAC args for the preview fast path, whose only encode is the audio
    (QA-030: it must be re-encoded to stay in sync). AudioToolbox (`aac_at`)
    is ~3x faster than ffmpeg's native coder on a Mac; elsewhere, or if it
    fails a real 0.1 s encode, the native encoder with its fast coder. Same
    rate/channel pinning as `_AAC_OUT`.

    `-aac_at_quality 2` is AudioToolbox's speed end of its quality/speed knob
    (QA-005): 2.6x faster again on speech (180 s: 1.13 s -> 0.43 s) for 0.7 dB
    of SNR at 192k (45.7 vs 46.4 dB on narration; the native coder: 38.7), and
    the same zero lag. This encode is the floor of every warm preview edit —
    a 12-min timeline's whole sound, re-encoded per split or volume change."""
    at_args = ["-c:a", "aac_at", "-b:a", "192k", "-aac_at_quality", "2"]
    try:
        proc = subprocess.run(
            [_pu.FFMPEG, "-v", "error", "-f", "lavfi", "-i",
             "anullsrc=channel_layout=stereo:sample_rate=48000", "-t", "0.1",
             *at_args, "-f", "null", "-"],
            capture_output=True, timeout=15, **_pu.SUBPROCESS_FLAGS)
        if proc.returncode == 0:
            return [*at_args, "-ar", "48000", "-ac", "2"]
    except Exception:
        pass
    return [*_AAC_OUT, "-aac_coder", "fast"]


def _render(edl: EDL, dst: Path, *, height: int, fps: int, preview: bool,
            cache_dir: Path | None = None, on_progress=None,
            cancel_event=None, crf: int | None = None,
            bitrate_kbps: int | None = None, bitrate_peak_cap: bool = True,
            chunked: bool = True) -> Path:
    # The single chokepoint every ffmpeg render passes through (preview and
    # export both land here), so it is where the concurrency ceiling belongs.
    #
    # A background export's `cancel_event` becomes the render.cancel scope for
    # the whole render: the chunk stage (get_or_build_chunks) and the slot wait
    # run their ffmpegs through render.cancel, which only ever honoured the
    # preview-supersede scope — so POST /jobs/{id}/cancel left the chunk
    # ffmpegs running to completion and the job 'running' for minutes.
    if cancel_event is not None and _cancel.current() is None:
        try:
            with _cancel.scope(cancel_event):
                return _render(edl, dst, height=height, fps=fps, preview=preview,
                               cache_dir=cache_dir, on_progress=on_progress,
                               cancel_event=cancel_event, crf=crf,
                               bitrate_kbps=bitrate_kbps,
                               bitrate_peak_cap=bitrate_peak_cap, chunked=chunked)
        except _cancel.RenderCancelled:
            from ..api.jobs import JobCancelled
            raise JobCancelled() from None
    # Cancel-aware acquire: a superseded preview (render.cancel) gives up its
    # place in the queue instead of waiting for a slot it will never use.
    _cancel.acquire(_RENDER_SLOTS)
    try:
        return _render_locked(edl, dst, height=height, fps=fps, preview=preview,
                              cache_dir=cache_dir, on_progress=on_progress,
                              cancel_event=cancel_event, crf=crf,
                              bitrate_kbps=bitrate_kbps,
                              bitrate_peak_cap=bitrate_peak_cap, chunked=chunked)
    finally:
        _RENDER_SLOTS.release()


def _render_locked(edl: EDL, dst: Path, *, height: int, fps: int, preview: bool,
                   cache_dir: Path | None = None, on_progress=None,
                   cancel_event=None, crf: int | None = None,
                   bitrate_kbps: int | None = None, bitrate_peak_cap: bool = True,
                   chunked: bool = True) -> Path:
    # Track solo (QA-086) is an audio rule: soloed-out lanes render muted.
    from .audio_mix import apply_solo
    edl = apply_solo(edl)
    canvas = edl.canvas
    # BOTH output dimensions must be even. H.264/yuv420p subsamples chroma 2x2,
    # so an odd dimension is unencodable — and long before the encoder, an odd
    # canvas height makes the filtergraph internally inconsistent, because
    # ffmpeg's `pad` FLOORS its target to the chroma multiple while rounding its
    # input up. A 4:5 project (1080x1350) hits exactly this: render_preview's
    # short-edge math yields 675, and the graph then breaks four different ways
    # depending on the timeline's shape —
    #   pad=540:675 fed a 540x675-tall clip -> "Padded dimensions cannot be
    #     smaller than input dimensions" -> -22 -> "Nothing was written…"
    #   gap filler `color=…:s=540x675` vs clip chains at 540x674 -> concat
    #     "Input link parameters … do not match"
    #   mask alphamerge -> "Input frame sizes do not match (540x674 vs 540x675)"
    #   no-v1 filler straight to the encoder -> libx264 "height not divisible
    #     by 2" (macOS hid this leg: h264_videotoolbox silently writes 674)
    # FLOOR (not round up) so this agrees with what `pad` already does to its
    # own target, making the clip chains, gap filler, mask PNG, PIP base and
    # encoder all settle on the same number.
    h_out = max(2, int(height) // 2 * 2)
    # Leave this expression alone. It already forces even, and fed h_out=674 it
    # still returns 540. Rewriting it as _even(round(canvas.w*h_out/canvas.h))
    # would return 538 and change a width that works today.
    w_out = int(round(canvas.w * (h_out / canvas.h) / 2) * 2)
    enc_args = _video_encoder_args(preview=preview, crf=crf, bitrate_kbps=bitrate_kbps,
                                   bitrate_peak_cap=bitrate_peak_cap)

    # QA-037: a reversed clip plays a cached intermediate that already runs
    # backwards (render.reverse); from here on it is an ordinary clip. A copy
    # of the EDL, and only when something is reversed.
    from .reverse import with_reversed_sources
    edl = with_reversed_sources(edl, cache_dir, fps)

    clips = _video_clips(edl)
    # The v1 base always spans the WHOLE timeline (see `_v1_segments`): gaps and
    # the trailing remainder become black+silent filler. A timeline with no v1
    # clips at all is therefore just the degenerate case — one full-length
    # filler — and NOT a special path. It used to short-circuit to a bare
    # black+anullsrc render that never called `build_audio_mix`, which silently
    # dropped every note of a music-only timeline.
    #
    # The v1 assembly walks LAYOUT time (`_v1_segments`' cursor is clip
    # start + effective duration), so the extent it pads to must be the
    # layout end, which is `edl.duration` (render length) plus the overlap the
    # transitions consumed — see `_build_filter_complex`'s docstring for the
    # measured 0.5 s short file this used to produce.
    seams = clock.seam_table(edl)
    total_duration = max(0.0, edl.duration + sum(d for _s, d in seams))
    if not clips:
        total_duration = max(1.0, total_duration)  # never emit a 0-length file

    # Pull transitions for V1 from the EDL (transitions live on the v1 track in M4)
    v1 = edl.get_track("v1")
    transitions = (v1.transitions if v1 else []) or []

    # Chunk cache: when no V1 transitions, render each clip ONCE to an
    # OUTPUT-resolution mp4 and cache by fingerprint. Subsequent renders that
    # don't change the clip skip straight to a fast concat. Disabled when
    # transitions are present (xfade needs both streams in one filter graph).
    #
    # Output res, not canvas res: the assembly passes chunks through with a
    # `null` filter (no rescale), so chunk dimensions ARE the final preview
    # dimensions. Rendering preview chunks at full 1080x1920 made the cold
    # first preview ~3x slower than needed and shipped megabytes of extra
    # video to the <video> tag. Export calls _render with height=canvas.h, so
    # export chunks stay full-res; the fingerprint includes the dims, keeping
    # preview/export chunks cached separately (they already differed by
    # encoder args anyway).
    #
    # EXPORT renders in ONE pass (`chunked=False`, decided in render_export)
    # unless the timeline has very many clips (QA-097). Chunking an export encoded every frame TWICE — once into its
    # chunk, again in the assembly — which made it ~2x slower than a single
    # pass (measured: 200 s clip 28.0 s vs 14 s; 40 clips 13.6 s vs 9.9 s),
    # cost ~1 dB PSNR of generation loss, and sat at 0 % with no progress for
    # the whole chunk stage. An export is rendered once per request, so the
    # cache saved nothing in exchange. The single pass holds every clip's
    # decoder open at once, so past `_EXPORT_SINGLE_PASS_MAX_CLIPS` inputs the
    # chunk path stays (it bounds decoder memory: 150 clips measured 3.3 GB
    # single-pass vs 2.4 GB chunked) — and then reports its own progress.
    chunk_paths: list[Path] | None = None
    chunk_share = [0.0]
    use_chunks = cache_dir is not None and not transitions and chunked
    outer_progress = on_progress
    if use_chunks:
        chunk_progress = None
        if outer_progress is not None:
            def chunk_progress(frac: float) -> None:
                chunk_share[0] = _CHUNK_PHASE_SHARE
                outer_progress(_CHUNK_PHASE_SHARE * max(0.0, min(1.0, frac)))
        try:
            from .chunks import get_or_build_chunks
            chunk_paths = get_or_build_chunks(
                clips,
                cache_dir=cache_dir / "chunks",
                canvas_w=w_out, canvas_h=h_out, fps=fps,
                encoder_args=enc_args,
                build_video_chain=_build_clip_video_chain,
                build_audio_chain=_build_clip_audio_chain,
                # Previews build long clips from cached picture segments, so
                # an edit re-encodes only the segments it touched (QA-005).
                segment=preview,
                on_progress=chunk_progress,
            )
        except _cancel.RenderCancelled:
            raise
        except Exception:
            # Cache miss / chunk render failure → fall back to monolithic.
            chunk_paths = None
    if outer_progress is not None and chunk_share[0] > 0.0:
        # The assembly fills the rest of the bar after the chunk stage.
        share = chunk_share[0]
        on_progress = (lambda p: outer_progress(  # noqa: E731
            share + (1.0 - share) * max(0.0, min(1.0, p))))

    fc, inputs, labels, mask_inputs = _build_filter_complex(
        clips, w_out, h_out, transitions=transitions, cache_dir=cache_dir,
        chunk_paths=chunk_paths, fps=fps, total_duration=total_duration,
        seams=seams,
    )
    v_label = labels[0]
    a_label = labels[1]

    # Track-level v1 mute: audio-only (v1 is the base video layer — unlike a
    # muted V2 track, which also hides its overlay). Was a dead control: the
    # render pipeline never read v1.muted. Applied BEFORE the PIP fold so it
    # mutes only v1's own audio, not overlay/music/vo.
    if v1 is not None and v1.muted:
        fc += f";{a_label}volume=0[a_v1muted]"
        a_label = "[a_v1muted]"

    extra_inputs: list[str] = list(mask_inputs)
    # Each clip + each mask are separate inputs; track the running input index
    next_idx = len(clips) + (len(mask_inputs) // 2)

    # V2 picture-in-picture overlays. Each PIP clip is added as a new -i and
    # composited on top of V1 with its transform; audio comes back as a list
    # we feed into the audio mixer below.
    pip_chain, pip_inputs, pip_v_label, pip_audio_clips = build_pip_overlay_chain(
        edl,
        source_label=v_label,
        out_label="[vpip_final]",
        first_input_index=next_idx,
        out_w=w_out, out_h=h_out,
        # In preview the PIP's PICTURE is drawn by the browser instead of baked,
        # so direct manipulation is live; its AUDIO still comes back here. See
        # the preview branch in pip.py.
        preview=preview,
        fps=fps,
    )
    if pip_chain:
        fc = fc + ";" + pip_chain
        v_label = pip_v_label
    extra_inputs += pip_inputs
    # Count actual "-i" occurrences rather than assume a fixed arg-width per
    # item: PIP inputs are a stable 6-arg shape today, but the input-count
    # math must not silently rot the day any builder's input recipe changes
    # (this is exactly how the text-overlay side of this same pattern broke
    # when animated overlays grew from 2 args to 10 — see the fix below).
    pip_inputs_count = pip_inputs.count("-i")
    # Capture the PIP inputs' own starting index NOW, before any later block
    # (text overlays) advances next_idx further — deriving it afterward via
    # subtraction is exactly the bug this whole fix removes: it silently
    # assumed next_idx had advanced by ONLY the PIP count since pre_pip's
    # "true" value, which broke the instant the text-overlay block also
    # advanced next_idx (PIP clip + any baked text/sticker together).
    pre_pip = next_idx
    next_idx += pip_inputs_count

    # Composite text overlay PNGs (rendered by Pillow) via ffmpeg overlay= filter.
    overlay_chain = ""
    if cache_dir is not None:
        chain, txt_inputs, after_label = build_overlay_chain(
            edl, cache_dir,
            source_label=v_label,
            out_label="[vtxt_final]",
            first_input_index=next_idx,
            out_w=w_out, out_h=h_out,
            preview=preview,
        )
        if chain:
            overlay_chain = chain
            fc = fc + ";" + chain
            v_label = after_label
        extra_inputs += txt_inputs
        # Count actual "-i" occurrences, NOT len(txt_inputs)//2 — a static
        # overlay is 2 args ("-i", path) but an ANIMATED overlay (keyframed
        # opacity, or an anim_in/anim_out preset) is 10 args (-itsoffset X
        # -loop 1 -framerate 30 -t D -i path). The old //2 divisor silently
        # undercounted every animated item as "2.5 inputs", shifting every
        # subsequent input index (PIP audio, music, vo) and breaking the
        # whole downstream audio mix the moment any text clip used an anim
        # preset — reproduced live: 1 anim clip + 1 music clip → ffmpeg
        # "Invalid file index" / "matches no streams" on export.
        next_idx += txt_inputs.count("-i")

    # Warm-render fast path: a chunk-only timeline (no transitions — implied
    # by chunk_paths — no PIP, no baked overlays, no masks) is exactly the
    # chunk sequence, so assemble with the concat DEMUXER and `-c:v copy`
    # instead of re-encoding the whole timeline (which cost ~55ms per
    # timeline-second on every edit regardless of edit size). Preview-only:
    # export keeps the single re-encode assembly (and its progress/cancel
    # plumbing) unchanged.
    # Gapped timelines are excluded: packet-level concat can only stitch the
    # chunk files themselves, so it would silently reproduce the old
    # gap-collapsing bug (and a trailing gap — music outlasting the video — is
    # extremely common). Those fall through to the re-encode path, which still
    # reuses the cached per-clip chunks and only pays for the assembly.
    # `not pip_audio_clips`, NOT `not pip_chain`: this path returns BEFORE the
    # audio fold below, so it must be gated on "this timeline has no PIP at all".
    # `pip_chain` was a safe proxy for that only while a PIP always emitted video
    # filters — in preview it no longer does (the browser draws the picture), so
    # the proxy quietly admitted PIP timelines here and stream-copied the chunks
    # with the PIP's audio never mixed in. Caught by test_pip_clip_gain_is_honored:
    # a near-silent v1 plus a full-gain PIP measured -74.2 dB, i.e. silence.
    # `pip_audio_clips` is appended to unconditionally on both branches, so it is
    # the honest "are there PIPs" signal.
    if (preview and chunk_paths is not None and not pip_chain and not pip_audio_clips
            and not overlay_chain and not mask_inputs
            and not _has_v1_gaps(clips, total_duration, fps)
            and on_progress is None and cancel_event is None):
        try:
            return _assemble_chunks_streamcopy(edl, chunk_paths, dst, fps=fps)
        except Exception:
            pass  # any concat/copy hiccup → fall through to the re-encode path

    # Fold V2 PiP audio into the V1 main audio before the music+vo mixer runs.
    # Each PIP clip's audio is positioned at its RENDER start via adelay and
    # amix'd with the main concat audio. Render start, not layout start: the
    # main audio it is mixed with is the xfade/acrossfade output, already
    # pulled left by every cross-fade before the PIP, and pip.py placed the
    # picture (`-itsoffset`) at that same instant. The audio comes from its
    # OWN input (pip.pip_audio_input_index — see INPUTS_PER_PIP there for why
    # one input cannot carry both), already `-t`-capped to the render window,
    # so no trim is needed here. `seams` is the table the v1 assembly above
    # was xfaded with — one table for the whole graph.
    if pip_audio_clips:
        # PIP inputs start at pre_pip (INPUTS_PER_PIP of them per clip) —
        # pre_pip is captured ABOVE, right after pip_inputs_count is known and
        # before next_idx advances any further (the text-overlay block below
        # also advances next_idx; re-deriving pre_pip via subtraction here
        # used to silently assume it hadn't, shifting these indices whenever
        # a PIP clip AND any baked text/sticker coexisted).
        pa_parts: list[str] = []
        pa_labels: list[str] = []
        for j, c in enumerate(pip_audio_clips):
            input_idx = pip_audio_input_index(pre_pip, j)
            # The same render window pip.py placed the picture in; the chain
            # is sample-exact to its frames and carries the clip's own
            # gain/fade/mute (QA-002 PIP half — see pip.pip_audio_chain).
            win = clock.render_window(seams, c.start, c.start + c.duration)
            rs, re = win if win is not None else (c.start, c.start + c.duration)
            pa_label = f"[pa{j}]"
            pa_parts.append(pip_audio_chain(c, f"[{input_idx}:a]", pa_label,
                                            rs=rs, re=re, fps=fps))
            pa_labels.append(pa_label)
        # Mix pip audio with main audio
        mix_inputs = a_label + "".join(pa_labels)
        mixed_label = "[a_with_pip]"
        pa_parts.append(
            f"{mix_inputs}amix=inputs={1 + len(pa_labels)}:duration=first"
            f":dropout_transition=0:normalize=0{mixed_label}"
        )
        fc = fc + ";" + ";".join(pa_parts)
        a_label = mixed_label

    # Mix in music + voiceover tracks (with optional ducking against main audio).
    # Loudnorm only runs on export — preview skips it (see audio_mix docstring).
    audio_chain, audio_inputs, final_audio_label = build_audio_mix(
        edl,
        main_audio_label=a_label,
        first_input_index=next_idx,
        apply_loudnorm=not preview,
    )
    if audio_chain:
        fc = fc + ";" + audio_chain
    extra_inputs += audio_inputs

    tmp = _part_path(dst)
    args = [_pu.FFMPEG, "-y", *inputs, *extra_inputs,
            "-filter_complex", fc,
            "-map", v_label, "-map", final_audio_label,
            "-r", _tb.ffmpeg_rate(fps),
            *enc_args,
            *_AAC_OUT,
            "-movflags", "+faststart",
            str(tmp)]
    # Export streams progress (and can be cancelled); preview keeps the plain
    # blocking path so nothing about its hot loop changes.
    if on_progress is not None or cancel_event is not None:
        try:
            rc, err = _run_ffmpeg_progress(args, edl.duration, on_progress, cancel_event)
        except BaseException:
            _pu.unlink_with_retry(tmp)
            raise
    else:
        # _cancel.run IS subprocess.run unless a superseded-preview scope is
        # active (render.cancel), in which case it can terminate ffmpeg early.
        try:
            proc = _cancel.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **_pu.SUBPROCESS_FLAGS)
        except BaseException:
            _pu.unlink_with_retry(tmp)
            raise
        rc, err = proc.returncode, proc.stderr
    if rc != 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(f"ffmpeg render failed (rc={rc}):\n{(err or '')[-2000:]}")
    _pu.replace_with_retry(tmp, dst)  # atomic swap; retries on Windows if a reader holds dst
    return dst


def _probe_duration(p: Path) -> float | None:
    """Container duration in seconds, or None if ffprobe can't say."""
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, **_pu.SUBPROCESS_FLAGS)
        return float(out.stdout.strip())
    except Exception:
        return None


def _assemble_chunks_streamcopy(edl: EDL, chunk_paths: list[Path],
                                dst: Path, *, fps=None) -> Path:
    """Assemble cached chunks: PICTURE by the concat demuxer with `-c:v copy`,
    SOUND decoded from each chunk, cut to its exact sample count and
    re-encoded once.

    Only valid when the timeline is exactly the chunk sequence (the caller
    guarantees no transitions/PIP/overlays/masks/gaps). Chunks share codec,
    resolution and encoder args by construction (fingerprint_clip includes
    them), which is what makes packet-level video concat safe.

    QA-030 — why the audio is NOT packet-copied any more. Every chunk's AAC
    carries encoder priming and a padded final frame, and its audio stream is
    a little longer than its picture. The concat demuxer offsets each file by
    the file's own (audio-inflated) duration and copies the priming through,
    so every join pushed the sound ~21-32 ms later against the picture:
    282 ms after 10 cuts, while the export (a re-encode) stayed in sync.
    Now each file's duration is PINNED in the concat list to its exact frame
    count, and the audio comes from decoding each chunk on its own (where the
    mp4 edit list strips the priming), `apad`+`atrim` to exactly its frames'
    worth of samples, joined with the concat FILTER and encoded once — the
    cost is an AAC encode of the timeline's audio, not a video re-encode.
    """
    fps = edl.canvas.fps if fps is None else fps
    clips = _video_clips(edl)
    all_frames = [clip_frames(c, fps) for c in clips]
    # One chunk per clip by construction; if that ever does not hold, pin
    # nothing per file (0 = unknown) and let the length check below decide.
    frames = all_frames if len(all_frames) == len(chunk_paths) else [0] * len(chunk_paths)
    tmp = _part_path(dst)
    list_path = tmp.with_suffix(".concat.txt")
    lines = []
    for p, n in zip(chunk_paths, frames):
        # concat-demuxer list syntax: single-quoted path with embedded quotes
        # close-escape-reopened; forward slashes keep Windows paths intact.
        esc = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{esc}'")
        # `inpoint 0`: without it the demuxer takes the file's start from its
        # EARLIEST packet, and the AAC priming packet sits before zero — so
        # every chunk's picture came out ~21 ms late against its own sound.
        lines.append("inpoint 0")
        if n > 0:
            # The picture's exact length: the next file starts here, not where
            # this file's longer AAC stream happens to end.
            lines.append(f"duration {_tb.time_of(n, fps):.6f}")
    _pu.write_text_utf8(list_path, "\n".join(lines) + "\n")
    try:
        args = [_pu.FFMPEG, "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_path)]
        a_parts: list[str] = []
        a_labels: list[str] = []
        for k, (p, n) in enumerate(zip(chunk_paths, frames)):
            # -vn: this input is only here for its sound; don't demux the
            # picture a second time (the concat input copies it).
            args += ["-vn", "-i", str(p)]
            lbl = f"[ca{k}]"
            chain = f"[{k + 1}:a]"
            if n > 0:
                m = _tb.samples_for_frames(n, fps)
                chain += f"apad=whole_len={m},atrim=end_sample={m}"
            else:
                chain += "anull"
            a_parts.append(chain + lbl)
            a_labels.append(lbl)
        if len(a_labels) == 1:
            a_parts.append(f"{a_labels[0]}anull[amain0]")
        else:
            a_parts.append("".join(a_labels)
                           + f"concat=n={len(a_labels)}:v=0:a=1[amain0]")
        main_label = "[amain0]"
        # Track-level v1 mute — mirror of the main render path (audio-only).
        v1_track = edl.get_track("v1")
        if v1_track is not None and v1_track.muted:
            a_parts.append(f"{main_label}volume=0[amain]")
            main_label = "[amain]"
        audio_chain, audio_inputs, final_audio_label = build_audio_mix(
            edl, main_audio_label=main_label, first_input_index=1 + len(chunk_paths),
            apply_loudnorm=False,
        )
        args += audio_inputs
        fc_all = ";".join(x for x in (";".join(a_parts), audio_chain) if x)
        final_lbl = final_audio_label if audio_chain else main_label
        args += ["-filter_complex", fc_all,
                 "-map", "0:v", "-map", final_lbl,
                 # The one encode this path pays for — the fastest AAC
                 # encoder that works here (a preview, never the export).
                 "-c:v", "copy", *_preview_aac_out(),
                 "-movflags", "+faststart", str(tmp)]
        try:
            proc = _cancel.run(args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", **_pu.SUBPROCESS_FLAGS)
        except BaseException:
            _pu.unlink_with_retry(tmp)
            raise
        if proc.returncode != 0:
            _pu.unlink_with_retry(tmp)
            raise RuntimeError(
                f"streamcopy assembly failed (rc={proc.returncode}):\n"
                f"{proc.stderr[-1500:]}")
        # Verify the packet-copy actually produced the whole timeline before
        # publishing it. This path is a pure optimisation whose caller already
        # falls back to the re-encode on ANY exception, so a cheap correctness
        # check costs one ffprobe and removes a whole class of silent
        # wrong-output bugs. It earns its keep: on Windows CI, a timeline whose
        # clips share one source file (identical chunk fingerprint → the same
        # chunk listed twice) concatenated to a single clip's length — 4s for
        # an 8s timeline — with rc=0 and no warning. Rather than depend on a
        # platform's concat-demuxer quirk, assert the invariant and re-encode
        # when it doesn't hold. Callers reach here only when the timeline has
        # no gaps, so the frame plan IS the expected v1 extent.
        expected = _tb.time_of(sum(all_frames), fps) if all_frames else float(edl.duration)
        if expected > 0:
            got = _probe_duration(tmp)
            if got is not None and abs(got - expected) > 0.5:
                _pu.unlink_with_retry(tmp)
                raise RuntimeError(
                    f"streamcopy assembly produced {got:.2f}s for a "
                    f"{expected:.2f}s timeline — falling back to re-encode")
        _pu.replace_with_retry(tmp, dst)
    finally:
        _pu.unlink_with_retry(list_path)
    return dst


def _video_only_fingerprint(edl: EDL) -> str:
    """Hash everything that affects the visual frame (V1 + V2/PIP + text +
    stickers + transitions + canvas + brand kit) but NOT music/vo/captions
    audio gain. Used to cache the encoded video so audio-only edits skip the
    video re-encode."""
    import hashlib, json
    from ..edl.schema import RENDER_BEHAVIOR_VERSION
    from .chunks import file_identity as _file_identity
    tracks = []
    for t in edl.tracks:
        if t.type not in ("video", "text", "sticker"):  # video covers v1 AND v2
            continue
        d = t.model_dump(by_alias=True, mode="json")
        # Per-clip audio (gain/fade/mute) never changes pixels; leaving it in
        # forced a full video re-encode on every volume edit instead of the
        # cheap remux. Track-level `muted` stays for V2/text/sticker (it hides
        # the overlay) but is stripped for v1, where mute is audio-only.
        for c in d.get("clips", []):
            c.pop("audio", None)
            # What is ON DISK, not just the path (QA-001): a file replaced at
            # the same path must not be remuxed from a stale cached video.
            if c.get("src"):
                c["_file"] = _file_identity(c["src"])
        if t.id == "v1":
            d.pop("muted", None)
        # Solo is audio-only (QA-086): it must take the cheap remux, not a
        # video re-encode.
        d.pop("solo", None)
        tracks.append(d)
    blob = {
        # See EDL.hash()'s docstring: a version salt so a pre-fix cached
        # video-only mp4 (e.g. rendered before the LUT-blend or animated-
        # overlay-timing fixes) can't be remuxed as if still correct.
        "v": RENDER_BEHAVIOR_VERSION,
        "canvas": edl.canvas.model_dump(),
        "brand": edl.brand_kit.model_dump() if edl.brand_kit else None,
        "tracks": tracks,
        # The timeline EXTENT is a video input now: v1 pads with black out to
        # edl.duration (`_v1_segments`), and that duration is set by every
        # track — including the music/vo lanes deliberately excluded above.
        # Without this, trimming a 12s music bed to 6s would keep the cached
        # 12s video and remux it against 6s of audio, serving a file with 6s
        # of silent black welded on. Audio-only edits that DON'T move the
        # timeline end still take the cheap remux, which is the point of this
        # fingerprint.
        "dur": round(float(edl.duration), 3),
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


def render_preview(edl: EDL, session_dir: Path, *, height: int = 540,
                   fps: float | int | None = None) -> RenderResult:
    """Render a preview keyed by EDL hash, with an audio-only-remux fast path.

    `height` is the SHORT edge of the preview. On a portrait 9:16 canvas a
    literal output-height of 540 produces a 304x540 frame — visibly soft in
    the editor's phone-shaped preview box. Treating 540 as the short edge
    gives 540x960 portrait / 960x540 landscape: crisp at retina box sizes,
    still ~3x fewer pixels than full canvas.

    Strategy:
      1. Full hash hit → return cached preview (instant).
      2. Else compute video-only fingerprint. If a cached video at that fp
         exists, ffmpeg-mux it against the new audio mix (`-c:v copy`). This
         skips the expensive video re-encode when only music/vo changed.
      3. Else do the full render and cache by both video-fp and full hash.
    """
    # The project timebase, not a hardcoded 30 (QA-002): at 30 a 25 fps
    # project previewed resampled frames the export never shows, and the
    # preview's frame at 8.0 s disagreed with the export's.
    if fps is None:
        fps = edl.canvas.fps
    canvas = edl.canvas
    if canvas.h > canvas.w:
        # Portrait: short edge is the WIDTH → scale height so width ≈ `height`.
        height = min(canvas.h, int(round(height * canvas.h / max(1, canvas.w))))
    else:
        height = min(canvas.h, height)
    # Snap to even HERE too, not only in _render. This value is baked into the
    # video-only cache filename below (`video_{fp}_{height}.mp4`), so without the
    # snap a 4:5 project would name its cache entry 675 while the file inside is
    # 674 — a cache key describing a height that never existed. Identity for
    # every preset that works today (960, 540, 540).
    height = max(2, int(height) // 2 * 2)
    h = edl.hash()
    out_dir = session_dir / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        _cache_budget.touch(dst)          # LRU recency (QA-106)
        return RenderResult(path=dst, cached=True, edl_hash=h)

    key = f"{session_dir.name}/{h}"
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(key)
        if existing is None:
            event = threading.Event()
            _INFLIGHT[key] = event
            owner = True
        else:
            event = existing
            owner = False

    if not owner:
        _cancel.wait(event, timeout=120)
        if dst.exists() and dst.stat().st_size > 0:
            return RenderResult(path=dst, cached=True, edl_hash=h)

    try:
        _cancel.check()
        # This render's working set (everything it uses from here on) is
        # never evicted by its own budget pass (QA-106).
        render_started = time.time()
        # Audio-only-remux fast path
        cache_dir = session_dir / "cache"
        videos_dir = cache_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        video_fp = _video_only_fingerprint(edl)
        cached_video = videos_dir / f"video_{video_fp}_{height}.mp4"

        # Same chunk_is_valid check we use for per-clip chunks: rejects
        # mp4s left over from killed renders that lack a moov atom.
        from .chunks import chunk_is_valid as _valid
        if _valid(cached_video):
            # Just remux the cached video against a freshly-rendered audio mix.
            _cache_budget.touch(cached_video)
            try:
                # QA-082: loudness-matched to the export (render/preview_loudness).
                from . import preview_loudness as _pl
                with _pl.matched(edl, session_dir, dst):
                    _remux_with_new_audio(edl, cached_video, dst, fps=fps,
                                          cache_dir=cache_dir)
                _cache_budget.enforce(session_dir, protect=(dst, cached_video),
                                      since=render_started)
                return RenderResult(path=dst, cached=False, edl_hash=h)
            except _cancel.RenderCancelled:
                raise
            except Exception:
                # Remux failed → fall through to full render
                pass

        from . import preview_loudness as _pl
        with _pl.matched(edl, session_dir, dst):
            _render(edl, dst, height=height, fps=fps, preview=True,
                    cache_dir=cache_dir)
        # Also cache the video-only version (extract from the just-rendered
        # full preview — `-c:v copy -an` is essentially free).
        try:
            subprocess.run(
                [_pu.FFMPEG, "-y", "-i", str(dst), "-c:v", "copy", "-an",
                 "-movflags", "+faststart", str(cached_video)],
                capture_output=True, check=True,
                **_pu.SUBPROCESS_FLAGS,
            )
        except Exception:
            pass

        files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        for old in files[:-10]:
            # Never delete the render we just produced. A long editing session
            # can push the file the <video> is currently range-streaming out of
            # the 10-newest window; the browser then gets a mid-playback read
            # error and reloads from 0. `dst` is by definition the one in use.
            if old == dst:
                continue
            _pu.unlink_with_retry(old)
        # Cap the video-only cache too
        vfiles = sorted(videos_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        for old in vfiles[:-15]:
            _pu.unlink_with_retry(old)
        # The counts above never bounded BYTES (QA-106): trim every render
        # cache of this project, then the whole workdir, to their budgets.
        _cache_budget.enforce(session_dir, protect=(dst, cached_video),
                              since=render_started)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
        event.set()
    return RenderResult(path=dst, cached=False, edl_hash=h)


def _assemble_v1_audio(fc_parts: list[str], clips: list[Clip], a_labels: list[str],
                       *, total_duration: float, seams: clock.SeamTable,
                       out_label: str, fps=None) -> None:
    """Audio-only twin of the timeline assembly in `_build_filter_complex`:
    the per-clip audio streams in `a_labels`, walked over `_v1_segments` (so
    gaps become silent filler exactly as the video got black filler) and
    joined with `acrossfade` at every seam in `seams`, plain concat elsewhere.
    Appends to `fc_parts` and ends on `out_label`.

    The seams come from the render clock's table rather than from re-matching
    `Transition` records here: the audio this produces is remuxed against a
    VIDEO the main path already cross-faded, and the table is the one
    statement of which seams that was and by how much. The main path reads
    the same table (`_build_filter_complex`), so the cached video and this
    audio cannot disagree — they did while the main path kept its own
    matcher (a legacy zero-duration record xfaded there and was uncounted
    here, so a music-only edit remuxed plain-concat audio onto a cross-faded
    picture). `Transition.duration` is now normalised in the schema, so no
    record carries a number the renderer will not use.

    `total_duration` is the LAYOUT end, as in `_build_filter_complex`.
    """
    # Same frame plan as the picture it will be muxed onto (QA-002): the
    # cached video was assembled from exactly these segment lengths.
    fps = 30 if fps is None else fps
    segments = _v1_frame_plan(clips, total_duration, fps)
    seg_a: list[str] = []
    seg_dur: list[float] = []
    seg_of_clip: dict[int, int] = {}
    for kind, ci, nfr in segments:
        if kind == "clip":
            seg_of_clip[ci] = len(seg_a)
            seg_a.append(a_labels[ci])
            seg_dur.append(_tb.time_of(nfr, fps))
        else:
            g = _tb.time_of(nfr, fps)
            ag = f"[ragap{len(seg_a)}]"
            _vf, af = _gap_filler_chains(2, 2, fps, nfr)
            fc_parts.append(af + ag)
            seg_a.append(ag)
            seg_dur.append(g)
    if not seg_a:
        return
    # Seam → the segment index of its LEFT clip; only adjacent clip segments
    # can carry one (the table already excludes gapped boundaries, matched
    # here on the same boundary it was built from).
    seg_fade: dict[int, float] = {}
    for idx, c in enumerate(clips[:-1]):
        si = seg_of_clip.get(idx)
        if si is None or seg_of_clip.get(idx + 1) != si + 1:
            continue
        boundary = c.start + c.effective_duration
        cost = next((d for seam, d in seams if abs(seam - boundary) < 0.001), 0.0)
        if cost > 0.0:
            seg_fade[si] = cost
    if not seg_fade:
        if len(seg_a) == 1:
            fc_parts.append(f"{seg_a[0]}anull{out_label}")
        else:
            fc_parts.append("".join(seg_a) + f"concat=n={len(seg_a)}:v=0:a=1{out_label}")
        return
    cur = seg_a[0]
    for i in range(1, len(seg_a)):
        new = f"[rxa{i}]"
        d = seg_fade.get(i - 1)
        if d:
            fc_parts.append(f"{cur}{seg_a[i]}acrossfade=d={d:.6f}{new}")
        else:
            fc_parts.append(f"{cur}{seg_a[i]}concat=n=2:v=0:a=1{new}")
        cur = new
    fc_parts.append(f"{cur}anull{out_label}")


def _remux_with_new_audio(edl: EDL, video_only: Path, dst: Path,
                          *, fps: int, cache_dir: Path) -> None:
    """Take a cached video-only mp4 and mux a fresh audio mix onto it.

    The audio mix is built the same way the main renderer does it (V1 source
    audio + music ducking + voiceover) but only the audio is encoded.
    Video is `-c:v copy` so this is essentially I/O bound.

    Track solo (QA-086) is applied here too — a solo toggle is audio-only, so
    it arrives on this path.

    "The same way" includes the v1 ASSEMBLY: gap filler and an `acrossfade`
    at every seam the cached video was `xfade`d at (`_assemble_v1_audio`).
    This path used to plain-`concat` the per-clip audio, so on a timeline
    with transitions a music-only edit served a preview whose speech ran
    late by the accumulated overlap against a picture that did not — the
    render-clock drift, re-created on the fast path alone.
    """
    from .audio_mix import apply_solo
    edl = apply_solo(edl)
    from .reverse import with_reversed_sources      # QA-037, as _render_locked
    edl = with_reversed_sources(edl, cache_dir, fps)
    clips = _video_clips(edl)
    tmp = _part_path(dst)
    if not clips:
        # No V1 audio source to feed the mixer — copy video, generate silence.
        try:
            subprocess.run([
                _pu.FFMPEG, "-y", "-i", str(video_only),
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-c:v", "copy", *_AAC_OUT, "-shortest",
                "-movflags", "+faststart", str(tmp),
            ], capture_output=True, check=True, **_pu.SUBPROCESS_FLAGS)
        except Exception:
            _pu.unlink_with_retry(tmp)
            raise
        _pu.replace_with_retry(tmp, dst)  # atomic swap; retries on Windows if a reader holds dst
        return

    # Build per-clip audio chains with the same input order as the main render.
    inputs: list[str] = ["-i", str(video_only)]  # idx 0 = video-only file
    fc_parts: list[str] = []
    a_labels: list[str] = []
    for i, c in enumerate(clips):
        idx = i + 1  # +1 because video_only is input 0
        inputs += clip_input_args(c, fps)
        fc_parts.append(_build_clip_audio_chain(
            c, input_label=f"[{idx}:a]", label_out=f"[a{i}]", fps=fps,
        ))
        a_labels.append(f"[a{i}]")
    seams = clock.seam_table(edl)
    # Layout end, not `edl.duration` — the cached video was padded to the
    # layout end (see `_render_locked`), and audio a total overlap shorter
    # than its picture would truncate the file on `-shortest`.
    _assemble_v1_audio(fc_parts, clips, a_labels,
                       total_duration=max(0.0, edl.duration + sum(d for _s, d in seams)),
                       seams=seams, out_label="[aout]", fps=fps)

    # Track-level v1 mute — mirror of the main render path (audio-only).
    a_main = "[aout]"
    v1_track = edl.get_track("v1")
    if v1_track is not None and v1_track.muted:
        fc_parts.append(f"{a_main}volume=0[aout_m]")
        a_main = "[aout_m]"

    # Fold V2/PIP audio the same way the full render does (aresample +
    # per-clip gain/fade/mute + adelay + amix). Skipping this dropped every
    # PIP clip's audio from the remuxed preview.
    next_idx = 1 + len(clips)
    # Same placement rule as pip.py: render window, and a PIP the seams
    # consumed entirely is not an input at all.
    pip_clips = [(c, win) for _tid, c in collect_pip_clips(edl)
                 if (win := clock.render_window(seams, c.start, c.start + c.duration)) is not None]
    if pip_clips:
        pa_labels: list[str] = []
        for j, (c, (rs, re)) in enumerate(pip_clips):
            idx = next_idx + j
            # Same frame-exact span and chain as the full render's fold (a
            # PIP straddling a seam is cut to its on-screen frames there too).
            inputs += pip_input_args(c, pip_frames(rs, re, fps)[1], fps)
            pa_label = f"[rpa{j}]"
            fc_parts.append(pip_audio_chain(c, f"[{idx}:a]", pa_label,
                                            rs=rs, re=re, fps=fps))
            pa_labels.append(pa_label)
        fc_parts.append(
            f"{a_main}{''.join(pa_labels)}amix=inputs={1 + len(pa_labels)}"
            f":duration=first:dropout_transition=0:normalize=0[a_with_pip]"
        )
        a_main = "[a_with_pip]"
        next_idx += len(pip_clips)

    # Mix in music + vo on top of the folded main audio. _remux_with_new_audio
    # is only called from the preview fast-path, so loudnorm stays off here.
    audio_chain, audio_inputs, final_audio_label = build_audio_mix(
        edl, main_audio_label=a_main, first_input_index=next_idx,
        apply_loudnorm=False,
    )
    fc = ";".join(fc_parts)
    if audio_chain:
        fc = fc + ";" + audio_chain

    args = [_pu.FFMPEG, "-y", *inputs, *audio_inputs,
            "-filter_complex", fc,
            "-map", "0:v", "-map", final_audio_label,
            "-c:v", "copy",
            # This path's only encode (preview-only, like the streamcopy
            # assembly): the fastest working AAC coder, not the native one —
            # 10.4 s -> ~5 s for a volume edit on a 12-min timeline (QA-005).
            *_preview_aac_out(),
            "-r", _tb.ffmpeg_rate(fps),
            "-movflags", "+faststart",
            str(tmp)]
    try:
        proc = _cancel.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **_pu.SUBPROCESS_FLAGS)
    except BaseException:
        _pu.unlink_with_retry(tmp)
        raise
    if proc.returncode != 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(f"audio remux failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
    _pu.replace_with_retry(tmp, dst)  # atomic swap; retries on Windows if a reader holds dst


#: Characters an export file name may not carry: path separators and the
#: characters Windows, the concat demuxer, a URL or a glob would misread.
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|#%\[\]{}]+')
#: Budget for the project-name part of an export file name, in UTF-8 bytes
#: (APFS and ext4 cap a whole name at 255 bytes; Devanagari is 3 per letter).
_EXPORT_NAME_MAX_BYTES = 150


def export_filename(project_name: str | None, w: int, h: int, fps, *, crf: int,
                    bitrate_kbps: int | None, ext: str) -> str:
    """The file an export writes: the PROJECT's name plus the settings that
    make this file different from another export of it (QA-098).

    Every export of a timeline used to be `export_<edl hash>.mp4`, whatever its
    resolution, rate or quality: a 480p export overwrote the 1080p one, two
    concurrent requests at different sizes handed both callers the same file
    (the 1080p requester received 854x480), and the name the user saved was a
    hash. Now e.g. `Trip to Goa 1920x1080 30fps q18.mp4` or
    `Reel 1080x1920 29.97fps 8000kbps.mov`. The same project exported again at
    the same settings replaces its previous file — that IS the same export.
    """
    raw = _UNSAFE_NAME_CHARS.sub(" ", str(project_name or ""))
    base = " ".join(raw.split()).strip(" .")
    while len(base.encode("utf-8")) > _EXPORT_NAME_MAX_BYTES:
        base = base[:-1]
    base = base.rstrip(" .") or "Export"
    rate = f"{_tb.fps_float(fps):.3f}".rstrip("0").rstrip(".")
    quality = f"{int(bitrate_kbps)}kbps" if bitrate_kbps else f"q{int(crf)}"
    return f"{base} {int(w)}x{int(h)} {rate}fps {quality}.{ext}"


#: One lock per export destination: two requests that resolve to the same
#: file render one after the other instead of racing their `.part` swaps.
_EXPORT_LOCKS: dict[str, threading.Lock] = {}
_EXPORT_LOCKS_GUARD = threading.Lock()


def _export_lock(dst: Path) -> threading.Lock:
    key = str(dst.resolve())
    with _EXPORT_LOCKS_GUARD:
        lock = _EXPORT_LOCKS.get(key)
        if lock is None:
            lock = _EXPORT_LOCKS[key] = threading.Lock()
        return lock


def session_render_in_flight(session_dir: Path) -> bool:
    """True while a preview or an export of this project is rendering — the
    renders whose resolved-but-not-yet-opened cache paths "Clear render
    cache" must not delete (REV-B5-CLEAR-CACHE)."""
    name = Path(session_dir).name
    with _INFLIGHT_LOCK:
        if any(k.startswith(f"{name}/") for k in _INFLIGHT):
            return True
    prefix = str((Path(session_dir) / "exports").resolve())
    with _EXPORT_LOCKS_GUARD:
        return any(k.startswith(prefix) and lock.locked() for k, lock in _EXPORT_LOCKS.items())


def render_export(edl: EDL, session_dir: Path, *, height: int | None = None,
                  fps: int | None = None, crf: int = 18, preset: str = "medium",
                  container: str = "mp4", filename: str | None = None,
                  on_progress=None, cancel_event=None,
                  bitrate_kbps: int | None = None,
                  project_name: str | None = None) -> RenderResult:
    """Final export at canvas resolution (or override) with higher quality.

    `height` is a NAMED resolution — "1080p" — and names the SHORT side for
    the canvas orientation (QA-025), exactly like `render_preview`'s `height`.
    It used to be applied as the literal output height, so "1080p" on a 9:16
    project exported 608x1080 and "2160p" 1216x2160 — sub-spec for every
    vertical platform. Now 1080p is 1080x1920 portrait, 1920x1080 landscape,
    1080x1080 square, 1080x1350 for 4:5. See `export_dimensions`.

    `bitrate_kbps`: None = the platform target the project carries
    (`canvas.bitrate_kbps`, set by `apply_export_preset`); 0 = no target
    (quality mode, `crf`); >0 = that target. A target is a delivery spec for
    the CANVAS size, so an export rendered SMALLER than the canvas scales it by
    the pixel ratio (a 720p render of a 1080p/8000 kbps preset gets ~3556) —
    never above the preset.

    `container` selects the output file extension ("mp4" or "mov"). Both are
    QuickTime/ISO-BMFF-family containers muxed by ffmpeg's same `mov` muxer
    family with identical H.264/AAC encoder args and `-movflags +faststart` —
    ffmpeg infers the muxer from the destination filename's extension, so no
    codec/arg branching is needed, only the output name changes. Unknown
    values fall back to "mp4" (defensive; the API layer already validates
    against Literal["mp4","mov"]).

    `on_progress(p)` (0..1) and `cancel_event` (threading.Event) let a background
    job stream progress and abort the underlying ffmpeg mid-render.

    `project_name` names the file (`export_filename`, QA-098); `filename`
    overrides it outright.
    """
    h = edl.hash()
    out_dir = session_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = container if container in ("mp4", "mov") else "mp4"
    canvas = edl.canvas
    w_out, h_out = export_dimensions(canvas.w, canvas.h, height)
    f_out = fps or edl.canvas.fps
    target = canvas.bitrate_kbps if bitrate_kbps is None else (bitrate_kbps or None)
    if target:
        ratio = (w_out * h_out) / max(1, canvas.w * canvas.h)
        target = max(1, int(round(target * min(1.0, ratio))))
    name = filename or export_filename(project_name, w_out, h_out, f_out, crf=crf,
                                       bitrate_kbps=target, ext=ext)
    dst = out_dir / name
    lock = _export_lock(dst)
    while not lock.acquire(timeout=0.1):
        if cancel_event is not None and cancel_event.is_set():
            from ..api.jobs import JobCancelled
            raise JobCancelled()
    try:
        res = _render_export_to(edl, dst, h, height=h_out, fps=f_out, crf=crf,
                                target=target, session_dir=session_dir,
                                on_progress=on_progress, cancel_event=cancel_event)
    finally:
        lock.release()
    # A many-clip export builds chunks (QA-097); keep the caches in budget.
    _cache_budget.enforce(session_dir)
    return res


def _render_export_to(edl: EDL, dst: Path, h: str, *, height: int, fps, crf: int,
                      target: int | None, session_dir: Path, on_progress,
                      cancel_event) -> RenderResult:
    h_out, f_out = height, fps
    # VideoToolbox starves easy footage under a peak cap, so a platform
    # target is encoded uncapped first and re-encoded capped only when the
    # file overshoots (see `_target_bitrate_args`).
    vt_target = bool(target) and _video_encoder_args(
        preview=False, bitrate_kbps=target)[1] == "h264_videotoolbox"
    seen = [0.0]

    def report(p: float) -> None:
        seen[0] = max(seen[0], p)
        on_progress(p)
    # One pass unless there are more clips than one ffmpeg should hold open
    # decoders for (QA-097, see the chunk stage in `_render_locked`).
    chunked = len(_video_clips(edl)) > _EXPORT_SINGLE_PASS_MAX_CLIPS
    _render(edl, dst, height=h_out, fps=f_out, preview=False,
            cache_dir=session_dir / "cache",
            on_progress=report if on_progress is not None else None,
            cancel_event=cancel_event, crf=crf,
            bitrate_kbps=target, bitrate_peak_cap=not vt_target, chunked=chunked)
    if vt_target and (_video_kbps(dst) or 0) > target * _BITRATE_TOLERANCE:
        # The first pass already reported ~100%; hold the bar there through
        # the capped pass instead of running it backwards.
        hold = (lambda _p: on_progress(max(0.99, seen[0]))) if on_progress is not None else None
        _render(edl, dst, height=h_out, fps=f_out, preview=False,
                cache_dir=session_dir / "cache",
                on_progress=hold, cancel_event=cancel_event, crf=crf,
                bitrate_kbps=target, bitrate_peak_cap=True, chunked=chunked)
    return RenderResult(path=dst, cached=False, edl_hash=h)


#: How far over a platform bitrate target an uncapped VideoToolbox export may
#: land before it is re-encoded with the peak cap.
_BITRATE_TOLERANCE = 1.15


def _video_kbps(path: Path) -> float | None:
    """The encoded video stream's average bitrate in kb/s (ffprobe), or None."""
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=bit_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, **_pu.SUBPROCESS_FLAGS)
        return int(out.stdout.strip().splitlines()[0]) / 1000
    except Exception:
        return None


def export_dimensions(canvas_w: int, canvas_h: int, short_side: int | None) -> tuple[int, int]:
    """(w, h) an export renders at for a named resolution (QA-025).

    `short_side` None = the canvas itself. Otherwise the named size is the
    canvas's SHORT side and the long side follows the canvas aspect. Both are
    even, computed with the exact arithmetic `_render_locked` applies to the
    height it is handed (floor-even height, width rounded-even from it), so
    this function is what the file will measure — the frontend's option labels
    (`lib/exportOptions.ts`) mirror it.
    """
    if not short_side:
        h = int(canvas_h)
    elif canvas_h > canvas_w:
        h = int(round(int(short_side) * canvas_h / max(1, canvas_w)))
    else:
        h = int(short_side)
    h = max(2, h // 2 * 2)
    w = int(round(canvas_w * (h / canvas_h) / 2) * 2)
    return w, h
