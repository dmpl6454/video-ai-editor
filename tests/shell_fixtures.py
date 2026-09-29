"""Finding a bash that can really run the repo's shell scripts."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def working_bash() -> str | None:
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


def lf_bytes(script: Path) -> bytes:
    """The script's bytes with CRLF folded to LF. The repo has no
    .gitattributes, so a Windows checkout with core.autocrlf=true hands bash
    `set -euo pipefail\\r`; line endings are not what these tests are about."""
    return script.read_bytes().replace(b"\r\n", b"\n")
