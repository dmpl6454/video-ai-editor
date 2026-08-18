"""EDL v2 schema: multi-track, keyframed, effects-aware."""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any, Literal, Union
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator

EDL_VERSION = 2

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
RENDER_BEHAVIOR_VERSION = 9

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


class AudioProps(_EDLModel):
    gain_db: float = 0.0
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0


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
    speed: float | dict | None = None  # number or curve {"curve":[[t,r],...]}
    reverse: bool = False
    # Visual fade-from/to-black on the clip's VIDEO, in clip-local SOURCE
    # seconds (same time convention as audio.fade_in/out — on a 2x clip a 1s
    # fade displays over 0.5s of wall-clock). Deliberately TOP-LEVEL fields,
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

    @property
    def duration(self) -> float:
        """SOURCE seconds consumed (out - in). NOT timeline time when speed
        != 1 — use effective_duration for timeline math."""
        return max(0.0, self.out - self.in_)

    @property
    def speed_factor(self) -> float:
        """Scalar speed, 1.0 for unset/curve dicts (curves render as 1.0
        today; when the compositor learns curves this stays the timeline-
        footprint contract point)."""
        if isinstance(self.speed, (int, float)) and self.speed > 0:
            return float(self.speed)
        return 1.0

    @property
    def effective_duration(self) -> float:
        """TIMELINE seconds this clip occupies: source duration / speed.
        A 10s source at 2x fills 5s of timeline — this is what
        recompute_duration, ripple math, and the timeline draw must use;
        `duration` alone silently assumed speed=1 everywhere (so speeding a
        clip never changed the transport total or clip widths)."""
        return self.duration / self.speed_factor


class TextStyle(_EDLModel):
    font: str = "Inter-Black"
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


class TextClip(_EDLModel):
    id: str = Field(default_factory=lambda: f"t_{uuid4().hex[:8]}")
    text: str
    start: float
    end: float
    style: TextStyle = Field(default_factory=TextStyle)
    transform: Transform = Field(default_factory=lambda: Transform(x=540, y=1700))
    anim_in: str | None = None
    anim_out: str | None = None
    role: Literal["super", "hook", "lower_third", "caption", "label", "watermark"] | None = None
    speaker: str | None = None  # for lower-thirds attached to a speaker


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


class CaptionsConfig(_EDLModel):
    enabled: bool = False
    style: Literal["default", "ig_chunky", "word_emphasis"] = "default"
    position: Literal["bottom", "center", "top"] = "bottom"
    lang: str | None = None


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


# Canvas bounds. The lower bound is not cosmetic: `set_canvas {w:0, h:-10}`
# was accepted verbatim (QA round 5, VAI-05) and every downstream consumer —
# scale/pad filters, overlay rescaling, the preview's aspect box — divides by
# these. 7680 is 8K, well past anything this app renders.
CANVAS_MIN, CANVAS_MAX = 16, 7680
FPS_MIN, FPS_MAX = 1, 240


class Canvas(_EDLModel):
    w: int = 1080
    h: int = 1920
    fps: int = 30
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

    @field_validator("fps")
    @classmethod
    def _check_fps(cls, v: int) -> int:
        return min(FPS_MAX, max(FPS_MIN, int(v)))
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


class EDL(_EDLModel):
    version: int = EDL_VERSION
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
            return 0.0
        clips = sorted((c for c in v1.clips if isinstance(c, Clip)),
                       key=lambda c: c.start)
        total = 0.0
        for cur, nxt in zip(clips, clips[1:]):
            boundary = cur.start + cur.effective_duration
            # A GAP (a positive one) is what makes `_v1_segments` insert black
            # filler and therefore what makes the renderer keep the seam a cut.
            # An OVERLAP is not: `_v1_segments` packs it with `max(cursor,
            # start)` and emits no filler, so the two clips stay adjacent
            # segments and the transition IS applied. Testing `abs(...)` here
            # would let a legacy overlapping pair report a longer timeline than
            # it renders. Same 1ms tolerance as compositor._GAP_EPS, duplicated
            # rather than imported because render/ imports this module and the
            # dependency cannot point back.
            if nxt.start - boundary > 0.001:
                continue          # a gap → filler → the renderer keeps the cut
            match = next((tr for tr in v1.transitions
                          if abs(tr.at - boundary) < 0.05), None)
            if match:
                # Never claim more than the shorter side can give: xfade cannot
                # overlap further than a clip is long.
                total += max(0.0, min(float(match.duration),
                                      cur.effective_duration,
                                      nxt.effective_duration))
        return total

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
