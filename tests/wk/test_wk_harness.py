"""The WK harness itself: the page server (anywhere) and real WKWebView runs
(macOS with a window server; marker ``wk``)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from .conftest import PAGES
from .harness import (WINDOW_PX, PageServer, WKHarness, WKPageError, window_origin,
                      wk_unavailable_reason)


@pytest.fixture()
def server(tmp_path):
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "a.json").write_text('{"ok": 1}')
    (tmp_path / "secret.txt").write_text("outside the mount")
    srv = PageServer({"pages": PAGES, "m": tmp_path / "m"})
    yield srv
    srv.close()


def _get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, b"", ""


def test_page_server_serves_mounts_and_refuses_traversal(server):
    status, body, ctype = _get(server.url("m/a.json"))
    assert (status, json.loads(body), ctype) == (200, {"ok": 1}, "application/json")
    assert _get(server.url("pages/harness_probe.html"))[0] == 200
    for bad in ("m/../secret.txt", "m/%2e%2e/secret.txt", "nope/a.json", "m", "m/missing.json"):
        assert _get(server.url(bad))[0] == 404, bad


def test_page_server_mailbox_records_results_and_errors(server):
    def post(path: str, body: bytes) -> int:
        req = urllib.request.Request(server.url(path), data=body, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    assert post("__result/tok1", b'{"x": 2}') == 204
    assert post("__error/tok1", b"boom") == 204
    assert server.box.results["tok1"] == b'{"x": 2}'
    assert server.box.errors["tok1"] == ["boom"]
    with pytest.raises(urllib.error.HTTPError):
        post("__result/bad%20token", b"{}")


def test_availability_reason_honours_the_kill_switch(monkeypatch):
    monkeypatch.setenv("VAI_WK", "0")
    assert wk_unavailable_reason() == "VAI_WK=0"


def test_window_slots_are_4px_and_never_overlap():
    """Concurrent WK runs (lanes run pytest at the same time) must not stack
    their windows: a covered page is throttled like a hidden one. Every
    process gets its own 4 px spot from its pid."""
    rects = []
    for pid in range(1000, 1256):
        x, y = window_origin(pid)
        assert x >= 0 and y >= 0 and x < 400 and y < 400
        rects.append((x, y))
    assert len(set(rects)) == 256
    for i, (x1, y1) in enumerate(rects):
        for x2, y2 in rects[i + 1:]:
            assert abs(x1 - x2) >= WINDOW_PX or abs(y1 - y2) >= WINDOW_PX


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ------------------------------------------------------------ real WebKit

@pytest.fixture(scope="module")
def bare_wk(tmp_path_factory):
    srv = PageServer({"pages": PAGES})
    harness = WKHarness(srv, tmp_path_factory.mktemp("wk-bare"))
    yield harness
    harness.close()
    srv.close()


@pytest.mark.wk
def test_wk_runs_a_page_in_webkit_unthrottled_in_a_4px_window(bare_wk):
    run = bare_wk.run("pages/harness_probe.html", {"mode": "echo", "echo": "hello"}, timeout=30)
    r = run.result
    assert r["echo"] == "hello"
    assert "AppleWebKit" in r["ua"] and "Chrome" not in r["ua"] and r["chrome"] is False  # WebKit, not Chromium
    assert f"window {WINDOW_PX}x{WINDOW_PX}" in run.stdout
    x, y = window_origin(run.pid)
    assert f"at {x},{y}" in run.stdout                   # its own slot, not (0, 0)
    assert run.stdout.strip().endswith("done")  # the child closed its window and exited on the result
    # On screen, so neither rAF nor timers are throttled (a hidden page gets ~1 Hz).
    assert r["visibility"] == "visible" and r["hidden"] is False
    assert r["rafsIn500ms"] >= 15
    assert r["timers"] == 20 and r["timerMs"] < 1500
    # The engine surface the preview needs (§10).
    assert r["MediaSource"] and r["webgl2"] and r["rvfc"]


@pytest.mark.wk
def test_wk_times_out_cleanly_when_the_page_never_posts(bare_wk):
    t0 = time.monotonic()
    with pytest.raises(WKPageError, match="posted no result within 3s"):
        bare_wk.run("pages/harness_probe.html", {"mode": "never"}, timeout=3)
    assert time.monotonic() - t0 < 15
    assert bare_wk._procs == []  # the WebKit child (and its window) is gone


@pytest.mark.wk
def test_wk_reports_uncaught_page_errors(bare_wk):
    with pytest.raises(WKPageError, match="boom from the page"):
        bare_wk.run("pages/harness_probe.html", {"mode": "throw"}, timeout=4)


@pytest.mark.wk
def test_wk_concurrent_runs_are_not_throttled(bare_wk):
    """Two WebKit children at once (two pytest lanes): neither window covers
    the other, so both pages keep full-rate rAF and timers."""
    results: list = [None, None]

    def go(i: int) -> None:
        results[i] = bare_wk.run("pages/harness_probe.html", {"mode": "echo", "echo": str(i)},
                                 timeout=30)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert all(r is not None for r in results)
    assert window_origin(results[0].pid) != window_origin(results[1].pid)
    for r in results:
        assert r.result["visibility"] == "visible"
        assert r.result["rafsIn500ms"] >= 15, r.result
        assert r.result["timerMs"] < 1500, r.result


@pytest.mark.wk
def test_wk_child_exits_when_its_parent_is_killed(tmp_path):
    """A killed pytest (SIGTERM/SIGKILL from a CI or workflow timeout) must
    not leave an orphaned WebKit child and its window on screen until the
    child's own deadline — nor a process that never exits because printing
    to the dead parent's pipe raised."""
    srv = PageServer({"pages": PAGES})
    harness_py = Path(__file__).resolve().parent / "harness.py"
    url = srv.url("pages/harness_probe.html", {"mode": "never", "token": "orphan"})
    mid = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys; p = subprocess.Popen([sys.executable] + sys.argv[1:], "
         "stdout=subprocess.PIPE, stderr=subprocess.STDOUT); print(p.pid, flush=True); "
         "sys.stdin.read()",
         str(harness_py), "--url", url, "--done", str(tmp_path / "never.done"),
         "--timeout", "40"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    child = 0
    try:
        child = int(mid.stdout.readline())
        time.sleep(3.0)                              # the window is up, the page loaded
        assert _pid_alive(child)
        mid.send_signal(signal.SIGTERM)
        mid.wait(5)
        end = time.monotonic() + 5.0
        while _pid_alive(child) and time.monotonic() < end:
            time.sleep(0.1)
        assert not _pid_alive(child), "orphaned WebKit child is still running"
    finally:
        if child and _pid_alive(child):
            os.kill(child, signal.SIGKILL)
        if mid.poll() is None:
            mid.kill()
        srv.close()
