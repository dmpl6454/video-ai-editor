"""A sticker image the tests make themselves.

`add_sticker(emoji=...)` needs the emoji artwork cache or an installed colour
emoji font (gate X3: never the network), which CI runners lack. Tests that are
about span, lane, ripple or duplicate logic rather than emoji artwork add a
`src` sticker from this PNG instead, which keeps them hermetic.
"""
from __future__ import annotations

from pathlib import Path

STICKER_SIZE = (32, 32)
STICKER_RGBA = (255, 120, 0, 255)


def sticker_png(tmp_path: Path) -> str:
    """Path of a tiny opaque PNG under `tmp_path` (written once per directory)."""
    from PIL import Image
    p = tmp_path / "sticker.png"
    if not p.exists():
        Image.new("RGBA", STICKER_SIZE, STICKER_RGBA).save(p)
    return str(p)
