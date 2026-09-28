"""The SOUND of a speed curve (Wave D, lane S1): a cached intermediate that
already follows the curve, which the clip's audio chain reads with `amovie`.

WHY AN INTERMEDIATE. ffmpeg has no time-varying stretcher. `asetrate` and
`atempo` take ONE rate; cutting the clip's sound into constant-speed pieces
restarts the resampler (or atempo's WSOLA, with its 20 ms lag of silence) at
every piece and clicks at every join, and driving `atempo` with timed
`asendcmd` changes lands each change wherever atempo's internal buffering
happens to be (tens of ms of source), which accumulates into audible drift
along a ramp. So the curve's sound is computed here, once, in numpy — the
render binary's only dependency that ships in the app (`build_app.sh`
excludes scipy) — from the EXACT map the picture follows:

    output sample j (clip-local)  →  source position
        src_start + 48000 · speed_curve.source_seconds(j / 48000)

which is the same closed-form integral whose inverse is the picture's
`setpts` (edl/speed_curve.py). Two modes, like a constant speed
(`AudioProps.keep_pitch`, audio_mix.speed_filters):

* varispeed (keep_pitch False): band-limited (Kaiser-windowed sinc)
  interpolation at the warped positions, the cutoff following the local
  speed so a 10x stretch does not alias. Pitch follows the speed like tape;
  continuous in position AND rate, so there is no join to click at.
* pitch kept (keep_pitch True, the default): WSOLA — Hann grains (21 ms at
  and above 1x, shorter when slower, `_grain`), each taken from the curve's
  source position for its centre ±5 ms where it best continues the previous
  grain, overlap-added and normalised by the summed window; above 2x the
  hop shrinks so every source sample is still covered. Measured on a click
  track: every transient within ±7 ms of the curve from 0.2x to 2x and
  ±10 ms at 0.1x (constant-speed atempo: ±20 ms,
  `audio_mix.KEEP_PITCH_MAX_OFFSET_MS`); above 2x a 4 ms click can be
  skipped, as with atempo. Varispeed: ±0.3 ms, every click, 0.1-10x.

The source samples are decoded exactly as the clip's 1x chain would feed its
speed stage (v1: `clip_input_args` + the pre-roll `atrim`; an audio lane: its
own `-ss in -to out`), so position 0 is the clip's first sample.

Files live in `<cache>/speed_audio/sa_<key>.wav` (a render cache, LRU-
bounded by render/cache_budget.py), keyed on the source's on-disk identity,
the range, the curve, the mode, the rate and the recipe; `prepare` builds
them at the top of every render entry point (next to the reversed-clip
intermediates) and `chain_source` names them in the graph.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .. import platformutil as _pu
from ..edl import speed_curve as _sc
from ..edl import timebase as _tb
from ..edl.schema import Clip
from . import cancel as _cancel

#: Bumped whenever the intermediate's recipe changes (part of the key).
#: sa-v5 (wave E, X1): the decode starts on S(in), cut in samples.
RECIPE = "sa-v5"
SAMPLE_RATE = 48000

#: WSOLA grain length (at >= 1x), its floor (slow speeds) and the waveform-
#: similarity search tolerance (samples @ 48 kHz); see `_grain`.
WSOLA_WIN = 1024
WSOLA_MIN_WIN = 128
WSOLA_TOL = 240
#: Coarse-search decimation of the WSOLA similarity search.
_WSOLA_DECIM = 4

#: Varispeed kernel: zero crossings each side at the cutoff, Kaiser beta,
#: and the cutoff margin below the (local) Nyquist.
_SINC_ZC = 12
_KAISER_BETA = 8.0
_CUTOFF = 0.97
_BLOCK = 2048

_REGISTRY: "OrderedDict[str, Path]" = OrderedDict()
_REGISTRY_MAX = 512
_LOCK = threading.Lock()
_BUILD_LOCKS: dict[str, threading.Lock] = {}


def has_curve(c: Clip) -> bool:
    return getattr(c, "freeze", None) is None and _sc.curve_points(c.speed) is not None


def _curve_map(c: Clip) -> _sc.CurveMap | None:
    pts = _sc.curve_points(c.speed)
    return _sc.curve_map(pts, c.duration) if pts is not None else None


def out_samples(c: Clip, fps) -> int:
    """Samples the intermediate holds: the clip's exact frame span on v1
    (`samples_for_frames(clip_frames)`), its effective duration on a lane."""
    if fps is not None:
        from .compositor import clip_frames
        return _tb.samples_for_frames(clip_frames(c, fps), fps)
    return max(1, int(math.ceil(c.effective_duration * SAMPLE_RATE)))


def key(c: Clip, fps) -> str:
    from .chunks import file_identity
    payload = {
        "r": RECIPE, "file": file_identity(c.src), "src": str(c.src),
        "in": float(c.in_), "out": float(c.out), "curve": c.speed,
        "pitch": bool(getattr(c.audio, "keep_pitch", True)),
        "fps": _tb.ffmpeg_rate(fps) if fps is not None else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _default_dir() -> Path:
    return Path(tempfile.gettempdir()) / "vae-speed-audio"


# ---------------------------------------------------------------- decode

def _decode(c: Clip, fps) -> np.ndarray:
    """(2, N) float32: the clip's sound from its first sample, as the 1x
    chain feeds its speed stage."""
    from .audio_mix import input_seek
    from .compositor import sound_input_args, source_has_audio
    if not source_has_audio(str(c.src)):
        return np.zeros((2, 0), dtype=np.float32)
    af = "aresample=async=1:first_pts=0,aformat=sample_fmts=flt:channel_layouts=stereo:sample_rates=48000"
    if fps is not None:
        # The 1x half-frame pre-roll (not the curve PICTURE's longer seek,
        # compositor.clip_input_args): the atrim below drops exactly it.
        # The cut is in SAMPLES, to S(in) (R9's one start rule,
        # `timebase.edit_sample`): the input opens on S(seek).
        from .audio_mix import head_trim
        args = sound_input_args(c, fps)
        seek = max(0.0, float(c.in_) - _tb.seek_preroll(c.in_, fps))
        af += head_trim(_tb.edit_sample(c.in_) - _tb.edit_sample(seek))
    else:
        args = [*input_seek(float(c.in_)), "-to", f"{float(c.out):.6f}", "-i", str(c.src)]
    cmd = [_pu.FFMPEG, "-v", "error", *args, "-vn", "-af", af, "-f", "f32le", "-"]
    proc = _cancel.run(cmd, capture_output=True, **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-1200:]
        raise RuntimeError(f"speed-curve audio decode failed (rc={proc.returncode}):\n{err}")
    x = np.frombuffer(proc.stdout, dtype=np.float32)
    x = x[: (len(x) // 2) * 2].reshape(-1, 2).T
    return np.ascontiguousarray(x)


# ---------------------------------------------------------------- the map

def positions(cm: _sc.CurveMap, n: int) -> tuple[np.ndarray, np.ndarray]:
    """(source position in samples, local speed) for output samples 0..n-1,
    from the curve's closed form (vectorised `source_seconds`/`speed_at`)."""
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    s = np.empty(n, dtype=np.float64)
    r = np.empty(n, dtype=np.float64)
    done = np.zeros(n, dtype=bool)
    for g in cm.segs:
        m = (~done) & (t < g.t1)
        tau = t[m] - g.t0
        s[m] = g.s0 + g.r * tau + (g.k / 4.0) * tau * tau
        r[m] = g.r + (g.k / 2.0) * tau
        done |= m
    m = ~done
    s[m] = cm.s_end + (t[m] - cm.D) * cm.r_end
    r[m] = cm.r_end
    return s * SAMPLE_RATE, np.maximum(r, 1e-6)


# ---------------------------------------------------------------- varispeed

def _kaiser(u: np.ndarray) -> np.ndarray:
    inside = np.abs(u) < 1.0
    w = np.zeros_like(u)
    w[inside] = np.i0(_KAISER_BETA * np.sqrt(1.0 - u[inside] ** 2)) / np.i0(_KAISER_BETA)
    return w


def varispeed(x: np.ndarray, pos: np.ndarray, rate: np.ndarray) -> np.ndarray:
    """Band-limited interpolation of ``x`` (channels, N) at fractional
    ``pos``; the low-pass cutoff is ``min(1, 1/rate)`` of Nyquist, per
    output sample, so speeding up never folds content down."""
    C, N = x.shape
    M = len(pos)
    y = np.zeros((C, M), dtype=np.float32)
    if N == 0 or M == 0:
        return y
    fc_all = np.minimum(1.0, 1.0 / rate) * _CUTOFF
    for b0 in range(0, M, _BLOCK):
        b1 = min(M, b0 + _BLOCK)
        p = pos[b0:b1]
        fc = fc_all[b0:b1]
        half = int(math.ceil(_SINC_ZC / float(fc.min()))) + 1
        base = np.floor(p).astype(np.int64)
        offs = np.arange(-half + 1, half + 1, dtype=np.int64)
        idx = base[:, None] + offs[None, :]
        d = idx.astype(np.float64) - p[:, None]
        width = (_SINC_ZC / fc)[:, None]
        h = fc[:, None] * np.sinc(fc[:, None] * d) * _kaiser(d / width)
        valid = (idx >= 0) & (idx < N)
        h = np.where(valid, h, 0.0)
        idc = np.clip(idx, 0, N - 1)
        for ch in range(C):
            y[ch, b0:b1] = np.einsum("ij,ij->i", h, x[ch][idc]).astype(np.float32)
    return y


# ---------------------------------------------------------------- WSOLA

def _segment(sig: np.ndarray, a: int, n: int) -> np.ndarray:
    """``sig[..., a:a+n]`` zero-padded outside the signal."""
    N = sig.shape[-1]
    out = np.zeros(sig.shape[:-1] + (n,), dtype=np.float32)
    lo, hi = max(0, a), min(N, a + n)
    if hi > lo:
        out[..., lo - a:hi - a] = sig[..., lo:hi]
    return out


def _grain(r: float) -> tuple[int, int]:
    """(grain length, synthesis hop) at local speed ``r``. Slower than 1x the
    grain shrinks with the speed: a transient inside a grain is replayed at
    1x, so it arrives up to ~L/3·(1/r − 1) early, and this keeps that under
    8 ms down to 0.2x and 10 ms at 0.1x (measured). The hop is half a grain (Hann at 50 % overlap): above
    2x the grains no longer cover every source sample, so — like atempo — a
    short transient can be skipped (the sound stays clean rather than an
    r-fold overlap smearing it)."""
    L = int(round(WSOLA_WIN * min(1.0, r))) // 2 * 2
    L = max(WSOLA_MIN_WIN, min(WSOLA_WIN, L))
    return L, L // 2


def wsola(x: np.ndarray, pos: np.ndarray, rate: np.ndarray | None = None) -> np.ndarray:
    """Pitch-kept time warp of ``x`` (channels, N) so that output sample j
    carries source position ``pos[j]``: Hann grains placed at the curve's
    source position for their centre, each shifted by at most
    ``WSOLA_TOL`` to continue the previous grain's waveform, overlap-added
    and normalised by the summed window (the grain length and hop follow
    the local speed, ``_grain``)."""
    C, N = x.shape
    M = len(pos)
    if rate is None:
        rate = np.gradient(pos) if M > 1 else np.ones(M)
    Lmax = WSOLA_WIN
    out = np.zeros((C, M + 2 * Lmax), dtype=np.float64)
    wsum = np.zeros(M + 2 * Lmax, dtype=np.float64)
    if N == 0 or M == 0:
        return np.zeros((C, M), dtype=np.float32)
    mono = x.mean(axis=0).astype(np.float32)
    ds = _WSOLA_DECIM
    slope_head = (pos[1] - pos[0]) if M > 1 else 1.0
    slope_tail = (pos[-1] - pos[-2]) if M > 1 else 1.0

    def pos_at(c: int) -> float:
        if c < 0:
            return float(pos[0] + c * slope_head)
        if c >= M:
            return float(pos[-1] + (c - M + 1) * slope_tail)
        return float(pos[c])

    prev_a = prev_o = None
    o = -Lmax // 2
    while o < M:
        r = float(rate[min(max(o + WSOLA_WIN // 4, 0), M - 1)])
        L, H = _grain(r)
        tol = min(WSOLA_TOL, L // 2)
        a = int(round(pos_at(o + L // 2))) - L // 2
        if prev_a is not None:
            # The previous grain's natural continuation at this output spot.
            tmpl = _segment(mono, prev_a + (o - prev_o), L)
            region = _segment(mono, a - tol, L + 2 * tol)
            co = np.correlate(region[::ds], tmpl[::ds], mode="valid")
            best = int(np.argmax(co)) * ds
            lo, hi = max(0, best - ds), min(2 * tol, best + ds)
            fine = [float(np.dot(region[j:j + L], tmpl)) for j in range(lo, hi + 1)]
            a = a - tol + lo + int(np.argmax(fine))
        win = 0.5 - 0.5 * np.cos(2 * np.pi * (np.arange(L) + 0.5) / L)
        lo_o = o + Lmax       # `out` is offset by Lmax so the first grains fit
        out[:, lo_o:lo_o + L] += _segment(x, a, L) * win[None, :]
        wsum[lo_o:lo_o + L] += win
        prev_a, prev_o = a, o
        o += H
    body = out[:, Lmax:Lmax + M]
    w = wsum[Lmax:Lmax + M]
    return (body / np.maximum(w, 1e-6)[None, :]).astype(np.float32)


# ---------------------------------------------------------------- files

def _write_wav_f32(path: Path, y: np.ndarray) -> None:
    """(2, M) float32 → IEEE-float stereo WAV at 48 kHz."""
    data = np.ascontiguousarray(y.T.astype("<f4")).tobytes()
    if len(data) > 0xFFFFFFFF - 64:        # RIFF sizes are 32-bit (~3.1 h here)
        raise RuntimeError("a speed-curve clip's sound is too long to render (over 3 hours)")
    C = y.shape[0]
    fmt = struct.pack("<HHIIHH", 3, C, SAMPLE_RATE, SAMPLE_RATE * 4 * C, 4 * C, 32)
    fact = struct.pack("<I", y.shape[1])
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<I", len(fact)) + fact
            + b"data" + struct.pack("<I", len(data)) + data)
    tmp = _pu.part_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", len(body)) + body)
        _pu.replace_with_retry(tmp, path)
    except BaseException:
        _pu.unlink_with_retry(tmp)
        raise


def render_curve_audio(c: Clip, fps) -> np.ndarray:
    """(2, M) float32: clip `c`'s sound following its speed curve."""
    cm = _curve_map(c)
    n = out_samples(c, fps)
    if cm is None:
        return np.zeros((2, n), dtype=np.float32)
    x = _decode(c, fps)
    pos, rate = positions(cm, n)
    if getattr(c.audio, "keep_pitch", True):
        return wsola(x, pos, rate)
    return varispeed(x, pos, rate)


def ensure(c: Clip, fps, cache_dir: Path | None = None) -> Path:
    """The cached intermediate for clip `c` (built if missing)."""
    k = key(c, fps)
    with _LOCK:
        hit = _REGISTRY.get(k)
        lock = _BUILD_LOCKS.setdefault(k, threading.Lock())
    if hit is not None and hit.exists() and cache_dir is None:
        return hit
    root = Path(cache_dir) / "speed_audio" if cache_dir is not None else _default_dir()
    dst = root / f"sa_{k}.wav"
    _cancel.acquire(lock)
    try:
        if not (dst.exists() and dst.stat().st_size > 44):
            if hit is not None and hit.exists() and cache_dir is None:
                return hit
            root.mkdir(parents=True, exist_ok=True)
            _write_wav_f32(dst, render_curve_audio(c, fps))
        else:
            from .cache_budget import touch
            touch(dst)
        with _LOCK:
            _REGISTRY[k] = dst
            _REGISTRY.move_to_end(k)
            while len(_REGISTRY) > _REGISTRY_MAX:
                _REGISTRY.popitem(last=False)
        return dst
    finally:
        lock.release()


def chain_source(c: Clip, fps) -> str:
    """`amovie=…,` reading clip `c`'s curve sound (a filter SOURCE, so the
    graph's input indices are untouched)."""
    path = ensure(c, fps)
    return f"amovie=filename={_pu.ffmpeg_filter_path(path)},"


def prepare(edl, cache_dir: Path | None, fps) -> None:
    """Build every speed-curve intermediate `edl` needs into `cache_dir`
    (v1 and PIP lanes at the render rate; music/vo/audio lanes at their own
    extent), so the graph only names files that exist. A no-op without curves."""
    for t in edl.tracks:
        if t.type not in ("video", "audio", "music", "vo"):
            continue
        for c in t.clips:
            if isinstance(c, Clip) and has_curve(c):
                if t.type == "video":
                    # v1 and the PIP lanes (their chains cut the sound to
                    # the same grid; pip.pip_audio_chain) at the render rate.
                    ensure(c, fps, cache_dir)
                elif t.type in ("audio", "music", "vo"):
                    ensure(c, None, cache_dir)


__all__ = ["RECIPE", "has_curve", "out_samples", "key", "positions", "varispeed",
           "wsola", "render_curve_audio", "ensure", "chain_source", "prepare"]
