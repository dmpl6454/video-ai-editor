"""Independent measurements for the benchmark (spec §6.2): every number a
case asserts is read from the EDL on disk, the persisted transcript, ffprobe
or a 360p render — and every source↔timeline conversion goes through
`agent/timemap` directly. Nothing here reads the app's `verify` event; the
cases assert separately that the event AGREES with these measurements.

WHY re-read `edl.json` instead of asking the store: the store the run
mutated is the app's in-memory object. A case is about what was PERSISTED
(what the desktop reloads, what `undo` restores), so the file is the truth.

WHY ground-truth spans, not whisper words, for "the filler is gone": the
narration module planted every filler at an exact source offset with an
exact voiced span. A filler counts as gone when less than half of its voiced
span still plays on the timeline (`survival`), which is a measurement of the
cut, not of whisper's word timing.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent import timemap
from video_ai_editor.edl.schema import EDL, Clip, Keyframe, TextClip, Transition
from video_ai_editor.render.verify_render import integrated_loudness, render_for_verify, total_silence

from .narration import Narration

#: A verify render is skipped past this (matches service.VERIFY_RENDER_MAX_DURATION_S).
RENDER_MAX_S = 600.0
#: Word-interior tolerance for "no split inside a word" (the recipe keeps
#: splits ≥ 120 ms from a word interior; measure at the same margin).
WORD_EDGE_S = 0.12


@dataclass(frozen=True)
class Assertion:
    """One machine-checkable claim: `name`, whether it held, what was measured
    against what. `passed=None` means it could not be measured here (and is
    reported, never counted)."""
    name: str
    passed: bool | None
    measured: Any = None
    expected: Any = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pass": self.passed, "measured": self.measured,
                "expected": self.expected, "detail": self.detail}


def check(name: str, passed: bool | None, measured: Any = None, expected: Any = None,
          detail: str = "") -> Assertion:
    return Assertion(name, None if passed is None else bool(passed), measured, expected, detail)


# --------------------------------------------------------------------------
# the persisted state
# --------------------------------------------------------------------------

def load_edl(session_dir: Path) -> EDL:
    return EDL.model_validate_json((Path(session_dir) / "edl.json").read_text(encoding="utf-8"))


def load_ops(session_dir: Path) -> list[dict[str, Any]]:
    p = Path(session_dir) / "ops.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data.get("ops") or [])


def edl_hash(session_dir: Path) -> str:
    return load_edl(session_dir).hash()


@dataclass(frozen=True)
class Snapshot:
    """The persisted state of a session at one instant — taken before a
    prompt so a case can measure the delta (and `undo`)."""
    hash: str
    duration: float
    extent: float
    ops: int
    v1_boundaries: tuple[float, ...]

    @classmethod
    def take(cls, session_dir: Path) -> "Snapshot":
        edl = load_edl(session_dir)
        return cls(hash=edl.hash(), duration=float(edl.duration), extent=float(edl.video_extent()),
                   ops=len(load_ops(session_dir)), v1_boundaries=tuple(v1_boundaries(edl)))


# --------------------------------------------------------------------------
# tracks + clips
# --------------------------------------------------------------------------

def v1_clips(edl: EDL) -> list[Clip]:
    return timemap.media_clips(edl, "v1")


def v1_boundaries(edl: EDL) -> list[float]:
    clips = v1_clips(edl)
    return [round(nxt.start, 4) for cur, nxt in zip(clips, clips[1:])
            if abs((cur.start + cur.effective_duration) - nxt.start) <= 0.05]


def text_clips(edl: EDL, role: str | None = None) -> list[TextClip]:
    out: list[TextClip] = []
    for t in edl.tracks:
        for c in t.clips:
            if isinstance(c, TextClip) and (role is None or c.role == role):
                out.append(c)
    return out


def caption_clips(edl: EDL) -> list[TextClip]:
    track = edl.get_track("captions")
    if track is not None and track.clips:
        return [c for c in track.clips if isinstance(c, TextClip)]
    return text_clips(edl, "caption")


def caption_style(edl: EDL) -> str | None:
    track = edl.get_track("captions")
    return track.config.style if track is not None and track.config is not None else None


def music_clips(edl: EDL) -> list[Clip]:
    track = edl.get_track("music")
    return [c for c in (track.clips if track else []) if isinstance(c, Clip)]


def music_duck_db(edl: EDL) -> float | None:
    track = edl.get_track("music")
    return float(track.duck.to_db) if track is not None and track.duck is not None else None


def vo_clips(edl: EDL) -> list[Clip]:
    return [c for t in edl.tracks if t.type == "vo" for c in t.clips if isinstance(c, Clip)]


def transitions(edl: EDL) -> list[Transition]:
    track = edl.get_track("v1")
    return list(track.transitions) if track else []


def lut_effects(clip: Clip) -> list[dict[str, Any]]:
    return [dict(e.params) for e in clip.effects if e.type == "lut"]


def pulse_clips(edl: EDL) -> list[Clip]:
    """v1 clips carrying a scale keyframe ramp (the beat-sync punch-in)."""
    return [c for c in v1_clips(edl)
            if isinstance(c.transform.scale, Keyframe) and len(c.transform.scale.keyframes) >= 2]


def min_shot(edl: EDL) -> float:
    return min((c.effective_duration for c in v1_clips(edl)), default=0.0)


# --------------------------------------------------------------------------
# ground truth ↔ timeline (timemap is the only converter)
# --------------------------------------------------------------------------

def survival(edl: EDL, span: tuple[float, float]) -> float:
    """Fraction of the SOURCE span `span` (seconds of the v1 source) that
    still plays somewhere on the timeline."""
    s, e = span
    if e <= s:
        return 0.0
    kept = sum(b - a for a, b in timemap.source_range_to_timeline(edl, "v1", s, e))
    return max(0.0, min(1.0, kept / (e - s)))


def fillers_gone(edl: EDL, narration: Narration, *, gone_below: float = 0.5) -> tuple[int, int, list[str]]:
    """(gone, planted, detail) — a planted filler is gone when less than
    `gone_below` of its voiced span survives."""
    rows: list[str] = []
    gone = 0
    for u in narration.fillers:
        frac = survival(edl, u.voiced)
        if frac < gone_below:
            gone += 1
        else:
            rows.append(f"{u.text}@{u.voiced[0]:.2f}s survives {frac:.0%}")
    return gone, len(narration.fillers), rows


def content_like_survival(edl: EDL, narration: Narration) -> float:
    return survival(edl, narration.content_like.voiced)


def sentences_survival(edl: EDL, narration: Narration) -> float:
    """Mean survival of the content sentences' voiced spans."""
    vals = [survival(edl, u.voiced) for u in narration.sentences]
    return sum(vals) / len(vals) if vals else 0.0


def timeline_speech_spans(edl: EDL, narration: Narration) -> list[tuple[float, float]]:
    """The ground-truth voiced spans, mapped onto the timeline and merged."""
    spans: list[tuple[float, float]] = []
    for s, e in narration.speech_spans:
        spans.extend(timemap.source_range_to_timeline(edl, "v1", s, e))
    return merge_spans(spans)


def merge_spans(spans: Iterable[tuple[float, float]], gap: float = 0.0) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def span_total(spans: Iterable[tuple[float, float]]) -> float:
    return sum(e - s for s, e in spans)


def intersection(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0.0, min(e1, e2) - max(s1, s2))
    return total


def caption_cover(edl: EDL, narration: Narration) -> float:
    """Share of the ground-truth speech (in TIMELINE seconds) under a caption."""
    speech = timeline_speech_spans(edl, narration)
    if not speech:
        return 0.0
    cues = merge_spans((float(c.start), float(c.end)) for c in caption_clips(edl))
    return intersection(cues, speech) / span_total(speech)


def captions_within_extent(edl: EDL, *, tol: float = 0.05) -> tuple[bool, float, float]:
    """(ok, last cue end, video extent)."""
    cues = caption_clips(edl)
    extent = float(edl.video_extent())
    last = max((float(c.end) for c in cues), default=0.0)
    return last <= extent + tol, last, extent


def transcript_words_source(session_dir: Path, edl: EDL) -> list[dict[str, Any]]:
    """The persisted whisper words (SOURCE seconds) of the v1 clip's ingest —
    read the way the app resolves them: the sibling `ingest.json` of the
    first v1 clip's source, else `<session>/transcript.json`."""
    clips = v1_clips(edl)
    candidates: list[Path] = []
    if clips:
        candidates.append(Path(clips[0].src).parent / "ingest.json")
    candidates.append(Path(session_dir) / "transcript.json")
    for p in candidates:
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        tx = data.get("transcript", data) if "segments" not in data else data
        if not isinstance(tx, dict):
            continue
        words = [w for s in tx.get("segments", []) for w in (s.get("words") or [])]
        if words:
            return words
    return []


def _norm_token(text: str) -> str:
    return "".join(ch for ch in text.strip().lower() if ch.isalnum())


def _cue_first_word(cue: TextClip, words: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The transcript word the cue's first token was made from: the nearest
    mapped word with the same normalised text that starts within 2 s before
    the cue or anywhere inside it; else the first word at or after the cue's
    start (a cue whose text was rewritten, e.g. translated)."""
    tokens = cue.text.split()
    head = _norm_token(tokens[0]) if tokens else ""
    lo, hi = float(cue.start) - 2.0, float(cue.end)
    inside = [w for w in words if lo <= float(w["start"]) <= hi]
    if head:
        same = [w for w in inside if _norm_token(str(w.get("word", ""))) == head]
        if same:
            return min(same, key=lambda w: abs(float(w["start"]) - float(cue.start)))
    return next((w for w in words if float(w["start"]) >= float(cue.start) - 0.05), None)


def captions_sync(session_dir: Path, edl: EDL, *, tol: float = 0.1) -> tuple[bool | None, float, str]:
    """Spec §4.4 `captions_sync`: for the first cue after each v1 seam, the
    cue's start lies within `tol` of the timeline time of ITS OWN first word
    (the transcript word its leading token came from, mapped through
    timemap). Returns (ok | None when unmeasurable, worst delta, detail)."""
    seams = v1_boundaries(edl)
    words = timemap.map_words_to_timeline(edl, "v1", transcript_words_source(session_dir, edl))
    cues = sorted(caption_clips(edl), key=lambda c: c.start)
    if not seams or not words or not cues:
        return None, 0.0, "no seams, words or cues to compare"
    worst = 0.0
    rows: list[str] = []
    for seam in seams:
        cue = next((c for c in cues if float(c.start) >= seam - 0.05), None)
        word = _cue_first_word(cue, words) if cue is not None else None
        if word is None or cue is None:
            continue
        delta = abs(float(cue.start) - float(word["start"]))
        worst = max(worst, delta)
        if delta > tol:
            rows.append(f"seam {seam:.2f}s: cue {cue.start:.2f} ({cue.text.split()[0] if cue.text.split() else ''!r}) "
                        f"vs its word {float(word['start']):.2f}")
    return worst <= tol, round(worst, 3), "; ".join(rows)


def split_inside_word(edl: EDL, session_dir: Path, boundaries: Iterable[float],
                      *, margin: float = WORD_EDGE_S) -> list[str]:
    """Boundaries that fall inside the interior of a transcript word (mapped
    onto the timeline), beyond `margin` from either edge."""
    words = timemap.map_words_to_timeline(edl, "v1", transcript_words_source(session_dir, edl))
    bad: list[str] = []
    for b in boundaries:
        for w in words:
            s, e = float(w["start"]), float(w["end"])
            if s + margin < b < e - margin:
                bad.append(f"{b:.3f}s inside {w.get('word', '').strip()!r} [{s:.2f},{e:.2f}]")
                break
    return bad


def first_kept_source_time(edl: EDL) -> float | None:
    clips = v1_clips(edl)
    return float(clips[0].in_) if clips else None


# --------------------------------------------------------------------------
# overlays + the safe zone
# --------------------------------------------------------------------------

def overlay_zone_violations(edl: EDL) -> list[str]:
    """Overlays whose renderer-resolved anchor falls outside the safe zone of
    the canvas aspect (watermarks are exempt by design)."""
    from video_ai_editor.agent.prompt.presets import aspect_of, safe_zone_for
    from video_ai_editor.agent.prompt.verify import overlay_positions
    zone = safe_zone_for(aspect_of(edl.canvas.w, edl.canvas.h))
    return [f"{oid}({role} x={x:.2f} y={y:.2f})" for oid, role, x, y in overlay_positions(edl)
            if not zone.contains(x, y)]


def hook_clip(edl: EDL) -> TextClip | None:
    hooks = sorted(text_clips(edl, "hook"), key=lambda c: c.start)
    return hooks[0] if hooks else None


# --------------------------------------------------------------------------
# renders + probes
# --------------------------------------------------------------------------

def render(session_dir: Path, edl: EDL | None = None) -> Path | None:
    """The 360p verify render of the persisted EDL (cached by EDL hash under
    the session); None past `RENDER_MAX_S`."""
    edl = edl or load_edl(session_dir)
    return render_for_verify(edl, Path(session_dir), max_duration_s=RENDER_MAX_S)


def probe_duration(path: Path) -> float:
    from video_ai_editor.ingest.probe import probe
    return float(probe(Path(path)).duration)


def audio_duration(path: Path) -> float:
    """The AUDIO stream's duration — exact to the sample, unlike the
    container/video duration, which is frame-quantised and longer (case 26:
    video 84.800 s, audio 84.734 s for an expected 84.734). Falls back to the
    container duration for a silent file."""
    from video_ai_editor.ingest.probe import probe
    p = probe(Path(path))
    a = p.audio
    return float(a.duration) if a is not None and a.duration else float(p.duration)


def caption_seam_deltas(session_dir: Path, edl: EDL) -> list[float | None]:
    """Per v1 seam (in order): `cue.start − its own first word's timeline
    start` for the first cue after the seam, or None when there is no cue or
    word to pair. Measured BEFORE and AFTER an edit and compared per seam,
    this is DRIFT — how much a cue moved relative to its word — which is the
    editor's claim; the absolute offset is whisper's segmentation (it starts
    'Um,' inside the preceding 2 s pause, 1.6 s early on the fixture), the
    same before and after, and no editor can change it."""
    seams = v1_boundaries(edl)
    words = timemap.map_words_to_timeline(edl, "v1", transcript_words_source(session_dir, edl))
    cues = sorted(caption_clips(edl), key=lambda c: c.start)
    out: list[float | None] = []
    for seam in seams:
        cue = next((c for c in cues if float(c.start) >= seam - 0.05), None)
        word = _cue_first_word(cue, words) if cue is not None else None
        out.append(None if cue is None or word is None else round(float(cue.start) - float(word["start"]), 4))
    return out


def render_silence(path: Path) -> float:
    return total_silence(Path(path))


def render_loudness(path: Path) -> float | None:
    return integrated_loudness(Path(path))


def frame_hash(path: Path, t: float, *, size: str = "64x36") -> str:
    """Hash of one frame at `t`, downscaled to `size` grey so encoder noise
    does not count as a difference but a punch-in or a cut does."""
    proc = subprocess.run([_pu.FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
                           "-i", str(path), "-frames:v", "1", "-vf", f"scale={size},format=gray",
                           "-f", "rawvideo", "-"], capture_output=True, **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"frame extraction at {t:.3f}s failed: {proc.stderr[-300:]!r}")
    return hashlib.md5(proc.stdout).hexdigest()


def expected_render_duration(edl: EDL) -> tuple[float, float]:
    """(expected rendered seconds, seconds the transitions overlap) computed
    from the clips and transitions alone — the renderer's rule, restated
    here rather than imported from `EDL.transition_overlap()` so a change to
    that method shows up as a benchmark disagreement."""
    clips = v1_clips(edl)
    extent = max((c.start + c.effective_duration for c in clips), default=0.0)
    overlap = 0.0
    trs = transitions(edl)
    for cur, nxt in zip(clips, clips[1:]):
        seam = cur.start + cur.effective_duration
        if nxt.start - seam > 0.001:
            continue
        match = next((t for t in trs if abs(t.at - seam) < 0.05), None)
        if match is not None:
            overlap += max(0.0, min(float(match.duration), cur.effective_duration, nxt.effective_duration))
    return extent - overlap, overlap


# --------------------------------------------------------------------------
# beats
# --------------------------------------------------------------------------

@lru_cache(maxsize=8)
def detected_beats(path: str) -> tuple[float, ...]:
    from video_ai_editor.ingest.beats import detect_beats
    return tuple(round(float(b), 4) for b in detect_beats(Path(path)))


def bed_beats_on_timeline(edl: EDL) -> tuple[list[float], float | None]:
    """librosa's beats of the bed on the music track, shifted to timeline
    time, plus the median beat period (for the tempo-octave rule)."""
    beds = music_clips(edl)
    if not beds:
        return [], None
    first = beds[0]
    beats = [b + float(first.start) - float(first.in_) for b in detected_beats(first.src)]
    if len(beats) < 3:
        return beats, None
    gaps = sorted(b - a for a, b in zip(beats, beats[1:]))
    return beats, gaps[len(gaps) // 2]


def beat_alignment(boundaries: Iterable[float], beats: list[float], period: float | None,
                   *, tol: float) -> tuple[float, list[float]]:
    """Fraction of `boundaries` within `tol` of a beat. Tempo-octave
    equivalence: half-beats count too (a detector locking at 2× or ½× the
    grid is the same rhythm)."""
    grid = list(beats)
    if period:
        grid += [b + period / 2 for b in beats]
    bad: list[float] = []
    rows = list(boundaries)
    for b in rows:
        if not any(abs(b - g) <= tol for g in grid):
            bad.append(b)
    frac = 1.0 if not rows else (len(rows) - len(bad)) / len(rows)
    return frac, bad


def pulses_on_beats(edl: EDL, beats: list[float], *, tol: float = 0.06) -> int:
    """v1 fragments whose scale ramp starts within `tol` of a detected beat."""
    hits = 0
    for c in pulse_clips(edl):
        kf = c.transform.scale
        assert isinstance(kf, Keyframe)
        t0 = float(c.start) + float(kf.keyframes[0][0])
        if any(abs(t0 - b) <= tol for b in beats):
            hits += 1
    return hits


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def script_ratio(text: str, *, block: tuple[int, int]) -> float:
    lo, hi = block
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if lo <= ord(ch) <= hi) / len(letters)


DEVANAGARI = (0x0900, 0x097F)
LATIN = (0x0041, 0x024F)


def captions_text(edl: EDL) -> str:
    return " ".join(str(c.text) for c in caption_clips(edl))


__all__ = ["Assertion", "check", "load_edl", "load_ops", "edl_hash", "Snapshot", "v1_clips",
           "v1_boundaries", "text_clips", "caption_clips", "caption_style", "music_clips",
           "music_duck_db", "vo_clips", "transitions", "lut_effects", "pulse_clips", "min_shot",
           "survival", "fillers_gone", "content_like_survival", "sentences_survival",
           "timeline_speech_spans", "merge_spans", "span_total", "intersection", "caption_cover",
           "captions_within_extent", "transcript_words_source", "captions_sync", "split_inside_word",
           "first_kept_source_time", "overlay_zone_violations", "hook_clip", "render", "probe_duration",
           "render_silence", "render_loudness", "frame_hash", "expected_render_duration",
           "detected_beats", "bed_beats_on_timeline", "beat_alignment", "pulses_on_beats",
           "script_ratio", "DEVANAGARI", "LATIN", "captions_text"]
