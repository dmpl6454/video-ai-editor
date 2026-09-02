"""build_app.sh must never overwrite the committed `Video AI Editor.spec`.

PyInstaller's CLI mode writes `<specpath>/<name>.spec`, and `specpath`
defaults to the CWD — the repo root — so every macOS build silently replaced
the hand-maintained Windows spec that build_win.ps1 and
tests/test_transcribe_backend.py::test_spec_bundles_faster_whisper_data_files
depend on. The guard is `--specpath` under build/ (git-ignored).

Trap that comes with it: PyInstaller resolves relative `--add-data` SOURCES
against the spec's directory (building/build_main.py,
format_binaries_and_datas(workingdir=spec_dir)), not the CWD, so a relative
source would silently look under build/… — every source must be absolute.

No PyInstaller run and no git-status assertion here: CI never runs the
build, so those checks would be vacuous. This pins the script text.
"""
from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build_app.sh"


def _code() -> str:
    """The script minus comment lines, so prose about the flags can't satisfy
    (or trip) an assertion about the flags."""
    return "\n".join(ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


def _shell_var(code: str, name: str) -> str:
    m = re.search(rf'^{name}="([^"]+)"', code, re.M)
    assert m, f"{name} is not assigned in build_app.sh"
    return m.group(1)


def test_build_app_sh_emits_its_spec_outside_the_repo_root():
    code = _code()
    m = re.search(r'--specpath\s+"([^"]+)"', code)
    assert m, "build_app.sh must pass --specpath, or PyInstaller writes its spec into the repo root"
    value = m.group(1)
    if value.startswith("$"):                      # --specpath "$SPEC_DIR"
        value = _shell_var(code, value.strip("${}"))
    assert "build/" in value, value


def test_build_app_sh_add_data_sources_are_absolute():
    pairs = re.findall(r'--add-data\s+"([^"]+):([^"]+)"', _code())
    assert len(pairs) == 5, pairs
    for src, _dst in pairs:
        assert src.startswith("$ROOT/"), (
            f"--add-data source {src!r} is relative: with --specpath, PyInstaller "
            "resolves it against the spec's directory, not the repo root")


def test_build_app_sh_parses():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
