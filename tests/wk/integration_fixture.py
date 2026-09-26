"""Fixture media and sessions for the instant-preview INTEGRATION suites
(tests/wk/test_wk_integration.py; INSTANT_PREVIEW_SPEC §13 P1-F3, P1-F4,
P1-S1, P1-A3, P1-B1 and the external-pause requirement).

Everything is real: bar-coded masters made by ffmpeg (proxy_fixture's bar:
source id + frame index burned into every frame), dropped into a session's
uploads the way an import leaves them, placed with real `/dispatch` calls,
their proxies (span packs + FLAC chunks) built by the app's own proxy
manager, served by the app's own routes.

The flash/click master ("F") carries the A/V sync signal: every
``FLASH_EVERY``-th frame is white below the bar, and its sound is a 0.5 ms
square burst starting exactly on that frame's first sample
(``samples_for_frames``: at 30 fps, frame i starts at sample 1600·i).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .proxy_fixture import BAR_BITS, BAR_CELL, FRAME_BITS, MASTER_CODEC_ARGS, SourceSpec, encode_master, validate

SR = 48000
FLASH_EVERY = 15

A = SourceSpec("A", 1, 1280, 720, 300, "testsrc2")
B = SourceSpec("B", 2, 1280, 720, 300, "testsrc")
C = SourceSpec("C", 3, 720, 1280, 250, "testsrc2", Fraction(25))
F = SourceSpec("F", 4, 1280, 720, 600, "color")
S = SourceSpec("S", 5, 960, 540, 150, "testsrc2", Fraction(25))


def flash_master(spec: SourceSpec, out: Path) -> Path:
    """The flash/click master: dark gray, white below the bar on every
    FLASH_EVERY-th frame, a click at each flash frame's first sample."""
    validate(spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    rate = f"{spec.rate.numerator}/{spec.rate.denominator}"
    dur = float(spec.frames / spec.rate) + 1
    base = spec.src_id << FRAME_BITS
    bar = (f"nullsrc=s={spec.w}x40:r={rate}:d={dur},format=gray,"
           f"geq=lum='if(bitand(floor(({base}+N)/pow(2\\,floor(X/{BAR_CELL})))\\,1)"
           f"*lt(X\\,{BAR_BITS * BAR_CELL})\\,235\\,16)'")
    period = int(SR * FLASH_EVERY * spec.rate.denominator / spec.rate.numerator)
    click = f"0.6*lt(mod(n\\,{period})\\,24)"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x303030:s={spec.w}x{spec.h}:r={rate}:d={dur}",
        "-f", "lavfi", "-i", bar,
        "-f", "lavfi", "-i", f"aevalsrc=exprs='{click}|{click}':s={SR}:d={dur}",
        "-filter_complex",
        f"[0:v]drawbox=x=0:y=40:w=iw:h=ih-40:color=white:t=fill:enable='eq(mod(n\\,{FLASH_EVERY})\\,0)',"
        "format=yuv420p[m];[1:v]format=yuv420p[b];[m][b]overlay=0:0:shortest=1,format=yuv420p,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv[v]",
        "-map", "[v]", "-map", "2:a", "-frames:v", str(spec.frames), "-fps_mode", "passthrough",
        *MASTER_CODEC_ARGS, "-c:a", "pcm_s16le", "-t", f"{float(spec.frames / spec.rate):.6f}", str(out),
    ], check=True, capture_output=True)
    return out


def build_masters(cache: Path) -> dict[str, Path]:
    """Encode (once per cache dir) every master the suites use."""
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for spec in (A, B, C, S):
        p = cache / f"{spec.name}.mp4"
        if not p.exists():
            encode_master(spec, p)
        out[spec.name] = p
    p = cache / "F.mov"
    if not p.exists():
        flash_master(F, p)
    out["F"] = p
    return out


@dataclass
class Session:
    sid: str
    #: src (as the EDL stores it) → bar source id
    src_ids: dict[str, int]
    clip_ids: list[str]


def make_session(backend, masters: dict[str, Path], wd: Path, canvas: tuple[int, int, int],
                 clips: list[tuple[str, float, float]]) -> Session:
    """A session with `clips` = [(master name, in s, out s), ...] laid end to
    end on v1, proxies built (video spans + FLAC) before it is returned."""
    from video_ai_editor.ingest.proxy_queue import MANAGER

    ids = {"A": A.src_id, "B": B.src_id, "C": C.src_id, "F": F.src_id, "S": S.src_id}
    sid = backend.call("POST", "/api/sessions")["id"]
    w, h, fps = canvas
    backend.call("POST", f"/api/sessions/{sid}/dispatch", {"tool": "set_canvas", "args": {"w": w, "h": h, "fps": fps}})
    local: dict[str, Path] = {}
    for name in sorted({c[0] for c in clips}):
        src = masters[name]
        dst = wd / sid / "uploads" / name / f"{name}.normalized{src.suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        local[name] = dst
    src_ids: dict[str, int] = {}
    clip_ids: list[str] = []
    start = 0.0
    for name, a, b in clips:
        ans = backend.call("POST", f"/api/sessions/{sid}/dispatch?include=edl",
                           {"tool": "add_clip", "args": {"src": str(local[name]), "track": "v1", "in": a, "out": b,
                                                         "start": round(start, 6)}})
        cid = ans["result"]["clip_id"]
        clip_ids.append(cid)
        stored = next(c["src"] for t in ans["edl"]["tracks"] for c in t["clips"] if c["id"] == cid)
        src_ids[stored] = ids[name]
        start += b - a
    for stored in src_ids:
        MANAGER.ensure(Path(stored), sid=sid, eager=True)
    assert MANAGER.wait_idle(300), "proxies did not finish building"
    return Session(sid, src_ids, clip_ids)


__all__ = ["A", "B", "C", "F", "S", "SR", "FLASH_EVERY", "Session", "build_masters", "make_session", "flash_master"]
