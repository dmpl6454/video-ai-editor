"""Source-time <-> timeline-time mapping for transcript consumers.

Two clocks run through this codebase and they only coincide on a fresh,
uncut, speed-1 timeline:

  * SOURCE seconds — offsets into a media FILE. Whisper word/segment times,
    silencedetect offsets (relative to the `[in_, out)` slice it was fed),
    `Clip.in_`/`Clip.out`.
  * TIMELINE seconds — what the viewer sees. `Clip.start`, `cut_range`'s
    args, every TextClip/Sticker `start`/`end`, `EDL.duration`.

A clip plays source `[in_, out)` at timeline `[start, start + (out-in_)/speed)`.
Every cut, trim, split or speed change breaks the identity between the two,
and a transcript is ALWAYS recorded against the source — so any tool that
reads transcript times and writes timeline coordinates has to go through a
mapping. Until this module there was no shared one: `remove_silences` did the
conversion inline and correctly, `remove_fillers` fed source times straight to
`cut_range`, and `auto_caption`/`add_caption_track` laid cues at source times.
Measured on a real 36s clip (Piper narration with fillers, faster-whisper
transcript): on a fresh timeline `remove_fillers` removed 4/4 fillers; run
AFTER `remove_silences` it removed 1/4 and cut two ranges of real speech
(source 9.65–10.96 and 18.66–19.17) — the natural pipeline order silently
destroyed content. `auto_caption` after the same cuts laid its last cue at
33.9s on a 24.2s timeline; `EDL.recompute_duration()` then grew the project to
the overlay end and the render came out 34s long, ~10s of it black + silence,
with the removed filler words still in the caption text.

Conventions, chosen so the identity case is bit-exact:

  * Per-clip conversion is `start + (t - in_) / speed_factor`. For the fresh
    clip (`in_=0, start=0, speed=1`) that is `0.0 + (t - 0.0) / 1.0 == t` in
    IEEE arithmetic, which is what lets the caption tools promise byte-for-byte
    unchanged output on an uncut timeline.
  * A clip covers source `[in_, out)` half-open, so a word that ends exactly at
    a cut edge has zero overlap and is dropped rather than surviving as a
    zero-length stub. A word STRADDLING a cut edge is clipped to the surviving
    part (its text is kept — it was audibly spoken on both sides), never
    stretched across the seam.
  * Mapping is per-OCCURRENCE: a clip that reuses a source region already on
    the timeline maps the same word twice, in timeline order. Clips are walked
    in timeline order so the output word stream is monotonic, which the cue
    packer (`caption_format.build_cues`) relies on for its pause detection.
  * `src` scopes the mapping to clips of ONE source file. A transcript belongs
    to a single file (the v1 clip's own `ingest.json`, see
    `dispatch._current_v1_ingest_json`; an imported .srt is treated the same
    way, see `dispatch._load_transcript_with_source`), so on a track with
    clips from two files only that file's clips may carry its words.
    `src=None` maps through every media clip on the track. It is NOT "origin
    unknown": on a two-file track it fans one file's words out over the
    other's footage (the review caught remove_fillers cutting the second file
    and add_caption_track laying every cue twice). Production callers always
    pass the transcript's file; None is for callers that know the track is
    single-source, where it is identical.

Pure functions over the EDL: nothing here commits, so callers keep composing
their own `EDLStore.batch()` around whatever cuts they derive from the result.
"""
from __future__ import annotations

import os

from ..edl.schema import EDL, Clip

# Tolerance for "this overlap is real": a word whose surviving slice is
# shorter than this is treated as fully removed. Float noise from the
# divide/multiply round trip in cut_range's own in/out arithmetic sits around
# 1e-12; a millionth of a second is far above that and far below anything a
# caption or a cut can express.
_EPS = 1e-6


# ---------------------------------------------------------------- per clip

def clip_source_to_timeline(c: Clip, t_source: float) -> float:
    """Where source instant `t_source` of clip `c` plays on the timeline.

    Does NOT check that `t_source` lies inside `[c.in_, c.out)` — callers that
    need that guarantee use `source_to_timeline` / `source_range_to_timeline`,
    which walk the track. Kept unguarded so `remove_silences` can convert an
    offset it already knows is inside the slice without a redundant search.
    """
    return c.start + (t_source - c.in_) / c.speed_factor


def clip_timeline_to_source(c: Clip, t_timeline: float) -> float:
    """Inverse of `clip_source_to_timeline`: the source instant clip `c`
    shows at timeline `t_timeline`. Unguarded for the same reason."""
    return c.in_ + (t_timeline - c.start) * c.speed_factor


# ---------------------------------------------------------------- track walk

def _same_source(a: str | os.PathLike | None, b: str | os.PathLike | None) -> bool:
    """Path equality without touching the filesystem.

    `os.path.abspath` + `normcase` rather than `Path.resolve()`: a clip's src
    may be a repaired or .vae-imported path that no longer exists, and this
    must still match it against the transcript it came from. A relative src
    never occurs in practice (`add_clip` runs `_safe_src`, which resolves) but
    abspath makes the comparison total either way.
    """
    if a is None or b is None:
        return False
    if (os.path.normcase(os.path.abspath(os.fspath(a)))
            == os.path.normcase(os.path.abspath(os.fspath(b)))):
        return True
    # A derived file (reframe/denoise/stabilize/upscale output in the session
    # cache) plays the SAME source seconds as the upload it was made from, so
    # it carries the upload's transcript — resolve both sides to their origin
    # (`agent/media_origin`: `.origin` sidecars, pure reads) before giving up.
    # Without this the first src-rewriting step of a plan lost the transcript
    # for the rest of the session (zero captions after a reframe).
    from .media_origin import origin_of
    oa, ob = origin_of(a), origin_of(b)
    return (os.path.normcase(os.path.abspath(oa)) == os.path.normcase(os.path.abspath(ob)))


def media_clips(edl: EDL, track_id: str, *, src: str | None = None) -> list[Clip]:
    """Media clips on `track_id` in TIMELINE order, optionally only those
    playing source file `src`. Empty when the track is missing."""
    t = edl.get_track(track_id)
    if t is None:
        return []
    clips = [c for c in t.clips if isinstance(c, Clip)]
    if src is not None:
        clips = [c for c in clips if _same_source(c.src, src)]
    return sorted(clips, key=lambda c: c.start)


def _overlap(c: Clip, s_start: float, s_end: float) -> tuple[float, float] | None:
    """The part of source interval [s_start, s_end] that clip `c` plays, in
    SOURCE seconds, or None if none of it does.

    A zero-length interval (an instant) is inside when `in_ <= t < out`, so a
    point query at the exact seam between two consecutive clips resolves to
    the clip that begins there, not the one that just ended.
    """
    if s_end < s_start:
        return None
    if s_end == s_start:
        return (s_start, s_start) if c.in_ <= s_start < c.out else None
    lo = max(s_start, c.in_)
    hi = min(s_end, c.out)
    if hi - lo <= _EPS:
        return None
    return (lo, hi)


def source_to_timeline(edl: EDL, track_id: str, t_source: float, *,
                       src: str | None = None) -> float | None:
    """Timeline instant where source `t_source` plays on `track_id`, or None
    when no clip on the track shows that instant (it was cut away, or the
    track is empty).

    First occurrence in timeline order wins when a region is reused.

    One closed-end fallback: `t_source` equal to the LARGEST `out` among the
    clips considered — the true end of what the timeline shows of the file —
    is accepted, so "where does the end of the file land" answers with the
    extent instead of None (callers mapping RANGES get that for free from
    `_overlap`; this is the point-query equivalent). It applies to that one
    instant only. An earlier version accepted `t == c.out` of ANY clip, which
    made the first instant of every removed region resolve to the cut seam —
    a source instant that IS cut away answering with a timeline time, the
    opposite of this function's contract. Among clips ending at that largest
    `out` (a region reused twice) the first in timeline order wins, matching
    the open-interval rule above.
    """
    clips = media_clips(edl, track_id, src=src)
    for c in clips:
        if c.in_ <= t_source < c.out:
            return clip_source_to_timeline(c, t_source)
    shown_end = max((c.out for c in clips if c.out > c.in_), default=None)
    if shown_end is not None and t_source == shown_end:
        c = next(c for c in clips if c.out == shown_end and c.out > c.in_)
        return clip_source_to_timeline(c, t_source)
    return None


def timeline_to_source(edl: EDL, track_id: str,
                       t_timeline: float) -> tuple[Clip, float] | None:
    """The clip on `track_id` playing at timeline `t_timeline` and the SOURCE
    instant it shows there, or None if the timeline is empty at that point
    (a gap, before the first clip, at/after the end)."""
    for c in media_clips(edl, track_id):
        if c.start <= t_timeline < c.start + c.effective_duration:
            return c, clip_timeline_to_source(c, t_timeline)
    return None


def source_range_to_timeline(edl: EDL, track_id: str, s_start: float, s_end: float, *,
                             src: str | None = None) -> list[tuple[float, float]]:
    """Every timeline range on which source [s_start, s_end] (or the part of it
    still present) plays, in timeline order. Empty when it was cut away
    entirely. A range that straddles a cut edge comes back clipped; one that
    spans a removed middle comes back as two ranges (one per surviving clip);
    one whose source is on the timeline twice comes back twice.
    """
    out: list[tuple[float, float]] = []
    for c in media_clips(edl, track_id, src=src):
        ov = _overlap(c, s_start, s_end)
        if ov is None:
            continue
        out.append((clip_source_to_timeline(c, ov[0]), clip_source_to_timeline(c, ov[1])))
    return out


# ---------------------------------------------------------------- transcripts

def _map_words_through_clip(c: Clip, words: list[dict]) -> list[dict]:
    """Words (source-timed dicts with start/end) that clip `c` plays, retimed
    to the timeline and clipped to the clip's window. Other keys (word, prob,
    speaker, ...) pass through untouched — new dicts, the input is never
    mutated."""
    out: list[dict] = []
    for w in words:
        ov = _overlap(c, float(w["start"]), float(w["end"]))
        if ov is None:
            continue
        out.append({**w,
                    "start": clip_source_to_timeline(c, ov[0]),
                    "end": clip_source_to_timeline(c, ov[1])})
    return out


def map_words_to_timeline(edl: EDL, track_id: str, words: list[dict], *,
                          src: str | None = None) -> list[dict]:
    """Retime a source-timed word stream onto the current timeline.

    Words in removed ranges are dropped, words straddling a cut edge are
    clipped, and the result is ordered by timeline time (clip order, then the
    words' own order within each clip) — so a region on the timeline twice
    yields its words twice, at each occurrence.
    """
    out: list[dict] = []
    for c in media_clips(edl, track_id, src=src):
        out.extend(_map_words_through_clip(c, words))
    return out


def _tokens_surviving(text: str, s: float, e: float, lo: float, hi: float) -> tuple[str, bool]:
    """For a segment WITHOUT word timing, which of its space-separated tokens
    fall inside the surviving source window [lo, hi] of its [s, e] span.

    Mirrors `caption_format.cues_from_segments`'s fallback exactly: tokens are
    assumed evenly spaced across the span. Returns (text, all_kept); when every
    token survives the ORIGINAL text is returned unchanged so an uncut
    timeline reproduces today's output byte-for-byte.
    """
    toks = text.strip().split(" ")
    span = e - s
    if span <= 0 or not toks:
        return text, True
    step = span / len(toks)
    kept = [tok for j, tok in enumerate(toks)
            if min(hi, s + (j + 1) * step) - max(lo, s + j * step) > _EPS]
    if len(kept) == len(toks):
        return text, True
    return " ".join(kept), False


def _map_segment_through_clip(c: Clip, seg: dict) -> dict | None:
    """One transcript segment as clip `c` plays it, or None if `c` shows none
    of it.

    With word timing the piece exists iff at least one word survives; its
    span is the segment's own [start, end] as clipped by `c` (so an uncut
    timeline keeps the segment's span, which `add_caption_track` uses
    directly, byte-identical) widened only if a surviving word pokes outside
    that span. Its text is the segment's text when every word survived, else
    the surviving words joined — that is how a removed filler leaves the
    caption. Without word timing the tokens are split evenly across the span
    (the same model `cues_from_segments` applies) and the ones in the removed
    part are dropped.
    """
    s, e = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
    words = seg.get("words") or []
    span_ov = _overlap(c, s, e)
    if words:
        kept = _map_words_through_clip(c, words)
        if not kept:
            return None
        lo = kept[0]["start"]
        hi = kept[-1]["end"]
        if span_ov is not None:
            lo = min(lo, clip_source_to_timeline(c, span_ov[0]))
            hi = max(hi, clip_source_to_timeline(c, span_ov[1]))
        all_kept = len(kept) == len(words)
        text = seg.get("text") if all_kept else " ".join(
            t for t in ((w.get("word") or "").strip() for w in kept) if t)
        return {**seg, "start": lo, "end": hi, "text": text, "words": kept}
    if span_ov is None:
        return None
    text, _all = _tokens_surviving(str(seg.get("text") or ""), s, e, *span_ov)
    return {**seg,
            "start": clip_source_to_timeline(c, span_ov[0]),
            "end": clip_source_to_timeline(c, span_ov[1]),
            "text": text}


def map_segments_to_timeline(edl: EDL, track_id: str, segments: list[dict], *,
                             src: str | None = None) -> list[dict]:
    """Retime whisper-style segments (`{start, end, text, words?}`) onto the
    current timeline: per clip in timeline order, per segment in order. Fully
    removed segments vanish; a segment split by a cut yields one piece per
    surviving side; one on the timeline twice yields two pieces. Segment ids
    are carried through untouched, so pieces of one segment share an id.
    """
    out: list[dict] = []
    for c in media_clips(edl, track_id, src=src):
        for seg in segments:
            piece = _map_segment_through_clip(c, seg)
            if piece is not None:
                out.append(piece)
    return out


def clamp_to_extent(start: float, end: float, extent: float) -> tuple[float, float] | None:
    """Confine an overlay's [start, end] to the video's timeline extent.

    `caption_format.build_cues` deliberately holds a cue PAST its last word to
    meet reading-speed and minimum-duration targets — up to `max_dur` beyond
    the last spoken word, since nothing follows it. On an uncut timeline that
    overhang is harmless (it lands on the tail of the footage); once the
    footage is shorter than the transcript it is exactly what pushed
    `EDL.recompute_duration()` past the video and rendered black frames with a
    caption on them. Returns None for a cue that would not be on screen at
    all (it starts at or after the end, or is inverted). Never moves `start`,
    so a cue entirely inside the extent is returned as-is — the identity case
    is untouched.

    A ZERO-length span is kept, not dropped: whisper does emit `start == end`
    words, and both caption tools laid them as zero-length TextClips before
    this module existed. The mapper keeps them too (`_overlap` treats an
    instant as inside `[in_, out)`), so the clamp must not be the one place
    that loses them — the review found `end <= start` here silently dropping
    a word from an UNCUT word_emphasis track.

    On an uncut timeline this trim is the one deliberate deviation from
    "byte-identical to before the mapper": a trailing hold that overran the
    footage was already the black-tail defect on a smaller scale. Pinned by
    tests/test_transcript_timemap.py::test_uncut_timeline_trailing_hold_is_trimmed_to_the_footage.
    """
    if start >= extent:
        return None
    end = min(end, extent)
    if end < start:
        return None
    return start, end
