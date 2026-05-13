"""Scene-change detection via ffmpeg `select='gt(scene,...)'`."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path
from pydantic import BaseModel


class Shot(BaseModel):
    index: int
    start: float
    end: float
    thumbnail: str | None = None  # path relative to session dir


_SHOWINFO_PTS = re.compile(r"pts_time:([\d.]+)")


def detect_shots(src: Path, threshold: float = 0.3) -> list[Shot]:
    """Return shot boundaries (first shot starts at 0; last ends at media duration)."""
    proc = subprocess.run(
        [
            "ffmpeg", "-i", str(src),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    times: list[float] = []
    for line in proc.stderr.splitlines():
        m = _SHOWINFO_PTS.search(line)
        if m and "showinfo" in line:
            times.append(float(m.group(1)))
    # Probe duration to close the last shot
    from .probe import probe
    info = probe(src)
    boundaries = [0.0] + times + [info.duration]
    shots: list[Shot] = []
    for i in range(len(boundaries) - 1):
        shots.append(Shot(index=i, start=boundaries[i], end=boundaries[i + 1]))
    return shots
