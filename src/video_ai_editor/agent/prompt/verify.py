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
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...edl import EDLStore
from ...edl.schema import EDL, Clip, Sticker, TextClip
from ..timemap import map_words_to_timeline, source_range_to_timeline
from .facts import TimelineFacts
from .recipes import FILLERS_STRICT
from .schema import CHECK_SPECS, CLIP_SENTINELS, Plan, Postcondition, bind_postconditions
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
                             "headline": self.headline}
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
    sentinel or None → every v1 clip (the executor fanned it out)."""
    if clip_id and clip_id not in CLIP_SENTINELS:
        return [str(clip_id)]
    return [c.id for c in v1_clips(ctx.edl)]


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
        if (ctx.facts_before.language or "").lower() != "hi":
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
    if misaligned:
        parts.append(f"{len(misaligned)} transcript words sat inside measured silence (misaligned timestamps)"
                     " — not counted: " + ", ".join(str(w.get("word")) for w in misaligned[:6]))
    return _ok(pc, not lost, len(lost), 0, unit="words lost", detail="; ".join(parts) or None)


def c_fillers_remaining_leq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    fillers = _filler_set(ctx, _arg(pc, "words"))
    cap = int(_arg(pc, "max") or 0)
    remaining = [w for w in ctx.words_on(ctx.edl) if _norm_token(w.get("word")) in fillers]
    return _ok(pc, len(remaining) <= cap, len(remaining), f"≤ {cap}", unit="fillers")


def c_duration_shrank(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    before, after = ctx.exec_result.duration_before, ctx.edl.duration
    min_ratio, min_seconds = _arg(pc, "min_ratio"), _arg(pc, "min_seconds")
    need = 0.01
    if min_ratio is not None:
        need = max(need, before * float(min_ratio))
    if min_seconds is not None:
        need = max(need, float(min_seconds))
    shrank = before - after
    return _ok(pc, shrank >= need, round(shrank, 3), f"≥ {need:.2f}", unit="s")


def c_duration_between(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    before, after = ctx.exec_result.duration_before, ctx.edl.duration
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
    detail = (f"longest remaining pause {longest:.2f} s; {len(long_runs)} pause(s) ≥ {floor:.2f} s "
              f"(min_dur {min_dur:g} + 2×keep_pad {keep_pad:g} + {_LONG_PAUSE_TOL_S:g} tolerance)")
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
        return _ok(pc, None, None, "reframed or fit=cover", detail="no v1 clips")
    before_src = {c.id: c.src for c in v1_clips(ctx.edl_before)}
    src_changed = any(before_src.get(c.id) not in (None, c.src) for c in clips)
    results = ctx.exec_result.results_for("auto_reframe")
    skipped = any("(skipped" in str(x) for r in results for x in (r.get("reframed") or []))
    all_cover = all(c.fit == "cover" for c in clips)
    passed = (bool(results) and not skipped and src_changed) or all_cover
    measured = {"src_changed": src_changed, "skipped": skipped, "all_cover": all_cover}
    return _ok(pc, passed, measured, "reframed or fit=cover")


def c_no_letterbox(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips = v1_clips(ctx.edl)
    if not clips:
        return _ok(pc, None, None, "every clip fills the canvas", detail="no v1 clips")
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
        return _ok(pc, None, None, 0, detail=f"{unknown} clip(s) could not be probed")
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
    passed = bool(clips) and (not want_duck or ducked) and (count is None or len(clips) == int(count))
    return _ok(pc, passed, {"clips": len(clips), "ducked": ducked},
               {"clips": f"= {int(count)}" if count is not None else "≥ 1", "ducked": bool(want_duck) or "any"})


def c_music_ducked(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    to_db = float(_arg(pc, "to_db") if _arg(pc, "to_db") is not None else -12)
    track = ctx.edl.get_track("music")
    if not track or not music_clips(ctx.edl):
        return _ok(pc, False, None, f"≤ {to_db} dB", detail="no music")
    if not track.duck:
        return _ok(pc, False, None, f"≤ {to_db} dB", detail="ducking off")
    return _ok(pc, track.duck.to_db <= to_db, track.duck.to_db, f"≤ {to_db}", unit="dB")


def c_music_within_video_extent(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    clips = music_clips(ctx.edl)
    extent = ctx.edl.video_extent()
    if not clips:
        return _ok(pc, None, None, f"≤ {extent:.2f}", detail="no music")
    last = max(c.start + c.effective_duration for c in clips)
    return _ok(pc, last <= extent + _EXTENT_SLACK_S, round(last, 3), f"≤ {extent:.2f}", unit="s")


def c_music_covers(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    min_ratio = float(_arg(pc, "min_ratio") or 0.95)
    extent = ctx.edl.video_extent()
    clips = music_clips(ctx.edl)
    if extent <= 0:
        return _ok(pc, None, None, f"≥ {min_ratio:.0%}", detail="no video extent")
    covered = _span_total(_merge_spans([(c.start, min(extent, c.start + c.effective_duration))
                                        for c in clips], gap=0.0))
    ratio = covered / extent
    return _ok(pc, ratio >= min_ratio, round(ratio, 3), f"≥ {min_ratio:.0%}", unit="ratio")


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
    changed = [cid for cid in _clip_targets(ctx, _arg(pc, "clip_id"))
               if (res := ctx.edl.get_clip(cid)) and isinstance(res[1], Clip)
               and before.get(cid) not in (None, res[1].src)]
    return _ok(pc, len(changed) >= 1, len(changed), "≥ 1", unit="clips re-rendered")


def c_speed_equals(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    factor = _arg(pc, "factor")
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


def c_transitions_count_geq(ctx: VerifyCtx, pc: Postcondition) -> CheckResult:
    n, ttype = int(_arg(pc, "n") or 1), _arg(pc, "type")
    t = ctx.edl.get_track("v1")
    trs = [tr for tr in (t.transitions if t else []) if not ttype or tr.type == ttype]
    return _ok(pc, len(trs) >= n, len(trs), f"≥ {n}", unit="transitions")


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
               {"sessions": count, "duration": f"[{lo:.1f}, {hi if hi != float('inf') else '∞'}]"})


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
                    store_resolver=store_resolver, cancel_event=cancel_event)
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


__all__ = ["CheckResult", "VerifyCtx", "CHECKS", "run_check", "verify_plan", "overlay_positions",
           "caption_clips", "text_clips", "music_clips", "v1_clips", "silence_runs"]
