"""The brain's captions at run time (review UX-03, UX-09).

The planner lays the caption CUES in the `captions` decision on the
reference clock, from the graph's own words: every speaker's, only the words
the plan keeps, each once (`planner/captions.py`). Two things here finish the
job against the LIVE timeline:

* `resolve_captions` — the `$brain:captions` sentinel of
  `add_caption_track.cues`. Each cue's reference seconds become, per angle
  file, the file's own seconds (`r + offsets[file]`, the convention every
  brain tool uses) and are mapped through the v1 layout by source
  (`agent/timemap`): whichever angle shows that moment carries the cue, so a
  guest is captioned while the picture is on the guest's camera. A cue whose
  moment was cut away is dropped and SAID.
* `speaker_cover` — what `captions_cover` measures when the session has a
  current Content Graph: for EVERY speaker, how much of that speaker's
  speech that plays lies under a cue, and the verdict is the WORST speaker's
  ratio. A caption track that covers the host and none of the guest cannot
  report 1 any more (the transcript-of-the-first-file measuring stick could).
"""
from __future__ import annotations

from typing import Any

from ..agent.timemap import source_range_to_timeline
from ..agent.tools import CAPTION_CUES_MAX
from ..edl import timebase as _tb
from ..edl.schema import EDL

#: Cues one dispatch lays: the tool's own cap, one number (`agent/tools.CAPTION_CUES_MAX`).
MAX_CUES = CAPTION_CUES_MAX
#: A speaker with less speech than this on the timeline is not held to a ratio.
MIN_SPEAKER_S = 1.0


def _merge(spans: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def timeline_spans(edl: EDL, offsets: dict[str, float], t0: float, t1: float) -> list[tuple[float, float]]:
    """Where reference seconds `[t0, t1]` play on v1 now: the union, over
    every angle file, of the timeline spans of that file's own seconds
    `[t0 + off, t1 + off]` (a camera switch abuts two of them: they merge)."""
    hits: list[tuple[float, float]] = []
    for path, off in offsets.items():
        hits += source_range_to_timeline(edl, "v1", t0 + off, t1 + off, src=path)
    return _merge([(a, b) for a, b in hits if b > a], 1.5 * _tb.frame_duration(edl.canvas.fps))


def _intersection(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0.0, min(e1, e2) - max(s1, s2))
    return total


# ---------------------------------------------------------------- the sentinel

def _lay(cx: Any, d: Any, offsets: dict[str, float]) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    gone = 0
    for cue in d.params.get("cues") or []:
        spans = timeline_spans(cx.edl, offsets, float(cue["t0"]), float(cue["t1"]))
        if not spans:
            gone += 1
            continue
        a, b = max(spans, key=lambda s: s[1] - s[0])          # a cue split by a later hand edit keeps its longest part
        out.append({"text": str(cue["text"]), "start": round(a, 4), "end": round(b, 4)})
    return out, gone


def resolve_captions(cx: Any) -> list[dict[str, Any]]:
    """The one dispatch `add_caption_track(cues=…)` for the plan's captions
    decision, in timeline seconds; `[]` when no cue plays."""
    from .resolve import _path_of
    cues: list[dict[str, Any]] = []
    dropped = 0
    for d in cx.edp.by_kind("captions"):
        primary = _path_of(str(d.params.get("src") or ""), cx.paths)
        offsets = dict(cx.offsets) or ({primary: 0.0} if primary else {})
        if not offsets:
            cx.drop(d, "the captions name no file of this project")
            continue
        laid, gone = _lay(cx, d, offsets)
        cues += laid
        dropped += gone
        cx.entry(d, cues=len(laid), dropped=gone)
    if dropped:
        cx.notices.append(f"{dropped} caption{'s' if dropped != 1 else ''} dropped: the moment {'they were' if dropped != 1 else 'it was'} "
                          f"laid on is already removed, earlier in this plan or in an earlier edit")
    cues.sort(key=lambda c: (c["start"], c["end"]))
    if len(cues) > MAX_CUES:
        cx.notices.append(f"caption cues capped at {MAX_CUES}: {len(cues) - MAX_CUES} left out")
        cues = cues[:MAX_CUES]
    return [{**cx.base, "cues": cues}] if cues else []


# ---------------------------------------------------------------- the measure

def _current_graph(ctx: Any) -> Any | None:
    from . import resolve as _resolve
    from . import store as _store
    from .planner.graph_view import load_graph
    try:
        sdir = _resolve.brain_dir_for(ctx.store).parent
        gid = _store.current_graph_id(sdir)
        return load_graph(sdir, gid) if gid is not None else None
    except Exception:  # noqa: BLE001 — an unreadable graph is "no graph", never a crash in a check
        return None


def speaker_cover(ctx: Any, cues: list[tuple[float, float]]) -> tuple[float, dict[str, float]] | None:
    """`(worst ratio, {speaker: ratio})` of the speech that plays under the
    cues, per speaker of the current graph; None without a graph (or with
    no speaker that has speech on the timeline) — the caller's own measure
    stands then."""
    g = _current_graph(ctx)
    if g is None or not g.words:
        return None
    offsets = {str(s["path"]): g.offset(s["key"]) for s in g.sources if s.get("path")}
    if not offsets:
        return None
    from .planner.captions import unread     # what gets no caption is not "speech to cover"
    skip = unread(g)
    by_spk: dict[str, list[tuple[float, float]]] = {}
    for w in g.words:
        if w.get("filler") or skip(w):
            continue
        by_spk.setdefault(str(w.get("spk") or "speaker"), []).extend(timeline_spans(ctx.edl, offsets, float(w["t0"]), float(w["t1"])))
    names = {str(s.get("id")): str(s.get("name") or str(s.get("role_guess") or "").capitalize() or s.get("id")) for s in g.speakers}
    merged_cues = _merge(cues, 0.0)
    ratios: dict[str, float] = {}
    for spk, spans in by_spk.items():
        speech = _merge(spans, 0.25)
        total = sum(b - a for a, b in speech)
        if total >= MIN_SPEAKER_S:
            ratios[names.get(spk, spk)] = round(_intersection(merged_cues, speech) / total, 3)
    return (min(ratios.values()), ratios) if ratios else None


__all__ = ["MAX_CUES", "timeline_spans", "resolve_captions", "speaker_cover"]
