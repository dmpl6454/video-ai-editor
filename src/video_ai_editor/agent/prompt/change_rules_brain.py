"""Card rules for the Editor Brain tools (EB1-B): the lines an angle swap
(`apply_camera_plan`) and the dialogue lane (`sync_dialogue_lane`) put on the
change card, claiming their keys so `changes.summarize`'s guarantee holds and
so a swapped piece is never called a deletion. Called from `change_rules.
Summary`; `cut_source_ranges` needs nothing here (its cuts are `cut_range`'s
and the main-lane rules already say "Deleted …").

An angle piece is a v1 piece of a clip that now plays ANOTHER file: its
`in`/`out` are that angle's seconds, so the parent's source window it stands
in for is not on the clip. `angle_windows` estimates it — directly after the
previous piece's window in timeline order (a switch AT a cut is the one
ambiguous case: the card then places the removed stretch after the switched
span rather than before it; the same seconds are reported either way, and the
brain's reasons say which) — so the deletion arithmetic sees no hole where
nothing was removed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...edl.schema import Clip
from ..media_origin import origin_of
from .change_words import _v1, secs, split_parent
from .changes import EPS

if TYPE_CHECKING:
    from .change_rules import Summary


def _same_upload(a: str, b: str) -> bool:
    """Two files that are one upload's renders (a reframe, a denoise, a
    stabilised copy: `<render>.origin` names the upload — agent/media_origin).
    A derivative is the SAME camera; only another upload is another angle."""
    return origin_of(a) == origin_of(b)


def is_angle_piece(b: Clip, pieces: list[Clip], p: Clip) -> bool:
    """`p` is a piece of `b` swapped to another camera: another UPLOAD over a
    PART of the clip, or over the whole clip with its window shifted. A
    piece that plays a derivative of the clip's own upload (a reframe, a
    denoise, an upscale — `<render>.origin`, agent/media_origin) is a media
    change at any piece count, which the attribute rule says as "media a -> b";
    so is a whole clip that plays a derivative at the same window."""
    if p.src == b.src or _same_upload(p.src, b.src):
        return False
    return len(pieces) >= 2 or (abs(p.in_ - b.in_) > EPS or abs(p.out - b.out) > EPS)


def angle_windows(b: Clip, pieces: list[Clip]) -> dict[str, tuple[float, float]]:
    """Parent-source window per piece of `b`: a piece of the same file is its
    own `(in, out)`; an angle piece is placed after the previous window."""
    out: dict[str, tuple[float, float]] = {}
    pos = b.in_
    for p in sorted(pieces, key=lambda p: p.start):
        if not is_angle_piece(b, pieces, p):
            out[p.id] = (p.in_, p.out)
            pos = p.out
            continue
        s0 = min(max(pos, b.in_), b.out)
        s1 = min(s0 + p.duration, b.out)
        out[p.id] = (s0, s1)
        pos = s1
    return out


def camera_lines(s: "Summary", before: list[Clip], pieces: dict[str, list[Clip]]) -> None:
    """One line per (clip, angle): the spans that now show the other camera."""
    groups: dict[tuple[str, str], list[Clip]] = {}
    for b in before:
        for p in pieces.get(b.id, []):
            if is_angle_piece(b, pieces[b.id], p):
                groups.setdefault((b.id, p.src), []).append(p)
    for (bid, angle), ps in groups.items():
        ps.sort(key=lambda p: p.start)
        keys = set().union(*(s.keys_of(p.id) for p in ps))
        spans = [s.names.span(p.start, p.start + p.effective_duration) for p in ps]
        listed = ", ".join(spans[:3]) + (f", +{len(spans) - 3} more" if len(spans) > 3 else "")
        was = s.names.media(next(b.src for b in before if b.id == bid))
        what = spans[0] if len(ps) == 1 else f"{len(ps)} spans ({listed})"
        s.add("Video", f"Camera: {what} show{'s' if len(ps) == 1 else ''} '{s.names.media(angle)}' "
                       f"instead of '{was}'", keys)


def _dialogue_lane_of(s: "Summary", track) -> tuple[list[Clip], str] | None:
    """The new clips of ONE source on an audio lane, each starting where a
    v1 piece starts — `sync_dialogue_lane`'s signature — or None."""
    before_ids = {c.id for t in s.before.tracks for c in t.clips}
    new = [c for c in track.clips if isinstance(c, Clip) and c.id not in before_ids]
    if not new or len({c.src for c in new}) != 1:
        return None
    starts = {round(c.start, 3) for c in _v1(s.after)}
    if not all(round(c.start, 3) in starts or any(abs(c.start - t) <= 0.5 for t in starts) for c in new):
        return None
    return new, new[0].src


def _muted_for_dialogue(s: "Summary", src: str) -> list[str]:
    """v1 pieces of another file whose `audio.mute` turned on in this diff."""
    before_ids = {c.id for t in s.before.tracks for c in t.clips}
    out: list[str] = []
    for c in _v1(s.after):
        k = f"clip:{c.id}.audio.mute"
        if c.src == src or k not in s.pending or c.audio.mute is not True:
            continue
        parent = c.id if c.id in before_ids else split_parent(c.id, before_ids)
        was = s.fb.get(f"clip:{parent}.audio.mute") if parent else None
        if was is not True:
            out.append(k)
    return out


def dialogue_lane_lines(s: "Summary") -> None:
    """The dialogue lane as one line (plus one for the muted camera sound),
    instead of one "Added audio" per piece and one "muted" per clip."""
    for t in s.after.tracks:
        if t.type != "audio":
            continue
        found = _dialogue_lane_of(s, t)
        if found is None:
            continue
        new, src = found
        keys = set().union(*(s.keys_of(c.id) for c in new))
        after_ids = {c.id for c in t.clips}
        gone = [c for tb in s.before.tracks if tb.id == t.id for c in tb.clips
                if isinstance(c, Clip) and c.src == src and c.id not in after_ids]
        music_gone = [c for tb in s.before.tracks if tb.type == "music" for c in tb.clips
                      if isinstance(c, Clip) and c.src == src
                      and c.id not in {x.id for tm in s.after.tracks for x in tm.clips}]
        for c in gone + music_gone:
            keys |= s.keys_of(c.id)
        keys |= {k for k in s.pending if k == f"track:{t.id}" or k.startswith(f"track:{t.id}.")}
        lo = min(c.start for c in new)
        hi = max(c.start + c.effective_duration for c in new)
        n = len(new)
        text = (f"Dialogue from '{s.names.media(src)}' on the {s.names.track(t.id)}: {n} piece"
                f"{'s' if n != 1 else ''} in step with the video ({s.names.span(lo, hi)}, "
                f"{secs(sum(c.effective_duration for c in new))})")
        if gone:
            text += "; replaces the lane's earlier pieces"
        if music_gone:
            text += "; the recorder was on the Music lane, it is now the dialogue"
        s.add("Audio", text, keys)
        muted = _muted_for_dialogue(s, src)
        if muted:
            m = len(muted)
            s.add("Audio", f"Camera sound muted on {m} video clip{'s' if m != 1 else ''}: the dialogue "
                           f"plays from the {s.names.track(t.id)}", set(muted))
