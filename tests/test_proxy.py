"""Preview proxies (wave D, INSTANT_PREVIEW_SPEC §5.1, §6 R5, §13 test_proxy).

Everything here runs the real ffmpeg: frame identity is proven by decoding
proxy frames and reading the barcode each master frame carries, never by
trusting the encoder's frame count.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from video_ai_editor.ingest import proxy as P
from video_ai_editor.ingest.proxy_queue import ProxyManager

from proxy_fixtures import (decode_audio_f32, decode_gray, decode_yuv420, decoded_frame_count,
                            make_barcode_master, make_flat_master, read_barcode, sample_nals,
                            sps_vui, y_psnr)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    from video_ai_editor import storage as _storage
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.setattr(_storage, "WORKDIR", wd)
    return wd


@pytest.fixture
def manager():
    m = ProxyManager()
    yield m
    m.shutdown()
    m.wait_idle(10)


def _build(manager: ProxyManager, src: Path, timeout: float = 120) -> str:
    key = manager.ensure(src)
    assert manager.wait_idle(timeout), "proxy build did not finish"
    return key


def _all_samples(key: str) -> list[bytes]:
    info = P.load_source(key)
    out: list[bytes] = []
    for n in range(info.spans):
        first, samples = P.unpack_span(P.span_path(key, n).read_bytes())
        assert first == P.span_range(info, n)[0]
        out.extend(samples)
    return out


def _avcc(key: str) -> bytes:
    return P.avcc_of(P.init_path(key).read_bytes())


# ---- frame identity ---------------------------------------------------------------

def test_eager_build_every_proxy_frame_is_its_master_frame(workdir, manager, tmp_path):
    """150 frames of a B-frame master (edit list and all): the proxy holds
    exactly the decoded frame count, one IDR per sample, and proxy frame i
    shows master frame i."""
    src = make_barcode_master(tmp_path / "m.mp4", frames=150, rate="30")
    key = _build(manager, src)
    idx = P.live_index(key)
    assert idx["state"] == "ready", idx
    assert idx["frames"] == decoded_frame_count(src) == 150
    assert idx["src_rate"] == {"num": 30, "den": 1}
    assert idx["span_frames"] == 60 and idx["spans"] == 3
    assert (idx["w"], idx["h"]) == (640, 360)
    samples = _all_samples(key)
    assert len(samples) == 150
    for s in samples:                      # all-intra: every sample is an IDR
        types = {nal[0] & 0x1F for nal in sample_nals(s)}
        assert 5 in types and 1 not in types
    frames = decode_gray(_avcc(key), samples, 640, 360)
    assert [read_barcode(f) for f in frames] == list(range(150))


def test_init_segment_is_passthrough_ready(workdir, manager, tmp_path):
    """Timescale 240000 (spec R1), no edit list, High profile level 4.1,
    8-bit 4:2:0, BT.709 limited tags."""
    src = make_barcode_master(tmp_path / "m.mp4", frames=30)
    key = _build(manager, src)
    init = P.init_path(key).read_bytes()
    assert P.mdhd_timescale(init) == P.TIMESCALE == 240000
    assert P.find_box(init, ["moov", "trak", "edts"]) is None
    avcc = _avcc(key)
    assert avcc[1] == 100 and avcc[3] == 41      # High @ 4.1
    assert P.read_index(key)["codec"] == P.codec_string(avcc)
    # The init CLASS key has one definition, shared with the client's
    # fmp4Writer (parseInitSegment().initKey): the avcC bytes, lowercase hex.
    assert P.read_index(key)["init_key"] == avcc.hex()
    # The colour tags live in the SPS VUI: ffprobe a stream rebuilt from it.
    from proxy_fixtures import annexb
    es = tmp_path / "p.h264"
    es.write_bytes(annexb(avcc, _all_samples(key)[:2]))
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=pix_fmt,profile,color_space,color_primaries,color_transfer,color_range",
                          "-of", "default=nw=1", str(es)], capture_output=True, text=True).stdout
    for want in ("pix_fmt=yuv420p", "profile=High", "color_space=bt709",
                 "color_primaries=bt709", "color_transfer=bt709", "color_range=tv"):
        assert want in out, out


def test_on_demand_span_equals_eager_span(workdir, manager, tmp_path):
    """A span encoded on its own (seeked into a long-GOP master) holds the
    same master frames as the eager encode, decodes with the SAME init, and
    is visually the same picture (Y-PSNR >= 50 dB)."""
    src = make_barcode_master(tmp_path / "m.mp4", frames=200, rate="30")
    key = _build(manager, src)
    info = P.load_source(key)
    avcc = _avcc(key)
    eager_first, eager = P.unpack_span(P.span_path(key, 2).read_bytes())
    P.span_path(key, 2).unlink()
    t0 = time.perf_counter()
    got = manager.request_span(key, 2, timeout=10)
    assert got is not None and time.perf_counter() - t0 < 10
    first, ondemand = P.unpack_span(got.read_bytes())
    assert first == eager_first == 120 and len(ondemand) == len(eager) == 60
    assert _avcc(key) == avcc                    # still one init for both
    a = decode_gray(avcc, eager, 640, 360)
    b = decode_gray(avcc, ondemand, 640, 360)
    assert [read_barcode(f) for f in b] == list(range(120, 180))
    assert y_psnr(a, b) >= 50.0
    # The last, short span on its own too.
    P.span_path(key, 3).unlink()
    first, tail = P.unpack_span(manager.request_span(key, 3, timeout=10).read_bytes())
    assert (first, len(tail)) == (180, 20)
    assert [read_barcode(f) for f in decode_gray(avcc, tail, 640, 360)] == list(range(180, 200))
    assert info.frames == 200


def test_first_span_of_a_fresh_import_is_fast(workdir, manager, tmp_path):
    """Spec §4.4/§11.4: the span under the playhead of a never-proxied 1080p
    source is ready in well under the 2 s route wait (target ~0.5 s)."""
    src = make_barcode_master(tmp_path / "hd.mp4", frames=240, rate="30", w=1920, h=1080,
                              audio=False)
    key = manager.ensure(src, eager=False, probe_timeout=5)
    t0 = time.perf_counter()
    p = manager.request_span(key, 2, timeout=5)
    dt = time.perf_counter() - t0
    assert p is not None
    first, samples = P.unpack_span(p.read_bytes())
    assert first == 120 and len(samples) == 60
    frames = decode_gray(_avcc(key), samples, 1280, 720)
    assert [read_barcode(f, 1080) for f in frames] == list(range(120, 180))
    print(f"on-demand 1080p span: {dt * 1000:.0f} ms")
    assert dt < 2.0


# ---- stitchability, rates, sizes, pixel formats --------------------------------------

def test_same_size_proxies_share_one_avcc(workdir, manager, tmp_path):
    """Two different sources of one size and rate: byte-identical avcC, so
    their spans interleave under ONE init. Another rate changes only the VUI
    timing, and so the init_key the client switches on."""
    a = make_barcode_master(tmp_path / "a.mp4", frames=30, rate="30")
    b = make_barcode_master(tmp_path / "b.mp4", frames=45, rate="30", pix_fmt="yuv422p",
                            profile="high422")
    c = make_barcode_master(tmp_path / "c.mp4", frames=25, rate="25")
    ka, kb, kc = (_build(manager, s) for s in (a, b, c))
    assert _avcc(ka) == _avcc(kb)
    assert P.read_index(ka)["init_key"] == P.read_index(kb)["init_key"]
    assert P.read_index(kc)["init_key"] != P.read_index(ka)["init_key"]
    # Interleave: a's frames then b's under a's init decode to the right bars.
    mixed = _all_samples(ka)[:10] + _all_samples(kb)[20:30]
    bars = [read_barcode(f) for f in decode_gray(_avcc(ka), mixed, 640, 360)]
    assert bars == list(range(10)) + list(range(20, 30))


@pytest.mark.parametrize("rate, span, want", [
    ("25", 50, (25, 1)),
    ("24000/1001", 48, (24000, 1001)),
    ("60", 120, (60, 1)),
])
def test_proxy_keeps_the_source_rate(workdir, manager, tmp_path, rate, span, want):
    src = make_barcode_master(tmp_path / "r.mp4", frames=130, rate=rate, audio=False)
    key = _build(manager, src)
    idx = P.read_index(key)
    assert (idx["src_rate"]["num"], idx["src_rate"]["den"]) == want
    assert idx["span_frames"] == span
    assert len(_all_samples(key)) == 130


@pytest.mark.parametrize("w, h, pw, ph", [(1920, 1080, 1280, 720), (1080, 1920, 720, 1280),
                                          (480, 270, 480, 270)])
def test_short_edge_is_720_and_never_upscaled(workdir, manager, tmp_path, w, h, pw, ph):
    src = make_barcode_master(tmp_path / "s.mp4", frames=12, w=w, h=h, audio=False)
    key = _build(manager, src)
    idx = P.read_index(key)
    assert (idx["w"], idx["h"]) == (pw, ph)
    frames = decode_gray(_avcc(key), _all_samples(key), pw, ph)
    assert [read_barcode(f, h) for f in frames] == list(range(12))


def test_10bit_444_master_gets_an_8bit_420_proxy_and_is_untouched(workdir, manager, tmp_path):
    """Spec N5: the 10-bit / 4:4:4 masters stay as they are; the proxy is
    what WebKit can decode (8-bit 4:2:0 High)."""
    src = make_barcode_master(tmp_path / "hi.mp4", frames=40, pix_fmt="yuv444p10le",
                              profile="high444")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    key = _build(manager, src)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    idx = P.read_index(key)
    assert idx["src_pix_fmt"] == "yuv444p10le"
    avcc = _avcc(key)
    assert avcc[1] == 100                        # High, not High 4:4:4 (244)
    bars = [read_barcode(f) for f in decode_gray(avcc, _all_samples(key), 640, 360)]
    assert bars == list(range(40))


# ---- audio -------------------------------------------------------------------------------

def test_flac_chunks_are_the_masters_own_pcm_sample_exact(workdir, manager, tmp_path):
    """Chunks of exactly 240000 samples (the last short) that concatenate to
    ffmpeg's own decode of the master, to 24-bit precision."""
    src = make_barcode_master(tmp_path / "au.mp4", frames=30, audio_seconds=12.0)
    key = _build(manager, src)
    idx = P.read_index(key)
    ref = decode_audio_f32(src)
    a = idx["audio"]
    assert a["silent"] is False and a["samples"] == len(ref)
    assert a["chunks"] == 3
    parts = []
    for n in range(a["chunks"]):
        data, rate = sf.read(P.chunk_path(key, n), dtype="float32", always_2d=True)
        info = sf.info(P.chunk_path(key, n))
        assert rate == 48000 and data.shape[1] == 2 and info.subtype == "PCM_24"
        assert len(data) == (240000 if n < 2 else len(ref) - 480000)
        parts.append(data)
    got = np.concatenate(parts)
    assert got.shape == ref.shape
    assert float(np.max(np.abs(got - np.clip(ref, -1, 1)))) <= 2.0 ** -22


def test_silent_master_has_no_chunks(workdir, manager, tmp_path):
    src = make_barcode_master(tmp_path / "mute.mp4", frames=20, audio=False)
    key = _build(manager, src)
    idx = P.live_index(key)
    assert idx["audio"]["silent"] is True and idx["audio"]["chunks"] == 0
    assert idx["state"] == "ready"
    assert not list((P.proxy_dir(key) / "a").glob("*.flac"))


def test_audio_only_source_is_a_chunks_only_proxy(workdir, manager, tmp_path):
    src = tmp_path / "bed.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=500:sample_rate=44100:duration=6", "-c:a", "aac", str(src)],
                   check=True)
    key = _build(manager, src)
    idx = P.live_index(key)
    assert idx["frames"] == 0 and idx["has_video"] is False and idx["spans"] == 0
    assert idx["audio"]["chunks"] == 2 and idx["state"] == "ready"
    assert idx["audio"]["samples"] == len(decode_audio_f32(src))


# ---- identity, failure, cancellation, export pause, budget ------------------------------------

def test_key_follows_file_identity(workdir, tmp_path):
    src = make_barcode_master(tmp_path / "k.mp4", frames=5, audio=False)
    k1 = P.proxy_key(src)
    assert P.is_valid_key(k1) and P.proxy_key(src) == k1
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert P.proxy_key(src) != k1


def test_recipe_crf_is_24_and_part_of_the_key(workdir, tmp_path, monkeypatch):
    """Spec §15.1 settled at crf 24 (measured); a recipe change re-keys."""
    src = make_barcode_master(tmp_path / "c.mp4", frames=5, audio=False)
    assert P.crf() == 24 and P.recipe()["crf"] == 24
    k24 = P.proxy_key(src)
    monkeypatch.setenv("VAI_PROXY_CRF", "23")
    assert P.crf() == 23 and P.proxy_key(src) != k24
    assert "-crf" in (args := P.encode_args(P.probe_source(src), 0, 5)) and \
        args[args.index("-crf") + 1] == "23"


def test_frame_count_mismatch_fails_the_proxy(workdir, manager, tmp_path):
    """If the encode does not yield exactly the pts table's frames the proxy
    is failed (degraded tier), never silently shifted."""
    src = make_barcode_master(tmp_path / "bad.mp4", frames=60, audio=False)
    key = P.proxy_key(src)
    info = P.probe_source(src, key)
    info.pts = info.pts[:-5]                     # claim 55 frames; ffmpeg gives 60
    P.save_source(info)
    P.write_index(key, P.static_index(info))
    assert manager.ensure(src) == key
    assert manager.wait_idle(60)
    idx = P.live_index(key)
    assert idx["state"] == "failed" and "frame" in idx["error"]


def _wait_for(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _ffmpeg_children() -> int:
    out = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "ffmpeg"],
                         capture_output=True, text=True).stdout
    return len(out.split())


def test_cancel_stops_the_encode_and_a_later_ensure_resumes(workdir, manager, tmp_path):
    src = make_barcode_master(tmp_path / "long.mp4", frames=900, rate="30", w=960, h=540,
                              audio=False)
    key = manager.ensure(src)
    assert _wait_for(lambda: P.span_path(key, 0).is_file())
    assert manager.cancel(src) is True
    assert manager.wait_idle(20)
    info = P.load_source(key)
    done = [n for n in range(info.spans) if P.span_path(key, n).is_file()]
    assert 0 < len(done) < info.spans, "cancel did not stop the build"
    assert _wait_for(lambda: _ffmpeg_children() == 0, 5)
    assert not list(P.proxy_dir(key).rglob(".*.part"))
    # Resume: only the missing spans are encoded, and the result is complete.
    manager.ensure(src)
    assert manager.wait_idle(180)
    assert P.live_index(key)["state"] == "ready"
    assert len(_all_samples(key)) == 900


def test_eager_builds_pause_during_an_export(workdir, manager, tmp_path):
    src = make_barcode_master(tmp_path / "exp.mp4", frames=900, rate="30", w=960, h=540,
                              audio=False)
    key = manager.ensure(src)
    assert _wait_for(lambda: P.span_path(key, 0).is_file())
    with manager.export_in_progress():
        time.sleep(0.3)                          # the running encode is killed ...
        info = P.load_source(key)
        count = sum(P.span_path(key, n).is_file() for n in range(info.spans))
        time.sleep(1.0)                          # ... and nothing is written meanwhile
        assert sum(P.span_path(key, n).is_file() for n in range(info.spans)) == count
        assert count < info.spans
        # An on-demand span still runs during an export.
        missing = next(n for n in range(info.spans) if not P.span_path(key, n).is_file())
        assert manager.request_span(key, missing, timeout=10) is not None
    assert manager.wait_idle(180)
    assert manager.stats["paused"] >= 1
    assert P.live_index(key)["state"] == "ready"
    bars = [read_barcode(f) for f in
            decode_gray(_avcc(key), _all_samples(key)[::50], 960, 540)]
    assert bars == list(range(0, 900, 50))


def test_proxy_lru_evicts_old_spans_and_orphaned_proxies(workdir, manager, tmp_path):
    from video_ai_editor.render import cache_budget
    a = make_barcode_master(tmp_path / "a.mp4", frames=240, audio=False)
    b = make_barcode_master(tmp_path / "b.mp4", frames=30, audio=False)
    ka, kb = _build(manager, a), _build(manager, b)
    root = P.proxies_root()
    old = time.time() - 3600
    for n in range(4):
        os.utime(P.span_path(ka, n), (old + n, old + n))
    total = cache_budget.proxy_usage(root)["bytes"]
    keep = total - P.span_path(ka, 0).stat().st_size - P.span_path(ka, 1).stat().st_size
    removed = cache_budget.enforce_proxies(root, budget=keep)
    assert set(removed) == {P.span_path(ka, 0), P.span_path(ka, 1)}
    assert P.live_index(ka)["state"] == "partial"
    # An evicted span is rebuilt on demand.
    assert manager.request_span(ka, 0, timeout=10) is not None
    # A proxy whose source is gone is removed whole.
    b.unlink()
    cache_budget.enforce_proxies(root)
    assert not P.proxy_dir(kb).exists() and P.proxy_dir(ka).exists()


def test_encode_is_niced(workdir, tmp_path, monkeypatch):
    from video_ai_editor import platformutil as _pu
    src = make_barcode_master(tmp_path / "n.mp4", frames=10, audio=False)
    info = P.probe_source(src)
    seen: list[list[str]] = []
    real = P.subprocess.Popen

    def spy(argv, *a, **kw):
        seen.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(P.subprocess, "Popen", spy)
    P.run_encode(info, 0, 10, on_span=lambda n, s: None)
    enc = [a for a in seen if "libx264" in a]
    assert len(enc) == 1
    assert enc[0] == _pu.low_priority_argv(enc[0][3:])
    assert enc[0][1:3] == ["-n", "10"] and enc[0][3] == _pu.FFMPEG


def test_a_busy_eager_build_does_not_hold_back_another_sources_preview(workdir, manager, tmp_path):
    """MAX_EAGER keeps a worker free: while one long source builds in the
    background, a NEW source's span and its FLAC chunks come on demand."""
    busy = make_barcode_master(tmp_path / "busy.mp4", frames=900, w=960, h=540, audio=False)
    fresh = make_barcode_master(tmp_path / "fresh.mp4", frames=120, audio_seconds=4.0)
    kb = manager.ensure(busy)
    assert _wait_for(lambda: P.span_path(kb, 0).is_file())
    kf = manager.ensure(fresh, eager=False, probe_timeout=5)
    assert manager.request_span(kf, 1, timeout=5) is not None
    assert manager.request_audio(kf, timeout=5) is True
    assert P.live_index(kb)["state"] != "ready"          # the busy build was still going
    first, samples = P.unpack_span(P.span_path(kf, 1).read_bytes())
    assert [read_barcode(f) for f in decode_gray(_avcc(kf), samples, 640, 360)] == list(range(60, 120))
    manager.cancel(busy)


def test_on_demand_spans_are_exact_on_a_variable_frame_rate_source(workdir, manager, tmp_path):
    """AI outputs need not be CFR: frame gaps alternating 10 ms / 57 ms. A
    span cut must still take exactly its own frames (midpoint trims)."""
    src = make_barcode_master(
        tmp_path / "vfr.mp4", frames=180, audio=False,
        extra=["-vf", "settb=1/30000,setpts='N*1000+if(mod(N\\,2)\\,700\\,0)'", "-fps_mode", "vfr",
               "-enc_time_base", "1/30000", "-video_track_timescale", "30000"])
    info = P.probe_source(src)
    gaps = {round(float((b - a) * info.time_base), 3) for a, b in zip(info.pts, info.pts[1:])}
    assert len(gaps) > 1, f"fixture is not VFR: {gaps}"
    key = manager.ensure(src, eager=False, probe_timeout=5)
    S = P.load_source(key).span_frames
    for n in (1, 2):
        first, samples = P.unpack_span(manager.request_span(key, n, timeout=10).read_bytes())
        want = list(range(n * S, min(180, (n + 1) * S)))
        assert [read_barcode(f) for f in decode_gray(_avcc(key), samples, 640, 360)] == want


# ---- container offsets, colour, anamorphic pixels, audio headroom (review RD1) --------------

@pytest.mark.parametrize("name, fmt_args", [
    ("offset.mp4", ["-g", "30", "-output_ts_offset", "3"]),
    ("offset.ts", ["-g", "30", "-output_ts_offset", "2.8", "-f", "mpegts"]),
])
def test_on_demand_span_of_a_source_that_starts_after_zero(workdir, manager, tmp_path,
                                                           name, fmt_args):
    """A container whose start_time is > 0 (a camera MTS, a raw file added
    through MCP): the pts table is absolute, so the seek must be absolute too
    (`-seek_timestamp 1`), or the demuxer lands past the span, the encode
    yields 0 frames and the whole proxy is failed. Middle span FIRST."""
    src = make_barcode_master(tmp_path / name, frames=240, rate="30", audio=False,
                              extra=fmt_args)
    info = P.probe_source(src)
    assert info.pts_time(0) > 2.5, "fixture does not start after zero"
    assert len(info.keyframes) >= 8, "fixture needs sync samples to seek to"
    key = manager.ensure(src, eager=False, probe_timeout=5)
    avcc = None
    for n in (2, 1, 3, 0):
        got = manager.request_span(key, n, timeout=20)
        assert got is not None, (n, P.live_index(key))
        first, samples = P.unpack_span(got.read_bytes())
        avcc = avcc or _avcc(key)
        assert first == n * 60 and len(samples) == 60
        bars = [read_barcode(f) for f in decode_gray(avcc, samples, 640, 360)]
        assert bars == list(range(n * 60, n * 60 + 60)), n
    assert P.live_index(key)["state"] == "ready"
    assert "-seek_timestamp" in P.encode_args(info, 60, 120)


def _proxy_planes(manager, src: Path) -> tuple:
    key = _build(manager, src)
    idx = P.read_index(key)
    return key, decode_yuv420(_avcc(key), _all_samples(key)[:1], idx["w"], idx["h"])[0]


def test_full_range_master_proxy_is_converted_to_limited(workdir, manager, tmp_path):
    """R12: a full-range (yuvj/pc) master keeps its range through export, so
    its BT.709-limited proxy must CONVERT the levels (0/255 -> 16/235), not
    just relabel them (WebKit would expand them again and clip)."""
    src = make_flat_master(tmp_path / "full.mp4", y=255, left_y=0, u=128, v=128,
                           pix_fmt="yuvj420p")
    assert P.probe_source(src).extra.get("color_range") == "pc"
    key, (Y, U, V) = _proxy_planes(manager, src)
    assert abs(int(Y[:, 2:24].mean()) - 16) <= 1, Y[:, :4]
    assert abs(int(Y[:, -24:-2].mean()) - 235) <= 1, Y[:, -4:]
    assert abs(int(U.mean()) - 128) <= 1 and abs(int(V.mean()) - 128) <= 1
    assert "color_range=tv" in sps_vui(_avcc(key), _all_samples(key), tmp_path)


def test_bt601_master_proxy_is_converted_to_bt709(workdir, manager, tmp_path):
    """R12: pure red stored in BT.601 (Y81 U90 V240) must become BT.709 red
    (Y63 U102 V240, within 2 levels) — a relabel would decode as (255,24,0)."""
    src = make_flat_master(tmp_path / "red601.mp4", y=81, u=90, v=240,
                           tags=["-colorspace", "smpte170m", "-color_primaries", "smpte170m",
                                 "-color_trc", "smpte170m"])
    assert P.probe_source(src).extra.get("color_space") == "smpte170m"
    key, (Y, U, V) = _proxy_planes(manager, src)
    got = (int(round(Y.mean())), int(round(U.mean())), int(round(V.mean())))
    assert all(abs(a - b) <= 2 for a, b in zip(got, (63, 102, 240))), got
    assert "color_space=bt709" in sps_vui(_avcc(key), _all_samples(key), tmp_path)


def test_untagged_master_proxy_levels_are_untouched(workdir, manager, tmp_path):
    """The conversion only applies to tagged sources: an untagged limited
    master keeps its stored values bit for bit (every bench file)."""
    src = make_flat_master(tmp_path / "plain.mp4", y=180, left_y=40, u=100, v=150)
    _, (Y, U, V) = _proxy_planes(manager, src)
    assert int(Y[:, 2:24].mean()) == 40 and int(Y[:, -24:-2].mean()) == 180
    assert int(U.mean()) == 100 and int(V.mean()) == 150


def test_anamorphic_master_gets_a_square_pixel_proxy(workdir, manager, tmp_path):
    """HDV-style 1440x1080 at SAR 4:3 displays 16:9: the proxy (and the w/h
    index.json publishes) is 1280x720 with square pixels."""
    src = make_barcode_master(tmp_path / "hdv.mp4", frames=10, w=1440, h=1080, audio=False,
                              extra=["-aspect", "16:9"])
    info = P.probe_source(src)
    assert (info.width, info.height) == (1920, 1080)
    key = _build(manager, src)
    idx = P.read_index(key)
    assert (idx["w"], idx["h"]) == (1280, 720)
    vui = sps_vui(_avcc(key), _all_samples(key), tmp_path)
    assert "width=1280" in vui and "sample_aspect_ratio=1:1" in vui, vui
    bars = [read_barcode(f, 1080) for f in decode_gray(_avcc(key), _all_samples(key), 1280, 720)]
    assert bars == list(range(10))


def test_frac_reads_ffprobe_ratios():
    assert P._frac("4:3") == Fraction(4, 3)
    assert P._frac("30000/1001") == Fraction(30000, 1001)
    assert P._frac("0:1") is None and P._frac("N/A") is None


def test_hot_master_audio_keeps_its_overs_through_the_recorded_gain(workdir, manager, tmp_path):
    """The export mixes ffmpeg's unclipped float decode; a 24-bit FLAC would
    hard-clip everything above full scale. A hot source is stored scaled down
    by a power of two and index.json carries gain_db, which the client's clip
    gain undoes: chunk x gain == ffmpeg's f32 decode (overs included)."""
    src = tmp_path / "hot.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc=1.45*sin(2*PI*220*t)|1.2*sin(2*PI*330*t):s=48000:d=6",
                    "-c:a", "pcm_f32le", str(src.with_suffix(".mov"))], check=True)
    src = src.with_suffix(".mov")
    ref = decode_audio_f32(src)
    assert float(np.max(np.abs(ref))) > 1.4
    key = _build(manager, src)
    a = P.read_index(key)["audio"]
    assert a["gain_db"] == pytest.approx(6.0206, abs=1e-3)
    assert a["chunk_gain"] == {"0": 2.0, "1": 2.0}          # 6 s = two chunks, both hot
    parts = []
    for n in range(a["chunks"]):
        data = sf.read(P.chunk_path(key, n), dtype="float64", always_2d=True)[0]
        g = P.chunk_gain(key, n)
        assert float(np.max(np.abs(data))) <= 1.0
        parts.append(data * g)
    got = np.concatenate(parts)
    assert got.shape == ref.shape
    assert float(np.max(np.abs(got - ref))) <= 2.0 * 2.0 ** -22
    assert float(np.max(np.abs(got))) > 1.4                 # the overs survive


def test_normal_master_audio_has_no_gain(workdir, manager, tmp_path):
    src = make_barcode_master(tmp_path / "au.mp4", frames=10, audio_seconds=2.0)
    key = _build(manager, src)
    a = P.read_index(key)["audio"]
    assert a["gain_db"] == 0.0 and a["chunk_gain"] == {} and P.chunk_gain(key, 0) == 1.0
