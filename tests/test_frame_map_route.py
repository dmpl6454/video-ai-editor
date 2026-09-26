"""GET /api/sessions/{sid}/frame_map (wave D, INSTANT_PREVIEW_SPEC §4.1 step 8,
§5.2, §8, R14): the reference program map of the session's CURRENT render.

Real ffmpeg throughout: the sources are bar-coded masters, the route's
SourceInfo comes from their real proxy probes, and the map it returns is
checked against the preview ffmpeg actually renders for that hash, decoded
frame by frame.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api import hardening, locks
from video_ai_editor.api import preview_routes as R
from video_ai_editor.ingest import proxy as P
from video_ai_editor.ingest.proxy_queue import MANAGER
from video_ai_editor.main import app
from video_ai_editor.render import frame_map as FM

from proxy_fixtures import make_barcode_master, read_barcode


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


def _timeline(client, tmp_path: Path, frames: int = 120) -> tuple[str, Path, list[dict]]:
    """A 640×360 @ 30 project with two cuts of one bar-coded master and a
    gap: [0, 1.0) = master 0..29, gap 1.0-1.5, [1.5, 2.5) = master 60..89."""
    sid = client.post("/api/sessions").json()["id"]
    src = make_barcode_master(tmp_path / sid / "uploads" / "clip" / "clip.normalized.mp4",
                              frames=frames)
    _dispatch(client, sid, "set_canvas", w=640, h=360, fps=30)
    a = _dispatch(client, sid, "add_clip", src=str(src), track="v1", start=0, **{"in": 0, "out": 1.0})
    b = _dispatch(client, sid, "add_clip", src=str(src), track="v1", start=1.5,
                  **{"in": 2.0, "out": 3.0})
    return sid, src, [a, b]


def _decode_gray(path: Path, w: int, h: int) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
                          "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(-1, h, w)


# ---- the map itself --------------------------------------------------------------------

def test_frame_map_of_the_current_hash_matches_the_real_render(client, tmp_path):
    """The body is `frame_map_json` of the current preview EDL with each
    source's proxy SourceInfo, and it names — frame for frame — the master
    frame the preview ffmpeg renders for that hash (decoded bars)."""
    sid, src, answers = _timeline(client, tmp_path)
    h = answers[-1]["render_hash"]
    r = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["render_hash"] == h and body["version"] == FM.FRAME_MAP_VERSION
    assert body["R"] == [30, 1] and body["T"] == 8000 and body["total"] == 75
    clip_src = answers[-1]["edl"]["tracks"][0]["clips"][0]["src"]
    assert set(body["sources"]) == {clip_src}
    s = body["sources"][clip_src]
    assert s["frames"] == 120 and s["rate"] == [30, 1] and (s["w"], s["h"]) == (640, 360)

    # The same numbers, computed independently from a fresh probe.
    from video_ai_editor.edl.schema import EDL
    edl = EDL.model_validate(answers[-1]["edl"])
    ref = FM.frame_map_json(edl, {clip_src: FM.SourceInfo.from_proxy(P.probe_source(src))})
    assert body["runs"] == ref["runs"] and body["audio"] == ref["audio"]

    # ... and the render ffmpeg makes for that hash agrees frame by frame.
    pr = client.post(f"/api/sessions/{sid}/preview")
    assert pr.status_code == 200, pr.text
    preview = tmp_path / sid / "previews" / f"{h}.mp4"
    frames = _decode_gray(preview, 640, 360)
    expect = FM.rle_frames(body["runs"])
    assert len(frames) == body["total"] == len(expect)
    for k, (f, e) in enumerate(zip(frames, expect)):
        if e["kind"] == FM.KIND_GAP:
            assert f.mean() < 20, f"k={k}: gap should be black"
        else:
            assert read_barcode(f) == e["frame"], f"k={k}"


def test_stale_hash_is_a_clear_409_with_the_current_hash(client, tmp_path):
    sid, src, answers = _timeline(client, tmp_path)
    old = answers[0]["render_hash"]
    cur = answers[-1]["render_hash"]
    r = client.get(f"/api/sessions/{sid}/frame_map", params={"h": old})
    assert r.status_code == 409
    err = r.json()["error"]["details"]
    assert err["code"] == "stale_render_hash" and err["render_hash"] == cur
    # After an undo the older hash is the current one again.
    _dispatch(client, sid, "undo")
    assert client.get(f"/api/sessions/{sid}/frame_map", params={"h": old}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/frame_map", params={"h": cur}).status_code == 409


def test_another_sessions_hash_is_stale_here(client, tmp_path):
    sid_a, _, answers = _timeline(client, tmp_path)
    sid_b = client.post("/api/sessions").json()["id"]
    r = client.get(f"/api/sessions/{sid_b}/frame_map", params={"h": answers[-1]["render_hash"]})
    assert r.status_code == 409


@pytest.mark.parametrize("h", ["zz", "0123456789ABCDEF", "../../etc/passwd", "0" * 15, "0" * 17])
def test_malformed_hash_is_400(client, tmp_path, h):
    sid = client.post("/api/sessions").json()["id"]
    assert client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).status_code == 400


def test_missing_hash_and_unknown_session(client):
    assert client.get("/api/sessions/s_nosuchsession/frame_map",
                      params={"h": "0" * 16}).status_code == 404
    assert client.get("/api/sessions/..%2F..%2Fetc/frame_map",
                      params={"h": "0" * 16}).status_code in (400, 404)
    sid = client.post("/api/sessions").json()["id"]
    assert client.get(f"/api/sessions/{sid}/frame_map").status_code == 422


def test_empty_timeline_has_a_map_too(client):
    sid = client.post("/api/sessions").json()["id"]
    h = client.post(f"/api/sessions/{sid}/dispatch", params={"include": "edl"},
                    json={"tool": "set_canvas", "args": {"fps": 25}}).json()["render_hash"]
    body = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).json()
    assert body["sources"] == {} and all(r["kind"] == FM.KIND_GAP for r in body["runs"])


# ---- pending, busy, failed ------------------------------------------------------------------

def test_unprobed_source_answers_202_retry(client, tmp_path, monkeypatch):
    sid, _, answers = _timeline(client, tmp_path)
    monkeypatch.setattr(P, "load_source", lambda key: None)
    monkeypatch.setattr(MANAGER, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(R, "FRAME_MAP_WAIT_S", 0.05)
    r = client.get(f"/api/sessions/{sid}/frame_map", params={"h": answers[-1]["render_hash"]})
    assert r.status_code == 202 and r.headers["retry-after"] == "0.2"


def test_an_edit_holding_the_session_answers_202(client, tmp_path, monkeypatch):
    sid, _, answers = _timeline(client, tmp_path)
    monkeypatch.setattr(R, "FRAME_MAP_WAIT_S", 0.05)
    lock = locks.session_lock(sid)
    with lock:
        r = client.get(f"/api/sessions/{sid}/frame_map", params={"h": answers[-1]["render_hash"]})
    assert r.status_code == 202 and r.json()["what"] == "session busy"


def test_a_source_that_cannot_be_probed_is_422(client, tmp_path, monkeypatch):
    sid, src, answers = _timeline(client, tmp_path)

    def broken(*a, **k):
        raise P.ProxyError("synthetic: no readable stream")

    monkeypatch.setattr(P, "probe_source", broken)
    r = client.get(f"/api/sessions/{sid}/frame_map", params={"h": answers[-1]["render_hash"]})
    assert r.status_code == 422
    d = r.json()["error"]["details"]
    assert d["code"] == "source_unavailable" and len(d["srcs"]) == 1
    assert (P.read_index(P.proxy_key(src)) or {}).get("failed")


def test_a_failed_proxy_encode_still_has_a_map(client, tmp_path):
    """A proxy whose ENCODE failed (spec §7: the degraded <video> tier plays
    that source from the master) still has its probe, and frame selection
    needs nothing else: the map is served."""
    sid, src, answers = _timeline(client, tmp_path)
    h = answers[-1]["render_hash"]
    assert client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).status_code == 200
    R._FRAME_MAP_CACHE.clear()
    P.mark_failed(P.proxy_key(src), "frame count mismatch (synthetic)")
    r = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h})
    assert r.status_code == 200 and r.json()["total"] == 75


def test_cached_per_hash_and_invalidated_by_new_source_bytes(client, tmp_path):
    sid, src, answers = _timeline(client, tmp_path)
    h = answers[-1]["render_hash"]
    a = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).json()
    assert len(R._FRAME_MAP_CACHE) == 1
    assert client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).json() == a
    assert len(R._FRAME_MAP_CACHE) == 1
    # The source is rewritten in place with fewer frames: same EDL, same
    # render hash, but a new proxy identity — the map is rebuilt from it.
    st = os.stat(src)
    make_barcode_master(src, frames=75)
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    b = client.get(f"/api/sessions/{sid}/frame_map", params={"h": h}).json()
    assert len(R._FRAME_MAP_CACHE) == 2
    assert next(iter(b["sources"].values()))["frames"] == 75
    assert b["runs"] != a["runs"]            # clip 2 (60..89) now runs past the end


# ---- posture ---------------------------------------------------------------------------------

def test_host_allowlist_and_cross_site_guard_frame_map(client, tmp_path):
    sid, _, answers = _timeline(client, tmp_path)
    url = f"/api/sessions/{sid}/frame_map?h={answers[-1]['render_hash']}"
    assert client.get(url, headers={"Host": "evil.example"}).status_code == 421
    assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(url).status_code == 405


def test_frame_map_keeps_the_per_path_rate_bucket():
    path = "/api/sessions/s_abcdef12/frame_map"
    assert hardening.rate_bucket(path) == (path, None)
