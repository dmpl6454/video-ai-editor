"""The render clock: LAYOUT time → RENDER time for every lane that is not v1.

WHY THIS EXISTS. Transitions live on the v1 track. The compositor cross-fades
two adjacent v1 segments with `xfade`, which PLAYS BOTH CLIPS AT ONCE for the
transition's duration: clip A's last `d` seconds and clip B's first `d`
seconds share one output window. So from the first seam on, every v1 frame
reaches the screen EARLIER than its layout position by the overlap
accumulated so far — `EDL.recompute_duration()` already knows this and sets
`edl.duration = layout end − Σ overlaps`.

Nothing else did. Text, captions, stickers, PiP video, PiP audio, music and
voiceover were all positioned in raw layout time (`enable=between(t,start,
end)`, `-itsoffset start`, `adelay=start`), so after twelve 0.2 s Zoom-Ins a
caption authored on a word landed 2.4 s after the word was spoken. The EDL-
level checks compared layout against layout and could not see it.

THE MODEL (one truth, shared with the desktop's `timelineLayout.ts`):

    render_time(t) = t − Σ{ d_i : seam s_i ≤ t }

over the v1 seams the renderer will actually cross-fade. A layout window
`[start, end)` maps to `[render_time(start), render_time(end))`; a window
that maps to zero or negative length — an overlay living entirely inside a
seam's consumed span — is DROPPED, never inverted (the desktop draws such a
clip as dropped for the same reason; both sides must agree or the canvas
promises something the file does not show).

WHY `seam ≤ t` AND NOT `<`: the seam is clip B's first frame, and B's first
frame is already inside the cross-fade window — it has already been pulled
left by that seam's overlap. Consequently an overlay on clip A's consumed
tail `[s − d, s)` maps to `[s − d, s − d)` and is dropped, while one on B's
head `[s, s + d)` maps to `[s − D − d, s − D)` and shows during the dissolve:
the crossfade window shows B's side of the overlay lanes, not both (two
captions stacked over one dissolve is not a look anyone authored).

WHICH SEAMS COUNT is NOT decided here. `EDL.v1_seam_table()` is the single
copy of the seam-matching rule (adjacent clips only, one transition per
seam within the renderer's 0.05 s tolerance, cost clamped to the shorter
neighbour); `transition_overlap()` is its sum and this module walks it. A
second copy of that rule in the renderer is exactly how the overlay lanes
drifted for as long as they did, so this file deliberately has no opinion
about transitions — only about arithmetic on the table.

Layout time stays the EDL's coordinate space. Nothing in edl/, dispatch,
timemap or the mobile app converts; only the renderer (through these three
functions) and the desktop's draw/write-back do.
"""
from __future__ import annotations

from typing import Sequence

from ..edl.schema import EDL

#: `(seam_layout_time, seconds_removed)` ascending — the shape
#: `EDL.v1_seam_table()` returns. Callers that position many items build it
#: once and pass it in; every function here also accepts the EDL itself.
SeamTable = Sequence[tuple[float, float]]

#: Same epsilon the desktop's `timelineLayout.ts` uses (`SEAM_EPS`), so a
#: window that the canvas draws as dropped is the one the renderer drops.
#: 1 µs: far below a frame, far above float noise on sums of durations.
SEAM_EPS = 1e-6


def seam_table(edl: EDL) -> list[tuple[float, float]]:
    """The seam table for `edl` — a plain list so callers can hold it across
    a whole filtergraph build instead of re-deriving it per item."""
    return list(edl.v1_seam_table())


def _seams(src: EDL | SeamTable) -> SeamTable:
    return src.v1_seam_table() if isinstance(src, EDL) else src


def overlap_before(src: EDL | SeamTable, t: float) -> float:
    """Seconds the cross-fades at or before layout instant `t` have removed
    from the output. 0.0 with no transitions; `transition_overlap()` past
    the last seam."""
    t = float(t)
    return sum(cost for seam, cost in _seams(src) if seam <= t + SEAM_EPS)


def render_time(src: EDL | SeamTable, t: float) -> float:
    """Where layout instant `t` lands on the rendered file's clock."""
    return float(t) - overlap_before(src, t)


def render_window(src: EDL | SeamTable, start: float, end: float
                  ) -> tuple[float, float] | None:
    """`[render_time(start), render_time(end))`, or None when that window has
    no positive length — the caller then emits nothing for the item (no
    input, no filter, no audio), which is what "dropped" means downstream.

    A window is never clamped or inverted: an item straddling a seam simply
    gets shorter by what the seam consumed, because that is how long it is
    on screen — A's tail and B's head under it are the same output frames.
    """
    seams = _seams(src)
    rs = render_time(seams, start)
    re = render_time(seams, end)
    if re - rs <= SEAM_EPS:
        return None
    return rs, re


def sound_window(src: EDL | SeamTable, start: float, length: float
                 ) -> tuple[float, float] | None:
    """`[render_time(start), render_time(start) + length)` for a SOUND lane
    (`schema.sound_lane`: music, voiceover, audio tracks), or None for a clip
    with no length.

    Final QA (round 3): unlike `render_window` it never shrinks. A
    transition overlaps the main-track PICTURES; a voiceover running across
    it is not two pictures sharing frames, and shrinking its window cut its
    last words by every seam it crossed (a 7 s voiceover over a dissolve and
    a clock wipe lost 0.9 s of speech). It still starts where its start
    plays, so a bed authored under a word dips where the word is heard."""
    if length <= SEAM_EPS:
        return None
    rs = render_time(src, start)
    return rs, rs + float(length)


def sound_windows(clips, seams: SeamTable, video_end: float
                  ) -> dict[str, tuple[float, float]]:
    """The render windows of every audible clip of ONE sound lane, by id —
    `schema.sound_render_windows`, the one copy of the sound-lane seam rule:
    a run of abutting clips (a split voiceover, a looped bed) starts where
    its first clip's start plays (`schema.sound_pulls`) and stays back to
    back; it plays whole when it ends inside the programme, and is cut where
    its layout end maps when it was laid to or past v1's layout end
    (`video_end`, `EDL.video_extent()`), so a bed aligned to the end of the
    video ends with the picture (final QA, K1)."""
    from ..edl.schema import sound_render_windows
    return sound_render_windows(list(clips), list(seams), float(video_end))


def linked_text_windows(edl: EDL) -> dict[str, tuple[float, float] | None]:
    """Text id → render window of every text clip LINKED to a sound-lane or
    PIP clip that still exists (`TextClip.linked_to`, final sweep 3 r2).

    A linked caption is stored on its clip's own clock (`clip.start` plus how
    far into the clip its words are heard), so it plays at
    ``rs + (cue.start − clip.start)`` of that clip's render window — the
    sound lane's (`sound_windows`) or the PIP's (`render_window` over its
    footprint, `pip.pip_layout_end`) — and stops no later than the clip
    does. None: the clip is not heard there (past its run's cut) or the cue
    falls outside it, and the renderer drops it. A text clip that is not
    linked, or whose clip is gone, is absent: the caller uses
    `render_window` (the layout clock) for it, as for every other overlay.
    Mirrored by `frontend/src/lib/timelineLayout.linkedTextSpan`."""
    from ..edl.schema import Clip, TextClip, sound_lane
    seams = seam_table(edl)
    hosts: dict[str, tuple] = {}
    for t in edl.tracks:
        for c in t.clips:
            if isinstance(c, Clip):
                hosts[c.id] = (t, c)
    lane_windows: dict[str, dict[str, tuple[float, float]]] = {}
    out: dict[str, tuple[float, float] | None] = {}
    for t in edl.tracks:
        for x in t.clips:
            link = getattr(x, "linked_to", None)
            if not isinstance(x, TextClip) or not link or link not in hosts:
                continue
            lane, c = hosts[link]
            if sound_lane(lane):
                if lane.id not in lane_windows:
                    lane_windows[lane.id] = sound_windows(lane.clips, seams, edl.video_extent())
                win = lane_windows[lane.id].get(c.id)
            elif lane.type == "video" and lane.id != "v1":
                win = render_window(seams, float(c.start), float(c.start) + float(c.effective_duration))
            else:
                continue                        # not a caption source: the layout clock
            if win is None:
                out[x.id] = None
                continue
            rs = win[0] + (float(x.start) - float(c.start))
            re = min(win[0] + (float(x.end) - float(c.start)), win[1])
            out[x.id] = (rs, re) if re - rs > SEAM_EPS else None
    return out


def text_window(seams: SeamTable, linked: dict[str, tuple[float, float] | None],
                clip_id: str | None, start: float, end: float
                ) -> tuple[float, float] | None:
    """Where one text / sticker overlay plays: its linked clip's window
    (`linked_text_windows`) when it has one, else `render_window`."""
    if clip_id is not None and clip_id in linked:
        return linked[clip_id]
    return render_window(seams, start, end)


__all__ = ["SeamTable", "SEAM_EPS", "seam_table", "overlap_before",
           "render_time", "render_window", "sound_window", "sound_windows",
           "linked_text_windows", "text_window"]
