"""Final QA: `set_text` changes what an existing text overlay SAYS and
nothing else (style, place, timing, animation stay); it refuses a clip that
is not a text overlay and empty words. The Prompt bar's retext recipe is the
reason it exists (tests/test_final_qa_prompt_phrases.py)."""
from __future__ import annotations

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore


def test_set_text_keeps_everything_but_the_words(tmp_path):
    st = EDLStore(tmp_path)
    tid = dispatch(st, "add_text", {"text": "Summer Trip", "start": 0.0, "end": 3.0, "y": 810,
                                    "size": 96, "anim_in": "fade"})["id"]
    before = st.edl.get_clip(tid)[1].model_dump(exclude={"text"})
    out = dispatch(st, "set_text", {"clip_id": tid, "text": "Winter Trip"})
    c = st.edl.get_clip(tid)[1]
    assert c.text == "Winter Trip" and out["old_text"] == "Summer Trip"
    assert c.model_dump(exclude={"text"}) == before
    assert st.ops.last().tool == "set_text"
    assert st.undo() and st.edl.get_clip(tid)[1].text == "Summer Trip"


def test_set_text_refuses_a_media_clip_and_empty_words(tmp_path):
    st = EDLStore(tmp_path)
    cid = dispatch(st, "add_clip", {"track": "v1", "src": str(tmp_path / "nope" / "x.mp4"),
                                    "in": 0, "out": 2, "start": 0})["clip_id"]
    tid = dispatch(st, "add_text", {"text": "A", "start": 0.0, "end": 1.0})["id"]
    with pytest.raises(ValueError, match="not a text overlay"):
        dispatch(st, "set_text", {"clip_id": cid, "text": "B"})
    with pytest.raises(ValueError):
        dispatch(st, "set_text", {"clip_id": tid, "text": "   "})
