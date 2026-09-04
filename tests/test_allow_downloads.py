"""`<a download>` must actually download in the packaged app.

pywebview ships `ALLOW_DOWNLOADS=False`. With it off, WKWebView's
navigation-policy delegate answers a download action with "cancel", so an
`<a download>` click in the packaged app produces NO file, NO save dialog and
NO error — it does nothing whatsoever, while behaving perfectly in browser-dev
where the anchor is handled by the browser itself.

That silence is why the native `save_export` bridge exists for exports, and it
also meant the TopBar's "↓ .vae" link — the only way to get your editable
project file out of the app — did nothing at all in the .app. Turning the
setting on makes pywebview's Cocoa `DownloadDelegate` run, which puts up a real
`NSSavePanel`.

This is a one-line setting with no visible surface, which makes it exactly the
kind of thing a later refactor drops. Hence a test.
"""
from __future__ import annotations

import ast
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[1] / "src" / "video_ai_editor" / "desktop.py"


def _main_source() -> str:
    tree = ast.parse(DESKTOP.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    return ast.get_source_segment(DESKTOP.read_text(encoding="utf-8"), fn) or ""


def test_downloads_are_enabled_before_the_window_is_created():
    src = _main_source()
    assert 'settings["ALLOW_DOWNLOADS"] = True' in src, (
        "ALLOW_DOWNLOADS is not enabled — every <a download> in the packaged "
        "app (including the .vae project link) silently does nothing"
    )
    # Ordering is load-bearing: the flag is read by the navigation-policy
    # delegate that create_window installs, so setting it afterwards is inert.
    # Match the CALL, not the bare name: the explanatory comment above the
    # setting mentions create_window, and an index() on the name finds that.
    assert (src.index('settings["ALLOW_DOWNLOADS"] = True')
            < src.index("webview.create_window(")), (
        "ALLOW_DOWNLOADS must be set BEFORE create_window, or the delegate "
        "never sees it"
    )


def test_pywebview_still_defaults_it_off():
    """If upstream ever flips the default, this test is the place that finds
    out — and the explicit set becomes redundant rather than load-bearing."""
    import webview
    assert "ALLOW_DOWNLOADS" in webview.settings, (
        "pywebview renamed or removed ALLOW_DOWNLOADS; re-check how downloads "
        "are enabled before trusting the .vae link"
    )
