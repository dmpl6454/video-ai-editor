"""Instant-preview SOUND acceptance (INSTANT_PREVIEW_SPEC §3.6, §6 R9/R10,
§8.4, §13 P1-A1, P1-A2), in real WKWebView (marker ``wk``) and in Chromium
(Playwright's headless shell) — the same page, bundled from the shipped
modules (``frontend/src/lib/preview/testkit/wkAudioPage.ts``).

* the proxy FLAC the app serves, decoded by the browser through
  ``audio/audioChunks``, is ffmpeg's decode of the same chunk bit for bit,
  and within one 24-bit step of ffmpeg's float decode of the master;
* P1-A1: the offline mix of cuts on and off the frame grid, a gap and a
  reversed clip plays EVERY output sample from exactly the source sample the
  server's render plays (sample-counter sources), each click at S(k), each
  clip boundary on the samples_for_frames running sums;
* P1-A2: the offline mix of a timeline with gain, gain_env, fades, mute,
  solo, channel modes, a seam, a varispeed clip, a music bed, a voice-over
  and an audio lane against the server's ``_audio_only_graph`` render:
  per-50 ms RMS within 0.25 dB where the feature is EXACT (1 dB where it is
  APPROX), cross-correlation lag 0 ± 1 sample;
* the live sink (WK): output sample S(k) lands on the anchor's context frame
  exactly, across refills; stop is silent after its 5 ms ramp and suspends;
  a restart re-anchors; a gain edit is heard within 100 ms.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from video_ai_editor.edl import timebase as tb
from video_ai_editor.render.frame_map import SourceInfo, audio_placements, build_program_map

from . import audio_fixture as fx
from .harness import PageServer, WKHarness, wk_unavailable_reason

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PAGES = Path(__file__).resolve().parent / "pages"
ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkAudioPage.ts"
CHROMIUM = Path(os.environ.get(
    "VAI_CHROMIUM", str(Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1243/"
                        "chrome-headless-shell-mac-arm64/chrome-headless-shell")))
SR = 48000
BLOCK = 2400                      # 50 ms
EXACT_DB = 0.25
APPROX_DB = 1.0
QUIET_DB = -70.0


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def audio_root(tmp_path_factory) -> dict:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    root = tmp_path_factory.mktemp("wk-audio")
    src = root / "src"
    src.mkdir()
    wd = root / "wd"
    wd.mkdir()
    r30, r2997 = Fraction(30), Fraction(30000, 1001)
    S = {
        "counter30": fx.counter_source(src / "counter30.mov", r30, 12),
        "counter2997": fx.counter_source(src / "counter2997.mov", r2997, 12),
        "click30": fx.click_source(src / "click30.mov", r30, 6),
        "click2997": fx.click_source(src / "click2997.mov", r2997, 6),
        "toneA": fx.tone_source(src / "toneA.mp4", 7, 440, 660),
        "toneB": fx.tone_source(src / "toneB.mp4", 7, 550, 330),
        "bed": fx.tone_source(src / "bed.m4a", 8, 220, 277, video=False, amp=0.3),
        "voice": fx.tone_source(src / "voice.m4a", 6, 880, 990, video=False, amp=0.25),
    }
    twins = {n: str(fx.lossless_twin(S[n], src / f"{n}.twin.mov")) for n in ("toneA", "toneB", "bed", "voice")}
    hot = src / "hot.mov"                         # peaks over full scale: chunk headroom gain
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc=exprs='1.7*sin(2*PI*100*t)|0.4*sin(2*PI*150*t)':s={SR}:d=6",
                    "-c:a", "pcm_f32le", str(hot)], check=True, capture_output=True)
    S["hot"] = hot
    # Streams that start apart (gate RX finding 2): the proxy is on the file clock.
    S["alate2997"] = fx.offset_source(src / "alate2997.mov", r2997, 12, audio_late=0.1)
    S["vlate2997"] = fx.offset_source(src / "vlate2997.mov", r2997, 12, video_late=0.1)
    keys: dict[str, tuple[str, dict]] = {}
    ref = root / "ref"
    ref.mkdir()
    for name, path in S.items():
        key, idx, info = fx.build_proxy_audio(path, wd)
        keys[name] = (key, info)
        fx.decode_master_f32(path).astype("<f4").tofile(ref / f"{key}.master.f32")
        fx.decode_chunks_f32(wd, key, idx).astype("<f4").tofile(ref / f"{key}.flac.f32")
    return {"root": root, "wd": wd, "src": {k: str(v) for k, v in S.items()}, "keys": keys,
            "twins": {str(S[n]): t for n, t in twins.items()}}


@pytest.fixture(scope="module")
def cases(audio_root) -> dict:
    """name → (edl, fps, server render (n,2), server render of the AAC
    sources themselves (n,2) or None). The reference of a timeline over AAC
    sources is the server's render of the same timeline over their lossless
    twins (fx.lossless_twin)."""
    root, S, K = audio_root["root"], audio_root["src"], audio_root["keys"]
    cache = root / "cache"
    out = {}

    def render(edl, fps, name, loud):
        from video_ai_editor.render.audio_mix import PreviewLoudness, preview_loudness_scope
        if loud is None:
            return fx.server_render(edl, fps, cache)
        with preview_loudness_scope(PreviewLoudness(loud, root / f"{name}.meas")):
            return fx.server_render(edl, fps, cache)

    def add(name, edl, fps, *, loud=None):
        used = {c.src for t in edl.tracks for c in t.clips if hasattr(c, "src")}
        twins = {s: t for s, t in audio_root["twins"].items() if s in used}
        server = render(fx.with_paths(edl, twins), fps, name, loud)
        server_aac = render(edl, fps, name, loud) if twins else None
        srcs = {s: K[n] for n, s in S.items() if s in used}
        fx.write_case(root, name, edl, srcs, (0, len(server)), loudness_gain_db=loud)
        out[name] = (edl, fps, server, server_aac)

    for label, rate in (("30", Fraction(30)), ("2997", Fraction(30000, 1001))):
        e, fps = fx.placement_edl(S[f"counter{label}"], S[f"click{label}"], rate)
        add(f"placement{label}", e, fps)
        if label == "30":
            add("placement30_cut", fx.ripple_delete(e, "c1", fps), fps)
    add("placement_offset", *fx.offset_edl(S["alate2997"], S["vlate2997"], Fraction(30000, 1001)))
    mix = dict(tone_a=S["toneA"], tone_b=S["toneB"], bed=S["bed"], voice=S["voice"])
    add("mix", *fx.mix_edl(**mix))
    add("mix_solo", *fx.mix_edl(**mix, solo="vo"))
    add("mix_duck", *fx.mix_edl(**mix, duck=True))
    e, fps = fx.mix_edl(**mix, loudness=-14.0)
    add("mix_loud", e, fps, loud=-4.5)
    add("mix_curve", *fx.curve_edl(S["toneA"], S["toneB"], S["bed"]))
    add("pip_speed", *fx.pip_speed_edl(S["toneA"], S["toneB"]))
    add("mix_hot", *fx.hot_edl(S["counter30"], S["counter2997"]))
    return out


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    if not exe.exists():
        pytest.skip("frontend/node_modules not installed (npm install)")
    out = tmp_path_factory.mktemp("wk-audio-bundle")
    subprocess.run([str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16",
                    f"--outfile={out / 'wkAudioPage.js'}", "--log-level=warning"],
                   check=True, cwd=FRONTEND, capture_output=True, text=True)
    return out


@pytest.fixture(scope="module")
def server(audio_root, cases, bundle):
    s = PageServer({"pages": PAGES, "testkit": bundle, "media": audio_root["root"],
                    "media/proxies": audio_root["wd"] / "proxies"})
    yield s
    s.close()


class Runner:
    def __init__(self, server: PageServer, engine: str, work: Path):
        self.server, self.engine, self.work = server, engine, work
        self._wk = WKHarness(server, work) if engine == "wk" else None

    def run(self, scenario: str, timeout: float = 90, **query) -> dict:
        q = {"scenario": scenario, **query}
        if self._wk is not None:
            r = self._wk.run("pages/audio.html", q, timeout=timeout).result
            assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"], r["ua"]
            return r
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            if self.engine == "chromium":
                b = p.chromium.launch(executable_path=str(CHROMIUM),
                                      args=["--autoplay-policy=no-user-gesture-required"])
            else:
                b = p.webkit.launch()
            try:
                page = b.new_page()
                page.goto(self.server.url("pages/audio.html", {**q, "token": f"pw{int(time.time() * 1e6)}"}))
                page.wait_for_function("window.__result !== undefined", timeout=timeout * 1000)
                r = page.evaluate("window.__result")
            finally:
                b.close()
        if r.get("fatal"):
            raise AssertionError(f"{scenario} failed in {self.engine}: {r['fatal']}")
        return r

    def close(self):
        if self._wk is not None:
            self._wk.close()


def _chromium_reason() -> str | None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "playwright not installed"
    return None if CHROMIUM.exists() else f"no headless Chromium at {CHROMIUM}"


ENGINES = [
    pytest.param("wk", marks=pytest.mark.wk, id="wk"),
    pytest.param("chromium", marks=pytest.mark.skipif(_chromium_reason() is not None, reason=str(_chromium_reason())),
                 id="chromium"),
    # Playwright's WebKit build: a second WebKit, not the product's (WK is).
    pytest.param("webkit", marks=pytest.mark.skipif(_chromium_reason() == "playwright not installed",
                                                    reason="playwright not installed"), id="pw-webkit"),
]


@pytest.fixture(scope="module", params=ENGINES)
def browser(request, server, tmp_path_factory):
    if request.param == "wk" and wk_unavailable_reason():
        pytest.skip(f"wk: {wk_unavailable_reason()}")
    r = Runner(server, request.param, tmp_path_factory.mktemp(f"runs-{request.param}"))
    yield r
    r.close()


def _pcm(r: dict) -> np.ndarray:
    L = np.frombuffer(base64.b64decode(r["L"]), dtype="<f4")
    R = np.frombuffer(base64.b64decode(r["R"]), dtype="<f4")
    return np.stack([L, R], axis=1)


# ------------------------------------------------------------------ engine facts

def test_engine_facts_the_mix_relies_on(browser):
    """The limiter's look-ahead is the 288 samples the live graph schedules
    early by (and the offline render trims), and with its makeup undone it is
    transparent below the ceiling; cancelAndHoldAtTime and value curves exist;
    a 48 kHz AudioContext can be made."""
    r = browser.run("probe")
    print(json.dumps({k: r[k] for k in ("limiterLatency", "limiterSineGainDb", "audioContext48k")}))
    assert r["limiterLatency"] == {"0": r["expectedLatency"], "-1": r["expectedLatency"]}
    assert all(abs(v) < 0.01 for v in r["limiterSineGainDb"].values()), r["limiterSineGainDb"]
    assert r["cancelAndHold"] and r["valueCurve"] and r["offline"]
    assert r["audioContext48k"] is True


# ------------------------------------------------------------------ FLAC

def test_proxy_flac_decodes_sample_exact(browser, audio_root):
    """Every chunk of every source, decoded by the browser's own FLAC decoder
    via AudioChunks: equal to ffmpeg's decode of the same chunk (× its
    headroom gain) bit for bit, and within one 24-bit step (× gain) of
    ffmpeg's float decode of the master — no offset, no missing sample."""
    keys = {n: k for n, (k, _i) in audio_root["keys"].items()}
    r = browser.run("flac", keys=",".join(keys.values()), timeout=120)
    rows = {row["key"]: row for row in r["keys"]}
    for name, key in keys.items():
        row = rows[key]
        assert row["samples"] == row["checked"] == row["masterSamples"] == row["refSamples"], (name, row)
        assert row["maxDiffVsFfmpegFlac"] == 0.0, (name, row)
        assert row["maxDiffVsMaster"] <= row["maxGain"] * 2.0 ** -23, (name, row)
    assert rows[keys["hot"]]["maxGain"] == 2.0 and rows[keys["hot"]]["maxAbs"] > 1.6   # the over survived
    print(json.dumps({n: {k: rows[key][k] for k in ("samples", "maxDiffVsMaster", "decodeMs")}
                      for n, key in keys.items()}))


# ------------------------------------------------------------------ P1-A1

@pytest.mark.parametrize("name", ["placement30", "placement2997"])
def test_p1_a1_every_sample_where_the_server_puts_it(browser, cases, audio_root, name):
    edl, fps, server, _aac = cases[name]
    r = browser.run("render", case=name, timeout=120)
    client = _pcm(r)
    assert client.shape == server.shape, (client.shape, server.shape)
    got, want = fx.decode_counter(client), fx.decode_counter(server)
    # Every output sample is the same SOURCE sample (counters decode equal)
    # and the same value — to the float residue of a zero, far below one
    # 24-bit step (the clicks are compared as audio here too).
    bad = np.nonzero(got != want)[0]
    assert len(bad) == 0, f"{len(bad)} samples differ, first at {bad[:5]}: client {got[bad[:5]]} server {want[bad[:5]]}"
    dev = np.abs(client.astype(np.float64) - server)
    worst = int(np.argmax(dev.max(axis=1)))
    assert dev.max() <= 2.0 ** -24, (f"max |client - server| {dev.max():.3g} at {worst}: "
                                     f"client {client[worst]} server {server[worst]}")

    # Placement against the model, and the S(k) grid.
    K = audio_root["keys"]
    info = {s: SourceInfo.from_json(K[n][1]) for n, s in audio_root["src"].items() if n in K}
    pm = build_program_map(edl, info)
    places = audio_placements(edl, pm, sources=info)
    starts = sorted(a.out0 for a in places)
    frames_at = [tb.samples_for_frames(k, fps) for k in range(pm.total + 1)]
    for a in places:
        c = pm.clips[a.clip]
        assert a.out0 in frames_at and (a.out0 + a.n) in frames_at, (c.id, a.out0, a.n)
        if "counter" in c.src:
            exp = np.full(a.n, -1, dtype=np.int64)
            for off, cnt, first, d in a.runs:
                exp[off:off + cnt] = first + d * np.arange(cnt)
            assert np.array_equal(got[a.out0:a.out0 + a.n], exp), c.id
        else:
            # The click source: one click per source frame, where the model
            # puts that source sample — and on the output grid S(k): exactly
            # at an integer number of samples per frame; within one sample at
            # 29.97, where the source's own frame starts and the program's
            # round independently (the export's placement, sample for sample).
            (off, cnt, first, d), = a.runs
            seg = client[a.out0:a.out0 + a.n, 0]
            clicks = a.out0 + np.nonzero(np.abs(seg) > 0.25)[0]
            src_rate = Fraction(30) if "click30" in c.src else Fraction(30000, 1001)
            src_clicks = [tb.samples_for_frames(j, src_rate) for j in range(4000)]
            model = [a.out0 + (s - first) for s in src_clicks if first <= s < first + cnt]
            assert list(clicks) == model, (c.id, list(clicks)[:5], model[:5])
            k0 = frames_at.index(a.out0)
            grid = frames_at[k0:frames_at.index(a.out0 + a.n)]
            slack = 0 if (SR / tb.rate_of(fps)).denominator == 1 else 1
            assert len(clicks) == len(grid)
            assert max(abs(int(x) - g) for x, g in zip(clicks, grid)) <= slack, (c.id, list(clicks)[:4], grid[:4])
    assert starts[0] == 0 and len(places) == 7
    # The gap: exact silence.
    assert r["plan"]["total"] == len(server)


def test_p1_a1_a_stream_that_starts_late_plays_on_the_file_clock(browser, cases, audio_root):
    """Audio starting 0.1 s after the file (and the mirror, video late), cut
    near the head and mid-file: the client plays every sample the server
    does — the proxy is decoded on the file clock like every render chain
    (was 4800 samples off on 32032 of 32032 per clip, gate RX finding 2)."""
    edl, fps, server, _aac = cases["placement_offset"]
    client = _pcm(browser.run("render", case="placement_offset", timeout=120))
    assert client.shape == server.shape, (client.shape, server.shape)
    got, want = fx.decode_counter(client), fx.decode_counter(server)
    bad = np.nonzero(got != want)[0]
    assert len(bad) == 0, f"{len(bad)} samples differ, first at {bad[:5]}: client {got[bad[:5]]} server {want[bad[:5]]}"
    assert np.abs(client.astype(np.float64) - server).max() <= 2.0 ** -24
    K = audio_root["keys"]
    info = {s: SourceInfo.from_json(K[n][1]) for n, s in audio_root["src"].items() if n in K}
    pm = build_program_map(edl, info)
    places = audio_placements(edl, pm, sources=info)
    assert len(places) == 4
    for a in places:
        c = pm.clips[a.clip]
        (off, _cnt, first, d), = a.runs
        lead = 4800 if "alate" in c.src else 0      # the audio stream's own sample 0 is file time 0.1
        assert off == 0 and d == 1 and got[a.out0] == first - lead, (c.id, int(got[a.out0]), first)


def test_offline_render_is_deterministic(browser, cases):
    """The same offline mix 30 times, bit for bit. (A disconnect() from a
    source's `ended` handler once raced WebKit's offline render thread and
    silenced other sources from a random quantum on: 3 of 25 renders.)"""
    r = browser.run("render_repeat", case="placement30", n=30, timeout=200)
    assert r["diffs"] == [], r["diffs"]


# ------------------------------------------------------------------ P1-A2

def _block_db(x: np.ndarray) -> np.ndarray:
    n = len(x) // BLOCK
    b = x[:n * BLOCK].reshape(n, BLOCK, 2).astype(np.float64)
    rms = np.sqrt(np.mean(b * b, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-12))


#: Server output this close to full scale is where its limiter may be at work
#: (alimiter auto-levels its 0.97 ceiling to 1.0).
NEAR_CEILING = 0.9


def _limiting_samples(plan: dict, n: int) -> np.ndarray:
    m = np.zeros(n, dtype=bool)
    for a, b in plan.get("limiting", []):
        m[a:b] = True
    return m


def _approx_blocks(plan: dict, n_blocks: int, server: np.ndarray | None = None) -> np.ndarray:
    """Blocks touched by an APPROX feature (a resampled clip ± its block; the
    whole programme for a master-level approximation; the bed under a duck;
    gate RX: a block of a `limiting` range where the server's sound is near
    full scale — elsewhere in a range the plan flags (its peak bound is
    conservative) both limiters are transparent and the block stays EXACT)."""
    mask = np.zeros(n_blocks, dtype=bool)
    if "loudness" in plan["approx"]:
        mask[:] = True
    if server is not None and plan.get("limiting"):
        near = (np.abs(server).max(axis=1) > NEAR_CEILING) & _limiting_samples(plan, len(server))
        hot = np.nonzero(near)[0] // BLOCK
        mask[np.clip(hot, 0, n_blocks - 1)] = True
    for c in plan["clips"]:
        if not c["exact"] or ("duck" in plan["approx"] and c["bus"] == "music"):
            a = max(0, c["out0"] // BLOCK - 1)
            b = min(n_blocks, (c["out0"] + c["n"]) // BLOCK + 2)
            mask[a:b] = True
    return mask


def _lag(client: np.ndarray, server: np.ndarray, max_lag: int = 64) -> int:
    a = client[:, 0].astype(np.float64) + client[:, 1]
    b = server[:, 0].astype(np.float64) + server[:, 1]
    best, arg = -np.inf, 0
    core = slice(max_lag, len(a) - max_lag)
    for lag in range(-max_lag, max_lag + 1):
        v = float(np.dot(a[core], b[max_lag + lag:len(b) - max_lag + lag]))
        if v > best:
            best, arg = v, lag
    return arg


@pytest.mark.parametrize("name", ["mix", "mix_solo", "mix_duck", "mix_loud", "mix_curve", "pip_speed", "mix_hot"])
def test_p1_a2_mix_parity_with_the_server_render(browser, cases, name):
    edl, fps, server, aac = cases[name]
    r = browser.run("render", case=name, timeout=120)
    client = _pcm(r)
    assert client.shape == server.shape, (client.shape, server.shape)
    cdb, sdb = _block_db(client), _block_db(server)
    approx = _approx_blocks(r["plan"], len(cdb), server)
    loud = (cdb > QUIET_DB) | (sdb > QUIET_DB)
    diff = np.abs(cdb - sdb)
    tol = np.where(approx, APPROX_DB, EXACT_DB)[:, None]
    bad = np.argwhere(loud & (diff > tol))
    report = {
        "blocks": len(cdb), "approx_blocks": int(approx.sum()), "approx": r["plan"]["approx"],
        "max_exact_db": float(diff[~approx][loud[~approx]].max(initial=0)),
        "max_approx_db": float(diff[approx][loud[approx]].max(initial=0)),
        "lag": _lag(client, server),
        "limiting": r["plan"].get("limiting"), "server_peak": float(np.abs(server).max()),
    }
    if aac is not None:
        report["vs_aac_render"] = _vs_aac_render(client, aac, server)
    print(name, browser.engine, json.dumps(report))
    assert len(bad) == 0, (f"{len(bad)} blocks off: " + ", ".join(
        f"t={b * BLOCK / SR:.2f}s ch{c} client {cdb[b, c]:.2f} server {sdb[b, c]:.2f}" for b, c in bad[:8]))
    assert abs(report["lag"]) <= 1, report


def test_p1_a2_the_limiter_is_approx_exactly_where_it_works(browser, cases):
    """Gate RX finding 3: over the ceiling the browser's limiter departs from
    alimiter (|Δ| up to 0.25), so every sample where client and server part
    lies inside a `limiting` range of the plan (which named no such range
    before: approx was empty). Outside those ranges they agree within 1e-4
    (the gate's own threshold; the compressor below its threshold is not
    bit-transparent — up to 2.8e-5 on a 0.5 sample, −0.0005 dB, in its first
    10 ms)."""
    edl, fps, server, _aac = cases["mix_hot"]
    r = browser.run("render", case="mix_hot", timeout=120)
    client = _pcm(r)
    assert "limiting" in r["plan"]["approx"] and r["plan"]["limiting"], r["plan"]["approx"]
    inside = _limiting_samples(r["plan"], len(server))
    d = np.abs(client.astype(np.float64) - server).max(axis=1)
    assert d[inside].max() > 1e-3, "the case no longer drives the limiter"
    assert d[~inside].max(initial=0) <= 1e-4, (int(np.argmax(np.where(inside, 0, d))), float(d[~inside].max()))


def _vs_aac_render(client: np.ndarray, aac: np.ndarray, twin: np.ndarray) -> dict:
    """Measured, not asserted: the server's render over the AAC masters
    themselves differs from its render over their exact decode (the twins)
    — an input `-ss` into AAC decodes its first ~650 samples wrong (a
    voice-over clip's head: 0.18 absolute), and `aresample=async=1` nudges
    AAC frame timestamps elsewhere (≤ 0.015). The export has both; the
    client plays the true samples. Blocks of the client over the 0.25 dB
    budget against that render are reported here."""
    cdb, adb = _block_db(client), _block_db(aac)
    loud = (cdb > QUIET_DB) | (adb > QUIET_DB)
    over = np.argwhere(loud & (np.abs(cdb - adb) > EXACT_DB))
    return {"max_abs_aac_vs_twin": float(np.abs(aac.astype(np.float64) - twin).max()),
            "blocks_over_budget": sorted({round(int(b) * BLOCK / SR, 2) for b, _c in over})}


# ------------------------------------------------------------------ live sink (WK)

@pytest.mark.wk
def test_live_sink_anchor_stop_restart_and_edits(server, cases, tmp_path, audio_root):
    """The AudioSink on a real AudioContext in WKWebView, tapped at its
    output: S(k0) sounds on the anchor's context frame and every later sample
    follows sample for sample across the 1 s refills; stop() is silent after
    its 5 ms ramp and suspends the context; start() again re-anchors; a
    ripple delete while playing (reschedule from ≈ 6 frames ahead) plays the
    NEW program sample for sample after its 5 ms cross-fade; a gain edit
    while playing is heard within 100 ms; start() while running re-anchors
    (5 ms cross-fade) and plays on the new anchor sample for sample; a
    context suspended under the sink (a pause nobody issued) stops it and is
    reported, and the next start() is exact again."""
    if wk_unavailable_reason():
        pytest.skip(wk_unavailable_reason())
    runner = Runner(server, "wk", tmp_path / "live")
    try:
        r = runner.run("live", case="placement30", case2="placement30_cut", timeout=60)
    finally:
        runner.close()
    L = np.frombuffer(base64.b64decode(r["L"]), dtype="<f4")
    first = r["firstFrame"]
    ev = {e["ev"]: e for e in r["events"]}
    assert r["sampleRate"] == SR
    ref = cases["placement30"][2][:, 0]
    ref_cut = cases["placement30_cut"][2][:, 0]

    def off_anchor(anchor: dict, a_frame: int, b_frame: int, program: np.ndarray) -> tuple[int, np.ndarray]:
        """Tap frames [a, b) against program samples on `anchor`: (count, bad offsets)."""
        base = round(anchor["at"] * SR)
        a, b = max(a_frame, base), b_frame
        s0 = anchor["sample"] + (a - base)
        n = min(b - a, len(program) - s0)
        got, want = L[a - first:a - first + n], program[s0:s0 + n]
        # Same sample, to a start-time residue (≈1e-12 of a sample leaks
        # 4.5e-13 of a click into the sample before it) far below 24 bits.
        return n, np.nonzero(np.abs(got.astype(np.float64) - want) > 2.0 ** -24)[0]

    stop_frame = round(ev["stop"]["at"] * SR)
    n, bad = off_anchor(ev["start"], 0, stop_frame, ref)
    b0 = round(ev["start"]["at"] * SR) - first
    assert n > 1.2 * SR and len(bad) == 0, (n, bad[:5], [(L[b0 + i - 1:b0 + i + 2].tolist(),
                                            ref[ev["start"]["sample"] + i - 1:ev["start"]["sample"] + i + 2].tolist()) for i in bad[:3]])
    # Silent from the stop's ramp end (+ one render quantum) to the restart.
    s_from = stop_frame - first + int(0.005 * SR) + 128
    s_to = round(ev["restart"]["at"] * SR) - first - int(0.006 * SR)
    assert s_to > s_from and np.max(np.abs(L[s_from:s_to])) == 0.0
    assert r["suspendedAfterStop"] == "suspended"
    # Restart → the cut: the old program; after the cut's cross-fade: the new.
    base2 = round(ev["restart"]["at"] * SR)
    cut_frame = base2 + ev["cut"]["sample"] - ev["restart"]["sample"]
    edit_frame = round(ev["edit"]["at"] * SR)
    n1, bad1 = off_anchor(ev["restart"], 0, cut_frame, ref)
    assert n1 > 0.2 * SR and len(bad1) == 0, (n1, bad1[:5])
    re_frame = round(ev["reanchor"]["call"] * SR) + int(0.010 * SR)      # the switch: call + the live lead
    n2, bad2 = off_anchor(ev["restart"], cut_frame + int(0.005 * SR), re_frame, ref_cut)
    assert n2 > 0.3 * SR and len(bad2) == 0, (n2, bad2[:5])
    # Re-anchored while running: after its 5 ms cross-fade (+ a quantum of
    # slack for the call's own quantum) every sample is on the NEW anchor.
    ext_frame = round(ev["external"]["at"] * SR)
    n3, bad3 = off_anchor(ev["reanchor"], re_frame + int(0.005 * SR) + 128, ext_frame, ref_cut)
    assert n3 > 0.3 * SR and len(bad3) == 0, (n3, bad3[:5])
    # The context suspended under the sink: it stopped and reported it; the
    # fresh anchor after resume is exact again.
    assert ev["external"]["running"] is False and ev["external"]["interrupted"] == ["suspended"], ev["external"]
    n4, bad4 = off_anchor(ev["resume"], 0, edit_frame, ref_cut)
    assert n4 > 0.3 * SR and len(bad4) == 0, (n4, bad4[:5])
    assert not np.array_equal(ref[ev["cut"]["sample"]:ev["cut"]["sample"] + SR // 2],
                              ref_cut[ev["cut"]["sample"]:ev["cut"]["sample"] + SR // 2])   # the edit changed something
    # The −60 dB edit: 20 ms blocks after the edit that still carry full level.
    e0 = edit_frame - first
    level = [float(np.max(np.abs(L[e0 + i:e0 + i + 960]))) for i in range(0, int(0.3 * SR), 960)]
    loud_until = next((i for i, v in enumerate(level) if v < 0.01), None)
    print(json.dumps({"stats": r["stats"], "edit_blocks_20ms_until_quiet": loud_until,
                      "outputLatency": r["outputLatency"], "baseLatency": r["baseLatency"]}))
    assert loud_until is not None and loud_until * 20 <= 100, level[:8]
