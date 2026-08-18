"""Version contract: /api/version and /api/health must report the VERSION file.

Versioning is a durable practice for this app — the VERSION file at the repo
root is the single source of truth, surfaced to the backend (these endpoints)
and the frontend top bar. These tests fail loudly if that wiring drifts.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from video_ai_editor.main import app

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def test_version_file_is_semver():
    assert VERSION_FILE.exists(), "VERSION file must exist at repo root"
    v = VERSION_FILE.read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"VERSION must be semver, got {v!r}"


def test_version_endpoint_matches_file():
    expected = VERSION_FILE.read_text().strip()
    c = TestClient(app)
    r = c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"] == expected


def test_health_reports_version():
    expected = VERSION_FILE.read_text().strip()
    c = TestClient(app)
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == expected


# --- UTF-8 BOM tolerance -----------------------------------------------------
# Found by actually running the packaged Windows exe: `/api/version` reported
# build "﻿c93af1e-dirty". build_win.ps1 wrote BUILD_ID with PowerShell
# 5.1's `Set-Content -Encoding utf8`, which prepends EF BB BF, and neither
# `read_text(encoding="utf-8")` nor `.strip()` removes a BOM. Reproduces ONLY
# on Windows and only in a real build — no dev path writes that file.

def test_read_text_config_strips_bom_and_is_otherwise_identical(tmp_path):
    import video_ai_editor.platformutil as pu

    bom = tmp_path / "with_bom.txt"
    bom.write_bytes(b"\xef\xbb\xbfc93af1e-dirty")
    assert pu.read_text_config(bom) == "c93af1e-dirty"
    # A BOM is not whitespace, so the old read+strip could not have fixed it.
    assert pu.read_text_utf8(bom).strip() != "c93af1e-dirty"

    # utf-8-sig must decode BOM-less input byte-for-byte the same, or this
    # helper would be a behaviour change rather than a strict widening.
    plain = tmp_path / "plain.txt"
    plain.write_text("héllo = wörld\nline2\n", encoding="utf-8")
    assert pu.read_text_config(plain) == pu.read_text_utf8(plain)


def test_build_id_ignores_a_bom_on_build_id_file(tmp_path):
    import video_ai_editor.config as cfg

    (tmp_path / "BUILD_ID").write_bytes(b"\xef\xbb\xbfdeadbee-dirty")
    saved_root, saved_cache = cfg.PROJECT_ROOT, cfg._BUILD_ID
    try:
        cfg.PROJECT_ROOT = tmp_path
        cfg._BUILD_ID = None  # defeat the lazy cache
        assert cfg.build_id() == "deadbee-dirty"
    finally:
        # Restore in this order so the module keeps the identity it had before
        # this test: a leaked cache would make every later assertion on
        # /api/version see this fixture's sha.
        cfg.PROJECT_ROOT = saved_root
        cfg._BUILD_ID = saved_cache


def test_dotenv_with_a_bom_still_loads_its_first_key(tmp_path):
    """The worst case of this bug: a Windows user's hand-written `.env`.

    Notepad and PowerShell 5.1 both write a BOM, so the FIRST key parsed as
    `﻿ANTHROPIC_API_KEY` — the real key was never set and the app reported
    it missing while the file plainly contained it.
    """
    import os

    import video_ai_editor.config as cfg

    key = "VAI_BOM_PROBE_KEY"  # first line, so the BOM lands on it
    env = tmp_path / ".env"
    env.write_bytes(f"{key}=sk-probe\nVAI_BOM_PROBE_SECOND=2\n".encode("utf-8-sig"))
    try:
        cfg._apply_env_file(env)
        assert os.environ.get(key) == "sk-probe"
        assert not any(k.startswith("﻿") for k in os.environ)
    finally:
        os.environ.pop(key, None)
        os.environ.pop("VAI_BOM_PROBE_SECOND", None)
        os.environ.pop("﻿" + key, None)


def test_build_win_ps1_writes_build_id_without_a_bom():
    """Guard the WRITER, since the reader fix alone would hide a regression here.

    `Set-Content -Encoding utf8` is BOM-less on PowerShell 7 but BOM'd on 5.1,
    and the documented command (`powershell -File build_win.ps1`) is 5.1 — so
    the bug depended on which shell ran the build, which is exactly the kind of
    difference a test should pin down rather than leave to chance.
    """
    ps1 = (Path(__file__).resolve().parents[1] / "build_win.ps1").read_text(
        encoding="utf-8")
    write_lines = [ln for ln in ps1.splitlines()
                   if "BUILD_ID" in ln and not ln.strip().startswith("#")]
    assert write_lines, "build_win.ps1 must still write BUILD_ID"
    assert not any("Set-Content" in ln for ln in write_lines), (
        "BUILD_ID must not be written with Set-Content -Encoding utf8 "
        "(BOM under PowerShell 5.1); use a BOM-less UTF8Encoding writer")
    assert "UTF8Encoding $false" in ps1


def test_build_win_ps1_verifies_the_packaged_ui_is_not_stale():
    """A frozen app serving an old bundle is invisible from the outside — the
    window opens, every route answers 200 — and it reads as "the fix didn't
    work". Same rule as /api/version's build id and desktop.py's mtime check:
    what ships must equal what runs.
    """
    ps1 = (Path(__file__).resolve().parents[1] / "build_win.ps1").read_text(
        encoding="utf-8")
    assert "Compare-Object" in ps1, "no staleness comparison in build_win.ps1"
    assert "_internal\\frontend\\dist\\assets" in ps1, (
        "the comparison must read the assets actually bundled into dist/")
    assert "STALE" in ps1
    # ...and pyinstaller's own exit code must be asserted: $ErrorActionPreference
    # does NOT trip on a native exe's non-zero exit.
    assert 'throw "pyinstaller failed' in ps1


def test_a_dirty_build_id_is_unique_per_build():
    """`<sha>-dirty` alone does not identify a build.

    During an uncommitted fix round every rebuild reports the identical
    `<sha>-dirty`, so the badge cannot answer "are you running my fix?" — the
    one question this whole mechanism exists for. It cost a real round: a fix
    was verified in a fresh build, reported as still broken, and neither side
    could establish whether the same bits were on screen.

    The stamp is appended ONLY when the tree is dirty, so a committed build
    keeps its clean `<sha>` identity and every existing expectation holds.
    """
    ps1 = (Path(__file__).resolve().parents[1] / "build_win.ps1").read_text(
        encoding="utf-8")
    dirty = [ln for ln in ps1.splitlines()
             if "-dirty" in ln and not ln.strip().startswith("#")]
    assert dirty, "build_win.ps1 must still mark a dirty tree"
    assert any("Get-Date" in ln for ln in dirty), (
        "a dirty BUILD_ID must carry a per-build stamp (Get-Date), otherwise "
        "two different builds of uncommitted work report the same identity")
