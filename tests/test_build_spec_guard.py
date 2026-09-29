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
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build_app.sh"


def _working_bash() -> str | None:
    """A bash that can run scripts, or None.

    `shutil.which("bash")` is the WSL launcher (System32\\bash.exe) on the
    Windows runners: with no distro installed it prints UTF-16 "no installed
    distributions" text and exits 1, which says nothing about our scripts.
    On Windows prefer Git Bash; everywhere, accept a candidate only if
    `bash -c` really runs and answers."""
    candidates: list[str] = []
    if os.name == "nt":
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)")]
        candidates += [str(Path(r) / "Git" / "bin" / "bash.exe") for r in roots if r]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(str(Path(local) / "Programs" / "Git" / "bin" / "bash.exe"))
        git = shutil.which("git")
        if git:                                     # <Git>\cmd\git.exe -> <Git>\bin\bash.exe
            candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for cand in candidates:
        if not Path(cand).is_file():
            continue
        try:
            probe = subprocess.run([cand, "-c", "echo vai-bash-ok"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == b"vai-bash-ok":
            return cand
    return None


BASH_SKIP = "no working bash (on Windows `bash` is the WSL launcher without a distro, and Git Bash was not found)"


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
    bash = _working_bash()
    if bash is None:
        pytest.skip(BASH_SKIP)
    # Fed on stdin with CRLF folded to LF: a Windows checkout may translate line
    # endings, which is not what this test is about (the script never runs there).
    proc = subprocess.run([bash, "-n"], input=SCRIPT.read_bytes().replace(b"\r\n", b"\n"),
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
