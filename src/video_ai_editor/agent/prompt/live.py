"""Step arguments that can only be known at RUN time, read from the live store.

The planner runs before anything executes, so three things it used to fix at
plan time were wrong by construction:

  * the hook line (QA-072) — a prompt sent while the upload was still being
    transcribed got the canned "WATCH THIS BEFORE YOU SCROLL" even though the
    executor then waited for the transcript to land;
  * where a target length ends (QA-069) — "make this a 30s reel" cut at
    exactly 30.00 s after the silence/filler cuts, mid-sentence;
  * which seams take a transition (QA-070) — capped silently at 12, and cuts
    earlier in the plan leave 0.1 s slivers a transition cannot sit on.

Each is a sentinel in the plan (schema.HOOK_SENTINEL, FIT_SENTINEL_PREFIX,
SEAM_SENTINEL) that `executor._dispatch_step` resolves here immediately
before dispatch, against `store.edl` AFTER every earlier step of the batch.
Everything here is a pure read of the store; nothing mutates it.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any

from ...edl import EDLStore
from ...edl import timebase as _tb
from ...edl.schema import EDL, Clip
from ..timemap import map_segments_to_timeline, map_words_to_timeline
from .heuristics import _CANNED_HOOK, heuristic_hook
from .schema import FIT_BEST_PREFIX, FIT_SENTINEL_PREFIX, HOOK_SENTINEL, SEAM_SENTINEL

_D = importlib.import_module("video_ai_editor.agent.dispatch")

#: `add_transition(at=$v1_seams)` covers at most this many seams. Well past
#: any real "transition between every clip" (the QA session had 16 seams);
#: the cap exists so a 400-fragment beat cut cannot build a 400-xfade graph,
#: and reaching it is SAID, never silent.
MAX_SEAM_FANOUT = 64
#: A seam whose neighbour is shorter than this gets no transition: silence
#: removal leaves 0.1 s slivers, and an xfade clamped to a 0.1 s clip is a
#: three-frame flicker, not a transition.
MIN_TRANSITION_NEIGHBOUR_S = 0.3
_SEAM_TOL_S = 0.05
#: A word followed by at least this much silence ends a phrase (the caption
#: packer's own breath threshold, caption_format.build_cues gap_break).
PHRASE_GAP_S = 0.35
#: Keep this much after the last kept word, so the cut is not on its release.
FIT_TAIL_PAD_S = 0.15
#: A sentence end earlier than this share of the target is too early — the
#: reel would come out far short of what was asked. Fall back to a pause.
FIT_MIN_SHARE = 0.6
_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*$")


def smpte(t: float, fps: Any) -> str:
    """HH:MM:SS:FF on the project grid, drop-frame (HH:MM:SS;FF) at 29.97 and
    59.94 — the exact rule of frontend/src/lib/timecode.ts (formatTimecode),
    so a time in a run log reads like the clock the user sees."""
    rate = _tb.fps_float(fps) or 30.0
    nom = max(1, round(rate))
    drop = 2 if abs(rate - 30000 / 1001) < 0.005 else 4 if abs(rate - 60000 / 1001) < 0.005 else 0
    frames = max(0, round(max(0.0, float(t)) * rate))
    if drop:
        per_ten = round(rate * 600)
        per_min = nom * 60 - drop
        tens, rem = divmod(frames, per_ten)
        frames += 9 * drop * tens + (drop * ((rem - drop) // per_min) if rem > drop else 0)
    ff = frames % nom
    secs = frames // nom
    return f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}{';' if drop else ':'}{ff:02d}"


def _v1_media(edl: EDL) -> list[Clip]:
    t = edl.get_track("v1")
    return sorted((c for c in (t.clips if t else []) if isinstance(c, Clip)), key=lambda c: c.start)


# --------------------------------------------------------------- transitions

@dataclass
class SeamFanout:
    seams: list[float]
    total: int                      # touching seams on the live v1
    short: int = 0                  # skipped: a neighbour under MIN_TRANSITION_NEIGHBOUR_S
    capped: int = 0                 # skipped: past MAX_SEAM_FANOUT

    def notices(self, fps: Any) -> list[str]:
        out: list[str] = []
        if self.short:
            out.append(f"{self.short} seam(s) skipped: a neighbouring clip is shorter than "
                       f"{MIN_TRANSITION_NEIGHBOUR_S:g} s")
        if self.capped:
            out.append(f"capped at {MAX_SEAM_FANOUT} of {self.total} seams")
        return out


def seam_fanout(edl: EDL, *, min_neighbour_s: float = MIN_TRANSITION_NEIGHBOUR_S) -> SeamFanout:
    """Every seam of the LIVE v1 a transition can honestly sit on."""
    clips = _v1_media(edl)
    touching: list[tuple[float, Clip, Clip]] = [
        (round(nxt.start, 4), cur, nxt) for cur, nxt in zip(clips, clips[1:])
        if abs((cur.start + cur.effective_duration) - nxt.start) <= _SEAM_TOL_S]
    usable = [at for at, cur, nxt in touching
              if min(cur.effective_duration, nxt.effective_duration) >= min_neighbour_s - 1e-9]
    fan = SeamFanout(seams=usable[:MAX_SEAM_FANOUT], total=len(touching),
                     short=len(touching) - len(usable))
    fan.capped = max(0, len(usable) - MAX_SEAM_FANOUT)
    return fan


def seams_from_boundaries(boundaries: list[float], extent: float,
                          *, min_neighbour_s: float = MIN_TRANSITION_NEIGHBOUR_S) -> tuple[list[float], int]:
    """Plan-time twin of `seam_fanout` from `facts.v1_boundaries`: clip i spans
    [b(i-1), b(i)], the last one ends at the video extent. Returns (usable,
    skipped for a short neighbour)."""
    bs = sorted(boundaries)
    edges = [0.0, *bs, max(extent, bs[-1] if bs else 0.0)]
    usable = [b for i, b in enumerate(bs)
              if min(edges[i + 1] - edges[i], edges[i + 2] - edges[i + 1]) >= min_neighbour_s - 1e-9]
    return usable, len(bs) - len(usable)


# --------------------------------------------------------------- target length

@dataclass
class FitCut:
    start: float | None             # None: already within the target
    end: float
    notices: list[str] = field(default_factory=list)


def parse_fit_sentinel(value: Any) -> float | None:
    """The target seconds of a `$fit_to:` / `$fit_best:` sentinel, else None."""
    if not isinstance(value, str):
        return None
    prefix = next((p for p in (FIT_SENTINEL_PREFIX, FIT_BEST_PREFIX) if value.startswith(p)), None)
    if prefix is None:
        return None
    try:
        v = float(value[len(prefix):])
    except ValueError:
        return None
    return v if v > 0 else None


def _timeline_words(store: EDLStore) -> list[dict]:
    tx, src = _D._load_transcript_with_source(store)
    if tx is None:
        return []
    words = [w.model_dump() for w in tx.words]
    if words:
        mapped = map_words_to_timeline(store.edl, "v1", words, src=src)
    else:
        # Word-less transcript (an imported .srt): segment ends are the boundaries.
        segs = map_segments_to_timeline(store.edl, "v1", [s.model_dump() for s in tx.segments], src=src)
        mapped = [{"start": float(s["start"]), "end": float(s["end"]),
                   "word": (s.get("text") or "").strip() + "."} for s in segs]
    return sorted(mapped, key=lambda w: float(w["start"]))


def fit_cut(store: EDLStore, max_s: float) -> FitCut:
    """Where "keep the first `max_s` seconds" should really end: after the
    last SENTENCE that finishes inside the target, else the last pause, else
    the target itself (said). Measured on the live timeline, after the cuts
    earlier in the plan."""
    edl = store.edl
    fps = edl.canvas.fps
    extent = edl.video_extent()
    rendered = extent - edl.transition_overlap()
    end = _tb.ceil_to_frame(extent + 1.0, fps)
    if rendered <= max_s + 1e-6:
        return FitCut(None, end, [f"already {rendered:.1f} s, within the {max_s:g} s target"])
    words = _timeline_words(store)
    sentence: float | None = None
    pause: float | None = None
    for i, w in enumerate(words):
        w_end = float(w["end"])
        nxt = float(words[i + 1]["start"]) if i + 1 < len(words) else None
        gap = (nxt - w_end) if nxt is not None else FIT_TAIL_PAD_S * 2
        cut_at = w_end + max(0.0, min(FIT_TAIL_PAD_S, gap / 2))
        if cut_at > max_s + 1e-6:
            break
        if _SENTENCE_END.search(str(w.get("word") or "").strip()):
            sentence = cut_at
        elif gap >= PHRASE_GAP_S:
            pause = cut_at
    floor = FIT_MIN_SHARE * max_s
    if sentence is not None and sentence >= floor:
        at, why = sentence, "ends on the last sentence that fits"
    elif pause is not None and pause >= floor:
        at, why = pause, "ends on the last pause that fits (no sentence ends in time)"
    else:
        at, why = max_s, ("no sentence or pause ends near the target — cut at the target"
                          if words else "no transcript — cut at the target")
    at = _tb.floor_to_frame(min(at, max_s), fps)
    return FitCut(at, end, [f"{why}: {smpte(at, fps)} ({at:.2f} s of the {max_s:g} s target)"])


@dataclass
class BestWindow:
    """The kept window [start, end) of the live timeline and the cuts that
    leave only it — tail first, so the head cut's ripple cannot move it."""
    start: float
    end: float
    cuts: list[tuple[float, float]]
    score: float
    notices: list[str] = field(default_factory=list)


def _scored_windows(words: list[dict], max_s: float) -> list[tuple[float, float, float, str]]:
    """Every sentence-aligned window that fits `max_s` and reaches
    FIT_MIN_SHARE of it, as (score, start, end, why) — scored by ai/shorts'
    own `_score_window` (speech density, a strong opening line, a finished
    last sentence, length fit; dead air and filler words penalised). Energy is
    neutral here: the timeline's mix is not rendered to plan one cut."""
    from ...ai.shorts import _PAUSE_BREAK_S, _score_window, _sentences
    # One segment per breath group: a real pause ends a sentence even when the
    # speaker's words carry no punctuation (whisper often leaves a ramble
    # unpunctuated while punctuating the rest).
    groups: list[list[dict]] = []
    for w in words:
        if groups and float(w["start"]) - float(groups[-1][-1]["end"]) < _PAUSE_BREAK_S:
            groups[-1].append(w)
        else:
            groups.append([w])
    tx = {"segments": [{"start": float(g[0]["start"]), "end": float(g[-1]["end"]),
                        "text": " ".join(str(w.get("word") or "") for w in g),
                        "words": [{"start": float(w["start"]), "end": float(w["end"]),
                                   "word": str(w.get("word") or "")} for w in g]} for g in groups]}
    sents = _sentences(tx)
    floor = FIT_MIN_SHARE * max_s
    out: list[tuple[float, float, float, str]] = []
    for i in range(len(sents)):
        # A breath of lead-in, at most half the gap to the previous sentence
        # (the same rule as the tail), so neither edge lands on a word.
        lead = FIT_TAIL_PAD_S if i == 0 else max(0.0, min(FIT_TAIL_PAD_S, (sents[i].start - sents[i - 1].end) / 2))
        start = max(0.0, sents[i].start - lead)
        for j in range(i, len(sents)):
            nxt = sents[j + 1].start if j + 1 < len(sents) else None
            end = sents[j].end + (FIT_TAIL_PAD_S if nxt is None else max(0.0, min(FIT_TAIL_PAD_S, (nxt - sents[j].end) / 2)))
            if end - start > max_s + 1e-6:
                break
            if end - start < floor:
                continue
            score, why, _stats = _score_window(sents[i:j + 1], [], 0.0, max_s)
            out.append((score, start, end, why))
    return out


def best_window(store: EDLStore, max_s: float) -> BestWindow | FitCut:
    """The best `max_s` seconds of the live timeline (QA-069, wave C).

    "make this a 30s reel" used to keep the FIRST 30 s (then: the first 30 s
    ending on a sentence). A reel is the strongest 30 s, wherever it is: every
    run of whole sentences that fits is scored and the best kept, cut to the
    frame grid. Returns a `FitCut` (the first-N rule, said) when there is no
    transcript to score, and `FitCut(None, …)` when the timeline already fits."""
    edl = store.edl
    fps = edl.canvas.fps
    extent = edl.video_extent()
    rendered = extent - edl.transition_overlap()
    if rendered <= max_s + 1e-6:
        return fit_cut(store, max_s)
    words = _timeline_words(store)
    cands = _scored_windows(words, max_s) if words else []
    if not cands:
        fc = fit_cut(store, max_s)
        fc.notices.append("no transcript to choose the best window from — kept the opening instead")
        return fc
    best = max(cands, key=lambda c: (round(c[0], 6), -c[1]))
    score, start, end, why = best
    start = _tb.floor_to_frame(start, fps) if start > 1e-6 else 0.0
    end = _tb.floor_to_frame(min(end, start + max_s, extent), fps)
    tail_end = _tb.ceil_to_frame(extent + 1.0, fps)
    cuts: list[tuple[float, float]] = []
    if end < extent - 1e-3:
        cuts.append((round(end, 4), round(tail_end, 4)))
    if start > 1e-3:
        cuts.append((0.0, round(start, 4)))
    # The opening window (earliest start, longest), named when it lost.
    first = min(cands, key=lambda c: (c[1], -c[2]))
    note = (f"kept the best {end - start:.1f} s of whole sentences ({smpte(start, fps)}–{smpte(end, fps)}): {why}"
            + (f"; the opening window scored {first[0]:.2f} against {score:.2f}"
               if first[1] < best[1] - 1e-6 else ""))
    return BestWindow(start, end, cuts, score, [note])


# --------------------------------------------------------------- hook text

def live_hook_text(store: EDLStore) -> tuple[str, str]:
    """(hook line, notice) from what is spoken on the timeline NOW."""
    tx, src = _D._load_transcript_with_source(store)
    head = ""
    if tx is not None:
        segs = map_segments_to_timeline(store.edl, "v1", [s.model_dump() for s in tx.segments], src=src)
        head = " ".join(" ".join((s.get("text") or "").strip() for s in segs).split())[:240]
    guess = heuristic_hook(head)
    if guess:
        return guess[:60], "hook text: from your first sentence (written once the transcript landed)"
    return _CANNED_HOOK, "hook text: generic — no speech was found on the timeline; reply with your own line to change it"


# --------------------------------------------------------------- one entry point

def resolve_live_args(store: EDLStore, tool: str,
                      args: dict[str, Any]) -> tuple[dict[str, Any] | list[dict[str, Any]] | None, list[str]]:
    """Replace this step's run-time sentinels. Returns (args, notices); args
    is None when the step has nothing to do (the timeline already fits), and
    a LIST of arg dicts when one step fans out (the best-window trim is a
    tail cut and a head cut — one step, so one undo)."""
    out = dict(args)
    notices: list[str] = []
    # Editor Brain (EB1): ONE branch — any `$brain:<kind>` arg hands the whole
    # step to brain/resolve.py, which maps the frozen EDP's decisions through
    # agent/timemap against the live tree and answers in this function's own
    # shape (one dict, a fan-out list, or None). A `plan_ref` that rides
    # along without a sentinel is dropped: no handler reads it.
    from ...brain import resolve as _brain
    if tool in _brain.RERUN_GUARDED_TOOLS:
        _brain.refuse_if_already_edited(store)       # a re-run on the brain's own output: refuse, never cut
    if _brain.brain_args(out):
        return _brain.resolve(store, tool, out)
    out.pop(_brain.PLAN_REF_ARG, None)
    if tool == "apply_hook_stack" and out.get("text") == HOOK_SENTINEL:
        out["text"], note = live_hook_text(store)
        notices.append(note)
    if tool == "cut_range" and isinstance(out.get("start"), str) and out["start"].startswith(FIT_BEST_PREFIX):
        max_s = parse_fit_sentinel(out["start"])
        if max_s is not None:
            bw = best_window(store, max_s)
            notices.extend(bw.notices)
            if isinstance(bw, BestWindow):
                if not bw.cuts:
                    return None, notices
                return [{**out, "start": a, "end": b} for a, b in bw.cuts], notices
            if bw.start is None:
                return None, notices
            out["start"], out["end"] = round(bw.start, 4), round(bw.end, 4)
            return out, notices
    if tool == "cut_range":
        max_s = parse_fit_sentinel(out.get("start"))
        if max_s is not None:
            fc = fit_cut(store, max_s)
            notices.extend(fc.notices)
            if fc.start is None:
                return None, notices
            out["start"], out["end"] = round(fc.start, 4), round(fc.end, 4)
    return out, notices


__all__ = ["MAX_SEAM_FANOUT", "MIN_TRANSITION_NEIGHBOUR_S", "SeamFanout", "FitCut", "BestWindow", "best_window",
           "smpte", "seam_fanout",
           "seams_from_boundaries", "fit_cut", "parse_fit_sentinel", "live_hook_text", "resolve_live_args",
           "SEAM_SENTINEL"]
