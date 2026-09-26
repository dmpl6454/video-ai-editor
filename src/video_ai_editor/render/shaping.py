"""Complex-script text shaping for the export rasteriser (QA-003).

WHY THIS EXISTS. `render/text_overlay.py` rasterises every text clip with
Pillow, and Pillow's only complex-script layout engine (libraqm) needs
libfribidi at runtime — absent from the app's Pillow wheel
(`features.check('raqm') is False`). Pillow's BASIC layout draws one glyph per
codepoint in logical order, so every exported Hindi caption was misspelled
('नमस्ते' with a visible halant, 'दुनिया' as 'दुनयिा' — the i-matra drawn after
its consonant) and Arabic came out as isolated letters left-to-right, while the
browser preview shaped both correctly. The error only surfaced after delivery.

THE ENGINE. Three self-contained wheels, all bundled by PyInstaller into the
packaged .app (none depends on a Homebrew library):
  * uharfbuzz   — HarfBuzz shaping: conjuncts, matra reordering, Arabic joining
                  forms, mark positioning; glyph ids + 26.6 positions.
  * freetype-py — FreeType with its own libfreetype: renders those glyph ids,
                  and strokes them with FT_Stroker exactly the way Pillow's
                  `stroke_width` does (round caps/joins, radius = stroke px,
                  both borders), so a shaped line looks like a Pillow line.
  * python-bidi — the Unicode Bidirectional Algorithm, to split a mixed
                  Arabic/Latin line into directional runs (rule L2 reorders).

Every text clip is shaped here when the engine is present (so exported Latin
gets the same GPOS kerning the browser preview applies). If the engine is
missing, Latin falls back to Pillow's basic layout, and text that NEEDS shaping
(`needs_shaping`) makes `ShapedFont` raise `ShapingUnavailable` — a render that
would misspell the user's words is refused, loudly, rather than delivered
silently wrong.
"""
from __future__ import annotations


from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw

try:  # pragma: no cover - import guard exercised by `available()`
    import freetype as _ft
    import uharfbuzz as _hb
    from bidi import algorithm as _bidi
    _IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover
    _ft = _hb = _bidi = None  # type: ignore[assignment]
    _IMPORT_ERROR = _e


class ShapingUnavailable(RuntimeError):
    """The complex-script shaper is not installed in this build."""


#: Codepoint ranges whose correct rendering REQUIRES shaping and/or bidi:
#: Hebrew, Arabic (+ supplements, presentation forms), Syriac, Thaana, every
#: Brahmic (Indic) block, Sinhala, Thai, Lao, Tibetan, Myanmar, Khmer.
_COMPLEX_RANGES: tuple[tuple[int, int], ...] = (
    (0x0590, 0x05FF), (0x0600, 0x06FF), (0x0700, 0x074F), (0x0750, 0x077F),
    (0x0780, 0x07BF), (0x08A0, 0x08FF), (0x0900, 0x0DFF), (0x0E00, 0x0EFF),
    (0x0F00, 0x0FFF), (0x1000, 0x109F), (0x1780, 0x17FF), (0xA8E0, 0xA8FF),
    (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF),
)


def needs_shaping(text: str) -> bool:
    """True when `text` holds a script Pillow's basic layout draws wrongly."""
    for ch in text:
        cp = ord(ch)
        if cp < 0x0590:
            continue
        for lo, hi in _COMPLEX_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def available() -> bool:
    return _IMPORT_ERROR is None


def require() -> None:
    if _IMPORT_ERROR is not None:
        raise ShapingUnavailable(
            "Exporting Hindi/Arabic (complex-script) text needs the text shaper "
            "(uharfbuzz + freetype-py + python-bidi), which is missing from this "
            f"build ({_IMPORT_ERROR}). Refusing to export misspelled text — "
            "reinstall the app, or run `uv sync` in a source checkout.")


@dataclass(frozen=True)
class _Run:
    kind: str                 # "text" | "emoji"
    text: str                 # logical-order characters of the run
    rtl: bool
    glyphs: tuple = ()        # ((gid, x_off, y_off, x_adv), ...) visual order, px
    width: float = 0.0
    font: "ShapedFont | None" = None   # the face that shaped it (None = the caller's)
    script: str | None = None          # ISO 15924 tag it was shaped as
    # Letter spacing (QA-078): the run's width includes `tracking` after each
    # of its grapheme clusters; `tracked_line_width` drops the line's last one.
    tracked: bool = False


#: Joiners and variation selectors stay with the cluster before them.
_CLUSTER_GLUE = frozenset("\u200d\ufe0e\ufe0f")


def grapheme_clusters(text: str) -> list[str]:
    """User-perceived characters of a SIMPLE-script string: a base character
    with its combining marks, variation selectors and ZWJ-joined partners.
    Complex scripts never reach this (they are not tracked). Mirrored by
    `graphemeClusters` in frontend/src/lib/textLayout.ts."""
    import unicodedata as _ud
    out: list[str] = []
    for ch in text:
        if out and (_ud.category(ch).startswith("M") or ch in _CLUSTER_GLUE
                    or out[-1].endswith("\u200d")):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def tracked_line_width(runs: "list[_Run]", tracking: float) -> float:
    """Rule 8 of the shared layout model: a line's width is its runs' widths
    less the spacing after its LAST cluster (tracking sits BETWEEN clusters)."""
    total = sum(r.width for r in runs)
    if tracking and runs and runs[-1].tracked:
        total -= tracking
    return total


# ---- script itemisation ------------------------------------------------------
#
# A line is shaped in runs of ONE script each (mixed-script parity, wave-B
# review). The runs used to be split only by bidi level and emoji, and every
# run was shaped with `guess_segment_properties()`, which takes the script
# from the FIRST strong character: "Hello नमस्ते दुनिया" was shaped as Latin, so
# the Devanagari came out misspelled ('नमस्‌ते दुनयिा'). And the export picked ONE
# font per clip by dominant script, so a Latin-dominant clip drew its
# Devanagari line in Inter — every glyph a .notdef box — while the browser
# preview falls back per character. Now each run carries its script (set on
# the HarfBuzz buffer explicitly) and its own face: the clip's font when it
# covers the run, else the bundled Noto face for that script.

#: (first, last, ISO 15924 tag). Anything not listed: a letter is Latin-like
#: (the clip's font), everything else is Common/Inherited and joins a
#: neighbouring run.
_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0370, 0x03FF, "Grek"), (0x0400, 0x052F, "Cyrl"),
    (0x0590, 0x05FF, "Hebr"), (0x0600, 0x06FF, "Arab"), (0x0700, 0x074F, "Syrc"),
    (0x0750, 0x077F, "Arab"), (0x0780, 0x07BF, "Thaa"), (0x08A0, 0x08FF, "Arab"),
    (0x0900, 0x097F, "Deva"), (0x0980, 0x09FF, "Beng"), (0x0A00, 0x0A7F, "Guru"),
    (0x0A80, 0x0AFF, "Gujr"), (0x0B00, 0x0B7F, "Orya"), (0x0B80, 0x0BFF, "Taml"),
    (0x0C00, 0x0C7F, "Telu"), (0x0C80, 0x0CFF, "Knda"), (0x0D00, 0x0D7F, "Mlym"),
    (0x0D80, 0x0DFF, "Sinh"), (0x0E00, 0x0E7F, "Thai"), (0x0E80, 0x0EFF, "Laoo"),
    (0x0F00, 0x0FFF, "Tibt"), (0x1000, 0x109F, "Mymr"), (0x1780, 0x17FF, "Khmr"),
    (0x1CD0, 0x1CFF, "Deva"), (0x3040, 0x30FF, "Hani"), (0x3400, 0x4DBF, "Hani"),
    (0x4E00, 0x9FFF, "Hani"), (0xA8E0, 0xA8FF, "Deva"), (0xFB1D, 0xFB4F, "Hebr"),
    (0xFB50, 0xFDFF, "Arab"), (0xFE70, 0xFEFF, "Arab"),
)
#: Script tags whose runs are right-to-left in isolation (bidi decides).
_ZW_JOINERS = frozenset("\u200c\u200d")


def script_of(ch: str) -> str | None:
    """ISO 15924 tag of `ch`, or None for a Common/Inherited character
    (spaces, digits, punctuation, combining marks, ZWJ/ZWNJ) that takes the
    script of the run around it."""
    import unicodedata as _ud
    if ch in _ZW_JOINERS:
        return None
    cp = ord(ch)
    if cp >= 0x0370:
        for lo, hi, tag in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                cat = _ud.category(ch)
                # Marks and digits inside a script block still belong to it
                # (matras, nukta, Devanagari digits); block-common punctuation
                # (danda) too — shaping needs them in the run.
                return tag if cat[0] in "LMN" or cat.startswith("P") else None
    return "Latn" if _ud.category(ch).startswith("L") else None


def _split_scripts(chunk: str) -> list[tuple[str | None, str]]:
    """`chunk` as consecutive (script, text) pieces; Common/Inherited
    characters join the PRECEDING piece (leading ones the following)."""
    tags = [script_of(ch) for ch in chunk]
    first = next((t for t in tags if t is not None), None)
    out: list[tuple[str | None, str]] = []
    cur_tag, cur = first, []
    for ch, tag in zip(chunk, tags):
        if tag is not None and tag != cur_tag and cur:
            out.append((cur_tag, "".join(cur)))
            cur = []
        if tag is not None:
            cur_tag = tag
        cur.append(ch)
    if cur:
        out.append((cur_tag, "".join(cur)))
    return out


def _levels(line: str) -> list[tuple[str, int]]:
    """(char, embedding level) in LOGICAL order, via python-bidi's UBA
    implementation (rules P2-P3, X1-X10, W1-W7, N1-N2, I1-I2)."""
    storage = _bidi.get_empty_storage()
    base = _bidi.get_base_level(line)
    storage["base_level"] = base
    storage["base_dir"] = ("L", "R")[base]
    _bidi.get_embedding_levels(line, storage)
    _bidi.explicit_embed_and_overrides(storage)
    _bidi.resolve_weak_types(storage)
    _bidi.resolve_neutral_types(storage, False)
    _bidi.resolve_implicit_levels(storage, False)
    return [(c["ch"], int(c["level"])) for c in storage["chars"]]


def _visual_order(runs: list[tuple[int, object]]) -> list[object]:
    """Rule L2 on runs: from the highest level down to the lowest odd level,
    reverse every maximal sequence of runs at that level or higher."""
    if not runs:
        return []
    items = list(runs)
    hi = max(lv for lv, _ in items)
    lo_odd = min((lv for lv, _ in items if lv % 2), default=None)
    if lo_odd is None:
        return [r for _, r in items]
    for level in range(hi, lo_odd - 1, -1):
        i = 0
        while i < len(items):
            if items[i][0] >= level:
                j = i
                while j < len(items) and items[j][0] >= level:
                    j += 1
                items[i:j] = items[i:j][::-1]
                i = j
            else:
                i += 1
    return [r for _, r in items]


class ShapedFont:
    """One font file at one pixel size (and optional `wght`), able to measure
    and draw a line of complex-script text. Mirrors Pillow's `FreeTypeFont`
    sizing: `size` is the em size in pixels (FT_Set_Pixel_Sizes(0, size))."""

    def __init__(self, path: Path | str, size: int, weight: float | None = None,
                 fallback: "Callable[[str], ShapedFont | None] | None" = None):
        require()
        self.path = str(path)
        self.size = int(size)
        # script tag -> the face for a run this one does not cover (text_overlay
        # supplies the bundled Noto faces); None = no fallback.
        self._fallback = fallback
        self._fallback_cache: dict[str, ShapedFont | None] = {}
        self.face = _ft.Face(self.path)
        self.face.set_pixel_sizes(0, self.size)
        blob = _hb.Blob.from_file_path(self.path)
        self.hb_face = _hb.Face(blob)
        self.hb_font = _hb.Font(self.hb_face)
        upem = self.hb_face.upem
        self._to_px = self.size / float(upem)
        if weight is not None:
            try:
                info = self.face.get_variation_info()
                coords = []
                for ax in info.axes:
                    tag = ax.tag if isinstance(ax.tag, str) else ax.tag.decode()
                    if tag == "wght":
                        coords.append(max(ax.minimum, min(ax.maximum, float(weight))))
                    else:
                        coords.append(ax.default)
                self.face.set_var_design_coords(tuple(coords))
                self.hb_font.set_variations({"wght": float(weight)})
            except Exception:
                pass  # not a variable font: its one weight is the weight

    # -- coverage / fallback -------------------------------------------------
    def covers(self, text: str) -> bool:
        """Every character that needs a glyph has one in this face."""
        for ch in text:
            if ch.isspace() or ch in _ZW_JOINERS or ord(ch) < 0x20:
                continue
            if self.face.get_char_index(ord(ch)) == 0:
                return False
        return True

    def face_for(self, script: str | None, text: str) -> "ShapedFont":
        """This face when it covers `text`, else the fallback face for its
        script (when there is one that covers it better), else this face."""
        if script is None or self._fallback is None or self.covers(text):
            return self
        if script not in self._fallback_cache:
            try:
                self._fallback_cache[script] = self._fallback(script)
            except Exception:  # noqa: BLE001 - a missing fallback keeps this face
                self._fallback_cache[script] = None
        return self._fallback_cache[script] or self

    # -- shaping -----------------------------------------------------------
    def _shape(self, text: str, rtl: bool, script: str | None = None) -> tuple[tuple, float]:
        buf = _hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        if script:
            # Explicitly, never guessed from the first strong character: a
            # Devanagari run after a Latin word was shaped as Latin.
            buf.script = script
        buf.direction = "rtl" if rtl else "ltr"
        _hb.shape(self.hb_font, buf, {"kern": True, "liga": True})
        glyphs = []
        pen = 0.0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            glyphs.append((info.codepoint, pen + pos.x_offset * self._to_px,
                           pos.y_offset * self._to_px))
            pen += pos.x_advance * self._to_px
        return tuple(glyphs), pen

    def runs(self, line: str, split_emoji: Callable[[str], list[tuple[str, str]]],
             emoji_box: float, tracking: float = 0.0) -> list[_Run]:
        """The line as runs in VISUAL (left-to-right) order. Emoji clusters are
        their own runs of fixed `emoji_box` width, placed by the bidi levels of
        the characters around them like any neutral.

        `tracking` (canvas px, QA-078): every grapheme cluster of a simple-
        script run, and every emoji, is followed by that much extra advance;
        each cluster is shaped on its own then, exactly as the preview draws
        it (no kerning or ligature across a spaced pair — CSS drops them too).
        A complex-script run is never spaced, and a right-to-left line not at
        all: both sides agree on that rule rather than on the browser's."""
        logical: list[tuple[int, str, str]] = []   # (level, kind, text)
        levels = _levels(line) if line else []
        if tracking and line and _bidi.get_base_level(line) % 2:
            tracking = 0.0
        # Group consecutive characters by (kind, level). Emoji kind comes from
        # the caller's tokenizer so preview and export split identically.
        pos = 0
        for kind, chunk in split_emoji(line):
            lv_chunk = levels[pos:pos + len(chunk)]
            pos += len(chunk)
            if kind == "emoji":
                lv = lv_chunk[0][1] if lv_chunk else 0
                logical.append((lv, "emoji", chunk))
                continue
            cur_lv, cur = None, []
            for ch, lv in lv_chunk:
                if cur and lv != cur_lv:
                    logical.append((cur_lv, "text", "".join(cur)))
                    cur = []
                cur_lv = lv
                cur.append(ch)
            if cur:
                logical.append((cur_lv, "text", "".join(cur)))
        out: list[tuple[int, _Run]] = []
        for lv, kind, chunk in logical:
            rtl = bool(lv % 2)
            if kind == "emoji":
                out.append((lv, _Run("emoji", chunk, rtl, (), float(emoji_box) + tracking,
                                     tracked=bool(tracking))))
                continue
            # One script per run (see _split_scripts), appended in LOGICAL
            # order at the chunk's level — _visual_order's rule L2 reverses a
            # right-to-left level's runs, exactly as it does around an emoji.
            for script, piece in _split_scripts(chunk):
                face = self.face_for(script, piece)
                if tracking and not rtl and not needs_shaping(piece):
                    glyphs_l: list = []
                    pen = 0.0
                    for cl in grapheme_clusters(piece):
                        g, w = face._shape(cl, False, script)
                        glyphs_l.extend((gid, gx + pen, gy) for gid, gx, gy in g)
                        pen += w + tracking
                    out.append((lv, _Run("text", piece, rtl, tuple(glyphs_l), pen,
                                         None if face is self else face, script, tracked=True)))
                    continue
                glyphs, width = face._shape(piece, rtl, script)
                out.append((lv, _Run("text", piece, rtl, glyphs, width,
                                     None if face is self else face, script)))
        return _visual_order(out)  # type: ignore[return-value]

    def width(self, line: str, split_emoji, emoji_box: float, tracking: float = 0.0) -> float:
        return tracked_line_width(self.runs(line, split_emoji, emoji_box, tracking), tracking)

    # -- rasterising -------------------------------------------------------
    #: Sub-pixel pen positions are snapped to this many steps per pixel so a
    #: glyph repeated along a line (or across a caption track) is rendered
    #: once — a caption re-rendered every glyph from its outline otherwise.
    _SUBPIXEL_STEPS = 4

    def _glyph_bitmap(self, gid: int, frac_step: int, stroke_w: int):
        key = (gid, frac_step, stroke_w)
        cache = self.__dict__.setdefault("_glyph_cache", {})
        if key in cache:
            return cache[key]
        self.face.load_glyph(gid, _ft.FT_LOAD_DEFAULT | _ft.FT_LOAD_NO_BITMAP
                             | _ft.FT_LOAD_NO_HINTING)
        glyph = self.face.glyph.get_glyph()
        if stroke_w > 0:
            stroker = _ft.Stroker()
            stroker.set(int(stroke_w * 64), _ft.FT_STROKER_LINECAP_ROUND,
                        _ft.FT_STROKER_LINEJOIN_ROUND, 0)
            glyph.stroke(stroker, True)
        origin = _ft.FT_Vector(int(round(64 * frac_step / self._SUBPIXEL_STEPS)), 0)
        bm_glyph = glyph.to_bitmap(_ft.FT_RENDER_MODE_NORMAL, origin, True)
        bm = bm_glyph.bitmap
        w, h = bm.width, bm.rows
        got = None
        if w > 0 and h > 0:
            arr = np.frombuffer(bytes(bm.buffer), np.uint8).reshape(h, bm.pitch)[:, :w]
            got = (arr, bm_glyph.left, bm_glyph.top)
        cache[key] = got
        return got

    def mask(self, runs: list[_Run], x: float, baseline: float,
             stroke_w: int = 0) -> tuple[Image.Image, int, int] | None:
        """A tight 'L' coverage mask of the text runs (emoji runs are skipped —
        the caller pastes artwork), drawn from `x` along `baseline`, and the
        image position of its top-left. With `stroke_w` > 0 it is the STROKED
        outline mask, as Pillow's stroke. None when nothing has ink."""
        placed = []
        pen = x
        for run in runs:
            if run.kind == "text":
                face = run.font or self
                for gid, gx, gy in run.glyphs:
                    ox = pen + gx
                    ix = int(ox // 1)
                    step = int(round((ox - ix) * self._SUBPIXEL_STEPS))
                    if step == self._SUBPIXEL_STEPS:
                        ix, step = ix + 1, 0
                    got = face._glyph_bitmap(gid, step, int(stroke_w))
                    if got is None:
                        continue
                    arr, left, top = got
                    placed.append((arr, ix + left, int(round(baseline - gy)) - top))
            pen += run.width
        if not placed:
            return None
        x0 = min(p[1] for p in placed)
        y0 = min(p[2] for p in placed)
        x1 = max(p[1] + p[0].shape[1] for p in placed)
        y1 = max(p[2] + p[0].shape[0] for p in placed)
        out = np.zeros((y1 - y0, x1 - x0), np.uint8)
        for arr, dx, dy in placed:
            # Union (max) of overlapping glyph coverage — joined Arabic
            # letters and stacked marks overlap by design.
            sub = out[dy - y0:dy - y0 + arr.shape[0], dx - x0:dx - x0 + arr.shape[1]]
            np.maximum(sub, arr, out=sub)
        return Image.fromarray(out, "L"), x0, y0


def draw_runs(img: Image.Image, font: ShapedFont, runs: list[_Run], x: float,
              baseline: float, *, fill, stroke_w: int = 0, stroke_fill=None) -> None:
    """Draw shaped runs onto `img` the way `ImageDraw.text(..., stroke_width,
    stroke_fill)` draws a Latin line: stroke mask in `stroke_fill` first,
    then the fill mask in `fill` on top."""
    draw = ImageDraw.Draw(img)
    if stroke_w > 0:
        got = font.mask(runs, x, baseline, stroke_w)
        if got is not None:
            m, mx, my = got
            draw.bitmap((mx, my), m, fill=stroke_fill if stroke_fill is not None else fill)
    got = font.mask(runs, x, baseline, 0)
    if got is not None:
        m, mx, my = got
        draw.bitmap((mx, my), m, fill=fill)


__all__ = ["ShapingUnavailable", "ShapedFont", "needs_shaping", "available",
           "require", "draw_runs", "script_of", "grapheme_clusters", "tracked_line_width"]

