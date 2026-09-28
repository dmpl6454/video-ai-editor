"""Wave E, F2: the Inspector's Canvas and Blend controls through the UI, in
Chromium AND WebKit, proven on the EDL and on the preview's pixels.

* Canvas: a letterboxed main-track clip is selected, Blur is picked, the
  strength is stepped with the ARROW KEYS and Space, "Apply to all" copies it
  to every main-track clip (one undo step), a swatch sets a colour, a picture
  is chosen through the file picker (uploaded to uploads/images), and Reset
  puts the black bars back — every step read back from the session's EDL.
  The server preview then shows the colour in its letterbox (the render
  path, decoded by the browser).
* Blend: an overlay clip's Blend menu sets Multiply and Screen; the live
  preview composites the overlay's layer with that `mix-blend-mode` over the
  video (lib/pipBlendLayers): a mid-grey overlay over a light-grey main
  video reads darker than Normal under Multiply and lighter under Screen.
  In Chromium, Linear Burn says it cannot preview live.

Harness: `test_frontend_a11y`'s server fixture — VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend. Screenshots go to VAE_F2_SHOTS.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import _client, _edl, _open, _wait_edl, engine, pw  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SHOTS = Path(os.environ.get("VAE_F2_SHOTS", "/tmp"))


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("f2_ui_media")
    main = d / "main_grey.mp4"
    # a light-grey landscape clip with a small moving box (so frames differ)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0xC0C0C0:s=640x360:r=30:d=4",
                    "-f", "lavfi", "-i", "sine=f=330:sample_rate=48000:duration=4",
                    "-vf", "drawbox=x='mod(t*120\\,600)':y=20:w=32:h=32:color=0x404040:t=fill,format=yuv420p",
                    "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(main)],
                   check=True, capture_output=True)
    over = d / "over_grey.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x808080:s=320x320:r=30:d=4",
                    "-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", str(over)],
                   check=True, capture_output=True)
    from PIL import Image
    pic = d / "sunset.png"
    Image.new("RGB", (400, 300), (230, 120, 40)).save(pic)
    return {"main": main, "over": over, "pic": pic}


def _dispatch(c, sid: str, tool: str, args: dict) -> dict:
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _track(edl: dict, tid: str) -> list[dict]:
    return next(t for t in edl["tracks"] if t["id"] == tid)["clips"]


def _project(base_url, media, name: str) -> tuple[str, list[str], str]:  # noqa: F811
    """9:16 canvas; v1: the landscape clip split in two (letterboxed); v2: the
    grey overlay centred on the picture."""
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        srcs = []
        for path, add in ((media["main"], "true"), (media["over"], "false")):
            with path.open("rb") as fh:
                r = c.post(f"/api/sessions/{sid}/upload", files={"file": (path.name, fh, "video/mp4")},
                           data={"add_to_timeline": add, "transcribe": "false"})
            assert r.status_code in (200, 202), r.text
            srcs.append(r.json()["src"])
        deadline = time.time() + 120
        while not _track(c.get(f"/api/sessions/{sid}/edl").json(), "v1"):
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)
        _dispatch(c, sid, "set_canvas", {"w": 720, "h": 1280})
        _dispatch(c, sid, "split_at", {"track": "v1", "time": 2.0})
        cid = _dispatch(c, sid, "add_clip", {"track": "v2", "src": srcs[1], "in": 0.0, "out": 3.5,
                                             "start": 0.0})["result"]["clip_id"]
        _dispatch(c, sid, "set_clip_transform", {"clip_id": cid, "x": 360, "y": 640, "scale": 0.8})
        edl = c.get(f"/api/sessions/{sid}/edl").json()
    return sid, [x["id"] for x in _track(edl, "v1")], cid


def _select(page, clip_id: str) -> None:
    page.evaluate("""async (id) => {
      const m = window.__vaeTest ?? await import('/src/store.ts')
      m.useStore.getState().setSelection(id)
    }""", clip_id)
    page.locator(f".props[data-clip-id='{clip_id}']").wait_for(timeout=10000)


def _seek(page, t: float) -> None:
    page.evaluate("""async (t) => {
      const m = window.__vaeTest ?? await import('/src/store.ts')
      m.useStore.getState().setPlayhead(t)
    }""", t)


def _bg(edl: dict, cid: str):
    return next(c for c in _track(edl, "v1") if c["id"] == cid).get("canvas_bg")


def _pixel(page, sel: str, fx: float, fy: float) -> tuple[int, int, int]:
    """RGB of the screenshot of `sel` at fraction (fx, fy) of its box."""
    from PIL import Image
    shot = Image.open(io.BytesIO(page.locator(sel).screenshot())).convert("RGB")
    return shot.getpixel((int(shot.width * fx), int(shot.height * fy)))


def test_canvas_section_sets_blur_colour_image_and_apply_to_all(engine, base_url, media):  # noqa: F811
    sid, v1, _pip = _project(base_url, media, f"f2-canvas-{engine.engine_name}")
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        _select(page, v1[0])
        sec = page.locator(".canvas-section")
        sec.wait_for()
        group = page.get_by_role("radiogroup", name="Canvas background")
        assert group.get_by_role("radio").count() == 4
        group.get_by_role("radio", name="Blur").click()
        _wait_edl(base_url, sid, lambda e: (_bg(e, v1[0]) or {}).get("type") == "blur", "blur set")
        assert _bg(_edl(base_url, sid), v1[0])["blur"] == 2
        assert _bg(_edl(base_url, sid), v1[1]) is None
        # keyboard: the strength radiogroup moves with the arrows, Space picks
        strength = page.get_by_role("radiogroup", name="Blur strength")
        strength.get_by_role("radio", name="Medium").focus()
        page.keyboard.press("ArrowRight")
        assert page.evaluate("document.activeElement.textContent").strip() == "Strong"
        page.keyboard.press("Space")
        _wait_edl(base_url, sid, lambda e: (_bg(e, v1[0]) or {}).get("blur") == 3, "blur 3 from the keyboard")
        page.get_by_role("button", name="Apply this canvas background to all main-track clips").click()
        _wait_edl(base_url, sid, lambda e: all((_bg(e, c) or {}).get("blur") == 3 for c in v1), "apply to all")
        page.screenshot(path=str(SHOTS / f"f2_canvas_blur_{engine.engine_name}.png"))
        # one undo step undoes Apply to all (the app's own Undo)
        page.evaluate("""async () => {
          const m = window.__vaeTest ?? await import('/src/store.ts')
          await m.useStore.getState().dispatch('undo', {})
        }""")
        _wait_edl(base_url, sid, lambda e: _bg(e, v1[1]) is None and _bg(e, v1[0])["blur"] == 3, "undo")
        # colour: the kind, then a swatch
        _select(page, v1[1])
        page.get_by_role("radiogroup", name="Canvas background").get_by_role("radio", name="Colour").click()
        _wait_edl(base_url, sid, lambda e: (_bg(e, v1[1]) or {}).get("type") == "color", "colour kind")
        page.get_by_role("radio", name="Colour #FFFFFF").click()
        _wait_edl(base_url, sid, lambda e: (_bg(e, v1[1]) or {}).get("color") == "#FFFFFF", "white swatch")
        # the server preview letterbox shows it (the render, decoded by the browser)
        _seek(page, 3.0)
        deadline = time.time() + 60
        while True:
            px = _pixel(page, ".preview-pane video[src*='preview.mp4']", 0.5, 0.06)
            if min(px) >= 235:
                break
            assert time.time() < deadline, f"the preview's letterbox never turned white: {px}"
            page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / f"f2_canvas_white_{engine.engine_name}.png"))
        # a picture through the file picker (uploaded to uploads/images)
        page.get_by_role("radiogroup", name="Canvas background").get_by_role("radio", name="Image").click()
        page.locator(".canvas-section input[type=file]").set_input_files(str(media["pic"]))
        e = _wait_edl(base_url, sid, lambda e: (_bg(e, v1[1]) or {}).get("type") == "image", "picture")
        assert "/uploads/images/" in _bg(e, v1[1])["image"].replace("\\", "/")
        assert page.locator(".canvas-image-name").inner_text().startswith("sunset")
        # Reset: black bars
        page.locator(".props button[title='Reset canvas to default']").click()
        _wait_edl(base_url, sid, lambda e: _bg(e, v1[1]) is None, "reset")
    finally:
        page.context.close()


def test_blend_menu_sets_the_mode_and_the_preview_blends_live(engine, base_url, media):  # noqa: F811
    sid, _v1, pip = _project(base_url, media, f"f2-blend-{engine.engine_name}")
    page = _open(engine, base_url, sid, 1400, 900)
    try:
        _seek(page, 1.0)
        _select(page, pip)
        menu = page.get_by_role("combobox", name="Blend mode")
        menu.wait_for()
        assert menu.locator("option").count() == 14

        def centre() -> tuple[int, int, int]:
            # the PIP's centre (the canvas centre) once the picture settles
            last, same = None, 0
            deadline = time.time() + 30
            while same < 3:
                px = _pixel(page, ".preview-pane", 0.5, 0.5)
                same = same + 1 if last is not None and max(abs(a - b) for a, b in zip(px, last)) <= 2 else 0
                last = px
                assert time.time() < deadline, "the preview never settled"
                page.wait_for_timeout(300)
            return last

        normal = centre()
        # review RE: this accepted 110-150, which let WebKit's colour-managed
        # drawImage (the grey read 139) through; PiP frames are a colour-exact
        # WebGL copy there now (lib/videoColour.ts), so the grey is the grey
        assert abs(normal[0] - 128) <= 4, f"Normal should show the grey overlay itself: {normal}"
        menu.select_option("multiply")
        _wait_edl(base_url, sid, lambda e: _track(e, "v2")[0].get("blend") == "multiply", "multiply")
        layers = page.locator("[data-layer='pip-blend'] canvas")
        layers.first.wait_for(state="attached")
        assert page.evaluate(
            "[...document.querySelectorAll(\"[data-layer='pip-blend'] canvas\")]"
            ".some(c => c.style.display === 'block' && c.style.mixBlendMode === 'multiply')")
        mul = centre()
        menu.select_option("screen")
        _wait_edl(base_url, sid, lambda e: _track(e, "v2")[0].get("blend") == "screen", "screen")
        scr = centre()
        page.screenshot(path=str(SHOTS / f"f2_blend_screen_{engine.engine_name}.png"))
        # grey 0x80 over light grey 0xC0: multiply ≈ 96, screen ≈ 223 (W3C)
        assert mul[0] < normal[0] - 15, (normal, mul)
        assert scr[0] > normal[0] + 40, (normal, scr)
        assert abs(mul[0] - 96) <= 4 and abs(scr[0] - 223) <= 4, (mul, scr)
        menu.select_option("linear_burn")
        _wait_edl(base_url, sid, lambda e: _track(e, "v2")[0].get("blend") == "linear_burn", "linear burn")
        note = page.locator("[data-blend-note]")
        if engine.engine_name == "chromium":
            assert "cannot preview Linear Burn" in note.inner_text()
        else:
            assert note.count() == 0
        # back to Normal: the layers stand down
        menu.select_option("normal")
        _wait_edl(base_url, sid, lambda e: "blend" not in _track(e, "v2")[0], "normal")
        page.wait_for_timeout(300)
        assert not page.evaluate(
            "[...document.querySelectorAll(\"[data-layer='pip-blend'] canvas\")].some(c => c.style.display === 'block')")
    finally:
        page.context.close()
