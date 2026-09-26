"""The bundled OFL fonts carry their licence (wave C, font licences).

0.7.2 shipped Anton, Bebas Neue, Inter, Montserrat and the Noto faces with no
licence text at all; OFL-1.1 §2 allows bundling only when "each copy contains
the above copyright notice and this license". `scripts/font_licences.py`
writes OFL.txt next to the fonts (both folders ride the .app's --add-data),
and build_app.sh runs its --check-app against the built bundle. These tests
drive the real script against the real fonts and a synthetic bundle.
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "font_licences.py"


def _load():
    spec = importlib.util.spec_from_file_location("font_licences", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fl = _load()


def test_every_bundled_font_folder_carries_the_notices_and_the_licence():
    for d in fl.FONT_DIRS:
        assert fl.font_files(d), f"{d} has no fonts"
        assert fl.check_dir(d) == [], fl.check_dir(d)
    # The same file in both places (the renderer's folder and the preview's).
    a, b = (d / fl.LICENCE_NAME for d in fl.FONT_DIRS)
    assert a.read_bytes() == b.read_bytes()


def test_the_licence_text_is_the_verbatim_ofl_1_1():
    body = fl.OFL_BODY
    assert body.startswith("-----------------------------------------------------------\n"
                           "SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n")
    assert body.rstrip().endswith("OTHER DEALINGS IN THE FONT SOFTWARE.")
    # google/fonts ofl/anton/OFL.txt, from its first rule line (one trailing
    # space after "embedded," is part of the upstream text).
    assert "can be bundled, embedded, \nredistributed" in body
    assert hashlib.sha256(body.encode()).hexdigest() == \
        "f05e84c3000faf09cf8e445d35018b01fc0d6026953840e9398aeab501b86da2"


def test_the_copyright_notice_is_read_from_the_font_itself():
    ft = pytest.importorskip("fontTools.ttLib")
    for d in fl.FONT_DIRS:
        for f in fl.font_files(d):
            want = ft.TTFont(f, lazy=True)["name"].getDebugName(0)
            assert fl.name_record(f, 0) == want, f
            assert want in (d / fl.LICENCE_NAME).read_text(encoding="utf-8")


def _fake_app(tmp_path: Path, with_licence: bool) -> Path:
    app = tmp_path / "Video AI Editor.app"
    for src, rel in zip(fl.FONT_DIRS, fl.APP_FONT_DIRS):
        dst = app / rel
        dst.mkdir(parents=True)
        for f in fl.font_files(src)[:2]:
            shutil.copy2(f, dst / f.name)
        if with_licence:
            shutil.copy2(src / fl.LICENCE_NAME, dst / fl.LICENCE_NAME)
    return app


def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_the_build_check_fails_a_bundle_without_the_licence(tmp_path):
    app = _fake_app(tmp_path, with_licence=False)
    r = _cli("--check-app", str(app))
    assert r.returncode == 1
    assert "OFL.txt: missing" in r.stderr


def test_the_build_check_passes_a_bundle_with_the_licence(tmp_path):
    app = _fake_app(tmp_path, with_licence=True)
    r = _cli("--check-app", str(app))
    assert r.returncode == 0, r.stderr


def test_the_build_check_catches_an_altered_licence_or_an_unlisted_font(tmp_path):
    app = _fake_app(tmp_path, with_licence=True)
    lic = app / fl.APP_FONT_DIRS[0] / fl.LICENCE_NAME
    lic.write_text(lic.read_text(encoding="utf-8").replace("TERMINATION", "TERMINATED"), encoding="utf-8")
    extra = next(f for f in fl.font_files(fl.FONT_DIRS[0]) if f.name.startswith("Noto"))
    other = app / fl.APP_FONT_DIRS[1] / "Renamed-Copy.ttf"
    shutil.copy2(extra, other)
    problems = fl.check_app(app)
    assert any("altered" in p for p in problems), problems
    assert any("Renamed-Copy.ttf is not listed" in p for p in problems), problems
