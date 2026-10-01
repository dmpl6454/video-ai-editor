"""The frozen shapes of the Editor Brain (spec §3.1-3.3, §5.1; EB1 brief
"Frozen contracts"): the Content Graph header, its layers, scenes and the
angle group, and the Edit Decision Plan with its decisions.

Rules every model here follows:
  * `extra="forbid"` — an unknown key is a refusal, never silently kept;
  * times are reference seconds rounded to 4 decimals on validation and
    monotone (`t0 < t1`; a word may be zero-length);
  * ids are prefixed by kind and referenced ids must exist within the file
    (`word.sent` → a sentence, `repeats.of` → a sentence);
  * `canonical_json` / `digest` are key-order independent and 4-decimal, so
    two planners writing the same decisions write the same bytes;
  * `MAX_LAYER_BYTES` / `MAX_GRAPH_BYTES` are the caps `store.py` enforces.

`reason.facts` MUST resolve to ids in the graph the EDP names:
`graph_ids()` collects them from a loaded graph, `check_facts()` refuses.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..agent.prompt.schema import PLAN_REF_PATTERN, BrainId

MAX_LAYER_BYTES = 32 * 1024 * 1024
MAX_GRAPH_BYTES = 64 * 1024 * 1024
TIME_DECIMALS = 4

SRC_KEY_RE = re.compile(r"^(src_)?[0-9a-f]{24}$")
GRAPH_ID_RE = r"^g_[0-9a-f]{12}$"
EDP_ID_RE = PLAN_REF_PATTERN            # ONE pattern for the id a plan step names (`agent/prompt/schema`)
DECISION_ID_RE = r"^k_[0-9]{4}$"
PLAN_ID_RE = r"^p_[0-9a-f]{8}$"

DECISION_KINDS = ("keep_window", "cut_range", "keep_pause", "open_on", "switch_angle", "punch_in",
                  "jump_cut_hide", "captions", "music", "reframe", "export_preset", "dialogue")
DecisionKind = Literal["keep_window", "cut_range", "keep_pause", "open_on", "switch_angle", "punch_in",
                       "jump_cut_hide", "captions", "music", "reframe", "export_preset", "dialogue"]

#: The closed reason-code vocabulary of this wave (spec §5.2 templates live
#: in brain/reasons.py, lane E). `pause_kept:{why}` is the one parametric code.
REASON_CODES: frozenset[str] = frozenset({
    "silence", "filler", "filler_acoustic", "false_start", "dead_air", "best_window", "duration_fit",
    "hook_strongest_opening", "speaker_turn", "at_cut", "jump_cut_hide", "emphasis_peak", "hook_emphasis",
    "caption_mode", "music_mood", "dialogue_lane", "control",
    "repeat",          # EB1 integration: spec §5.2 row; a retake's duplicate (lane E's request)
})
_PAUSE_KEPT_RE = re.compile(r"^pause_kept:[a-z_]{2,32}$")

_STRICT = ConfigDict(extra="forbid")


def _r(v: float) -> float:
    out = round(float(v), TIME_DECIMALS)
    return 0.0 if out == 0 else out


class _Model(BaseModel):
    model_config = _STRICT


class _Span(_Model):
    """`t0 <= t1`, 4-decimal. Subclasses set `_strict = True` for `t0 < t1`."""
    t0: float
    t1: float
    _strict: bool = False

    @field_validator("t0", "t1")
    @classmethod
    def _round(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("time must be finite")
        return _r(v)

    @model_validator(mode="after")
    def _monotone(self) -> "_Span":
        if self.t1 < self.t0 or (self._strict and self.t1 <= self.t0):
            raise ValueError(f"t1 must be after t0 ({self.t0} .. {self.t1})")
        return self


class _StrictSpan(_Span):
    _strict: bool = True


# --------------------------------------------------------------------------
# graph header (`<session>/brain/graph/<gid>.json`)
# --------------------------------------------------------------------------

class Drift(_Model):
    anchors_ref_s: list[float] = Field(default_factory=list)
    offsets_s: list[float] = Field(default_factory=list)
    max_dev_ms: float | None = None


class SourceFile(_Model):
    key: str = Field(pattern=SRC_KEY_RE.pattern)
    leaf: str
    ref_t0: float | None = None
    ref_t1: float | None = None
    sync_offset_s: float = 0.0
    drift: Drift | None = None


class AngleGuess(_Model):
    kind: str
    sees: list[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    by: str = "own_mic"


class Source(_Model):
    key: str = Field(pattern=SRC_KEY_RE.pattern)
    role: Literal["reference_audio", "angle", "broll", "music", "other"] = "angle"
    leaf: str = ""
    #: The absolute path of the file this wave (the graph lives in the
    #: session, never in a shared cache); the route's summary drops it.
    path: str | None = None
    content_key: str | None = None
    duration: float | None = None
    sync_offset_s: float = 0.0
    has_video: bool | None = None
    has_audio: bool | None = None
    fps: str | float | int | None = None
    angle: str | None = None
    dialogue: bool = False
    layers: dict[str, str] = Field(default_factory=dict)
    angle_guess: AngleGuess | None = None
    files: list[SourceFile] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class AngleHint(_Model):
    angle: str | None = None
    by: str = "own_mic"
    confidence: float | None = None


class GraphSpeaker(_Model):
    id: str
    label: str | None = None
    name: str | None = None
    role_guess: str | None = None
    share: float | None = None
    questions: int | None = None
    angle_hint: AngleHint | str | None = None


class ContentType(_Model):
    guess: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class Topic(_StrictSpan):
    id: str
    title: str | None = None
    by: str | None = None
    sents: list[str] = Field(default_factory=list)


class MusicHint(_Model):
    mood: str
    energy: float | None = None
    evidence: list[str] = Field(default_factory=list)


class ProjectInfo(_Model):
    canvas: list[int] | None = None
    fps: float | int | str | None = None
    session_language: str | None = None
    controls_seen: list[str] = Field(default_factory=list)


class Graph(_Model):
    version: Literal[1] = 1
    id: str = Field(pattern=GRAPH_ID_RE)
    analysis_version: int = 1
    clock: Literal["reference"] = "reference"
    reference: str = Field(pattern=SRC_KEY_RE.pattern)
    sources: list[Source] = Field(min_length=1)
    speakers: list[GraphSpeaker] = Field(default_factory=list)
    content_type: ContentType | None = None
    layers: dict[str, str] = Field(default_factory=dict)
    digests: dict[str, str] = Field(default_factory=dict)
    scenes: str = "scenes.json"
    topics: list[Topic] = Field(default_factory=list)
    music_hint: MusicHint | None = None
    project: ProjectInfo | None = None
    timings_s: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reference_is_a_source(self) -> "Graph":
        keys = {s.key.removeprefix("src_") for s in self.sources}
        if self.reference.removeprefix("src_") not in keys:
            raise ValueError("reference must name one of the sources")
        return self


# --------------------------------------------------------------------------
# layers (`WORKDIR/analysis/<src_key>/<layer>/<params>.json`)
# --------------------------------------------------------------------------

class Word(_Span):
    id: str
    text: str
    prob: float = 1.0
    spk: str | None = None
    sent: str | None = None
    filler: bool = False
    check: str | None = None


class Features(_Model):
    wpm: float | None = None
    fillers: int = 0
    has_number: bool = False
    strong_number: bool = False
    weak_number: bool = False
    claim: bool = False
    conclusion_marker: bool = False
    story_marker: bool = False
    contrast_words: int = 0
    anaphora_start: bool = False
    len_words: int = 0


class Sentence(_StrictSpan):
    id: str
    text: str
    spk: str | None = None
    kind: str = "statement"
    is_question: bool = False
    answer_of: str | None = None
    complete: bool = True
    weak_start: bool = False
    topic: str | None = None
    features: Features | None = None


class Turn(_StrictSpan):
    id: str | None = None
    spk: str | None = None
    sents: list[str] = Field(default_factory=list)


class AcousticFiller(_StrictSpan):
    id: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, float] = Field(default_factory=dict)


class WordToCheck(_Model):
    word: str
    why: str


class FalseStart(_Span):
    id: str
    kept: str | None = None
    text: str = ""


class Repeat(_Model):
    id: str
    dup: str
    of: str
    similarity: float | None = None


class WeakQuestion(_Model):
    id: str
    sent: str
    spk: str | None = None
    answer: str | None = None
    removable: bool = True
    why: str = ""


class DeadAir(_Span):
    id: str


class Technical(_Span):
    id: str
    why: str = ""


class SpeechFlags(_Model):
    false_starts: list[FalseStart] = Field(default_factory=list)
    repeats: list[Repeat] = Field(default_factory=list)
    weak_questions: list[WeakQuestion] = Field(default_factory=list)
    dead_air: list[DeadAir] = Field(default_factory=list)
    technical: list[Technical] = Field(default_factory=list)


class SpeechLayer(_Model):
    params: dict[str, Any] = Field(default_factory=dict)
    words: list[Word] = Field(default_factory=list)
    sentences: list[Sentence] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
    acoustic_fillers: list[AcousticFiller] = Field(default_factory=list)
    words_to_check: list[WordToCheck] = Field(default_factory=list)
    flags: SpeechFlags = Field(default_factory=SpeechFlags)

    @model_validator(mode="after")
    def _references(self) -> "SpeechLayer":
        sents = {s.id for s in self.sentences}
        words = {w.id for w in self.words}
        for w in self.words:
            if w.sent is not None and w.sent not in sents:
                raise ValueError(f"word {w.id}: sent {w.sent!r} is not a sentence of this layer")
        for r in self.flags.repeats:
            if r.dup not in sents or r.of not in sents:
                raise ValueError(f"repeat {r.id}: dup/of must name sentences of this layer")
        for f in self.flags.false_starts:
            if f.kept is not None and f.kept not in sents:
                raise ValueError(f"false start {f.id}: kept {f.kept!r} is not a sentence")
        for q in self.flags.weak_questions:
            if q.sent not in sents:
                raise ValueError(f"weak question {q.id}: sent {q.sent!r} is not a sentence")
        for c in self.words_to_check:
            if c.word not in words:
                raise ValueError(f"words_to_check: {c.word!r} is not a word of this layer")
        for t in self.turns:
            for sid in t.sents:
                if sid not in sents:
                    raise ValueError(f"turn {t.id}: sent {sid!r} is not a sentence")
        return self

    def ids(self) -> set[str]:
        out = {w.id for w in self.words} | {s.id for s in self.sentences} | {a.id for a in self.acoustic_fillers}
        out |= {t.id for t in self.turns if t.id}
        f = self.flags
        out |= {x.id for x in (*f.false_starts, *f.repeats, *f.weak_questions, *f.dead_air, *f.technical)}
        return out


class Silence(_StrictSpan):
    id: str


class AudioEvent(_Span):
    id: str
    kind: str
    src: str | None = None
    confidence: float | None = None


class AudioLayer(_Model):
    params: dict[str, Any] = Field(default_factory=dict)
    hz: int = 100
    env_10ms: str = ""
    vad: list[list[float]] = Field(default_factory=list)
    silences: list[Silence] = Field(default_factory=list)
    loudness_i: float | None = None
    noise_floor_db: float | None = None
    clipping: list[list[float]] = Field(default_factory=list)
    own_mic_energy: dict[str, list[float]] = Field(default_factory=dict)
    events: list[AudioEvent] = Field(default_factory=list)

    @field_validator("vad", "clipping")
    @classmethod
    def _pairs(cls, v: list[list[float]]) -> list[list[float]]:
        for pair in v:
            if len(pair) != 2 or pair[1] < pair[0]:
                raise ValueError(f"span must be [t0, t1] with t0 <= t1, got {pair}")
        return [[_r(a), _r(b)] for a, b in v]

    def ids(self) -> set[str]:
        return {s.id for s in self.silences} | {e.id for e in self.events}


class Utterance(_StrictSpan):
    spk: str


class LayerSpeaker(_Model):
    id: str
    label: str | None = None
    name: str | None = None
    share: float | None = None
    questions: int | None = None
    role_guess: str | None = None
    angle_hint: AngleHint | str | None = None


class FlipRisk(_Model):
    t: float
    why: str = ""


class SpeakersLayer(_Model):
    params: dict[str, Any] = Field(default_factory=dict)
    engine: str = ""
    k: int = 1
    k_method: str | None = None
    silhouette: float | None = None
    utterances: list[Utterance] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)
    speakers: list[LayerSpeaker] = Field(default_factory=list)
    overlaps: list[list[float]] = Field(default_factory=list)
    flip_risk: list[FlipRisk] = Field(default_factory=list)

    def ids(self) -> set[str]:
        return {s.id for s in self.speakers} | {t.id for t in self.turns if t.id}


class Scores(_Model):
    hook: float = 0.0
    importance: float = 0.0
    standalone: float = 0.0
    quotable: float = 0.0
    emotion: float = 0.0
    virality: float = 0.0
    humour: float | None = None
    quality: float | None = None
    evidence: dict[str, list[str]] = Field(default_factory=dict)


class Annotation(_Model):
    sent: str
    by: str
    model: str | None = None
    task: str | None = None
    prompt_hash: str | None = None
    at: float | int | None = None
    hook: float | None = None
    quotable: float | None = None
    kind: str | None = None
    why: str | None = None


class Budget(_Model):
    calls: int = 0
    spent_s: float = 0.0
    partial_from: str | float | None = None


class SemanticLayer(_Model):
    params: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, Scores] = Field(default_factory=dict)
    annotations: list[Annotation] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    budget: Budget | None = None

    def ids(self) -> set[str]:
        return set(self.scores) | {t.id for t in self.topics}


class Scene(_StrictSpan):
    id: str
    kind: str = "speech"
    spk: str | None = None
    topic: str | None = None
    sents: list[str] = Field(default_factory=list)
    text: str = ""
    features: Features | None = None
    shot: dict[str, Any] | None = None
    quality: dict[str, Any] | None = None
    scores: Scores | None = None
    evidence: dict[str, list[str]] | None = None
    labels: dict[str, str] | None = None


class Scenes(BaseModel):
    """`scenes.json` — a bare list on disk; `Scenes.model_validate(list)`."""
    model_config = _STRICT
    items: list[Scene]

    @model_validator(mode="before")
    @classmethod
    def _wrap(cls, data: Any) -> Any:
        return {"items": data} if isinstance(data, list) else data

    def ids(self) -> set[str]:
        return {s.id for s in self.items}


class AngleMember(_Model):
    angle: str
    src_key: str = Field(pattern=SRC_KEY_RE.pattern)
    path: str
    sync_offset_s: float = 0.0
    confidence: float | None = None
    sees: list[str] = Field(default_factory=list)
    by: str | None = None


class Angles(_Model):
    reference: str
    dialogue: str | None = None
    members: list[AngleMember] = Field(default_factory=list)


# --------------------------------------------------------------------------
# the Edit Decision Plan (`<session>/brain/decisions/<did>.json`)
# --------------------------------------------------------------------------

class Ref(_StrictSpan):
    """A span in ONE file's own seconds: `src` is that file's graph source
    key (resolved to its path through the graph / angles.json) or, for a
    hand-written plan, its absolute path."""
    src: str


class GraphRef(_Model):
    id: str = Field(pattern=GRAPH_ID_RE)
    digest: str = ""


class Controls(_Model):
    content_type: str = "auto"
    energy: int = Field(5, ge=1, le=10)
    captions: str = "auto"
    music: str = "off"
    duration_s: float | None = None
    platform: str | None = None
    ratio: str | None = None
    count: int = Field(1, ge=1)


class HookSummary(_StrictSpan):
    sent: str
    src: str
    quote: str = ""


class Beat(_Model):
    beat: str
    sents: list[str] = Field(default_factory=list)


class DialogueSummary(_Model):
    src: str
    lane: str = "a1"
    offsets: dict[str, float] = Field(default_factory=dict)
    seams: int = 0


class CameraSummary(_Model):
    angles: int = 1
    switches: int = 0
    at_cut: int = 0


class MusicSummary(_Model):
    bed: str | None = None
    shape: str = "bed"
    rel_lu: float | None = None
    duck_lu: float | None = None


class CaptionsSummary(_Model):
    mode: str
    style: str
    position: str = "bottom"


class Deferred(_Model):
    asked: str
    why: str


class Summary(_Model):
    project_type: str
    target: str
    duration_s: float | None = None
    hook: HookSummary | None = None
    story: list[Beat] = Field(default_factory=list)
    dialogue: DialogueSummary | None = None
    camera: CameraSummary = Field(default_factory=CameraSummary)
    pauses_kept: int = 0
    music: MusicSummary | None = None
    captions: CaptionsSummary | None = None
    estimated_seconds: float = 0.0
    deferred: list[Deferred] = Field(default_factory=list)


class Reason(_Model):
    code: str
    facts: list[str] = Field(default_factory=list)
    text: str = ""

    @field_validator("code")
    @classmethod
    def _closed(cls, v: str) -> str:
        if v in REASON_CODES or _PAUSE_KEPT_RE.fullmatch(v):
            return v
        raise ValueError(f"unknown reason code {v!r}")


class Produced(_Model):
    clip_ids: list[str] = Field(default_factory=list)
    keyframe_times: list[float] = Field(default_factory=list)
    marker_ids: list[str] = Field(default_factory=list)
    cue_range: list[float] | None = None


class Decision(_Model):
    id: str = Field(pattern=DECISION_ID_RE)
    kind: DecisionKind
    ref: Ref | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reason: Reason
    score: float = Field(1.0, ge=0.0, le=1.0)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    optional: bool = False
    by: BrainId = "recipes"
    produced: Produced | None = None


class Compiled(_Model):
    #: optional: the Plan id exists only after `Plan.new()` (EB1 integration)
    plan_id: str | None = Field(default=None, pattern=PLAN_ID_RE)
    steps: int = 0
    sentinels: list[str] = Field(default_factory=list)


class EDP(_Model):
    version: Literal[1] = 1
    id: str = Field(pattern=EDP_ID_RE)
    planner_version: int = 1
    created: str
    graph: GraphRef
    controls: Controls = Field(default_factory=Controls)
    style: str
    seed: int = 0
    previous: str | None = Field(None, pattern=EDP_ID_RE)
    scope: Ref | None = None
    brain: BrainId
    content_brain: BrainId | None = None
    summary: Summary
    decisions: list[Decision] = Field(default_factory=list)
    children: list[Any] = Field(default_factory=list)
    compiled: Compiled | None = None
    score: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _unique_decision_ids(self) -> "EDP":
        ids = [d.id for d in self.decisions]
        if len(set(ids)) != len(ids):
            raise ValueError("decision ids must be unique")
        return self

    def by_kind(self, *kinds: str) -> list[Decision]:
        return [d for d in self.decisions if d.kind in kinds]


# --------------------------------------------------------------------------
# canonical bytes + reference checks
# --------------------------------------------------------------------------

def _rounded(v: Any) -> Any:
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return _r(v)
    if isinstance(v, dict):
        return {str(k): _rounded(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_rounded(x) for x in v]
    return v


def canonical_json(model: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Key-sorted, compact, 4-decimal JSON — the bytes two planners writing
    the same decisions both write."""
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    return json.dumps(_rounded(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(model: BaseModel | dict[str, Any] | list[Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()


def graph_ids(graph: Graph, layers: dict[str, Any], *, scenes: Scenes | None = None,
              angles: Angles | None = None) -> set[str]:
    """Every id a `reason.facts` entry may name: source keys (with and
    without the `src_` prefix), speakers, topics, `music_hint`, and each
    loaded layer's own ids."""
    out: set[str] = set()
    for s in graph.sources:
        bare = s.key.removeprefix("src_")
        out |= {bare, f"src_{bare}"}
        out |= {f.key.removeprefix("src_") for f in s.files}
    out |= {sp.id for sp in graph.speakers} | {t.id for t in graph.topics}
    if graph.music_hint is not None:
        out.add("music_hint")
    if graph.content_type is not None:
        out.add("content_type")
    for layer in layers.values():
        ids = getattr(layer, "ids", None)
        if callable(ids):
            out |= ids()
    if scenes is not None:
        out |= scenes.ids()
    if angles is not None:
        out |= {m.src_key.removeprefix("src_") for m in angles.members}
    return out


def unresolved_facts(edp: EDP, ids: set[str]) -> list[tuple[str, str]]:
    return [(d.id, f) for d in edp.decisions for f in d.reason.facts if f not in ids]


def check_facts(edp: EDP, ids: set[str]) -> None:
    missing = unresolved_facts(edp, ids)
    if missing:
        listed = ", ".join(f"{d}: {f}" for d, f in missing[:8])
        raise ValueError(f"reason.facts name ids the graph {edp.graph.id} does not have — {listed}")


__all__ = [
    "MAX_LAYER_BYTES", "MAX_GRAPH_BYTES", "TIME_DECIMALS", "SRC_KEY_RE", "DECISION_KINDS", "REASON_CODES",
    "Graph", "Source", "GraphSpeaker", "Topic", "SpeechLayer", "AudioLayer", "SpeakersLayer", "SemanticLayer",
    "Scenes", "Scene", "Angles", "AngleMember", "Word", "Sentence", "Silence",
    "EDP", "Decision", "Reason", "Ref", "Summary", "Controls", "Compiled", "Produced",
    "canonical_json", "digest", "graph_ids", "unresolved_facts", "check_facts",
]
