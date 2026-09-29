"""Preview-mode variants of the Prompt-bar suites (0.8.0 "Preview, then apply").

The existing suites test PLAN SEMANTICS with the auto-apply switch
(tests/conftest.py sets "Ask before applying" OFF for their harness). This
file runs a representative sample of their phrasings — cuts, deletes,
moves, speed, reverse, freeze, fades, transitions, zoom, rotate, canvas,
music, mutes, titles, captions — through the REAL key-free service
(grammar → planner → validate → executor → dispatch; the capcut-sweep
session: three 4 s clips, 16:9, a music bed) twice, on two byte-identical
copies of one session:

  * A — auto-apply (the setting OFF): what the phrase commits today;
  * B — preview (the setting ON): the preview must commit NOTHING, show a
    card whose lines are the diff of what A committed, and Apply must then
    commit the very tree A committed (the same fingerprint, the same lines)
    as one op.

So preview mode changes WHEN an edit lands, never WHAT lands.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401
from test_prompt_capcut_sweep import _session, media  # noqa: E402,F401

from video_ai_editor.agent.prompt import changes as C  # noqa: E402
from video_ai_editor.agent.prompt import executor, pending, service  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

#: (phrase, ui) — one or two per family of the capcut sweep / K3 corpus.
SAMPLE: list[tuple[str, dict]] = [
    ("trim the first 2 seconds", {}),
    ("delete the second clip", {}),
    ("move the last clip to the start", {}),
    ("duplicate the second clip", {}),
    ("speed up the second clip 2x", {}),
    ("reverse the second clip", {}),
    ("freeze the frame at 2s for 2 seconds", {}),
    ("fade out the last clip over 2 seconds", {}),
    ("add a cross dissolve between the clips", {}),
    ("zoom this clip to 150%", {"selection": "B"}),
    ("rotate the first clip 90 degrees", {}),
    ("make it 9:16", {}),
    ("turn the music down", {}),
    ("mute the second clip", {}),
    ("add a title saying Summer Trip", {}),
    ("split at 3 seconds", {}),
]


def _turn(st: EDLStore, phrase: str, ui: dict, *, confirm: bool) -> list[dict]:
    events = F.collect(service.prompt_turn(st, phrase, [], brain="recipes", ui_state=ui, confirm=confirm))
    h = executor.get_run(Path(st.dir).name)
    if h is not None and h.thread is not None:
        h.thread.join(30.0)
    assert events and events[-1]["type"] == "done", events[-3:]
    return events


def _twins(media: dict, n: int) -> tuple[EDLStore, EDLStore, dict[str, str]]:
    a, ids = _session(media, f"s_pva{n:03d}")
    dst = media["root"] / f"s_pvb{n:03d}"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(a.dir, dst)
    return a, EDLStore(dst), ids


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("i", range(len(SAMPLE)), ids=[p for p, _ in SAMPLE])
def test_preview_then_apply_commits_what_auto_apply_commits(media, i):
    phrase, ui_raw = SAMPLE[i]
    a, b, ids = _twins(media, i)
    ui = {"playhead": 5.5, **{k: (ids[v] if k == "selection" else v) for k, v in ui_raw.items()}}
    assert a.edl.to_json() == b.edl.to_json()
    a_before, b_before = a.edl.model_copy(deep=True), b.edl.model_copy(deep=True)
    b_ops = len(b.ops.ops)

    (media["src"].parent / "ingest.json").write_bytes(media["ingest"])
    _turn(a, phrase, ui, confirm=False)
    assert a.edl.hash() != a_before.hash(), f"{phrase!r} changed nothing in auto mode — not a sample of an edit"

    (media["src"].parent / "ingest.json").write_bytes(media["ingest"])
    events = _turn(b, phrase, ui, confirm=True)
    assert b.edl.to_json() == b_before.to_json() and len(b.ops.ops) == b_ops, "the preview committed"
    card = [e for e in events if e["type"] == "clarify" and e.get("preview")]
    assert card, [(e["type"], e.get("text")) for e in events]
    lines_a = [c.text for c in C.summarize(a_before, a.edl, session_dir=Path(b.dir))]
    record = pending.load_pending(Path(b.dir))
    assert record["preview"]["all_lines"] == lines_a, "the card must list what auto-apply commits"

    applied = F.collect(service.resume(b, card[-1]["token"], {"apply": "yes"}))
    h = executor.get_run(Path(b.dir).name)
    h.thread.join(30.0)
    assert [e["type"] for e in applied].count("op") == 1, [(e["type"], e.get("text")) for e in applied]
    assert len(b.ops.ops) == b_ops + 1
    assert C.canonical(b.edl, b_before) == C.canonical(a.edl, a_before, {str(a.dir): str(b.dir)})
    assert C.canonical(b.edl, b_before) == record["preview"]["fingerprint"]
