"""The benchmark package must leave the process exactly as it found it.

`harness.open_bench` arms a process-wide socket EgressGuard, sets BENCH_ENV
(`HF_HUB_OFFLINE=1`, an empty `ANTHROPIC_API_KEY`, …), redirects WORKDIR and
replaces the model / voice download entry points. All of that is correct
INSIDE the benchmark and poison outside it: when the `bench` fixture was
session-scoped, any marker expression that collected tests/benchmark
(`-m "not wk"` replaces pyproject's `-m 'not benchmark'`) kept it armed for
every later test in the run, and seven unrelated tests failed only in the full
run — a socket-guard self-test, an emoji sticker add, the LAN posture and key
tests, a reel render (gate X3).

The hooks below are the regression net for that class of leak, not just
that instance: they snapshot every piece of global state the harness is known
to touch before the FIRST benchmark test sets up any fixture, and fail the
last benchmark test's teardown if any of it has not come back by the time the
run leaves the package. Hooks, not an autouse fixture: pytest sets
higher-scoped fixtures up first, so a package fixture's "before" would be
taken AFTER a session-scoped `bench` had already armed everything, and would
compare clean against the very leak it exists to catch.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest

from .harness import BENCH_ENV


def _global_state() -> dict[str, Any]:
    from video_ai_editor import config, main as _main, storage
    from video_ai_editor.ai import tts as _tts
    from video_ai_editor.agent.prompt.brains import router as _router

    state: dict[str, Any] = {
        "socket.socket.connect": socket.socket.connect,
        "socket.socket.connect_ex": socket.socket.connect_ex,
        "socket.create_connection": socket.create_connection,
        "socket.getaddrinfo": socket.getaddrinfo,
        "config.WORKDIR": config.WORKDIR,
        "storage.WORKDIR": storage.WORKDIR,
        "main.WORKDIR": _main.WORKDIR,
        "main._STORES": _main._STORES,
        "config._FORCED_RESTRICT": config._FORCED_RESTRICT,
        "tts.download_voice": getattr(_tts, "download_voice", None),
        # Singletons that freeze the env they are built under (CloudBrain
        # reads VAI_PROMPT_CLOUD once) and the memoised brains report: built
        # inside the benchmark, they kept the claude rung off for the rest of
        # the run. The singleton by identity (the same object, or None, must
        # come back); the cache by content, since it is filled in place.
        "router._DEFAULT_BRAINS": _router._DEFAULT_BRAINS,
        "router._REPORT_CACHE": dict(_router._REPORT_CACHE),
    }
    for k in (*BENCH_ENV, "VAI_BRAIN"):
        state[f"env:{k}"] = os.environ.get(k)
    try:
        import huggingface_hub
        state["huggingface_hub.snapshot_download"] = huggingface_hub.snapshot_download
    except ImportError:
        pass
    return state


_PKG = Path(__file__).resolve().parent
_BASELINE: dict[str, Any] | None = None


def _in_package(item: pytest.Item | None) -> bool:
    return item is not None and Path(str(item.path)).resolve().is_relative_to(_PKG)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Before the first benchmark item's fixtures: the state the run came in with."""
    global _BASELINE
    if _BASELINE is None and _in_package(item):
        _BASELINE = _global_state()


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """After the fixture teardown of the last benchmark item before the run
    leaves the package: everything must be back."""
    global _BASELINE
    if _BASELINE is None or not _in_package(item) or _in_package(nextitem):
        return
    before, _BASELINE = _BASELINE, None
    after = _global_state()
    leaked = sorted(k for k in before if after.get(k) is not before[k] and after.get(k) != before[k])
    assert not leaked, (
        "tests/benchmark left process-wide state changed for every test after it: "
        f"{leaked}. Scope whatever set it to the benchmark (the `bench` fixture is "
        "module-scoped for exactly this reason).")
