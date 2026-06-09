"""Parallel chunk rendering: correctness + worker selection.

The render path renders missing per-clip chunks concurrently across the
performance cores. These tests lock in that:
  - a multi-clip cold cache produces one valid chunk per clip,
  - the result is identical regardless of worker count (parallel == serial),
  - the worker-count heuristic respects VAI_CHUNK_WORKERS and clamps sanely.
"""
from __future__ import annotations
import os
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl.schema import Clip
from video_ai_editor.render import chunks as C
from video_ai_editor.render.compositor import (
    _build_clip_video_chain, _build_clip_audio_chain, _video_encoder_args,
)


def _mk(path: Path, color: str):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c={color}:s=320x180:d=1:r=30",
         "-f", "lavfi", "-i", "sine=f=440:d=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True,
    )


def _clips(tmp_path: Path, n: int) -> list[Clip]:
    colors = ["red", "green", "blue", "yellow", "cyan", "magenta", "white", "orange"]
    out = []
    for i in range(n):
        s = tmp_path / f"s{i}.mp4"
        _mk(s, colors[i % len(colors)])
        out.append(Clip(src=str(s), in_=0, out=1, start=i, id=f"c{i}"))
    return out


def _kw():
    return dict(
        canvas_w=320, canvas_h=180, fps=30,
        encoder_args=_video_encoder_args(preview=True),
        build_video_chain=_build_clip_video_chain,
        build_audio_chain=_build_clip_audio_chain,
    )


def test_worker_count_respects_env(monkeypatch):
    monkeypatch.setenv("VAI_CHUNK_WORKERS", "3")
    assert C._chunk_workers(8) == 3
    monkeypatch.setenv("VAI_CHUNK_WORKERS", "garbage")
    # bad value → falls back to the core heuristic, clamped to n_clips
    assert C._chunk_workers(2) == 2


def test_worker_count_clamps_to_clip_count(monkeypatch):
    monkeypatch.delenv("VAI_CHUNK_WORKERS", raising=False)
    # Never more workers than there are clips to build.
    assert C._chunk_workers(1) == 1
    assert C._chunk_workers(2) <= 2


def test_multiclip_parallel_builds_all_chunks(tmp_path: Path):
    clips = _clips(tmp_path, 5)
    cache = tmp_path / "cache"
    paths = C.get_or_build_chunks(clips, cache_dir=cache, **_kw())
    assert len(paths) == 5
    for p in paths:
        assert p.exists() and p.stat().st_size > 1024
        assert C.chunk_is_valid(p)


def test_parallel_matches_serial_fingerprints(tmp_path: Path, monkeypatch):
    clips = _clips(tmp_path, 4)
    # Serial
    monkeypatch.setenv("VAI_CHUNK_WORKERS", "1")
    ser = C.get_or_build_chunks(clips, cache_dir=tmp_path / "ser", **_kw())
    # Parallel
    monkeypatch.setenv("VAI_CHUNK_WORKERS", "4")
    par = C.get_or_build_chunks(clips, cache_dir=tmp_path / "par", **_kw())
    # Same fingerprints → same filenames (content-addressed), independent of
    # worker count. Filenames are the fingerprint, so basenames must match.
    assert [p.name for p in ser] == [p.name for p in par]
    for p in par:
        assert C.chunk_is_valid(p)


def test_warm_cache_is_noop(tmp_path: Path):
    clips = _clips(tmp_path, 3)
    cache = tmp_path / "cache"
    first = C.get_or_build_chunks(clips, cache_dir=cache, **_kw())
    mtimes = {p: p.stat().st_mtime_ns for p in first}
    # Second call: every chunk is a cache hit → no rebuild, mtimes unchanged.
    second = C.get_or_build_chunks(clips, cache_dir=cache, **_kw())
    assert [p.name for p in first] == [p.name for p in second]
    for p in second:
        assert p.stat().st_mtime_ns == mtimes[p], f"{p.name} was rebuilt on warm cache"
