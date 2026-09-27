"""Trim a speed-CURVE clip to the playhead from the keyboard (W, CapCut) and
split it (⌘B), in Chromium AND WebKit, proven by the EXPORT (wave D3, E1b).

`trim_clip` takes SOURCE seconds; on a curve clip the source time under the
playhead is the curve's INTEGRAL (`lib/trimToPlayhead.ts` →
`framePlan.sourceOffsetAt`), not the timeline offset times the mean speed —
that landed the new edge up to ~0.6 s of footage away from the playhead
(Hero: 1.0 s into the clip is 1.0 s of source, the mean said 0.79 s). The
server keeps exactly the frames the clip showed (`dispatch._trim_curve`,
the in-anchored chain), so the exported file, decoded frame by frame, must
be the untouched timeline's frames with the trimmed tail removed, and a
split must change nothing at all.

Harness: `test_frontend_a11y`'s server fixture (VAE_A11Y_BASE_URL = a Vite
dev server proxying /api to a backend). Screenshots to VAE_SPEED_SHOTS.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402
from test_frontend_a11y import base_url  # noqa: E402,F401  (fixture)
from test_speed_ui_e2e import (  # noqa: E402,F401  (fixtures + helpers)
    SHOTS, TimelineGeometry, _client, _edl, _expected, _open, _project, _timecode, _v1,
    _wait_edl, bars, engine, expect, pw,
)

from video_ai_editor.edl import timebase as tb  # noqa: E402
from video_ai_editor.edl.schema import Clip  # noqa: E402


def _export(page, tmp_path: Path, name: str) -> dict:
    page.locator(".topbar-pinned button.primary").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for()
    with page.expect_download(timeout=600_000) as dl:
        dialog.get_by_role("button", name="Export", exact=True).click()
    out = tmp_path / f"{name}.mp4"
    dl.value.save_as(str(out))
    page.keyboard.press("Escape")
    return G.measure(out)


def _wait_playhead(page, t: float) -> None:
    """The clock's Enter lands the playhead asynchronously (the seek settles
    through the preview engine); a fixed sleep let ⌘B run while the playhead
    was still a frame or two off in WebKit (it then split nothing)."""
    page.wait_for_function(
        "async (t) => { const s = (await (window.__vaeTest ?? import('/src/store.ts'))).useStore.getState();"
        " return !s.isPlaying && Math.abs(s.playhead - t) < 0.5 / 30 }", arg=t, timeout=10_000)


def test_w_trims_a_hero_clip_to_the_frame_under_the_playhead_and_a_split_changes_nothing(
        engine, base_url, bars, tmp_path):  # noqa: F811
    name = engine.engine_name
    sid = _project(base_url, bars, f"curve trim {name}")
    clips = [c["id"] for c in _v1(_edl(base_url, sid))]
    with _client(base_url) as c:
        r = c.post(f"/api/sessions/{sid}/dispatch",
                   json={"tool": "set_speed", "args": {"clip_id": clips[2], "preset": "hero"}})
        assert r.status_code == 200, r.text
    before = _edl(base_url, sid)
    hero = next(c for c in _v1(before) if c["id"] == clips[2])
    exp_before = _expected(before)["top"]

    page = _open(engine, base_url, sid, 1280, 800)
    geo = TimelineGeometry(page)
    # 1. Select the Hero clip, park the playhead 1.0 s into it (its slow
    #    ramp starts at 0.76 s), press W.
    geo.select(base_url, sid, hero["id"])
    t = tb.quantize(hero["start"] + 1.0, 30)
    clock = page.get_by_role("textbox", name="Playhead timecode")
    clock.click()
    clock.fill(_timecode(t))
    clock.press("Enter")
    _wait_playhead(page, t)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.keyboard.press("KeyW")
    after = _wait_edl(base_url, sid, lambda e: next(c for c in _v1(e) if c["id"] == hero["id"])["out"]
                      != hero["out"], "the W trim")
    trimmed = next(c for c in _v1(after) if c["id"] == hero["id"])
    whole = Clip.model_validate(hero)
    local = t - hero["start"]
    assert trimmed["out"] == pytest.approx(hero["in"] + whole.source_offset_at(local), abs=1e-9)
    assert trimmed["speed"]["name"] == "custom"
    # The Inspector follows: the kept piece is a custom curve ending at the
    # playhead.
    props = page.locator(".props")
    expect(props.get_by_role("radio", name="Custom", exact=True)).to_have_attribute("aria-checked", "true")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / f"curve_trim_{name}_1280x800.png"))

    # 2. Split the trimmed clip with ⌘B at a frame inside its slow ramp.
    geo.select(base_url, sid, hero["id"])
    t2 = tb.quantize(hero["start"] + 0.9, 30)
    clock.click()
    clock.fill(_timecode(t2))
    clock.press("Enter")
    _wait_playhead(page, t2)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.keyboard.press("Meta+KeyB")
    split = _wait_edl(base_url, sid, lambda e: len(_v1(e)) == len(_v1(after)) + 1, "the split")

    # 3. Export, decode: the untouched timeline minus the trimmed tail.
    k0 = tb.frame_of(hero["start"], 30)
    k_cut = tb.frame_of(t, 30)
    n_hero = _frames_of(before, hero["id"])
    want = exp_before[:k_cut] + exp_before[k0 + n_hero:]
    measured = _export(page, tmp_path, f"curve_trim_{name}")["top"]
    assert measured == want
    assert measured == _expected(split)["top"]
    page.context.close()


def _frames_of(edl: dict, clip_id: str) -> int:
    from video_ai_editor.render.compositor import clip_frames
    return clip_frames(Clip.model_validate(next(c for c in _v1(edl) if c["id"] == clip_id)), 30)
