"""EDL v2 schema: multi-track, keyframed, effects-aware."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Literal, Union
from uuid import uuid4
from pydantic import BaseModel, Field

EDL_VERSION = 2

# A keyframed value is either a scalar or a list of [time, value] pairs with an interp.
KeyframeList = list[tuple[float, float]]
Interp = Literal["linear", "ease-in", "ease-out", "ease-in-out", "step", "back-out", "bounce"]


class Keyframe(BaseModel):
    keyframes: KeyframeList
    interp: Interp = "linear"


# A property can be a number or a Keyframe.
KFNum = Union[float, Keyframe]


class Transform(BaseModel):
    x: KFNum = 0.0
    y: KFNum = 0.0
    scale: KFNum = 1.0
    rotation: KFNum = 0.0
    opacity: KFNum = 1.0


class AudioProps(BaseModel):
    gain_db: float = 0.0
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0


class Effect(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class Mask(BaseModel):
    type: Literal["linear", "mirror", "circle", "rectangle", "heart", "star"]
    feather: float = 0.0
    angle: float = 0.0
    position: tuple[float, float] = (540.0, 960.0)
    invert: bool = False


class ChromaKey(BaseModel):
    color: str = "#00FF00"
    similarity: float = 0.4
    smoothness: float = 0.1
    spill_suppress: float = 0.5


class Clip(BaseModel):
    id: str = Field(default_factory=lambda: f"c_{uuid4().hex[:8]}")
    src: str
    in_: float = Field(0.0, alias="in")
    out: float = 0.0
    start: float = 0.0
    transform: Transform = Field(default_factory=Transform)
    speed: float | dict | None = None  # number or curve {"curve":[[t,r],...]}
    reverse: bool = False
    effects: list[Effect] = Field(default_factory=list)
    mask: Mask | None = None
    chromakey: ChromaKey | None = None
    audio: AudioProps = Field(default_factory=AudioProps)
    matte_src: str | None = None
    track_to: str | None = None  # motion-tracking target id

    model_config = {"populate_by_name": True}

    @property
    def duration(self) -> float:
        return max(0.0, self.out - self.in_)


class TextStyle(BaseModel):
    font: str = "Inter-Black"
    size: float = 96
    color: str = "#FFFFFF"
    stroke: str = "#000000"
    stroke_w: float = 4
    shadow: tuple[float, float, float, str] | None = (4, 4, 16, "#000000AA")


class TextClip(BaseModel):
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


class Sticker(BaseModel):
    """Image overlay clip: PNG (or fetched emoji) composited on the canvas."""
    id: str = Field(default_factory=lambda: f"st_{uuid4().hex[:8]}")
    src: str   # absolute path to the PNG
    start: float
    end: float
    transform: Transform = Field(default_factory=Transform)
    label: str | None = None  # for emoji stickers, the original character


class Transition(BaseModel):
    at: float
    type: Literal["fade", "dissolve", "slide", "zoom", "glitch", "whip", "spin"] = "fade"
    duration: float = 0.5


class CaptionsConfig(BaseModel):
    enabled: bool = False
    style: Literal["default", "ig_chunky", "word_emphasis"] = "default"
    position: Literal["bottom", "center", "top"] = "bottom"
    lang: str | None = None


class MusicDuck(BaseModel):
    to_db: float = -18.0
    track_ref: str = "a1"


class Track(BaseModel):
    id: str
    type: Literal["video", "audio", "music", "vo", "text", "sticker", "effect", "captions"]
    z: int = 0
    clips: list[Clip | TextClip | Sticker] = Field(default_factory=list)
    duck: MusicDuck | None = None
    config: CaptionsConfig | None = None  # captions track only
    transitions: list[Transition] = Field(default_factory=list)
    label: str | None = None
    muted: bool = False
    locked: bool = False
    muted: bool = False  # render skips this track if true


class Canvas(BaseModel):
    w: int = 1080
    h: int = 1920
    fps: int = 30
    bg: str = "#000000"
    # Audio loudness target for export (LUFS). Reels/TikTok target is -16; -14
    # for YouTube. None = skip the loudnorm pass.
    loudness_lufs: float | None = -16.0
    # Export bitrate hint (kbps); compositor uses default if None.
    bitrate_kbps: int | None = None


class BrandKit(BaseModel):
    handle: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    end_card: str | None = None  # path to end-card image
    palette: list[str] = Field(default_factory=list)
    font: str | None = None


class Marker(BaseModel):
    """Visual bookmark on the ruler — labels a moment for quick navigation."""
    id: str = Field(default_factory=lambda: f"mk_{uuid4().hex[:8]}")
    time: float
    label: str = ""
    color: str = "#ff4d6d"


class EDL(BaseModel):
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
        canonical = json.dumps(self.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

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

    def recompute_duration(self) -> None:
        end = 0.0
        for t in self.tracks:
            for c in t.clips:
                if isinstance(c, Clip):
                    end = max(end, c.start + c.duration)
                else:
                    # TextClip and Sticker both expose `.end`
                    end = max(end, getattr(c, "end", 0.0))
        self.duration = end


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
