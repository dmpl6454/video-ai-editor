"""Text animation presets (anim_in/anim_out) + per-clip TextStyle rendering.

anim_in/anim_out were write-only schema fields for months: add_text documented
"pop, fade, slide_up, slide_down" and the shipped countdown_3_2_1 template set
them, but no renderer read them. TextClip.style (color/font) was likewise
accepted, persisted, and ignored. These tests pin:

  - tool-boundary validation (bad names rejected loudly),
  - the emitted filter graph (pop scale exprs, fades, itsoffset input timing),
  - track-z compositing order,
  - and — through a real export render — that the animation and the style
    color actually reach pixels, for a clip that does NOT start at t=0 (the
    old looped-input timing only lined up for clips at the timeline head).
"""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Clip, TextClip, TextStyle
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render.text_overlay import (
    build_overlay_chain, cache_text_pngs, resolve_style_overrides,
)


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=0x202020:s=320x240:d=4:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
        check=True)
    s = EDLStore(tmp_path / "sess")
    s.edl.get_track("v1").clips.append(Clip(src=str(src), in_=0, out=4, start=0))
    s.edl.recompute_duration()
    return s


# ---------- tool boundary ----------

def test_add_text_rejects_unknown_anim(store: EDLStore):
    with pytest.raises(ValueError, match="anim_in"):
        dispatch(store, "add_text",
                 {"text": "X", "start": 0, "end": 1, "anim_in": "explode"})


def test_add_text_accepts_all_documented_presets(store: EDLStore):
    for name in ("pop", "fade", "slide_up", "slide_down"):
        dispatch(store, "add_text",
                 {"text": name, "start": 0, "end": 1, "anim_in": name, "anim_out": name,
                  "allow_stack": True})


def test_add_text_normalizes_anim_case_at_storage(store: EDLStore):
    """Regression pin: anim names must be stored LOWERCASE, not just validated
    case-insensitively. The server renderer re-lowercases defensively, but the
    frontend preview (TextLayer.tsx) does a strict `=== 'pop'` compare — a
    stored 'Pop' would validate and render correctly server-side while
    silently not animating in the browser preview."""
    r = dispatch(store, "add_text", {"text": "X", "start": 0, "end": 1,
                                     "anim_in": "Pop", "anim_out": "FADE"})
    tc = next(c for t in store.edl.tracks for c in t.clips if c.id == r["id"])
    assert tc.anim_in == "pop"
    assert tc.anim_out == "fade"


def test_add_text_rejects_invalid_color(store: EDLStore):
    with pytest.raises(ValueError, match="hex"):
        dispatch(store, "add_text", {"text": "X", "start": 0, "end": 1, "color": "red"})
    with pytest.raises(ValueError, match="hex"):
        dispatch(store, "add_text", {"text": "X", "start": 0, "end": 1, "color": "#12345"})


def test_add_text_accepts_valid_hex_colors(store: EDLStore):
    for color in ("#FF2020", "#ff2020", "#FF2020AA"):
        r = dispatch(store, "add_text", {"text": color, "start": 0, "end": 1,
                                         "color": color, "allow_stack": True})
        tc = next(c for t in store.edl.tracks for c in t.clips if c.id == r["id"])
        assert tc.style.color == color


# ---------- style resolution ----------

def test_style_sentinels_mean_role_defaults():
    c = TextClip(text="x", start=0, end=1)  # default TextStyle
    assert resolve_style_overrides(c) == (None, None)


def test_style_color_and_font_resolve():
    c = TextClip(text="x", start=0, end=1,
                 style=TextStyle(color="#FF2020", font="BebasNeue-Regular"))
    fill, font = resolve_style_overrides(c)
    assert fill == (255, 32, 32, 255)
    assert font is not None and font.name == "BebasNeue-Regular.ttf"


def test_default_role_schema_default_style_is_unset():
    """Regression pin: the 'default' role's OWN font is Inter-Bold, not the
    TextStyle schema default 'Inter-Black' — so a per-role-only sentinel
    check would misread every default-role clip's default-populated
    TextStyle as an explicit Inter-Black override. The schema default must
    ALWAYS mean unset, independent of role."""
    c = TextClip(text="x", start=0, end=1)  # role=None -> "default" role style
    fill, font = resolve_style_overrides(c, role="default")
    assert fill is None and font is None


def test_caption_font_sentinel_is_per_role_not_global():
    """'Inter-Black' is caption's OWN role font (ROLE_STYLES) AND the
    TextStyle schema default — a caption clip carrying either value renders
    identically (its own role font), so both are correctly treated as
    'unset'. This is the harmless half of the sentinel/role-font collision;
    see test_default_role_schema_default_style_is_unset for the half that
    would have been a real regression if left per-role-only."""
    caption = TextClip(text="x", start=0, end=1, role="caption",
                       style=TextStyle(font="Inter-Black"))
    _, font = resolve_style_overrides(caption, role="caption")
    assert font is None, "caption's own role font should resolve as unset"


def test_non_default_non_caption_role_gets_own_font_not_inter_black():
    """A role whose OWN font is NOT Inter-Black (e.g. hook -> BebasNeue) and
    whose style was never touched still resolves as unset (schema-default
    sentinel), and correctly renders in ITS OWN font, not Inter-Black —
    demonstrating the schema-default half of the two-part check does not
    accidentally force Inter-Black onto every other role."""
    hook = TextClip(text="x", start=0, end=1, role="hook")  # untouched style
    fill, font = resolve_style_overrides(hook, role="hook")
    assert fill is None and font is None  # unset -> render_text_png uses hook's BebasNeue


def test_style_changes_png_cache_key(store: EDLStore, tmp_path: Path):
    """Two clips differing only in style color must not share a cached PNG."""
    tr = store.edl.get_track("tx_super")
    tr.clips.append(TextClip(text="SAME", start=0, end=1, role="super"))
    tr.clips.append(TextClip(text="SAME", start=2, end=3, role="super",
                             style=TextStyle(color="#FF2020")))
    paired = cache_text_pngs(store.edl, tmp_path / "cache")
    pngs = {p.name for _, _, p in paired}
    assert len(pngs) == 2


# ---------- emitted graph ----------

def _chain_for(store: EDLStore, tmp_path: Path) -> tuple[str, list[str]]:
    chain, inputs, _ = build_overlay_chain(
        store.edl, tmp_path / "cache", source_label="[base]", out_label="[vout]",
        first_input_index=1, out_w=320, out_h=240)
    return chain, inputs


def test_anim_text_gets_offset_looped_input(store: EDLStore, tmp_path: Path):
    dispatch(store, "add_text", {"text": "HI", "start": 1.0, "end": 3.0,
                                 "role": "hook", "anim_in": "fade"})
    chain, inputs, = _chain_for(store, tmp_path)
    i = inputs.index("-itsoffset")
    assert inputs[i + 1] == "1.000"
    assert "fade=t=in:st=1.000" in chain


def test_pop_emits_per_frame_scale(store: EDLStore, tmp_path: Path):
    dispatch(store, "add_text", {"text": "HI", "start": 0.0, "end": 2.0,
                                 "role": "hook", "anim_in": "pop"})
    chain, _ = _chain_for(store, tmp_path)
    assert "eval=frame" in chain and "0.657" in chain


def test_static_text_still_plain_input(store: EDLStore, tmp_path: Path):
    dispatch(store, "add_text", {"text": "HI", "start": 0.0, "end": 2.0})
    _, inputs = _chain_for(store, tmp_path)
    assert "-itsoffset" not in inputs and "-loop" not in inputs


def test_overlays_sorted_by_track_z(store: EDLStore, tmp_path: Path):
    """A sticker on a lower-z track must composite BEFORE (under) text on a
    higher-z track, regardless of collection order (text used to be appended
    first unconditionally, putting every sticker on top of every text clip)."""
    from PIL import Image
    from video_ai_editor.edl.schema import Sticker
    png = tmp_path / "s.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(png)
    store.edl.get_track("stickers").clips.append(  # z=12
        Sticker(src=str(png), start=0, end=2))
    store.edl.get_track("captions").clips.append(  # z=13
        TextClip(text="CAP", start=0, end=2, role="caption"))
    _, inputs = _chain_for(store, tmp_path)
    # PNG paths land in the -i input list in composite order: the (lower-z)
    # sticker PNG must be an earlier input than the caption's text PNG.
    pngs = [a for a in inputs if a.endswith(".png")]
    assert len(pngs) == 2
    assert "st_" in Path(pngs[0]).name, pngs
    assert "text_" in Path(pngs[1]).name, pngs


# ---------- pixels ----------

def test_anim_and_style_color_reach_pixels(store: EDLStore, tmp_path: Path):
    """Export render: red 'POP' at [1,3] with pop-in + fade-out. Verifies the
    itsoffset timing fix (clip starts mid-timeline), the pop growth, the fade
    tail, the enable window, and that style.color actually renders."""
    import numpy as np
    from PIL import Image
    from video_ai_editor.render.compositor import render_export

    dispatch(store, "add_text", {"text": "POP", "start": 1.0, "end": 3.0,
                                 "role": "hook", "anim_in": "pop",
                                 "anim_out": "fade", "color": "#FF2020"})
    res = render_export(store.edl, store.dir, height=240)
    out = Path(res.path)

    def red_px(t: float) -> int:
        f = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", str(t),
                        "-i", str(out), "-frames:v", "1", str(f)], check=True)
        a = np.asarray(Image.open(f).convert("RGB")).astype(int)
        mask = (a[:, :, 0] > 120) & (a[:, :, 0] > a[:, :, 1] + 60) & (a[:, :, 0] > a[:, :, 2] + 60)
        return int(mask.sum())

    before, mid_pop, full, fade_tail, after = (red_px(t) for t in (0.5, 1.10, 2.0, 2.93, 3.5))
    assert before == 0, "text visible before its window"
    assert 0 < mid_pop < full, f"pop should grow: mid={mid_pop} full={full}"
    assert fade_tail < full, "fade-out did not dim the text"
    assert after == 0, "text visible after its window"
    assert full > 50, "style color (#FF2020) never reached pixels"
