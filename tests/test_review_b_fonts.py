"""Wave-B review: a `font` argument is a bundled font NAME, never a path.

set_caption_style gained a free-form `font` (QA-075) with no guard, and
add_text / set_property style.font / apply_brand_kit resolved the value with
`FONTS_DIR / name` — pathlib drops the left side of a join with an absolute
path, so '/etc/hosts' reached ImageFont.truetype and every later export 500'd.
"""
from __future__ import annotations

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, CaptionsConfig, TextClip, TextStyle, Track

HOSTILE = ["/etc/hosts", "../../../../../etc/hosts", "..\\..\\x", "fonts/../../etc/hosts", "\x00"]


def _store(tmp_path, *, captions=True) -> EDLStore:
    clips = [TextClip(id="t_c0", text="hello", start=0, end=1, role="caption")]
    tracks = [Track(id="captions", type="captions", config=CaptionsConfig(enabled=True), clips=clips)] if captions else []
    tracks.append(Track(id="text", type="text", clips=[TextClip(id="t_x", text="hi", start=0, end=1)]))
    (tmp_path / "edl.json").write_text(EDL(canvas=Canvas(w=320, h=240), tracks=tracks).model_dump_json())
    return EDLStore(tmp_path)


@pytest.mark.parametrize("bad", HOSTILE)
def test_set_caption_style_refuses_a_font_path(tmp_path, bad):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="not a bundled font"):
        dispatch(store, "set_caption_style", {"font": bad})
    assert store.edl.get_track("captions").config.look is None or \
        store.edl.get_track("captions").config.look.font is None


@pytest.mark.parametrize("bad", HOSTILE[:2])
def test_add_text_and_set_property_refuse_a_font_path(tmp_path, bad):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="not a bundled font"):
        dispatch(store, "add_text", {"text": "x", "start": 0, "end": 1, "font": bad})
    with pytest.raises(ValueError, match="not a bundled font"):
        dispatch(store, "set_property", {"clip_id": "t_x", "path": "style.font", "value": bad})
    with pytest.raises(ValueError, match="not a bundled font"):
        dispatch(store, "apply_brand_kit", {"font": bad})
    assert store.edl.get_clip("t_x")[1].style.font is None


def test_bundled_names_are_still_accepted(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_caption_style", {"font": "Anton-Regular"})
    assert store.edl.get_track("captions").config.look.font == "Anton-Regular"
    dispatch(store, "set_property", {"clip_id": "t_x", "path": "style.font", "value": "BebasNeue-Regular.ttf"})
    dispatch(store, "set_caption_style", {"font": None})
    assert store.edl.get_track("captions").config.look.font is None


@pytest.mark.parametrize("bad", ["/etc/hosts", "../../../../../etc/hosts"])
def test_a_stored_font_path_never_leaves_the_fonts_dir_at_render(tmp_path, bad):
    """An EDL that already carries a hostile font (written before this fix)
    must still render — the renderer falls back to the role font."""
    from video_ai_editor.render.text_overlay import build_overlay_chain, _font_path
    from video_ai_editor.config import FONTS_DIR
    assert _font_path(bad).resolve().is_relative_to(FONTS_DIR.resolve())
    e = EDL(canvas=Canvas(w=320, h=240), tracks=[Track(id="text", type="text", clips=[
        TextClip(text="HI", start=0, end=1, style=TextStyle(font=bad))])])
    chain, _, _ = build_overlay_chain(e, tmp_path / "cache", source_label="[v]", out_label="[vout]",
                                      first_input_index=1, out_w=320, out_h=240)
    assert "overlay" in chain


def test_http_set_caption_style_font_path_is_400(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", tmp_path / "wd", raising=False)
    (tmp_path / "wd").mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    c = TestClient(_main.app)
    sid = c.post("/api/sessions", json={"name": "f"}).json()["id"]
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "add_text", "args": {"text": "x", "start": 0, "end": 1, "font": "/etc/hosts"}})
    assert r.status_code == 400, r.text
