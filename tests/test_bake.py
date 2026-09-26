"""Bakes (wave D, INSTANT_PREVIEW_SPEC §5.3, R13, §13 test_bake / P1-B1 server
side): all-intra spans of `previews/<render_hash>.mp4`, served per session.

The frame-identity test is the one that matters: every bake span is decoded
with the bake's own init and each frame's bar is read, and it must equal the
bar of the SAME output frame of the preview ffmpeg rendered — bake frame k is
preview frame k (R13), for every k of the render.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api import hardening
from video_ai_editor.api import preview_routes as R
from video_ai_editor.ingest import proxy as P
from video_ai_editor.ingest.proxy_queue import MANAGER
from video_ai_editor.main import app
from video_ai_editor.render import bake as B
from video_ai_editor.render import cache_budget
from video_ai_editor.render import frame_map as FM

from proxy_fixtures import decode_gray, make_barcode_master, read_barcode, y_psnr


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    monkeypatch.delenv("VAI_PREVIEW_ENGINE", raising=False)
    monkeypatch.delenv("VAI_PROXY_EAGER", raising=False)
    _main._STORES.clear()
    R._FRAME_MAP_CACHE.clear()
    hardening.RATE.windows.clear()
    c = TestClient(app)
    yield c
    MANAGER.wait_idle(30)


def _dispatch(client, sid: str, tool: str, **args) -> dict:
    r = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl"},
                    json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _graded_timeline(client, tmp_path: Path, fps: int = 30) -> tuple[str, str, dict]:
    """640×360 project: clip A (master 0..44) then clip B (master 90..149)
    with a colour grade — the BAKED range of Phase 1 (§7) — rendered."""
    sid = client.post("/api/sessions").json()["id"]
    src = make_barcode_master(tmp_path / sid / "uploads" / "clip" / "clip.normalized.mp4",
                              frames=180, rate=str(fps))
    _dispatch(client, sid, "set_canvas", w=640, h=360, fps=fps)
    _dispatch(client, sid, "add_clip", src=str(src), track="v1", start=0, **{"in": 0, "out": 1.5})
    b = _dispatch(client, sid, "add_clip", src=str(src), track="v1", start=1.5,
                  **{"in": 3.0, "out": 5.0})
    last = _dispatch(client, sid, "color_grade", clip_id=b["result"]["clip_id"], brightness=0.04)
    r = client.post(f"/api/sessions/{sid}/preview")
    assert r.status_code == 200, r.text
    return sid, last["render_hash"], last


def _decode_file(path: Path, w: int, h: int) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
                          "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(-1, h, w)


def _bake(client, sid: str, h: str) -> tuple[dict, bytes, list[bytes]]:
    idx = client.get(f"/api/sessions/{sid}/bake/{h}/index.json")
    assert idx.status_code == 200, idx.text
    body = idx.json()
    first_init_key = body["init_key"]          # named by the very first answer
    samples: list[bytes] = []
    for n in range(body["spans"]):
        r = client.get(f"/api/sessions/{sid}/bake/{h}/v/{n}.bin")
        assert r.status_code == 200, (n, r.status_code, r.text[:200])
        first, s = P.unpack_span(r.content)
        assert first == n * body["span_frames"]
        samples += s
    init = client.get(f"/api/sessions/{sid}/bake/{h}/init.mp4")
    assert init.status_code == 200 and init.headers["content-type"] == "video/mp4"
    body = client.get(f"/api/sessions/{sid}/bake/{h}/index.json").json()
    assert body["span_ready"] == [True] * body["spans"]
    assert body["init_key"] == first_init_key == P.avcc_of(init.content).hex()
    return body, init.content, samples


# ---- frame identity (R13) ------------------------------------------------------------------

def test_bake_frames_are_exactly_the_preview_frames(client, tmp_path):
    sid, h, last = _graded_timeline(client, tmp_path)
    preview = tmp_path / sid / "previews" / f"{h}.mp4"
    assert preview.is_file()
    body, init, samples = _bake(client, sid, h)
    assert body["bake"] is True and body["render_hash"] == h
    assert (body["w"], body["h"]) == (640, 360)         # the preview's own size
    assert body["rate"] == {"num": 30, "den": 1}
    assert body["init_key"] == P.avcc_of(init).hex()
    assert body["codec"].startswith("avc1.64")

    ref = _decode_file(preview, 640, 360)
    got = decode_gray(P.avcc_of(init), samples, 640, 360)
    # Bake frame k IS preview frame k, for every k of the render ...
    assert len(got) == len(ref) == body["frames"]
    assert [read_barcode(g) for g in got] == [read_barcode(f) for f in ref]
    # ... and the same picture, one all-intra crf-24 generation later. The
    # fixture's texture is a hard-edged sawtooth (worst case for an intra
    # coder): measured min 33.3 dB, mean 34.3 dB on it; the flat bar cells
    # themselves are near-lossless.
    psnr = [y_psnr(g, f) for g, f in zip(got, ref)]
    assert min(psnr) > 30.0, min(psnr)
    assert min(y_psnr(g[:32], f[:32]) for g, f in zip(got, ref)) > 40.0

    # And it is output frame k of the program map of that hash (R13 + R4):
    fm = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).json()
    assert fm["total"] == len(got)
    want = [e["frame"] for e in FM.rle_frames(fm["runs"])]
    assert [read_barcode(g) for g in got] == want
    assert want[:45] == list(range(45)) and want[45:] == list(range(90, 150))


def test_bake_of_a_25fps_project_is_its_own_init_class(client, tmp_path):
    """avcC class rules (§3.2): a bake carries the project rate in its VUI, so
    it is a different init class from a 30 fps proxy of the same size."""
    sid, h, _ = _graded_timeline(client, tmp_path, fps=25)
    body, init, samples = _bake(client, sid, h)
    assert body["rate"] == {"num": 25, "den": 1}
    fm = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).json()
    assert body["frames"] == fm["total"] == round(3.5 * 25)
    got = decode_gray(P.avcc_of(init), samples, 640, 360)
    ref = _decode_file(tmp_path / sid / "previews" / f"{h}.mp4", 640, 360)
    assert [read_barcode(g) for g in got] == [read_barcode(f) for f in ref]
    other = make_barcode_master(tmp_path / "p30.mp4", frames=30, rate="30", audio=False)
    k30 = MANAGER.ensure(other, eager=False, probe_timeout=5)
    assert MANAGER.request_span(k30, 0, timeout=30) is not None
    assert P.read_index(k30)["init_key"] != body["init_key"]


# ---- lifecycle -----------------------------------------------------------------------------

def test_bake_is_404_until_the_preview_lands(client, tmp_path):
    sid = client.post("/api/sessions").json()["id"]
    src = make_barcode_master(tmp_path / sid / "uploads" / "c" / "c.mp4", frames=30, audio=False)
    _dispatch(client, sid, "set_canvas", w=640, h=360, fps=30)
    h = _dispatch(client, sid, "add_clip", src=str(src), track="v1", start=0,
                  **{"in": 0, "out": 1.0})["render_hash"]
    for path in ("index.json", "init.mp4", "v/0.bin"):
        assert client.get(f"/api/sessions/{sid}/bake/{h}/{path}").status_code == 404, path
    assert client.post(f"/api/sessions/{sid}/preview").status_code == 200
    assert client.get(f"/api/sessions/{sid}/bake/{h}/v/0.bin").status_code == 200


def test_ranges_queue_exactly_the_spans_they_touch(client, tmp_path, monkeypatch):
    sid, h, _ = _graded_timeline(client, tmp_path)
    asked: list[int] = []
    real = MANAGER.request_span
    monkeypatch.setattr(MANAGER, "request_span",
                        lambda key, n, timeout=2.0: asked.append(n) or real(key, n, timeout=timeout))
    r = client.get(f"/api/sessions/{sid}/bake/{h}/index.json", params={"ranges": "45-105"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["span_frames"] == 60 and body["queued"] == [0, 1]
    # Exactly those spans were asked for (span 0 twice: once queued, once
    # awaited so the first answer can name the init class).
    assert asked[:2] == [0, 1] and set(asked) == {0, 1}
    assert body["init_key"] == P.avcc_of(P.init_path(body["key"]).read_bytes()).hex()
    assert MANAGER.wait_idle(60)
    ready = client.get(f"/api/sessions/{sid}/bake/{h}/index.json").json()["span_ready"]
    assert ready[:2] == [True, True]
    assert B.spans_for_ranges(P.load_source(B.bake_key(tmp_path / sid / "previews" / f"{h}.mp4")),
                              [(0, 1), (59, 61), (104, 105)]) == [0, 1]


@pytest.mark.parametrize("ranges", ["5-5", "a-b", "1-2;3-4", "-3-4", "9-2", ",".join(["1-2"] * 300)])
def test_malformed_ranges_are_400(client, tmp_path, ranges):
    sid, h, _ = _graded_timeline(client, tmp_path)
    r = client.get(f"/api/sessions/{sid}/bake/{h}/index.json", params={"ranges": ranges})
    assert r.status_code == 400


def test_bake_key_survives_the_render_cache_touching_the_preview(client, tmp_path):
    """The render cache refreshes a preview's mtime every time it is reused;
    an mtime-keyed bake would be re-encoded after every reuse."""
    sid, h, _ = _graded_timeline(client, tmp_path)
    preview = tmp_path / sid / "previews" / f"{h}.mp4"
    k1 = B.bake_key(preview)
    assert client.get(f"/api/sessions/{sid}/bake/{h}/v/0.bin").status_code == 200
    os.utime(preview, None)
    cache_budget.touch(preview)
    assert B.bake_key(preview) == k1
    assert P.span_path(k1, 0).is_file()


def test_a_newer_hash_supersedes_the_older_bake(client, tmp_path, monkeypatch):
    sid, h1, last = _graded_timeline(client, tmp_path)
    cancelled: list[str] = []
    monkeypatch.setattr(MANAGER, "cancel", lambda src: cancelled.append(str(src)) or False)
    assert client.get(f"/api/sessions/{sid}/bake/{h1}/index.json").status_code == 200
    clip = last["edl"]["tracks"][0]["clips"][0]["id"]
    h2 = _dispatch(client, sid, "trim_clip", clip_id=clip, out=1.2)["render_hash"]
    assert client.post(f"/api/sessions/{sid}/preview").status_code == 200
    assert client.get(f"/api/sessions/{sid}/bake/{h2}/index.json").status_code == 200
    assert cancelled == [os.path.realpath(tmp_path / sid / "previews" / f"{h1}.mp4")]


def test_bakes_are_one_lru_class_with_proxies_and_go_with_their_preview(client, tmp_path):
    sid, h, _ = _graded_timeline(client, tmp_path)
    preview = tmp_path / sid / "previews" / f"{h}.mp4"
    assert client.get(f"/api/sessions/{sid}/bake/{h}/v/1.bin").status_code == 200
    key = B.bake_key(preview)
    span = P.span_path(key, 1)
    assert span in {e.path for e in cache_budget.proxy_entries(P.proxies_root())}
    preview.unlink()                                  # evicted by the render cache
    cache_budget.enforce_proxies(P.proxies_root())
    assert not P.proxy_dir(key).exists()
    assert client.get(f"/api/sessions/{sid}/bake/{h}/v/1.bin").status_code == 404


# ---- access control ----------------------------------------------------------------------------

def test_another_sessions_bake_is_404(client, tmp_path):
    sid_a, h, _ = _graded_timeline(client, tmp_path)
    sid_b = client.post("/api/sessions").json()["id"]
    for path in ("index.json", "init.mp4", "v/0.bin"):
        assert client.get(f"/api/sessions/{sid_b}/bake/{h}/{path}").status_code == 404, path
    assert client.get(f"/api/sessions/{sid_a}/bake/{h}/v/0.bin").status_code == 200
    # The bake key is not servable once its only session is gone.
    key = B.bake_key(tmp_path / sid_a / "previews" / f"{h}.mp4")
    assert client.get(f"/api/proxies/{key}/v/0.bin").status_code == 200
    assert client.delete(f"/api/sessions/{sid_a}").status_code == 200
    assert client.get(f"/api/proxies/{key}/v/0.bin").status_code == 404
    assert client.get(f"/api/sessions/{sid_a}/bake/{h}/v/0.bin").status_code == 404


@pytest.mark.parametrize("tail", [
    "bake/..%2F..%2Fedl/init.mp4",
    "bake/0123456789abcdef/v/..%2F..%2Findex.bin",
    "bake/0123456789abcdef/v/-1.bin",
    "bake/0123456789abcdef/v/1e3.bin",
    "bake/0123456789ABCDEF/init.mp4",
    "bake/0123456789abcdef0/init.mp4",
    "bake/%2e%2e/init.mp4",
])
def test_traversal_and_malformed_bake_paths_are_404(client, tmp_path, tail):
    sid, h, _ = _graded_timeline(client, tmp_path)
    r = client.get(f"/api/sessions/{sid}/{tail}")
    assert r.status_code == 404, (tail, r.status_code)


def test_a_file_planted_under_previews_with_a_hash_name_is_only_served_as_a_bake(client, tmp_path):
    """Only previews/<16 hex>.mp4 of THIS session is ever opened, and only
    through the proxy recipe: the route serves re-encoded span packs, never
    the file's own bytes."""
    sid, h, _ = _graded_timeline(client, tmp_path)
    r = client.get(f"/api/sessions/{sid}/bake/{h}/v/0.bin")
    raw = (tmp_path / sid / "previews" / f"{h}.mp4").read_bytes()
    assert r.status_code == 200 and r.content[:8] != raw[:8] and r.content not in raw


def test_bake_routes_are_behind_the_host_allowlist(client, tmp_path):
    sid, h, _ = _graded_timeline(client, tmp_path)
    url = f"/api/sessions/{sid}/bake/{h}/v/0.bin"
    assert client.get(url, headers={"Host": "evil.example"}).status_code == 421
    assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(url).status_code == 405


# ---- rate bucket -----------------------------------------------------------------------------

def test_bake_media_share_the_preview_media_bucket(client, tmp_path, monkeypatch):
    sid, h, _ = _graded_timeline(client, tmp_path)
    assert client.get(f"/api/sessions/{sid}/bake/{h}/v/0.bin").status_code == 200
    hardening.RATE.windows.clear()
    monkeypatch.setattr(hardening.RATE, "window_s", 60.0)
    monkeypatch.setattr(hardening.RATE, "default_rps", 5.0 / 60)
    monkeypatch.setattr(hardening, "PREVIEW_MEDIA_RPS", 30.0 / 60)
    codes = [client.get(f"/api/sessions/{sid}/bake/{h}/v/0.bin").status_code for _ in range(20)]
    assert codes == [200] * 20                       # far past the per-path 5
    more = [client.get(f"/api/sessions/{sid}/bake/{h}/init.mp4").status_code for _ in range(15)]
    assert 429 in more and more.count(200) == 10     # one shared 30 bucket
    hardening.RATE.windows.clear()
    idx = [client.get(f"/api/sessions/{sid}/bake/{h}/index.json").status_code for _ in range(8)]
    assert idx.count(429) == 3                       # index.json: per-path


def test_bake_rate_bucket_mapping():
    shared = (hardening.PREVIEW_MEDIA_BUCKET, hardening.PREVIEW_MEDIA_RPS)
    base = "/api/sessions/s_abcdef12/bake/0123456789abcdef"
    for p in (f"{base}/init.mp4", f"{base}/v/0.bin", f"{base}/v/123456.bin"):
        assert hardening.rate_bucket(p) == shared and hardening.rate_bucket(p, "HEAD") == shared
        assert hardening.rate_bucket(p, "POST") == (p, None)
    for p in (f"{base}/index.json", f"{base}/a/0.flac", f"{base}/v/1234567.bin",
              "/api/sessions/s_abcdef12/bake/0123456789ABCDEF/init.mp4",
              "/api/sessions/x/bake/0123456789abcdef/init.mp4"):
        assert hardening.rate_bucket(p) == (p, None), p
