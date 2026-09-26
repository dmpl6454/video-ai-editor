"""The same WK probes on REAL proxies: bar-coded long-GOP masters (B-frames,
edit list, a 25 fps source among 30 fps ones, a portrait source) go through
``ingest/proxy.py``'s ``ProxyManager`` exactly as imports will, and the page
reads the proxy directories it wrote. This closes the loop master → proxy →
fmp4Writer → WebKit decode → the bar on screen (R5 frame identity end to end).
"""
from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from .playback import assert_drops_bounded

from .conftest import PAGES
from .harness import PageServer, WKHarness
from .proxy_fixture import FRAME_BITS, SourceSpec, encode_master

pytestmark = pytest.mark.wk

proxy_queue = pytest.importorskip("video_ai_editor.ingest.proxy_queue")
P = pytest.importorskip("video_ai_editor.ingest.proxy")

MASTERS = (
    SourceSpec("A", 9, 1280, 720, 240, "testsrc2", Fraction(30)),
    SourceSpec("B", 10, 1280, 720, 120, "testsrc", Fraction(25)),   # a 25 fps source in a 30 fps project
    SourceSpec("C", 11, 720, 1280, 60, "testsrc2", Fraction(30)),
)


@pytest.fixture(scope="module")
def real_proxies(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    from video_ai_editor import storage as _storage

    root = tmp_path_factory.mktemp("wk-real")
    masters = {s.name: encode_master(s, root / "masters" / f"{s.name}.mp4") for s in MASTERS}
    old = _storage.WORKDIR
    _storage.WORKDIR = root / "wd"
    _storage.WORKDIR.mkdir()
    manager = proxy_queue.ProxyManager()
    try:
        keys = {name: manager.ensure(path) for name, path in masters.items()}
        assert manager.wait_idle(180), "proxy builds did not finish"
        dirs = {name: P.proxy_dir(key) for name, key in keys.items()}
        for name, d in dirs.items():
            assert (d / "init.mp4").is_file(), f"{name}: no init.mp4 ({P.read_index(keys[name])})"
        yield dirs
    finally:
        manager.shutdown()
        manager.wait_idle(10)
        _storage.WORKDIR = old


@pytest.fixture(scope="module")
def real_wk(real_proxies, wk_testkit, tmp_path_factory):
    mounts = {"pages": PAGES, "testkit": wk_testkit, **{f"media/{n}": d for n, d in real_proxies.items()}}
    srv = PageServer(mounts)
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-real-runs"))
    yield harness
    harness.close()
    srv.close()


def _run(wk, scenario: str) -> dict:
    ids = ",".join(str(s.src_id) for s in MASTERS)
    return wk.run("pages/mse.html", {"scenario": scenario, "num": 30, "den": 1, "srcIds": ids}, timeout=90).result


def test_real_proxy_indexes_match_their_masters(real_proxies):
    for spec in MASTERS:
        idx = json.loads((real_proxies[spec.name] / "index.json").read_text())
        assert idx["frames"] == spec.frames
        assert (idx["w"], idx["h"]) == (spec.w, spec.h)
        assert Fraction(idx["src_rate"]["num"], idx["src_rate"]["den"]) == spec.rate


def test_real_proxies_paused_seeks_exact(real_wk):
    r = _run(real_wk, "seek_exact")
    bad = [s for s in r["seeks"] if s["atSeeked"] != s["exp"]]
    assert len(r["seeks"]) >= 42
    assert bad == [], [(s["k"], s["exp"] >> FRAME_BITS, s["exp"] & 2047, s["atSeeked"]) for s in bad]
    assert len(r["buffered"]) == 1
    # A 25 fps and a 30 fps proxy of one size need not share an avcC (x264 writes
    # the rate into the SPS VUI), so the writer may switch inits between them;
    # it must stay a small, exact number: one per class change in the plan.
    print(json.dumps({"real_proxy_inits": r["inits"], "codec": r["codec"]}))
    assert 3 <= r["inits"] <= 5


def test_real_proxies_init_switch_and_playback(real_wk):
    r = _run(real_wk, "init_switch")
    t = r["trace"]
    assert t["timedOut"] is False and t["waiting"] == 0
    assert t["mismatches"] == []
    assert_drops_bounded(t)
    assert [s for s in r["seeks"] if s["atSeeked"] != s["exp"]] == []


def test_real_proxies_rvfc_clock(real_wk):
    r = _run(real_wk, "rvfc_clock")
    assert r["timedOut"] is False and r["waiting"] == 0
    assert r["mismatches"] == [] and r["noPicture"] == 0
    assert r["maxOffGrid"] < 1e-9 and r["monotonic"] is True  # on the k/R grid to ~1e-14 (a few tfdt ticks off is ~1e-4)
