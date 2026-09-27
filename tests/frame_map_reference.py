"""A FROZEN scalar copy of ``render/frame_map.py`` (wave D3, lane E1b): the
per-frame, exact-``Fraction`` implementation the fast one replaced, kept as
the oracle ``tests/test_frame_map_perf.py`` holds the fast one to — the
same ``frame_map_json``, byte for byte, on the goldens and on fuzzed EDLs.

Frozen at the in-anchored curve clock (``edl/speed_curve.py`` "the v1
chain's clock"). Only the pieces the fast path rewrote live here (frame
selection, the program-map fold, the RLE, the sound placement arithmetic);
everything else is the live module's. Do not "optimise" this file: its
value is that it is the slow, obvious reading of the ffmpeg chain.
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from video_ai_editor.edl import speed_curve as _sc
from video_ai_editor.edl import timebase as _tb
from video_ai_editor.edl.schema import Clip, seam_matching, seam_table_for
from video_ai_editor.render import frame_map as live
from video_ai_editor.render.frame_map import (
    KIND_BLEND, KIND_CLIP, KIND_GAP, FRAME_MAP_VERSION, AudioPlacement, ProgramMap,
    SourceInfo, _plan_view, _speed_divisor, _varints, curve_retimer, ffmpeg_us,
    intermediate_source, reverse_segment_frames, reversed_frames, round_half_away,
    speed_curve_map, ticks_per_frame,
)

_US = Fraction(1, 1_000_000)
_SAMPLE_RATE = 48000


def rescale(a: int, src_tb: Fraction, dst_tb: Fraction) -> int:
    return round_half_away(Fraction(a) * src_tb / dst_tb)


def pts(src: SourceInfo, i: int) -> int:
    t = src.pts_table
    if t:
        if 0 <= i < len(t):
            return t[i]
        step = t[-1] - t[-2] if len(t) > 1 else round_half_away(src.frame_ticks)
        return t[-1] + (i - len(t) + 1) * step
    f = src.frame_ticks
    if f.denominator == 1:
        return src.start_ticks + i * f.numerator
    return src.start_ticks + round_half_away(i * f)


def _first_at_or_after(src: SourceInfo, ticks: int) -> int:
    f = src.frame_ticks
    i = max(0, math.floor((ticks - src.start_ticks) / f) - 1)
    while pts(src, i) < ticks:
        i += 1
    while i > 0 and pts(src, i - 1) >= ticks:
        i -= 1
    return i


def select_frames(src: SourceInfo, *, seek_us, dur_us: int, n: int, fps, speed=None,
                  curve=None, anchor=None) -> list[int]:
    if n <= 0:
        return []
    tb = src.time_base
    r = _tb.rate_of(fps)
    out_tb = 1 / r
    last = src.frames - 1
    if last < 0:
        return [0] * n
    off = 0
    if seek_us is not None:
        off = rescale(-seek_us, _US, tb)
        f0 = _first_at_or_after(src, -off)
    else:
        f0 = 0
    if f0 > last:
        return [last] * n
    base = pts(src, f0)
    dur_tb = rescale(dur_us, _US, tb)
    qlast = 0
    while f0 + qlast + 1 <= last and pts(src, f0 + qlast + 1) - base < dur_tb:
        qlast += 1
    div = _speed_divisor(speed)
    if curve is not None and anchor is not None:
        TB = tb.numerator / tb.denominator
        a_seek, a_in = float(anchor[0]), float(anchor[1])
        back = (off if seek_us is not None else 0) + (
            int(_sc.c_round(a_seek / TB)) if a_seek > 0 else 0)
        tb = _sc.curve_time_base(tb, r.numerator)
        k = int(src.time_base / tb)
        TBc = tb.numerator / tb.denominator

        def ticks(q: int) -> int:
            return _sc.anchored_ticks((pts(src, f0 + q) + back) * k, TBc, a_in, curve)
    else:
        if curve is not None:
            retime = curve_retimer(curve, tb)
        else:
            def retime(x: int) -> int:
                return int(x / div) if div is not None else x

        def ticks(q: int) -> int:
            return retime(pts(src, f0 + q) - base)

    slots = [rescale(ticks(q), tb, out_tb) for q in range(qlast + 1)]
    eof = rescale(ticks(qlast + 1), tb, out_tb)
    out: list[int] = []
    q = 0
    for s in range(n):
        lim = min(s, eof - 1)
        while q < qlast and slots[q + 1] <= lim:
            q += 1
        out.append(f0 + q)
    return out


def forward_clip_frames(src: SourceInfo, *, in_: float, out: float, speed, n: int, fps) -> list[int]:
    curve = speed_curve_map(speed, in_, out)
    if curve is not None:
        seek = _sc.curve_seek(in_)
    else:
        pre = _tb.seek_preroll(in_, fps)
        seek = max(0.0, float(in_) - pre)
    end = float(out) + 2 * _tb.frame_duration(fps)
    seek_us = ffmpeg_us(seek) if seek > 0 else None
    dur_us = ffmpeg_us(end) - (seek_us or 0)
    return select_frames(src, seek_us=seek_us, dur_us=dur_us, n=n, fps=fps, speed=speed,
                         curve=curve, anchor=(seek, float(in_)) if curve is not None else None)


def freeze_frame(src: SourceInfo, *, in_: float, fps) -> int:
    from video_ai_editor.render.compositor import freeze_input_span
    seek, end = freeze_input_span(in_, fps)
    seek_us = ffmpeg_us(seek) if seek > 0 else None
    dur_us = ffmpeg_us(end) - (seek_us or 0)
    return select_frames(src, seek_us=seek_us, dur_us=dur_us, n=1, fps=fps)[0]


def reversed_intermediate(src: SourceInfo, *, in_: float, out: float, fps) -> list[int]:
    m = max(1, _tb.frame_of(float(out) - float(in_), fps))
    seg = reverse_segment_frames(src.width, src.height, fps)
    forward: list[int] = []
    for j0 in range(0, m, seg):
        n = min(seg, m - j0)
        t0 = float(in_) + _tb.time_of(j0, fps)
        pre = _tb.seek_preroll(t0, fps)
        seek = max(0.0, t0 - pre)
        span = pre + _tb.time_of(n + 2, fps)
        seek_us = ffmpeg_us(seek) if seek > 0 else None
        forward += select_frames(src, seek_us=seek_us, dur_us=ffmpeg_us(span), n=n, fps=fps)
    return forward[::-1]


def clip_frame_list(c: Clip, src: SourceInfo, fps) -> list[int]:
    from video_ai_editor.render.compositor import clip_frames
    if getattr(c, "freeze", None) is not None:
        return [freeze_frame(src, in_=c.in_, fps=fps)] * clip_frames(c, fps)
    if getattr(c, "reverse", False):
        inter = reversed_intermediate(src, in_=c.in_, out=c.out, fps=fps)
        view = live._reversed_view(c, fps)
        n = clip_frames(view, fps)
        idx = forward_clip_frames(intermediate_source(len(inter), fps), in_=view.in_,
                                  out=view.out, speed=view.speed, n=n, fps=fps)
        return [inter[i] for i in idx]
    return forward_clip_frames(src, in_=c.in_, out=c.out, speed=c.speed,
                               n=clip_frames(c, fps), fps=fps)


@dataclass(frozen=True)
class _Leaf:
    kind: int
    clip: int
    frame: int


@dataclass(frozen=True)
class _Blend:
    a: object
    b: _Leaf
    j: int
    d: int


def _leaf_a(e) -> _Leaf:
    while isinstance(e, _Blend):
        e = e.a
    return e


def build_program_map(edl, sources, fps=None) -> ProgramMap:
    from video_ai_editor.render.compositor import _v1_frame_plan
    lookup = sources.__getitem__ if isinstance(sources, Mapping) else sources
    fps = edl.canvas.fps if fps is None else fps
    r = _tb.rate_of(fps)
    view, planned, originals = _plan_view(edl, fps)
    v1 = view.get_track("v1")
    transitions = list((v1.transitions if v1 else []) or [])
    seams = seam_table_for(planned, transitions, fps=fps) if transitions else []
    total_duration = max(0.0, view.duration + sum(d for _s, d in seams))
    if not planned:
        total_duration = max(1.0, total_duration)
    plan = _v1_frame_plan(planned, total_duration, fps)
    segs: list[list] = []
    seg_of_clip: dict[int, int] = {}
    per_clip: dict[int, list[int]] = {}
    for kind, ci, nfr in plan:
        if kind == "clip":
            c = originals[ci]
            frames = per_clip.get(ci)
            if frames is None:
                frames = clip_frame_list(c, lookup(c.src), fps)
                per_clip[ci] = frames
            seg_of_clip[ci] = len(segs)
            segs.append([_Leaf(KIND_CLIP, ci, frames[j]) for j in range(nfr)])
        else:
            segs.append([_Leaf(KIND_GAP, -1, -1)] * nfr)
    seg_trans: dict[int, int] = {}
    seg_cost: dict[int, float] = {}
    seam_rows: list[dict] = []
    for idx, c in enumerate(planned[:-1]):
        si = seg_of_clip.get(idx)
        if si is None or seg_of_clip.get(idx + 1) != si + 1:
            continue
        boundary = c.start + c.effective_duration
        cost = next((d for seam, d in seams if abs(seam - boundary) < 0.001), 0.0)
        record = seam_matching(transitions, boundary)
        if cost > 0.0 and record is not None:
            d = _tb.frame_of(cost, fps)
            seg_trans[si] = d
            seg_cost[si] = cost
            seam_rows.append({"left": idx, "right": idx + 1, "frames": d, "type": record.type})
    starts: dict[int, int] = {}
    lens: dict[int, int] = {}
    cur: list = []
    for i, seg in enumerate(segs):
        d = seg_trans.get(i - 1, 0) if i > 0 else 0
        if d > 0:
            offset = max(0, len(cur) - d)
            blended = [_Blend(cur[offset + j], seg[j], j, d) for j in range(d)]
            start_new = offset
            cur = cur[:offset] + blended + seg[d:]
        else:
            start_new = len(cur)
            cur = cur + seg
        ci = next((c for c, s in seg_of_clip.items() if s == i), None)
        if ci is not None:
            starts[ci] = start_new
            lens[ci] = len(seg)
    pm = ProgramMap(fps=r, total=len(cur), clips=originals, seams=seam_rows)
    for e in cur:
        if isinstance(e, _Blend):
            a = _leaf_a(e.a)
            pm.kind.append(KIND_BLEND)
            pm.clip.append(a.clip)
            pm.frame.append(a.frame)
            pm.b_clip.append(e.b.clip)
            pm.b_frame.append(e.b.frame)
            pm.p_num.append(e.d - e.j)
            pm.p_den.append(e.d)
            pm.nested.append(isinstance(e.a, _Blend))
        else:
            pm.kind.append(e.kind)
            pm.clip.append(e.clip)
            pm.frame.append(e.frame)
            pm.b_clip.append(-1)
            pm.b_frame.append(-1)
            pm.p_num.append(0)
            pm.p_den.append(0)
            pm.nested.append(False)
    pm.clip_start = [starts.get(i, -1) for i in range(len(originals))]
    pm.clip_len = [lens.get(i, 0) for i in range(len(originals))]
    pm.segments = [(kind, ci if ci is not None else -1, nfr, seg_cost.get(si - 1, 0.0) if si else 0.0)
                   for si, (kind, ci, nfr) in enumerate(plan)]
    return pm


def _clip_sample0(t: float, fps) -> int:
    pre = _tb.seek_preroll(t, fps)
    seek = max(0.0, float(t) - pre)
    sr = Fraction(1, _SAMPLE_RATE)
    j0 = rescale(ffmpeg_us(seek), _US, sr) if seek > 0 else 0
    return j0 + (rescale(ffmpeg_us(pre), _US, sr) if pre > 1e-9 else 0)


def _reversed_runs(c: Clip, src, fps, n: int):
    m_frames = reversed_frames(c, fps)
    seg = reverse_segment_frames(src.width if src else 1920, src.height if src else 1080, fps)
    forward = []
    for j0 in range(0, m_frames, seg):
        nf = min(seg, m_frames - j0)
        s0 = _tb.samples_for_frames(j0, fps)
        cnt = _tb.samples_for_frames(j0 + nf, fps) - s0
        t0 = float(c.in_) + _tb.time_of(j0, fps)
        forward.append((_clip_sample0(t0, fps), cnt))
    runs, off = [], 0
    for first, cnt in reversed(forward):
        take = min(cnt, n - off)
        if take <= 0:
            break
        runs.append((off, take, first + cnt - 1, -1))
        off += take
    return tuple(runs)


def audio_placements(edl, pm: ProgramMap, fps=None, sources=None) -> list[AudioPlacement]:
    fps = edl.canvas.fps if fps is None else fps
    lookup = (sources.__getitem__ if isinstance(sources, Mapping) else sources) if sources else None
    out: list[AudioPlacement] = []
    cursor = 0
    last = None
    for kind, ci, nfr, cost in pm.segments:
        m = _tb.samples_for_frames(nfr, fps)
        ov = rescale(ffmpeg_us(cost), _US, Fraction(1, _SAMPLE_RATE)) if cost > 0 else 0
        start = cursor - ov
        if ov and last is not None:
            out[last] = dataclasses.replace(out[last], fade_out=ov)
        if kind == "clip":
            c = pm.clips[ci]
            div = _speed_divisor(c.speed)
            rate = div or 1.0
            curve = _sc.curve_points(c.speed)
            if getattr(c, "freeze", None) is not None:
                out.append(AudioPlacement(clip=ci, out0=start, n=m, src0=_clip_sample0(c.in_, fps),
                                          rate=0.0, mode="silence", runs=(), fade_in=ov))
                last = len(out) - 1
                cursor = start + m
                continue
            if curve is not None:
                rate = _sc.mean_speed(curve)
            if getattr(c, "reverse", False):
                mode = "reverse"
                src = None
                if lookup is not None:
                    try:
                        src = lookup(c.src)
                    except KeyError:
                        src = None
                runs = _reversed_runs(c, src, fps, m) if div is None and curve is None else ()
            elif curve is not None:
                mode, runs = "curve", ()
            else:
                mode = "exact" if div is None else (
                    "tempo" if getattr(c.audio, "keep_pitch", True) else "varispeed")
                runs = ((0, m, _clip_sample0(c.in_, fps), 1),) if div is None else ()
            out.append(AudioPlacement(clip=ci, out0=start, n=m, src0=_clip_sample0(c.in_, fps),
                                      rate=rate, mode=mode, runs=runs, fade_in=ov))
            last = len(out) - 1
        else:
            last = None
        cursor = start + m
    return out


def audio_total_samples(edl, pm: ProgramMap, fps=None) -> int:
    fps = edl.canvas.fps if fps is None else fps
    cursor = 0
    for _kind, _ci, nfr, cost in pm.segments:
        ov = rescale(ffmpeg_us(cost), _US, Fraction(1, _SAMPLE_RATE)) if cost > 0 else 0
        cursor += _tb.samples_for_frames(nfr, fps) - ov
    return cursor


def _encode_seq(vals) -> dict:
    f0 = vals[0]
    if len(vals) == 1:
        return {"f0": f0, "step": 1}
    step = vals[1] - vals[0]
    if all(vals[i + 1] - vals[i] == step for i in range(len(vals) - 1)):
        return {"f0": f0, "step": step}
    return {"f0": f0, "d": _varints(vals[i + 1] - vals[i] for i in range(len(vals) - 1))}


def to_rle(pm: ProgramMap, src_key=None) -> list[dict]:
    key = src_key or (lambda c: c.src)
    runs: list[dict] = []
    k = 0
    while k < pm.total:
        kind, ci, bi, nest = pm.kind[k], pm.clip[k], pm.b_clip[k], pm.nested[k]
        e = k + 1
        while e < pm.total and pm.kind[e] == kind and pm.clip[e] == ci \
                and pm.b_clip[e] == bi and pm.nested[e] == nest:
            if kind == KIND_BLEND and (pm.p_den[e] != pm.p_den[k]
                                       or pm.p_num[e] != pm.p_num[e - 1] - 1):
                break
            e += 1
        run: dict = {"k0": k, "n": e - k, "kind": kind}
        if kind != KIND_GAP:
            c = pm.clips[ci]
            run.update(clip_id=c.id, src=key(c), a=_encode_seq(pm.frame[k:e]))
        if kind == KIND_BLEND:
            b = pm.clips[bi]
            den = pm.p_den[k]
            j0 = den - pm.p_num[k] if den else 0
            run.update(b_clip_id=b.id, b_src=key(b), b=_encode_seq(pm.b_frame[k:e]),
                       p_den=den, p_j0=j0, nested=nest)
        runs.append(run)
        k = e
    return runs


def frame_map_json(edl, sources, *, src_key=None) -> dict:
    pm = build_program_map(edl, sources)
    r = pm.fps
    key = src_key or (lambda c: c.src)
    return {
        "version": FRAME_MAP_VERSION,
        "render_hash": edl.render_hash(),
        "R": [r.numerator, r.denominator],
        "T": ticks_per_frame(r),
        "total": pm.total,
        "runs": to_rle(pm, key),
        "seams": pm.seams,
        "audio": [{"clip_id": pm.clips[a.clip].id, "src": key(pm.clips[a.clip]),
                   "out0": a.out0, "n": a.n, "src0": a.src0, "rate": a.rate,
                   "mode": a.mode, "runs": [list(r) for r in a.runs],
                   "fade_in": a.fade_in, "fade_out": a.fade_out}
                  for a in audio_placements(edl, pm, sources=sources)],
        "audio_total": audio_total_samples(edl, pm),
    }
