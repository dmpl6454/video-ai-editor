"""Clearing a text box must clear the text — in the render, not just the panel.

Reported as "when I applied the text, and delete the text by backspace or
delete, the previous was still showing up, it should be empty and no text should
be there."

The cause was entirely client-side: Properties.commitText refused to dispatch a
BLANK value, on the theory that an empty TextClip is unrecoverable. So the box
went empty while the preview and the export kept the old string — the panel and
the render disagreeing about what the clip says.

The renderer has always handled blank correctly (collect_text_clips skips it),
which is why the fix is a one-line guard removal. These tests pin the server half
so it stays true: if a future change starts baking blank clips, the frontend fix
silently becomes a way to composite an invisible-but-present overlay instead.
"""
import pathlib
import tempfile

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import TextClip
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.render.text_overlay import cache_text_pngs, collect_text_clips


def _store():
    s = EDLStore(pathlib.Path(tempfile.mkdtemp()))
    s.edl.get_track("tx_super").clips.append(
        TextClip(id="t1", text="RADIOACTIVE", start=0, end=3, role="super"))
    s.commit("seed", {}, "seed")
    return s


def test_setting_text_to_empty_is_accepted_and_persisted():
    """The dispatch layer never rejected this; only the panel did."""
    s = _store()
    dispatch(s, "set_property", {"clip_id": "t1", "path": "text", "value": ""})
    _, c = s.edl.get_clip("t1")
    assert c.text == ""


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_a_blank_clip_bakes_no_png_and_no_overlay(blank):
    """Whitespace counts as blank, matching the panel and TextLayer, which both
    compare on a trimmed string. Otherwise a box cleared to a stray space would
    keep rendering while looking empty — the reported bug with extra steps."""
    s = _store()
    dispatch(s, "set_property", {"clip_id": "t1", "path": "text", "value": blank})
    assert collect_text_clips(s.edl) == []
    assert cache_text_pngs(s.edl, pathlib.Path(tempfile.mkdtemp())) == []


def test_the_clip_itself_survives_so_the_text_can_be_typed_again():
    """Emptying a text box is not deleting the element. The clip keeps its place
    on the timeline (where it is labelled "(empty)") and stays addressable, which
    is what makes the removed "never commit blank" guard unnecessary."""
    s = _store()
    dispatch(s, "set_property", {"clip_id": "t1", "path": "text", "value": ""})
    assert s.edl.get_clip("t1") is not None

    dispatch(s, "set_property", {"clip_id": "t1", "path": "text", "value": "BACK"})
    pairs = cache_text_pngs(s.edl, pathlib.Path(tempfile.mkdtemp()))
    assert len(pairs) == 1
    assert np.array(Image.open(pairs[0][2]))[:, :, 3].max() > 0, "text did not come back"


def test_clearing_then_rendering_leaves_no_trace_of_the_old_string():
    """End to end on pixels: the old text must not survive via a cached PNG.

    The cache key hashes the text, so a blank clip cannot collide with its own
    previous content — but the clip is skipped entirely before the key is even
    computed, so this asserts the stronger property: nothing is emitted at all.
    """
    cache = pathlib.Path(tempfile.mkdtemp())
    s = _store()
    before = cache_text_pngs(s.edl, cache)
    assert len(before) == 1
    assert np.array(Image.open(before[0][2]))[:, :, 3].max() > 0

    dispatch(s, "set_property", {"clip_id": "t1", "path": "text", "value": ""})
    # Same cache dir on purpose: a stale PNG from the previous render is exactly
    # what would resurrect the old string if the clip were still collected.
    assert cache_text_pngs(s.edl, cache) == []
