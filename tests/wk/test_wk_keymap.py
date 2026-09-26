"""The keymap in real WKWebView, driven by NATIVE key events (LEFT_RAIL_SPEC
§4, R4; risk 3: "⌘E and ⌥⌘K may never reach the page").

``wk_keys_child.py`` opens ``frontend/src/keymap/wkKeymapPage.ts`` (the real
engine and command registry, bundled by esbuild) in a 4 px WKWebView and plays
NSEvents through ``-[WKWebView performKeyEquivalent:]`` / ``keyDown:`` — the
route AppKit takes once the app's window has the key. The page logs, per key,
what WebCore delivered (``code`` and modifiers), the chord and command the
engine resolved, whether the scope rule ran it, whether it was handled, and
the effect (layout store, which stand-in control was pressed).

The pywebview host adds two more places a key could be taken before the page:
its main menu's key equivalents and ``BrowserView.WebKitHost.keyDown_``. The
static test below reads both out of the installed pywebview and checks no R4
chord is among them.
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import FRONTEND, _esbuild
from .harness import PageServer, WKPageError

CHILD = Path(__file__).resolve().parent / "wk_keys_child.py"
ENTRY = FRONTEND / "src" / "keymap" / "wkKeymapPage.ts"

# (focus, key) → what must happen. `runs`/`handled` False = left to the target.
STEPS = [
    ("timeline", "cmd+e", {"code": "KeyE", "meta": True, "command": "exportVideo", "runs": True, "clicked": "export"}),
    ("row", "alt+8", {"code": "Digit8", "alt": True, "command": "panelAI", "runs": True, "leftTab": "ai"}),
    ("row", "cmd+alt+k", {"code": "KeyK", "meta": True, "alt": True, "command": "openShortcuts", "runs": True,
                          "clicked": "customize"}),
    ("prompt", "alt+1", {"code": "Digit1", "alt": True, "command": "panelMedia", "runs": False, "leftTab": "ai"}),
    ("timeline", "alt+1", {"code": "Digit1", "command": "panelMedia", "runs": True, "leftTab": "media"}),
    ("timeline", "alt+backslash", {"code": "Backslash", "command": "toggleToolPanel", "runs": True, "leftOpen": False}),
    ("timeline", "alt+backslash", {"code": "Backslash", "command": "toggleToolPanel", "runs": True, "leftOpen": True}),
    # The focused tab keeps Space: WebKit's own button activation clicks it.
    ("rail-tab-media", "space", {"code": "Space", "command": "playPause", "runs": False, "clicked": "rail-tab-media"}),
    ("rail-tab-media", "n", {"code": "KeyN", "command": "toggleSnap", "runs": True}),
    ("rail-tab-media", "cmd+z", {"code": "KeyZ", "meta": True, "command": "undo", "runs": True}),
    ("row", "alt+t", {"code": "KeyT", "alt": True, "command": "addText", "runs": True, "clicked": "addtext"}),
    ("prompt", "f6", {"code": "F6", "command": "cycleRegion", "runs": True}),
    ("timeline", "alt+9", {"code": "Digit9", "command": "showInspector", "runs": True}),
]


@pytest.fixture(scope="module")
def keys_page(tmp_path_factory) -> Path:
    exe = _esbuild()
    if exe is None:
        pytest.skip("frontend/node_modules not installed (npm install), so no esbuild")
    out = tmp_path_factory.mktemp("wk-keys")
    subprocess.run(
        [str(exe), str(ENTRY), "--bundle", "--format=esm", "--target=safari16", "--loader:.css=empty",
         "--define:process.env.NODE_ENV=\"production\"", f"--outfile={out / 'wkKeymapPage.js'}",
         "--log-level=warning"],
        check=True, cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    (out / "keys.html").write_text(
        '<!doctype html><meta charset="utf-8"><body></body>'
        '<script type="module" src="wkKeymapPage.js"></script>', encoding="utf-8")
    return out


def _run(server: PageServer, work: Path, steps: list[dict], timeout: float = 60.0) -> tuple[dict, str]:
    token = secrets.token_hex(8)
    done = work / f"{token}.done"
    with server.box.cond:
        server.box.done_files[token] = done
    url = server.url("keys/keys.html", {"token": token})
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), "--url", url, "--done", str(done), "--timeout", str(timeout),
         "--steps", json.dumps(steps)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        out, _ = proc.communicate(timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    with server.box.cond:
        body = server.box.results.pop(token, None)
        errors = server.box.errors.pop(token, [])
        server.box.done_files.pop(token, None)
    if body is None:
        raise WKPageError(f"keys page posted no result (exit {proc.returncode}); errors {errors}; child: {out[-800:]}")
    assert "closed" in out, out          # the 4 px window is gone when the run ends
    return json.loads(body), out


@pytest.mark.wk
def test_native_keys_reach_the_engine_in_wkwebview(keys_page, tmp_path):
    server = PageServer({"keys": keys_page})
    try:
        result, child_out = _run(server, tmp_path, [{"focus": f, "key": k} for f, k, _ in STEPS])
    finally:
        server.close()
    log = result["log"]
    assert result["platform"] == "MacIntel", result
    assert len(log) == len(STEPS), (log, child_out)
    for (focus, key, want), got in zip(STEPS, log):
        where = f"{key} on #{focus}: {got}"
        assert got["target"] == focus, where
        assert got["code"] == want["code"], where
        assert got["meta"] is want.get("meta", False), where
        if "alt" in want:
            assert got["alt"] is True, where
        assert got["command"] == want["command"], where
        assert got["runs"] is want["runs"], where
        # The engine handles (preventDefault) exactly the keys it runs; the
        # rest stay with the target (typing, the tab's own Space).
        assert got["handled"] is want["runs"], where
        if "leftTab" in want:
            assert got["leftTab"] == want["leftTab"], where
        if "leftOpen" in want:
            assert got["leftOpen"] is want["leftOpen"], where
        if "clicked" in want:
            assert got["clicked"][-1:] == [want["clicked"]], where
    # ⌘ chords went through performKeyEquivalent, and WKWebView took each one.
    for key in ("cmd+e", "cmd+alt+k", "cmd+z"):
        assert f"{key}: performKeyEquivalent -> True" in child_out, child_out


def _pywebview_cocoa() -> str:
    try:
        import webview  # noqa: PLC0415
    except ImportError:
        pytest.skip("pywebview not installed")
    src = Path(webview.__file__).resolve().parent / "platforms" / "cocoa.py"
    if not src.exists():
        pytest.skip("pywebview has no cocoa backend here")
    return src.read_text(encoding="utf-8")


def test_the_pywebview_host_claims_no_r4_chord():
    """The app window is pywebview's: its main menu's key equivalents and its
    WKWebView subclass's ⌘-key overrides could take a chord before the page.
    ⌘E and ⌥⌘K (and the plain ⌥ chords) must be none of them."""
    src = _pywebview_cocoa()
    menu_keys = set(re.findall(r"keyEquivalent_\(\s*[^,]+,\s*'[^']*',\s*'([^']*)'", src))
    menu_keys |= {k for k in re.findall(r"\(\s*self\.localization\[[^\]]+\],\s*'[^']+',\s*'([^']*)'\s*\)", src)}
    host = src[src.index("class WebKitHost"):]
    host = host[host.index("def keyDown_"):]
    host = host[:host.index("super(BrowserView.WebKitHost, self).keyDown_")]
    cmd_chars = set(re.findall(r"char == '([^']+)'", host))
    # Sanity: the scan found pywebview's known ones (Quit, Hide, Copy…, undo).
    assert {"q", "h", "c", "v"} <= menu_keys, menu_keys
    assert {"z", "q", "w"} <= cmd_chars, cmd_chars
    for ch in ("e", "k"):
        assert ch not in menu_keys, f"pywebview's menu binds ⌘{ch.upper()}"
        assert ch not in cmd_chars, f"pywebview's WebKitHost.keyDown_ takes ⌘{ch.upper()}"
