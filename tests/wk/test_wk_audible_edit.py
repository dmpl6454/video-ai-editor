"""§11.1 AUDIBLE-EDIT LATENCY while playing, in real WKWebView
(INSTANT_PREVIEW_SPEC §11.1 "Trim in/out, ripple, delete, move … | Audio
audible (playing) ≤ 250 ms", §3.6 "a structural edit while playing …
reschedules from presentedK + lead").

The app path end to end: a real backend (LiveBackend), tone masters (a bar
picture, a continuous sine: A 440 Hz, B 1320 Hz) alternating on v1, the
app's PreviewController with the REAL AudioEngine, its output tapped by an
AudioWorklet. While playing, the A clip under the playhead is ripple-deleted
(`/dispatch?include=edl`, then applyTimeline, as the store does), so B slides
under the playhead: the latency is from the commit (dispatch start) to the
first HEARD 5 ms of the output where B's tone dominates A's (Goertzel, the
tap's context clock mapped to performance.now() by getOutputTimestamp).
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from .integration_fixture import SR, make_session
from .playback import load_per_core, machine_is_quiet, timing_budget
from .proxy_fixture import BAR_BITS, BAR_CELL, FRAME_BITS, MASTER_CODEC_ARGS, SourceSpec, validate
from .test_wk_integration import env  # noqa: F401  (fixture)

pytestmark = pytest.mark.wk

AUDIBLE_BUDGET_MS = 250
TONE_A = SourceSpec("A", 1, 1280, 720, 900, "testsrc2", Fraction(30))
TONE_B = SourceSpec("B", 2, 1280, 720, 900, "testsrc", Fraction(30))


def tone_master(spec: SourceSpec, freq: int, out: Path) -> Path:
    """A bar-coded master whose sound is one continuous sine."""
    validate(spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    rate = f"{spec.rate.numerator}/{spec.rate.denominator}"
    dur = float(spec.frames / spec.rate)
    base = spec.src_id << FRAME_BITS
    bar = (f"nullsrc=s={spec.w}x40:r={rate}:d={dur + 1},format=gray,"
           f"geq=lum='if(bitand(floor(({base}+N)/pow(2\\,floor(X/{BAR_CELL})))\\,1)"
           f"*lt(X\\,{BAR_BITS * BAR_CELL})\\,235\\,16)'")
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"{spec.pattern}=s={spec.w}x{spec.h}:r={rate}:d={dur + 1}",
        "-f", "lavfi", "-i", bar,
        "-f", "lavfi", "-i", f"sine=f={freq}:sample_rate={SR}:d={dur + 1}",
        "-filter_complex", "[1:v]format=yuv420p[b];[0:v]format=yuv420p[m];[m][b]overlay=0:0:shortest=1,format=yuv420p[v];"
                           "[2:a]volume=0.5,pan=stereo|c0=c0|c1=c0[a]",
        "-map", "[v]", "-map", "[a]", "-frames:v", str(spec.frames), "-fps_mode", "passthrough",
        *MASTER_CODEC_ARGS, "-c:a", "pcm_s16le", "-t", f"{dur:.6f}", str(out),
    ], check=True, capture_output=True)
    return out


@pytest.fixture(scope="module", params=[30, 24, 60], ids=["30fps", "24fps", "60fps"])
def tone_session(env, request):  # noqa: F811
    """The same tone timeline in a 30, 24 and 60 fps project: a lead counted
    in FRAMES is 250 ms at 24 fps, the whole budget."""
    root = env.root / "tones"
    masters = {"A": root / "A.mov", "B": root / "B.mov"}
    if not masters["A"].exists():
        tone_master(TONE_A, 440, masters["A"])
    if not masters["B"].exists():
        tone_master(TONE_B, 1320, masters["B"])
    clips = []
    for i in range(14):
        a = round((i % 6) * 2.0, 3)
        clips += [("A", a, a + 2.0), ("B", a, a + 2.0)]
    s = make_session(env.backend, masters, env.root / "wd", (1280, 720, request.param), clips)
    return s, request.param


@pytest.mark.wk
def test_audible_edit_latency_while_playing(env, tone_session):  # noqa: F811
    s, fps = tone_session
    cfg = {"sid": s.sid, "srcIds": s.src_ids, "canvas": [640, 360], "edits": 10}
    tag = f"cfg-audible-{fps}"
    (env.static / f"{tag}.json").write_text(json.dumps(cfg))
    r = env.wk("audible_edit", tag, timeout=240)
    trials = r["trials"]
    heard = [t["heardMs"] for t in trials if t.get("heardMs") is not None]
    srt = sorted(heard)
    p95 = srt[min(len(srt) - 1, int(0.95 * (len(srt) - 1) + 0.5))] if srt else None
    print(json.dumps({"audible_edit": {"fps": fps, "heardMs": heard, "p95": p95, "max": max(heard, default=None),
                                       "picture": [t.get("pictureMs") for t in trials], "rtt": [t.get("rttMs") for t in trials], "outputLatency": r["outputLatency"],
                                       "events": r["events"][:8], "restarts": r["restarts"]},
                      "load_per_core": round(load_per_core(), 2), "quiet": machine_is_quiet()}))
    assert len(trials) >= 8 and all("heardMs" in t for t in trials), trials
    assert all(t["heardMs"] is not None for t in trials), trials      # B was heard after every edit
    assert all(t["heardMs"] > 0 for t in trials), trials              # never before the commit
    assert p95 <= timing_budget(AUDIBLE_BUDGET_MS), (p95, heard, load_per_core())
