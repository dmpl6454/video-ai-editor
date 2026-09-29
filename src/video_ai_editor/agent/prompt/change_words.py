"""Words for the preview card's change list (changes.py): values, names,
times — how an editor says "Clip 2 'beach.mp4'", "-6 dB", "00:00:04:12".
Split from change_rules.py so each file stays one idea."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ...edl.schema import EDL, Clip, Sticker, TextClip
from .live import smpte

#: A value that is absent from a dump (an `exclude_if` field at its default,
#: a clip that does not exist on one side). One object, shared with changes.py.
MISSING: Any = object()


_COLOUR_NAMES: dict[str, str] = {
    "#FFFFFF": "white", "#000000": "black", "#FF0000": "red", "#FF3B30": "red", "#00FF00": "green",
    "#34C759": "green", "#0000FF": "blue", "#0A84FF": "blue", "#FFFF00": "yellow", "#FFD400": "yellow",
    "#FF2D92": "pink", "#FF9500": "orange", "#AF52DE": "purple", "#32D2FF": "cyan", "#00FFFF": "cyan",
    "#FFC300": "gold", "#8E8E93": "grey", "#808080": "grey", "#A4FF00": "lime", "#30B0C7": "teal",
}


def colour(v: Any) -> str:
    if v is None or v is MISSING:
        return "none"
    s = str(v).strip()
    return _COLOUR_NAMES.get(s[:7].upper(), s) if s.startswith("#") else s


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None or v is MISSING:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def fmt_num(v: Any, unit: str = "") -> str:
    n = _num(v)
    if n is None:
        return _fmt_value(v)
    s = f"{round(n, 2):g}"
    return f"{s} {unit}".strip() if unit and not unit.startswith("%") else f"{s}{unit}"


def _kf(v: Any) -> bool:
    return isinstance(v, dict) and "keyframes" in v


def _fmt_value(v: Any) -> str:
    if v is MISSING or v is None:
        return "none"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{round(v, 2):g}"
    if _kf(v):
        n = len(v.get("keyframes") or [])
        return f"animated ({n} keyframe{'s' if n != 1 else ''})"
    if isinstance(v, str):
        return f"'{v[:40]}{'…' if len(v) > 40 else ''}'"
    if isinstance(v, dict):
        kind = v.get("type") or v.get("kind")
        return str(kind) if kind else "set"
    if isinstance(v, list):
        return f"{len(v)} item{'s' if len(v) != 1 else ''}"
    return str(v)


def secs(v: float) -> str:
    return f"{round(v, 1):.1f} s"


def speed_word(v: Any) -> str:
    if v is None or v is MISSING:
        return "1x"
    if isinstance(v, dict):
        name = v.get("preset") or v.get("name")
        return f"curve '{name}'" if name else "a speed curve"
    return f"{round(float(v), 2):g}x"


class Names:
    """How the card names things: "Clip 2 'beach.mp4'", "Music 'bed.mp3'",
    "Title 'Summer Trip'". Clip numbers are positions on the main lane
    BEFORE the change (what the person sees on screen now)."""

    def __init__(self, before: EDL, after: EDL, session_dir: Path | None = None) -> None:
        self.before, self.after = before, after
        self.session_dir = session_dir
        self.fps = before.canvas.fps
        self._v1_index = {c.id: i + 1 for i, c in enumerate(_v1(before))}
        self._track_of: dict[str, Any] = {}
        self._clip: dict[str, Any] = {}
        for edl in (after, before):
            for t in edl.tracks:
                for c in t.clips:
                    self._track_of[c.id] = t
                    self._clip[c.id] = c

    def tc(self, t: float) -> str:
        return smpte(t, self.fps)

    def span(self, a: float, b: float) -> str:
        return f"{self.tc(a)}-{self.tc(b)}"

    def media(self, src: str) -> str:
        try:
            from ...media_offline import display_name_for
            return display_name_for(self.session_dir, str(src))
        except Exception:  # noqa: BLE001 — a name must never break the card
            return Path(str(src)).name

    def v1_number(self, cid: str) -> int | None:
        if cid in self._v1_index:
            return self._v1_index[cid]
        parent = split_parent(cid, self._v1_index)
        return self._v1_index.get(parent) if parent else None

    def clip(self, cid: str) -> str:
        c = self._clip.get(cid)
        t = self._track_of.get(cid)
        if c is None:
            return f"Clip {cid}"
        if isinstance(c, TextClip):
            word = "Caption" if t is not None and t.type == "captions" else "Title"
            return f"{word} '{_short(c.text)}'"
        if isinstance(c, Sticker):
            return f"Sticker '{c.label or Path(c.src).stem}'"
        name = self.media(c.src)
        if t is not None and t.id == "v1":
            n = self.v1_number(cid)
            if n is None:
                return f"New clip '{name}'"
            part = "" if cid in self._v1_index else " (new part)"
            return f"Clip {n} '{name}'{part}"
        kind = {"music": "Music", "vo": "Voice-over", "audio": "Audio"}.get(t.type if t else "", "Overlay")
        return f"{kind} '{name}'"

    def track(self, tid: str) -> str:
        t = next((x for x in self.after.tracks if x.id == tid), None) or \
            next((x for x in self.before.tracks if x.id == tid), None)
        label = (t.label if t is not None and t.label else None) or {
            "v1": "Main video", "v2": "Overlay", "a1": "Audio", "music": "Music", "vo": "Voice-over",
            "captions": "Captions", "stickers": "Stickers", "tx_hook": "Hook text", "tx_super": "Titles",
            "tx_lt": "Lower thirds"}.get(tid, tid)
        return f"{label} track"


def _short(text: str, n: int = 32) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _v1(edl: EDL) -> list[Clip]:
    t = edl.get_track("v1")
    return sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)


def split_parent(cid: str, known: Iterable[str]) -> str | None:
    """`c_ab12cd34_ef5678` → `c_ab12cd34` when that is a known clip: the id
    `split_at` / `cut_range` give the right-hand piece of a clip (dispatch).
    The longest known prefix wins (a piece of a piece)."""
    best: str | None = None
    for k in known:
        if k != cid and cid.startswith(f"c_{k[2:]}_") and (best is None or len(k) > len(best)):
            best = k
    return best


#: A caption track's config / look fields, as an editor says them.
_CAPTION_WORDS: dict[str, str] = {
    "style": "style", "position": "position", "enabled": "shown", "lang": "language",
    "look.color": "colour", "look.stroke": "outline colour", "look.stroke_w": "outline width",
    "look.background": "box", "look.font": "font", "look.size": "size", "look.upper": "all capitals",
    "look.shadow_on": "shadow",
}


def _kf_ends(v: dict) -> tuple[Any, Any] | None:
    kfs = [k for k in (v.get("keyframes") or []) if isinstance(k, (list, tuple)) and len(k) == 2]
    return (kfs[0][1], kfs[-1][1]) if kfs else None


def _pct(v: Any) -> str:
    if _kf(v):
        ends = _kf_ends(v)
        n = len(v.get("keyframes") or [])
        if ends is None:
            return _fmt_value(v)
        # "animated 100% -> 115%": the reply said the amounts, the card did not
        return f"animated ({_pct(ends[0])} to {_pct(ends[1])}, {n} keyframe{'s' if n != 1 else ''})"
    n = 1.0 if v is MISSING or v is None else _num(v)
    return f"{round((n if n is not None else 1.0) * 100):g}%"


def _deg(v: Any) -> str:
    if _kf(v):
        return _fmt_value(v)
    n = _num(v) if v is not MISSING else 0.0
    return f"{round(n or 0.0, 1):g}°"


def _ratio(w: Any, h: Any) -> str:
    try:
        from math import gcd
        w, h = int(w), int(h)
        g = gcd(w, h) or 1
        r = f"{w // g}:{h // g}"
        return {"8:5": "16:10"}.get(r, r)
    except (TypeError, ValueError):
        return "?"


def _trans_name(t: Any) -> str:
    try:
        from ...render.transitions import display_name
        return display_name(str(t or "fade"))
    except Exception:  # noqa: BLE001
        return str(t or "fade")


def _effect_label(e: dict) -> str:
    t = str(e.get("type") or "effect")
    if t == "lut":
        return f"look '{Path(str((e.get('params') or {}).get('src') or '')).stem}'"
    if t in ("color", "color_grade"):
        return "colour grade"
    return f"effect {t.replace('_', ' ')}"


def _effect_detail(e: dict) -> str:
    """The amounts an added effect carries: "brightness 0.1", "strength 80%"."""
    params = {k: v for k, v in (e.get("params") or {}).items() if k != "src"}
    bits: list[str] = []
    for k in sorted(params):
        v = params[k]
        if k == "intensity" and isinstance(v, (int, float)) and not isinstance(v, bool):
            if abs(float(v) - 1.0) > 1e-6:          # full strength is the default: say nothing
                bits.append(f"strength {round(float(v) * 100):g}%")
        else:
            bits.append(f"{k.replace('_', ' ')} {_fmt_value(v)}")
    return f" ({'; '.join(bits[:3])})" if bits else ""


def _param_diffs(e_b: dict, e_a: dict) -> str:
    pb, pa = e_b.get("params") or {}, e_a.get("params") or {}
    diffs = [f"{k} {_fmt_value(pb.get(k, MISSING))} -> {_fmt_value(pa.get(k, MISSING))}"
             for k in sorted(set(pb) | set(pa)) if pb.get(k, MISSING) != pa.get(k, MISSING)]
    others = sorted(k for k in set(e_b) | set(e_a) if k != "params" and e_b.get(k) != e_a.get(k))
    diffs += [f"{k} {_fmt_value(e_b.get(k, MISSING))} -> {_fmt_value(e_a.get(k, MISSING))}" for k in others]
    return "; ".join(diffs[:3]) or "settings"


def _effects_words(bv: Any, av: Any) -> str:
    """Every change to a clip's effect list, each said: an added effect with
    its amounts, a removed one, AND a changed existing one — pairs matched
    by label in order (the k-th colour grade before is the k-th after). A
    parameter change used to be said only when nothing was added or removed,
    so "more contrast and black and white" showed only the new look."""
    bl = [e for e in (bv if isinstance(bv, list) else []) if isinstance(e, dict)]
    al = [e for e in (av if isinstance(av, list) else []) if isinstance(e, dict)]
    pool: dict[str, list[dict]] = {}
    for e in bl:
        pool.setdefault(_effect_label(e), []).append(e)
    added: list[str] = []
    changed: list[str] = []
    for e in al:
        label = _effect_label(e)
        mates = pool.get(label)
        if not mates:
            added.append(label + _effect_detail(e))
            continue
        e_b = mates.pop(0)
        if e_b != e:
            changed.append(f"{label} changed ({_param_diffs(e_b, e)})")
    removed = [label for label, rest in pool.items() for _ in rest]
    parts: list[str] = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    parts.extend(changed)
    if not parts:
        parts.append("effects re-ordered")
    return "; ".join(parts)
