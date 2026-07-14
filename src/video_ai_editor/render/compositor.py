"""Renderer — video tracks composited through ffmpeg filter_complex.

M1 capability:  cut/trim/concat/reorder on V1 + A1, scaled to canvas, hash-keyed
                preview cache, preview-vs-export quality split.
M2 addition:    text + captions tracks composited as PNG overlays.

GPU encoding: when VideoToolbox is available (Apple Silicon), preview + export
encode via h264_videotoolbox for ~5–10× the throughput of libx264.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import threading
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
from .pip import build_pip_overlay_chain
from ..edl.keyframes import is_keyframed, to_ffmpeg_expr


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
                            text=True, encoding="utf-8", errors="replace")
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
                             capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
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
        )
        return r.returncode == 0
    except Exception:
        return False


# Probe order: Apple HW first (only lists on Mac), then the three Windows/Linux
# HW encoders, then guaranteed software fallback.
_HW_ENCODER_ORDER = ["h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"]


def _video_encoder_args(*, preview: bool, crf: int | None = None) -> list[str]:
    """Pick the fastest usable H.264 encoder; fall back to libx264.

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
    for name in _HW_ENCODER_ORDER:
        if _usable_encoder(name):
            return _hw_encoder_args(name, preview=preview, crf=crf)
    default_crf = 30 if preview else 20
    crf_val = crf if crf is not None else default_crf
    preset = "ultrafast" if preview else "medium"
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf_val), "-pix_fmt", "yuv420p"]


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


def _build_clip_video_chain(c: Clip, *, input_label: str, label_out: str,
                            canvas_w: int, canvas_h: int) -> str:
    """Build the per-clip video filter chain (scale + transform + effects + speed),
    starting from `input_label` (e.g. "[0:v]") and ending at `label_out`.

    Used by both the monolithic renderer (where input_label = [N:v] for the
    Nth input) and the chunk renderer (where input_label = [0:v]).
    """
    v_chain = (f"{input_label}"
               f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
               f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    tx = c.transform
    rot_static = float(tx.rotation) if isinstance(tx.rotation, (int, float)) else 0.0
    sc_static = float(tx.scale) if isinstance(tx.scale, (int, float)) else 1.0
    rot_animated = is_keyframed(tx.rotation)
    sc_animated = is_keyframed(tx.scale)
    x_animated = is_keyframed(tx.x)
    y_animated = is_keyframed(tx.y)
    tvar = f"(t-{c.start:.4f})"

    if rot_animated:
        re = to_ffmpeg_expr(tx.rotation, time_var=tvar)
        re_rad = f"({re})*PI/180"
        v_chain += (f",rotate=a='{re_rad}':c=black:"
                    f"ow='rotw({re_rad})':oh='roth({re_rad})'")
        v_chain += (f",scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                    f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black")
    elif abs(rot_static) > 0.001:
        rad = rot_static * 3.14159265 / 180.0
        v_chain += f",rotate={rad}:c=black:ow=rotw({rad}):oh=roth({rad})"
        v_chain += (f",scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                    f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black")

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
    elif abs(sc_static - 1.0) > 0.001 and sc_static > 0:
        sw = max(2, int(canvas_w * sc_static) // 2 * 2)
        sh = max(2, int(canvas_h * sc_static) // 2 * 2)
        v_chain += f",scale={sw}:{sh}"
        if sc_static < 1.0:
            v_chain += f",pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        else:
            v_chain += f",crop={canvas_w}:{canvas_h}:(iw-{canvas_w})/2:(ih-{canvas_h})/2"

    ec = effect_chain(c.effects or [])
    if ec:
        v_chain += "," + ec

    # Chroma key (green/blue screen) — produces transparent regions; on V1 they
    # show through to canvas bg colour, on PiP they show through to the layer below.
    if getattr(c, "chromakey", None) is not None:
        v_chain += "," + build_chromakey_filter(c.chromakey)

    if isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0:
        v_chain += f",setpts=PTS/{float(c.speed)}"

    # Normalize sample aspect ratio at the end. rotate / scale-with-eval=frame
    # can produce SAR like 86519:86488 which makes concat fail with
    # "Input link parameters do not match" when neighbours have SAR 1:1.
    v_chain += ",setsar=1"
    v_chain += label_out
    return v_chain


def _build_clip_audio_chain(c: Clip, *, input_label: str, label_out: str) -> str:
    """Per-clip audio chain: resample + atempo for speed + gain/fade/mute.

    Gain/fade/mute were previously only applied to music + vo clips (via
    audio_mix._audio_clip_filter); V1 clips lost those properties silently
    on render. The fade is positioned relative to the clip's LOCAL audio
    (which is `-ss/-to`-trimmed and starts at t=0), since concat then
    sequences these clips into the timeline absolute time.
    """
    a_chain = (f"{input_label}aresample=async=1:first_pts=0,"
               f"aformat=channel_layouts=stereo:sample_rates=48000")
    if isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0:
        remaining = float(c.speed)
        while remaining > 2.0:
            a_chain += ",atempo=2.0"
            remaining /= 2.0
        while remaining < 0.5:
            a_chain += ",atempo=0.5"
            remaining /= 0.5
        if abs(remaining - 1.0) > 0.001:
            a_chain += f",atempo={remaining:.4f}"
    if c.audio:
        if abs(c.audio.gain_db) > 0.01:
            a_chain += f",volume={c.audio.gain_db:.2f}dB"
        if c.audio.fade_in > 0.001:
            # fade-in starts at local t=0 and runs for fade_in seconds.
            a_chain += f",afade=t=in:st=0:d={c.audio.fade_in:.3f}"
        if c.audio.fade_out > 0.001:
            # fade-out ends at clip duration; start at duration - fade_out.
            fade_out_start = max(0.0, c.duration - c.audio.fade_out)
            a_chain += (f",afade=t=out:st={fade_out_start:.3f}"
                        f":d={c.audio.fade_out:.3f}")
        if c.audio.mute:
            a_chain += ",volume=0"
    a_chain += label_out
    return a_chain


def _build_filter_complex(clips: list[Clip], canvas_w: int, canvas_h: int,
                          *, transitions: list | None = None,
                          cache_dir: Path | None = None,
                          chunk_paths: list[Path] | None = None,
                          ) -> tuple[str, list[str], list[str], list[str]]:
    """Build the video+audio filter chain for a list of V1 clips.

    Returns (filter_str, input_args, [v_label, a_label], extra_inputs_for_masks).
    Each clip is decoded with input-side seeking, scaled+padded to canvas,
    then runs through any per-clip effect chain, then mask alphamerge if a
    mask is set, then enters the timeline assembly (concat OR xfade).
    """
    inputs: list[str] = []
    extra_inputs: list[str] = []
    fc_parts: list[str] = []
    v_labels: list[str] = []
    a_labels: list[str] = []

    transitions = transitions or []
    # transitions are between adjacent clip joins, indexed by left clip's index
    trans_at: dict[int, tuple[str, float]] = {}
    if transitions and clips:
        # Build a map: clip i (0-based) → transition that bridges to i+1
        running = 0.0
        for idx, c in enumerate(clips[:-1]):
            running += c.duration
            for tr in transitions:
                if abs(tr.at - running) < 0.05:
                    trans_at[idx] = (tr.type, tr.duration)

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
            fc_parts.append(f"[{i}:a]anull[a{i}]")
            v_labels.append(f"[ve{i}]")
            a_labels.append(f"[a{i}]")
            continue

        inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        fc_parts.append(_build_clip_video_chain(
            c, input_label=f"[{i}:v]", label_out=f"[ve{i}]",
            canvas_w=canvas_w, canvas_h=canvas_h,
        ))
        if c.mask is not None and cache_dir is not None:
            mask_path = cache_dir / f"mask_{c.id}_{c.mask.type}_{int(c.mask.feather)}_{canvas_w}x{canvas_h}.png"
            if not mask_png_is_valid(mask_path):
                render_mask_png(c.mask, canvas_w, canvas_h, mask_path)
            pending_masks.append((i, mask_path))

        fc_parts.append(_build_clip_audio_chain(
            c, input_label=f"[{i}:a]", label_out=f"[a{i}]",
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
            f"color=c=black:s={canvas_w}x{canvas_h}:r=30[bg{clip_i}];"
            f"[bg{clip_i}][vmrgba{clip_i}]overlay=format=auto:shortest=1{v_masked}"
        )
        v_labels[clip_i] = v_masked

    if not clips:
        return "", [], [], []

    # ---- Timeline assembly ----
    if not trans_at:
        # Plain concat (interleaved [v0][a0][v1][a1]...)
        interleaved = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
        fc_parts.append(f"{interleaved}concat=n={len(clips)}:v=1:a=1[vout][aout]")
    else:
        # Chain xfade for video, acrossfade for audio between adjacent clips.
        cur_v = v_labels[0]
        cur_a = a_labels[0]
        cur_dur = clips[0].duration
        for i in range(1, len(clips)):
            tr_for_left = trans_at.get(i - 1)
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
                xf = f"xfade=transition={xf_name}:duration={tdur}:offset={offset:.3f}"
                if xf_expr:
                    # expr is wrapped in single quotes; it contains no quotes itself.
                    xf += f":expr='{xf_expr}'"
                fc_parts.append(
                    f"{cur_v}{v_labels[i]}{xf}{new_v}"
                )
                fc_parts.append(
                    f"{cur_a}{a_labels[i]}acrossfade=d={tdur}{new_a}"
                )
                cur_dur = cur_dur + clips[i].duration - tdur
            else:
                fc_parts.append(
                    f"{cur_v}{v_labels[i]}concat=n=2:v=1:a=0{new_v}"
                )
                fc_parts.append(
                    f"{cur_a}{a_labels[i]}concat=n=2:v=0:a=1{new_a}"
                )
                cur_dur += clips[i].duration
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


def _render(edl: EDL, dst: Path, *, height: int, fps: int, preview: bool,
            cache_dir: Path | None = None, on_progress=None,
            cancel_event=None, crf: int | None = None) -> Path:
    canvas = edl.canvas
    h_out = height
    w_out = int(round(canvas.w * (h_out / canvas.h) / 2) * 2)
    enc_args = _video_encoder_args(preview=preview, crf=crf)

    clips = _video_clips(edl)
    if not clips:
        dur = max(1.0, edl.duration)
        subprocess.run([
            _pu.FFMPEG, "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={w_out}x{h_out}:r={fps}:d={dur}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
            "-shortest", *enc_args, *_AAC_OUT, str(dst),
        ], check=True, capture_output=True)
        return dst

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
    chunk_paths: list[Path] | None = None
    if cache_dir is not None and not transitions:
        try:
            from .chunks import get_or_build_chunks
            chunk_paths = get_or_build_chunks(
                clips,
                cache_dir=cache_dir / "chunks",
                canvas_w=w_out, canvas_h=h_out, fps=fps,
                encoder_args=enc_args,
                build_video_chain=_build_clip_video_chain,
                build_audio_chain=_build_clip_audio_chain,
            )
        except Exception:
            # Cache miss / chunk render failure → fall back to monolithic.
            chunk_paths = None

    fc, inputs, labels, mask_inputs = _build_filter_complex(
        clips, w_out, h_out, transitions=transitions, cache_dir=cache_dir,
        chunk_paths=chunk_paths,
    )
    v_label = labels[0]
    a_label = labels[1]

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
    )
    if pip_chain:
        fc = fc + ";" + pip_chain
        v_label = pip_v_label
    extra_inputs += pip_inputs
    # Each PIP clip used 6 args (-ss v -to v -i path) → +1 index each.
    pip_inputs_count = len(pip_inputs) // 6
    next_idx += pip_inputs_count

    # Composite text overlay PNGs (rendered by Pillow) via ffmpeg overlay= filter.
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
            fc = fc + ";" + chain
            v_label = after_label
        extra_inputs += txt_inputs
        next_idx += len(txt_inputs) // 2  # each overlay PNG adds two args (-i path)

    # Fold V2 PiP audio into the V1 main audio before the music+vo mixer runs.
    # Each PIP clip's audio is positioned at its timeline start via adelay and
    # amix'd with the main concat audio.
    if pip_audio_clips:
        # PIP video inputs were added at indices [pre_pip..pre_pip+N-1] where
        # pre_pip = original next_idx BEFORE pip_inputs_count was added.
        pre_pip = next_idx - pip_inputs_count
        pa_parts: list[str] = []
        pa_labels: list[str] = []
        for j, c in enumerate(pip_audio_clips):
            input_idx = pre_pip + j
            delay_ms = max(0, int(round(c.start * 1000)))
            chain = (f"[{input_idx}:a]aresample=async=1:first_pts=0,"
                     f"aformat=channel_layouts=stereo:sample_rates=48000")
            if delay_ms > 0:
                chain += f",adelay=delays={delay_ms}|{delay_ms}:all=1"
            pa_label = f"[pa{j}]"
            chain += pa_label
            pa_parts.append(chain)
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
            "-r", str(fps),
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
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        rc, err = proc.returncode, proc.stderr
    if rc != 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(f"ffmpeg render failed (rc={rc}):\n{(err or '')[-2000:]}")
    _pu.replace_with_retry(tmp, dst)  # atomic swap; retries on Windows if a reader holds dst
    return dst


def _video_only_fingerprint(edl: EDL) -> str:
    """Hash everything that affects the visual frame (V1 + V2/PIP + text +
    stickers + transitions + canvas + brand kit) but NOT music/vo/captions
    audio gain. Used to cache the encoded video so audio-only edits skip the
    video re-encode."""
    import hashlib, json
    blob = {
        "canvas": edl.canvas.model_dump(),
        "brand": edl.brand_kit.model_dump() if edl.brand_kit else None,
        "tracks": [
            t.model_dump(by_alias=True, mode="json")
            for t in edl.tracks
            if t.type in ("video", "text", "sticker")  # video covers v1 AND v2
        ],
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


def render_preview(edl: EDL, session_dir: Path, *, height: int = 540, fps: int = 30) -> RenderResult:
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
    canvas = edl.canvas
    if canvas.h > canvas.w:
        # Portrait: short edge is the WIDTH → scale height so width ≈ `height`.
        height = min(canvas.h, int(round(height * canvas.h / max(1, canvas.w))))
    else:
        height = min(canvas.h, height)
    h = edl.hash()
    out_dir = session_dir / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
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
        event.wait(timeout=120)
        if dst.exists() and dst.stat().st_size > 0:
            return RenderResult(path=dst, cached=True, edl_hash=h)

    try:
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
            try:
                _remux_with_new_audio(edl, cached_video, dst, fps=fps,
                                      cache_dir=cache_dir)
                return RenderResult(path=dst, cached=False, edl_hash=h)
            except Exception:
                # Remux failed → fall through to full render
                pass

        _render(edl, dst, height=height, fps=fps, preview=True,
                cache_dir=cache_dir)
        # Also cache the video-only version (extract from the just-rendered
        # full preview — `-c:v copy -an` is essentially free).
        try:
            subprocess.run(
                [_pu.FFMPEG, "-y", "-i", str(dst), "-c:v", "copy", "-an",
                 "-movflags", "+faststart", str(cached_video)],
                capture_output=True, check=True,
            )
        except Exception:
            pass

        files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        for old in files[:-10]:
            _pu.unlink_with_retry(old)
        # Cap the video-only cache too
        vfiles = sorted(videos_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        for old in vfiles[:-15]:
            _pu.unlink_with_retry(old)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
        event.set()
    return RenderResult(path=dst, cached=False, edl_hash=h)


def _remux_with_new_audio(edl: EDL, video_only: Path, dst: Path,
                          *, fps: int, cache_dir: Path) -> None:
    """Take a cached video-only mp4 and mux a fresh audio mix onto it.

    The audio mix is built the same way the main renderer does it (V1 source
    audio + music ducking + voiceover) but only the audio is encoded.
    Video is `-c:v copy` so this is essentially I/O bound.
    """
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
            ], capture_output=True, check=True)
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
        inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        fc_parts.append(_build_clip_audio_chain(
            c, input_label=f"[{idx}:a]", label_out=f"[a{i}]"
        ))
        a_labels.append(f"[a{i}]")
    # Concat per-clip audio
    if len(a_labels) == 1:
        fc_parts.append(f"{a_labels[0]}anull[aout]")
    else:
        # Pair-wise concat with identical sample format
        fc_parts.append(
            "".join(a_labels) + f"concat=n={len(a_labels)}:v=0:a=1[aout]"
        )

    # Mix in music + vo on top of [aout]. _remux_with_new_audio is only called
    # from the preview fast-path, so loudnorm stays off here.
    next_idx = 1 + len(clips)
    audio_chain, audio_inputs, final_audio_label = build_audio_mix(
        edl, main_audio_label="[aout]", first_input_index=next_idx,
        apply_loudnorm=False,
    )
    fc = ";".join(fc_parts)
    if audio_chain:
        fc = fc + ";" + audio_chain

    args = [_pu.FFMPEG, "-y", *inputs, *audio_inputs,
            "-filter_complex", fc,
            "-map", "0:v", "-map", final_audio_label,
            "-c:v", "copy",
            *_AAC_OUT,
            "-r", str(fps),
            "-movflags", "+faststart",
            str(tmp)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(f"audio remux failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
    _pu.replace_with_retry(tmp, dst)  # atomic swap; retries on Windows if a reader holds dst


def render_export(edl: EDL, session_dir: Path, *, height: int | None = None,
                  fps: int | None = None, crf: int = 18, preset: str = "medium",
                  container: str = "mp4", filename: str | None = None,
                  on_progress=None, cancel_event=None) -> RenderResult:
    """Final export at canvas resolution (or override) with higher quality.

    `container` selects the output file extension ("mp4" or "mov"). Both are
    QuickTime/ISO-BMFF-family containers muxed by ffmpeg's same `mov` muxer
    family with identical H.264/AAC encoder args and `-movflags +faststart` —
    ffmpeg infers the muxer from the destination filename's extension, so no
    codec/arg branching is needed, only the output name changes. Unknown
    values fall back to "mp4" (defensive; the API layer already validates
    against Literal["mp4","mov"]).

    `on_progress(p)` (0..1) and `cancel_event` (threading.Event) let a background
    job stream progress and abort the underlying ffmpeg mid-render.
    """
    h = edl.hash()
    out_dir = session_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = container if container in ("mp4", "mov") else "mp4"
    name = filename or f"export_{h}.{ext}"
    dst = out_dir / name
    h_out = height or edl.canvas.h
    f_out = fps or edl.canvas.fps
    _render(edl, dst, height=h_out, fps=f_out, preview=False,
            cache_dir=session_dir / "cache",
            on_progress=on_progress, cancel_event=cancel_event, crf=crf)
    return RenderResult(path=dst, cached=False, edl_hash=h)
