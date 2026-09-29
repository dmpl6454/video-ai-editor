"""A UI text insert (allow_stack) over an existing text goes to its OWN lane.

Final sweep 2: Callout → then "Add text at playhead" at the same playhead put
both clips on track "text" over the same [start, end). The lane drew one block
(with the dashed "legacy overlapping clips" outline) and a click always picked
the arrow underneath, so the text whose label showed could never be selected
from the timeline. CapCut puts the second text on a new track.
"""
from __future__ import annotations

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import TextClip
from video_ai_editor.edl.snapshot import EDLStore


def _lane_of(store: EDLStore, cid: str) -> str:
    return next(t.id for t in store.edl.tracks for c in t.clips if c.id == cid)


def _overlaps_on_lane(store: EDLStore) -> list[str]:
    bad = []
    for t in store.edl.tracks:
        txt = sorted((c for c in t.clips if isinstance(c, TextClip)), key=lambda c: c.start)
        for a, b in zip(txt, txt[1:]):
            if b.start < a.end - 1e-9:
                bad.append(t.id)
    return bad


def test_template_then_default_text_land_on_different_lanes(tmp_path):
    s = EDLStore(tmp_path / "s")
    arrow = dispatch(s, "apply_text_template", {"name": "callout_arrow", "start": 3, "end": 6,
                                                "fields": {"text": "", "hashtag": "", "handle": ""},
                                                "allow_stack": True})["id"]
    yours = dispatch(s, "add_text", {"text": "Your text", "start": 3, "end": 6, "role": "super",
                                     "allow_stack": True})["id"]
    assert _lane_of(s, arrow) == "text"
    assert _lane_of(s, yours) != "text"
    assert s.edl.get_track(_lane_of(s, yours)).type == "text"
    assert _overlaps_on_lane(s) == []
    # both survive (a UI insert never deletes content)
    ids = {c.id for t in s.edl.tracks for c in t.clips}
    assert {arrow, yours} <= ids


def test_a_third_stacked_text_gets_a_third_lane_and_a_free_lane_is_reused(tmp_path):
    s = EDLStore(tmp_path / "s")
    a = dispatch(s, "add_text", {"text": "A", "start": 0, "end": 3, "allow_stack": True})["id"]
    b = dispatch(s, "add_text", {"text": "B", "start": 1, "end": 4, "allow_stack": True})["id"]
    c = dispatch(s, "add_text", {"text": "C", "start": 2, "end": 5, "allow_stack": True})["id"]
    assert len({_lane_of(s, a), _lane_of(s, b), _lane_of(s, c)}) == 3
    # after all three, [10, 12) is free on "text": it goes there
    d = dispatch(s, "add_text", {"text": "D", "start": 10, "end": 12, "allow_stack": True})["id"]
    assert _lane_of(s, d) == "text"
    assert _overlaps_on_lane(s) == []
    labels = [t.label for t in s.edl.tracks if t.type == "text"]
    assert "Text 2" in labels and "Text 3" in labels


def test_the_chat_default_still_replaces_same_role_overlap(tmp_path):
    """Without allow_stack the double-subtitle guard is unchanged."""
    s = EDLStore(tmp_path / "s")
    dispatch(s, "add_text", {"text": "A", "start": 0, "end": 3, "role": "super"})
    r = dispatch(s, "add_text", {"text": "B", "start": 1, "end": 4, "role": "super"})
    assert r.get("replaced")
    texts = [c.text for t in s.edl.tracks for c in t.clips if isinstance(c, TextClip)]
    assert texts == ["B"]


def test_a_locked_text_lane_is_skipped(tmp_path):
    s = EDLStore(tmp_path / "s")
    dispatch(s, "add_text", {"text": "A", "start": 0, "end": 3, "allow_stack": True})
    s.edl.get_track("text").locked = True
    b = dispatch(s, "add_text", {"text": "B", "start": 10, "end": 12, "allow_stack": True})["id"]
    assert _lane_of(s, b) != "text"
