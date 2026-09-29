"""Postconditions measured on the real EDL, transcript, ffprobe and — for two
checks — a 360p render (spec §4.4).

Every name in `schema.CHECK_SPECS` has an implementation here (a test walks
the table). Each check returns a `CheckResult` with what was MEASURED and
what was EXPECTED, so the summary can say "captions cover 71% of speech,
expected ≥ 90%" instead of "failed". `passed=None` means "could not measure"
(no speech to cover, no overlays to place, timeline too long to render) —
reported, never counted as a pass.

Three clocks, one rule (facts.py, render/clock.py): transcript words are
SOURCE seconds; every caption cue, clip start, transition `at` and tool
argument is LAYOUT (timeline) seconds — the EDL's coordinate space; and
`edl.duration`, ffprobe and the frames of a verify render are RENDER
seconds, `render_time(t) = t − Σ{d : v1 seam ≤ t}` (layout end minus the
overlap the cross-fades consumed; `EDL.v1_seam_table()`). All speech here is
mapped through `timemap.map_words_to_timeline` against the edl being judged
— the edl BEFORE the run for "what was there", the edl AFTER for "what
survived" — so a check never compares source against layout directly; and
the checks that read `edl.duration` (`duration_*`) are the render clock,
while `captions_within_extent`/`captions_sync`/`hook_text_starts_leq` are
layout against layout. Mixing those two is how the overlay-lane drift stayed
invisible to the EDL-level checks for as long as it did.

WHY the verifier reads the transcript itself instead of trusting
`facts_after.speech_spans`: facts are the planner's input; the verifier is
the planner's auditor. Reading the same file through the same mapper is one
code path fewer to disagree with the tools it is checking.
"""
from __future__ import annotations

import importlib
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...edl import EDLStore
from ...edl.schema import EDL, Clip, Sticker, TextClip
from ..timemap import map_words_to_timeline, source_range_to_timeline
from .facts import TimelineFacts
from .langs import base_lang
from .recipes import FILLERS_STRICT
from .schema import BLOCKING_CHECKS, CHECK_SPECS, CLIP_SENTINELS, Plan, Postcondition, bind_postconditions
from .service import VERIFY_RENDER_MAX_DURATION_S

_D = importlib.import_module("video_ai_editor.agent.dispatch")

Emit = Callable[[dict[str, Any]], None]

#: Words closer than this (timeline seconds) merge into one speech span.
_SPEECH_GAP_S = 0.3
#: `captions_within_extent` / `music_within_video_extent` slack (§4.4).
_EXTENT_SLACK_S = 0.05
#: `beat_pulse_present`: a keyframe start within this of a detected beat.
_BEAT_TOL_S = 0.06
#: `no_letterbox`: probed frame aspect vs canvas aspect tolerance.
_ASPECT_TOL = 0.02
#: `speech_preserved`: a kept word must still play at least this much of
#: its source range (see `c_speech_preserved` for why not 100%) …
_WORD_KEEP_RATIO = 0.8
#: … OR its END plays and at least this much does. Whisper starts a word up
#: to ~0.3 s BEFORE the voiced onset after a pause (measured: ' The' =
#: 15.91–16.37 s, onset 16.27 s), so a silence cut that trims the pre-onset
#: air keeps the whole voice yet only 44% of the whisper span. A cut through
#: the middle or the tail of a word still fails: the end must play.
_WORD_TAIL_RATIO = 0.4
_WORD_END_SLACK_S = 0.02
#: `speech_preserved`: energy is ground truth, timestamps are estimates. A
#: "lost" word whose SOURCE span lies at least this much inside a silent run
#: of the source is a misaligned timestamp, not a cut word. Measured on the
#: TikTok run: the transcript held "than, it, looks, in, photos." at
#: 14.29–16.28 s as five back-to-back 0.40 s spans (whisper's uniform-spacing
#: fallback) while silencedetect on the same source read 14.118–16.248 s as
#: silent; remove_silences cut that air correctly and the check reported 20
#: words lost. 70% (not 100%) because silencedetect's own edges are ±1 hop.
_WORD_IN_SILENCE_RATIO = 0.7
#: `remove_silences` recipe defaults (recipes.py / dispatch.remove_silences)
#: — what the verifier assumes when no plan step names them.
_SILENCE_NOISE_DB = -30.0
_SILENCE_MIN_DUR_S = 0.5
_SILENCE_KEEP_PAD_S = 0.1
#: `silence_total_leq`: a remaining silent run counts as a "long pause" only
#: when it is at least `min_dur + 2×keep_pad + this`. After remove_silences,
#: every cut pause leaves exactly 2×keep_pad of deliberately kept air, and
#: sub-`min_dur` pauses were never its job; the old "≤ 1.0 s of ANY silence"
#: read 7 × 0.2 s of kept air plus natural breaths as 3.79 s of failure on a
#: run whose dead air was entirely gone. 0.15 s absorbs silencedetect's edge
#: jitter and the encoder's pre-echo on the verify render.
_LONG_PAUSE_TOL_S = 0.15

_SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass
class CheckResult:
    check: str
    human: str
    passed: bool | None
    measured: Any = None
    expected: Any = None
    unit: str | None = None
    detail: str | None = None
    headline: bool = True

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"check": self.check, "human": self.human, "pass": self.passed,
                             "measured": self.measured, "expected": self.expected,
                             "headline": self.headline, "blocking": self.check in BLOCKING_CHECKS}
        if self.unit is not None:
            d["unit"] = self.unit
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass
class VerifyCtx:
    store: EDLStore
    plan: Plan
    exec_result: Any                      # executor.ExecResult
    facts_before: TimelineFacts
    render_path: Path | None = None
    render_skip_reason: str | None = None
    store_resolver: Callable[[str], EDLStore] | None = None
    cancel_event: threading.Event | None = None
    speech_render_skip_reason: str | None = None
    _probe_cache: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    _transcript: Any = None
    _tx_loaded: bool = False
    _speech_render: Path | None = None
    _speech_render_tried: bool = False
    #: Whether this verify may render at all (`verify_plan(render=...)`). A
    #: ctx built by hand (tests, tools) does not render unless it says so.
    render_allowed: bool = False
    duck_probe_skip_reason: str | None = None
    _duck_probe: Path | None = None
    _duck_probe_tried: bool = False
    _source_silence_cache: dict[str, list[tuple[float, float]] | None] = field(default_factory=dict)

    @property
    def edl(self) -> EDL:
        return self.store.edl

    @property
    def edl_before(self) -> EDL:
        return self.exec_result.edl_before

    def transcript(self) -> tuple[Any, str | None]:
        if not self._tx_loaded:
            self._transcript = _D._load_transcript_with_source(self.store)
            self._tx_loaded = True
        return self._transcript

    def words_source(self) -> list[dict]:
        tx, _ = self.transcript()
        return [w.model_dump() for w in tx.words] if tx is not None else []

    def words_on(self, edl: EDL) -> list[dict]:
        _, src = self.transcript()
        return map_words_to_timeline(edl, "v1", self.words_source(), src=src)

    def speech_spans(self, edl: EDL) -> list[tuple[float, float]]:
        return _merge_spans([(float(w["start"]), float(w["end"])) for w in self.words_on(edl)])

    def source_silences(self) -> list[tuple[float, float]] | None:
        """Silent runs of the file the transcript's times index, in SOURCE
        seconds, measured once per verify with the plan's remove_silences
        threshold/min-duration (or the recipe defaults) and cached on the
        ctx. None when there is no source or ffmpeg could not read it —
        the caller then judges by timestamps alone, as before."""
        _, src = self.transcript()
        if not src:
            return None
        key = str(src)
        if key not in self._source_silence_cache:
            noise_db, min_dur, _ = _silence_params(self)
            try:
                self._source_silence_cache[key] = silence_runs(Path(key), noise_db=noise_db, min_dur=min_dur)
            except Exception:  # noqa: BLE001 — an unreadable source is "unknown", not a crash
                self._source_silence_cache[key] = None
        return self._source_silence_cache[key]

    def speech_render(self) -> Path | None:
        """The verify render with the music track muted (cached by the
        stripped EDL's hash like the main one); None — with a reason — when
        it cannot be made. Only rendered when a check asks for it."""
        if self._speech_render_tried:
            return self._speech_render
        self._speech_render_tried = True
        if self.render_path is None:
            self.speech_render_skip_reason = self.render_skip_reason or "verify render unavailable"
            return None
        from ...render.verify_render import render_for_verify
        try:
            self._speech_render = render_for_verify(
                self.edl, Path(self.store.dir), max_duration_s=VERIFY_RENDER_MAX_DURATION_S,
                cancel_event=self.cancel_event, speech_only=True)
        except Exception as e:  # noqa: BLE001 — an unmeasured check, never a crash
            self.speech_render_skip_reason = f"speech-only render failed: {type(e).__name__}: {e}"
            self._speech_render = None
        return self._speech_render

    def duck_probe_render(self) -> Path | None:
        """The duck-gain stem of the current timeline (`audio_mix.
        STEM_DUCK_PROBE`: a 1 kHz carrier times the gain the renderer applied
        to the music), rendered once per verify; None — with a reason — when
        rendering is off or impossible."""
        if self._duck_probe_tried:
            return self._duck_probe
        self._duck_probe_tried = True
        if not self.render_allowed:
            self.duck_probe_skip_reason = "verify render disabled"
            return None
        from ...render.audio_mix import STEM_DUCK_PROBE
        from ...render.verify_render import render_for_verify
        try:
            self._duck_probe = render_for_verify(
                self.edl, Path(self.store.dir), max_duration_s=VERIFY_RENDER_MAX_DURATION_S,
                cancel_event=self.cancel_event, stem=STEM_DUCK_PROBE)
            if self._duck_probe is None:
                self.duck_probe_skip_reason = (f"timeline is {self.edl.duration:.0f}s "
                                               f"(> {VERIFY_RENDER_MAX_DURATION_S:.0f}s)")
        except Exception as e:  # noqa: BLE001 — an unmeasured check, never a crash
            self.duck_probe_skip_reason = f"duck probe render failed: {type(e).__name__}: {e}"
            self._duck_probe = None
        return self._duck_probe

    def probe_dims(self, src: str) -> tuple[int, int] | None:
        if src not in self._probe_cache:
            try:
                from ...ingest.probe import probe
                v = probe(Path(src)).video
                self._probe_cache[src] = (int(v.width), int(v.height)) if v and v.width and v.height else None
            except Exception:  # noqa: BLE001 — an unprobeable file is "unknown", not a crash
                self._probe_cache[src] = None
        return self._probe_cache[src]

    def child_store(self, sid: str) -> EDLStore | None:
        if self.store_resolver is not None:
            try:
                return self.store_resolver(sid)
            except Exception:  # noqa: BLE001
                return None
        from ...storage import session_dir, session_exists
        return EDLStore(session_dir(sid)) if session_exists(sid) else None


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------

def _merge_spans(spans: list[tuple[float, float]], gap: float = _SPEECH_GAP_S) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _span_total(spans: list[tuple[float, float]]) -> float:
    return sum(max(0.0, e - s) for s, e in spans)


def _intersection(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0.0, min(e1, e2) - max(s1, s2))
    return total


def silence_runs(path: Path, *, noise_db: float = _SILENCE_NOISE_DB, min_dur: float = _SILENCE_MIN_DUR_S,
                 end: float | None = None) -> list[tuple[float, float]]:
    """`(start, end)` of every stretch below `noise_db` for at least `min_dur`
    that ffmpeg `silencedetect` finds in `path`'s audio, in file seconds.
    A run still open when the stream ends (no `silence_end` line) is closed
    at `end` when given, else left open-ended (`inf`) so intersection math
    still counts it. WHY a runs reader beside `verify_render.total_silence`:
    both checks here need the individual runs — the longest one, and which
    words fall inside one — and a sum cannot answer either."""
    from ...render.verify_render import _ffmpeg_stderr
    err = _ffmpeg_stderr(["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}"])
    starts = [float(m) for m in _SIL_START_RE.findall(err)]
    ends = [float(m) for m in _SIL_END_RE.findall(err)]
    runs = [(max(0.0, s), e) for s, e in zip(starts, ends)]
    if len(starts) > len(ends):
        runs.append((max(0.0, starts[-1]), float(end) if end is not None else float("inf")))
    return runs


def _silence_params(ctx: Any) -> tuple[float, float, float]:
    """`(noise_db, min_dur, keep_pad)` the plan's `remove_silences` step ran
    with, else the recipe defaults — so the verifier measures silence the
    way the tool did, not against a threshold of its own."""
    for step in ctx.plan.steps:
        if step.tool == "remove_silences":
            a = step.args
            return (float(a.get("threshold_db", _SILENCE_NOISE_DB)), float(a.get("min_dur", _SILENCE_MIN_DUR_S)),
                    float(a.get("keep_pad", _SILENCE_KEEP_PAD_S)))
    return _SILENCE_NOISE_DB, _SILENCE_MIN_DUR_S, _SILENCE_KEEP_PAD_S


def _long_pause_floor(ctx: Any) -> float:
    """Shortest remaining silent run `silence_total_leq` counts as a pause the
    tool should have removed (see `_LONG_PAUSE_TOL_S`)."""
    _, min_dur, keep_pad = _silence_params(ctx)
    return round(min_dur + 2.0 * keep_pad + _LONG_PAUSE_TOL_S, 3)


def _inside_silence(w: dict, runs: list[tuple[float, float]]) -> bool:
    """True when ≥ `_WORD_IN_SILENCE_RATIO` of the word's SOURCE span lies in
    a measured silent run (a zero-length word: its instant does)."""
    s, e = float(w["start"]), float(w["end"])
    if e - s <= 0.0:
        return any(a <= s <= b for a, b in runs)
    return _intersection([(s, e)], runs) >= _WORD_IN_SILENCE_RATIO * (e - s)


def v1_clips(edl: EDL) -> list[Clip]:
    t = edl.get_track("v1")
    return sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)


def caption_clips(edl: EDL) -> list[TextClip]:
    out: list[TextClip] = []
    for t in edl.tracks:
        if t.type == "captions":
            out.extend(c for c in t.clips if isinstance(c, TextClip))
        elif t.type == "text":
            out.extend(c for c in t.clips if isinstance(c, TextClip) and c.role == "caption")
    return sorted(out, key=lambda c: c.start)


def text_clips(edl: EDL) -> list[TextClip]:
    return [c for t in edl.tracks if t.type in ("text", "captions")
            for c in t.clips if isinstance(c, TextClip)]


def music_clips(edl: EDL) -> list[Clip]:
    t = edl.get_track("music")
    return [c for c in (t.clips if t else []) if isinstance(c, Clip)]


def _norm_token(word: str) -> str:
    return (word or "").strip().lower().rstrip(",.!?;:")


def _filler_set(ctx: VerifyCtx, words: Any) -> set[str]:
    chosen = set(FILLERS_STRICT)
    if isinstance(words, (list, tuple)):
        chosen |= {str(w).lower() for w in words}
    for step in ctx.plan.steps:
        if step.tool == "remove_fillers" and isinstance(step.args.get("words"), (list, tuple)):
            chosen |= {str(w).lower() for w in step.args["words"]}
    return chosen


def _clip_targets(ctx: VerifyCtx, clip_id: Any) -> list[str]:
    """The clip ids a `clip_id` arg means at verify time: a real id as-is, a
    sentinel or None → every v1 clip (the executor fanned it out), except
    `$v1_first` / `$v1_last`, which name ONE clip: "slow motion on the last
    clip" was graded against every clip and failed (wave D3 prompt sweep)."""
    if clip_id and clip_id not in CLIP_SENTINELS:
        return [str(clip_id)]
    ids = [c.id for c in v1_clips(ctx.edl)]
    if clip_id == "$v1_first":
        return ids[:1]
    if clip_id == "$v1_last":
        return ids[-1:]
    if clip_id in ("$playhead", "$selected"):
        # run 4: the ONE clip the sentinel names on the verified timeline —
        # after "split at the playhead" the right half, whose id the plan
        # never knew ("from here to the end" was graded against every clip)
        one = _ui_sentinel_clip(ctx, clip_id)
        return [one.id] if one is not None else ids
    return ids


def _ui_sentinel_clip(ctx: VerifyCtx, ref: str) -> Clip | None:
    f = ctx.facts_before
    if ref == "$selected":
        hit = ctx.edl.get_clip(str(f.selection)) if f.selection else None
        return hit[1] if hit and isinstance(hit[1], Clip) else None
    ph = f.playhead
    if ph is None:
        return None
    return next((c for c in v1_clips(ctx.edl) if c.start - 1e-6 <= ph < c.start + c.effective_duration - 1e-6), None)


def _ok(pc: Postcondition, passed: bool | None, measured: Any, expected: Any, *,
        unit: str | None = None, detail: str | None = None) -> CheckResult:
    return CheckResult(check=pc.check, human=pc.human, passed=passed, measured=measured,
                       expected=expected, unit=unit, detail=detail, headline=pc.headline)


def _arg(pc: Postcondition, name: str) -> Any:
    spec = CHECK_SPECS[pc.check]
    return pc.args.get(name, spec.args.get(name))


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def c_transcript_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    n = len(ctx.words_source())
    return _ok(pc, n > 0, n, "> 0", unit="words")


def c_captions_nonempty(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    n = len(caption_clips(ctx.edl))
    return _ok(pc, n >= 1, n, "≥ 1", unit="cues")


def c_captions_cover(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    min_ratio = float(_arg(pc, "min_ratio") or 0.9)
    speech = ctx.speech_spans(ctx.edl)
    speech_s = _span_total(speech)
    if speech_s <= 0.0:
        return _ok(pc, None, None, f"≥ {min_ratio:.0%}", detail="no speech on the timeline to cover")
    cues = _merge_spans([(c.start, c.end) for c in caption_clips(ctx.edl)], gap=0.0)
    ratio = _intersection(cues, speech) / speech_s
    return _ok(pc, ratio >= min_ratio, round(ratio, 3), f"≥ {min_ratio:.0%}", unit="ratio")


def c_captions_within_extent(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    cues = caption_clips(ctx.edl)
    extent = ctx.edl.video_extent()
    if not cues:
        return _ok(pc, None, None, f"≤ {extent:.2f}", detail="no captions")
    last = max(c.end for c in cues)
    return _ok(pc, last <= extent + _EXTENT_SLACK_S, round(last, 3), f"≤ {extent:.2f}", unit="s")


#: `_cue_first_word`: a cue's head word may start this long BEFORE the cue
#: (a rewritten cue) or anywhere inside it; whisper's own cues never lead
#: their first word by more than ~2 s (the segment starts in the pause).
_CUE_HEAD_LOOKBACK_S = 2.0
#: … and the fallback for a cue whose head token matches no word (translated
#: or edited text): the first word at or after its start, with this slack.
_CUE_HEAD_SLACK_S = 0.05


def _head_token(text: Any) -> str:
    """The first whitespace token of `text`, letters and digits only —
    'Um,' and 'um' are the same head; `_norm_token` keeps inner punctuation
    and only strips a trailing mark, which is right for filler matching but
    misses "…um" or "(um)"."""
    tokens = str(text or "").split()
    return "".join(ch for ch in tokens[0].lower() if ch.isalnum()) if tokens else ""


def _words_keyed(ctx: VerifyCtx, edl: EDL) -> list[dict]:
    """`ctx.words_on(edl)` with a `src_key` on every word naming the SOURCE
    word it came from (start + head token). Mapping overwrites start/end
    with timeline seconds, so this is the only way to recognise the same
    word on the edl BEFORE the run and the edl AFTER it."""
    _, src = ctx.transcript()
    tagged = [{**w, "src_key": (round(float(w["start"]), 3), _head_token(w.get("word")))}
              for w in ctx.words_source()]
    return map_words_to_timeline(edl, "v1", tagged, src=src)


def _cue_first_word(cue: TextClip, words: list[dict]) -> dict | None:
    """The mapped word the cue's first token was made from: the nearest word
    with the same head token that starts within `_CUE_HEAD_LOOKBACK_S`
    before the cue or anywhere inside it; else (rewritten text) the first
    word at or after the cue's start. Same pairing as the benchmark's
    `measure._cue_first_word`, kept separate on purpose: the benchmark is
    the verifier's independent witness and must not import its ruler."""
    head = _head_token(cue.text)
    lo, hi = float(cue.start) - _CUE_HEAD_LOOKBACK_S, float(cue.end)
    inside = [w for w in words if lo <= float(w["start"]) <= hi]
    same = [w for w in inside if _head_token(w.get("word")) == head] if head else []
    if same:
        return min(same, key=lambda w: abs(float(w["start"]) - float(cue.start)))
    return next((w for w in words if float(w["start"]) >= float(cue.start) - _CUE_HEAD_SLACK_S), None)


def _seam_cue_words(edl: EDL, words: list[dict], tol: float) -> list[tuple[float, TextClip, dict]]:
    """Per v1 seam: the first cue at or after it (within `tol`) and that
    cue's OWN first word — or nothing for that seam when either is missing."""
    cues = caption_clips(edl)
    out: list[tuple[float, TextClip, dict]] = []
    for seam in (c.start for c in v1_clips(edl)[1:]):
        cue = next((c for c in cues if c.start >= seam - tol), None)
        word = _cue_first_word(cue, words) if cue is not None else None
        if cue is not None and word is not None:
            out.append((seam, cue, word))
    return out


def _cue_offsets(edl: EDL, words: list[dict]) -> dict[Any, float]:
    """`src_key → cue.start − word.start` (timeline seconds) for every
    caption cue on `edl`, keyed by the source word the cue leads with. The
    first cue to claim a word keeps it."""
    out: dict[Any, float] = {}
    for cue in caption_clips(edl):
        word = _cue_first_word(cue, words)
        if word is not None and word["src_key"] not in out:
            out[word["src_key"]] = float(cue.start) - float(word["start"])
    return out


def c_captions_sync(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """For the first cue after each v1 seam: how far the cue moved RELATIVE
    TO ITS OWN FIRST WORD during the run (spec §4.4, "captions stay in sync
    across cuts").

    WHY drift and not the raw cue-vs-word offset: the first version paired
    the first cue after a seam with the first WORD after the seam — two
    independent lookups — so on a transcript whose segment starts inside the
    preceding pause ("Um," 1.6 s before it is voiced on the benchmark
    fixture) every transitions-with-captions prompt reported "done with
    issues, captions_sync 1.63 s" although the re-lay had moved nothing:
    it measured whisper's segmentation, which no editor can change. The
    benchmark (`measure.caption_seam_deltas`, case 18) already measures the
    per-seam offset BEFORE and AFTER and calls the difference drift; the
    app's verdict must agree with it. A cue whose word had no cue before
    the run (captions laid fresh by this plan) has no baseline and is judged
    on the spec's literal claim — cue start within `tol` of its own first
    word — and the detail says so."""
    tol = float(_arg(pc, "tol") or 0.1)
    pairs = _seam_cue_words(ctx.edl, _words_keyed(ctx, ctx.edl), tol)
    if not pairs:
        return _ok(pc, None, None, f"±{tol}s", detail="no seam, cue or word to compare")
    baseline = _cue_offsets(ctx.edl_before, _words_keyed(ctx, ctx.edl_before))
    worst, moved, fresh = 0.0, [], 0
    for seam, cue, word in pairs:
        offset = float(cue.start) - float(word["start"])
        base = baseline.get(word["src_key"])
        delta = abs(offset - base) if base is not None else abs(offset)
        fresh += 1 if base is None else 0
        worst = max(worst, delta)
        if delta > tol:
            moved.append(f"seam {seam:.2f}s: cue {cue.start:.2f} vs its word {float(word['start']):.2f}"
                         + (f" (was {base:+.2f} before the run)" if base is not None else " (no cue before the run)"))
    held = [f"{len(pairs)} seam(s): cue-vs-word offset unchanged by the run"]
    if fresh:
        held.append(f"{fresh} cue(s) had no counterpart before the run and were judged on the absolute offset")
    return _ok(pc, worst <= tol, round(worst, 3), f"≤ {tol}", unit="s",
               detail="; ".join(moved or held))


def _script_ratios(text: str) -> dict[str, float]:
    counts = {"deva": 0, "latin": 0, "other": 0}
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:
            counts["deva"] += 1
        elif ch.isascii() or 0x00C0 <= cp <= 0x024F:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    total = sum(counts.values()) or 1
    return {k: v / total for k, v in counts.items()}


def c_captions_language(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    target = _arg(pc, "target")
    cues = caption_clips(ctx.edl)
    if not target or not cues:
        return _ok(pc, None, None, target, detail="no target or no captions")
    ratios = _script_ratios(" ".join(c.text for c in cues))
    target = str(target).lower()
    if target == "hi":
        return _ok(pc, ratios["deva"] >= 0.7, round(ratios["deva"], 3), "devanagari ≥ 70%", unit="ratio")
    if target == "hinglish":
        # QA-043: Devanagari delivered for a Hinglish request is a failure
        # whatever the source — it used to read "4/4 held".
        if ratios["deva"] >= 0.3:
            return _ok(pc, False, round(ratios["latin"], 3), "latin ≥ 90%", unit="ratio",
                       detail=f"{ratios['deva']:.0%} of the caption letters are Devanagari")
        spoken = ctx.facts_before.spoken_language or ctx.facts_before.language
        if not spoken:
            # The plan may have made the transcript itself (upload with
            # transcribe=false, then "add hinglish captions"): facts_before
            # predates it, so read the language the run persisted.
            tx, _src = ctx.transcript()
            spoken = getattr(tx, "language", None) if tx is not None else None
        spoken = base_lang(spoken) if spoken else None
        if spoken is None:
            return _ok(pc, None, round(ratios["latin"], 3), "latin ≥ 90% of a Hindi source",
                       detail="source language unknown; romanisation cannot be judged")
        if spoken != "hi":
            return _ok(pc, None, round(ratios["latin"], 3), "latin ≥ 90% of a Hindi source",
                       detail="source language is not Hindi; romanisation cannot be judged")
        return _ok(pc, ratios["latin"] >= 0.9, round(ratios["latin"], 3), "latin ≥ 90%", unit="ratio")
    return _ok(pc, ratios["latin"] >= 0.9, round(ratios["latin"], 3), "latin ≥ 90%", unit="ratio")


def c_captions_style(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    style = _arg(pc, "style")
    track = ctx.edl.get_track("captions")
    current = track.config.style if track and track.config else None
    n = len(caption_clips(ctx.edl))
    if style is None:
        return _ok(pc, None, current, None, detail="no style requested")
    if n == 0:
        # A style on an EMPTY track is not a measurement ("measured ig_chunky,
        # expected ig_chunky" with zero cues read as a pass) — say so.
        return _ok(pc, None, current, style, detail="no captions were laid, so the style cannot be judged")
    return _ok(pc, current == style, current, style)


def c_caption_look(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """The stored caption LOOK and every cue carry what was asked (Final QA:
    "make the captions yellow" re-laid white cues and still passed 4/4)."""
    track = ctx.edl.get_track("captions")
    cues = caption_clips(ctx.edl)
    if track is None or track.config is None or not cues:
        return _ok(pc, False, 0, "captions", unit="cues", detail="there are no captions")
    look = track.config.look
    want = {k: _arg(pc, k) for k in ("color", "size", "upper", "stroke_w", "background")}
    bad: list[str] = []
    for key, value in want.items():
        if value is None:
            continue
        stored = getattr(look, key, None) if look is not None else None
        same = (abs(float(stored) - float(value)) < 0.5) if key in ("size", "stroke_w") and stored is not None \
            else (str(stored).upper() == str(value).upper() if isinstance(value, str) else stored == value)
        if not same:
            bad.append(f"{key}={stored!r}")
            continue
        off = [c for c in cues if getattr(c.style, key, None) is not None and not (
            abs(float(getattr(c.style, key)) - float(value)) < 0.5 if key in ("size", "stroke_w")
            else (str(getattr(c.style, key)).upper() == str(value).upper() if isinstance(value, str)
                  else getattr(c.style, key) == value))]
        if off:
            bad.append(f"{len(off)} cue(s) without {key}")
    position = _arg(pc, "position")
    if position is not None and track.config.position != position:
        bad.append(f"position={track.config.position!r}")
    return _ok(pc, not bad, ", ".join(bad) or "as asked", "as asked",
               detail=None if not bad else "the look did not land on the captions")


def _fit_trim_source_start(ctx: VerifyCtx, src: str | None) -> float | None:
    """The source second the kept timeline STARTS on, when this run applied a
    best-window target-length trim (`$fit_best:`, wave C QA-069) — the words
    before it were trimmed as asked. None otherwise."""
    if _fit_trim_source_end(ctx, src) is None:
        return None
    from .schema import FIT_BEST_PREFIX
    if not any(s.tool == "cut_range" and str(s.args.get("start") or "").startswith(FIT_BEST_PREFIX)
               for s in list(getattr(ctx.plan, "steps", []) or [])):
        return None
    from ..timemap import media_clips
    t = ctx.edl.get_track("v1")
    clips = sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)
    same = {id(c) for c in media_clips(ctx.edl, "v1", src=src)} if src is not None else {id(c) for c in clips}
    if not clips or id(clips[0]) not in same:
        return None
    return float(clips[0].in_)


def _fit_trim_source_end(ctx: VerifyCtx, src: str | None) -> float | None:
    """The source second the kept timeline ends on, when this run applied a
    target-length trim (`cut_range(start="$fit_to:…")` or `"$fit_best:…"`,
    agent/prompt/live.py); None otherwise, or when the last v1 clip is not
    the transcript's file."""
    from .live import parse_fit_sentinel
    steps = list(getattr(ctx.plan, "steps", []) or [])
    ran = {o.index for o in getattr(ctx.exec_result, "steps", []) or []
           if getattr(o, "status", "") == "ok" and getattr(o, "args", None)}
    if not any(i in ran and s.tool == "cut_range" and parse_fit_sentinel(s.args.get("start")) is not None
               for i, s in enumerate(steps)):
        return None
    from ..timemap import media_clips
    t = ctx.edl.get_track("v1")
    clips = sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)
    # media_clips knows a derived file (denoise, reframe) is still that source.
    same = {id(c) for c in media_clips(ctx.edl, "v1", src=src)} if src is not None else {id(c) for c in clips}
    if not clips or id(clips[-1]) not in same:
        return None
    return float(clips[-1].out)


def c_speech_preserved(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Every non-filler word that was on the timeline BEFORE the run is still
    on it AFTER. Decided on SOURCE ranges through both edls (keyed by source
    time, §4.4) — the mapped timeline positions differ by construction."""
    fillers = _filler_set(ctx, None)
    _, src = ctx.transcript()

    def _present(edl: EDL, w: dict) -> bool:
        # Present = most of the word's SOURCE range still plays. "Any sliver"
        # would let a cut through the middle of a word pass; "every sample"
        # would fail a word whose edge lost 50 ms to a neighbouring filler's
        # pad (`remove_fillers(pad=0.05)`). 80% keeps both honest — and a
        # word whose END plays with ≥ 40% of its span counts too, because
        # whisper's start pads the voiced onset after a pause (see
        # `_WORD_TAIL_RATIO`); a cut anywhere in the tail still fails.
        s, e = float(w["start"]), float(w["end"])
        if e - s <= 0.0:
            return bool(source_range_to_timeline(edl, "v1", s, e, src=src))
        played = sum(max(0.0, b - a) for a, b in source_range_to_timeline(edl, "v1", s, e, src=src))
        if played >= _WORD_KEEP_RATIO * (e - s):
            return True
        tail = max(s, e - _WORD_END_SLACK_S)
        end_plays = bool(source_range_to_timeline(edl, "v1", tail, e, src=src))
        return end_plays and played >= _WORD_TAIL_RATIO * (e - s)

    before = [w for w in ctx.words_source()
              if _norm_token(w.get("word")) not in fillers and _present(ctx.edl_before, w)]
    if not before:
        return _ok(pc, None, None, "every kept word survives", detail="no words were on the timeline")
    gone = [w for w in before if not _present(ctx.edl, w)]
    # QA-069: a target-length trim ("make this a 30s reel") removes the tail
    # ON PURPOSE — those words are what was asked for, not lost speech. They
    # were reported as "✗ 87 words lost" on a run that did exactly its job.
    tail_src = _fit_trim_source_end(ctx, src)
    head_src = _fit_trim_source_start(ctx, src)
    trimmed = [w for w in gone if (tail_src is not None and float(w["start"]) >= tail_src - 0.05)
               or (head_src is not None and float(w["end"]) <= head_src + 0.05)]
    gone = [w for w in gone if not any(w is t for t in trimmed)]
    # Energy is ground truth, timestamps are estimates: a vanished word whose
    # source span sits inside a silent run of the SOURCE (measured with the
    # plan's own silencedetect settings) was never voiced there — the
    # transcript put it in the pause remove_silences rightly cut. Reported
    # separately, never counted (see `_WORD_IN_SILENCE_RATIO`). Only read
    # when something vanished, so a clean run costs no ffmpeg call.
    runs = ctx.source_silences() if gone else None
    misaligned = [w for w in gone if runs and _inside_silence(w, runs)]
    lost = [w for w in gone if not (runs and _inside_silence(w, runs))]
    parts: list[str] = []
    if lost:
        parts.append("lost: " + ", ".join(str(w.get("word")) for w in lost[:6]))
    if trimmed:
        parts.append(f"{len(trimmed)} words outside the kept target length were trimmed as asked — not counted")
    if misaligned:
        parts.append(f"{len(misaligned)} transcript words sat inside measured silence (misaligned timestamps)"
                     " — not counted: " + ", ".join(str(w.get("word")) for w in misaligned[:6]))
    return _ok(pc, not lost, len(lost), 0, unit="words lost", detail="; ".join(parts) or None)


def c_fillers_remaining_leq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    fillers = _filler_set(ctx, _arg(pc, "words"))
    cap = int(_arg(pc, "max") or 0)
    remaining = [w for w in ctx.words_on(ctx.edl) if _norm_token(w.get("word")) in fillers]
    return _ok(pc, len(remaining) <= cap, len(remaining), f"≤ {cap}", unit="fillers")


def _main_lane_len(edl: EDL | None, fallback: float) -> float:
    """How long the PICTURE runs on the main lane (layout seconds) — what a
    v1 cut / speed / duplicate / delete changes (Final QA). `edl.duration`
    is a max over every track: with a 40 s music bed under 15 s of video, a
    correct "delete clip 3" measured 13 s against an expected 36 s (the bed,
    trimmed to the new video on the ripple, dropped out of the maths) and
    reported "done with issues". A timeline with no main-lane clips keeps
    measuring `edl.duration`."""
    if edl is None:
        return fallback
    try:
        ve = float(edl.video_extent())
    except Exception:
        return fallback
    return ve if ve > 0 else fallback


def _durations(ctx: VerifyCtx) -> tuple[float, float]:
    before_edl = getattr(ctx.exec_result, "edl_before", None)
    before = _main_lane_len(before_edl, ctx.exec_result.duration_before)
    return before, _main_lane_len(ctx.edl, ctx.edl.duration)


def c_duration_shrank(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    before, after = _durations(ctx)
    min_ratio, min_seconds = _arg(pc, "min_ratio"), _arg(pc, "min_seconds")
    need = 0.01
    if min_ratio is not None:
        need = max(need, before * float(min_ratio))
    if min_seconds is not None:
        need = max(need, float(min_seconds))
    shrank = before - after
    return _ok(pc, shrank >= need, round(shrank, 3), f"≥ {need:.2f}", unit="s")


def c_duration_between(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    before, after = _durations(ctx)
    target, start, end, factor = (_arg(pc, k) for k in ("target", "start", "end", "factor"))
    if target is not None:
        expected = float(target)
    elif start is not None and end is not None:
        expected = before - (float(end) - float(start))
    elif factor:
        expected = before / float(factor)
    else:
        return _ok(pc, None, round(after, 3), None, detail="no target, range or factor given")
    tol = float(_arg(pc, "tol") or 0.1)
    if _arg(pc, "tol_ratio") is not None:
        tol = max(tol, expected * float(_arg(pc, "tol_ratio")))
    return _ok(pc, abs(after - expected) <= tol, round(after, 3), f"{expected:.2f} ± {tol:.2f}", unit="s")


def c_duration_leq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    cap = _arg(pc, "max")
    if cap is None:
        return _ok(pc, None, round(ctx.edl.duration, 3), None, detail="no max given")
    return _ok(pc, ctx.edl.duration <= float(cap), round(ctx.edl.duration, 3), f"≤ {float(cap):.2f}", unit="s")


def _need_render(ctx: VerifyCtx, pc: Postcondition) -> CheckResult | None:
    if ctx.render_path is None:
        return _ok(pc, None, None, None,
                   detail=ctx.render_skip_reason or "verify render unavailable")
    return None


def c_silence_total_leq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """No LONG pause remains in the SPEECH: the silent runs of a render with
    the music track muted, counting only those at least `_long_pause_floor`
    long (the plan's min_dur + 2×keep_pad + tolerance); passes when the long
    runs total ≤ `max_total_s`. Reports the longest remaining run so a
    failure names the pause. WHY runs, not the sum of all silence: after
    remove_silences the kept air alone is 2×keep_pad per cut pause and
    natural sub-min_dur breaths were never its job (`_LONG_PAUSE_TOL_S`).
    WHY the music is muted: on the full mix a −14 dB bed covers every pause
    and silencedetect finds nothing — the check was vacuously true whenever
    music was on the timeline (the TikTok run measured 0 s under a bed
    covering 100%)."""
    missing = _need_render(ctx, pc)
    if missing:
        return missing
    cap = float(_arg(pc, "max_total_s") or 1.0)
    path = ctx.render_path
    if music_clips(ctx.edl):
        path = ctx.speech_render()
        if path is None:
            return _ok(pc, None, None, f"≤ {cap}",
                       detail=ctx.speech_render_skip_reason or "music is on the timeline and no speech-only render exists")
    noise_db, min_dur, keep_pad = _silence_params(ctx)
    floor = _long_pause_floor(ctx)
    lengths = [e - s for s, e in silence_runs(path, noise_db=noise_db, min_dur=min_dur, end=ctx.edl.duration)]
    long_runs = [d for d in lengths if d >= floor]
    total = round(sum(long_runs), 3)
    longest = max(lengths, default=0.0)
    # Editor language (QA-101): the floor explained, never the argument names.
    n_long = len(long_runs)
    detail = (f"longest remaining pause {longest:.2f} s; {n_long} pause{'' if n_long == 1 else 's'} "
              f"of {floor:.2f} s or longer (shorter pauses are kept on purpose: the {min_dur:g} s "
              f"silence length plus {keep_pad:g} s of breathing room each side)")
    if music_clips(ctx.edl):
        detail += "; measured with the music track muted"
    return _ok(pc, total <= cap, total, f"≤ {cap}", unit="s", detail=detail)


def c_canvas_aspect(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    ratio = _arg(pc, "ratio")
    current = _D.canvas_aspect_name(ctx.edl.canvas.w, ctx.edl.canvas.h)
    if ratio is None:
        return _ok(pc, None, current, None, detail="no ratio requested")
    return _ok(pc, current == ratio, current, ratio)


def c_reframe_effective(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips = v1_clips(ctx.edl)
    if not clips:
        return _ok(pc, None, None, "reframed to fill the frame", detail="no clips on the main video track")
    before_src = {c.id: c.src for c in v1_clips(ctx.edl_before)}
    src_changed = any(before_src.get(c.id) not in (None, c.src) for c in clips)
    results = ctx.exec_result.results_for("auto_reframe")
    skipped = any("(skipped" in str(x) for r in results for x in (r.get("reframed") or []))
    all_cover = all(c.fit == "cover" for c in clips)
    passed = (bool(results) and not skipped and src_changed) or all_cover
    measured = {"src_changed": src_changed, "skipped": skipped, "all_cover": all_cover}
    return _ok(pc, passed, measured, "reframed to fill the frame")


def c_no_letterbox(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips = v1_clips(ctx.edl)
    if not clips:
        return _ok(pc, None, None, "every clip fills the canvas", detail="no clips on the main video track")
    canvas = ctx.edl.canvas.w / ctx.edl.canvas.h
    bad: list[str] = []
    unknown = 0
    for c in clips:
        if c.fit == "cover":
            continue
        dims = ctx.probe_dims(c.src)
        if dims is None:
            unknown += 1
            continue
        if abs(dims[0] / dims[1] - canvas) > canvas * _ASPECT_TOL:
            bad.append(c.id)
    if bad:
        return _ok(pc, False, len(bad), 0, unit="letterboxed clips", detail=", ".join(bad[:6]))
    if unknown:
        return _ok(pc, None, None, 0, detail=(f"{unknown} clip{'' if unknown == 1 else 's'} "
                                              "could not be measured"))
    return _ok(pc, True, 0, 0, unit="letterboxed clips")


def overlay_positions(edl: EDL) -> list[tuple[str, str, float, float]]:
    """(id, role, x_frac, y_frac) for every text/sticker overlay, resolved
    exactly as the renderer resolves them (`resolve_anchor_overrides` +
    `_y_for_role`). Watermarks live in the margin by design and are exempt."""
    from ...render.text_overlay import _y_for_role, resolve_anchor_overrides
    w, h = edl.canvas.w, edl.canvas.h
    out: list[tuple[str, str, float, float]] = []
    for t in edl.tracks:
        if t.type not in ("text", "captions", "sticker") or t.muted:
            continue
        for c in t.clips:
            if isinstance(c, TextClip):
                role = c.role or "default"
                if role == "watermark":
                    continue
                ax, ay = resolve_anchor_overrides(c, role, w, h)
                y = _y_for_role(role, ay, h, w)
                x = float(ax) if ax is not None else w / 2
                out.append((c.id, role, x / w, y / h))
            elif isinstance(c, Sticker):
                x = c.transform.x if isinstance(c.transform.x, (int, float)) else w / 2
                y = c.transform.y if isinstance(c.transform.y, (int, float)) else h / 2
                out.append((c.id, "sticker", float(x) / w, float(y) / h))
    return out


def c_overlays_inside_safe_zone(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    ratio = _arg(pc, "ratio") or _D.canvas_aspect_name(ctx.edl.canvas.w, ctx.edl.canvas.h)
    zone = _D._safe_zone(str(ratio))
    positions = overlay_positions(ctx.edl)
    if not positions:
        return _ok(pc, None, None, zone, detail="no overlays to place")
    outside = [f"{oid}({role} y={y:.2f} x={x:.2f})" for oid, role, x, y in positions
               if not (zone["y_min"] <= y <= zone["y_max"] and x <= zone["x_max"])]
    return _ok(pc, not outside, len(outside), 0, unit="overlays outside",
               detail=", ".join(outside[:6]) if outside else None)


def c_music_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips = music_clips(ctx.edl)
    track = ctx.edl.get_track("music")
    want_duck = _arg(pc, "ducked")
    count = _arg(pc, "count")          # the replace path: exactly one bed, not one on top of another
    ducked = bool(track and track.duck)
    if count is not None and int(count) == 0:
        # "remove the music": the check is an EMPTY music lane.
        return _ok(pc, not clips, {"clips": len(clips)}, {"clips": "= 0"})
    passed = bool(clips) and (not want_duck or ducked) and (count is None or len(clips) == int(count))
    return _ok(pc, passed, {"clips": len(clips), "ducked": ducked},
               {"clips": f"= {int(count)}" if count is not None else "≥ 1", "ducked": bool(want_duck) or "any"})


def c_music_ducked(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    to_db = float(_arg(pc, "to_db") if _arg(pc, "to_db") is not None else -12)
    track = ctx.edl.get_track("music")
    if _arg(pc, "enabled") is False:
        # QA-031: "turn off ducking" is verified as OFF — it used to be
        # measured against "on" whatever was asked.
        if not track or not music_clips(ctx.edl):
            return _ok(pc, None, None, "ducking off", detail="no music")
        return _ok(pc, track.duck is None, "on" if track.duck else "off", "off")
    if not track or not music_clips(ctx.edl):
        return _ok(pc, False, None, f"≤ {to_db} dB", detail="no music")
    if not track.duck:
        return _ok(pc, False, None, f"≤ {to_db} dB", detail="ducking off")
    # QA-079: measure the dip the renderer APPLIED, under the speech the
    # timeline actually has. Reading `to_db` back from the EDL "verified" a
    # −18 dB duck that the old fixed-threshold compressor never rendered.
    probe = ctx.duck_probe_render()
    if probe is None:
        return _ok(pc, track.duck.to_db <= to_db, track.duck.to_db, f"≤ {to_db}", unit="dB",
                   detail=f"setting only, dip not measured ({ctx.duck_probe_skip_reason})")
    measured, how = _measured_duck_db(ctx, probe)
    if measured is None:
        return _ok(pc, None, None, f"≤ {to_db}", unit="dB", detail=how)
    return _ok(pc, measured <= to_db + _DUCK_TOL_DB, round(measured, 1), f"≤ {to_db}", unit="dB",
               detail=f"rendered dip {how}")


#: Slack between the requested and the rendered dip (attack ramps, window edges).
_DUCK_TOL_DB = 1.5


def _measured_duck_db(ctx: VerifyCtx, probe: Path) -> tuple[float | None, str]:
    """(dip in dB, how it was measured) from the duck probe stem: the median
    50 ms window gain inside the speech the transcript places on the render
    clock, or — with no transcript — the deepest sustained tenth of the
    timeline."""
    import statistics
    from ...render import clock
    from ...render.audio_mix import DUCK_PROBE_AMPLITUDE
    from ...render.verify_render import window_levels_db
    win = 0.05
    gains = window_levels_db(probe, win_s=win, ref=DUCK_PROBE_AMPLITUDE / math.sqrt(2))
    if not gains:
        return None, "the duck probe render has no audio"
    spans: list[tuple[float, float]] = []
    tx, _ = ctx.transcript()
    if tx is not None:
        seams = clock.seam_table(ctx.edl)
        for s, e in ctx.speech_spans(ctx.edl):
            w = clock.render_window(seams, s, e)
            if w is not None:
                spans.append(w)
    if spans:
        # Skip each span's first 100 ms: the attack ramp is the ducker working.
        inside = [g for i, g in enumerate(gains)
                  if any(s + 0.1 <= i * win and (i + 1) * win <= e for s, e in spans)]
        if not inside:
            return None, "the transcript's speech is too short to measure a dip"
        return statistics.median(inside), "median over the transcript's speech"
    ordered = sorted(gains)
    return ordered[max(0, len(ordered) // 10 - 1)], "deepest tenth of the timeline (no transcript)"


def _music_render_spans(edl: EDL) -> list[tuple[float, float]]:
    """The music clips' `(start, end)` on the RENDER clock, exactly as the
    mix plays them (`schema.sound_render_windows`: a bed starts where its
    run's start plays, plays whole inside the programme, and is cut where
    its layout end maps when laid to or past v1's end — final QA, K1)."""
    from ...edl.schema import sound_render_windows
    wins = sound_render_windows(music_clips(edl), edl.v1_seam_table(), edl.video_extent())
    return list(wins.values())


def c_music_within_video_extent(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    # Both ends on the render clock: the bed plays whole across transitions,
    # the picture ends at `render_video_end` (layout vs layout passed a bed
    # that ran past the picture into black by the transitions' overlap).
    spans = _music_render_spans(ctx.edl)
    extent = ctx.edl.render_video_end()
    if not spans:
        return _ok(pc, None, None, f"≤ {extent:.2f}", detail="no music")
    last = max(e for _s, e in spans)
    return _ok(pc, last <= extent + _EXTENT_SLACK_S, round(last, 3), f"≤ {extent:.2f}", unit="s")


def c_music_covers(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    min_ratio = float(_arg(pc, "min_ratio") or 0.95)
    extent = ctx.edl.render_video_end() if ctx.edl.video_extent() > 0 else 0.0
    if extent <= 0:
        return _ok(pc, None, None, f"≥ {min_ratio:.0%}", detail="no video extent")
    covered = _span_total(_merge_spans([(s, min(extent, e)) for s, e in _music_render_spans(ctx.edl)],
                                       gap=0.0))
    ratio = covered / extent
    return _ok(pc, ratio >= min_ratio, round(ratio, 3), f"≥ {min_ratio:.0%}", unit="ratio")


def _clips_for_ref(ctx: VerifyCtx, ref: Any) -> list[Clip]:
    """v1 clips a fade/mute `clip_id` names at verify time: `$v1_first` /
    `$v1_last` the first / last clip, another sentinel (or None) every clip,
    a real id that clip (on any track)."""
    clips = v1_clips(ctx.edl)
    if ref == "$v1_first":
        return clips[:1]
    if ref == "$v1_last":
        return clips[-1:]
    if ref in ("$playhead", "$selected"):
        one = _ui_sentinel_clip(ctx, ref)
        return [one] if one is not None else clips
    if ref and ref not in CLIP_SENTINELS:
        hit = ctx.edl.get_clip(str(ref))
        return [hit[1]] if hit and isinstance(hit[1], Clip) else []
    return clips


def _fade_result(pc: Postcondition, clips: list, get_in, get_out) -> CheckResult:
    want_in, want_out = _arg(pc, "in_s"), _arg(pc, "out_s")
    if not clips:
        return _ok(pc, False, None, {"in": want_in, "out": want_out}, detail="no clip to fade")
    got = [{"in": round(float(get_in(c) or 0.0), 3), "out": round(float(get_out(c) or 0.0), 3)} for c in clips]
    ok = all((want_in is None or g["in"] >= float(want_in) - 1e-3)
             and (want_out is None or g["out"] >= float(want_out) - 1e-3) for g in got)
    return _ok(pc, ok, got[0] if len(got) == 1 else got,
               {k: f"≥ {v:g}s" for k, v in (("in", want_in), ("out", want_out)) if v is not None} or "any")


def c_video_fade_set(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    return _fade_result(pc, _clips_for_ref(ctx, _arg(pc, "clip_id")),
                        lambda c: getattr(c, "video_fade_in", 0.0), lambda c: getattr(c, "video_fade_out", 0.0))


def c_audio_fade_set(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    return _fade_result(pc, _clips_for_ref(ctx, _arg(pc, "clip_id")),
                        lambda c: c.audio.fade_in, lambda c: c.audio.fade_out)


def c_music_fade_set(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """The bed's first piece fades in ≥ `in_s`, its last piece out ≥ `out_s`."""
    clips = sorted(music_clips(ctx.edl), key=lambda c: c.start)
    want_in, want_out = _arg(pc, "in_s"), _arg(pc, "out_s")
    if not clips:
        return _ok(pc, False, None, {"in": want_in, "out": want_out}, detail="no music")
    got = {"in": round(float(clips[0].audio.fade_in or 0.0), 3), "out": round(float(clips[-1].audio.fade_out or 0.0), 3)}
    ok = (want_in is None or got["in"] >= float(want_in) - 1e-3) and (want_out is None or got["out"] >= float(want_out) - 1e-3)
    return _ok(pc, ok, got, {k: f"≥ {v:g}s" for k, v in (("in", want_in), ("out", want_out)) if v is not None} or "any")


def c_volume_db(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Every clip of the `target` track (or the one clip) carries `db` of gain
    — what `render/audio_mix` applies."""
    target, db = _arg(pc, "target"), _arg(pc, "db")
    tol = float(_arg(pc, "tol") or 0.05)
    track = ctx.edl.get_track(str(target)) if target else None
    if track is not None:
        clips = [c for c in track.clips if isinstance(c, Clip)]
    else:
        hit = ctx.edl.get_clip(str(target)) if target else None
        clips = [hit[1]] if hit and isinstance(hit[1], Clip) else []
    if not clips:
        return _ok(pc, False, None, db, unit="dB", detail=f"nothing to measure on {target!r}")
    gains = sorted({round(float(c.audio.gain_db), 2) for c in clips})
    if db is None:
        return _ok(pc, None, gains, None, unit="dB", detail="no level requested")
    ok = all(abs(g - float(db)) <= tol for g in gains)
    return _ok(pc, ok, gains[0] if len(gains) == 1 else gains, float(db), unit="dB")


def c_track_muted(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    track_id, want = _arg(pc, "track"), _arg(pc, "muted")
    track = ctx.edl.get_track(str(track_id)) if track_id else None
    if track is None:
        return _ok(pc, False, None, want, detail=f"no track {track_id!r}")
    if want is None:
        return _ok(pc, None, bool(track.muted), None, detail="no state requested")
    return _ok(pc, bool(track.muted) == bool(want), bool(track.muted), bool(want))


def c_clips_muted(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips = _clips_for_ref(ctx, _arg(pc, "clip_id"))
    want = _arg(pc, "muted")
    if not clips:
        return _ok(pc, False, None, want, detail="no clip")
    states = [bool(c.audio.mute) for c in clips]
    if want is None:
        return _ok(pc, None, states, None, detail="no state requested")
    return _ok(pc, all(st == bool(want) for st in states), states[0] if len(set(states)) == 1 else states, bool(want))


def c_clip_reversed(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """QA-037: every clip `clip_id` names carries `reverse` as asked — the
    flag render/reverse.py reads (the render test decodes the frames)."""
    clips = _clips_for_ref(ctx, _arg(pc, "clip_id"))
    want = _arg(pc, "reverse")
    want = True if want is None else bool(want)
    if not clips:
        return _ok(pc, False, None, want, detail="no clip")
    states = [bool(c.reverse) for c in clips]
    return _ok(pc, all(st == want for st in states), states[0] if len(set(states)) == 1 else states, want)


def c_clip_zoomed(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Review RD3: the clip(s) `clip_id` names end up zoomed the way asked —
    IN: the scale (a static level, or the last key of a push) above 1; OUT:
    below the start (a push out) or below 1 (a level)."""
    from ...edl.keyframes import is_keyframed
    clips = _clips_for_ref(ctx, _arg(pc, "clip_id"))
    want = str(_arg(pc, "direction") or "in")
    if not clips:
        return _ok(pc, False, None, want, detail="no clip")
    got = []
    for c in clips:
        v = c.transform.scale
        if is_keyframed(v):
            keys = sorted((float(t), float(x)) for t, x in v.keyframes)
            first, last = keys[0][1], keys[-1][1]
        else:
            first, last = 1.0, float(v)
        got.append(round(last, 3))
        ok = last > first + 1e-6 if want == "in" else last < first - 1e-6
        if not ok:
            return _ok(pc, False, got[0] if len(clips) == 1 else got, f"zoom {want}")
    return _ok(pc, True, got[0] if len(got) == 1 else got, f"zoom {want}")


def c_beat_splits_geq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    n = int(_arg(pc, "n") or 2)
    delta = max(0, len(v1_clips(ctx.edl)) - 1) - max(0, len(v1_clips(ctx.edl_before)) - 1)
    return _ok(pc, delta >= n, delta, f"≥ {n}", unit="new boundaries")


def c_min_shot_geq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    seconds = float(_arg(pc, "seconds") or 0.8)
    clips = v1_clips(ctx.edl)
    if not clips:
        return _ok(pc, None, None, f"≥ {seconds}", detail="no v1 clips")
    shortest = min(c.effective_duration for c in clips)
    return _ok(pc, shortest >= seconds, round(shortest, 3), f"≥ {seconds}", unit="s")


def c_beat_pulse_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    from ...edl.schema import Keyframe
    n = int(_arg(pc, "n") or 2)
    music = music_clips(ctx.edl)
    if not music:
        return _ok(pc, None, None, f"≥ {n}", detail="no music bed to detect beats on")
    try:
        from ...ingest.beats import detect_beats
        beats = [float(b) + music[0].start - music[0].in_ for b in detect_beats(Path(music[0].src))]
    except Exception as e:  # noqa: BLE001 — librosa or the file may be unavailable
        return _ok(pc, None, None, f"≥ {n}", detail=f"beat detection unavailable: {e}")
    hits = 0
    for c in v1_clips(ctx.edl):
        kf = c.transform.scale
        if isinstance(kf, Keyframe) and len(kf.keyframes) >= 2:
            t0 = c.start + float(kf.keyframes[0][0])
            if any(abs(t0 - b) <= _BEAT_TOL_S for b in beats):
                hits += 1
    return _ok(pc, hits >= n, hits, f"≥ {n}", unit="pulses on beats")


def c_hook_text_starts_leq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    t = float(_arg(pc, "t") if _arg(pc, "t") is not None else 0.5)
    hooks = [c for c in text_clips(ctx.edl) if c.role == "hook" and not _is_caption_track_hook(ctx.edl, c)]
    if not hooks:
        return _ok(pc, False, None, f"≤ {t}", detail="no hook text")
    first = min(c.start for c in hooks)
    return _ok(pc, first <= t, round(first, 3), f"≤ {t}", unit="s")


def _is_caption_track_hook(edl: EDL, clip: TextClip) -> bool:
    """word_emphasis captions use role=hook on the captions track; they are
    captions, not the hook."""
    cap = edl.get_track("captions")
    return bool(cap) and any(c is clip for c in cap.clips)


def c_hook_axes_geq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    from ...show.audit import hook_axes
    n = int(_arg(pc, "n") or 3)
    axes = hook_axes(ctx.edl)
    return _ok(pc, axes["hook_score"] >= n, axes["hook_score"], f"≥ {n}", unit="axes",
               detail=("missing: " + ", ".join(axes["missing"])) if axes["missing"] else None)


def c_text_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    contains, start_geq, role = _arg(pc, "contains"), _arg(pc, "start_geq"), _arg(pc, "role")
    clips = [c for c in text_clips(ctx.edl) if c.role != "caption"]
    if role:
        clips = [c for c in clips if c.role == role]
    if contains:
        clips = [c for c in clips if str(contains).lower() in c.text.lower()]
    if start_geq is not None:
        clips = [c for c in clips if c.start >= float(start_geq)]
    return _ok(pc, len(clips) >= 1, len(clips), "≥ 1", unit="text clips",
               detail=f"contains={contains!r} role={role!r} start≥{start_geq}" if not clips else None)


def c_text_style_is(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """K3: the text `clip_id` names carries the look asked (set_text_style)."""
    cid = _arg(pc, "clip_id")
    t = next((c for c in text_clips(ctx.edl) if c.id == cid), None)
    if t is None:
        return _ok(pc, False, None, "the text", detail=f"text {cid} is gone")
    bad: list[str] = []
    color, size, font, bold, place = (_arg(pc, k) for k in ("color", "size", "font", "bold", "position"))
    if color and t.style.color.upper()[:7] != str(color).upper()[:7]:
        bad.append(f"colour {t.style.color}")
    if size is not None and abs(float(t.style.size) - float(size)) > 0.5:
        bad.append(f"size {t.style.size:g}")
    if font and (t.style.font or "") != str(font).rsplit(".", 1)[0] and (t.style.font or "") != str(font):
        bad.append(f"font {t.style.font}")
    if bold and not any(k in (t.style.font or "Inter-Bold") for k in ("Black", "Anton", "Bebas", "Bold")):
        bad.append("not bold")
    if place:
        frac = float(t.transform.y) / float(ctx.edl.canvas.h) if not hasattr(t.transform.y, "keyframes") else 0.5
        want = {"top": (0.0, 0.34), "middle": (0.34, 0.66), "center": (0.34, 0.66), "bottom": (0.66, 1.0)}[place]
        if not want[0] <= frac <= want[1]:
            bad.append(f"at y {frac:.2f}")
    for side in ("anim_in", "anim_out"):
        # run 4: "fade the title in" — the text's own In / Out animation
        want_anim = _arg(pc, side)
        if want_anim is not None and (getattr(t, side, None) or "") != str(want_anim):
            bad.append(f"{side.replace('_', ' ')} {getattr(t, side, None) or 'none'}")
    return _ok(pc, not bad, ", ".join(bad) or "as asked", "as asked")


def c_brand_watermark_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    n = sum(1 for c in text_clips(ctx.edl) if c.role == "watermark")
    return _ok(pc, n >= 1, n, "≥ 1", unit="watermarks")


def c_brand_kit_set(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    handle = ctx.edl.brand_kit.handle if ctx.edl.brand_kit else None
    return _ok(pc, bool(handle), handle, "a handle")


def c_vo_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    t = ctx.edl.get_track("vo")
    n = sum(1 for c in (t.clips if t else []) if isinstance(c, Clip))
    return _ok(pc, n >= 1, n, "≥ 1", unit="voiceover clips")


def c_effect_present(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    etype, track_id, want_all = _arg(pc, "type"), _arg(pc, "track") or "v1", _arg(pc, "all")
    cid = _arg(pc, "clip_id")
    if isinstance(cid, str) and cid and cid != "$v1_all" and not cid.startswith("$ask:"):
        # Final QA r2: the clip the step named, not every clip on the lane.
        ids = _clip_targets(ctx, cid)
        found = [ctx.edl.get_clip(x) for x in ids]
        have = [r for r in found if r and isinstance(r[1], Clip)
                and any((e.type == etype) if etype else True for e in r[1].effects)]
        return _ok(pc, bool(ids) and len(have) == len(ids), f"{len(have)}/{len(ids)}", "the named clip",
                   unit="clips")
    t = ctx.edl.get_track(str(track_id))
    clips = [c for c in (t.clips if t else []) if isinstance(c, Clip)]
    if not clips:
        return _ok(pc, None, None, etype, detail=f"no clips on {track_id}")
    have = [c for c in clips if any(e.type == etype for e in c.effects)] if etype else \
           [c for c in clips if c.effects]
    passed = len(have) == len(clips) if want_all in (None, True) else len(have) >= 1
    return _ok(pc, passed, f"{len(have)}/{len(clips)}", "all" if want_all in (None, True) else "≥ 1",
               unit="clips")


def c_clip_src_changed(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    before = {c.id: c.src for c in v1_clips(ctx.edl_before)}
    before_srcs = set(before.values())

    def _changed(cid: str) -> bool:
        res = ctx.edl.get_clip(cid)
        if not (res and isinstance(res[1], Clip)):
            return False
        if cid in before:
            return before[cid] != res[1].src
        # A piece a cut earlier in the run split off (new id): re-rendered when
        # its file is none of the files v1 played before the run. Without
        # this, a trim that removed the ORIGINAL first piece (the best-window
        # reel's head cut, QA-069) left no pre-run id to compare, and a clean
        # noise_reduce read as "0 clips re-rendered".
        return res[1].src not in before_srcs

    changed = [cid for cid in _clip_targets(ctx, _arg(pc, "clip_id")) if _changed(cid)]
    return _ok(pc, len(changed) >= 1, len(changed), "≥ 1", unit="clips re-rendered")


def c_speed_equals(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    factor = _arg(pc, "factor")
    preset = _arg(pc, "preset")
    if factor is None and preset is not None:
        # A curve by name (lane S2): the stored curve carries the preset id
        # only while its points are exactly the preset's (speed_presets).
        from ...edl.speed_presets import preset_id
        want = preset_id(preset) or str(preset)
        names = {cid: (res[1].speed.get("name") if isinstance(res[1].speed, dict) else None)
                 for cid in _clip_targets(ctx, _arg(pc, "clip_id"))
                 if (res := ctx.edl.get_clip(cid)) and isinstance(res[1], Clip)}
        if not names:
            return _ok(pc, False, None, want, detail="clip not found")
        shown = next(iter(names.values())) if len(set(names.values())) == 1 else names
        return _ok(pc, all(v == want for v in names.values()), shown, want)
    if factor is None:
        return _ok(pc, None, None, None, detail="no factor given")
    targets = _clip_targets(ctx, _arg(pc, "clip_id"))
    speeds = {cid: res[1].speed_factor for cid in targets
              if (res := ctx.edl.get_clip(cid)) and isinstance(res[1], Clip)}
    if not speeds:
        return _ok(pc, False, None, float(factor), detail="clip not found")
    passed = all(abs(v - float(factor)) < 1e-6 for v in speeds.values())
    shown = next(iter(speeds.values())) if len(set(speeds.values())) == 1 else speeds
    return _ok(pc, passed, shown, float(factor), unit="×")


def c_freeze_held(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """A freeze frame on the main lane (lane S2): the longest hold, or the
    named clip's, is at least `duration` (default: any hold at all)."""
    want = _arg(pc, "duration")
    tol = float(_arg(pc, "tol") or 0.05)
    cid = _arg(pc, "clip_id")
    holds = [float(c.freeze) for c in v1_clips(ctx.edl)
             if getattr(c, "freeze", None) is not None and (not cid or cid in CLIP_SENTINELS or c.id == cid)]
    if not holds:
        return _ok(pc, False, 0.0, want if want is not None else "> 0", unit="s", detail="no freeze frame")
    longest = max(holds)
    if want is None:
        return _ok(pc, True, longest, "> 0", unit="s")
    return _ok(pc, longest >= float(want) - tol, longest, float(want), unit="s")


def c_transitions_count_geq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    n, ttype = int(_arg(pc, "n") or 1), _arg(pc, "type")
    t = ctx.edl.get_track("v1")
    trs = [tr for tr in (t.transitions if t else []) if not ttype or tr.type == ttype]
    return _ok(pc, len(trs) >= n, len(trs), f"≥ {n}", unit="transitions")


# ---- wave E (F4b): edits by name --------------------------------------------------

def c_transitions_absent(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """No transition on v1 at `at` (within the seam tolerance), or none at
    all when `at` is not given — what "remove the transition(s)" asked."""
    at = _arg(pc, "at")
    t = ctx.edl.get_track("v1")
    trs = list(t.transitions if t else [])
    if at is not None:
        trs = [tr for tr in trs if abs(float(tr.at) - float(at)) < 0.05]
    return _ok(pc, not trs, len(trs), 0, unit="transitions",
               detail=None if at is None else f"at {float(at):g}s")


def c_clips_absent(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    ids = _arg(pc, "clip_ids") or []
    ids = [ids] if isinstance(ids, str) else list(ids)
    if not ids:
        return _ok(pc, None, None, 0, detail="no clips named")
    left = [cid for cid in ids if ctx.edl.get_clip(str(cid))]
    return _ok(pc, not left, len(left), 0, unit="clips left")


def _named_clips(ctx: VerifyCtx, ref: Any) -> list[Clip]:
    """Media clips an id, a list of ids, or a sentinel names."""
    if isinstance(ref, (list, tuple)):
        hits = [ctx.edl.get_clip(str(r)) for r in ref]
        return [h[1] for h in hits if h and isinstance(h[1], Clip)]
    return _clips_for_ref(ctx, ref)


def c_effect_absent(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    types = _arg(pc, "types") or ["lut"]
    types = {types} if isinstance(types, str) else set(types)
    clips = _named_clips(ctx, _arg(pc, "clip_id"))
    if not clips:
        return _ok(pc, False, None, 0, detail="no clip to measure")
    left = sum(1 for c in clips for e in c.effects if e.type in types)
    return _ok(pc, left == 0, left, 0, unit=f"{'/'.join(sorted(types))} effects left")


def c_clip_duration(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    want, tol = _arg(pc, "seconds"), float(_arg(pc, "tol") or 0.02)
    clips = _named_clips(ctx, _arg(pc, "clip_id"))
    if len(clips) != 1 or want is None:
        return _ok(pc, False, None, want, unit="s", detail="needs one clip and a length")
    got = round(float(clips[0].effective_duration), 4)
    return _ok(pc, abs(got - float(want)) <= tol, got, float(want), unit="s")


def c_clip_flipped(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips: list[Any] = _named_clips(ctx, _arg(pc, "clip_id"))
    if not clips and isinstance(_arg(pc, "clip_id"), str):
        # a sticker (Final QA r3): flip_clip mirrors stickers too
        hit = ctx.edl.get_clip(str(_arg(pc, "clip_id")))
        clips = [hit[1]] if hit and hasattr(hit[1], "transform") else []
    want = {k: _arg(pc, k) for k in ("flip_h", "flip_v") if _arg(pc, k) is not None}
    if not clips:
        return _ok(pc, False, None, want, detail="no clip")
    got = [{k: bool(getattr(c.transform, k, False)) for k in want} for c in clips]
    ok = all(g[k] == bool(v) for g in got for k, v in want.items())
    return _ok(pc, ok, got[0] if len(got) == 1 else got, want)


def c_export_preset_applied(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    name = _arg(pc, "name")
    preset = _D._EXPORT_PRESETS.get(str(name).lower()) if name else None
    canvas = ctx.edl.canvas
    measured = {"w": canvas.w, "h": canvas.h, "bitrate_kbps": canvas.bitrate_kbps,
                "lufs": canvas.loudness_lufs}
    if preset is None:
        return _ok(pc, None, measured, name, detail="unknown or missing preset name")
    passed = (canvas.w, canvas.h) == (preset["w"], preset["h"]) and \
        canvas.bitrate_kbps == preset["bitrate_kbps"] and canvas.loudness_lufs == preset["lufs"]
    return _ok(pc, passed, measured, {k: preset[k] for k in ("w", "h", "bitrate_kbps", "lufs")})


def c_loudness_target_set(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    lufs = _arg(pc, "lufs")
    current = ctx.edl.canvas.loudness_lufs
    if lufs is None:
        return _ok(pc, current is not None, current, "a target")
    return _ok(pc, current is not None and abs(float(current) - float(lufs)) < 1e-6, current, float(lufs),
               unit="LUFS")


def c_loudness_within(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    missing = _need_render(ctx, pc)
    if missing:
        return missing
    target = ctx.edl.canvas.loudness_lufs
    if target is None:
        return _ok(pc, None, None, None, detail="no loudness target set")
    from ...render.verify_render import integrated_loudness
    tol = float(_arg(pc, "tol") or 1.0)
    measured = integrated_loudness(ctx.render_path)
    if measured is None:
        return _ok(pc, None, None, f"{target} ± {tol}", detail="no measurable audio in the render")
    return _ok(pc, abs(measured - float(target)) <= tol, round(measured, 2), f"{target} ± {tol}", unit="LUFS")


def c_shorts_created(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    count, max_dur, min_dur = _arg(pc, "count"), _arg(pc, "max_dur"), _arg(pc, "min_dur")
    sessions = list(ctx.exec_result.new_sessions)
    if not sessions:
        return _ok(pc, False, 0, count or "≥ 1", unit="sessions")
    durations: list[float] = []
    for sid in sessions:
        child = ctx.child_store(sid)
        durations.append(round(child.edl.video_extent(), 2) if child else -1.0)
    ok_count = len(sessions) == int(count) if count is not None else True
    lo = float(min_dur) - 0.5 if min_dur is not None else 0.0
    hi = float(max_dur) + 0.5 if max_dur is not None else float("inf")
    ok_dur = all(lo <= d <= hi for d in durations)
    return _ok(pc, ok_count and ok_dur, {"sessions": len(sessions), "durations": durations},
               {"sessions": count, "duration": {"min": round(lo, 2),
                                                "max": round(hi, 2) if hi != float("inf") else None,
                                                "unit": "s"}})


def c_shorts_finished(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    sessions = list(ctx.exec_result.new_sessions)
    if not sessions:
        return _ok(pc, False, 0, "every short", detail="no shorts were created")
    unfinished: list[str] = []
    for sid in sessions:
        child = ctx.child_store(sid)
        if child is None:
            unfinished.append(f"{sid} (missing)")
            continue
        e = child.edl
        has_caps = bool(caption_clips(e))
        has_hook = any(c.role == "hook" and c.start <= 0.5 for c in text_clips(e)
                       if not _is_caption_track_hook(e, c))
        vertical = _D.canvas_aspect_name(e.canvas.w, e.canvas.h) == "9:16"
        if not (has_caps and has_hook and vertical):
            unfinished.append(f"{sid} captions={has_caps} hook={has_hook} 9:16={vertical}")
    return _ok(pc, not unfinished, len(sessions) - len(unfinished), len(sessions), unit="finished",
               detail="; ".join(unfinished) if unfinished else None)


def c_audit_ok(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    from ...show.audit import audit
    a = audit(ctx.edl.model_copy(deep=True))
    errors = [i["key"] for i in a["issues"] if i["level"] == "error"]
    passed = bool(a["ok"]) and not errors and a["hook"]["hook_score"] == 3
    return _ok(pc, passed, {"score": a["score"], "errors": errors, "hook_score": a["hook"]["hook_score"]},
               {"errors": 0, "hook_score": 3}, detail="score is reported, not gated")


def c_tool_ok(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    tool = _arg(pc, "tool")
    outcomes = ctx.exec_result.outcomes_for(tool) if tool else list(ctx.exec_result.steps)
    if not outcomes:
        return _ok(pc, None, None, "ok", detail=f"no step for {tool}" if tool else "no steps ran")
    statuses = [o.status for o in outcomes]
    return _ok(pc, all(s == "ok" for s in statuses), statuses if len(statuses) > 1 else statuses[0], "ok")


def _clips_for_refs(ctx: VerifyCtx, ref: Any) -> list[Clip]:
    """`_clips_for_ref`, for one reference or a list of ids."""
    if isinstance(ref, (list, tuple)):
        out: list[Clip] = []
        for r in ref:
            out += _clips_for_ref(ctx, r)
        return out
    return _clips_for_ref(ctx, ref)


def _named_targets(ctx: VerifyCtx, pc: Postcondition, *, stickers: bool = False) -> list | None:
    """What a wave-E step names, in the order the tools read it: `clip_id`,
    then `clip_ids`, then every clip (and, with `stickers`, sticker) on
    `track`. None when nothing is named (review RE: a model plan's
    `clip_ids` / `track` used to verify against nothing, or every v1 clip)."""
    from ...edl.schema import Sticker
    ref = _arg(pc, "clip_id")
    if ref is None:
        ref = _arg(pc, "clip_ids")
    if ref is not None:
        refs = list(ref) if isinstance(ref, (list, tuple)) else [ref]
        out: list = []
        for r in refs:
            hit = ctx.edl.get_clip(str(r)) if r and not str(r).startswith("$") else None
            out += [hit[1]] if hit else _clips_for_ref(ctx, r)
        return out
    track = _arg(pc, "track")
    if track is not None:
        t = ctx.edl.get_track(str(track))
        kinds = (Clip, Sticker) if stickers else (Clip,)
        return [c for c in (t.clips if t else []) if isinstance(c, kinds)]
    return None


def c_canvas_bg_set(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Wave E (F2): every clip `clip_id` names (default: every main-track
    clip) carries the CapCut canvas background asked for — the field the v1
    chain's contain branch reads (render/canvas_bg.py; the render tests
    decode the pixels). `type` None / "none" = black bars."""
    named = _named_targets(ctx, pc)
    clips = v1_clips(ctx.edl) if named is None else named
    kind = _arg(pc, "type")
    kind = None if kind in (None, "none", "") else str(kind)
    want: dict[str, Any] = {"type": kind}
    col, blur = _arg(pc, "color"), _arg(pc, "blur")
    if kind == "color" and col is not None:
        from ...edl.canvas_blend import normalize_color
        want["color"] = normalize_color(col)
    if kind == "blur" and blur is not None:
        want["blur"] = int(blur)
    if not clips:
        return _ok(pc, False, None, want, detail="no clip")
    got = []
    for c in clips:
        bg = getattr(c, "canvas_bg", None)
        g: dict[str, Any] = {"type": None if bg is None else bg.type}
        if bg is not None and "color" in want:
            g["color"] = bg.color
        if bg is not None and "blur" in want:
            g["blur"] = bg.blur
        got.append(g)
    ok = all(g == want for g in got)
    return _ok(pc, ok, got[0] if len(got) == 1 else got, want)


def c_voice_effect_is(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Wave E (F3): every clip `clip_id` names (one id or a list), or every
    clip on `track`, carries the voice effect asked for (None = none) and,
    when asked, its intensity — the fields the audio chains read
    (render/audio_mix.voice_filters; tests/test_voice_effects_render.py
    decodes the sound)."""
    named = _named_targets(ctx, pc) or []
    want = _arg(pc, "effect")
    if _arg(pc, "clip_id") is None and _arg(pc, "clip_ids") is None and want not in (None, "", "none"):
        # a whole lane: the clips with sound (dispatch skips a freeze and a
        # picture-only source)
        from ...render.compositor import source_has_audio
        named = [c for c in named if c.freeze is None and source_has_audio(str(c.src))]
    clips = named
    want = None if want in (None, "", "none") else str(want)
    inten = _arg(pc, "intensity")
    if not clips:
        return _ok(pc, False, None, want, detail="no clip")
    got = [c.audio.voice_effect for c in clips]
    ok = all(g == want for g in got)
    if ok and want is not None and inten is not None:
        ok = all(abs(float(c.audio.voice_intensity) - float(inten)) < 1e-6 for c in clips)
    return _ok(pc, ok, got[0] if len(set(got)) == 1 else got, want)


def c_animation_is(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Wave E (F1): every clip / sticker `clip_id` names (one id, a list or a
    v1 sentinel) carries the clip animation asked for — the fields
    render/compositor.py, pip.py and text_overlay.py read. A side given as a
    preset id must be that preset, "none" must be off; a side not given (None)
    is not checked."""
    named = _named_targets(ctx, pc, stickers=True)
    objs: list = _clips_for_ref(ctx, None) if named is None else named
    want = {k: _arg(pc, k) for k in ("in", "out", "combo")}
    want = {k: (None if str(v) == "none" else str(v)) for k, v in want.items()
            if v is not None and not str(v).startswith("$")}
    if not objs:
        return _ok(pc, False, None, want, detail="no clip or sticker")
    got = [{k: getattr(o, f"anim_{k}", None) for k in ("in", "out", "combo")} for o in objs]
    ok = bool(want) and all(g[k] == v for g in got for k, v in want.items())
    return _ok(pc, ok, got[0] if len(got) == 1 else got, want or "any")


def c_blend_is(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    """Wave E (F2): the overlay clip(s) named blend with `mode` — the field
    render/pip.py composites with."""
    want = _arg(pc, "mode")
    clips = _named_targets(ctx, pc) or []
    if not clips:
        return _ok(pc, False, None, want, detail="no overlay clip")
    got = [str(getattr(c, "blend", "normal")) for c in clips]
    if want is None:
        return _ok(pc, None, got, None, detail="no mode requested")
    return _ok(pc, all(g == want for g in got), got[0] if len(got) == 1 else got, want)


CHECKS: dict[str, Callable[[VerifyCtx, Postcondition], CheckResult]] = {
    name[2:]: fn for name, fn in globals().items()
    if name.startswith("c_") and callable(fn)
}


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def _postconditions(plan: Plan) -> list[Postcondition]:
    """The plan's postconditions, or the natural defaults per step when a
    (raw-tool) plan carries none — every executed plan is verified (§1.1)."""
    if plan.postconditions:
        return list(plan.postconditions)
    out: list[Postcondition] = []
    for step in plan.steps:
        out.extend(bind_postconditions(step.tool, step.args))
    return out


def run_check(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    fn = CHECKS.get(pc.check)
    if fn is None:
        return CheckResult(check=pc.check, human=pc.human, passed=None,
                           detail="unknown check", headline=pc.headline)
    unresolved = sorted(k for k, v in pc.args.items() if isinstance(v, str) and v.startswith(("$ask:", "$arg:")))
    if unresolved:
        # QA-032: a check still holding a placeholder measures nothing real —
        # `text_present(contains="$ask:handle")` passed against the burned-in
        # placeholder itself. It fails, loudly.
        return CheckResult(check=pc.check, human=pc.human, passed=False,
                           measured={k: pc.args[k] for k in unresolved}, expected="a real value",
                           detail="unresolved placeholder — the value was never supplied", headline=pc.headline)
    try:
        return fn(ctx, pc)
    except Exception as e:  # noqa: BLE001 — a broken check is reported, never fatal
        return CheckResult(check=pc.check, human=pc.human, passed=None,
                           detail=f"could not measure: {type(e).__name__}: {e}", headline=pc.headline)


def verify_plan(store: EDLStore, plan: Plan, exec_result: Any, facts_before: TimelineFacts, *,
                emit: Emit, cancel_event: threading.Event | None = None,
                store_resolver: Callable[[str], EDLStore] | None = None,
                render: bool = True) -> dict[str, Any]:
    """Measure every postcondition of `plan` on `store`. Returns the `verify`
    event payload (minus `type`): `{plan_id, checks, passed, total,
    unmeasured, rendered}` where `total` counts headline checks that could be
    measured and `passed` those that held."""
    pcs = _postconditions(plan)
    ctx = VerifyCtx(store=store, plan=plan, exec_result=exec_result, facts_before=facts_before,
                    store_resolver=store_resolver, cancel_event=cancel_event, render_allowed=render)
    total_steps = len(plan.steps)
    if render and any(pc.needs_render for pc in pcs):
        from ...render.verify_render import render_for_verify
        emit({"type": "step", "index": total_steps, "total": total_steps, "tool": "verify_render",
              "status": "running", "progress": 0.0})
        try:
            ctx.render_path = render_for_verify(
                store.edl, Path(store.dir), max_duration_s=VERIFY_RENDER_MAX_DURATION_S,
                on_progress=lambda p: emit({"type": "step", "index": total_steps, "total": total_steps,
                                            "tool": "verify_render", "status": "running",
                                            "progress": float(p)}),
                cancel_event=cancel_event)
            if ctx.render_path is None:
                ctx.render_skip_reason = (f"verify render skipped: timeline is "
                                          f"{store.edl.duration:.0f}s (> {VERIFY_RENDER_MAX_DURATION_S:.0f}s)")
        except Exception as e:  # noqa: BLE001 — a failed verify render is an unmeasured check
            ctx.render_skip_reason = f"verify render failed: {type(e).__name__}: {e}"
        emit({"type": "step", "index": total_steps, "total": total_steps, "tool": "verify_render",
              "status": "ok" if ctx.render_path else "skipped", "progress": 1.0,
              "summary": ctx.render_skip_reason or "rendered"})
    elif any(pc.needs_render for pc in pcs):
        ctx.render_skip_reason = "verify render disabled"

    checks = [run_check(ctx, pc) for pc in pcs]
    headline = [c for c in checks if c.headline and c.passed is not None]
    return {
        "plan_id": plan.id,
        "checks": [c.as_dict() for c in checks],
        "passed": sum(1 for c in headline if c.passed),
        "total": len(headline),
        "unmeasured": sum(1 for c in checks if c.passed is None),
        "rendered": ctx.render_path is not None,
    }


def blocking_failures(store: EDLStore, plan: Plan, exec_result: Any, facts_before: TimelineFacts, *,
                      cancel_event: threading.Event | None = None) -> list[CheckResult]:
    """K3: the BLOCKING postconditions (schema.BLOCKING_CHECKS — measured on
    the EDL alone) that do NOT hold on the live tree. The executor calls this
    inside its batch, before the commit, so a plan that did not do what it
    set out to do is rolled back instead of kept "with issues". No render
    runs here; a check that cannot measure (passed=None) never blocks."""
    ctx = VerifyCtx(store=store, plan=plan, exec_result=exec_result, facts_before=facts_before,
                    cancel_event=cancel_event, render_allowed=False)
    # An OPTIONAL step that was skipped (it raised) owes nothing: its own
    # natural postconditions are not held against the run that went on.
    skipped = [s for s in getattr(exec_result, "steps", []) if getattr(s, "status", None) == "skipped"]
    owed_by_skipped = {(pc.check, repr(sorted(pc.args.items())))
                       for s in skipped if 0 <= s.index < len(plan.steps)
                       for pc in bind_postconditions(plan.steps[s.index].tool, plan.steps[s.index].args)}
    out: list[CheckResult] = []
    for pc in _postconditions(plan):
        if pc.check not in BLOCKING_CHECKS or pc.needs_render:
            continue
        if (pc.check, repr(sorted(pc.args.items()))) in owed_by_skipped:
            continue
        res = run_check(ctx, pc)
        if res.passed is False:
            out.append(res)
    return out


__all__ = ["CheckResult", "VerifyCtx", "CHECKS", "run_check", "verify_plan", "blocking_failures", "overlay_positions",
           "caption_clips", "text_clips", "music_clips", "v1_clips", "silence_runs"]
