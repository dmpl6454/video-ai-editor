"""Instant-preview routes (wave D, INSTANT_PREVIEW_SPEC §5.2, §13 route tests).

Real ffmpeg, real proxies: spans are fetched over HTTP, decoded with the
served init and bar-read, so a route that served the wrong bytes fails here.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api import hardening
from video_ai_editor.ingest import proxy as P
from video_ai_editor.ingest.proxy_queue import MANAGER
from video_ai_editor.main import app

from proxy_fixtures import decode_gray, make_barcode_master, read_barcode


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    monkeypatch.delenv("VAI_PREVIEW_ENGINE", raising=False)
    monkeypatch.delenv("VAI_PROXY_EAGER", raising=False)
    _main._STORES.clear()
    hardening.RATE.windows.clear()
    c = TestClient(app)
    yield c
    MANAGER.wait_idle(30)


def _session_with_master(client, tmp_path: Path, frames: int = 90, **kw) -> tuple[str, Path]:
    sid = client.post("/api/sessions").json()["id"]
    src = make_barcode_master(tmp_path / sid / "uploads" / "clip" / "clip.normalized.mp4",
                              frames=frames, **kw)
    return sid, src


def _proxy(client, sid: str, src: Path) -> dict:
    r = client.get(f"/api/sessions/{sid}/proxy", params={"src": str(src)})
    assert r.status_code == 200, r.text
    return r.json()


# ---- preview.engine ------------------------------------------------------------------

def test_preview_engine_defaults_to_server(client):
    r = client.get("/api/settings/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "server" and body["source"] == "default"
    assert body["choices"] == ["auto", "client", "server"]
    assert body["eager_proxies"] is False


def test_preview_engine_reads_app_settings_and_env(client, monkeypatch):
    from video_ai_editor.api import pairing
    monkeypatch.setattr(pairing, "load_settings", lambda: {"preview": {"engine": "auto"}})
    assert client.get("/api/settings/preview").json()["engine"] == "auto"
    assert client.get("/api/settings/preview").json()["eager_proxies"] is True
    monkeypatch.setattr(pairing, "load_settings", lambda: {"preview": {"engine": "warp"}})
    assert client.get("/api/settings/preview").json()["engine"] == "server"   # typo ≠ change
    monkeypatch.setenv("VAI_PREVIEW_ENGINE", "client")
    body = client.get("/api/settings/preview").json()
    assert (body["engine"], body["source"]) == ("client", "env")


def test_preview_engine_is_written_only_by_put(client):
    """The write path is PUT (tests/test_preview_engine_write.py); no other
    verb changes the setting."""
    for verb in ("post", "patch", "delete"):
        r = getattr(client, verb)("/api/settings/preview")
        assert r.status_code == 405, verb


# ---- proxy discovery ------------------------------------------------------------------

def test_session_proxy_probes_and_returns_the_index(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=90)
    body = _proxy(client, sid, src)
    assert P.is_valid_key(body["key"])
    assert body["frames"] == 90 and body["spans"] == 2 and (body["w"], body["h"]) == (640, 360)
    r = client.get(body["index_url"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    idx = r.json()
    assert idx["span_ready"] == [False, False] and idx["state"] == "pending"
    assert idx["pack_format"].startswith("u32be first")


def test_session_proxy_refuses_sources_outside_the_session(client, tmp_path):
    sid = client.post("/api/sessions").json()["id"]
    outside = make_barcode_master(tmp_path / "elsewhere.mp4", frames=5, audio=False)
    r = client.get(f"/api/sessions/{sid}/proxy", params={"src": str(outside)})
    assert r.status_code == 403
    r = client.get(f"/api/sessions/{sid}/proxy",
                   params={"src": str(tmp_path / sid / "uploads" / "nope.mp4")})
    assert r.status_code == 404


def test_timeline_proxies_lists_every_media_source(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=30)
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "add_clip", "args": {"src": str(src), "track": "v1", "in": 0, "out": 1.0, "start": 0}})
    assert r.status_code == 200, r.text
    rows = client.get(f"/api/sessions/{sid}/proxies").json()["proxies"]
    assert [Path(r["src"]).resolve() for r in rows] == [src.resolve()]
    assert rows[0]["frames"] == 30


# ---- spans, init, chunks ----------------------------------------------------------------

def test_span_is_encoded_on_demand_and_decodes_with_the_served_init(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=90)
    key = _proxy(client, sid, src)["key"]
    r = client.get(f"/api/proxies/{key}/v/1.bin")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/octet-stream"
    first, samples = P.unpack_span(r.content)
    assert (first, len(samples)) == (60, 30)
    init = client.get(f"/api/proxies/{key}/init.mp4")
    assert init.status_code == 200 and init.headers["content-type"] == "video/mp4"
    frames = decode_gray(P.avcc_of(init.content), samples, 640, 360)
    assert [read_barcode(f) for f in frames] == list(range(60, 90))
    idx = client.get(f"/api/proxies/{key}/index.json").json()
    assert idx["span_ready"][1] is True


def test_span_supports_http_range(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=60)
    key = _proxy(client, sid, src)["key"]
    full = client.get(f"/api/proxies/{key}/v/0.bin").content
    r = client.get(f"/api/proxies/{key}/v/0.bin", headers={"Range": "bytes=8-1031"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 8-1031/{len(full)}"
    assert r.content == full[8:1032]


def test_span_not_ready_in_time_answers_202_retry(client, tmp_path, monkeypatch):
    sid, src = _session_with_master(client, tmp_path, frames=60)
    key = _proxy(client, sid, src)["key"]
    monkeypatch.setattr(MANAGER, "request_span", lambda *a, **k: None)
    r = client.get(f"/api/proxies/{key}/v/0.bin")
    assert r.status_code == 202
    assert r.headers["retry-after"] == "0.2"
    assert r.json()["status"] == "pending"


def test_flac_chunk_route(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=30, audio_seconds=7.0)
    key = _proxy(client, sid, src)["key"]
    r = client.get(f"/api/proxies/{key}/a/1.flac")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/flac" and r.content[:4] == b"fLaC"
    assert client.get(f"/api/proxies/{key}/a/7.flac").status_code == 404


def test_first_flac_chunk_is_served_while_later_chunks_still_encode(client, tmp_path,
                                                                    monkeypatch):
    """§4.4/§11.1: chunk 0 is answered as soon as IT is on disk, not after
    the whole source's audio (a long source takes seconds to finish). The
    FLAC writer is slowed for every chunk after the first to make 'the rest
    is still encoding' deterministic."""
    import soundfile
    import time as _time
    real_write = soundfile.write
    calls = []

    def slow_write(path, data, *a, **kw):
        calls.append(path)
        if len(calls) > 1:
            _time.sleep(0.8)
        return real_write(path, data, *a, **kw)

    monkeypatch.setattr(soundfile, "write", slow_write)
    sid, src = _session_with_master(client, tmp_path, frames=30, audio_seconds=30.0)
    key = _proxy(client, sid, src)["key"]
    t0 = _time.perf_counter()
    r = client.get(f"/api/proxies/{key}/a/0.flac")
    dt = _time.perf_counter() - t0
    assert r.status_code == 200, r.text
    assert r.content[:4] == b"fLaC"
    assert dt < 0.7, f"chunk 0 took {dt:.2f} s"
    assert not P.audio_done(key), "the rest of the audio should still be encoding"
    # A chunk past the end is still a 404 once the audio is complete.
    assert MANAGER.request_audio(key, timeout=20) is True
    chunks = P.read_index(key)["audio"]["chunks"]
    assert chunks >= 6
    assert client.get(f"/api/proxies/{key}/a/{chunks}.flac").status_code == 404
    assert client.get(f"/api/proxies/{key}/a/{chunks - 1}.flac").status_code == 200


def test_flac_chunk_carries_its_gain(client, tmp_path):
    """A chunk stored with headroom says so on the response (X-Audio-Gain,
    the linear power of two to multiply back); a normal chunk says 1."""
    sid = client.post("/api/sessions").json()["id"]
    hot = tmp_path / sid / "uploads" / "hot" / "hot.mov"
    hot.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc=1.45*sin(2*PI*220*t):s=48000:d=2", "-c:a", "pcm_f32le",
                    str(hot)], check=True)
    key = _proxy(client, sid, hot)["key"]
    r = client.get(f"/api/proxies/{key}/a/0.flac")
    assert r.status_code == 200 and r.headers["x-audio-gain"] == "2"
    sid2, src = _session_with_master(client, tmp_path, frames=30, audio_seconds=2.0)
    key2 = _proxy(client, sid2, src)["key"]
    assert client.get(f"/api/proxies/{key2}/a/0.flac").headers["x-audio-gain"] == "1"


def test_failed_proxy_answers_410(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=30, audio=False)
    key = _proxy(client, sid, src)["key"]
    P.mark_failed(key, "frame count mismatch")
    r = client.get(f"/api/proxies/{key}/v/0.bin")
    assert r.status_code == 410
    assert r.json()["error"]["details"]["code"] == "proxy_failed"


# ---- access control ---------------------------------------------------------------------------

def test_key_not_referenced_by_a_live_session_is_404(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=30, audio=False)
    key = _proxy(client, sid, src)["key"]
    assert client.get(f"/api/proxies/{key}/v/0.bin").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    for path in ("index.json", "init.mp4", "v/0.bin", "a/0.flac"):
        assert client.get(f"/api/proxies/{key}/{path}").status_code == 404, path
    # A proxy some session never asked for (made directly) is not served either.
    other = make_barcode_master(tmp_path / "x.mp4", frames=5, audio=False)
    k2 = MANAGER.ensure(other, eager=False, probe_timeout=5)
    assert client.get(f"/api/proxies/{k2}/index.json").status_code == 404


@pytest.mark.parametrize("path", [
    "/api/proxies/zz/index.json",
    "/api/proxies/..%2F..%2Fetc/index.json",
    "/api/proxies/0123456789abcdef01234567/v/..%2F..%2Findex.bin",
    "/api/proxies/0123456789abcdef01234567/v/-1.bin",
    "/api/proxies/0123456789abcdef01234567/a/1e3.flac",
])
def test_malformed_keys_and_indices_are_404(client, path):
    assert client.get(path).status_code == 404


def test_host_allowlist_still_guards_preview_routes(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=30, audio=False)
    key = _proxy(client, sid, src)["key"]
    r = client.get(f"/api/proxies/{key}/v/0.bin", headers={"Host": "evil.example"})
    assert r.status_code == 421
    r = client.get(f"/api/proxies/{key}/v/0.bin", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


# ---- rate limit ----------------------------------------------------------------------------------

def test_preview_media_share_one_bucket_exempt_from_per_path_limit(client, tmp_path, monkeypatch):
    sid, src = _session_with_master(client, tmp_path, frames=30, audio=False)
    key = _proxy(client, sid, src)["key"]
    assert client.get(f"/api/proxies/{key}/v/0.bin").status_code == 200
    hardening.RATE.windows.clear()
    # A 60 s window so the counts cannot drift on a loaded machine: the
    # limiter's capacity is rps × window, i.e. 5 per path and 30 shared.
    monkeypatch.setattr(hardening.RATE, "window_s", 60.0)
    monkeypatch.setattr(hardening.RATE, "default_rps", 5.0 / 60)
    monkeypatch.setattr(hardening, "PREVIEW_MEDIA_RPS", 30.0 / 60)
    # 20 hits on ONE span path: far past the per-path default of 5.
    codes = [client.get(f"/api/proxies/{key}/v/0.bin").status_code for _ in range(20)]
    assert codes == [200] * 20
    # ... and every preview-media path shares the one 30 rps bucket.
    more = [client.get(f"/api/proxies/{key}/init.mp4").status_code for _ in range(15)]
    assert 429 in more and more.count(200) == 10
    # Other routes keep their own per-path default.
    hardening.RATE.windows.clear()
    assert client.get(f"/api/proxies/{key}/init.mp4").status_code == 200
    head = [client.get(f"/api/sessions/{sid}/head").status_code for _ in range(8)]
    assert head.count(429) == 3


def test_rate_bucket_mapping():
    shared = ("preview-media", hardening.PREVIEW_MEDIA_RPS)
    key = "0123456789abcdef01234567"
    for media in (f"/api/proxies/{key}/v/1.bin", f"/api/proxies/{key}/a/12.flac",
                  f"/api/proxies/{key}/init.mp4"):
        assert hardening.rate_bucket(media) == shared, media
        assert hardening.rate_bucket(media, "HEAD") == shared, media
        # Only reads of media are exempt: any other method keeps its own bucket.
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert hardening.rate_bucket(media, method) == (media, None), (media, method)
    # JSON and anything that is not a span/chunk/init keeps the per-path bucket.
    for other in (f"/api/proxies/{key}/index.json", f"/api/proxies/{key}/v/x.txt",
                  f"/api/proxies/{key}/refs.json", "/api/proxies/",
                  "/api/sessions/s_x/cache-src/rife/a.mp4"):
        assert hardening.rate_bucket(other) == (other, None), other
    assert hardening.rate_bucket("/api/sessions/s_x/files/uploads/a.mp4") == \
        ("/api/sessions/s_x/files/uploads/a.mp4", None)
    assert hardening.rate_bucket("/api/sessions/s_x/thumb") == ("/api/sessions/s_x/thumb",
                                                                 hardening.FILMSTRIP_RPS)


# ---- no unspecified file serving (spec §14 risk 16) ------------------------------------------------

def test_no_cache_src_route(client, tmp_path):
    """Spec §5.2/§14.16 limit new file serving to workdir/proxies and
    previews: an AI output under a session's cache/ is NOT served by a
    preview route (the degraded tier of Phase 1d brings its own, specified,
    route when it needs one)."""
    assert not [r for r in app.routes if "cache-src" in getattr(r, "path", "")]
    sid = client.post("/api/sessions").json()["id"]
    p = tmp_path / sid / "cache" / "stabilize" / "stab_abc.mp4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    r = client.get(f"/api/sessions/{sid}/cache-src/stabilize/stab_abc.mp4")
    assert r.status_code != 200 or r.content != p.read_bytes()


# ---- /media rows ----------------------------------------------------------------------------------

def test_media_rows_gain_stream_and_proxy_fields(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=60, rate="25")
    (src.parent / "ingest.json").write_text(
        '{"src": "%s", "normalized": "%s", "probe": {"duration": 2.4}}' % (src, src))
    rows = client.get(f"/api/sessions/{sid}/media").json()["media"]
    row = next(r for r in rows if Path(r["src"]).resolve() == src.resolve())
    assert row["proxy"]["state"] == "none" and "frames" not in row   # no probe on this path
    key = _proxy(client, sid, src)["key"]
    rows = client.get(f"/api/sessions/{sid}/media").json()["media"]
    row = next(r for r in rows if Path(r["src"]).resolve() == src.resolve())
    assert row["fps"] == {"num": 25, "den": 1} and row["frames"] == 60
    assert row["pix_fmt"] == "yuv420p" and row["has_audio"] is True
    assert row["proxy"] == {"key": key, "state": "pending", "w": 640, "h": 360}


# ---- eager builds follow preview.engine --------------------------------------------------------

def test_edits_queue_no_proxy_work_while_the_engine_is_server(client, tmp_path):
    sid, src = _session_with_master(client, tmp_path, frames=30, audio=False)
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "add_clip", "args": {"src": str(src), "track": "v1",
                                                        "in": 0, "out": 1.0, "start": 0}})
    assert r.status_code == 200
    assert MANAGER.wait_idle(30)
    assert not P.proxy_dir(P.proxy_key(src)).exists()     # nothing built, nothing written


def test_edits_queue_eager_proxies_when_the_engine_is_on(client, tmp_path, monkeypatch):
    """How AI outputs landing in cache/ get proxies: any committed edit
    queues the timeline's sources that have none yet."""
    monkeypatch.setenv("VAI_PREVIEW_ENGINE", "auto")
    sid, src = _session_with_master(client, tmp_path, frames=30, audio=False)
    stab = make_barcode_master(tmp_path / sid / "cache" / "stabilize" / "s.mp4", frames=20,
                               audio=False)
    for s in (src, stab):
        r = client.post(f"/api/sessions/{sid}/dispatch",
                        json={"tool": "add_clip", "args": {"src": str(s), "track": "v1",
                                                            "in": 0, "out": 0.5, "start": 0}})
        assert r.status_code == 200, r.text
    assert MANAGER.wait_idle(60)
    for s in (src, stab):
        key = P.proxy_key(s)
        assert P.live_index(key)["state"] == "ready"
        assert sid in P.refs(key)


def test_upload_hook_queues_the_new_master(client, tmp_path, monkeypatch):
    from video_ai_editor import main as _main
    sid, src = _session_with_master(client, tmp_path, frames=20, audio=False)
    _main._queue_preview_proxy(sid, src)                  # engine server: no-op
    assert MANAGER.wait_idle(30) and not P.proxy_dir(P.proxy_key(src)).exists()
    monkeypatch.setenv("VAI_PROXY_EAGER", "1")
    _main._queue_preview_proxy(sid, src)
    assert MANAGER.wait_idle(60)
    assert P.live_index(P.proxy_key(src))["state"] == "ready"
    _main._queue_preview_proxy(sid, tmp_path / "gone.mp4")  # never raises


def test_a_real_upload_gets_its_proxy_when_eager(client, tmp_path, monkeypatch):
    monkeypatch.setenv("VAI_PROXY_EAGER", "1")
    sid = client.post("/api/sessions").json()["id"]
    raw = make_barcode_master(tmp_path / "raw" / "take.mp4", frames=45, rate="25")
    with raw.open("rb") as fh:
        r = client.post(f"/api/sessions/{sid}/upload",
                        files={"file": ("take.mp4", fh, "video/mp4")},
                        data={"transcribe": "false"})
    assert r.status_code == 200, r.text
    norm = Path(r.json()["normalized"])
    assert MANAGER.wait_idle(60)
    idx = P.live_index(P.proxy_key(norm))
    assert idx["state"] == "ready" and idx["frames"] == 45
    assert idx["src_rate"] == {"num": 25, "den": 1}


# ---- the proxies root is not a session (review RD1) ------------------------------------------

@pytest.mark.parametrize("path", ["/api/sessions/proxies", "/api/sessions/proxies/edl",
                                  "/api/sessions/proxies/media", "/api/sessions/proxies/proxies"])
def test_the_proxies_root_is_not_a_session(client, tmp_path, path):
    """WORKDIR/proxies is the first non-session directory at the WORKDIR
    root: it must never be adopted as a session id (which would build a
    project tree — uploads/, exports/ … — inside the proxy cache)."""
    sid, src = _session_with_master(client, tmp_path, frames=10, audio=False)
    _proxy(client, sid, src)
    root = P.proxies_root()
    assert root.is_dir() and root.parent == tmp_path
    before = sorted(p.name for p in root.iterdir())
    r = client.get(path)
    assert r.status_code in (400, 404), (path, r.status_code)
    assert sorted(p.name for p in root.iterdir()) == before


def test_a_full_disk_answers_507_and_stops_re_encoding_until_it_clears(client, tmp_path, monkeypatch):
    """Final QA r2 (robustness): with the disk full every span encode failed
    on write (OSError ENOSPC), the route answered 202 'pending' for good and
    each poll started a fresh full encode — 102 failed jobs in two minutes,
    the engine under a spinner, never reaching its degraded tier. Now the
    failure answers 507 {code: disk_full} (a 5xx the engine counts toward
    SPAN_DEGRADE_AFTER), a repeat poll inside the hold starts no encode, and
    index.json says why."""
    import errno
    from video_ai_editor.ingest import proxy_queue as Q
    sid, src = _session_with_master(client, tmp_path, frames=60, audio=False)
    key = _proxy(client, sid, src)["key"]
    MANAGER.wait_idle(30)
    encodes = {"n": 0}
    real_encode = P.run_encode
    real_write = P.write_span

    def counting_encode(*a, **k):
        encodes["n"] += 1
        return real_encode(*a, **k)

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(P, "run_encode", counting_encode)
    monkeypatch.setattr(P, "write_span", full)
    r = client.get(f"/api/proxies/{key}/v/0.bin")
    assert r.status_code == 507, r.text
    assert r.json()["error"]["details"]["code"] == "disk_full"
    n = encodes["n"]
    assert n >= 1
    for _ in range(5):
        assert client.get(f"/api/proxies/{key}/v/0.bin").status_code == 507
    assert encodes["n"] == n                 # no re-encode per poll while the disk is full
    assert client.get(f"/api/proxies/{key}/index.json").json().get("span_error") == "disk_full"
    # space freed: once the hold is over, the span encodes and is served
    monkeypatch.setattr(P, "write_span", real_write)
    monkeypatch.setattr(Q, "DISK_FULL_HOLD_S", 0.0)
    r = client.get(f"/api/proxies/{key}/v/0.bin")
    assert r.status_code == 200, r.text
    assert "span_error" not in client.get(f"/api/proxies/{key}/index.json").json()
