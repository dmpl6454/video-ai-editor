"""Sidecar subtitle import/export — round-trip with the transcript model."""
from __future__ import annotations
import re
from pathlib import Path
from .transcribe import Transcript, Segment, Word

_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


def _parse_ts(s: str) -> float:
    m = _TS.match(s.strip())
    if not m:
        return 0.0
    h, mn, sec, ms = (int(x) for x in m.groups())
    return h * 3600 + mn * 60 + sec + ms / 1000.0


def _fmt_ts(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    mn = int((t % 3600) // 60)
    sec = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{mn:02d}:{sec:02d},{ms:03d}"


def detect_sidecar(video_path: Path) -> Path | None:
    """Return path to a sidecar .srt/.vtt/.ass next to the video, if any."""
    base = video_path.with_suffix("")
    for ext in (".srt", ".vtt", ".ass"):
        p = base.with_suffix(ext)
        if p.exists():
            return p
    return None


def import_srt(srt_path: Path, language: str = "en") -> Transcript:
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    segs: list[Segment] = []
    for i, block in enumerate(blocks):
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        # First line may be index, second is timing, rest is text.
        timing_idx = 1 if "-->" in lines[1] else 0
        if "-->" not in lines[timing_idx]:
            continue
        start_s, end_s = lines[timing_idx].split("-->")
        start = _parse_ts(start_s)
        end = _parse_ts(end_s)
        body = "\n".join(lines[timing_idx + 1:]).strip()
        segs.append(Segment(id=i, start=start, end=end, text=body, words=[
            Word(start=start, end=end, word=body, prob=1.0)
        ]))
    duration = segs[-1].end if segs else 0.0
    return Transcript(language=language, duration=duration, segments=segs)


def export_srt(transcript: Transcript) -> str:
    out: list[str] = []
    for i, seg in enumerate(transcript.segments, start=1):
        out.append(str(i))
        out.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
        out.append(seg.text.strip())
        out.append("")
    return "\n".join(out)


def _fmt_ts_vtt(t: float) -> str:
    # WebVTT uses '.' for the milliseconds separator.
    return _fmt_ts(t).replace(",", ".")


def export_vtt(transcript: Transcript) -> str:
    """Export to WebVTT (HTML5 <track>-compatible)."""
    out: list[str] = ["WEBVTT", ""]
    for seg in transcript.segments:
        out.append(f"{_fmt_ts_vtt(seg.start)} --> {_fmt_ts_vtt(seg.end)}")
        out.append(seg.text.strip())
        out.append("")
    return "\n".join(out)


def export_ass(transcript: Transcript, *,
               font: str = "Inter Black", size: int = 42,
               primary: str = "&H00FFFFFF", outline: str = "&H00000000") -> str:
    """Export to .ass — readable in VLC/MPV; useful for libass-driven burn-ins."""
    head = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\nPlayResX: 1920\nPlayResY: 1080\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: D,{font},{size},{primary},&H000000FF,{outline},&H64000000,"
        "-1,0,0,0,100,100,0,0,1,3,2,2,40,40,80,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    def _ass_ts(t: float) -> str:
        h = int(t // 3600)
        mn = int((t % 3600) // 60)
        sec = t - h * 3600 - mn * 60
        return f"{h}:{mn:02d}:{sec:05.2f}"
    body = []
    for seg in transcript.segments:
        text = seg.text.strip().replace("\n", "\\N")
        body.append(f"Dialogue: 0,{_ass_ts(seg.start)},{_ass_ts(seg.end)},D,,0,0,0,,{text}")
    return head + "\n".join(body) + "\n"
