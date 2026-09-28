"""Voice effects in the instant-preview SOUND (wave E, F3): the client's offline
mix against the server's render of the same timeline, per preset, in real
WKWebView (marker ``wk``), Playwright's Chromium and Playwright's WebKit —
the shipped modules (``lib/preview/testkit/wkAudioPage.ts``: audio/audioPlan
+ audio/mixGraph + lib/voice/voiceFx, the FLAC chunks through audioChunks).

The class is APPROX (``support.ts`` VOICE_FX_PARITY): the client runs the
export's filters, echo taps, saturation, ring modulation and vibrato line
sample for sample and convolves the Hall with the export's IR, but shifts
pitch with a granular shifter where the export uses asetrate + atempo. Per
50 ms block of a synthesized voice (a harmonic tone with a syllable
envelope): the mean |client/export − 1| of the spectral centroid and the mean
|Δ| of the block level must stay within the table's numbers — which were set
from these measurements (printed per engine) — and the non-pitch presets must
line up with the export to the sample (cross-correlation lag 0 ± 1).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl import voice_effects as V
from video_ai_editor.edl.schema import Canvas, empty_edl

from . import audio_fixture as fx
from .harness import PageServer
from .test_wk_audio import (  # noqa: F401 — module fixtures reused
    ENGINES, PAGES, Runner, _lag, _pcm, bundle,
)

REPO = Path(__file__).resolve().parents[2]
SUPPORT_TS = REPO / "frontend" / "src" / "lib" / "preview" / "timeline" / "support.ts"
SR = 48000
BLOCK = 2400
QUIET_DB = -45.0
PITCHED = {"chipmunk", "deep", "monster"}

#: A voice-like sound, the same on both sides: 150 Hz with 24 1/k harmonics,
#: a 4 Hz syllable envelope and a slow pitch drift (a real voice is never flat).
VOICE = ("(0.35+0.65*gt(sin(2*PI*4*t)\\,-0.2))*("
         + "+".join(f"{0.3 / k:.5f}*sin(2*PI*{k}*(150*t+3*sin(2*PI*0.7*t)))" for k in range(1, 25)) + ")")


def parity_table() -> dict[str, dict[str, float]]:
    """VOICE_FX_PARITY as support.ts states it (the numbers the class claims)."""
    body = SUPPORT_TS.read_text(encoding="utf-8")
    block = body[body.index("export const VOICE_FX_PARITY"):]
    block = block[:block.index("} as const")]
    out = {}
    for m in re.finditer(r"^\s*(\w+): \{([^}]*)\}", block, re.M):
        row: dict = {k: float(v) for k, v in re.findall(r"(\w+): ([\d.]+)", m.group(2))}
        row["mode"] = re.search(r"mode: MODE_(\w+)", m.group(2)).group(1)
        out[m.group(1)] = row
    return out


@pytest.fixture(scope="module")
def vfx_root(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("wk-voice")
    wd = root / "wd"
    wd.mkdir()
    src = root / "voice.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=30:d=5",
                    "-f", "lavfi", "-i", f"aevalsrc=exprs='{VOICE}|{VOICE}':s={SR}:d=5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_f32le", "-shortest", str(src)],
                   check=True, capture_output=True)
    key, _idx, info = fx.build_proxy_audio(src, wd)
    return {"root": root, "wd": wd, "src": str(src), "key": (key, info)}


def _edl(src: str, preset: str | None, lane: str):
    e = empty_edl(Canvas(w=64, h=36, fps=30))
    e.canvas.loudness_lufs = None
    audio = {"voice_effect": preset} if preset else {}
    if lane == "v1":
        e.get_track("v1").clips.append(fx._clip(src, "a", 0.0, 0.2, 3.2, audio=audio))
    else:
        e.get_track("v1").clips.append(fx._clip(src, "base", 0.0, 0.0, 3.4, audio={"mute": True}))
        e.get_track(lane).clips.append(fx._clip(src, "x", 0.2, 0.5, 3.0, audio=audio))
    e.recompute_duration()
    return e


CASES = [(p, "v1") for p in V.PRESET_IDS] + [("echo", "vo"), ("chipmunk", "v2"), ("telephone", "music")]


@pytest.fixture(scope="module")
def vfx_cases(vfx_root) -> dict:
    out = {}
    for preset, lane in CASES:
        name = f"vfx_{preset}_{lane}"
        e = _edl(vfx_root["src"], preset, lane)
        server = fx.server_render(e, 30, vfx_root["root"] / "cache")
        dry = fx.server_render(_edl(vfx_root["src"], None, lane), 30, vfx_root["root"] / "cache")
        fx.write_case(vfx_root["root"], name, e, {vfx_root["src"]: vfx_root["key"]}, (0, len(server)))
        out[name] = (preset, lane, server, dry)
    return out


@pytest.fixture(scope="module")
def server(vfx_root, vfx_cases, bundle):  # noqa: F811 — overrides test_wk_audio's for this module
    s = PageServer({"pages": PAGES, "testkit": bundle, "media": vfx_root["root"],
                    "media/proxies": vfx_root["wd"] / "proxies"})
    yield s
    s.close()


@pytest.fixture(scope="module", params=ENGINES)
def browser(request, server, tmp_path_factory):  # noqa: F811
    from .harness import wk_unavailable_reason
    if request.param == "wk" and wk_unavailable_reason():
        pytest.skip(f"wk: {wk_unavailable_reason()}")
    r = Runner(server, request.param, tmp_path_factory.mktemp(f"vfx-{request.param}"))
    yield r
    r.close()


def _blocks(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-50 ms spectral centroid (Hz) and level (dB) of the mono sum."""
    m = x[:, 0].astype(np.float64) + x[:, 1]
    n = len(m) // BLOCK
    b = m[:n * BLOCK].reshape(n, BLOCK)
    win = np.hanning(BLOCK)
    s = np.abs(np.fft.rfft(b * win, axis=1)) ** 2
    f = np.fft.rfftfreq(BLOCK, 1 / SR)
    cent = (s * f).sum(axis=1) / np.maximum(s.sum(axis=1), 1e-30)
    rms = 10 * np.log10(np.mean(b * b, axis=1) + 1e-20)
    return cent, rms


MEASURED: dict[str, dict] = {}


@pytest.mark.parametrize("preset,lane", CASES, ids=[f"{p}-{lane}" for p, lane in CASES])
def test_voice_effect_parity_with_the_export(browser, vfx_cases, preset, lane):
    name = f"vfx_{preset}_{lane}"
    _p, _l, server, dry = vfx_cases[name]
    r = browser.run("render", case=name, timeout=120)
    client = _pcm(r)
    assert client.shape == server.shape, (client.shape, server.shape)
    assert "voice" in r["plan"]["approx"], r["plan"]["approx"]
    cc, cr = _blocks(client)
    sc, sr = _blocks(server)
    dc, dr = _blocks(dry)
    loud = (sr > QUIET_DB) & (cr > QUIET_DB) & (dr > QUIET_DB)
    loud[:2] = False                           # the clip's attack
    loud[-2:] = False                          # its cut
    c_err = np.abs(cc[loud] / sc[loud] - 1)
    r_err = np.abs(cr[loud] - sr[loud])
    # how far the effect moves the sound (the dry clip against the export)
    effect = max(float(np.mean(np.abs(dc[loud] / sc[loud] - 1))) * 100, float(np.mean(np.abs(dr[loud] - sr[loud]))))
    row = {
        "blocks": int(loud.sum()), "centroid_mean_pct": round(100 * float(c_err.mean()), 4),
        "centroid_max_pct": round(100 * float(c_err.max()), 4), "rms_mean_db": round(float(r_err.mean()), 4),
        "rms_max_db": round(float(r_err.max()), 4), "lag": _lag(client, server),
        "max_abs_diff": float(np.abs(client.astype(np.float64) - server).max()), "effect_size": round(effect, 2),
    }
    MEASURED[f"{browser.engine}:{preset}:{lane}"] = row
    print(name, browser.engine, json.dumps(row))
    bound = parity_table()[preset]
    assert row["blocks"] > 20
    # the effect is there: the client is far nearer the wet export than the
    # dry clip is (the measure means something)
    assert row["centroid_mean_pct"] + row["rms_mean_db"] < 0.5 * row["effect_size"], row
    assert row["centroid_mean_pct"] <= bound["centroidPct"], row
    assert row["rms_mean_db"] <= bound["rmsDb"], row
    if preset not in PITCHED:
        # everything but the pitch shifters runs the export's own arithmetic
        assert abs(row["lag"]) <= 1, row
        assert row["max_abs_diff"] <= 3e-5, row
    if bound["mode"] == "EXACT":
        # the P1-A2 EXACT budget, every block
        assert row["rms_max_db"] <= 0.25, row
    assert (bound["mode"] == "APPROX") == (preset in PITCHED or preset == "reverb")


def test_the_parity_table_covers_every_preset():
    assert set(parity_table()) == set(V.PRESET_IDS)
