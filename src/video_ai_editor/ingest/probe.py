"""ffprobe wrapper — read duration, streams, codec, fps, etc."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from pydantic import BaseModel

from .. import platformutil as _pu


class ProbeStream(BaseModel):
    index: int
    codec_type: str
    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    avg_frame_rate: str | None = None
    sample_rate: int | None = None
    channels: int | None = None


class ProbeResult(BaseModel):
    duration: float
    format_name: str
    bit_rate: int | None = None
    streams: list[ProbeStream] = []

    @property
    def video(self) -> ProbeStream | None:
        return next((s for s in self.streams if s.codec_type == "video"), None)

    @property
    def audio(self) -> ProbeStream | None:
        return next((s for s in self.streams if s.codec_type == "audio"), None)

    @property
    def fps(self) -> float | None:
        v = self.video
        if v and v.avg_frame_rate and "/" in v.avg_frame_rate:
            num, den = v.avg_frame_rate.split("/")
            try:
                d = float(den)
                return float(num) / d if d else None
            except Exception:
                return None
        return None


def probe(path: Path) -> ProbeResult:
    out = subprocess.run(
        [
            _pu.FFPROBE, "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    data = json.loads(out.stdout)
    fmt = data.get("format", {})
    streams = [
        ProbeStream(
            index=s.get("index", 0),
            codec_type=s.get("codec_type", ""),
            codec_name=s.get("codec_name"),
            width=s.get("width"),
            height=s.get("height"),
            duration=float(s["duration"]) if s.get("duration") else None,
            avg_frame_rate=s.get("avg_frame_rate"),
            sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
            channels=s.get("channels"),
        )
        for s in data.get("streams", [])
    ]
    return ProbeResult(
        duration=float(fmt.get("duration", 0.0)),
        format_name=fmt.get("format_name", ""),
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        streams=streams,
    )
