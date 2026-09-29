"""Inspector > Timing for a music bed the programme end cuts, in Chromium AND
WebKit (final QA run 3, P3).

A bed laid to v1's layout end over three 0.5 s dissolves is cut where that
end plays (`schema.sound_render_windows`): the export and the Timeline block
stop at 18.5 s. The Inspector read End 00:00:20:00 and Duration 20:00 — a
second and a half the export never plays. End (and so Duration) must show
where it actually plays to.

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend, else frontend/dist).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import EDL, sound_render_windows  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
FPS = 30


@pytest.fixture(scope="module")
def pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine(request, pw):
    try:
        b = getattr(pw, request.param).launch()
    except Exception as e:  # noqa: BLE001 — a missing engine is a skip, not a failure
        pytest.skip(f"no Playwright {request.param}: {e}")
    yield b
    b.close()


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("bed")
    video, bed = d / "picture20.mp4", d / "bed30.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=s=320x180:d=20:r={FPS}",
                    "-f", "lavfi", "-i", "sine=f=330:d=20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=f=220:d=30", "-c:a", "aac", str(bed)],
                   check=True, capture_output=True)
    return video, bed


def _client(base_url):  # noqa: F811
    import httpx
    return httpx.Client(base_url=base_url, timeout=180)


def _dispatch(c, sid: str, tool: str, args: dict) -> dict:
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, (tool, r.text)
    return r.json()


def _project(base_url, media, name: str) -> tuple[str, dict]:  # noqa: F811
    """Four 5 s shots with a 0.5 s dissolve on each cut, and a music bed laid
    from 0 to v1's layout end (20 s)."""
    video, bed = media
    with _client(base_url) as c:
        sid = c.post("/api/sessions", json={"name": name}).json()["id"]
        with video.open("rb") as fh:
            r = c.post(f"/api/sessions/{sid}/upload", files={"file": (video.name, fh, "video/mp4")},
                       data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code in (200, 202), r.text
        deadline = time.time() + 120
        while not next(t for t in c.get(f"/api/sessions/{sid}/edl").json()["tracks"]
                       if t["id"] == "v1")["clips"]:
            assert time.time() < deadline, "the import never reached the timeline"
            time.sleep(0.3)
        for t in (5.0, 10.0, 15.0):
            _dispatch(c, sid, "split_at", {"time": t})
        for t in (5.0, 10.0, 15.0):
            _dispatch(c, sid, "add_transition", {"at": t, "type": "dissolve", "duration": 0.5})
        _dispatch(c, sid, "add_clip", {"track": "music", "src": str(bed), "in": 0, "out": 20, "start": 0})
        edl = c.get(f"/api/sessions/{sid}/edl").json()
    return sid, edl


def _timecode(t: float) -> str:
    f = tb.frame_of(t, FPS)
    return f"{f // (3600 * FPS):02d}:{f // (60 * FPS) % 60:02d}:{f // FPS % 60:02d}:{f % FPS:02d}"


def test_inspector_end_of_a_bed_cut_by_the_programme_end(engine, base_url, media):  # noqa: F811
    sid, edl_json = _project(base_url, media, f"bed end {engine.browser_type.name}")
    edl = EDL.model_validate(edl_json)
    music = next(t for t in edl.tracks if t.type == "music")
    bed = music.clips[0]
    assert (bed.start, bed.out) == (0.0, 20.0)
    assert len(edl.get_track("v1").transitions) == 3
    # The export's truth: where the bed actually plays.
    rs, re_ = sound_render_windows(music.clips, edl.v1_seam_table(), edl.video_extent())[bed.id]
    assert rs == pytest.approx(0.0) and re_ == pytest.approx(18.5, abs=1 / FPS)

    ctx = engine.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script("try { " + " ".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
        for k, v in {"vai.sessionId": sid, "vai.rightTab": "inspect"}.items()) + " } catch (e) {}")
    page = ctx.new_page()
    try:
        page.goto(base_url + "/?vae-test")
        page.locator(".timeline-canvas-wrap canvas").first.wait_for()
        page.wait_for_function("() => !!window.__vaeTest?.useStore?.getState().edl")
        page.evaluate(f"() => window.__vaeTest.useStore.getState().setSelection({json.dumps(bed.id)})")
        props = page.locator(f'.props[data-clip-id="{bed.id}"]')
        props.wait_for()
        end = props.get_by_label("End", exact=True)
        dur = props.get_by_label("Duration", exact=True)
        start = props.get_by_label("Start", exact=True)
        page.wait_for_function(
            "(el) => el && el.value", arg=end.element_handle())
        assert start.input_value() == _timecode(rs)
        assert end.input_value() == _timecode(re_), "End must be where the bed stops playing"
        assert dur.input_value() == _timecode(re_ - rs)
        assert end.input_value() != _timecode(20.0)
    finally:
        ctx.close()
