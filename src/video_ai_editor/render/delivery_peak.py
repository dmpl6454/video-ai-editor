"""QA-121: hold a DELIVERED (AAC-encoded) export under the true-peak ceiling
without moving its loudness.

The export's 4x-oversampled limiter holds the PCM master at
`EXPORT_TP_LIMIT_DB`, but the AAC encoder re-adds inter-sample peak the
limiter could not see. On dense, limited music that overshoot is not spread
evenly: it is a handful of frames where one transient's quantisation noise
lands in phase. Measured on the QA-121 bed (noise hats at -14 LUFS), every
second of one encode peaked at -1.2..-1.8 dBTP except ONE 5 ms spot at
+0.6 dBTP — and which encode gets that spot is decided by -40 dB of
unrelated room noise elsewhere in the mix, so from the outside the export
looked nondeterministic (the same timeline with a different noise take
failed about 1 run in 20).

The fix-up used to answer that one spot by lowering the WHOLE programme's
ceiling by the full overshoot (1.75 dB there), which cut integrated loudness
by 0.7 LU and dropped a -14 export to -15.3 LUFS, outside its ±1 LU target;
and because every re-encode rolls the dice again, a lower ceiling did not
even reliably hold the peak (the click bed: -0.8 → -0.9 → -0.9 → 0.0 dBTP).

Now the delivered file is scanned at the meter's own 192 kHz, and only the
spots over the ceiling are dipped — a raised-cosine gain dip ±50 ms wide
(one AAC frame's MDCT window is 42.7 ms), exactly as deep as the spot
overshot — then re-encoded and measured again. Each pass re-encodes the
ORIGINAL delivered audio with every dip found so far (no generations stack);
a spot that is still over deepens its dip, a new one gets its own. A
programme with overshoot everywhere (more spots than `MAX_DIPS`) is not a
local problem and falls back to lowering the whole ceiling, as before.

Everything here is a pure function of the delivered file, so the same export
always gets the same fix-up (no randomness, no threading in the AAC coder).
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import numpy as np

from .. import platformutil as _pu
from . import cancel as _cancel

#: Re-encode attempts before keeping the best candidate.
FIX_PASSES = 4
#: Aim this far under the ceiling, so ebur128's 0.1 dB rounding cannot land a
#: corrected file exactly on it.
FIX_MARGIN_DB = 0.15
#: Half-width (s) of one dip: covers the AAC frame(s) — 1024-sample hop,
#: 2048-sample MDCT window, 42.7 ms at 48 kHz — whose coding made the spot.
DIP_HALF_S = 0.05
#: Resolution of the hot-spot scan (s).
SCAN_BLOCK_S = 0.005
#: More dips than this is overshoot everywhere, not a spot: lower the ceiling.
MAX_DIPS = 48
#: Samples per gain step of the dip envelope (1.3 ms at 48 kHz): a 2 dB dip
#: over 50 ms moves ~0.05 dB per step — no zipper noise.
_ENVELOPE_STEP = 64
#: The meter's own oversampling rate (ebur128 peak=true resamples to 192k).
_SCAN_RATE = 192000
_SCAN_CHUNK_S = 1.0

_log = logging.getLogger("video_ai_editor")
_I_RE = re.compile(r"I:\s+(-?[\d.]+|-inf) LUFS")
_TP_RE = re.compile(r"Peak:\s+(-?[\d.]+|-inf) dBFS")


def _db(v: str) -> float:
    return float("-inf") if v == "-inf" else float(v)


def delivered_loudness(path: Path) -> tuple[float, float] | None:
    """(integrated LUFS, true peak dBTP) of `path`'s first audio stream,
    measured the way a platform does (ebur128 peak=true); None if
    unmeasurable."""
    try:
        proc = _cancel.run_prioritised(
            [_pu.FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
             "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    err = proc.stderr or ""
    tail = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    i, tp = _I_RE.search(tail), _TP_RE.search(tail)
    if proc.returncode != 0 or i is None or tp is None:
        return None
    return _db(i.group(1)), _db(tp.group(1))


def block_peaks_db(path: Path, *, cancel_event=None) -> np.ndarray | None:
    """Per-`SCAN_BLOCK_S` true peak (dBTP, max over channels) of `path`'s
    first audio stream: decoded and resampled to 192 kHz the way ebur128's
    true-peak meter does it, streamed (a 10-min programme is ~0.9 GB of
    samples), so memory stays at one chunk. None if it cannot be decoded."""
    block = int(round(_SCAN_RATE * SCAN_BLOCK_S))
    chunk_bytes = int(_SCAN_RATE * _SCAN_CHUNK_S) // block * block * 2 * 4
    argv, kw = _cancel._prioritised(
        [_pu.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "2",
         "-af", f"aresample={_SCAN_RATE}", "-f", "f32le", "-"],
        {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL})
    try:
        proc = subprocess.Popen(argv, **{**_pu.SUBPROCESS_FLAGS, **kw})
    except OSError:
        return None
    peaks: list[np.ndarray] = []
    tail = b""
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            buf = proc.stdout.read(chunk_bytes)
            if not buf:
                break
            buf = tail + buf
            usable = len(buf) // 8 * 8
            tail = buf[usable:]
            x = np.abs(np.frombuffer(buf[:usable], np.float32).reshape(-1, 2)).max(axis=1)
            n = len(x) // block * block
            if n:
                peaks.append(x[:n].reshape(-1, block).max(axis=1))
            if n < len(x):             # only the stream's last read is ragged
                peaks.append(np.array([x[n:].max()], np.float32))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if proc.returncode != 0 or not peaks:
        return None
    lin = np.concatenate(peaks).astype(np.float64)
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(lin)


def hot_spots(peaks_db: np.ndarray, target_db: float) -> list[tuple[float, float]]:
    """(centre s, overshoot dB) of each run of scan blocks over `target_db`;
    blocks closer than a dip's half-width are one spot, centred on its
    loudest block."""
    over = np.flatnonzero(peaks_db > target_db)
    spots: list[tuple[float, float]] = []
    gap = max(1, int(round(DIP_HALF_S / SCAN_BLOCK_S)))
    start = 0
    for k in range(1, len(over) + 1):
        if k == len(over) or over[k] - over[k - 1] > gap:
            run = over[start:k]
            top = int(run[np.argmax(peaks_db[run])])
            spots.append(((top + 0.5) * SCAN_BLOCK_S, float(peaks_db[top] - target_db)))
            start = k
    return spots


def merge_dips(dips: list[tuple[float, float]], spots: list[tuple[float, float]]
               ) -> list[tuple[float, float]]:
    """A new list: each spot deepens the dip already within a half-width of
    it (its residual overshoot is how much deeper that dip must go), or adds
    a dip of its own."""
    out = list(dips)
    for t, over in spots:
        near = [k for k, (dt, _d) in enumerate(out) if abs(dt - t) < DIP_HALF_S]
        if near:
            k = min(near, key=lambda j: abs(out[j][0] - t))
            out[k] = (out[k][0], out[k][1] + over)
        else:
            out.append((t, over))
    return sorted(out)


def dip_filter(dips: list[tuple[float, float]]) -> str:
    """ffmpeg filters (no labels) applying `dips` as one smooth gain
    envelope: each is a raised-cosine dip of `depth` dB, ±`DIP_HALF_S`
    around its centre. "" for no dips."""
    if not dips:
        return ""
    h = DIP_HALF_S
    terms = "+".join(
        f"{d:.3f}*lt(abs(t-{t:.4f}),{h})*(1+cos(PI*(t-{t:.4f})/{h}))/2" for t, d in dips)
    return (f"asetnsamples=n={_ENVELOPE_STEP}:p=0,"
            f"volume=volume='pow(10,-({terms})/20)':eval=frame")


def _limiter(limit_db: float) -> str:
    lin = 10.0 ** (limit_db / 20.0)
    return (f"aresample={_SCAN_RATE},alimiter=limit={lin:.6f}:level=0:latency=1"
            f":attack=1:release=50,aresample=48000")


def _encode(dst: Path, cand: Path, af: str, aac_args: list[str]) -> bool:
    audio_only = dst.suffix.lower() == ".m4a"
    maps = ["-map", "0:a:0"] if audio_only else ["-map", "0:v?", "-map", "0:a:0", "-c:v", "copy"]
    proc = _cancel.run_prioritised(
        [_pu.FFMPEG, "-y", "-v", "error", "-i", str(dst), *maps, "-map_metadata", "0",
         "-af", af, *aac_args, "-movflags", "+faststart", str(cand)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    return proc.returncode == 0


def hold(dst: Path, *, ceiling_db: float, limit_db: float, aac_args: list[str],
         cancel_event=None) -> None:
    """Bring the delivered file `dst` under `ceiling_db` dBTP in place, if it
    is over, keeping its loudness (see the module docstring). `limit_db` is
    the master limiter's ceiling; `aac_args` the delivery encoder's args."""
    first = delivered_loudness(dst)
    if first is None or first[1] <= ceiling_db:
        return
    i0, tp0 = first
    target = ceiling_db - FIX_MARGIN_DB
    dips: list[tuple[float, float]] = []
    global_mode, seen_tp, scan = False, tp0, dst
    made: list[Path] = []
    best: tuple[tuple, Path, float] | None = None
    try:
        for n in range(FIX_PASSES):
            if cancel_event is not None and cancel_event.is_set():
                break
            if not global_mode:
                peaks = block_peaks_db(scan, cancel_event=cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    break
                spots = hot_spots(peaks, target) if peaks is not None else []
                merged = merge_dips(dips, spots)
                if spots and len(merged) <= MAX_DIPS:
                    dips = merged
                else:                  # overshoot everywhere, or unscannable
                    global_mode, dips = True, []
            if global_mode:
                # Lower the whole ceiling by exactly how far the encode overshot.
                limit_db -= seen_tp - ceiling_db + FIX_MARGIN_DB
            af = ",".join(f for f in (dip_filter(dips), _limiter(limit_db)) if f)
            cand = dst.with_name(f"{dst.stem}.tpfix{n}{dst.suffix}")
            made.append(cand)
            meas = delivered_loudness(cand) if _encode(dst, cand, af, aac_args) else None
            if meas is None:
                break
            i, seen_tp = meas
            passed = seen_tp <= ceiling_db
            # Under the ceiling beats over it; then the least loudness moved.
            key = (0, abs(i - i0)) if passed else (1, seen_tp)
            if best is None or key < best[0]:
                best = (key, cand, seen_tp)
            if passed:
                break
            scan = cand                # its residual spots deepen the dips
        if best is not None and best[2] < tp0:
            _pu.replace_with_retry(best[1], dst)
            _log.info("export true peak %.1f dBTP after the encode; %s, now %.1f dBTP",
                      tp0, "ceiling lowered" if global_mode else f"{len(dips)} spot(s) dipped",
                      best[2])
    finally:
        for cand in made:
            if cand.exists():
                _pu.unlink_with_retry(cand)
