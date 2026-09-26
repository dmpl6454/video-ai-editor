"""Fixtures for the WKWebView acceptance suites (marker ``wk``).

``wk`` tests are skipped automatically off macOS, without a window server
(ssh, headless CI), or with ``VAI_WK=0``. Everything they need is built per
session: the frontend testkit bundle (esbuild over the REAL
``frontend/src/lib/preview`` modules, so the pages run the code the app
ships) and the bar-coded proxy-shaped fixture sources.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import PageServer, WKHarness, wk_unavailable_reason
from .proxy_fixture import build_wk_fixtures

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
PAGES = Path(__file__).resolve().parent / "pages"
TESTKIT_ENTRY = FRONTEND / "src" / "lib" / "preview" / "testkit" / "wkMsePage.ts"

_REASON = wk_unavailable_reason()


def pytest_collection_modifyitems(config, items):
    if _REASON is None:
        return
    skip = pytest.mark.skip(reason=f"wk: {_REASON}")
    for item in items:
        if item.get_closest_marker("wk") is not None:
            item.add_marker(skip)


def _esbuild() -> Path | None:
    exe = FRONTEND / "node_modules" / ".bin" / "esbuild"
    return exe if exe.exists() else None


@pytest.fixture(scope="session")
def wk_testkit(tmp_path_factory) -> Path:
    """The testkit page bundle, built from frontend/src by esbuild."""
    exe = _esbuild()
    if exe is None:
        pytest.skip("frontend/node_modules not installed (npm install), so no esbuild for the testkit bundle")
    out = tmp_path_factory.mktemp("wk-testkit")
    subprocess.run(
        [str(exe), str(TESTKIT_ENTRY), "--bundle", "--format=esm", "--target=safari16",
         "--sourcemap=inline", f"--outfile={out / 'wkMsePage.js'}", "--log-level=warning"],
        check=True, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return out


@pytest.fixture(scope="session")
def wk_media(tmp_path_factory) -> Path:
    """Directory holding the proxy-shaped bar-coded fixture sources A, B, C."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    cache = os.environ.get("VAI_WK_FIXTURE_CACHE")
    root = Path(cache) if cache else tmp_path_factory.mktemp("wk-media")
    dirs = build_wk_fixtures(root)
    return next(iter(dirs.values())).parent


@pytest.fixture(scope="session")
def wk_server(wk_testkit, wk_media):
    server = PageServer({"pages": PAGES, "testkit": wk_testkit, "media": wk_media})
    yield server
    server.close()


@pytest.fixture(scope="session")
def wk(wk_server, tmp_path_factory):
    """A :class:`WKHarness`: ``wk.run('pages/mse.html', {...})`` → posted JSON."""
    harness = WKHarness(wk_server, tmp_path_factory.mktemp("wk-runs"))
    yield harness
    harness.close()
