"""TimelineFacts — everything the planner may know about a session (spec §2.1).

Frozen contract (§0.2): P owns `build_facts`; B feeds facts to the brains as a
≤ 400-char, path-free summary; X rebuilds them after commit for the verifier.

Two rules every consumer relies on:

  * **All speech facts are in TIMELINE seconds.** Whisper records words
    against the SOURCE file; `build_facts` maps them through
    `timemap.map_segments_to_timeline(edl, "v1", segments)` so a word at
    source 12 s on a timeline with `[5,10)` cut appears here at 7 s. The
    verifier compares caption cues (timeline) with `speech_spans` (timeline)
    directly — mixing the two clocks is exactly the bug that laid cues 10 s
    past the end of the video before BASE (baseline finding 6).
  * **Pure reads.** `build_facts` never transcribes, never probes a model
    cache with anything heavier than `Path.exists`, never triggers the 2.2 s
    feature-report cold path (it takes `feature_report` from
    `ai.features.cached_feature_report()`), and caches the first v1 clip's
    ffprobe per `src` in `store.probe_cache`.

`transcript_pending` is the honest word for the first minute after an upload:
`_bg_transcribe` is a BackgroundTask that swallows exceptions, so "no
transcript yet" and "transcription failed" look identical on disk except for
the mtime of `ingest.json`.

Two fields beyond the §2.1 table, both defaulted so every consumer that
constructs facts by hand keeps working:

  * `word_spans` — the WORD-level timeline spans behind `speech_spans`. The
    `beat_sync` recipe must not split inside a word (§2.4), and segment spans
    cannot say where a word ends; re-reading the transcript inside a recipe
    would break "facts are the only input the planner sees".
  * `transcript_head` — the first ~240 characters of what is still spoken on
    the timeline. The hook heuristic (and B's `hook_candidates` text task,
    whose payload is literally `transcript_head`) needs some words; the
    canned clickbait lines the old heuristic rotated were baseline finding 3.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Aspect = Literal["9:16", "16:9", "1:1", "4:5", "other"]
TranscriptBackend = Literal["faster_whisper", "whisper_cli", None]

#: `ingest.json` without a `transcript` key younger than this counts as
#: "pending" (the upload transcription may still be running), not "absent".
TRANSCRIPT_PENDING_MAX_AGE_S = 600

#: Adjacent speech pieces closer than this merge into one span — a segment
#: boundary is a whisper artefact, not a pause a viewer hears.
SPEECH_MERGE_GAP_S = 0.3

TRANSCRIPT_HEAD_CHARS = 240

#: First-use artefacts (§1.4): key → (bytes, human label). Keys are what the
#: recipes look up; `first_use` holds only the MISSING ones.
FIRST_USE_BYTES: dict[str, tuple[int, str]] = {
    "whisper:small": (480_000_000, "faster-whisper small (480 MB)"),
    "whisper:large-v3-turbo": (1_600_000_000, "faster-whisper large-v3-turbo (1.6 GB)"),
    "whisper:large-v3": (3_100_000_000, "faster-whisper large-v3 (3.1 GB)"),
    "madlad": (3_000_000_000, "MADLAD-400 translation model (3 GB)"),
    "piper:en_US-amy-medium": (60_000_000, "Piper voice en_US-amy-medium (60 MB)"),
    "piper:en_US-ryan-medium": (60_000_000, "Piper voice en_US-ryan-medium (60 MB)"),
    "piper:en_GB-alan-medium": (60_000_000, "Piper voice en_GB-alan-medium (60 MB)"),
    "piper:hi_IN-priyamvada-medium": (60_000_000, "Piper voice hi_IN-priyamvada-medium (60 MB)"),
    "piper:hi_IN-pratham-medium": (60_000_000, "Piper voice hi_IN-pratham-medium (60 MB)"),
}
WHISPER_MODELS: tuple[str, ...] = ("small", "large-v3-turbo", "large-v3")
VOICE_IDS: tuple[str, ...] = tuple(k.split(":", 1)[1] for k in FIRST_USE_BYTES if k.startswith("piper:"))

_AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class SpeechSpan(BaseModel):
    """One span of speech in TIMELINE seconds."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class TimelineFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    duration: float
    canvas_w: int
    canvas_h: int
    fps: int
    aspect: Aspect
    source_aspect: Aspect | None = None
    v1_clip_ids: list[str] = Field(default_factory=list)
    clip_ids: list[str] = Field(default_factory=list)
    track_ids: list[str] = Field(default_factory=list)
    selection: str | None = None
    playhead: float | None = None
    has_transcript: bool = False
    transcript_pending: bool = False
    transcript_backend: TranscriptBackend = None
    language: str | None = None
    words: int = 0
    speech_spans: list[SpeechSpan] = Field(default_factory=list)   # TIMELINE seconds
    speech_seconds: float = 0.0
    filler_count: int = 0        # FILLERS_STRICT tokens that survive on the timeline
    has_music: bool = False
    music_ducked: bool = False
    has_captions: bool = False
    caption_style: str | None = None
    v1_boundaries: list[float] = Field(default_factory=list)
    hook_axes: dict[str, Any] = Field(default_factory=dict)
    brand_handle: str | None = None
    loudness_lufs: float | None = None
    uploads_audio: list[str] = Field(default_factory=list)
    uploads_images: list[str] = Field(default_factory=list)
    allowed_paths: set[str] = Field(default_factory=set)           # resolved absolute paths a plan may read
    tools_available: set[str] = Field(default_factory=set)
    first_use: dict[str, int] = Field(default_factory=dict)        # missing artefact → bytes
    ingest_json_path: str | None = None                            # executor's transcript snapshot
    word_spans: list[SpeechSpan] = Field(default_factory=list)     # TIMELINE seconds, per word
    transcript_head: str = ""                                      # first words still on the timeline
    music_clip_ids: list[str] = Field(default_factory=list)        # the bed(s) a "replace" must remove first

    @classmethod
    def minimal(cls, session_id: str = "s_test", **overrides: Any) -> "TimelineFacts":
        """A syntactically complete facts object for tests and stubs: a 30 s
        1920×1080 30 fps 16:9 timeline with one v1 clip and no transcript.
        Override any field by keyword."""
        base: dict[str, Any] = dict(
            session_id=session_id, duration=30.0, canvas_w=1920, canvas_h=1080, fps=30,
            aspect="16:9", source_aspect="16:9", v1_clip_ids=["c_v1"], clip_ids=["c_v1"],
            track_ids=["v1"], loudness_lufs=-16.0)
        return cls(**{**base, **overrides})

    def with_(self, **changes: Any) -> "TimelineFacts":
        return TimelineFacts.model_validate({**self.model_dump(), **changes})

    @property
    def has_speech(self) -> bool:
        return self.speech_seconds > 0.0

    @property
    def silence_seconds(self) -> float:
        """Timeline seconds NOT covered by speech spans — what `remove_silences`
        can hope to remove. 0 when there is no transcript to predict from."""
        if not self.has_speech:
            return 0.0
        return max(0.0, self.duration - self.speech_seconds)

    def is_cached(self, artefact: str) -> bool:
        """`artefact` is a FIRST_USE_BYTES key; cached means not in `first_use`."""
        return artefact not in self.first_use

    def whisper_cached_best(self) -> str:
        """The best caption model on disk (§2.4 `captions`): large-v3 → turbo →
        small. `small` is returned even when missing — it is the upload model,
        so its download is the one the app already asks for."""
        for m in ("large-v3", "large-v3-turbo", "small"):
            if self.is_cached(f"whisper:{m}"):
                return m
        return "small"


# --------------------------------------------------------------------------
# build_facts
# --------------------------------------------------------------------------

def _merge_spans(spans: list[tuple[float, float]], gap: float) -> list[SpeechSpan]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if e < s:
            continue
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [SpeechSpan(start=round(s, 4), end=round(e, 4)) for s, e in out]


def _whisper_snapshot_exists(model: str) -> bool:
    """`<HF cache>/models--*--faster-whisper-<model>*/snapshots/*/model.bin` —
    faster-whisper resolves `small` to `Systran/faster-whisper-small` and turbo
    to a differently-owned repo, so the owner segment is a wildcard. Pure
    `Path.glob`; never imports faster_whisper or the hub."""
    try:
        from huggingface_hub import constants as _hf
        hub = Path(_hf.HF_HUB_CACHE)
    except Exception:
        hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    if not hub.is_dir():
        return False
    return any(hub.glob(f"models--*--faster-whisper-{model}/snapshots/*/model.bin"))


def _whisper_cli_model_exists(model: str) -> bool:
    try:
        from ...ingest import transcribe as _t
        return _t._whisper_cpp_model_path(model).exists()
    except Exception:
        return False


def _transcript_backend() -> TranscriptBackend:
    try:
        if importlib.util.find_spec("faster_whisper") is not None:
            return "faster_whisper"
    except (ImportError, ValueError):
        pass
    try:
        from ...ingest import transcribe as _t
        if _t._whisper_cpp_available():
            return "whisper_cli"
    except Exception:
        pass
    return None


def first_use_probe(backend: TranscriptBackend) -> dict[str, int]:
    """Missing first-use artefacts → bytes (§1.4). Cheap existence checks only."""
    missing: dict[str, int] = {}
    for m in WHISPER_MODELS:
        present = (_whisper_cli_model_exists(m) if backend == "whisper_cli"
                   else _whisper_snapshot_exists(m))
        if not present:
            missing[f"whisper:{m}"] = FIRST_USE_BYTES[f"whisper:{m}"][0]
    try:
        from ...ai import translate as _tr
        madlad_present = (_tr._model_dir() / "model.bin").exists()
    except Exception:
        madlad_present = False
    if not madlad_present:
        missing["madlad"] = FIRST_USE_BYTES["madlad"][0]
    try:
        from ...ai import tts as _tts
        for v in VOICE_IDS:
            if not _tts.voice_paths(v)[0].exists():
                missing[f"piper:{v}"] = FIRST_USE_BYTES[f"piper:{v}"][0]
    except Exception:
        for v in VOICE_IDS:
            missing[f"piper:{v}"] = FIRST_USE_BYTES[f"piper:{v}"][0]
    return missing


def _probe_cached(store: Any, src: str) -> dict | None:
    """ffprobe of `src`, memoised on the store per path. The store is a plain
    object owned by main._STORES for the life of the session, so a dict hung
    off it is exactly the per-session cache §2.1 asks for."""
    cache = getattr(store, "probe_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            store.probe_cache = cache
        except AttributeError:
            pass
    if src in cache:
        return cache[src]
    info: dict | None = None
    try:
        from ...ingest.probe import probe
        p = probe(Path(src))
        vs = next((s for s in p.streams if getattr(s, "codec_type", None) == "video"), None)
        w = getattr(vs, "width", None) if vs else None
        h = getattr(vs, "height", None) if vs else None
        info = {"duration": p.duration, "width": w, "height": h}
    except Exception:
        info = None
    cache[src] = info
    return info


def _feature_tools_unavailable(report: dict | None) -> set[str]:
    """Tools a plan may not use on this machine. An entry flagged `optional`
    (ai/features.py `speed_tier`, e.g. `gpu_transcribe`) names tools that
    still WORK without it, only slower — it must not gate them: with it
    counted, every Mac lost `auto_caption` and got a CUDA fix in a question."""
    out: set[str] = set()
    for entry in (report or {}).get("unavailable") or []:
        if entry.get("optional"):
            continue
        out.update(entry.get("tools") or [])
        if entry.get("key") == "captions":
            out.add("transcribe")
    return out


def _all_plan_tool_names() -> set[str]:
    from .. import tools as _tools
    names = {t["name"] for t in _tools.list_tools()}
    # Not advertised today but real: the hook stack (no schema anywhere) and
    # the §4.9 `transcribe` tool the recipes emit as a prerequisite.
    return names | {"apply_hook_stack", "transcribe"}


def _files_under(root: Path, exts: set[str] | None = None) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if exts is not None and p.suffix.lower() not in exts:
            continue
        out.append(p)
    return out


def _resolved(p: Path) -> str:
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def build_facts(store: Any, ui_state: dict | None, *, feature_report: dict | None = None) -> TimelineFacts:
    """Pure reads of `store` + `ui_state` → TimelineFacts (spec §2.1).

    `store` is an `EDLStore` (needs `.edl` and `.dir`). `feature_report` is the
    memoised `ai.features` report; when omitted the memo in `ai.features` is
    used if X has landed it, else the plain report (2.2 s cold, once per
    process is the caller's problem — pass it in on hot paths).
    """
    if store is None or not hasattr(store, "edl") or not hasattr(store, "dir"):
        raise ValueError("build_facts needs an EDLStore (with .edl and .dir); "
                         "tests construct TimelineFacts.minimal() instead")
    from ..timemap import map_segments_to_timeline, map_words_to_timeline, media_clips
    from ..dispatch import _current_v1_ingest_json, _load_transcript_with_source
    from ...edl.schema import Clip, TextClip
    from ...show.audit import hook_axes as _hook_axes
    from .presets import aspect_of, music_beds, presets_dir
    from .recipes import FILLERS_STRICT

    edl = store.edl
    sdir = Path(store.dir)
    canvas = edl.canvas
    v1_clips = media_clips(edl, "v1")
    first_src = v1_clips[0].src if v1_clips else None

    # --- geometry -----------------------------------------------------
    source_aspect: Aspect | None = None
    if first_src:
        info = _probe_cached(store, first_src)
        if info and info.get("width") and info.get("height"):
            source_aspect = aspect_of(info["width"], info["height"])

    v1_boundaries: list[float] = []
    for prev, cur in zip(v1_clips, v1_clips[1:]):
        seam = prev.start + prev.effective_duration
        if abs(cur.start - seam) <= 0.05:
            v1_boundaries.append(round(cur.start, 4))

    clip_ids: list[str] = []
    for t in edl.tracks:
        clip_ids.extend(c.id for c in t.clips)

    # --- transcript ---------------------------------------------------
    transcript, tx_src = _load_transcript_with_source(store)
    words_total = 0
    language: str | None = None
    speech_spans: list[SpeechSpan] = []
    word_spans: list[SpeechSpan] = []
    speech_seconds = 0.0
    filler_count = 0
    head = ""
    if transcript is not None:
        language = transcript.language or None
        segments = [s.model_dump() for s in transcript.segments]
        words = [w.model_dump() for s in transcript.segments for w in s.words]
        words_total = len(words) if words else sum(len((s.get("text") or "").split()) for s in segments)
        mapped_segments = map_segments_to_timeline(edl, "v1", segments, src=tx_src)
        speech_spans = _merge_spans([(float(s["start"]), float(s["end"])) for s in mapped_segments],
                                    SPEECH_MERGE_GAP_S)
        speech_seconds = round(sum(sp.duration for sp in speech_spans), 3)
        mapped_words = map_words_to_timeline(edl, "v1", words, src=tx_src) if words else []
        word_spans = [SpeechSpan(start=round(float(w["start"]), 4), end=round(float(w["end"]), 4))
                      for w in mapped_words]
        fillers = set(FILLERS_STRICT)
        filler_count = sum(1 for w in mapped_words
                           if (w.get("word") or "").strip().lower().rstrip(",.!?") in fillers)
        text = " ".join((s.get("text") or "").strip() for s in mapped_segments)
        head = " ".join(text.split())[:TRANSCRIPT_HEAD_CHARS]

    ingest_json = _current_v1_ingest_json(store)
    transcript_pending = False
    if transcript is None and ingest_json is not None:
        try:
            data = json.loads(ingest_json.read_text(encoding="utf-8"))
            fresh = (time.time() - ingest_json.stat().st_mtime) < TRANSCRIPT_PENDING_MAX_AGE_S
            transcript_pending = not data.get("transcript") and fresh
        except (OSError, ValueError):
            transcript_pending = False

    # --- tracks -------------------------------------------------------
    music = edl.get_track("music")
    music_clips = [c for c in (music.clips if music else []) if isinstance(c, Clip)]
    captions = edl.get_track("captions")
    caption_clips = list(captions.clips) if captions else []
    if not caption_clips:
        caption_clips = [c for t in edl.tracks if t.type == "text"
                         for c in t.clips if isinstance(c, TextClip) and c.role == "caption"]
    caption_style = None
    if caption_clips and captions is not None and captions.config is not None:
        caption_style = captions.config.style

    # --- files a plan may read ----------------------------------------
    uploads = sdir / "uploads"
    audio_files = _files_under(uploads / "audio", _AUDIO_EXT)
    image_files = _files_under(uploads / "stickers", _IMAGE_EXT) + _files_under(uploads / "images", _IMAGE_EXT)
    allowed: set[str] = set()
    allowed.update(_resolved(p) for p in _files_under(uploads))
    allowed.update(_resolved(b.path) for b in music_beds())
    allowed.update(_resolved(p) for p in _files_under(presets_dir() / "end_cards"))
    allowed.update(_resolved(p) for p in _files_under(sdir / "cache" / "tts", {".wav"}))

    # --- capabilities -------------------------------------------------
    if feature_report is None:
        from ...ai import features as _features
        cached = getattr(_features, "cached_feature_report", None)
        feature_report = cached() if callable(cached) else _features.feature_report()
    backend = _transcript_backend()
    tools_available = _all_plan_tool_names() - _feature_tools_unavailable(feature_report)

    ui = ui_state or {}
    playhead = ui.get("playhead")
    selection = ui.get("selection")

    return TimelineFacts(
        session_id=sdir.name,
        duration=round(float(edl.duration), 4),
        canvas_w=int(canvas.w), canvas_h=int(canvas.h), fps=int(canvas.fps),
        aspect=aspect_of(canvas.w, canvas.h), source_aspect=source_aspect,
        v1_clip_ids=[c.id for c in v1_clips], clip_ids=clip_ids,
        track_ids=[t.id for t in edl.tracks],
        selection=str(selection) if selection else None,
        playhead=float(playhead) if isinstance(playhead, (int, float)) else None,
        has_transcript=words_total > 0, transcript_pending=transcript_pending,
        transcript_backend=backend, language=language, words=words_total,
        speech_spans=speech_spans, speech_seconds=speech_seconds, filler_count=filler_count,
        has_music=bool(music_clips), music_ducked=bool(music and music.duck is not None),
        music_clip_ids=[c.id for c in music_clips],
        has_captions=bool(caption_clips), caption_style=caption_style,
        v1_boundaries=v1_boundaries, hook_axes=_hook_axes(edl),
        brand_handle=(edl.brand_kit.handle if edl.brand_kit and edl.brand_kit.handle else None),
        loudness_lufs=canvas.loudness_lufs,
        uploads_audio=[_resolved(p) for p in audio_files],
        uploads_images=[_resolved(p) for p in image_files],
        allowed_paths=allowed, tools_available=tools_available,
        first_use=first_use_probe(backend),
        ingest_json_path=str(ingest_json) if ingest_json else None,
        word_spans=word_spans, transcript_head=head,
    )


__all__ = ["Aspect", "TranscriptBackend", "TRANSCRIPT_PENDING_MAX_AGE_S", "SPEECH_MERGE_GAP_S",
           "FIRST_USE_BYTES", "WHISPER_MODELS", "VOICE_IDS",
           "SpeechSpan", "TimelineFacts", "build_facts", "first_use_probe"]
