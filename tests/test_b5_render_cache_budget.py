"""QA-106: render caches are bounded by BYTES, least-recently-used first.

Before: every render cache was capped by a file COUNT (10 previews, 15 cached
videos, 200 chunks, 400 segments), so nine edits on a 12-minute project left
~1.6 GB behind. Now each project's previews/chunks/segments/cached videos are
trimmed LRU to a byte budget after every render, then the whole workdir is
trimmed to a total budget, idle projects first. Real preview renders; sizes
are what is on disk.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.api.hardening import RATE
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.main import app
from video_ai_editor.render import cache_budget, render_preview


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("media") / "noisy.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=6",
         "-f", "lavfi", "-i", "sine=f=440:duration=6", "-vf", "noise=alls=60:allf=t",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(p)], check=True, capture_output=True)
    return p


@pytest.fixture
def fast_lru(monkeypatch):
    """No in-use grace period, and no workdir sweep unless a test asks."""
    monkeypatch.setattr(cache_budget, "PROTECT_RECENT_S", 0.0)
    monkeypatch.setattr(cache_budget, "_LAST_SWEEP", [time.time() + 3600])


def _store(sd: Path, src: Path) -> EDLStore:
    sd.mkdir(parents=True, exist_ok=True)
    for sub in ("previews", "cache", "exports", "uploads"):
        (sd / sub).mkdir(exist_ok=True)
    e = empty_edl()
    e.canvas = Canvas(w=640, h=360, fps=30)
    (sd / "edl.json").write_text(e.model_dump_json())
    s = EDLStore(sd)
    dispatch(s, "add_clip", {"track": "v1", "src": str(src), "in": 0.0, "out": 6.0,
                             "start": 0.0})
    return s


def _rotate(s: EDLStore, deg: float) -> None:
    cid = s.edl.get_track("v1").clips[0].id
    dispatch(s, "set_clip_transform", {"clip_id": cid, "rotation": deg})


def _cache_bytes(sd: Path) -> int:
    return sum(e.size for e in cache_budget.entries(sd))


def test_edits_keep_the_project_cache_under_its_byte_budget(tmp_path, src, fast_lru,
                                                             monkeypatch):
    s = _store(tmp_path / "s_budget01", src)
    render_preview(s.edl, s.dir)
    one_set = _cache_bytes(s.dir)          # preview + chunk + cached video
    budget = int(one_set * 2.5)
    monkeypatch.setenv("VAI_RENDER_CACHE_MB", str(budget / (1024 * 1024)))
    assert cache_budget.session_budget_bytes() == pytest.approx(budget, abs=1024 * 1024)
    budget = cache_budget.session_budget_bytes()

    hashes = []
    for deg in (2, 4, 6, 8, 10, 12):
        _rotate(s, deg)
        res = render_preview(s.edl, s.dir)
        hashes.append(res.edl_hash)
        assert res.path.exists(), "the render just produced was evicted"
        assert _cache_bytes(s.dir) <= budget, (
            f"after {len(hashes)} edits the cache holds {_cache_bytes(s.dir)} B "
            f"against a {budget} B budget")
    # LRU, not arbitrary: the newest previews survive, the oldest went first.
    assert (s.dir / "previews" / f"{hashes[-1]}.mp4").exists()
    assert not (s.dir / "previews" / f"{hashes[0]}.mp4").exists()


def test_a_reused_entry_outlives_a_newer_unused_one(tmp_path, src, fast_lru):
    s = _store(tmp_path / "s_budget02", src)
    _rotate(s, 3)
    old = render_preview(s.edl, s.dir).path
    _rotate(s, 5)
    newer = render_preview(s.edl, s.dir).path
    # Back to the first state: its preview is a cache HIT, which makes it the
    # most recently used entry.
    _rotate(s, 3)
    hit = render_preview(s.edl, s.dir)
    assert hit.cached and hit.path == old
    budget = old.stat().st_size + 1
    cache_budget.enforce_session(s.dir, budget=budget)
    assert old.exists(), "the entry just reused was evicted before an unused one"
    assert not newer.exists()


def test_workdir_budget_evicts_the_idle_project_first(tmp_path, src, fast_lru):
    idle = _store(tmp_path / "s_idle00001", src)
    render_preview(idle.edl, idle.dir)
    time.sleep(1.1)                      # mtime granularity on some filesystems
    active = _store(tmp_path / "s_active0001", src)
    render_preview(active.edl, active.dir)
    active_bytes = _cache_bytes(active.dir)
    cache_budget.enforce_workdir(tmp_path, budget=active_bytes, force=True)
    assert _cache_bytes(idle.dir) == 0, "the idle project kept its cache"
    assert _cache_bytes(active.dir) == active_bytes


def test_only_render_caches_are_ever_evicted(tmp_path, fast_lru):
    """AI outputs under cache/ become clip sources; exports are deliverables."""
    sd = tmp_path / "s_keepme0001"
    keep = [sd / "cache" / "stabilize" / "clip.mp4", sd / "cache" / "upscale" / "x.mp4",
            sd / "exports" / "Film 1920x1080 30fps q18.mp4", sd / "uploads" / "a.mp4",
            sd / "previews" / ".abc.123.part.mp4"]
    gone = [sd / "previews" / "0123456789abcdef.mp4", sd / "cache" / "chunks" / "chunk_x.mp4",
            sd / "cache" / "chunks" / "seg_y.mp4", sd / "cache" / "videos" / "video_z_540.mp4"]
    for p in keep + gone:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 4096)
    cache_budget.enforce_session(sd, budget=0)
    assert all(p.exists() for p in keep)
    assert not any(p.exists() for p in gone)


def test_render_cache_route_reports_and_clears(tmp_path, src, monkeypatch):
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    c = TestClient(app)
    sid = c.post("/api/sessions", json={"name": "cache"}).json()["id"]
    c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_clip", "args": {
        "track": "v1", "src": str(src), "in": 0.0, "out": 6.0, "start": 0.0}})
    first = c.post(f"/api/sessions/{sid}/preview").json()["edl_hash"]
    edl = c.get(f"/api/sessions/{sid}/edl").json()
    cid = next(t for t in edl["tracks"] if t["id"] == "v1")["clips"][0]["id"]
    c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "set_clip_transform", "args": {"clip_id": cid, "rotation": 7}})
    cur = c.post(f"/api/sessions/{sid}/preview").json()["edl_hash"]

    u = c.get(f"/api/sessions/{sid}/render-cache").json()
    assert u["bytes"] > 0 and u["budget_bytes"] > 0
    assert u["by_area"]["previews"] > 0

    r = c.delete(f"/api/sessions/{sid}/render-cache").json()
    assert r["freed_bytes"] > 0
    pdir = tmp_path / sid / "previews"
    assert (pdir / f"{cur}.mp4").exists(), "Clear removed the preview on screen"
    assert not (pdir / f"{first}.mp4").exists()
    assert r["bytes"] == os.path.getsize(pdir / f"{cur}.mp4")


def test_a_renders_own_working_set_is_never_evicted_by_its_budget_pass(tmp_path, fast_lru):
    """A timeline whose working set alone exceeds the budget keeps it: what
    this render used would otherwise be rebuilt on the very next edit."""
    sd = tmp_path / "s_working01"
    old = sd / "cache" / "chunks" / "chunk_old.mp4"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"o" * 8192)
    os.utime(old, (time.time() - 600, time.time() - 600))
    started = time.time()
    fresh = [sd / "cache" / "chunks" / f"chunk_new{i}.mp4" for i in range(3)]
    for p in fresh:
        p.write_bytes(b"n" * 8192)
    cache_budget.enforce_session(sd, budget=4096, since=started)
    assert not old.exists()
    assert all(p.exists() for p in fresh)
