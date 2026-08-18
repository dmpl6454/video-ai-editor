"""Text presets must be POSITIONABLE — no preset may wear the caption role.

Reported as "The hashtag text can't be repositioned with cursor or the
coordinates."

`caption` is the one role whose position is owned server-side by the captions
block, and FOUR independent places enforce that:

  * resolve_anchor_overrides pins it to (None, None)
  * _y_for_role ignores transform_y for it
  * TextLayer.resolveAnchor returns {ax: null, ay: null}
  * StickerLayer refuses to publish a draggable box for it

All four are right for a real caption TRACK, where the block lays out every line.
All four applied to the #Hashtag preset by accident, because the role was chosen
for its FONT (caption is the only role whose font is Inter-Black). The giveaway:
`add_text` puts these clips on the generic `text` track, so nothing owns their
position and nothing should be overriding it.

The invariant is asserted over EVERY preset, not just the hashtag, because the
next one added would inherit the same trap.
"""
import pathlib
import tempfile

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.render.text_overlay import (
    cache_text_pngs, collect_text_clips, resolve_anchor_overrides,
)

# A DELIBERATELY MODEST move. The big presets set their text at 170-220px, so
# centring one near the canvas edge pushes half the glyphs outside the frame,
# where they are clipped — the visible ink centroid then shifts by less than the
# anchor did and the measurement silently stops meaning what it says. Measured:
# countdown_3_2_1 moved -304.5px for a -340px anchor change at x=200.
TX, TY = 450, 700

PRESETS = [
    ("hashtag_chunky", {"hashtag": "ijk"}),
    ("callout_arrow", {"text": "look"}),
    ("big_question", {"text": "why though"}),
    ("end_card_handle", {"handle": "@me"}),
    ("countdown_3_2_1", {}),
    ("watermark_handle", {"handle": "@me"}),
]


def _make(name, fields):
    s = EDLStore(pathlib.Path(tempfile.mkdtemp()))
    r = dispatch(s, "apply_text_template",
                 {"name": name, "fields": fields, "start": 0, "end": 3})
    cid = r.get("clip_id") or r["id"]
    return s, cid


def _role(s, cid):
    return next(rl for c, rl in collect_text_clips(s.edl) if c.id == cid)


def _centroid(png):
    a = np.array(Image.open(png))[:, :, 3]
    ys, xs = np.nonzero(a)
    assert len(xs), "the preset rendered no ink at all"
    return xs.mean(), ys.mean()


@pytest.mark.parametrize("name,fields", PRESETS, ids=[p[0] for p in PRESETS])
def test_no_preset_uses_the_position_locked_caption_role(name, fields):
    s, cid = _make(name, fields)
    assert _role(s, cid) != "caption", (
        f"{name} would be unpositionable: the caption role's x/y are owned by "
        "the captions block and ignored everywhere else")


@pytest.mark.parametrize("name,fields", PRESETS, ids=[p[0] for p in PRESETS])
def test_every_preset_honours_an_explicit_coordinate(name, fields):
    """The panel's x/y fields, end to end into baked pixels.

    Asserted on the PNG's ink centroid rather than on the stored transform: the
    clip accepting an x/y it then ignores at render time is exactly the reported
    bug, so storing it proves nothing.
    """
    s, cid = _make(name, fields)
    role = _role(s, cid)
    _, c = s.edl.get_clip(cid)
    # Where it starts, as the renderer resolves it — a None x means "centre".
    ax0, ay0 = resolve_anchor_overrides(c, role, s.edl.canvas.w, s.edl.canvas.h)
    eff_x0 = s.edl.canvas.w / 2 if ax0 is None else ax0
    from video_ai_editor.render.text_overlay import _y_for_role
    eff_y0 = _y_for_role(role, ay0, s.edl.canvas.h)
    before = _centroid(cache_text_pngs(s.edl, pathlib.Path(tempfile.mkdtemp()))[0][2])

    dispatch(s, "set_clip_transform", {"clip_id": cid, "x": TX, "y": TY})
    _, c = s.edl.get_clip(cid)
    ax, ay = resolve_anchor_overrides(c, role, s.edl.canvas.w, s.edl.canvas.h)
    assert (ax, ay) == (float(TX), float(TY)), f"{name}: anchors resolved to {(ax, ay)}"

    after = _centroid(cache_text_pngs(s.edl, pathlib.Path(tempfile.mkdtemp()))[0][2])
    # Compared as a DELTA, not an absolute landing point: the ink centroid is not
    # the box centre (countdown_3_2_1 sets "3 · 2 · 1" at 220px, whose glyphs are
    # distributed asymmetrically about it), so an absolute check would encode each
    # preset's typography instead of its positioning. The shift, however, is
    # exactly the anchor change whatever the glyphs look like.
    dx, dy = after[0] - before[0], after[1] - before[1]
    assert abs(dx - (TX - eff_x0)) < 2, f"{name}: moved {dx:.1f}px in x, expected {TX - eff_x0:.1f}"
    assert abs(dy - (TY - eff_y0)) < 2, f"{name}: moved {dy:.1f}px in y, expected {TY - eff_y0:.1f}"


def test_the_hashtag_keeps_its_chunky_size():
    """Switching off the caption role must not shrink it — the look is why that
    role was picked, so size/outline are now stated explicitly on the preset."""
    s, cid = _make("hashtag_chunky", {"hashtag": "ijk"})
    _, c = s.edl.get_clip(cid)
    assert c.style.size == pytest.approx(64)
    assert c.style.stroke_w == pytest.approx(5)


def test_a_real_caption_track_is_still_position_locked():
    """The lock itself must stay: this fix narrows WHICH clips wear the caption
    role, it does not weaken the rule for captions that a block owns."""
    from video_ai_editor.edl.schema import TextClip

    s = EDLStore(pathlib.Path(tempfile.mkdtemp()))
    cap = TextClip(id="cap1", text="spoken words", start=0, end=2, role="caption")
    cap.transform.x, cap.transform.y = 200.0, 400.0
    s.edl.get_track("tx_caption").clips.append(cap) \
        if s.edl.get_track("tx_caption") else None
    ax, ay = resolve_anchor_overrides(cap, "caption", 1080, 1920)
    assert (ax, ay) == (None, None), "caption positioning must stay server-owned"
