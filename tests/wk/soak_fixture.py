"""Fixture media and sessions for the instant-preview SOAK (tests/wk/test_wk_soak.py;
INSTANT_PREVIEW_SPEC §11.3, §13 P1-M1 and P1-E1).

Twelve 1920x1080 30 fps masters of 60 s each (12 source-minutes), made by
ffmpeg the way the other WK suites make theirs:

* picture: a moving test pattern with light temporal noise (so the 720p
  all-intra proxies carry a camera-like bitrate, not a flat card's) and the
  frame-identity bar of proxy_fixture burned into rows 0..39 (source id in
  bits 11..14, source frame in bits 0..10);
* sound: a click track — a 0.5 ms burst on the first sample of every 15th
  frame over a quiet per-source carrier — PCM in a .mov, as an import
  leaves a master.

The timeline lays 72 clips of 10 s end to end on v1 (every source six times
at different `in` points): 12 minutes, 21,600 output frames.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .proxy_fixture import BAR_BITS, BAR_CELL, FRAME_BITS, MASTER_CODEC_ARGS, SourceSpec, validate

SR = 48000
FPS = 30
W, H = 1920, 1080
SOURCE_FRAMES = 1800           # 60 s, inside the bar's 11-bit frame field
N_SOURCES = 12
CLIP_S = 10.0
N_CLIPS = 72                   # 72 x 10 s = 12 minutes
CLICK_EVERY = 15

SPECS = [SourceSpec(f"S{i:02d}", i, W, H, SOURCE_FRAMES, "testsrc2" if i % 2 else "testsrc", Fraction(FPS))
         for i in range(1, N_SOURCES + 1)]


def encode_soak_master(spec: SourceSpec, out: Path) -> Path:
    """One 1080p bar-coded click-track master (`spec.frames` frames)."""
    validate(spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    rate = f"{spec.rate.numerator}/{spec.rate.denominator}"
    dur = float(spec.frames / spec.rate) + 1
    base = spec.src_id << FRAME_BITS
    bar = (f"nullsrc=s={spec.w}x40:r={rate}:d={dur},format=gray,"
           f"geq=lum='if(bitand(floor(({base}+N)/pow(2\\,floor(X/{BAR_CELL})))\\,1)"
           f"*lt(X\\,{BAR_BITS * BAR_CELL})\\,235\\,16)'")
    period = int(SR * CLICK_EVERY * spec.rate.denominator / spec.rate.numerator)
    hz = 300 + 40 * spec.src_id
    tone = f"0.08*sin(2*PI*{hz}*t)+0.6*lt(mod(n\\,{period})\\,24)"
    tmp = out.with_suffix(".part.mov")
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"{spec.pattern}=s={spec.w}x{spec.h}:r={rate}:d={dur}",
        "-f", "lavfi", "-i", bar,
        "-f", "lavfi", "-i", f"aevalsrc=exprs='{tone}|{tone}':s={SR}:d={dur}",
        "-filter_complex",
        "[0:v]noise=alls=10:allf=t,format=yuv420p[m];[1:v]format=yuv420p[b];"
        "[m][b]overlay=0:0:shortest=1,format=yuv420p,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv[v]",
        "-map", "[v]", "-map", "2:a", "-frames:v", str(spec.frames), "-fps_mode", "passthrough",
        *MASTER_CODEC_ARGS, "-c:a", "pcm_s16le", "-t", f"{float(spec.frames / spec.rate):.6f}", str(tmp),
    ], check=True, capture_output=True)
    os.replace(tmp, out)
    return out


def build_soak_masters(cache: Path, names: list[str] | None = None) -> dict[str, Path]:
    """Encode (once per cache dir) the masters named (default: all twelve)."""
    cache.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for spec in SPECS:
        if names is not None and spec.name not in names:
            continue
        p = cache / f"{spec.name}.mov"
        if not p.exists():
            encode_soak_master(spec, p)
        out[spec.name] = p
    return out


def twelve_minute_layout() -> list[tuple[str, float, float]]:
    """72 clips x 10 s: source i % 12, `in` stepping through the master."""
    out = []
    for i in range(N_CLIPS):
        spec = SPECS[i % N_SOURCES]
        a = round(((i // N_SOURCES) * 8.3 + (i % 5) * 0.7) % (SOURCE_FRAMES / FPS - CLIP_S - 0.5), 3)
        out.append((spec.name, a, round(a + CLIP_S, 3)))
    return out


def short_layout() -> list[tuple[str, float, float]]:
    """P1-E1's script: 12 clips x 3 s over three sources."""
    return [(SPECS[i % 3].name, round(1.0 + i * 1.7, 3), round(4.0 + i * 1.7, 3)) for i in range(12)]


def restart_layout() -> list[tuple[str, float, float]]:
    """96 s over three sources (12 x 8 s): longer than laneA's 40 s window,
    so the start is NOT buffered when playback reaches the end."""
    return [(SPECS[i % 3].name, round(1.0 + (i * 3.1) % 40, 3), round(9.0 + (i * 3.1) % 40, 3)) for i in range(12)]


@dataclass
class Session:
    sid: str
    #: src (as the EDL stores it) → bar source id
    src_ids: dict[str, int]
    clip_ids: list[str]
    total_s: float


def _place(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)                       # same volume: no 1 GB copy
    except OSError:
        shutil.copyfile(src, dst)


def _call(backend, method: str, path: str, body: dict | None = None) -> dict:
    """backend.call, waiting out the per-path rate bucket (429) the way the
    app's own client does: a 72-clip layout outruns 60 requests/s."""
    import time
    import urllib.error

    for attempt in range(100):
        try:
            return backend.call(method, path, body)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 99:
                raise
            time.sleep(min(2.0, float(e.headers.get("Retry-After") or 0.2)))
    raise AssertionError("unreachable")


def make_session(backend, masters: dict[str, Path], wd: Path, clips: list[tuple[str, float, float]]) -> Session:
    """A 1920x1080 30 fps session with `clips` laid end to end on v1, the
    masters dropped into its uploads the way an import leaves them, and
    every proxy (span packs + FLAC chunks) built before it is returned."""
    from video_ai_editor.ingest.proxy_queue import MANAGER

    ids = {s.name: s.src_id for s in SPECS}
    sid = _call(backend, "POST", "/api/sessions")["id"]
    _call(backend, "POST", f"/api/sessions/{sid}/dispatch", {"tool": "set_canvas", "args": {"w": W, "h": H, "fps": FPS}})
    local: dict[str, Path] = {}
    for name in sorted({c[0] for c in clips}):
        dst = wd / sid / "uploads" / name / f"{name}.normalized.mov"
        _place(masters[name], dst)
        local[name] = dst
    src_ids: dict[str, int] = {}
    clip_ids: list[str] = []
    start = 0.0
    for name, a, b in clips:
        ans = _call(backend, "POST", f"/api/sessions/{sid}/dispatch?include=edl",
                           {"tool": "add_clip", "args": {"src": str(local[name]), "track": "v1", "in": a, "out": b,
                                                         "start": round(start, 6)}})
        cid = ans["result"]["clip_id"]
        clip_ids.append(cid)
        stored = next(c["src"] for t in ans["edl"]["tracks"] for c in t["clips"] if c["id"] == cid)
        src_ids[stored] = ids[name]
        start += b - a
    for stored in src_ids:
        MANAGER.ensure(Path(stored), sid=sid, eager=True)
    assert MANAGER.wait_idle(1200), "proxies did not finish building"
    return Session(sid, src_ids, clip_ids, start)


__all__ = ["SPECS", "SR", "FPS", "W", "H", "N_CLIPS", "CLIP_S", "Session", "build_soak_masters",
           "encode_soak_master", "make_session", "twelve_minute_layout", "short_layout", "restart_layout"]
