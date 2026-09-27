"""WebKit child that delivers NATIVE key events to a real WKWebView.

The app's shortcuts arrive as AppKit NSEvents, not DOM events: a chord only
works if WKWebView hands it to the page, and if the host's menu or the
pywebview ``WebKitHost.keyDown_`` override does not take it first. This child
opens the page like :mod:`harness` (a 4 px borderless window on every Space,
never activated, with the App Nap opt-out), makes the web view its window's
first responder, then plays ``--steps`` (JSON: ``[{"focus": id, "key": spec}]``)
the way AppKit routes a key once it has picked this window:

* a chord with ⌘ goes to ``-[WKWebView performKeyEquivalent:]`` (key
  equivalents are offered to the view hierarchy first; WKWebView gives the
  page the first chance at every one while it is first responder);
* any other key goes to ``-[WKWebView keyDown:]``;
* then ``keyUp:``.

A key spec is ``"cmd+alt+k"``-style: modifiers ``cmd``, ``alt``, ``shift``,
``ctrl`` and one key from :data:`KEYS`. After the last step it calls the
page's ``window.__keysDone()``, which posts the log to the harness mailbox,
and exits once the result lands (or at the deadline, or when orphaned). The
window is closed before exit.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import WINDOW_PX, _say, window_origin  # noqa: E402

#: macOS virtual key code, characters, characters ignoring modifiers (US).
KEYS: dict[str, tuple[int, str]] = {
    "1": (18, "1"), "2": (19, "2"), "3": (20, "3"), "8": (28, "8"), "9": (25, "9"), "0": (29, "0"),
    "e": (14, "e"), "k": (40, "k"), "t": (17, "t"), "z": (6, "z"), "j": (38, "j"), "l": (37, "l"),
    "n": (45, "n"), "space": (49, " "), "backslash": (42, "\\"), "f6": (97, ""),
    "tab": (48, "\t"),
}


def parse(spec: str) -> tuple[set[str], str]:
    *mods, key = spec.lower().split("+")
    return set(mods), key


def _event(AppKit, kind, spec: str, win):  # noqa: N803 - AppKit module
    mods, key = parse(spec)
    code, chars = KEYS[key]
    flags = 0
    if "cmd" in mods:
        flags |= AppKit.NSEventModifierFlagCommand
    if "alt" in mods:
        flags |= AppKit.NSEventModifierFlagOption
    if "shift" in mods:
        flags |= AppKit.NSEventModifierFlagShift
    if "ctrl" in mods:
        flags |= AppKit.NSEventModifierFlagControl
    if key == "f6":
        flags |= AppKit.NSEventModifierFlagFunction
    typed = chars.upper() if "shift" in mods and len(chars) == 1 else chars
    return AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(  # noqa: E501
        kind, (0, 0), flags, time.monotonic(), win.windowNumber(), None, typed, chars, False, code)


def main(url: str, done: Path, timeout: float, steps: list[dict],
         tab_to_all: bool = False) -> int:  # pragma: no cover - child process
    import AppKit  # noqa: PLC0415
    import Foundation  # noqa: PLC0415
    import WebKit  # noqa: PLC0415
    from PyObjCTools import AppHelper  # noqa: PLC0415

    deadline = time.time() + timeout
    parent = os.getppid()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    activity = Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        Foundation.NSActivityUserInitiated | Foundation.NSActivityLatencyCritical,
        "WK keymap harness")
    x, y = window_origin(os.getpid())
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        Foundation.NSMakeRect(x, y, WINDOW_PX, WINDOW_PX), AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered, False)
    win.setReleasedWhenClosed_(False)
    config = WebKit.WKWebViewConfiguration.alloc().init()
    config.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.defaultDataStore())
    wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(Foundation.NSMakeRect(0, 0, 800, 600), config)
    if tab_to_all:
        # the app's own setting (desktop.enable_tab_to_all_controls)
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from video_ai_editor.desktop import enable_tab_to_all_controls  # noqa: PLC0415
        _say(f"tab to all controls: {enable_tab_to_all_controls(wv)}")
    win.contentView().addSubview_(wv)
    win.setLevel_(AppKit.NSStatusWindowLevel)
    win.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    win.orderFrontRegardless()
    wv.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(url)))
    state = {"phase": "loading", "i": 0, "finished_at": None}

    def js(src: str, cb=None):
        wv.evaluateJavaScript_completionHandler_(src, cb or (lambda _r, _e: None))

    def close_and_exit(code: int):
        wv.stopLoading_(None)
        win.orderOut_(None)
        win.close()
        _say("closed")
        Foundation.NSProcessInfo.processInfo().endActivity_(activity)
        os._exit(code)

    def on_ready(result, _err):
        if result and state["phase"] == "loading":
            win.makeFirstResponder_(wv)
            _say(f"first responder: {win.firstResponder() == wv}")
            state["phase"] = "keys"

    def send_step():
        step = steps[state["i"]]
        down = _event(AppKit, AppKit.NSEventTypeKeyDown, step["key"], win)
        up = _event(AppKit, AppKit.NSEventTypeKeyUp, step["key"], win)
        if "cmd" in parse(step["key"])[0]:
            took = wv.performKeyEquivalent_(down)
            _say(f"{step['key']}: performKeyEquivalent -> {bool(took)}")
        else:
            wv.keyDown_(down)
        wv.keyUp_(up)
        state["i"] += 1

    def tick():
        orphaned = os.getppid() != parent
        if done.exists() or time.time() > deadline or orphaned:
            close_and_exit(0 if done.exists() else 3)
        if state["phase"] == "loading" and not wv.isLoading():
            js("!!window.__keysReady", on_ready)
        elif state["phase"] == "keys":
            if state["i"] < len(steps):
                step = steps[state["i"]]
                # Focus the step's target, then send its key on the next tick
                # (the focus is applied in the web process first).
                if step.get("focused") is None:
                    js(f"window.__focus({json.dumps(step['focus'])})",
                       lambda r, _e, s=step: s.__setitem__("focused", r or ""))
                elif step["focused"] != "sent":
                    step["focused"] = "sent"
                    send_step()
            else:
                state["phase"] = "reporting"
                js("setTimeout(() => window.__keysDone(), 150)")
        AppHelper.callLater(0.08, tick)

    AppHelper.callLater(0.1, tick)
    AppHelper.runEventLoop(installInterrupt=True)
    return 3


if __name__ == "__main__":  # pragma: no cover - child entry point
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--done", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--steps", required=True)
    ap.add_argument("--tab-to-all", action="store_true")
    ns = ap.parse_args()
    sys.exit(main(ns.url, ns.done, ns.timeout, json.loads(ns.steps), tab_to_all=ns.tab_to_all))
