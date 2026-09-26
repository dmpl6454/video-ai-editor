"""WKWebView acceptance harness (INSTANT_PREVIEW_SPEC §9.5, §13).

The product runs in WKWebView (pywebview's cocoa backend), never Chromium,
so every engine-dependent assertion runs here. This is the design-phase probe
``wk.py`` promoted into the repo:

* a ``WKWebViewConfiguration`` like pywebview 6.2.1's — the default
  configuration with ``WKWebsiteDataStore.defaultDataStore()``;
* an ON-SCREEN borderless window 4 px square (a hidden or occluded page gets
  throttled timers and paused media) at status-window level, never activated;
* the page is served over loopback by :class:`PageServer`, and posts its JSON
  result to ``/__result/<token>``; the WebKit process exits as soon as the
  result lands (or at its deadline, or as soon as its parent dies), closing
  the window;
* each child's window sits at its own 4 px slot (:func:`window_origin`), so
  concurrent runs from different pytest processes never cover each other.

The WebKit side runs in a CHILD process (``python harness.py --url …``): AppKit
wants the main thread and its own run loop, and a separate process guarantees
the window is gone when the run ends, even if the page hangs or pytest is
interrupted (:meth:`WKHarness.close` kills stragglers).
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlencode

WINDOW_PX = 4
#: Window slots: each WebKit child sits at its own 4 px spot (from its pid),
#: WINDOW_GAP apart, so concurrent runs from different pytest processes never
#: cover each other (a covered page is throttled like a hidden one).
WINDOW_GAP = 2
_SLOTS_PER_ROW = 32
_SLOTS = 256
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --------------------------------------------------------------- availability

def wk_unavailable_reason() -> str | None:
    """Why the WK suites cannot run here, or None when they can."""
    if os.environ.get("VAI_WK", "").strip() == "0":
        return "VAI_WK=0"
    if sys.platform != "darwin":
        return "WKWebView harness needs macOS"
    try:
        import Quartz  # noqa: PLC0415 - optional, macOS-only
        import WebKit  # noqa: F401, PLC0415
    except ImportError as exc:  # pragma: no cover - pyobjc ships with pywebview on macOS
        return f"PyObjC WebKit/Quartz not importable ({exc})"
    session = Quartz.CGSessionCopyCurrentDictionary()
    if session is None:
        return "no window server (no GUI login session, e.g. ssh or CI without a console)"
    if not Quartz.CGMainDisplayID():
        return "no display attached to the window server"
    return None


def window_origin(pid: int) -> tuple[int, int]:
    """(x, y) screen origin of the 4 px window of the child with ``pid``."""
    slot = int(pid) % _SLOTS
    step = WINDOW_PX + WINDOW_GAP
    return (slot % _SLOTS_PER_ROW) * step, (slot // _SLOTS_PER_ROW) * step


# ------------------------------------------------------------------- server

@dataclass
class _Mailbox:
    results: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)
    done_files: dict[str, Path] = field(default_factory=dict)
    #: token -> the child's window-control file (POST /__window/<token>/hide|show)
    control_files: dict[str, Path] = field(default_factory=dict)
    cond: threading.Condition = field(default_factory=threading.Condition)


class PageServer:
    """Loopback static server with mounted roots and a result mailbox.

    ``mounts`` maps a URL prefix (``"pages"``, or nested like ``"media/A"``;
    the longest match wins) to a directory. GET serves files under a mount
    (no traversal outside it, symlinks included). POST ``/__result/<token>`` stores
    the page's JSON result; POST ``/__error/<token>`` records an uncaught page
    error so a failing page reports why instead of timing out silently.
    """

    def __init__(self, mounts: dict[str, Path], port: int = 0):
        self.mounts = {k.strip("/"): Path(v).resolve() for k, v in mounts.items()}
        self.mount_order = sorted(self.mounts, key=len, reverse=True)
        self.box = _Mailbox()
        self.request_log: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # quiet
                return

            def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass

            def do_POST(self):  # noqa: N802 - http.server API
                n = int(self.headers.get("content-length", 0) or 0)
                body = self.rfile.read(n)
                if window_command(server.box, self.path):
                    return self._send(204)
                m = re.match(r"^/__(result|error)/([^/?]+)", self.path)
                if not m or not _TOKEN.match(m.group(2)):
                    return self._send(404)
                kind, token = m.groups()
                with server.box.cond:
                    if kind == "result":
                        server.box.results[token] = body
                        done = server.box.done_files.get(token)
                        if done is not None:
                            done.write_text("done")
                    else:
                        server.box.errors.setdefault(token, []).append(body.decode("utf-8", "replace"))
                    server.box.cond.notify_all()
                return self._send(204)

            def do_GET(self):  # noqa: N802
                path = unquote(self.path.split("?", 1)[0])
                server.request_log.append(path)
                rel = path.lstrip("/")
                # longest mount prefix wins, so "media/A" can sit beside "media"
                prefix = next((m for m in server.mount_order if rel.startswith(m + "/")), None)
                if prefix is None:
                    return self._send(404)
                root = server.mounts[prefix]
                target = (root / rel[len(prefix) + 1:]).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    return self._send(404)
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if target.suffix in (".js", ".mjs"):
                    ctype = "text/javascript"
                return self._send(200, target.read_bytes(), ctype)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="wk-page-server", daemon=True)
        self._thread.start()

    def url(self, path: str, query: dict[str, object] | None = None) -> str:
        q = f"?{urlencode(query)}" if query else ""
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}{q}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def window_command(box: _Mailbox, path: str) -> bool:
    """POST /__window/<token>/hide|show|occlude|reveal: tell that page's
    WebKit child to order its window out (the page becomes hidden, as when
    the user switches Space or minimises) or back in; or to cover it with an
    opaque window of its own (occluded, as under another app's window) and
    take that cover away. True when `path` was such a command."""
    m = re.match(r"^/__window/([^/?]+)/(hide|show|occlude|reveal)$", path)
    if not m or not _TOKEN.match(m.group(1)):
        return False
    with box.cond:
        ctl = box.control_files.get(m.group(1))
    if ctl is not None:
        ctl.write_text(m.group(2))
    return True


# ------------------------------------------------------------------ harness

class WKPageError(AssertionError):
    """The page did not post a result (timeout, crash, or uncaught error)."""


@dataclass
class WKRun:
    result: dict
    errors: list[str]
    elapsed_s: float
    stdout: str
    pid: int = 0


class WKHarness:
    """Load pages in real WKWebView and collect the JSON they post."""

    def __init__(self, server: PageServer, work_dir: Path):
        self.server = server
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._procs: list[subprocess.Popen] = []

    def run(self, path: str, query: dict[str, object] | None = None, timeout: float = 60.0) -> WKRun:
        token = secrets.token_hex(8)
        done = self.work_dir / f"{token}.done"
        control = self.work_dir / f"{token}.ctl"
        with self.server.box.cond:
            self.server.box.done_files[token] = done
            self.server.box.control_files[token] = control
        url = self.server.url(path, {**(query or {}), "token": token})
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--url", url, "--done", str(done),
             "--timeout", str(timeout), "--control", str(control)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        self._procs.append(proc)
        try:
            out, _ = proc.communicate(timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        finally:
            self._procs.remove(proc)
        elapsed = time.monotonic() - t0
        with self.server.box.cond:
            body = self.server.box.results.pop(token, None)
            errors = self.server.box.errors.pop(token, [])
            self.server.box.done_files.pop(token, None)
            self.server.box.control_files.pop(token, None)
        if body is None:
            raise WKPageError(
                f"{path} posted no result within {timeout:.0f}s (exit {proc.returncode}); "
                f"page errors: {errors or 'none'}; webkit stdout: {out.strip()[-800:]}")
        result = json.loads(body)
        if isinstance(result, dict) and result.get("fatal"):
            raise WKPageError(f"{path} failed in the page: {result['fatal']}")
        return WKRun(result=result, errors=errors, elapsed_s=elapsed, stdout=out, pid=proc.pid)

    def close(self) -> None:
        for proc in list(self._procs):
            if proc.poll() is None:
                proc.kill()
        self._procs.clear()


# ------------------------------------------------- the WebKit child process

def _webview_main(url: str, done: Path, timeout: float,
                  control: Path | None = None) -> int:  # pragma: no cover - runs in the child
    import AppKit  # noqa: PLC0415
    import Foundation  # noqa: PLC0415
    import WebKit  # noqa: PLC0415
    from PyObjCTools import AppHelper  # noqa: PLC0415

    deadline = time.time() + timeout
    parent = os.getppid()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon, never steals focus
    # App Nap. An accessory app with a 4 px window and no user input is exactly
    # what macOS throttles when the machine is busy, and a throttled host stops
    # driving WebKit's display refresh: requestVideoFrameCallback just stops
    # mid-stream. Measured by the wave D1 gate under load (WindowServer ~75% CPU):
    # frames 1-129 presented perfectly at 33.37 ms/frame with waiting=0, then
    # nothing until the timeout. The shipped app is a large foreground window
    # and is not subject to it, so this is a harness fix, not an engine fix.
    # Hold a latency-critical, user-initiated activity for the harness's life
    # (runEventLoop blocks, so the local reference keeps it alive).
    activity = Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        Foundation.NSActivityUserInitiated | Foundation.NSActivityLatencyCritical
        | Foundation.NSActivityIdleDisplaySleepDisabled,
        "WK acceptance harness: real-time media playback")
    x, y = window_origin(os.getpid())
    rect = Foundation.NSMakeRect(x, y, WINDOW_PX, WINDOW_PX)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
    win.setReleasedWhenClosed_(False)
    config = WebKit.WKWebViewConfiguration.alloc().init()
    config.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.defaultDataStore())
    # The web view is laid out at a normal size inside the 4 px window, as in
    # the probe: layout, media and rVFC behave as in the app; only 4 px show.
    wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(Foundation.NSMakeRect(0, 0, 800, 600), config)
    win.contentView().addSubview_(wv)
    win.setLevel_(AppKit.NSStatusWindowLevel)
    # Occlusion. When the window is occluded, WKWebView reports the page as
    # hidden and WebKit PAUSES a muted video (its power policy), so the trace
    # stops mid-stream. Measured after the App Nap fix: visibility 'hidden',
    # paused true, currentTime frozen, zero dropped frames before it. A window
    # that exists on one Space is occluded the moment the user switches Space
    # or brings a full-screen app forward, which a user at the Mac does all the
    # time. Join every Space, including over full-screen apps, and stay out of
    # Exposé and window cycling.
    win.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    win.orderFrontRegardless()
    f = win.frame()
    _say(f"window {int(f.size.width)}x{int(f.size.height)} at {int(f.origin.x)},{int(f.origin.y)}")
    wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(url)))

    cover: list = []

    def occlude():
        # An opaque borderless window one level above ours, exactly over it:
        # the window server then reports ours occluded (the page is hidden
        # and WebKit pauses muted media, as under another app's window).
        c = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            win.frame(), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        c.setReleasedWhenClosed_(False)
        c.setOpaque_(True)
        c.setBackgroundColor_(AppKit.NSColor.blackColor())
        c.setLevel_(AppKit.NSStatusWindowLevel + 1)
        c.setCollectionBehavior_(win.collectionBehavior())
        c.orderFrontRegardless()
        cover.append(c)

    def reveal():
        while cover:
            c = cover.pop()
            c.orderOut_(None)
            c.close()

    def tick():
        if control is not None and control.exists():
            # hide/show on the page's request: an ordered-out window makes
            # WKWebView report the page hidden (WebKit then pauses muted media)
            cmd = control.read_text().strip()
            control.unlink(missing_ok=True)
            if cmd == "hide":
                win.orderOut_(None)
            elif cmd == "show":
                win.orderFrontRegardless()
            elif cmd == "occlude":
                occlude()
            elif cmd == "reveal":
                reveal()
            _say(f"window {cmd} occlusion={int(win.occlusionState())}")
        orphaned = os.getppid() != parent          # pytest was killed: reparented
        if done.exists() or time.time() > deadline or orphaned:
            reveal()
            wv.stopLoading_(None)
            win.orderOut_(None)
            win.close()
            finished = done.exists()
            _say("closed")
            _say("done" if finished else "orphaned" if orphaned else "deadline")
            # Exit HERE, not via stopEventLoop: that path needs the prints
            # above to succeed (a dead parent's pipe raises) and code after
            # runEventLoop is not guaranteed to run. The window is closed.
            os._exit(0 if finished else 3)
        AppHelper.callLater(0.1, tick)

    AppHelper.callLater(0.1, tick)
    AppHelper.runEventLoop(installInterrupt=True)
    Foundation.NSProcessInfo.processInfo().endActivity_(activity)
    return 3


def _say(line: str) -> None:
    """Print to the parent; a dead parent's pipe must not stop the child."""
    try:
        print(line, flush=True)
    except OSError:
        pass


if __name__ == "__main__":  # pragma: no cover - child entry point
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--done", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--control", type=Path, default=None)
    ns = ap.parse_args()
    sys.exit(_webview_main(ns.url, ns.done, ns.timeout, ns.control))
