"""Tab reaches every control in the app's WKWebView (review RD3).

By default WebKit on macOS tabs only to text fields unless the Mac's own
"Keyboard navigation" setting is on: the editor had six Tab stops, and Import,
Export, Split, Freeze or any Inspector button were out of a keyboard user's
reach (Option-Tab was the only way). The desktop window now turns on
WKPreferences.tabFocusesLinks (`desktop.enable_tab_to_all_controls`, applied
by `_mac_keyboard_tabbing`), Safari's "Press Tab to highlight each item".

Driven by native Tab key events through ``wk_keys_child.py`` in a real
WKWebView, with and without the app's setting.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from .harness import PageServer, WKPageError

pytestmark = pytest.mark.wk

CHILD = Path(__file__).resolve().parent / "wk_keys_child.py"
PAGES = Path(__file__).resolve().parent / "pages"


def _tabs(tmp: Path, tab_to_all: bool, n: int = 5) -> tuple[list, str]:
    server = PageServer({"pages": PAGES})
    try:
        token = secrets.token_hex(8)
        done = tmp / f"{token}.done"
        with server.box.cond:
            server.box.done_files[token] = done
        steps = [{"focus": "field" if i == 0 else "", "key": "tab"} for i in range(n)]
        proc = subprocess.run(
            [sys.executable, str(CHILD), "--url", server.url("pages/tab_focus.html", {"token": token}),
             "--done", str(done), "--timeout", "40", "--steps", json.dumps(steps),
             *(["--tab-to-all"] if tab_to_all else [])],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        with server.box.cond:
            body = server.box.results.pop(token, None)
            server.box.done_files.pop(token, None)
        if body is None:
            raise WKPageError(f"tab page posted nothing (exit {proc.returncode}): {proc.stdout[-800:]}")
        assert "closed" in proc.stdout, proc.stdout   # the 4 px window is gone
        return json.loads(body)["log"], proc.stdout
    finally:
        server.close()


def _keyboard_navigation_on() -> bool:
    out = subprocess.run(["defaults", "read", "-g", "AppleKeyboardUIMode"], capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() not in ("", "0")


def test_tab_reaches_the_buttons_with_the_apps_setting(tmp_path):
    log, child = _tabs(tmp_path, tab_to_all=True)
    assert "tab to all controls: True" in child, child
    assert log[:4] == ["import", "export", "keep", "split"], log


def test_without_it_tab_skips_the_buttons(tmp_path):
    """The state the review measured (the Mac's Keyboard navigation off)."""
    if _keyboard_navigation_on():
        pytest.skip("this Mac has Keyboard navigation on: Tab reaches buttons anyway")
    log, _ = _tabs(tmp_path, tab_to_all=False, n=2)
    assert log[0] == "notes", log
