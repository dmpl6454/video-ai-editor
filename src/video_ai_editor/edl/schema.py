"""EDL v2 schema: multi-track, keyframed, effects-aware."""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any, Literal, Union
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from . import speed_curve as _speed_curve

# 3 (QA-076): `TextStyle.font` is nullable (None = the role's own font) and a
#   text clip's scalar x/y is always where it renders. v2 overloaded values as
#   "unset" sentinels: font "Inter-Black" (so Inter Black could not be picked)
#   and y = canvas.h·0.85 (add_text's old default — so a typed Y of 918 on a
#   1080p canvas snapped 76 px to the role anchor while 919 did not). A v2 EDL
#   is migrated on load (EDL._migrate_v2) into exactly what it rendered.
EDL_VERSION = 3

# See EDL.hash()'s docstring — this is a render-cache-busting salt, not a
# schema-migration version. Bump on any renderer change that makes the same
# EDL bytes produce different pixels.
# v3: keyframe easing now exports for real (to_ffmpeg_expr used to emit
#     linear for every interp mode) — cached chunks/videos baked pre-fix
#     linear motion for eased keyframes.
# v4: text transform.x/y + style.size/stroke now render (previously ignored)
#     and sticker z-order sorts — unchanged EDL bytes produce different
#     pixels.
# v5: clip video_fade_in/out render; text transform.opacity renders
# v6: v1 honours clip.start — leading offsets, interior gaps and the trailing
#     remainder render as black+silence, so the output is exactly edl.duration
#     long (it used to concat v1 clips from t=0 and drop the gaps). Plain
#     `audio` lanes (a1) are now mixed in too.
# v7: v1 media clips honour a STATIC transform.x/y pan (previously only the
#     keyframed path emitted x/y, and the static path hardcoded a dead-centre
#     crop — so the Properties Position inputs committed and re-rendered but
#     never moved the picture). A pure pan at scale 1 now zooms by the minimum
#     factor needed to expose the offset. Unchanged for every clip whose x/y are
#     both 0, which is the overwhelming majority — but the salt has to move
#     because an existing EDL with a non-zero x/y renders differently now.
# v8: two fixes that change the pixels of an EDL nobody edited.
#     (a) v1 rotation is IN PLACE (`rotate=<rad>:c=black`) instead of expanding
#         to the rotated bounding box and scaling that back down to fit — a 3°
#         straighten used to visibly zoom the whole shot out (30° rendered at
#         57% size), and the live CSS preview, which rotates in place, jumped
#         the moment the value committed.
#     (b) a PIP's input is placed at its timeline position with `-itsoffset`.
#         Without it the overlay stream ran from t=0 and had ENDED before its
#         `enable` window opened, so overlay's eof_action=repeat froze the
#         clip's LAST frame for the whole appearance — a black box whenever the
#         clip ends dark. Any session holding a cached preview/export/chunk of
#         a rotated clip or a PIP at start>0 would otherwise keep being served
#         the pre-fix pixels forever, which reads as "the fix did nothing".
# v9: preview renders get a bounded keyframe interval on EVERY encoder
#     (`compositor._PREVIEW_GOP`), not just the libx264 fallback. The scrubber
#     decodes from the nearest prior keyframe on each paused drag tick, so the
#     GOP sets how smooth scrubbing feels; h264_qsv was emitting 60-frame GOPs
#     and a measured drag painted only 12.4 fps. The pixels are identical —
#     this salt moves because the FILE differs, and without the bump every
#     existing session would keep being served its long-GOP preview from cache
#     and the fix would read as having done nothing.
# 10: every non-v1 lane (text, captions, stickers, PiP picture+audio, music,
#     vo) moved from layout time to the render clock (render/clock.py); a
#     cached video-only mp4 or preview from before has its overlays baked
#     late by the transition overlap and must not be served or remuxed.
# 11: the compositor now xfades with the SEAM TABLE's clamped cost (never more
#     than the shorter neighbour, first record at a stacked cut) and pads the
#     trailing filler out to the LAYOUT end, so a timeline whose overlay lane
#     outlives v1 renders to edl.duration instead of edl.duration − overlap. A
#     cache written under 10 can hold a short file for such an EDL.
# 12: (QA-002/030/038/039) clips are cut by FRAME COUNT on the project grid
#     (half-frame seek pre-roll, fps=, trim=end_frame, sample-exact audio)
#     instead of float -ss/-to, audio/video fades on a speed-changed clip sit
#     in timeline time, and preview renders at canvas.fps. A cached chunk or
#     preview from 11 holds a frame per seam that the timeline does not.
# 13: (QA-037, QA-002 PIP) `Clip.reverse` is rendered (it was ignored, so a
#     cached render of a reversed clip plays forwards) and PIPs are cut and
#     placed by frame count (an off-grid PIP rendered a frame late).
# 14: (QA-076/078/075) text x/y and font have no sentinels (a value renders
#     where it says), text can carry a background box, alignment, line
#     spacing, a shadow override and its own animation length, and a captions
#     track's `position` moves its cues — the same EDL can bake different text.
# 15: (wave-B review) mixed-script text is shaped per script run with a
#     per-run face (a Devanagari line in a Latin-dominant caption drew .notdef
#     boxes; "Hello नमस्ते" was shaped as Latin), and the music duck key is
#     gated on the raw key's absolute level (room tone ducked the bed's head).
# 16: (Wave D frame-map goldens) xfade inputs share a 1/R clock instead of
#     AVTB, so a seam whose left side had been through µs roundings no longer
#     starts one frame late (the picture ran a frame longer than the sound).
# 17: (Wave D, lane S1) a speed CURVE renders (it rendered at 1x and filled
#     its source length of timeline) and `Clip.freeze` holds one frame: a
#     cached chunk or preview of a curve clip shows the wrong frames.
# 18: (Wave D, review RD2) keyframed transform/opacity on a v1 clip are
#     evaluated at clip-local TIMELINE seconds (the clip's retime of the
#     chain's `t`), not `t - start`: a clip not at 0, or retimed, animates
#     differently.
# 19: (wave D3, E2) a PIP (v2+) clip's speed, curve and freeze are rendered —
#     picture and sound, on its effective_duration window (they were ignored).
# 20: (wave D3, E1b) a speed-CURVE clip's chain runs on the file clock anchored at `in`
#     (edl/speed_curve.py): a split/cut/trim piece exports its parent's frames.
# 21: (wave D3, E1a) anamorphic sources fit by their displayed shape; keyframed v1 geometry runs on the output grid (+x right, zoom about the centre); v1 pans scale with the output size.
# 22: (wave D3, review RD3) a reversed clip keeps its footprint (odd 2x/4x ties), a transition after a retimed clip is applied, keyed PiP opacity/rotation animate, overlay/text step keys switch on their frame, a static opacity keeps an odd pan, a render with no picture fails.
RENDER_BEHAVIOR_VERSION = 22

# A keyframed value is either a scalar or a list of [time, value] pairs with an interp.
KeyframeList = list[tuple[float, float]]
Interp = Literal["linear", "ease-in", "ease-out", "ease-in-out", "step", "back-out", "bounce"]


class _EDLModel(BaseModel):
    """Base for every node in the EDL tree.

    `validate_assignment=True` is the load-bearing setting here. The dispatch
    handlers mutate the tree by direct attribute assignment — `cap.config.style
    = style` in `add_caption_track`, `setattr(obj, key, value)` in
    `set_property` — and **Pydantic v2 does not validate on assignment** unless
    told to. So an out-of-domain value (QA round 5, VAI-01: `style='karaoke'`)
    used to land in the tree, get serialised to `edl.json`, and only surface as
    a ValidationError on the NEXT load — by which point every retained snapshot
    was poisoned too and the session opened with zero clips.

    Constructing a model has always validated, which is why the four other
    enum-constrained tools (`add_mask`, `add_super_text`, `add_keyframe`,
    `set_aspect_ratio`) were already safe: they build a new model instead of
    assigning to an existing one. This closes the assignment half.

    Measured cost: ~0.5 µs vs ~0.14 µs per assignment.
    """

    model_config = ConfigDict(validate_assignment=True)


def _finite(v: float, field: str) -> float:
    if not math.isfinite(v):
        raise ValueError(f"{field} must be a finite number, got {v!r}")
    return v


class Keyframe(_EDLModel):
    keyframes: KeyframeList
    interp: Interp = "linear"


# A property can be a number or a Keyframe.
KFNum = Union[float, Keyframe]


# Domain bounds for Transform. Deliberately generous: they exist to stop
# nonsense reaching the renderer (opacity 5.0 — QA round 5 VAI-08 — scale 0,
# NaN), not to second-guess a deliberate extreme.
#
# Out-of-range values are CLAMPED, not rejected. Rejecting would make an EDL
# that already holds one unloadable, which is precisely the class of total-loss
# failure this round is fixing. Non-finite values ARE rejected: there is no
# sensible clamp for NaN, and nothing in the codebase computes a transform
# (they come from UI numbers and tool args), so one can only arrive from a
# caller the new assignment validation now blocks at the source.
_OPACITY_RANGE = (0.0, 1.0)
_SCALE_RANGE = (0.01, 100.0)
_ROTATION_RANGE = (-3600.0, 3600.0)  # ±10 full turns; keyframed spins fit easily
_POSITION_RANGE = (-100_000.0, 100_000.0)


def _clamp_kfnum(v: KFNum, lo: float, hi: float, field: str) -> KFNum:
    """Clamp a scalar-or-keyframed transform property into [lo, hi].

    Applies to BOTH shapes of `KFNum` — a bound expressed as
    `Field(ge=…, le=…)` cannot, because pydantic cannot attach a numeric
    constraint to a `float | Keyframe` union.
    """
    if isinstance(v, Keyframe):
        clamped = [(float(t), min(hi, max(lo, _finite(float(val), field))))
                   for t, val in v.keyframes]
        if clamped != list(v.keyframes):
            return Keyframe(keyframes=clamped, interp=v.interp)
        return v
    return min(hi, max(lo, _finite(float(v), field)))


class Transform(_EDLModel):
    x: KFNum = 0.0
    y: KFNum = 0.0
    scale: KFNum = 1.0
    rotation: KFNum = 0.0
    opacity: KFNum = 1.0

    @field_validator("x", "y")
    @classmethod
    def _check_position(cls, v: KFNum) -> KFNum:
        return _clamp_kfnum(v, *_POSITION_RANGE, "position")

    @field_validator("scale")
    @classmethod
    def _check_scale(cls, v: KFNum) -> KFNum:
        return _clamp_kfnum(v, *_SCALE_RANGE, "scale")

    @field_validator("rotation")
    @classmethod
    def _check_rotation(cls, v: KFNum) -> KFNum:
        return _clamp_kfnum(v, *_ROTATION_RANGE, "rotation")

    @field_validator("opacity")
    @classmethod
    def _check_opacity(cls, v: KFNum) -> KFNum:
        return _clamp_kfnum(v, *_OPACITY_RANGE, "opacity")


#: Clip gain bounds — the same physical range set_volume's schema declares.
GAIN_DB_RANGE = (-96.0, 24.0)
#: Longest timeline/source time a clip may address (6 h) — the same cap
#: agent/tools.TIMELINE_MAX_SECONDS puts on every time argument (QA-041).
#: The model clamps too, because `set_property` reaches Clip.start/in/out
#: and AudioProps.gain_db without going through a typed tool's bounds.
TIME_RANGE = (0.0, 6 * 3600.0)
#: Scalar playback-speed bounds (set_speed's `factor`).
SPEED_RANGE = (0.1, 100.0)
#: Text size / outline bounds in canvas px (add_text's `size` / `stroke_w`).
#: Unbounded, 1e6 made ImageFont.truetype raise "invalid pixel size" and a
#: 1e5 outline overflowed freetype's rasteriser — every later render 500'd.
TEXT_SIZE_RANGE = (1.0, 2000.0)
TEXT_STROKE_RANGE = (0.0, 200.0)
#: Letter spacing (tracking) bounds in canvas px (QA-078). Negative tightens.
TEXT_LETTER_SPACING_RANGE = (-20.0, 100.0)


def _clamp_num(v: float, rng: tuple[float, float], field: str) -> float:
    return min(rng[1], max(rng[0], _finite(float(v), field)))


class AudioProps(_EDLModel):
    gain_db: float = 0.0
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    # Volume automation (QA-086): dB OFFSETS keyed in clip-local TIMELINE
    # seconds (the fades' clock), ADDED to `gain_db` — so a clip-gain trim
    # moves the whole curve and keeps its shape, the clip-gain + rubber-band
    # model every NLE uses. `add_keyframe prop="audio.gain_db"` speaks the
    # absolute level and stores `level − gain_db`. None = no automation.
    gain_env: Keyframe | None = None
    # How a speed change treats the sound (QA-039 residual): True time-
    # stretches at the original pitch (atempo — WSOLA moves transients up to
    # ±12 ms), False is varispeed (asetrate: sample-exact, pitch follows the
    # speed like tape).
    keep_pitch: bool = True
    # Channel mode (QA-122): "stereo" plays the source as it is; "left" /
    # "right" put that one channel on both sides (a lav into one camera
    # input); "mono" folds both channels to the middle. Rendered by
    # `render/audio_mix.channel_filter` on every lane, drawn by the waveform.
    channels: Literal["stereo", "left", "right", "mono"] = "stereo"

    @field_validator("gain_db")
    @classmethod
    def _check_gain(cls, v: float) -> float:
        return min(GAIN_DB_RANGE[1], max(GAIN_DB_RANGE[0], _finite(float(v), "gain_db")))


class Effect(_EDLModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class Mask(_EDLModel):
    # "rounded" was added for PIP shapes (render/pip.py cuts circle/rounded
    # procedurally). Widening a Literal is backward-compatible: every EDL that
    # already validates still does.
    type: Literal["linear", "mirror", "circle", "rectangle", "rounded", "heart", "star"]
    feather: float = 0.0
    angle: float = 0.0
    position: tuple[float, float] = (540.0, 960.0)
    invert: bool = False


class Framing(_EDLModel):
    """Pan/zoom/rotation of the picture inside a PIP's shape. See Clip.framing.

    `rotation` spins the PICTURE within the shape and leaves the shape itself
    alone — the circle stays a circle sitting where it was, and the footage
    turns inside it. That is a different control from `Transform.rotation`,
    which turns the whole element (shape included) on the canvas, and the two
    compose: a PIP can sit at 20° on the canvas with its picture levelled at
    -20° inside.
    """
    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0
    rotation: float = 0.0

    @field_validator("x", "y")
    @classmethod
    def _clamp_offset(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("framing offset must be finite")
        return max(-1.0, min(1.0, float(v)))

    @field_validator("rotation")
    @classmethod
    def _clamp_rotation(cls, v: float) -> float:
        # Bounds live on the model, not the handler, for the reason the module
        # header gives: set_pip_framing is not the only writer.
        if not math.isfinite(v):
            raise ValueError("framing rotation must be finite")
        return max(-180.0, min(180.0, float(v)))

    @field_validator("zoom")
    @classmethod
    def _clamp_zoom(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("framing zoom must be finite")
        # Below 1 there is less source than box and the crop would run off the
        # edge into black — the same failure the v1 cover path clamps for.
        return max(1.0, min(10.0, float(v)))


class ChromaKey(_EDLModel):
    color: str = "#00FF00"
    similarity: float = 0.4
    smoothness: float = 0.1
    spill_suppress: float = 0.5


class Clip(_EDLModel):
    id: str = Field(default_factory=lambda: f"c_{uuid4().hex[:8]}")
    src: str
    in_: float = Field(0.0, alias="in")
    out: float = 0.0
    start: float = 0.0
    transform: Transform = Field(default_factory=Transform)
    # A number (constant speed), None (1x), or a CURVE {"curve": [[x, r], ...]}
    # — x a position over the clip's OUTPUT, 0..1, r the speed there
    # (0.1-10x), piecewise linear (`edl/speed_curve.py`, normalised on
    # validation). The clip's timeline footprint is the curve's integral.
    speed: float | dict | None = None
    reverse: bool = False
    # FREEZE FRAME (CapCut "Freeze"): when set, the clip is a STILL — it holds
    # ONE frame of `src` for `freeze` timeline seconds, and its sound is
    # silence. The held frame is the one a 1x clip starting at `in` shows
    # first (`render/frame_map.freeze_frame`), so the preview draws it from
    # the source's own proxy, index for index, with no new image file. `out`
    # is not read by any render (keep it `in` + one frame); `speed` and
    # `reverse` do not apply and are cleared. None (the default, omitted from
    # the JSON) = an ordinary clip.
    # Omitted from the JSON while unset (`exclude_if`, pydantic >= 2.11 — the
    # lock pins 2.13), so an EDL written before the field existed serialises,
    # hashes and renders exactly as it did.
    freeze: float | None = Field(None, exclude_if=lambda v: v is None)
    # Visual fade-from/to-black on the clip's VIDEO, in clip-local TIMELINE
    # seconds (same time convention as audio.fade_in/out since QA-038 — a 1s
    # fade on a 2x clip lasts 1s on screen). Deliberately TOP-LEVEL fields,
    # NOT inside AudioProps: compositor._video_only_fingerprint pops each
    # clip's "audio" key (audio props never change pixels), so a video-fade
    # change must live outside it to invalidate the cached video-only mp4 —
    # which top-level fields do automatically since it dumps whole clips.
    video_fade_in: float = 0.0
    video_fade_out: float = 0.0
    # How to reconcile a source whose aspect ratio differs from the canvas:
    #   "contain" — scale down to fit, pad black (letterbox). The historical and
    #               default behaviour, so existing EDLs render byte-identically.
    #   "cover"   — scale up to fill, crop the overflow. This is the "crop" the
    #               tester asked for; combined with transform.scale/x/y it gives
    #               a full manual reframe without a separate crop-rect field
    #               (which would have needed its own canvas-resize rescaling
    #               rules, exactly like the overlay x/y trap in CLAUDE.md).
    # Top-level, NOT inside `audio`, for the same fingerprint reason as the
    # video fades above: it changes pixels, so it must invalidate the cached
    # video-only mp4.
    fit: Literal["contain", "cover"] = "contain"
    # Framing INSIDE a PIP's shape — which part of the source appears in the
    # circle/rounded/cropped box, and how far zoomed in.
    #
    # A separate field from `transform` because the two answer different
    # questions and a PIP needs both at once: transform.x/y/scale place and size
    # the element ON THE CANVAS, while this pans and zooms the picture WITHIN
    # that element. Reusing transform for the second job would make moving a PIP
    # and reframing it the same control ("if i chose circle i should be able to
    # frame the pip in the circle").
    #
    # `x`/`y` are NORMALISED (-1..1) offsets of the crop window inside the
    # covered source: 0 is centred, -1/+1 push it to the edges, and the render
    # clamps to whatever margin the crop actually has, so a value with no room
    # to move is a no-op rather than a black edge. Normalised rather than pixels
    # so a canvas resize needs no rescaling pass — the trap the overlay x/y
    # fields document in CLAUDE.md.
    framing: Framing | None = None
    effects: list[Effect] = Field(default_factory=list)
    mask: Mask | None = None
    chromakey: ChromaKey | None = None
    audio: AudioProps = Field(default_factory=AudioProps)
    matte_src: str | None = None
    track_to: str | None = None  # motion-tracking target id

    model_config = ConfigDict(populate_by_name=True, validate_assignment=True)

    @field_validator("in_", "out", "start")
    @classmethod
    def _check_time(cls, v: float, info: ValidationInfo) -> float:
        # QA-041: clamped into [0, 6 h] like every time argument; non-finite
        # raises. Clamping (not rejecting) keeps an EDL that already holds a
        # bad value loadable — set_property rejects before the clamp.
        return _clamp_num(v, TIME_RANGE, info.field_name or "time")

    @field_validator("speed")
    @classmethod
    def _check_speed(cls, v: float | dict | None) -> float | dict | None:
        if v is None:
            return v
        if isinstance(v, dict):
            # A curve is normalised (sorted, clamped, ends pinned); a dict
            # with no usable curve means 1x — what it always rendered as.
            return _speed_curve.normalize_curve(v)
        f = _finite(float(v), "speed")
        # <= 0 has always meant "normal speed" (speed_factor); store it as such.
        return None if f <= 0 else min(SPEED_RANGE[1], max(SPEED_RANGE[0], f))

    @field_validator("freeze")
    @classmethod
    def _check_freeze(cls, v: float | None) -> float | None:
        if v is None:
            return None
        f = _finite(float(v), "freeze")
        return None if f <= 0 else min(TIME_RANGE[1], f)

    @model_validator(mode="after")
    def _freeze_is_a_still(self) -> "Clip":
        # A still has no speed and no direction. Cleared here (not rejected)
        # so every consumer — render, program map, UI — sees one meaning.
        # object.__setattr__: this runs on assignment too, and must not
        # re-enter validation.
        if self.freeze is not None:
            if self.speed is not None:
                object.__setattr__(self, "speed", None)
            if self.reverse:
                object.__setattr__(self, "reverse", False)
        return self

    @property
    def duration(self) -> float:
        """SOURCE seconds consumed (out - in). NOT timeline time when speed
        != 1 — use effective_duration for timeline math."""
        return max(0.0, self.out - self.in_)

    @property
    def speed_curve(self) -> list[tuple[float, float]] | None:
        """The curve's points when `speed` is a curve, else None."""
        return _speed_curve.curve_points(self.speed)

    @property
    def speed_factor(self) -> float:
        """MEAN speed: source seconds per timeline second. The scalar for a
        constant speed, 1.0 unset; a curve's mean (its footprint is
        `duration / mean`); a freeze's `duration / freeze` (1.0 when it
        consumes no source). Linear callers (`agent/timemap`, trim/split math)
        are therefore exact at a curve clip's EDGES only — inside one, use
        `source_offset_at` / `timeline_offset_at`."""
        if self.freeze is not None:
            d = self.duration
            return d / self.freeze if d > 0 else 1.0
        if isinstance(self.speed, (int, float)) and self.speed > 0:
            return float(self.speed)
        pts = self.speed_curve
        if pts is not None:
            return _speed_curve.mean_speed(pts)
        return 1.0

    @property
    def effective_duration(self) -> float:
        """TIMELINE seconds this clip occupies: source duration / speed.
        A 10s source at 2x fills 5s of timeline — this is what
        recompute_duration, ripple math, and the timeline draw must use;
        `duration` alone silently assumed speed=1 everywhere (so speeding a
        clip never changed the transport total or clip widths). A curve fills
        its integral (`duration / mean speed`, edl/speed_curve.py); a freeze
        fills `freeze` seconds. The frame count is `frame_of` of this, like
        any speed (`compositor.clip_frames`)."""
        if self.freeze is not None:
            return float(self.freeze)
        pts = self.speed_curve
        if pts is not None:
            return self.duration / _speed_curve.mean_speed(pts)
        return self.duration / self.speed_factor

    def source_offset_at(self, t: float) -> float:
        """Source seconds past `in` shown at clip-local TIMELINE seconds `t`
        (exact for a curve: its integral; 0 for a freeze)."""
        if self.freeze is not None:
            return 0.0
        pts = self.speed_curve
        if pts is not None:
            cm = _speed_curve.curve_map(pts, self.duration)
            return _speed_curve.source_seconds(cm, t) if cm else 0.0
        return max(0.0, t) * self.speed_factor

    def timeline_offset_at(self, s: float) -> float:
        """Clip-local TIMELINE seconds at which source second `in + s`
        plays (the inverse of `source_offset_at`; 0 for a freeze)."""
        if self.freeze is not None:
            return 0.0
        pts = self.speed_curve
        if pts is not None:
            cm = _speed_curve.curve_map(pts, self.duration)
            return _speed_curve.out_seconds(cm, max(0.0, s)) if cm else 0.0
        return max(0.0, s) / self.speed_factor


class TextStyle(_EDLModel):
    # None = the role's own font (QA-076). Was the string "Inter-Black" doubling
    # as "unset", which made Inter Black itself unselectable.
    font: str | None = None
    size: float = 96
    color: str = "#FFFFFF"
    stroke: str = "#000000"
    stroke_w: float = 4
    shadow: tuple[float, float, float, str] | None = (4, 4, 16, "#000000AA")
    # ALL-CAPS, tri-state: None = use the role's own default (super/hook are
    # capitalised as a house style; every other role is not). True/False is an
    # explicit choice that overrides it.
    #
    # Reported as "Text layer only shows capital alphabets and doesn't support
    # the small alphabets": the caps rule lived ONLY in the two renderers'
    # role tables, with nothing in the schema to override it, so a lowercase
    # hook or super was unreachable — typing one silently produced caps in both
    # the preview and the export.
    #
    # `None` rather than a `False` default deliberately: every other override on
    # this model has to use a sentinel value because its schema default is
    # indistinguishable from "never touched" (see resolve_size_override's known
    # limitation). A fresh nullable field needs no sentinel, so "unset" and
    # "explicitly lowercase" stay distinguishable — which matters here, since
    # defaulting to False would have retroactively un-capitalised every existing
    # hook and super in every saved project.
    upper: bool | None = None
    # ---- QA-078: the text tool's basics, drawn by BOTH renderers through the
    # shared layout model (render/text_overlay.py ↔ frontend lib/textLayout.ts).
    # A filled box behind the whole block, "#RRGGBB" or "#RRGGBBAA"; None = no box.
    background: str | None = None
    # Horizontal alignment of the lines inside the block (the block itself is
    # still centred on the anchor x).
    align: Literal["left", "center", "right"] = "center"
    # Line height multiplier on the model's LINE_HEIGHT_RATIO (1 = unchanged).
    line_spacing: float = 1.0
    # Drop shadow: None = the role's own choice, True/False = explicit.
    shadow_on: bool | None = None
    # Letter spacing (tracking) in canvas px added after every grapheme of a
    # simple-script run, never inside a complex-script run (Devanagari,
    # Arabic, … — spacing their clusters apart breaks the joins and the
    # headline). Rule 8 of the shared layout model; 0 = off.
    letter_spacing: float = 0.0

    @field_validator("background")
    @classmethod
    def _check_background(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        import re as _re
        if not _re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", v):
            raise ValueError(f"background must be #RRGGBB or #RRGGBBAA, got {v!r}")
        return v

    @field_validator("line_spacing")
    @classmethod
    def _clamp_line_spacing(cls, v: float) -> float:
        return min(3.0, max(0.5, _finite(float(v), "line_spacing")))

    @field_validator("letter_spacing")
    @classmethod
    def _clamp_letter_spacing(cls, v: float) -> float:
        return _clamp_num(v, TEXT_LETTER_SPACING_RANGE, "letter_spacing")

    @field_validator("size")
    @classmethod
    def _clamp_size(cls, v: float) -> float:
        return _clamp_num(v, TEXT_SIZE_RANGE, "size")

    @field_validator("stroke_w")
    @classmethod
    def _clamp_stroke_w(cls, v: float) -> float:
        return _clamp_num(v, TEXT_STROKE_RANGE, "stroke_w")


class TextClip(_EDLModel):
    id: str = Field(default_factory=lambda: f"t_{uuid4().hex[:8]}")
    text: str
    start: float
    end: float
    style: TextStyle = Field(default_factory=TextStyle)
    transform: Transform = Field(default_factory=lambda: Transform(x=540, y=1700))
    anim_in: str | None = None
    anim_out: str | None = None
    # QA-078: seconds each in/out animation lasts; None = the house 0.35 s.
    # Still capped at 40 % of the clip by both renderers.
    anim_dur: float | None = None
    role: Literal["super", "hook", "lower_third", "caption", "label", "watermark"] | None = None
    speaker: str | None = None  # for lower-thirds attached to a speaker

    @field_validator("anim_dur")
    @classmethod
    def _clamp_anim_dur(cls, v: float | None) -> float | None:
        # Same range as text_overlay.ANIM_DUR_RANGE / lib/textLayout.
        return None if v is None else min(3.0, max(0.1, _finite(float(v), "anim_dur")))


class Sticker(_EDLModel):
    """Image overlay clip: PNG (or fetched emoji) composited on the canvas."""
    id: str = Field(default_factory=lambda: f"st_{uuid4().hex[:8]}")
    src: str   # absolute path to the PNG
    start: float
    end: float
    transform: Transform = Field(default_factory=Transform)
    # Per-clip stacking order WITHIN the sticker track (set_clip_z). Higher
    # composites on top; ties fall back to the legacy start-order (later start
    # wins). 0 = legacy default, so pre-existing EDLs render unchanged.
    z: int = 0
    label: str | None = None  # for emoji stickers, the original character


class Transition(_EDLModel):
    # `type` is any name in render/transitions.py (NATIVE + ALIASES + custom).
    # Kept as a plain str instead of a Literal so the ~45-name catalog can grow
    # without touching the schema; the renderer resolves unknowns to `fade`
    # rather than crashing, and add_transition validates with a helpful error.
    at: float
    type: str = "fade"
    duration: float = 0.5

    @field_validator("duration")
    @classmethod
    def _duration_is_what_renders(cls, v: float, info: ValidationInfo) -> float:
        """A stored duration below the renderer's floor is the transition's
        own default — the SAME number the compositor has always used
        (`render.transitions.effective_duration`), written into the data so
        every reader sees it.

        WHY IN THE DATA, not at render time: the compositor resolved a stored
        0.0 / 0.05 to the default while `v1_seam_table()` (hence
        `edl.duration`, `render/clock.py`, the desktop's `seamTable`, the
        mobile strip and the benchmark's own copy of the rule) charged the
        raw number. Measured: `Transition(at=2.0, duration=0.05)` between two
        2 s clips rendered a 3.5 s file (xfade 0.5) while the transport read
        3.95 and every overlay after the seam sat 0.45 s late — the drift the
        render clock exists to remove, re-created by one record. Reachable
        through dispatch/MCP before `add_transition` floored it; still
        reachable by a direct edit of `v1.transitions` or a legacy edl.json,
        which is why the schema owns it. A FIELD validator (not a model
        `before` hook) because `_EDLModel` validates assignment and only
        field validators have their return value applied on `tr.duration =
        0.02` — measured: a before-model validator ran on assignment but its
        normalised dict was discarded. `info.data` carries `type` (declared
        above this field) on construction, JSON load and assignment alike.

        Lazy import: render/ imports this module, so the dependency cannot be
        module-level; by the time a Transition is validated `edl.schema` is
        fully loaded and the cycle is closed. `render/transitions.py` itself
        imports only `logging`.
        """
        from ..render.transitions import effective_duration
        return effective_duration(str(info.data.get("type", "fade")), v)


class CaptionLook(_EDLModel):
    """The captions track's LOOK (QA-075): set once with `set_caption_style`,
    applied to every cue, and re-applied to cues a later caption build lays
    down. Each field None = the caption role's own style."""
    font: str | None = None
    color: str | None = None
    size: float | None = None
    stroke: str | None = None
    stroke_w: float | None = None
    background: str | None = None
    shadow_on: bool | None = None
    upper: bool | None = None

    @field_validator("size")
    @classmethod
    def _clamp_size(cls, v: float | None) -> float | None:
        return None if v is None else _clamp_num(v, TEXT_SIZE_RANGE, "size")

    @field_validator("stroke_w")
    @classmethod
    def _clamp_stroke_w(cls, v: float | None) -> float | None:
        return None if v is None else _clamp_num(v, TEXT_STROKE_RANGE, "stroke_w")


class CaptionsConfig(_EDLModel):
    enabled: bool = False
    style: Literal["default", "ig_chunky", "word_emphasis"] = "default"
    # Where the cues sit — rendered by both renderers since QA-075
    # (text_overlay.caption_position_y / lib/textLayout.captionAnchorY).
    position: Literal["bottom", "center", "top"] = "bottom"
    lang: str | None = None
    look: CaptionLook | None = None


class MusicDuck(_EDLModel):
    to_db: float = -18.0
    track_ref: str = "a1"


class Track(_EDLModel):
    id: str
    type: Literal["video", "audio", "music", "vo", "text", "sticker", "effect", "captions"]
    z: int = 0
    clips: list[Clip | TextClip | Sticker] = Field(default_factory=list)
    duck: MusicDuck | None = None
    config: CaptionsConfig | None = None  # captions track only
    transitions: list[Transition] = Field(default_factory=list)
    label: str | None = None
    muted: bool = False  # render skips this track if true
    locked: bool = False
    # Solo (QA-086): while ANY track is soloed, only soloed tracks are heard —
    # an audio-only rule (a soloed-out video lane keeps its picture).
    solo: bool = False


# Canvas bounds. The lower bound is not cosmetic: `set_canvas {w:0, h:-10}`
# was accepted verbatim (QA round 5, VAI-05) and every downstream consumer —
# scale/pad filters, overlay rescaling, the preview's aspect box — divides by
# these. 7680 is 8K, well past anything this app renders.
CANVAS_MIN, CANVAS_MAX = 16, 7680
FPS_MIN, FPS_MAX = 1, 240


class Canvas(_EDLModel):
    w: int = 1080
    h: int = 1920
    # The project timebase (QA-009). A float so broadcast rates are
    # representable (29.97002997… = 30000/1001); the validator snaps every value
    # through `edl.timebase.fps_float`, so 29.97, 29.97002997 and "30000/1001"
    # are one rate. Integer rates are stored as ints so an EDL written before
    # this change serialises — and therefore hashes — byte-identically.
    fps: int | float = 30
    bg: str = "#000000"

    @field_validator("w", "h")
    @classmethod
    def _check_dimension(cls, v: int) -> int:
        # Snapped even because H.264 chroma subsampling requires it and the
        # round-4 letterbox parity math already assumes it; clamped rather than
        # rejected for the same reason as Transform — an EDL that already holds
        # a bad value must stay loadable.
        v = min(CANVAS_MAX, max(CANVAS_MIN, int(v)))
        return v - (v % 2)

    @field_validator("fps", mode="before")
    @classmethod
    def _check_fps(cls, v: object) -> int | float:
        from . import timebase as _tb
        if isinstance(v, bool):
            raise ValueError("fps must be a number")
        try:
            raw = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            if isinstance(v, str) and "/" in v:
                raw = _tb.fps_float(v)
            else:
                raise ValueError(f"fps must be a number, got {v!r}") from None
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise ValueError("fps must be finite")
        f = _tb.fps_float(min(FPS_MAX, max(FPS_MIN, raw)))
        return int(f) if f.is_integer() else f
    # Audio loudness target for export (LUFS). Reels/TikTok target is -16; -14
    # for YouTube. None = skip the loudnorm pass.
    loudness_lufs: float | None = -16.0
    # Export bitrate hint (kbps); compositor uses default if None.
    bitrate_kbps: int | None = None


class BrandKit(_EDLModel):
    handle: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    end_card: str | None = None  # path to end-card image
    palette: list[str] = Field(default_factory=list)
    font: str | None = None


class Marker(_EDLModel):
    """Visual bookmark on the ruler — labels a moment for quick navigation."""
    id: str = Field(default_factory=lambda: f"mk_{uuid4().hex[:8]}")
    time: float
    label: str = ""
    color: str = "#fbbf24"  # amber — must differ from the playhead red (#ff4d6d, Timeline.tsx)


def _migrate_v2_text(data: dict) -> dict:
    """A v2 EDL dict → v3, rendering exactly as before (QA-076).

    Two v2 sentinels go: `style.font == "Inter-Black"` meant "the role's font"
    and becomes None; a non-caption text clip whose scalar y sat on add_text's
    old no-arg default (canvas.h·0.85) rendered at its ROLE anchor, so that is
    the y it gets. Every other value already rendered where it said."""
    from ..render.text_overlay import _y_for_role   # lazy: render imports this module
    canvas = data.get("canvas") or {}
    cw = int(canvas.get("w", 1080) if isinstance(canvas, dict) else getattr(canvas, "w", 1080))
    ch = int(canvas.get("h", 1920) if isinstance(canvas, dict) else getattr(canvas, "h", 1920))
    tracks = []
    for t in data.get("tracks") or []:
        if not isinstance(t, dict) or t.get("type") not in ("text", "captions"):
            tracks.append(t)
            continue
        clips = []
        for c in t.get("clips") or []:
            if isinstance(c, dict) and "text" in c:
                c = dict(c)
                st = c.get("style")
                if isinstance(st, dict) and st.get("font") == "Inter-Black":
                    c["style"] = {**st, "font": None}
                role = c.get("role") or "default"
                tx = c.get("transform")
                if role != "caption" and isinstance(tx, dict):
                    y = tx.get("y")
                    if isinstance(y, (int, float)) and not isinstance(y, bool) and abs(float(y) - ch * 0.85) < 0.5:
                        c["transform"] = {**tx, "y": _y_for_role(role, None, ch, cw)}
            clips.append(c)
        tracks.append({**t, "clips": clips})
    return {**data, "tracks": tracks, "version": EDL_VERSION}


#: Track fields that never reach a rendered frame or sample (a lane's lock and
#: display name). `EDL.render_hash` leaves them out, and the markers.
NON_RENDER_TRACK_FIELDS = ("locked", "label")


class EDL(_EDLModel):
    version: int = EDL_VERSION

    @model_validator(mode="before")
    @classmethod
    def _migrate_v2(cls, data: Any) -> Any:
        """Loading a v2 project (edl.json, a snapshot, a .vae) upgrades it to
        v3 in memory; the next commit writes v3. A dict with no version is a
        caller building a current EDL, never migrated."""
        if isinstance(data, dict) and isinstance(data.get("version"), int) and data["version"] < 3:
            return _migrate_v2_text(data)
        return data
    duration: float = 0.0
    canvas: Canvas = Field(default_factory=Canvas)
    tracks: list[Track] = Field(default_factory=list)
    brand_kit: BrandKit | None = None
    show_template: str | None = None
    markers: list[Marker] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)

    def hash(self) -> str:
        # RENDER_BEHAVIOR_VERSION is a salt, bumped whenever a code change
        # makes the SAME EDL fields render to DIFFERENT pixels (not just
        # when the schema itself changes) — e.g. the LUT-intensity blend
        # fix, the animated-overlay timing fix, and text-style/z-order
        # support all changed what an unchanged EDL renders to. Without this,
        # `render/compositor.py`'s preview cache (keyed by this hash) would
        # keep serving a pre-fix cached .mp4 for a session that hasn't
        # touched its EDL since before the fix shipped. Mirrors
        # render/chunks.py's own _RENDER_BEHAVIOR_VERSION for the same
        # reason on the per-clip chunk cache.
        canonical = json.dumps(self.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{RENDER_BEHAVIOR_VERSION}|{canonical}".encode()).hexdigest()[:16]

    def render_hash(self) -> str:
        """The key of a RENDER of this EDL (preview files, the supersede
        registry, verify renders): `hash()` without the fields no render
        reads (QA-131).

        `hash()` stays the identity of the EDL itself — the ops log's
        before/after, stale-edit detection and "is this export outdated" all
        need a marker edit to count as an edit. The preview cache must not:
        adding a marker re-rendered a byte-identical preview (49.8 s on a
        12-minute timeline) and left the player blank while it did. The
        frontend's `videoFingerprint` already ignores markers."""
        d = self.model_dump(by_alias=True, mode="json")
        d.pop("markers", None)
        for t in d.get("tracks") or []:
            for k in NON_RENDER_TRACK_FIELDS:
                t.pop(k, None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{RENDER_BEHAVIOR_VERSION}|render|{canonical}".encode()).hexdigest()[:16]

    def get_track(self, track_id: str) -> Track | None:
        for t in self.tracks:
            if t.id == track_id:
                return t
        return None

    def get_clip(self, clip_id: str) -> tuple[Track, Clip | TextClip] | None:
        for t in self.tracks:
            for c in t.clips:
                if c.id == clip_id:
                    return (t, c)
        return None

    def video_extent(self) -> float:
        """Timeline seconds occupied by the V1 video track (0.0 if empty).

        Distinct from `duration`, which is a max over EVERY track — so a
        6-minute music bed makes `duration` 373s even when the video is 29s.
        Callers that mean "how long is the video" must use this: sizing a music
        bed, appending the next upload, or deciding where the timeline ends.

        Uses `effective_duration` (source duration / speed) so a sped-up clip
        reports the timeline length it actually occupies.
        """
        t = self.get_track("v1")
        return max((c.start + c.effective_duration
                    for c in (t.clips if t else []) if isinstance(c, Clip)),
                   default=0.0)

    def v1_seam_table(self) -> list[tuple[float, float]]:
        """The v1 seams the renderer will cross-fade, as `(seam, seconds)` in
        LAYOUT time, ascending: `seam` is the boundary (left clip's start +
        effective duration) and `seconds` is what that xfade removes from the
        output. Empty when there are no applicable transitions.

        This is THE seam-matching rule — `transition_overlap()` sums it and
        `render/clock.py` (the layout→render time map every non-v1 lane is
        positioned through) walks it. It lives here rather than in render/
        because render/ imports this module and the dependency cannot point
        back; a second copy of the rule in the renderer is exactly how the
        overlay lanes drifted for as long as they did.

        Mirrors the renderer's applicability rule exactly, because counting a
        transition it will NOT apply is the same bug pointing the other way:
          * only a boundary between two ADJACENT clips (a gap there becomes a
            black filler segment, and a cross-fade across black is meaningless,
            so the renderer leaves that seam a hard cut);
          * matched to the boundary within the same 0.05s tolerance;
          * one transition per seam (the renderer keys `seg_trans` by segment
            index, so a legacy EDL carrying a stack at one cut still only ever
            renders — and therefore only ever costs — one).
        """
        v1 = self.get_track("v1")
        if not v1 or not v1.transitions:
            return []
        return seam_table_for([c for c in v1.clips if isinstance(c, Clip)],
                              v1.transitions, fps=self.canvas.fps)

    def transition_overlap(self) -> float:
        """Seconds the v1 transitions remove from the rendered timeline.

        An `xfade` PLAYS THE TWO CLIPS AT ONCE for its duration, so every
        transition the renderer applies makes the output that much shorter than
        the clips' geometric extent (compositor.py says so in one line:
        `cur_dur = cur_dur + seg_dur[i] - tdur`). Nothing told the EDL, so the
        timeline, the transport denominator and every "how long is this" caller
        kept reporting the un-shortened length: an 8s timeline split at 2/4/6
        with three 0.5s transitions renders **6.5s**, and playback simply
        stopped with the transport reading 6.50 / 8.00 and a dead tail nobody
        could explain. Reported as "the 8 sec video got stopped at 7 sec".

        The applicability rule itself is `v1_seam_table()` — this is its sum.
        """
        return sum(cost for _seam, cost in self.v1_seam_table())

    def recompute_duration(self) -> None:
        end = 0.0
        for t in self.tracks:
            if t.id == "v1":
                # v1 is ASSEMBLED, not just laid out: `_v1_segments` walks the
                # clips with `cursor = max(cursor, start) + effective_duration`,
                # so two clips that overlap in the EDL are still emitted one
                # after the other and the file comes out longer than the
                # geometric max. Measured: two 4s clips overlapping by 2s
                # report 6.0s and render 8.0s. Only `add_clip` can still create
                # that (move_clip snaps to the first free gap), but Claude and
                # MCP both reach it. For every non-overlapping timeline — which
                # is all of them in practice — this cursor equals the plain max,
                # so nothing moves.
                cursor = 0.0
                for c in sorted((c for c in t.clips if isinstance(c, Clip)),
                                key=lambda c: c.start):
                    cursor = max(cursor, c.start) + c.effective_duration
                end = max(end, cursor)
                continue
            for c in t.clips:
                if isinstance(c, Clip):
                    # Every other lane is placed at an absolute time (music via
                    # adelay, PIP via overlay+itsoffset), so its extent is the
                    # plain maximum.
                    end = max(end, c.start + c.effective_duration)
                else:
                    # TextClip and Sticker both expose `.end`
                    end = max(end, getattr(c, "end", 0.0))
        # What the renderer will actually produce — see transition_overlap().
        # Subtracted from the whole timeline, not just v1's own extent: the
        # output IS the v1 assembly, and every other lane is mixed onto it.
        self.duration = max(0.0, end - self.transition_overlap())


#: Two Transition records within this many seconds of one boundary are "the
#: same cut" — `add_transition` replaces within it, `remove_transition`
#: sweeps within it, and the seam table matches within it. A click on the
#: timeline is not exact arithmetic.
SEAM_MATCH_TOL_S = 0.05
#: A positive gap wider than this between two v1 clips becomes black filler
#: (compositor._GAP_EPS — duplicated because render/ imports this module).
V1_GAP_EPS_S = 0.001


def seam_matching(transitions: list[Transition], boundary: float) -> Transition | None:
    """The FIRST record within `SEAM_MATCH_TOL_S` of `boundary`, or None.

    First, not last: a legacy EDL or an MCP edit can still stack two records
    at one cut, and the compositor's own matcher used to keep iterating
    (last wins) while this table kept the first. Measured: fade 0.2 @ 2.0 +
    fade 0.8 @ 2.03 → the table said 0.2 (edl.duration 3.8, clock pulls B by
    0.2) while the render xfaded 0.8 (a 3.2 s file, overlays 0.6 s late).
    The compositor now asks THIS function for the record, so there is one
    answer.
    """
    return next((tr for tr in transitions
                 if abs(tr.at - boundary) < SEAM_MATCH_TOL_S), None)


def seam_table_for(clips: list[Clip], transitions: list[Transition],
                   fps: float | int | None = None) -> list[tuple[float, float]]:
    """`EDL.v1_seam_table()` for an explicit clip list and transition list —
    the compositor calls this with the very lists it assembles, so the seams
    it xfades and the seams the EDL charges are one computation.

    With `fps` (the project timebase) every cost is a WHOLE number of frames
    (at least one): xfade overlaps the picture by whole frames while
    acrossfade overlaps the sound by the exact seconds it is given, so an
    off-grid cost (0.5 s at 25 fps = 12.5 frames) put the audio half a frame
    ahead of the picture from that seam to the end of the video."""
    if not transitions:
        return []
    ordered = sorted(clips, key=lambda c: c.start)
    seams: list[tuple[float, float]] = []
    for cur, nxt in zip(ordered, ordered[1:]):
        boundary = cur.start + cur.effective_duration
        # A GAP (a positive one) is what makes `_v1_segments` insert black
        # filler and therefore what makes the renderer keep the seam a cut.
        # An OVERLAP is not: `_v1_segments` packs it with `max(cursor,
        # start)` and emits no filler, so the two clips stay adjacent
        # segments and the transition IS applied. Testing `abs(...)` here
        # would let a legacy overlapping pair report a longer timeline than
        # it renders.
        #
        # With `fps` the gap is judged on the FRAME GRID, as `_v1_frame_plan`
        # judges it (review RD3): a filler exists only where the next clip
        # starts at least one whole frame after this one's last frame. A
        # retimed clip's exact end is rarely on the grid (1.5x of 91 frames
        # ends at 5.0222 s, and set_speed ripples the next clip to 5.0333),
        # and the old 1 ms rule called that sub-frame sliver a gap: the
        # transition the user added there was stored, reported, and never
        # applied (no xfade, no blend frames, no transport change).
        # (Seconds-adjacent pairs stay seams whatever the grid says: an
        # off-grid legacy/MCP layout can round a filler frame between them,
        # and the renderer has always cross-faded those — golden fuzz_08.)
        if nxt.start - boundary > V1_GAP_EPS_S:
            if fps is None:
                continue      # a gap → filler → the renderer keeps the cut
            from . import timebase as _tb
            gap_frames = (_tb.frame_of(nxt.start, fps)
                          - _tb.frame_of(cur.start, fps)
                          - max(1, _tb.frame_of(cur.effective_duration, fps)))
            if gap_frames >= 1:
                continue      # a whole-frame gap → filler → a hard cut
        match = seam_matching(transitions, boundary)
        if match is None and nxt.start > boundary:
            # A record placed at the NEXT clip's start (what the UI and
            # add_transition use for a cut) when the two are a sub-frame
            # apart (more than the tolerance only below 20 fps): one seam.
            match = seam_matching(transitions, nxt.start)
        if match:
            # Never claim more than the shorter side can give: xfade cannot
            # overlap further than a clip is long. The compositor xfades with
            # THIS clamped number too — it used to pass the raw record
            # duration to xfade, and ffmpeg then ran the picture ahead of
            # every other lane by (d − clamped) and ended the video stream
            # before the audio (A=2 s, B=0.3 s, fade 0.5: video 1.8 s, audio
            # 2.0 s, edl.duration 2.0).
            shorter = min(cur.effective_duration, nxt.effective_duration)
            cost = max(0.0, min(float(match.duration), shorter))
            if fps is not None and cost > 0.0:
                from . import timebase as _tb
                cost = _tb.time_of(max(1, _tb.frame_of(cost, fps)), fps)
                if cost > shorter + 1e-9:
                    cost = _tb.floor_to_frame(shorter, fps)
            if cost > 0.0:
                seams.append((boundary, cost))
    return seams


def empty_edl(canvas: Canvas | None = None) -> EDL:
    """Empty EDL with the standard track layout pre-created."""
    canvas = canvas or Canvas()
    return EDL(
        canvas=canvas,
        tracks=[
            Track(id="v1", type="video", z=0, label="Main video"),
            Track(id="v2", type="video", z=1, label="PIP / overlay video"),
            Track(id="a1", type="audio", z=0, label="Main audio"),
            Track(id="music", type="music", z=0, label="Music"),
            Track(id="vo", type="vo", z=0, label="Voiceover"),
            Track(id="tx_hook", type="text", z=10, label="Hook"),
            Track(id="tx_super", type="text", z=11, label="Super text"),
            Track(id="tx_lt", type="text", z=12, label="Lower thirds"),
            Track(id="stickers", type="sticker", z=12, label="Stickers"),
            Track(id="captions", type="captions", z=13, config=CaptionsConfig()),
        ],
    )
