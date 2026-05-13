"""Render text overlay clips as RGBA PNGs (via Pillow), then composite via ffmpeg `overlay`.

Reason: brew's ffmpeg 8 lacks libass and libfreetype, so neither `subtitles=` nor
`drawtext=` is available. PNG overlays via `overlay=` filter work on every build.
PNGs are cached by content hash so re-renders are cheap.

Emoji handling: bundled fonts (Inter, Anton, Bebas Neue, Montserrat) carry no
emoji glyphs, so emoji codepoints would draw as boxes. We fall back to the
system's Apple Color Emoji font when present (macOS) for emoji runs, and strip
emoji entirely on non-Mac systems.
"""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from ..config import FONTS_DIR
from ..edl import EDL
from ..edl.schema import TextClip, Sticker
from ..edl.keyframes import is_keyframed, sample, to_ffmpeg_expr


# Emoji Unicode ranges — pragmatic, not exhaustive.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # most pictographs
    "\U00002600-\U000027BF"  # misc symbols / dingbats
    "\U0001F1E6-\U0001F1FF"  # flags
    "‍️⃣"      # ZWJ + variation selectors + keycap
    "]+", flags=re.UNICODE,
)


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", s).strip()


# Per-role rendering style.
ROLE_STYLES: dict[str, dict] = {
    "super":       {"font": "Anton-Regular.ttf",        "size": 140, "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 6, "shadow": True},
    "hook":        {"font": "BebasNeue-Regular.ttf",    "size": 170, "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 7, "shadow": True},
    "lower_third": {"font": "Montserrat-Bold.ttf",      "size": 56,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 3, "shadow": True},
    "caption":     {"font": "Inter-Black.ttf",          "size": 64,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 5, "shadow": True},
    "label":       {"font": "Inter-Bold.ttf",           "size": 48,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 3, "shadow": True},
    "watermark":   {"font": "Inter-Bold.ttf",           "size": 32,  "fill": (255, 255, 255, 200), "stroke": (0, 0, 0, 140), "stroke_w": 2, "shadow": False},
    "default":     {"font": "Inter-Bold.ttf",           "size": 64,  "fill": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "stroke_w": 4, "shadow": True},
}


def _font_path(name: str) -> Path:
    p = FONTS_DIR / name
    if not p.exists():
        p = FONTS_DIR / "Inter-Bold.ttf"
    return p


# Script detection — pick a Noto fallback font when the text uses non-Latin
# codepoints. PIL's truetype rendering can't auto-fallback, so we switch the
# whole text's font when its dominant script is non-Latin. Single-script
# captions cover ~all real-world cases.
def _pick_script_font(text: str) -> Path | None:
    """Return a path to a Noto font if `text` is dominantly a non-Latin script."""
    counts = {"deva": 0, "arab": 0, "cjk": 0, "latin": 0}
    for ch in text:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:
            counts["deva"] += 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
            counts["arab"] += 1
        elif (0x4E00 <= cp <= 0x9FFF) or (0x3000 <= cp <= 0x30FF) or (0x3400 <= cp <= 0x4DBF):
            counts["cjk"] += 1
        elif ch.isalpha():
            counts["latin"] += 1
    dominant = max(counts, key=counts.get)
    if counts[dominant] == 0 or dominant == "latin":
        return None
    name = {"deva": "NotoSansDevanagari-VF.ttf",
            "arab": "NotoSansArabic-VF.ttf",
            "cjk":  "NotoSansSC-VF.ttf"}[dominant]
    p = FONTS_DIR / name
    return p if p.exists() else None


def _y_for_role(role: str, transform_y: float, canvas_h: int) -> float:
    """Center-y in canvas coords."""
    if role == "watermark":
        return canvas_h - canvas_h * 0.04
    if role == "hook":
        return canvas_h * 0.50
    if role == "caption":
        return canvas_h - canvas_h * 0.16
    if role == "lower_third":
        return canvas_h - canvas_h * 0.20
    if isinstance(transform_y, (int, float)):
        return float(transform_y)
    return canvas_h * 0.75


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            trial = f"{cur} {w}"
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def render_text_png(text: str, role: str, canvas_w: int, canvas_h: int) -> Image.Image:
    """Render a transparent canvas-sized PNG with text drawn for the given role.

    Emoji are stripped from the text before drawing (bundled fonts have no
    emoji glyphs and would render as boxes).
    """
    text = _strip_emoji(text)
    if not text:
        return Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    style = ROLE_STYLES.get(role, ROLE_STYLES["default"])
    # Fall back to a Noto script font when the caption isn't Latin
    script_font = _pick_script_font(text)
    chosen_font = script_font if script_font is not None else _font_path(style["font"])
    font = ImageFont.truetype(str(chosen_font), style["size"])
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    max_w = int(canvas_w * 0.86)
    lines = _wrap(draw, text.upper() if role in ("super", "hook") else text, font, max_w)
    line_h = font.size + 8
    total_h = line_h * len(lines)
    y_center = _y_for_role(role, canvas_h * 0.75, canvas_h)
    y_top = int(y_center - total_h / 2)
    for i, line in enumerate(lines):
        w = draw.textlength(line, font=font)
        x = int((canvas_w - w) / 2)
        y = y_top + i * line_h
        if style.get("shadow"):
            for dx, dy in ((4, 6),):
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 140),
                          stroke_width=style["stroke_w"], stroke_fill=(0, 0, 0, 140))
        draw.text((x, y), line, font=font, fill=style["fill"],
                  stroke_width=style["stroke_w"], stroke_fill=style["stroke"])
    return img


def collect_text_clips(edl: EDL) -> list[tuple[TextClip, str]]:
    """Return all text clips paired with their resolved role.

    Skips tracks that are muted.
    """
    out: list[tuple[TextClip, str]] = []
    for track in edl.tracks:
        if track.type not in ("text", "captions"):
            continue
        if track.muted:
            continue
        for c in track.clips:
            if isinstance(c, TextClip) and c.text.strip():
                out.append((c, c.role or "default"))
    out.sort(key=lambda x: (x[0].start, x[0].end))
    return out


def cache_text_pngs(edl: EDL, cache_dir: Path) -> list[tuple[TextClip, str, Path]]:
    """Render each text clip to a PNG (cached by content hash). Return paired list.

    Cache key uses the *displayable* text (after emoji-strip) so changing the
    rendering rules invalidates the cache for free.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    paired: list[tuple[TextClip, str, Path]] = []
    for c, role in collect_text_clips(edl):
        displayable = _strip_emoji(c.text)
        key = hashlib.sha256(
            f"v2|{role}|{canvas.w}x{canvas.h}|{displayable}".encode()
        ).hexdigest()[:16]
        png = cache_dir / f"text_{key}.png"
        if not png.exists():
            img = render_text_png(c.text, role, canvas.w, canvas.h)
            img.save(png)
        paired.append((c, role, png))
    return paired


def collect_stickers(edl: EDL) -> list[Sticker]:
    out: list[Sticker] = []
    for track in edl.tracks:
        if track.type != "sticker" or track.muted:
            continue
        for c in track.clips:
            if isinstance(c, Sticker):
                out.append(c)
    out.sort(key=lambda s: s.start)
    return out


def _sticker_is_animated(s: Sticker) -> bool:
    """A sticker animates server-side if any of x / y / opacity has keyframes.
    Scale + rotation animate in the browser preview only — the render bakes
    them as their current (last) value."""
    tx = s.transform
    return any(is_keyframed(getattr(tx, p)) for p in ("x", "y", "opacity"))


def _scalar_or_last(v: float | dict | object, default: float = 0.0) -> float:
    """Return either the scalar or the value of the last keyframe."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        kfs = v.get("keyframes") or []
    else:
        kfs = getattr(v, "keyframes", []) or []
    if not kfs:
        return default
    return float(sorted(kfs, key=lambda p: p[0])[-1][1])


def _render_sticker_smallpng(sticker: Sticker, canvas_w: int, canvas_h: int,
                             dst: Path) -> tuple[int, int] | None:
    """Render the sticker PNG at its natural size (no canvas padding).
    Returns (w, h) of the resulting PNG, or None on failure."""
    try:
        src = Image.open(sticker.src).convert("RGBA")
    except Exception:
        return None
    tx = sticker.transform
    scale = _scalar_or_last(tx.scale, 1.0)
    rotation = _scalar_or_last(tx.rotation, 0.0)
    base = max(canvas_w, canvas_h)
    target_long = max(16, int(base * 0.22 * scale))
    sw, sh = src.size
    if sw >= sh:
        tw = target_long
        th = max(8, int(sh * (tw / sw)))
    else:
        th = target_long
        tw = max(8, int(sw * (th / sh)))
    img = src.resize((tw, th), Image.LANCZOS)
    if abs(rotation) > 0.01:
        img = img.rotate(-rotation, resample=Image.BICUBIC, expand=True)
    img.save(dst)
    return img.size


def cache_animated_sticker_pngs(edl: EDL, cache_dir: Path
                                ) -> list[tuple[Sticker, Path, tuple[int, int]]]:
    """For animated stickers, render the PNG at natural size (no canvas padding).
    Returns [(sticker, png_path, (w, h)), ...]."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    out: list[tuple[Sticker, Path, tuple[int, int]]] = []
    for s in collect_stickers(edl):
        if not _sticker_is_animated(s):
            continue
        if not s.src or not Path(s.src).exists():
            continue
        scale = _scalar_or_last(s.transform.scale, 1.0)
        rot = _scalar_or_last(s.transform.rotation, 0.0)
        key = hashlib.sha256(
            f"sa|{s.id}|{s.src}|{canvas.w}x{canvas.h}|{scale:.3f}|{rot:.1f}|{Path(s.src).stat().st_mtime}".encode()
        ).hexdigest()[:16]
        dst = cache_dir / f"sa_{key}.png"
        if not dst.exists():
            sz = _render_sticker_smallpng(s, canvas.w, canvas.h, dst)
            if sz is None:
                continue
        from PIL import Image as _PIL
        with _PIL.open(dst) as im:
            sz = im.size
        out.append((s, dst, sz))
    return out


def cache_sticker_pngs(edl: EDL, cache_dir: Path) -> list[tuple[Sticker, Path]]:
    """For each STATIC sticker, produce a canvas-sized RGBA PNG with the sticker
    image placed at its transform position + scale. Animated stickers use the
    expression-based path in cache_animated_sticker_pngs."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    canvas = edl.canvas
    out: list[tuple[Sticker, Path]] = []
    for s in collect_stickers(edl):
        if _sticker_is_animated(s):
            continue
        if not s.src or not Path(s.src).exists():
            continue
        tx = s.transform
        scalar = float(tx.scale if not isinstance(tx.scale, dict) else 1)
        x = float(tx.x if not isinstance(tx.x, dict) else canvas.w / 2)
        y = float(tx.y if not isinstance(tx.y, dict) else canvas.h / 2)
        opacity = float(tx.opacity if not isinstance(tx.opacity, dict) else 1)
        rotation = float(tx.rotation if not isinstance(tx.rotation, dict) else 0)
        key = hashlib.sha256(
            f"st|{s.id}|{s.src}|{canvas.w}x{canvas.h}|{scalar:.3f}|{x:.1f},{y:.1f}|{opacity:.2f}|{rotation:.1f}|{Path(s.src).stat().st_mtime}".encode()
        ).hexdigest()[:16]
        dst = cache_dir / f"st_{key}.png"
        if not dst.exists():
            try:
                src = Image.open(s.src).convert("RGBA")
            except Exception:
                continue
            # Default sticker size is 22% of the canvas's longer edge
            base = max(canvas.w, canvas.h)
            target_long = max(16, int(base * 0.22 * scalar))
            sw, sh = src.size
            if sw >= sh:
                tw = target_long
                th = max(8, int(sh * (tw / sw)))
            else:
                th = target_long
                tw = max(8, int(sw * (th / sh)))
            sticker_img = src.resize((tw, th), Image.LANCZOS)
            if abs(rotation) > 0.01:
                sticker_img = sticker_img.rotate(-rotation, resample=Image.BICUBIC, expand=True)
                tw, th = sticker_img.size
            if opacity < 0.999:
                # Multiply alpha by opacity
                a = sticker_img.split()[-1].point(lambda v: int(v * opacity))
                sticker_img.putalpha(a)
            canvas_img = Image.new("RGBA", (canvas.w, canvas.h), (0, 0, 0, 0))
            paste_x = int(x - tw / 2)
            paste_y = int(y - th / 2)
            canvas_img.alpha_composite(sticker_img, dest=(paste_x, paste_y))
            canvas_img.save(dst)
        out.append((s, dst))
    return out


def build_overlay_chain(
    edl: EDL,
    cache_dir: Path,
    *,
    source_label: str,
    out_label: str,
    first_input_index: int,
    out_w: int,
    out_h: int,
) -> tuple[str, list[str], str]:
    """Return (filter_str, extra_inputs, final_label).

    `first_input_index` is the index of the first overlay input we'll add (after
    the existing video clip inputs). Each PNG is added as a new `-i` input.
    """
    text_paired = cache_text_pngs(edl, cache_dir)
    static_stickers = cache_sticker_pngs(edl, cache_dir)
    animated_stickers = cache_animated_sticker_pngs(edl, cache_dir)

    # Unified item list. Static items get full canvas-sized PNGs that scale to
    # output then overlay at (0,0). Animated stickers get small PNGs overlaid
    # via x/y expressions. Text with keyframed opacity uses a separate "anim_text"
    # path that re-uses the canvas-sized PNG but applies geq alpha.
    canvas = edl.canvas
    items: list[dict] = []
    for c, _role, png in text_paired:
        # Text "transform" exists; check for keyframed opacity
        opa = getattr(c.transform, "opacity", None) if hasattr(c, "transform") else None
        if is_keyframed(opa):
            items.append({"kind": "anim_text", "text_clip": c, "png": png})
        else:
            items.append({"kind": "static", "start": c.start, "end": c.end, "png": png,
                          "opacity": _scalar_or_last(opa, 1.0) if opa is not None else 1.0})
    for s, png in static_stickers:
        items.append({"kind": "static", "start": s.start, "end": s.end, "png": png, "opacity": 1.0})
    for s, png, (sw, sh) in animated_stickers:
        items.append({"kind": "anim", "sticker": s, "png": png, "size": (sw, sh)})

    if not items:
        return "", [], source_label

    extra_inputs: list[str] = []
    parts: list[str] = []
    cur = source_label
    for i, item in enumerate(items):
        idx = first_input_index + i
        # Animated overlays (sticker with keyframed opacity, or text with
        # keyframed opacity) need a time-dimension input so the per-pixel `T`
        # in geq actually ticks. Static items use plain `-i path`. Bound the
        # looped input with `-t` so ffmpeg knows when to stop reading frames.
        if item["kind"] == "anim" and is_keyframed(item["sticker"].transform.opacity):
            s: Sticker = item["sticker"]
            dur = max(0.5, s.end - s.start) + 0.5
            extra_inputs += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(item["png"])]
        elif item["kind"] == "anim_text":
            tc = item["text_clip"]
            dur = max(0.5, tc.end - tc.start) + 0.5
            extra_inputs += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(item["png"])]
        else:
            extra_inputs += ["-i", str(item["png"])]
        is_last = i == len(items) - 1
        next_label = out_label if is_last else f"[ov_post{i}]"

        if item["kind"] == "static":
            scaled = f"[ov{i}]"
            parts.append(f"[{idx}:v]scale={out_w}:{out_h}{scaled}")
            parts.append(
                f"{cur}{scaled}overlay=enable='between(t\\,{item['start']:.3f}\\,{item['end']:.3f})'{next_label}"
            )
        elif item["kind"] == "anim_text":
            tc = item["text_clip"]
            scaled = f"[ovs{i}]"
            preprocessed = f"[ov{i}]"
            parts.append(f"[{idx}:v]scale={out_w}:{out_h}{scaled}")
            aexpr = to_ffmpeg_expr(tc.transform.opacity, time_var="T")
            parts.append(
                f"{scaled}format=yuva420p,"
                f"geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='alpha(X\\,Y)*({aexpr})'"
                f"{preprocessed}"
            )
            parts.append(
                f"{cur}{preprocessed}overlay=enable='between(t\\,{tc.start:.3f}\\,{tc.end:.3f})'{next_label}"
            )
        else:
            s: Sticker = item["sticker"]
            sw, sh = item["size"]  # PNG natural pixel size (canvas-aligned)
            tx = s.transform
            tvar = f"(t-{s.start:.4f})"  # clip-local time inside expressions
            sx = out_w / max(1, canvas.w)
            sy = out_h / max(1, canvas.h)
            # The PNG is at canvas-pixel size; rescale to match output pixels.
            sticker_out_w = max(2, int(round(sw * sx)))
            sticker_out_h = max(2, int(round(sh * sy)))

            # Pre-scale the sticker stream so overlay_w / overlay_h match
            # output-pixel size — the centering math then works.
            scaled_label = f"[ovs{i}]"
            parts.append(f"[{idx}:v]scale={sticker_out_w}:{sticker_out_h}{scaled_label}")
            sticker_stream = scaled_label

            # Position. Center on (x, y): subtract overlay_w/_h via ffmpeg vars.
            if is_keyframed(tx.x):
                xe = to_ffmpeg_expr(tx.x, time_var=tvar)
                xexpr = f"({xe})*{sx:.6f}-overlay_w/2"
            else:
                xc = _scalar_or_last(tx.x, canvas.w / 2)
                xexpr = f"{xc * sx - sticker_out_w / 2:.2f}"
            if is_keyframed(tx.y):
                ye = to_ffmpeg_expr(tx.y, time_var=tvar)
                yexpr = f"({ye})*{sy:.6f}-overlay_h/2"
            else:
                yc = _scalar_or_last(tx.y, canvas.h / 2)
                yexpr = f"{yc * sy - sticker_out_h / 2:.2f}"

            # Opacity. Animated → geq with `T` (now meaningful because we loop
            # the input above so the still PNG becomes a video stream). Static
            # → cheap colorchannelmixer.
            preprocessed = f"[ov{i}]"
            if is_keyframed(tx.opacity):
                aexpr = to_ffmpeg_expr(tx.opacity, time_var="T")
                parts.append(
                    f"{sticker_stream}format=yuva420p,"
                    f"geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':a='alpha(X\\,Y)*({aexpr})'"
                    f"{preprocessed}"
                )
            else:
                opa = _scalar_or_last(tx.opacity, 1.0)
                if opa < 0.999:
                    parts.append(f"{sticker_stream}format=yuva420p,colorchannelmixer=aa={opa:.3f}{preprocessed}")
                else:
                    parts.append(f"{sticker_stream}null{preprocessed}")

            parts.append(
                f"{cur}{preprocessed}overlay=x='{xexpr}':y='{yexpr}':enable='between(t\\,{s.start:.3f}\\,{s.end:.3f})'{next_label}"
            )
        cur = next_label
    return ";".join(parts), extra_inputs, cur
