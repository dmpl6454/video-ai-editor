"""Wave D milestone 2, review RD2 fixes, measured in a real browser — Chromium
AND Playwright WebKit (the automated stand-in for the app's WKWebView).

* The key-free Prompt bar (Recipes + Apple Intelligence, no API key): "add a
  hero speed ramp" plays the Hero CURVE (it committed a constant 1.25x), a
  freeze frame is a freeze (it was read as a title), "split at 1 second"
  splits (it was not understood).
* A clarify question left over from before a reload no longer swallows a NEW
  sentence: Enter drops it and runs the new text.
* The Inspector's Normal speed slider starts a curve clip at the curve's mean
  speed (it read 1.00x).
* A disabled icon-only toolbar button (empty project) is clearly dimmer than
  an enabled one.

Harness: test_frontend_a11y's server (VAE_A11Y_BASE_URL for a running Vite
dev server + backend started with no API key), test_wave_d_rail_ui's engines.
"""
from __future__ import annotations

import time

import httpx
import pytest

from test_frontend_a11y import _clip, base_url  # noqa: F401  (fixture)
from test_wave_d_rail_ui import SHOTS, _open, engine, pw  # noqa: F401


def _session(base_url, tmp_path, name: str, *, media: bool = True) -> str:  # noqa: F811
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        if media:
            clip = _clip(tmp_path / f"{name.replace(' ', '_')}.mp4")
            with clip.open("rb") as fh:
                r = c.post(f"/api/sessions/{sid}/upload", files={"file": (clip.name, fh, "video/mp4")},
                           data={"add_to_timeline": "true", "transcribe": "false"})
            assert r.status_code in (200, 202), r.text
    return sid


def _v1(base_url, sid) -> list[dict]:  # noqa: F811
    edl = httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=10).json()
    return next(t for t in edl["tracks"] if t["id"] == "v1")["clips"]


def _until(fn, ok, timeout=30.0):
    deadline = time.monotonic() + timeout
    got = fn()
    while not ok(got) and time.monotonic() < deadline:
        time.sleep(0.2)
        got = fn()
    return got


def _prompt(page, text: str) -> None:
    box = page.get_by_role("textbox", name="What should happen to this video?")
    box.fill(text)
    box.press("Enter")


def test_key_free_prompt_bar_does_curves_freezes_and_splits(engine, base_url, tmp_path):  # noqa: F811
    sid = _session(base_url, tmp_path, f"rd2 prompt {engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    _prompt(page, "add a hero speed ramp")
    clips = _until(lambda: _v1(base_url, sid), lambda cs: isinstance(cs[0].get("speed"), dict))
    assert isinstance(clips[0]["speed"], dict) and clips[0]["speed"].get("name") == "hero", clips[0].get("speed")
    page.screenshot(path=str(SHOTS / f"rd2_prompt_hero_{engine.engine_name}.png"))

    n = len(_v1(base_url, sid))
    _prompt(page, "split at 1 second")
    clips = _until(lambda: _v1(base_url, sid), lambda cs: len(cs) == n + 1)
    assert len(clips) == n + 1, clips

    _prompt(page, "freeze frame at 0.5 seconds")
    clips = _until(lambda: _v1(base_url, sid), lambda cs: any(c.get("freeze") for c in cs))
    assert any(c.get("freeze") for c in clips), clips
    assert not any(t["type"] == "text" and t["clips"]
                   for t in httpx.get(f"{base_url}/api/sessions/{sid}/edl", timeout=10).json()["tracks"])
    page.screenshot(path=str(SHOTS / f"rd2_prompt_freeze_{engine.engine_name}.png"))
    page.context.close()


def test_a_stale_clarify_question_does_not_swallow_a_new_sentence(engine, base_url, tmp_path):  # noqa: F811
    sid = _session(base_url, tmp_path, f"rd2 clarify {engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    # "a speed ramp" with no name asks which curve
    _prompt(page, "add a speed ramp")
    page.locator(".clarify").first.wait_for(timeout=30000)
    page.reload()
    page.locator(".clarify").first.wait_for(timeout=30000)      # the question survives the reload
    n = len(_v1(base_url, sid))
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/prompt" in r.url else None)
    _prompt(page, "split at 1 second")
    clips = _until(lambda: _v1(base_url, sid), lambda cs: len(cs) == n + 1)
    assert len(clips) == n + 1, "Enter with a new sentence over a stale question ran nothing"
    assert posts, "no POST /prompt was sent"
    page.screenshot(path=str(SHOTS / f"rd2_stale_clarify_{engine.engine_name}.png"))
    page.context.close()


def test_normal_speed_slider_starts_a_curve_clip_at_its_mean(engine, base_url, tmp_path):  # noqa: F811
    sid = _session(base_url, tmp_path, f"rd2 slider {engine.engine_name}")
    cid = _v1(base_url, sid)[0]["id"]
    curve = [[0, 1], [0.759, 1], [1, 0.406]]
    r = httpx.post(f"{base_url}/api/sessions/{sid}/dispatch",
                   json={"tool": "set_speed", "args": {"clip_id": cid, "curve": curve}}, timeout=30)
    assert r.status_code == 200, r.text
    page = _open(engine, base_url, sid, 1440, 900)
    page.get_by_role("application", name="Timeline").focus()
    page.keyboard.press("ControlOrMeta+a")
    page.get_by_role("radio", name="Normal").click()
    slider = page.get_by_role("slider", name="Speed")
    slider.wait_for(timeout=5000)
    # mean of the curve: 1·0.759 + (1 + 0.406)/2·0.241 = 0.9284
    shown = float(slider.get_attribute("aria-valuetext").rstrip("×"))
    assert shown == pytest.approx(0.93, abs=0.006), shown
    page.screenshot(path=str(SHOTS / f"rd2_normal_over_curve_{engine.engine_name}.png"))
    page.context.close()


def test_disabled_toolbar_icons_look_disabled(engine, base_url, tmp_path):  # noqa: F811
    sid = _session(base_url, tmp_path, f"rd2 empty {engine.engine_name}", media=False)
    page = _open(engine, base_url, sid, 1440, 900)
    got = page.evaluate("""() => {
      const lum = (c) => { const m = c.match(/\\d+(\\.\\d+)?/g).map(Number).slice(0, 3).map(v => v / 255)
        .map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4); return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2] }
      const icons = [...document.querySelectorAll('.timeline-toolbar .tb-icon')].filter(b => b.getBoundingClientRect().width)
      return icons.map(b => ({ name: b.getAttribute('aria-label'), disabled: b.disabled, lum: lum(getComputedStyle(b).color) }))
    }""")
    on = [g for g in got if not g["disabled"]]
    off = [g for g in got if g["disabled"]]
    assert on and off, got
    assert {"Split at playhead", "Freeze frame"} <= {g["name"] for g in off} or len(off) >= 3, off
    ratio = (min(g["lum"] for g in on) + 0.05) / (max(g["lum"] for g in off) + 0.05)
    assert ratio >= 2.0, (ratio, got)
    page.locator(".timeline-toolbar").screenshot(path=str(SHOTS / f"rd2_empty_toolbar_{engine.engine_name}.png"))
    page.context.close()
