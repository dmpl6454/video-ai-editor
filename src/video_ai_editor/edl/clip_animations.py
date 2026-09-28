"""Clip animations (CapCut In / Out / Combo) — ONE preset table, read by the
schema (which names a field may hold), every renderer (the v1 chain in
`render/compositor.py`, overlays in `render/pip.py`, stickers in
`render/text_overlay.py`), the editing engine (`agent/dispatch.set_animation`),
the agent tool schema, the Prompt Editor's validator and the Inspector
(`GET /api/animations/presets`, `api/animation_routes.py`). The browser's
renderers (`lib/anim/clipAnim.ts`: the engine's geometry, pipDraw and
StickerLayer) read `lib/anim/clipAnimTable.json`, which is THIS table dumped by
`tests/gen_clip_anim_table.py` and pinned equal to it by
`tests/test_clip_animations.py` — the browser keeps no hand-written copy.

The model (mirrors TextClip's anim_in / anim_out / anim_dur):

* `anim_in` / `anim_out` name an In / an Out preset, `anim_combo` a Combo that
  loops over the whole clip. A Combo excludes In and Out (CapCut): the model
  keeps the Combo when both are present, `set_animation` clears the other side.
* `anim_dur` / `anim_out_dur`: seconds the In / Out lasts (None = 0.5 s),
  in ANIM_DUR_RANGE and never more than ANIM_SHARE (40 %, text's cap) of the
  clip's on-screen length, so In and Out never overlap.

An animation is a set of CHANNELS applied ON TOP of the clip's own pose, on
the clip-local TIMELINE clock (`playhead - clip.start`, the keyframe clock):

  scale     × the keyed/static scale        (multiplier)
  x, y      + the keyed/static position     (fraction of the canvas W / H)
  rotation  + the keyed/static rotation     (degrees, clockwise)
  opacity   × the picture                   (a linear 0→1 In / 1→0 Out ramp,
                                             rendered with ffmpeg `fade`)
  blur      a blurred copy mixed over the sharp picture with weight 1→0 In /
            0→1 Out (linear; `gblur` + an alpha `fade` in ffmpeg)

In/Out channels are keyframes over the animation's own progress p ∈ [0, 1]
(p = 1 is the rest pose of an In, p = 0 that of an Out), scaled to seconds per
clip (`plan`) and rendered with `edl.keyframes.frame_exact_expr` — the very
code (and easing) a transform keyframe uses, so a frame shows the animation's
value at its own time on every renderer. Combo channels are sums of sines of
the clip-local time.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

#: Seconds an In / Out lasts when the clip names none (CapCut's default).
ANIM_DUR_DEFAULT = 0.5
#: Bounds of a clip's own `anim_dur` / `anim_out_dur` — text's ANIM_DUR_RANGE.
ANIM_DUR_RANGE: tuple[float, float] = (0.1, 3.0)
#: Most of the clip's on-screen length one side may take — text's 40 %:
#: In + Out never overlap.
ANIM_SHARE = 0.4
#: Gaussian sigma of a full blur, as a share of the output's SHORTER side.
BLUR_SIGMA_FRAC = 0.02

KINDS = ("in", "out", "combo")
#: The model fields each kind lives in.
FIELD_OF_KIND = {"in": "anim_in", "out": "anim_out", "combo": "anim_combo"}
#: Channel names and how they compose with the clip's own pose.
MULTIPLY = ("scale",)
ADD = ("x", "y", "rotation")
KEYED_CHANNELS = MULTIPLY + ADD
RAMP_CHANNELS = ("opacity", "blur")

_EASE_IN = "ease-in"
_EASE_OUT = "ease-out"
_LINEAR = "linear"


@dataclass(frozen=True)
class Channel:
    """A channel of an In/Out: keys over progress p in [0, 1] and an interp
    (the keyframe interps of `edl.keyframes`)."""
    keys: tuple[tuple[float, float], ...]
    interp: str = _LINEAR


@dataclass(frozen=True)
class Wave:
    """A Combo channel: bias + Σ amp·sin(2π·hz·t + phase), t the clip-local
    seconds."""
    bias: float
    terms: tuple[tuple[float, float, float], ...]

    @property
    def peak(self) -> float:
        return abs(self.bias) + sum(abs(a) for a, _hz, _ph in self.terms)


@dataclass(frozen=True)
class AnimPreset:
    id: str
    kind: str
    label: str
    hint: str
    #: lucide icon name the Inspector shows (lib/icons).
    icon: str
    channels: dict[str, Channel] = field(default_factory=dict)
    waves: dict[str, Wave] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "hint": self.hint, "icon": self.icon,
            "channels": {k: {"keys": [list(p) for p in ch.keys], "interp": ch.interp}
                         for k, ch in self.channels.items()},
            "waves": {k: {"bias": w.bias, "terms": [list(t) for t in w.terms]} for k, w in self.waves.items()},
        }


def _ch(*keys: tuple[float, float], interp: str = _LINEAR) -> Channel:
    return Channel(keys=tuple((float(p), float(v)) for p, v in keys), interp=interp)


_FADE_IN = _ch((0, 0), (1, 1))
_FADE_OUT = _ch((0, 1), (1, 0))
_TAU = 2 * math.pi

#: The In set, in CapCut's menu order.
IN_PRESETS: tuple[AnimPreset, ...] = (
    AnimPreset("fade_in", "in", "Fade In", "Fades up from black", "animFadeIn", {"opacity": _FADE_IN}),
    AnimPreset("zoom_in", "in", "Zoom In", "Grows from half size into place", "zoomIn",
               {"scale": _ch((0, 0.5), (1, 1), interp=_EASE_OUT)}),
    AnimPreset("zoom_out", "in", "Zoom Out", "Starts enlarged and settles into place", "zoomOut",
               {"scale": _ch((0, 1.5), (1, 1), interp=_EASE_OUT)}),
    AnimPreset("slide_left", "in", "Slide Left", "Slides in from the right, moving left", "animLeft",
               {"x": _ch((0, 1), (1, 0), interp=_EASE_OUT)}),
    AnimPreset("slide_right", "in", "Slide Right", "Slides in from the left, moving right", "animRight",
               {"x": _ch((0, -1), (1, 0), interp=_EASE_OUT)}),
    AnimPreset("slide_up", "in", "Slide Up", "Slides in from below, moving up", "animUp",
               {"y": _ch((0, 1), (1, 0), interp=_EASE_OUT)}),
    AnimPreset("slide_down", "in", "Slide Down", "Slides in from above, moving down", "animDown",
               {"y": _ch((0, -1), (1, 0), interp=_EASE_OUT)}),
    AnimPreset("rotate", "in", "Rotate", "Turns a quarter turn into place while fading up", "animRotate",
               {"rotation": _ch((0, -90), (1, 0), interp=_EASE_OUT), "opacity": _FADE_IN}),
    AnimPreset("spin", "in", "Spin", "Spins a full turn while growing into place", "animSpin",
               {"rotation": _ch((0, -360), (1, 0), interp=_EASE_OUT),
                "scale": _ch((0, 0.2), (1, 1), interp=_EASE_OUT)}),
    AnimPreset("blur_in", "in", "Blur In", "Comes into focus from a blur", "blur", {"blur": _ch((0, 1), (1, 0))}),
    AnimPreset("bounce", "in", "Bounce", "Pops in with an overshoot and a settle", "animBounce",
               {"scale": _ch((0, 0.3), (0.5, 1.12), (0.7, 0.94), (0.85, 1.03), (1, 1))}),
)

#: The Out set: the mirror of the In set (named by what the picture does as
#: it leaves), in the same order.
OUT_PRESETS: tuple[AnimPreset, ...] = (
    AnimPreset("fade_out", "out", "Fade Out", "Fades down to black", "animFadeOut", {"opacity": _FADE_OUT}),
    AnimPreset("zoom_in", "out", "Zoom In", "Grows as it leaves", "zoomIn",
               {"scale": _ch((0, 1), (1, 1.5), interp=_EASE_IN)}),
    AnimPreset("zoom_out", "out", "Zoom Out", "Shrinks to half size as it leaves", "zoomOut",
               {"scale": _ch((0, 1), (1, 0.5), interp=_EASE_IN)}),
    AnimPreset("slide_left", "out", "Slide Left", "Slides out to the left", "animLeft",
               {"x": _ch((0, 0), (1, -1), interp=_EASE_IN)}),
    AnimPreset("slide_right", "out", "Slide Right", "Slides out to the right", "animRight",
               {"x": _ch((0, 0), (1, 1), interp=_EASE_IN)}),
    AnimPreset("slide_up", "out", "Slide Up", "Slides out upwards", "animUp",
               {"y": _ch((0, 0), (1, -1), interp=_EASE_IN)}),
    AnimPreset("slide_down", "out", "Slide Down", "Slides out downwards", "animDown",
               {"y": _ch((0, 0), (1, 1), interp=_EASE_IN)}),
    AnimPreset("rotate", "out", "Rotate", "Turns a quarter turn away while fading down", "animRotate",
               {"rotation": _ch((0, 0), (1, 90), interp=_EASE_IN), "opacity": _FADE_OUT}),
    AnimPreset("spin", "out", "Spin", "Spins a full turn while shrinking away", "animSpin",
               {"rotation": _ch((0, 0), (1, 360), interp=_EASE_IN),
                "scale": _ch((0, 1), (1, 0.2), interp=_EASE_IN)}),
    AnimPreset("blur_out", "out", "Blur Out", "Goes out of focus as it leaves", "blur", {"blur": _ch((0, 0), (1, 1))}),
    AnimPreset("bounce", "out", "Bounce", "A small bounce, then shrinks away", "animBounce",
               {"scale": _ch((0, 1), (0.15, 1.03), (0.3, 0.94), (0.5, 1.12), (1, 0.3))}),
)

#: The Combo set: loops over the whole clip.
COMBO_PRESETS: tuple[AnimPreset, ...] = (
    AnimPreset("rock", "combo", "Rock", "Rocks gently side to side", "animRock",
               waves={"rotation": Wave(0.0, ((8.0, 1.0, 0.0),))}),
    AnimPreset("swing", "combo", "Swing", "Swings and sways side to side", "animSwing",
               waves={"rotation": Wave(0.0, ((12.0, 1 / 1.4, 0.0),)),
                      "x": Wave(0.0, ((0.03, 1 / 1.4, 0.0),))}),
    AnimPreset("pendulum", "combo", "Pendulum", "Swings like a pendulum hung above", "animPendulum",
               waves={"rotation": Wave(0.0, ((18.0, 0.5, 0.0),)),
                      "x": Wave(0.0, ((0.08, 0.5, 0.0),)),
                      "y": Wave(-0.006, ((0.006, 1.0, math.pi / 2),))}),
    AnimPreset("shake", "combo", "Shake", "A quick handheld shake", "animShake",
               waves={"x": Wave(0.0, ((0.012, 9.0, 0.0), (0.006, 13.0, 1.3))),
                      "y": Wave(0.0, ((0.010, 11.0, 0.7), (0.005, 17.0, 2.1)))}),
    AnimPreset("zoom_in_out", "combo", "Zoom In-Out", "Breathes in and out", "animBreathe",
               waves={"scale": Wave(1.06, ((-0.06, 1 / 1.6, math.pi / 2),))}),
)

PRESETS: dict[str, tuple[AnimPreset, ...]] = {"in": IN_PRESETS, "out": OUT_PRESETS, "combo": COMBO_PRESETS}
PRESET_IDS: dict[str, tuple[str, ...]] = {k: tuple(p.id for p in v) for k, v in PRESETS.items()}
_BY_KIND: dict[str, dict[str, AnimPreset]] = {k: {p.id: p for p in v} for k, v in PRESETS.items()}


def _check_table() -> None:
    """Import-time checks: every opacity/blur channel is the canonical linear
    ramp (the renderers draw it with `fade`, which is exactly that ramp), an
    In ends at the rest pose and an Out starts at it."""
    rest = {"scale": 1.0, "x": 0.0, "y": 0.0, "rotation": 0.0}
    for kind in ("in", "out"):
        for p in PRESETS[kind]:
            assert p.channels and not p.waves, p.id
            for name, ch in p.channels.items():
                assert name in KEYED_CHANNELS + RAMP_CHANNELS, (p.id, name)
                assert ch.keys[0][0] == 0.0 and ch.keys[-1][0] == 1.0, (p.id, name)
                if name == "opacity":
                    assert ch == (_FADE_IN if kind == "in" else _FADE_OUT), (p.id, name)
                elif name == "blur":
                    want = _ch((0, 1), (1, 0)) if kind == "in" else _ch((0, 0), (1, 1))
                    assert ch == want, (p.id, name)
                else:
                    at_rest = ch.keys[-1][1] if kind == "in" else ch.keys[0][1]
                    assert at_rest == rest[name], (p.id, name)
    for p in COMBO_PRESETS:
        assert p.waves and not p.channels and set(p.waves) <= set(KEYED_CHANNELS), p.id


_check_table()


# ------------------------------------------------------------- names

def preset_id(kind: str, name: Any) -> str | None:
    """The canonical id of a `kind` preset for a user/agent spelling
    ("Zoom In", "zoom-in", "ZOOM_IN", "zoomin" → "zoom_in"), or None."""
    if not isinstance(name, str) or kind not in _BY_KIND:
        return None
    raw = name.strip().lower().replace("-", " ").replace("_", " ")
    key = "_".join(raw.split())
    table = _BY_KIND[kind]
    if key in table:
        return key
    squashed = key.replace("_", "")
    for pid in table:
        if pid.replace("_", "") == squashed:
            return pid
    # "fade" / "blur" as an In or Out name ("fade in" is the In spelled with its kind)
    for suffix in ("_" + kind,):
        if key + suffix in table:
            return key + suffix
    return None


def preset(kind: str, pid: str | None) -> AnimPreset | None:
    return _BY_KIND.get(kind, {}).get(pid or "")


def normalize_name(kind: str, name: Any, *, where: str = "?") -> str | None:
    """The model's reading of a stored name: None for unset, the canonical id
    for a known spelling, and None WITH a log line for an unknown one — never
    a load failure over a name a newer build wrote (`set_animation` validates
    loudly at the tool boundary)."""
    if name is None or (isinstance(name, str) and not name.strip()):
        return None
    pid = preset_id(kind, name)
    if pid is None:
        _log.warning("unknown %s animation %r on %s — ignoring (valid: %s)",
                     kind, name, where, ", ".join(PRESET_IDS[kind]))
    return pid


def clamp_dur(v: float | None) -> float | None:
    if v is None:
        return None
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"animation duration must be a finite number, got {v!r}")
    return min(ANIM_DUR_RANGE[1], max(ANIM_DUR_RANGE[0], f))


def duration_of(want: float | None, window: float) -> float:
    """Seconds one side lasts on screen: the clip's own length or the default,
    never more than ANIM_SHARE of the `window` (and never under 0.1 s) — the
    same rule as `text_overlay.anim_duration`."""
    base = ANIM_DUR_DEFAULT if not isinstance(want, (int, float)) else \
        min(ANIM_DUR_RANGE[1], max(ANIM_DUR_RANGE[0], float(want)))
    return min(base, max(0.1, float(window) * ANIM_SHARE))


def has_animation(c: Any) -> bool:
    return bool(getattr(c, "anim_in", None) or getattr(c, "anim_out", None) or getattr(c, "anim_combo", None))


# ------------------------------------------------------------- the plan

def _kf(keys, interp: str) -> dict:
    return {"keyframes": [[float(t), float(v)] for t, v in keys], "interp": interp}


@dataclass(frozen=True)
class AnimPlan:
    """One clip's animation, resolved to clip-local seconds over a window of
    `window` seconds (the clip's on-screen length)."""
    window: float
    d_in: float
    d_out: float
    #: channel → keyframe dicts (clip-local seconds) whose values compose
    #: (multiply for scale, add otherwise).
    keyed: dict[str, list[dict]]
    waves: dict[str, Wave]
    #: (start, duration) of the opacity ramps and the blur mixes.
    fade_in: tuple[float, float] | None
    fade_out: tuple[float, float] | None
    blur_in: tuple[float, float] | None
    blur_out: tuple[float, float] | None

    # -- geometry channels -------------------------------------------
    def channels(self) -> set[str]:
        return set(self.keyed) | set(self.waves)

    def animates(self, name: str) -> bool:
        return name in self.keyed or name in self.waves

    @property
    def geometric(self) -> bool:
        return bool(self.channels())

    def value(self, name: str, t: float) -> float:
        """The channel at clip-local seconds t (1 for scale, else 0, at rest)."""
        from .keyframes import sample
        mul = name in MULTIPLY
        v = 1.0 if mul else 0.0
        for kf in self.keyed.get(name, []):
            s = sample(kf, t)
            v = v * s if mul else v + s
        w = self.waves.get(name)
        if w is not None:
            s = w.bias + sum(a * math.sin(_TAU * hz * t + ph) for a, hz, ph in w.terms)
            v = v * s if mul else v + s
        return v

    def expr(self, name: str, tvar: str) -> str | None:
        """ffmpeg expression of the channel on clock `tvar` (None when the
        channel does not animate): each keyed part through
        `frame_exact_expr`, each wave as its sines."""
        from .keyframes import frame_exact_expr
        parts = [f"({frame_exact_expr(kf, tvar)})" for kf in self.keyed.get(name, [])]
        w = self.waves.get(name)
        if w is not None:
            s = f"{w.bias:.6f}" + "".join(
                f"+({a:.6f})*sin({_TAU * hz:.9f}*{tvar}+{ph:.9f})" for a, hz, ph in w.terms)
            parts.append(f"({s})")
        if not parts:
            return None
        return ("*" if name in MULTIPLY else "+").join(parts)

    def peak(self, name: str) -> float:
        """The largest |value| the channel can take (keys bound linear and
        eased segments alike; a wave its bias plus every amplitude)."""
        mul = name in MULTIPLY
        v = 1.0 if mul else 0.0
        for kf in self.keyed.get(name, []):
            m = max(abs(p[1]) for p in kf["keyframes"])
            v = v * m if mul else v + m
        w = self.waves.get(name)
        if w is not None:
            v = v * w.peak if mul else v + w.peak
        return v


def plan(anim_in: str | None, anim_out: str | None, anim_combo: str | None,
         dur_in: float | None, dur_out: float | None, window: float) -> AnimPlan | None:
    """The animation of a clip over `window` seconds, or None for none."""
    p_in = preset("in", anim_in)
    p_out = preset("out", anim_out)
    p_combo = preset("combo", anim_combo)
    if p_combo is not None:
        p_in = p_out = None
    if p_in is None and p_out is None and p_combo is None:
        return None
    window = max(0.0, float(window))
    d_in = duration_of(dur_in, window) if p_in else 0.0
    d_out = duration_of(dur_out, window) if p_out else 0.0
    keyed: dict[str, list[dict]] = {}
    fade_in = fade_out = blur_in = blur_out = None
    if p_in is not None:
        for name, ch in p_in.channels.items():
            if name == "opacity":
                fade_in = (0.0, d_in)
            elif name == "blur":
                blur_in = (0.0, d_in)
            else:
                keyed.setdefault(name, []).append(_kf([(p * d_in, v) for p, v in ch.keys], ch.interp))
    if p_out is not None:
        st = max(0.0, window - d_out)
        for name, ch in p_out.channels.items():
            if name == "opacity":
                fade_out = (st, d_out)
            elif name == "blur":
                blur_out = (st, d_out)
            else:
                keyed.setdefault(name, []).append(_kf([(st + p * d_out, v) for p, v in ch.keys], ch.interp))
    waves = dict(p_combo.waves) if p_combo is not None else {}
    return AnimPlan(window=window, d_in=d_in, d_out=d_out, keyed=keyed, waves=waves,
                    fade_in=fade_in, fade_out=fade_out, blur_in=blur_in, blur_out=blur_out)


def plan_of(c: Any, window: float) -> AnimPlan | None:
    """`plan` from a Clip or Sticker's fields."""
    if not has_animation(c):
        return None
    return plan(getattr(c, "anim_in", None), getattr(c, "anim_out", None), getattr(c, "anim_combo", None),
                getattr(c, "anim_dur", None), getattr(c, "anim_out_dur", None), window)


def fade_gain(pl: AnimPlan, t: float) -> float:
    """The opacity ramps at clip-local t, as ffmpeg's `fade` computes them
    (16-bit, st/d printed %.3f) — `lib/preview/render/geometry.fadeFactor`."""
    g = 1.0
    for ramp, out in ((pl.fade_in, False), (pl.fade_out, True)):
        if ramp is None:
            continue
        st, d = float(f"{ramp[0]:.3f}"), float(f"{ramp[1]:.3f}")
        if t < st:
            f = 0
        elif t >= st + d:
            f = 65535
        else:
            f = min(65535, max(0, int((t - st) * 65535 / d)))
        if out:
            f = 65535 - f
        g *= f / 65535
    return g


def blur_sigma(out_w: int, out_h: int) -> float:
    return round(BLUR_SIGMA_FRAC * min(out_w, out_h), 3)


def blur_mix_filters(pl: AnimPlan, *, src: str, dst: str, uid: str, sigma: float,
                     tvar: str = "t", alpha_input: bool = False, t0: float = 0.0) -> str:
    """Filtergraph text mixing a blurred copy with `src` into `dst`, weight 1→0
    over an In's window and 0→1 over an Out's. Empty string when the plan has
    no blur (the caller then keeps its own chain). `tvar` is the clip-local
    clock of the `enable` gates; `t0` the stream time of clip-local 0.

    * An opaque picture (the v1 base): per side, `split`, `gblur` (only inside
      its window), an alpha `fade` of the copy (16-bit, `blur_weight`) and an
      `overlay` — cheap on a full canvas.
    * A picture WITH alpha (an overlay element, a sticker): `overlay` would
      keep the sharp copy's hard alpha edge under the blurred one, so the two
      are mixed linearly in every plane, alpha included, with one `blend`
      (`A + (B - A)·w`, w the same ramps on the frame's own time `T`)."""
    sides = [(pl.blur_in, "out"), (pl.blur_out, "in")]
    sides = [(w, f) for w, f in sides if w is not None]
    if not sides:
        return ""
    if alpha_input:
        a, b, c = f"[ab{uid}a]", f"[ab{uid}b]", f"[ab{uid}c]"
        gates = "+".join(f"between({tvar}\\,{st:.6f}\\,{st + d:.6f})" for (st, d), _k in sides)
        terms = []
        for (st, d), kind in sides:
            q = f"clip((T-{t0 + st:.6f})/{d:.6f}\\,0\\,1)"
            terms.append(f"(1-{q})*lt(T\\,{t0 + st + d:.6f})" if kind == "out" else f"{q}*gte(T\\,{t0 + st:.6f})")
        w = "+".join(terms)
        return ";".join([
            f"{src}split=2{a}{b}",
            f"{b}gblur=sigma={sigma:.3f}:steps=2:enable='{gates}'{c}",
            f"{a}{c}blend=all_expr='A+(B-A)*({w})'{dst}",
        ])
    parts: list[str] = []
    cur = src
    for i, ((st, d), fade_kind) in enumerate(sides):
        a, b, c, nxt = f"[ab{uid}a{i}]", f"[ab{uid}b{i}]", f"[ab{uid}c{i}]", (dst if i == len(sides) - 1 else f"[ab{uid}m{i}]")
        gate = f"between({tvar}\\,{st:.6f}\\,{st + d:.6f})"
        parts.append(f"{cur}split=2{a}{b}")
        parts.append(f"{b}gblur=sigma={sigma:.3f}:steps=2:enable='{gate}',format=yuva420p,"
                     f"fade=t={fade_kind}:st={t0 + st:.3f}:d={d:.3f}:alpha=1{c}")
        parts.append(f"{a}{c}overlay=eof_action=pass:format=auto,format=yuv420p{nxt}")
        cur = nxt
    return ";".join(parts)


def blur_weight(pl: AnimPlan, t: float) -> float:
    """The blurred copy's weight at clip-local t (the alpha `fade` of
    `blur_mix_filters`, 16-bit like ffmpeg)."""
    w = 0.0
    for ramp, fade_kind in ((pl.blur_in, "out"), (pl.blur_out, "in")):
        if ramp is None:
            continue
        st, d = float(f"{ramp[0]:.3f}"), float(f"{ramp[1]:.3f}")
        if t < st:
            f = 0
        elif t >= st + d:
            f = 65535
        else:
            f = min(65535, max(0, int((t - st) * 65535 / d)))
        if fade_kind == "out":
            f = 65535 - f
        w = max(w, f / 65535)
    return w


# ------------------------------------------------------------- the catalog

def table_json() -> dict[str, Any]:
    """The whole table: what `GET /api/animations/presets` answers and what
    `lib/anim/clipAnimTable.json` holds (the browser renderers' copy, pinned
    equal by tests/test_clip_animations.py)."""
    return {
        "in": [p.to_json() for p in IN_PRESETS],
        "out": [p.to_json() for p in OUT_PRESETS],
        "combo": [p.to_json() for p in COMBO_PRESETS],
        "dur_default": ANIM_DUR_DEFAULT,
        "dur_range": list(ANIM_DUR_RANGE),
        "share": ANIM_SHARE,
        "blur_sigma_frac": BLUR_SIGMA_FRAC,
    }


def describe(c: Any) -> str:
    """A short human reading of a clip's animation (History, replies)."""
    parts = []
    for kind in KINDS:
        p = preset(kind, getattr(c, FIELD_OF_KIND[kind], None))
        if p is not None:
            parts.append(f"{kind.capitalize()} {p.label}")
    return ", ".join(parts) if parts else "none"


__all__ = [
    "ANIM_DUR_DEFAULT", "ANIM_DUR_RANGE", "ANIM_SHARE", "BLUR_SIGMA_FRAC", "KINDS", "FIELD_OF_KIND",
    "Channel", "Wave", "AnimPreset", "IN_PRESETS", "OUT_PRESETS", "COMBO_PRESETS", "PRESETS", "PRESET_IDS",
    "preset_id", "preset", "normalize_name", "clamp_dur", "duration_of", "has_animation",
    "AnimPlan", "plan", "plan_of", "fade_gain", "blur_sigma", "blur_mix_filters", "blur_weight",
    "table_json", "describe",
]
