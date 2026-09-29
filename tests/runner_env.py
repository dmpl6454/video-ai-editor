"""What THIS machine is, asked of the machine: one copy of the probes the
CI-round-3 tests share (they were pasted into three modules).

Nothing here guesses from `os.name` where the system can be asked.
"""
from __future__ import annotations

import subprocess
import sys

from video_ai_editor import platformutil as _pu


def is_virtual_mac() -> bool:
    """True on a macOS GUEST (GitHub's macOS runners): the kernel's own
    answer, `sysctl -n kern.hv_vmm_present` = 1. A real Mac prints 0. A guest
    has VideoToolbox without the hardware encoder's rate control and a
    WebKit without a GPU process from the first report (CI run 36599751632)."""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(["sysctl", "-n", "kern.hv_vmm_present"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip() == "1"


def ffmpeg_alive(marker) -> list[tuple[int, int, str]]:
    """(pid, parent pid, end of the command line) of every ffmpeg whose
    command line carries `marker` (a session id, a source path), whoever its
    parent is — for failure messages. On Windows the process a test started
    can be a package manager's launcher whose CHILD is the real ffmpeg (CI
    round 3); only the process table sees the child."""
    return [(pid, ppid, cmd[-120:]) for pid, ppid, cmd in _pu.process_table()
            if "ffmpeg" in cmd.lower() and str(marker) in cmd]
