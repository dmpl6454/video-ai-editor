"""Generate an .ass (libass) subtitle file from EDL text tracks + captions track.

Composited at render time via ffmpeg's `subtitles=` filter so we get GPU-quality
vector text with stroke, shadow, fades, and animation tags.
"""
from __future__ import annotations
from pathlib import Path
from ..edl import EDL
from ..edl.schema import TextClip


# Default styles per text role. Match the house-style fonts.
STYLES = {
    "default": "Inter Bold",
    "super": "Anton",
    "hook": "Bebas Neue",
    "lower_third": "Montserrat Bold",
    "caption": "Inter Black",
    "label": "Inter Bold",
    "watermark": "Inter Bold",
}


def _ts(t: float) -> str:
    """ASS timestamp h:mm:ss.cs"""
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _color(c: str) -> str:
    """Convert #RRGGBB or #RRGGBBAA → ASS &HBBGGRR or &HAABBGGRR."""
    s = c.lstrip("#")
    if len(s) == 6:
        r, g, b = s[0:2], s[2:4], s[4:6]
        return f"&H00{b}{g}{r}".upper()
    if len(s) == 8:
        r, g, b, a = s[0:2], s[2:4], s[4:6], s[6:8]
        # ASS alpha: 00 = opaque, FF = transparent
        ass_a = f"{255 - int(a, 16):02x}"
        return f"&H{ass_a}{b}{g}{r}".upper()
    return "&H00FFFFFF"


def _build_styles_block(canvas_w: int, canvas_h: int) -> str:
    """Define one ASS style per role."""
    rows = []
    rows.append(
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        f"Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        f"Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    role_specs = {
        # name      font            size  primary    outline   bold  border outline shadow align
        "super":    ("Anton",        140, "&H00FFFFFF", "&H00000000", -1,  1,    6,      4,    2),
        "hook":     ("Bebas Neue",   170, "&H00FFFFFF", "&H00000000", -1,  1,    7,      6,    5),
        "lower_third": ("Montserrat", 60, "&H00FFFFFF", "&H00000000", -1,  1,    3,      3,    1),
        "caption":  ("Inter Black",   72, "&H00FFFFFF", "&H00000000", -1,  1,    5,      4,    2),
        "label":    ("Inter Bold",    52, "&H00FFFFFF", "&H00000000", -1,  1,    3,      3,    7),
        "watermark":("Inter Bold",    36, "&H80FFFFFF", "&H80000000",  0,  1,    2,      2,    3),
        "default":  ("Inter Bold",    72, "&H00FFFFFF", "&H00000000", -1,  1,    4,      3,    2),
    }
    for name, (font, size, primary, outline, bold, border, ol, sh, align) in role_specs.items():
        rows.append(
            f"Style: {name},{font},{size},{primary},&H000000FF,{outline},&H80000000,"
            f"{bold},0,0,0,100,100,0,0,{border},{ol},{sh},{align},80,80,120,1"
        )
    return "\n".join(rows)


def edl_to_ass(edl: EDL) -> str:
    """Build a complete .ass document from all text-typed tracks in the EDL."""
    canvas = edl.canvas
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {canvas.w}\n"
        f"PlayResY: {canvas.h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        f"{_build_styles_block(canvas.w, canvas.h)}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for track in edl.tracks:
        if track.type not in ("text", "captions"):
            continue
        for c in track.clips:
            if not isinstance(c, TextClip):
                continue
            role = c.role or "default"
            text = c.text.replace("\n", "\\N")
            # Simple in/out fade: 200ms each side
            tags = "{\\fad(200,200)}"
            events.append(
                f"Dialogue: 0,{_ts(c.start)},{_ts(c.end)},{role},,0,0,0,,{tags}{text}"
            )
    return header + "\n".join(events) + "\n"


def write_ass(edl: EDL, dst: Path) -> Path | None:
    """Write the .ass for the EDL. Returns the path, or None if no text events."""
    content = edl_to_ass(edl)
    if "Dialogue:" not in content:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")
    return dst
