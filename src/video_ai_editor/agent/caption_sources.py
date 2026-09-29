"""Which sounds on the timeline `auto_caption` transcribes, and where their
words land on the caption lane (final sweep 2).

auto_caption used to transcribe only the FIRST main-track clip's file: a
second v1 source (a talking clip after silent b-roll) and the voiceover lane
were never captioned, and nothing said so. Every distinct audible speech
source is now a `SpeechSource`:

* ``vo``    — the voiceover lane (and nothing else on it is music);
* ``v1``    — each distinct file the main track plays;
* ``pip``   — each distinct file a PIP / overlay video lane plays (final
  sweep 2 r2: the guest of a stacked split-screen podcast is heard in the
  export — `render/pip.py` folds a PIP's sound into the mix — but was never
  captioned);
* ``audio`` — every other audio lane (``type == "audio"``).

The music lane is never captioned (lyrics are not speech the user recorded).
A muted lane, and a source whose every clip is muted, are silent and skipped.
A file already captioned from v1 is not captioned again from an audio lane
(a detached clip sound plays the same words).

Where the words go: v1 sources map through `timemap` exactly as before (the
caption lane is laid out in LAYOUT time). A SOUND lane plays on the render
clock (`schema.sound_render_windows`: a run starts where its start plays and
then plays whole), and a PIP lane plays in its picture's render window
(`render/clock.render_window`, the window `render/pip.py` places its picture
and sound in), so a word heard at render time ``R`` is stored at the
layout time the ruler draws at ``R`` (`layout_time`, the Python twin of
`timelineLayout.layoutTime`) — behind a 0.5 s dissolve a voiceover imported
at the ruler's 06:00 is captioned at 06:00, not half a second late.

Where two sources speak at once, the voiceover wins: a narrated vlog's own
ambient footage speech under the narration is not stacked on top of it
(`merge_tiers`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, TypeVar

from ..edl.schema import EDL, Clip, Track, sound_lane, sound_render_windows
from .timemap import _map_segment_through_clip, _same_source

#: Caption priority when sources overlap in time: lower wins.
TIER = {"vo": 0, "v1": 1, "pip": 2, "audio": 3}

#: A cue shorter than this after an overlap trim is dropped, not shown.
MIN_CUE_S = 0.05

_EPS = 1e-6


@dataclass
class SpeechSource:
    src: str
    kind: str                      # "vo" | "v1" | "pip" | "audio"
    track_id: str
    clips: list[Clip] = field(default_factory=list)

    @property
    def tier(self) -> int:
        return TIER[self.kind]


def _audible(track: Track, c: Clip) -> bool:
    return not track.muted and not bool(getattr(c.audio, "mute", False)) and c.freeze is None


def speech_sources(edl: EDL) -> list[SpeechSource]:
    """Every distinct audible speech source, v1 sources first (in timeline
    order), then the voiceover lane, then each PIP / overlay video lane,
    then other audio lanes. A file already listed is not listed again."""
    out: list[SpeechSource] = []

    def _add(kind: str, track: Track) -> None:
        for c in sorted((c for c in track.clips if isinstance(c, Clip)), key=lambda c: c.start):
            if not _audible(track, c):
                continue
            hit = next((s for s in out if _same_source(s.src, c.src)), None)
            if hit is None:
                out.append(SpeechSource(src=str(c.src), kind=kind, track_id=track.id, clips=[c]))
            elif hit.track_id == track.id:
                hit.clips.append(c)
            # else: the same words already come from another lane

    v1 = edl.get_track("v1")
    if v1 is not None:
        _add("v1", v1)
    vo = edl.get_track("vo")
    if vo is not None:
        _add("vo", vo)
    for t in edl.tracks:
        # `render/pip.collect_pip_clips`' lane rule: every video lane but v1.
        if t.id != "v1" and t.type == "video":
            _add("pip", t)
    for t in edl.tracks:
        if t.id not in ("v1", "vo", "music") and t.type == "audio":
            _add("audio", t)
    return out


def _pip_render_windows(clips: list[Clip], seams: list[tuple[float, float]]
                        ) -> dict[str, tuple[float, float]]:
    """`(render start, render end)` of each PIP clip, by id — the window
    `render/pip.py` plays its picture AND its sound in (`clock.render_window`
    over the clip's timeline footprint, `pip.pip_layout_end`)."""
    from ..render.clock import render_window
    out: dict[str, tuple[float, float]] = {}
    for c in clips:
        win = render_window(seams, float(c.start), float(c.start) + float(c.effective_duration))
        if win is not None:
            out[c.id] = win
    return out


def layout_time(seams: list[tuple[float, float]], r: float) -> float:
    """A RENDER instant → the LAYOUT time an overlay must be stored at to be
    drawn there (`frontend/src/lib/timelineLayout.layoutTime`): inside a
    cross-fade window it is the seam; outside every window it is exactly
    ``r + Σ{ d : window entirely before r }`` and `render_time` of it is
    ``r`` again."""
    consumed = 0.0
    for seam, cost in seams:
        w_start = seam - (consumed + cost)       # where the clip after it starts
        w_end = seam - consumed                  # where the clip before it stops
        if cost > 0 and w_start - _EPS <= r < w_end - _EPS:
            return seam
        if r < w_end - _EPS:
            break
        consumed += cost
    return r + consumed


def sound_segments(edl: EDL, source: SpeechSource,
                   segments: list[dict]) -> tuple[list[dict], float]:
    """`segments` (source-timed) as the caption lane must store them for a
    SOUND-lane or PIP-lane source, and the layout time its cues may not run
    past."""
    track = edl.get_track(source.track_id)
    if track is None:
        return [], 0.0
    seams = list(edl.v1_seam_table())
    if sound_lane(track):
        windows = sound_render_windows(list(track.clips), seams, edl.video_extent())
    elif track.type == "video" and track.id != "v1":
        windows = _pip_render_windows(source.clips, seams)
    else:
        return [], 0.0
    out: list[dict] = []
    extent = 0.0
    for c in source.clips:
        win = windows.get(c.id)
        if win is None:
            continue
        rs, re = win

        def conv(t: float, c=c, rs=rs, re=re) -> float:
            return layout_time(seams, min(rs + (t - c.start), re))

        extent = max(extent, conv(c.start + c.effective_duration))
        for seg in segments:
            piece = _map_segment_through_clip(c, seg)
            if piece is None or rs + (piece["start"] - c.start) >= re - _EPS:
                continue
            words = [{**w, "start": conv(w["start"]), "end": conv(w["end"])}
                     for w in (piece.get("words") or [])
                     if rs + (w["start"] - c.start) < re - _EPS]
            if piece.get("words") and not words:
                continue
            out.append({**piece, "start": conv(piece["start"]), "end": conv(piece["end"]),
                        **({"words": words} if piece.get("words") else {})})
    out.sort(key=lambda s: s["start"])
    return out, extent


T = TypeVar("T")


def merge_tiers(tiers: Iterable[list[tuple[float, float, T]]]) -> list[tuple[float, float, T]]:
    """Cue spans from several sources, highest priority first: a span that
    overlaps an already placed higher-priority span is dropped; spans of one
    tier that still overlap (a cue held past a seam into the next source)
    are trimmed so one caption shows at a time."""
    placed: list[tuple[float, float, T, int]] = []
    for rank, tier in enumerate(tiers):
        higher = list(placed)
        for a, b, item in sorted(tier, key=lambda x: (x[0], x[1])):
            if any(min(b, hb) - max(a, ha) > _EPS for ha, hb, _i, _r in higher):
                continue
            placed.append((a, b, item, rank))
    placed.sort(key=lambda x: (x[0], x[1]))
    out: list[tuple[float, float, T]] = []
    for i, (a, b, item, _r) in enumerate(placed):
        if i + 1 < len(placed):
            b = min(b, placed[i + 1][0])
        if b - a >= MIN_CUE_S:
            out.append((a, b, item))
    return out


__all__ = ["SpeechSource", "TIER", "speech_sources", "layout_time", "sound_segments",
           "merge_tiers"]
