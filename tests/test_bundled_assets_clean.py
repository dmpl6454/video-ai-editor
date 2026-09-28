"""Final QA: what ships in the app bundle carries nothing from the build Mac,
and saving a show never writes into the (signed, read-only) bundle.

`presets/shows/_feature_test.json` — a test's leftover, tracked in git and
bundled with the whole presets/ folder — held the owner's home path
(`/Users/…/workdir/s_…/uploads/audio/test_music.wav`) and `@test.handle`, and
the shipped app listed it under Shows. And in a frozen build `shows_dir()` was
`sys._MEIPASS/presets/shows`, so "save show template" wrote into the signed
bundle (codesign: "a sealed resource is missing or invalid").
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = [ROOT / "presets", ROOT / "fonts", ROOT / "frontend" / "dist"]   # the .spec datas
_HOME_MARKERS = (b"/Users/", b"C:\\\\Users\\\\", b"/home/")


def _files():
    for base in SHIPPED:
        if base.is_dir():
            yield from (p for p in base.rglob("*") if p.is_file())


def test_no_shipped_asset_carries_a_home_directory_path():
    bad = [str(p.relative_to(ROOT)) for p in _files()
           if any(m in p.read_bytes() for m in _HOME_MARKERS)]
    assert not bad, f"assets that would ship a machine's home path: {bad}"


def test_no_test_or_private_show_preset_ships():
    shows = ROOT / "presets" / "shows"
    leftovers = [p.name for p in shows.glob("*.json") if p.name.startswith(("_", "test"))] if shows.is_dir() else []
    assert not leftovers, leftovers


def test_a_frozen_build_saves_shows_to_the_user_folder_and_still_lists_bundled_ones(tmp_path, monkeypatch):
    from video_ai_editor import platformutil as _pu
    from video_ai_editor.edl import EDL
    from video_ai_editor.show import templates
    bundle = tmp_path / "App.app" / "Contents" / "Frameworks"
    (bundle / "presets" / "shows").mkdir(parents=True)
    (bundle / "presets" / "shows" / "quicksolutions_techtip.json").write_text('{"canvas": null}')
    user = tmp_path / "user-data"
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(templates, "PRESETS_DIR", bundle / "presets")
    monkeypatch.setattr(_pu, "user_data_dir", lambda app: user)
    before = sorted(p.relative_to(bundle) for p in bundle.rglob("*"))
    written = templates.save_show("weekly", EDL())
    assert written == user / "shows" / "weekly.json" and written.exists()
    assert sorted(p.relative_to(bundle) for p in bundle.rglob("*")) == before   # the bundle is untouched
    assert templates.list_shows() == ["quicksolutions_techtip", "weekly"]
    assert templates.load_show("quicksolutions_techtip") == {"canvas": None}
    assert templates.load_show("weekly")["canvas"]["w"]
    with pytest.raises(ValueError):
        templates.load_show("missing")
