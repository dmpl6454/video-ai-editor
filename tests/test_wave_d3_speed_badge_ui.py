"""Wave D3 (lane E3): the CapCut speed badge on timeline clips, in a real
browser — Chromium AND Playwright WebKit (the stand-in for the app's
WKWebView).

A 2x clip reads "2.0x", a Hero curve "Hero", a 0.5x clip "0.5x" and a freeze
frame "Freeze"; a clip at normal speed has none. At every zoom the badge is
readable (icon + text → text → icon, never a clipped word), it never
overlaps the clip's name (the name ellipsizes short of it) and the waveform
is not drawn under it (the badge's pixels are the badge's own plate). The
Timeline publishes what it drew as `data-speed-badges` on its canvas; the
pixels are read back from the canvas itself.

Harness: test_frontend_a11y's server (VAE_A11Y_BASE_URL for a running Vite
dev server + backend started with no API key), test_wave_d_rail_ui's engines;
screenshots land in VAE_RAIL_SHOTS.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from test_frontend_a11y import base_url  # noqa: F401  (fixture)
from test_wave_d_rail_ui import SHOTS, _open, engine, pw  # noqa: F401


def _clip(dst: Path, seconds: int = 12) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
        check=True, capture_output=True)
    return dst


def _dispatch(c: httpx.Client, sid: str, tool: str, args: dict) -> dict:
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _v1(c: httpx.Client, sid: str) -> list[dict]:
    edl = c.get(f"/api/sessions/{sid}/edl").json()
    return sorted(next(t for t in edl["tracks"] if t["id"] == "v1")["clips"], key=lambda x: x["start"])


def _retimed_session(base_url, tmp_path, name: str) -> tuple[str, dict[str, str]]:  # noqa: F811
    """Five v1 clips: 2x · Hero · normal · 0.5x · a freeze frame."""
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        clip = _clip(tmp_path / f"{name.replace(' ', '_')}.mp4")
        with clip.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (clip.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        for t in (3.0, 6.0, 9.0):
            _dispatch(c, sid, "split_at", {"track": "v1", "time": t})
        a, b, n, d = (x["id"] for x in _v1(c, sid))
        _dispatch(c, sid, "set_speed", {"clip_id": a, "factor": 2.0})
        _dispatch(c, sid, "set_speed", {"clip_id": b, "preset": "hero"})
        _dispatch(c, sid, "set_speed", {"clip_id": d, "factor": 0.5})
        end = max(x["start"] for x in _v1(c, sid) if x["id"] == d)
        _dispatch(c, sid, "freeze_frame", {"time": round(end + 1.0, 3)})
        clips = _v1(c, sid)
        frz = next(x["id"] for x in clips if x.get("freeze"))
    return sid, {"2x": a, "hero": b, "normal": n, "half": d, "freeze": frz}


def _badges(page) -> list[dict]:
    raw = page.locator('canvas[aria-label="Timeline"]').get_attribute("data-speed-badges")
    return json.loads(raw or "[]")


def _plate_pixels(page, b: dict) -> dict:
    """Pixel stats of the badge's rect on the canvas: the share of the plate
    colour (a dark plate) and of bright text pixels."""
    return page.evaluate("""(b) => {
        const cv = document.querySelector('canvas[aria-label="Timeline"]')
        const r = cv.width / cv.getBoundingClientRect().width
        const d = cv.getContext('2d').getImageData(Math.round((b.x + 1) * r), Math.round((b.y + 1) * r),
                                                   Math.max(1, Math.round((b.w - 2) * r)), Math.max(1, Math.round((b.h - 2) * r))).data
        let dark = 0, bright = 0, n = 0
        for (let i = 0; i < d.length; i += 4) {
            const l = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]
            if (l < 60) dark++
            if (l > 150) bright++
            n++
        }
        return { dark: dark / n, bright: bright / n }
    }""", b)


def _zoom_to(page, px_per_s: float) -> None:
    page.evaluate("""async (z) => {
        const m = await (window.__vaeTest ?? import('/src/store.ts'))
        m.useStore.getState().setTimelineZoom
          ? m.useStore.getState().setTimelineZoom(z)
          : m.useStore.setState({ timelineZoom: z })
    }""", px_per_s)
    page.wait_for_timeout(400)


def test_retimed_clips_carry_a_readable_speed_badge_at_every_zoom(engine, base_url, tmp_path):  # noqa: F811
    sid, ids = _retimed_session(base_url, tmp_path, f"d3 badge {engine.engine_name}")
    page = _open(engine, base_url, sid, 1440, 900)
    want = {ids["2x"]: "2.0x", ids["hero"]: "Hero", ids["half"]: "0.5x", ids["freeze"]: "Freeze"}
    seen_tiers: set[str] = set()
    for zoom in (160, 80, 30, 12, 5):
        _zoom_to(page, zoom)
        badges = {b["id"]: b for b in _badges(page)}
        assert ids["normal"] not in badges, "a clip at normal speed has no badge"
        shot = SHOTS / f"d3_speed_badge_{engine.engine_name}_z{zoom}.png"
        page.locator(".timeline-canvas-wrap").screenshot(path=str(shot))
        for cid, text in want.items():
            b = badges.get(cid)
            if b is None:
                seen_tiers.add("none")
                continue
            tier = "full" if b["icon"] and b["text"] else ("text" if b["text"] else "icon")
            seen_tiers.add(tier)
            if b["text"]:
                assert b["text"] == text, (zoom, b)
            # never over the clip's name
            if b["nameRight"] >= 0:
                assert b["nameRight"] <= b["x"] - 4 + 0.5, (zoom, b)
            # the waveform is not drawn under it: the rect is the dark plate,
            # with the text (or the icon) bright on it
            px = _plate_pixels(page, b)
            assert px["dark"] >= 0.45, (zoom, b, px)
            assert px["bright"] >= 0.02, (zoom, b, px)
    # the zoom sweep exercised the full badge and at least one degraded tier
    assert "full" in seen_tiers and seen_tiers & {"text", "icon", "none"}, seen_tiers
    page.context.close()


def test_the_badge_follows_a_prompt_speed_change(engine, base_url, tmp_path):  # noqa: F811
    """The key-free Prompt bar sets a speed; the badge appears on THAT clip only."""
    with httpx.Client(base_url=base_url, timeout=120) as c:
        sid = c.post("/api/sessions", json={"name": f"d3 badge prompt {engine.engine_name}"}).json()["id"]
        clip = _clip(tmp_path / "prompt.mp4", 8)
        with clip.open("rb") as fh:
            c.post(f"/api/sessions/{sid}/upload", files={"file": (clip.name, fh, "video/mp4")},
                   data={"add_to_timeline": "true", "transcribe": "false"})
        _dispatch(c, sid, "split_at", {"track": "v1", "time": 4.0})
        first, second = (x["id"] for x in _v1(c, sid))
    page = _open(engine, base_url, sid, 1440, 900)
    _zoom_to(page, 80)
    assert _badges(page) == []
    box = page.get_by_role("textbox", name="What should happen to this video?")
    box.fill("speed up the second clip 2x")
    box.press("Enter")
    page.wait_for_function(
        """() => (JSON.parse(document.querySelector('canvas[aria-label="Timeline"]').dataset.speedBadges || '[]')).length > 0""",
        timeout=30000)
    badges = _badges(page)
    assert [(b["id"], b["text"]) for b in badges] == [(second, "2.0x")], badges
    page.locator(".timeline-canvas-wrap").screenshot(path=str(SHOTS / f"d3_speed_badge_prompt_{engine.engine_name}.png"))
    page.context.close()
