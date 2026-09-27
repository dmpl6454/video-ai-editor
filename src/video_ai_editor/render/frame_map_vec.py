"""Exact integer and vectorised arithmetic for the program map
(``render/frame_map.py``, wave D3 lane E1b): the pieces its fast path is
built from, kept free of any frame-map type so both sides import them.

Every function here returns what the per-frame ``Fraction`` reading of the
ffmpeg chain returns (``tests/frame_map_reference.py``), bit for bit:
integer rounding is done in integers (``av_rescale_q``'s half away from
zero), doubles go through the same IEEE operations in the same order, and an
int64 path that could overflow falls back to Python integers.
"""
from __future__ import annotations

import array
import base64
from typing import Iterable, Sequence

import numpy as np

from ..edl import speed_curve as _sc


def _select_scan(slots: list[int], eof: int, f0: int, qlast: int, n: int) -> list[int]:
    """The fps filter's scan (the definition `select_frames` vectorises)."""
    out: list[int] = []
    q = 0
    for s in range(n):
        lim = min(s, eof - 1)
        while q < qlast and slots[q + 1] <= lim:
            q += 1
        out.append(f0 + q)
    return out


def _rescale_int(a: int, num: int, den: int) -> int:
    """``round_half_away(a · num / den)`` in integers (``den`` > 0)."""
    p = a * num
    if p >= 0:
        return (2 * p + den) // (2 * den)
    return -((-2 * p + den) // (2 * den))


#: Largest |value · multiplier| the int64 fast paths accept (2^62).
_I64_SAFE = 1 << 62


def _rescale_vec(a: np.ndarray, num: int, den: int, *, monotone: bool = True) -> np.ndarray:
    """``_rescale_int`` elementwise over an int64 array (``monotone``: known
    non-decreasing, so its ends are its extremes), exact: falls back to
    Python integers when a product could leave int64."""
    if not a.size:
        return a
    lo, hi = (int(a[0]), int(a[-1])) if monotone else (int(a.min()), int(a.max()))
    if 2 * max(-lo, hi) * num + den >= _I64_SAFE:
        return np.array([_rescale_int(int(v), num, den) for v in a], dtype=object)
    p = a * (2 * num)
    if lo >= 0:
        p += den
        p //= 2 * den
        return p
    return np.where(p >= 0, (p + den) // (2 * den), -((-p + den) // (2 * den)))


def _out_seconds_vec(cm: "_sc.CurveMap", T: np.ndarray) -> np.ndarray:
    """``speed_curve.out_seconds`` elementwise: the same IEEE operations in
    the same order (numpy's sqrt is correctly rounded, like C's)."""
    res = np.empty_like(T)
    done = T < 0
    if done.any():
        res[done] = T[done] / _sc.start_speed(cm)
    for g in cm.segs:
        m = ~done & (T < g.s1)
        if m.any():
            q = T[m] - g.s0
            res[m] = g.t0 + 2.0 * q / (g.r + np.sqrt(g.rr + g.k * q))
            done |= m
    m = ~done
    if m.any():
        res[m] = cm.D + (T[m] - cm.s_end) / cm.r_end
    return res


def _trunc_i64(x: np.ndarray) -> np.ndarray:
    """``int(x)`` elementwise (toward zero, like ffmpeg's ``D2TS``); Python
    integers when a value would not fit int64."""
    if x.size and float(np.abs(x).max()) >= _I64_SAFE:
        return np.array([int(v) for v in x], dtype=object)
    return np.trunc(x).astype(np.int64)


def _zigzag(v: int) -> int:
    return (v << 1) if v >= 0 else ((-v << 1) - 1)


def _varints(vals: Iterable[int]) -> str:
    buf = bytearray()
    for v in vals:
        z = _zigzag(v)
        while True:
            b = z & 0x7F
            z >>= 7
            if z:
                buf.append(b | 0x80)
            else:
                buf.append(b)
                break
    return base64.b64encode(bytes(buf)).decode("ascii")


def _decode_varints(text: str) -> list[int]:
    out, z, shift = [], 0, 0
    for b in base64.b64decode(text):
        z |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
            continue
        out.append((z >> 1) if not (z & 1) else -((z + 1) >> 1))
        z, shift = 0, 0
    return out


def _i64(vals: Sequence[int]) -> np.ndarray:
    """A list of ints as an int64 array (``array.array`` unboxes fastest)."""
    return np.frombuffer(array.array("q", vals), dtype=np.int64)


def _i8(vals: Sequence[bool]) -> np.ndarray:
    """A list of bools as an int8 array."""
    return np.frombuffer(array.array("b", vals), dtype=np.int8)


class _ArithIndex:
    """``_encode_seq_np`` of any slice of one int64 array, with the arithmetic
    test answered from a prefix count of delta changes."""

    def __init__(self, vals: np.ndarray):
        self.vals = vals
        self.d = np.diff(vals)
        ch = np.zeros(len(vals), dtype=np.int64)
        if len(self.d) > 1:
            ch[2:] = np.cumsum(self.d[1:] != self.d[:-1])
        # ch[i] = number of j in [1, i-1] with d[j] != d[j-1]
        self.ch = ch

    def encode(self, k: int, e: int) -> dict:
        vals = self.vals
        f0 = int(vals[k])
        if e - k == 1:
            return {"f0": f0, "step": 1}
        # deltas d[k .. e-2] constant  <=>  no change at j in [k+1, e-2]
        if self.ch[e - 1] - self.ch[k + 1] == 0:
            return {"f0": f0, "step": int(self.d[k])}
        return _encode_seq_np(vals[k:e])


def _encode_seq_np(vals: np.ndarray) -> dict:
    """A frame sequence as ``{"f0", "step"}`` when arithmetic with an integer
    step, else ``{"f0", "d"}`` — base64 zigzag varints of the deltas (the
    ``tests/frame_map_reference.py`` encoder, byte for byte)."""
    f0 = int(vals[0])
    if vals.size == 1:
        return {"f0": f0, "step": 1}
    d = np.diff(vals)
    step = int(d[0])
    if bool((d == step).all()):
        return {"f0": f0, "step": step}
    z = np.where(d >= 0, d << 1, ((-d) << 1) - 1)
    if int(z.max()) < 0x80:
        # Every zigzag value fits one varint byte: the bytes ARE the values.
        return {"f0": f0, "d": base64.b64encode(z.astype(np.uint8).tobytes()).decode("ascii")}
    return {"f0": f0, "d": _varints(d.tolist())}
