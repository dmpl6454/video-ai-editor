"""Final sweep 3, round 2 — Prompt bar (key-free) fixes, driven through the
real Recipes planner and `service.prompt_turn`, as the Prompt bar runs it.

Owner decision for 0.8.0: the key-free Prompt bar is PREVIEW, THEN APPLY —
nothing it plans may change the timeline until the person presses Apply.

  * a prompt that only STARTS with "undo" / "revert" ("revert clip 2 to
    normal speed", "undo the black and white") undid the LAST edit at once,
    with no card, whatever it named; "undo" typed over an open card undid an
    earlier edit although the card said "Nothing has changed yet";
  * the card called a title that Apply shrinks from 3 s to a 0.1 s stub (or
    halves / doubles with a speed change) "moves with the video";
  * the "Added title" line hid the look and place the title would get;
  * "add a crossfade" on a captioned project re-laid the captions from the
    first clip's transcript (lost, restyled or rebuilt them) and dead-ended;
  * and the MEDIUM misreadings listed per test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import desktop_posture, no_downloads  # noqa: E402,F401

from video_ai_editor import storage  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import pending, service  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

pytestmark = pytest.mark.usefixtures("desktop_posture", "no_downloads")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(service, "_RESOLVE_STORE", None)
    return tmp_path


def _turn(st, message: str, *, confirm: bool = True, ui: dict | None = None) -> list[dict]:
    return F.collect(service.prompt_turn(st, message, [], brain="recipes", ui_state=ui or {}, confirm=confirm))


def _text(frames: list[dict]) -> str:
    return "".join(f.get("text", "") for f in frames if f["type"] == "text_delta") + " ".join(
        f.get("message", "") for f in frames if f["type"] == "error")


def _card(frames: list[dict]) -> dict | None:
    c = [f for f in frames if f["type"] == "clarify" and f.get("preview")]
    return c[-1]["preview"] if c else None


def _plan(frames: list[dict]) -> dict:
    return [f for f in frames if f["type"] == "plan"][-1]["plan"]


def _lines(card: dict) -> list[str]:
    return list(card.get("lines") or []) + list(card.get("hidden") or [])


def _three(root: Path) -> EDLStore:
    """v1 = one speech clip split into three (0-4, 4-8, 8-12)."""
    src = F.speech_clip(root)
    F.write_ingest(src)
    st = EDLStore(root / "s1")
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 12, "start": 0})
    dispatch(st, "split_at", {"track": "v1", "time": 4.0})
    dispatch(st, "split_at", {"track": "v1", "time": 8.0})
    return st


def _v1(st) -> list:
    return list(st.edl.get_track("v1").clips)


def _apply(st, frames: list[dict]) -> list[dict]:
    assert _card(frames) is not None, _text(frames)
    return _turn(st, "yes")


# --------------------------------------------------------------------------
# CRITICAL: a named undo / revert
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "undo the speed change on clip 2", "revert the title", "revert the colour on clip 1",
    "undo the black and white", "undo the fade and make clip 1 louder",
])
def test_a_named_undo_never_undoes_the_last_edit(env, message):
    st = _three(env)
    ids = [c.id for c in _v1(st)]
    dispatch(st, "set_speed", {"clip_id": ids[1], "factor": 2.0})
    dispatch(st, "set_clip_muted", {"clip_id": ids[2], "muted": True})
    before, depth = st.edl.hash(), st.undo_depth
    for confirm in (True, False):
        frames = _turn(st, message, confirm=confirm)
        assert st.edl.hash() == before, (message, confirm, _text(frames))
        assert st.undo_depth == depth
        assert not [f for f in frames if f["type"] == "op"]
        text = _text(frames)
        # it names the last edit in words (never a raw clip id) and says how to undo it
        assert "Clip 3" in text and "undo" in text.lower(), text
        assert "c_" not in text, text


def test_revert_to_normal_speed_plans_that_speed_change(env):
    st = _three(env)
    ids = [c.id for c in _v1(st)]
    dispatch(st, "set_speed", {"clip_id": ids[1], "factor": 2.0})
    dispatch(st, "set_clip_muted", {"clip_id": ids[2], "muted": True})
    before = st.edl.hash()
    frames = _turn(st, "revert clip 2 to normal speed")
    assert st.edl.hash() == before
    card = _card(frames)
    assert card is not None, _text(frames)
    joined = " ".join(_lines(card))
    assert "Clip 2" in joined and "1x" in joined, joined
    _apply(st, frames)
    assert _v1(st)[1].speed == 1.0 and _v1(st)[2].audio.mute is True


@pytest.mark.parametrize("message", ["undo", "go back", "revert that"])
def test_undo_typed_over_an_open_card_drops_the_card_and_undoes_nothing(env, message):
    st = _three(env)
    bed = F.music_bed(env, dur=12.0)
    dispatch(st, "add_music", {"src": str(bed)})
    before, depth = st.edl.hash(), st.undo_depth
    assert _card(_turn(st, "mute clip 3")) is not None
    frames = _turn(st, message)
    assert st.edl.hash() == before and st.undo_depth == depth, _text(frames)
    assert pending.load_pending(Path(st.dir)) is None
    assert "nothing" in _text(frames).lower()


def test_a_bare_undo_still_undoes_and_names_the_clip(env):
    st = _three(env)
    ids = [c.id for c in _v1(st)]
    dispatch(st, "set_clip_muted", {"clip_id": ids[2], "muted": True})
    frames = _turn(st, "undo")
    assert _v1(st)[2].audio.mute is False
    text = _text(frames)
    assert "Undid" in text and "Clip 3" in text and "c_" not in text, text


# --------------------------------------------------------------------------
# CRITICAL: a title that Apply shrinks or stretches is not "moving with the video"
# --------------------------------------------------------------------------

def _titled(root: Path, *titles: tuple[str, float, float]) -> EDLStore:
    st = _three(root)
    dispatch(st, "add_music", {"src": str(F.music_bed(root, dur=12.0))})
    for text, a, b in titles:
        dispatch(st, "add_text", {"text": text, "start": a, "end": b, "allow_stack": True})
    return st


def _title_span(st, text: str) -> tuple[float, float]:
    c = next(c for t in st.edl.tracks for c in t.clips if getattr(c, "text", None) == text)
    return round(c.start, 2), round(c.end, 2)


@pytest.mark.parametrize("message, title, applied", [
    ("delete the first clip", ("Summer Trip", 0.0, 3.0), (0.0, 0.1)),
    ("remove 0s to 3s", ("Summer Trip", 0.0, 3.0), (0.0, 0.1)),
    ("delete clip 2", ("SALE", 5.0, 7.0), (4.0, 4.1)),
    ("speed up clip 1 to 2x", ("Day One", 0.0, 3.0), (0.0, 1.5)),
    ("make clip 1 half speed", ("Day One", 0.0, 3.0), (0.0, 6.0)),
])
def test_a_title_whose_length_changes_gets_its_own_line(env, message, title, applied):
    st = _titled(env, title)
    frames = _turn(st, message)
    card = _card(frames)
    assert card is not None, _text(frames)
    lines = _lines(card)
    mine = [ln for ln in lines if f"'{title[0].upper()}'" in ln.upper() and "Title" in ln]
    assert mine, lines
    assert not any("move with the video" in ln or "moves with the video" in ln for ln in lines
                   if "title" in ln.lower()), lines
    _apply(st, frames)
    assert _title_span(st, title[0]) == applied
    a, b = applied
    # the line says how long it will show
    assert any(f"{round(b - a, 1):.1f} s" in ln for ln in mine), mine


def test_a_title_that_only_shifts_still_folds_into_the_follow_line(env):
    st = _titled(env, ("Late", 9.0, 11.0))
    card = _card(_turn(st, "delete clip 2"))
    lines = _lines(card)
    assert any("1 title moves with the video" in ln for ln in lines), lines


# --------------------------------------------------------------------------
# MEDIUM: a preview that would change nothing says so as its own outcome
# --------------------------------------------------------------------------

def test_a_preview_that_changes_nothing_is_marked_as_such(env):
    st = _three(env)
    before = st.edl.hash()
    frames = _turn(st, "unmute everything")
    assert st.edl.hash() == before and _card(frames) is None
    said = [f for f in frames if f["type"] == "text_delta"]
    assert said and said[-1].get("outcome") == "nothing_to_apply", said
    assert "would not change anything" in said[-1]["text"]


# --------------------------------------------------------------------------
# MEDIUM: a full disk — the run log's own file must not swallow the error
# --------------------------------------------------------------------------

def test_a_full_disk_reaches_the_client_as_an_error_before_done(env, monkeypatch):
    import errno
    from video_ai_editor.agent.prompt import preview as _pv
    from video_ai_editor.agent.prompt import runlog

    st = _three(env)
    before = st.edl.hash()

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device", str(Path(st.dir) / "prompt_run.json.tmp"))

    real_scratch = _pv.scratch_store

    def scratch_full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device", str(Path(st.dir) / "meta.json"))

    monkeypatch.setattr(runlog, "write_record", full)
    monkeypatch.setattr(_pv, "scratch_store", scratch_full)
    frames = _turn(st, "mute the first clip")
    kinds = [f["type"] for f in frames]
    assert "error" in kinds and kinds[-1] == "done", kinds
    assert kinds.index("error") < kinds.index("done")
    msg = next(f for f in frames if f["type"] == "error")["message"]
    assert "disk is full" in msg.lower() and "Errno" not in msg and "prompt_run" not in msg, msg
    assert st.edl.hash() == before
    monkeypatch.setattr(_pv, "scratch_store", real_scratch)


def test_an_apply_is_not_blamed_on_the_run_log_file(env, monkeypatch):
    """Apply on a full disk reported 'Step 1/1 set_clip_muted failed: [Errno
    28] … prompt_run.json.tmp' — the run log's own file, blamed on the edit."""
    import errno
    from video_ai_editor.agent.prompt import runlog

    st = _three(env)
    frames = _turn(st, "mute the first clip")
    assert _card(frames) is not None

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device", "prompt_run.json.tmp")

    monkeypatch.setattr(runlog, "write_record", full)
    done = _turn(st, "yes")
    assert _v1(st)[0].audio.mute is True, _text(done)
    assert "prompt_run" not in _text(done) and "failed" not in _text(done).lower(), _text(done)


# --------------------------------------------------------------------------
# HIGH: transitions on a captioned project keep the captions as they are
# --------------------------------------------------------------------------

def _cues(st) -> list[tuple[str, float, float]]:
    return [(c.text, round(c.start, 2), round(c.end, 2)) for t in st.edl.tracks if t.type == "captions"
            for c in t.clips]


@pytest.mark.parametrize("message", ["add a crossfade between every clip",
                                     "add a Dissolve transition between every clip lasting 0.5 seconds"])
def test_transitions_on_a_captioned_project_do_not_rebuild_the_captions(env, message):
    st = _three(env)
    _apply(st, _turn(st, "add captions"))
    cues = _cues(st)
    assert cues, "the fixture has captions"
    # a hand edit on one cue: a rebuild would lose it
    cap = next(t for t in st.edl.tracks if t.type == "captions")
    dispatch(st, "set_text", {"clip_id": cap.clips[0].id, "text": "HELLO THERE (edited)"})
    cues = _cues(st)
    frames = _turn(st, message)
    card = _card(frames)
    assert card is not None, _text(frames)
    tools = [s["tool"] for s in _plan(frames)["steps"]]
    assert set(tools) == {"add_transition"}, tools
    assert not any("caption" in ln.lower() for ln in _lines(card)), _lines(card)
    _apply(st, frames)
    assert [c[0] for c in _cues(st)] == [c[0] for c in cues]
    assert len(st.edl.get_track("v1").transitions) == 2
    # the benchmark's own check (case 18): no cue runs past the video
    assert max(c[2] for c in _cues(st)) <= float(st.edl.video_extent()) + 0.05


# --------------------------------------------------------------------------
# HIGH: a new title's look and place — honoured, and shown on the card
# --------------------------------------------------------------------------

def _one_clip(root: Path) -> EDLStore:
    src = F.speech_clip(root)
    F.write_ingest(src)
    st = EDLStore(root / "s1")
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 12, "start": 0})
    return st


def _added_text(frames: list[dict]) -> dict:
    return next(s["args"] for s in _plan(frames)["steps"] if s["tool"] == "add_text")


def _title_line(card: dict) -> str:
    return next(ln for ln in _lines(card) if ln.startswith("Added title"))


@pytest.mark.parametrize("message, check, words", [
    ("add a small blue title 'Hi' at the bottom",
     lambda a, w, h: a["y"] > 0.6 * h and a["size"] < 120 and a["color"] == "#0A84FF", ("bottom", "blue")),
    ("add a title 'Menu' at the bottom of the screen", lambda a, w, h: a["y"] > 0.6 * h, ("bottom",)),
    ("put 'LIVE' in the top right corner", lambda a, w, h: a["y"] < 0.34 * h and a["x"] > 0.6 * w, ("top", "right")),
    ("add a green title saying GO in the center", lambda a, w, h: 0.4 * h <= a["y"] <= 0.6 * h, ("middle", "green")),
    ("add 'Sale ends Friday' in a black box",
     lambda a, w, h: a["color"].upper() != "#000000" and str(a.get("background") or "").upper().startswith("#000000"),
     ("black box", "white text")),
    ("add text 'Hello' with no outline", lambda a, w, h: float(a["stroke_w"]) == 0.0, ("no outline",)),
    ("add a title 'Breaking' that slides in from the left", lambda a, w, h: a.get("anim_in") not in (None, "pop"),
     ("slides",)),
    ("write 'Episode 3' at the top left from 0 to 2s",
     lambda a, w, h: a["y"] < 0.34 * h and a["x"] < 0.4 * w and (a["start"], a["end"]) == (0.0, 2.0), ("top", "left")),
])
def test_a_new_titles_look_is_honoured_and_named_on_the_card(env, message, check, words):
    st = _one_clip(env)
    w, h = st.edl.canvas.w, st.edl.canvas.h
    frames = _turn(st, message)
    card = _card(frames)
    assert card is not None, _text(frames)
    args = _added_text(frames)
    assert check(args, w, h), args
    line = _title_line(card)
    for word in words:
        assert word in line, line


def test_the_added_title_line_names_place_colour_outline_and_font():
    from video_ai_editor.agent.prompt import changes as C
    from video_ai_editor.edl.schema import EDL
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        st = EDLStore(Path(d) / "s")
        dispatch(st, "set_canvas", {"w": 1920, "h": 1080})
        before = st.edl.model_copy(deep=True)
        dispatch(st, "add_text", {"text": "Hi", "start": 0.0, "end": 3.0, "x": 960, "y": 324, "size": 140,
                                  "color": "#000000", "stroke": "#000000", "stroke_w": 6, "font": "Anton-Regular",
                                  "anim_in": "pop"})
        line = next(c.text for c in C.summarize(before, st.edl) if c.text.startswith("Added title"))
        assert line.startswith("Added title 'Hi' (00:00:00:00-00:00:03:00)"), line
        for word in ("upper third", "black text", "black outline", "Anton", "pops in", "size 140"):
            assert word in line, line
        assert isinstance(before, EDL)


# --------------------------------------------------------------------------
# MEDIUM: a title "on clip 2" goes on clip 2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message, text", [
    ("add the title 'Step 2: Pour the sauce' on clip 2", "Step 2: Pour the sauce"),
    ("add a title 'Pour' on the second clip", "Pour"),
    ("add a title 'Pour' over clip 2", "Pour"),
    ("add the title Step 2 on clip 2", "Step 2"),
])
def test_a_title_on_a_named_clip_starts_on_that_clip(env, message, text):
    st = _three(env)
    _apply(st, _turn(st, "add a title 'Step 1: Chop the onions' over the first clip"))
    assert _title_span(st, "Step 1: Chop the onions") == (0.0, 3.0)
    frames = _turn(st, message)
    card = _card(frames)
    assert card is not None, _text(frames)
    args = _added_text(frames)
    assert args["text"].lower() == text.lower(), args
    assert (args["start"], args["end"]) == (4.0, 7.0), args
    assert not any("Removed" in ln for ln in _lines(card)), _lines(card)


def test_a_title_on_a_short_clip_ends_with_that_clip(env):
    st = _three(env)
    frames = _turn(st, "add a title 'Last' on the last clip for 6 seconds")
    args = _added_text(frames)
    assert (args["start"], args["end"]) == (8.0, 12.0), args


# --------------------------------------------------------------------------
# MEDIUM: a clip named by its media or shot name is that clip, not every clip
# --------------------------------------------------------------------------

def _recipe(root: Path) -> EDLStore:
    """v1 = r_chop (0-4), r_pour (4-8), r_plate (8-12): three named uploads."""
    st = EDLStore(root / "s1")
    t = 0.0
    for name in ("r_chop", "r_pour", "r_plate"):
        src = F.speech_clip(root, name=name, dur=4.0)
        F.write_ingest(src, with_transcript=False)
        dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 4.0, "start": t})
        t += 4.0
    return st


@pytest.mark.parametrize("message", ["slow down the pour shot to 0.5x", "slow down the pour clip to 0.5x",
                                     "make the r_pour clip 0.5x"])
def test_a_clip_named_by_its_media_is_that_clip(env, message):
    st = _recipe(env)
    ids = [c.id for c in _v1(st)]
    frames = _turn(st, message)
    card = _card(frames)
    assert card is not None, _text(frames)
    speed = [s for s in _plan(frames)["steps"] if s["tool"] == "set_speed"]
    assert speed and all(s["args"]["clip_id"] in (ids[1], "$v1_nth:2") for s in speed), speed
    assert not any(ln.startswith("All 3 clips") for ln in _lines(card)), _lines(card)


def test_a_title_on_a_shot_named_by_its_media(env):
    st = _recipe(env)
    frames = _turn(st, "put a title 'Pour' on the pour shot")
    args = _added_text(frames)
    assert (args["start"], args["end"]) == (4.0, 7.0), args


# --------------------------------------------------------------------------
# LOW: typo-fixed words are judged as read; "move" is never "remove"
# --------------------------------------------------------------------------

def test_a_typo_fixed_request_is_judged_by_the_words_it_was_planned_from(env):
    st = _three(env)
    frames = _turn(st, "spped up the secnd clip a bit")
    card = _card(frames)
    assert card is not None, _text(frames)
    assert any(ln.startswith("Clip 2 ") and "speed" in ln for ln in _lines(card)), _lines(card)


@pytest.mark.parametrize("message", ["move SALE to 9 seconds", "move the playhead to 3s"])
def test_move_is_never_read_as_remove(env, message):
    st = _titled(env, ("SALE", 5.0, 7.0))
    before = st.edl.hash()
    frames = _turn(st, message)
    assert st.edl.hash() == before
    text = _text(frames)
    assert "“remove" not in text and "Which part should I cut" not in text, text


# --------------------------------------------------------------------------
# MEDIUM: an In AND an Out in one sentence are both planned
# --------------------------------------------------------------------------

def _logo(root: Path) -> Path:
    import subprocess
    p = root / "logo.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1",
                    str(p)], check=True, capture_output=True)
    return p


def _anims(frames: list[dict]) -> list[dict]:
    return [s["args"] for s in _plan(frames)["steps"] if s["tool"] == "set_animation"]


@pytest.mark.parametrize("message, want_in, want_out", [
    ("make the logo pop in and spin out", "bounce", "spin"),
    ("make the logo zoom in and spin out", "zoom_in", "spin"),
    ("slide the logo in from the left and fade it out", "slide_right", "fade_out"),
    ("give the logo a zoom in intro and a spin outro", "zoom_in", "spin"),
])
def test_an_in_and_an_out_on_the_sticker(env, message, want_in, want_out):
    st = _three(env)
    dispatch(st, "add_sticker", {"src": str(_logo(env)), "start": 1.0, "end": 5.0})
    sid = next(c.id for t in st.edl.tracks for c in t.clips if t.id.startswith("stickers"))
    frames = _turn(st, message)
    assert _card(frames) is not None, _text(frames)
    tools = [s["tool"] for s in _plan(frames)["steps"]]
    assert set(tools) == {"set_animation"}, tools        # never a fade on the last video clip
    got = {k: v for a in _anims(frames) for k, v in a.items() if k in ("in", "out")}
    assert got == {"in": want_in, "out": want_out}, _anims(frames)
    assert all(a.get("clip_id") == sid for a in _anims(frames)), _anims(frames)


def test_fade_the_overlay_in_and_out(env):
    st = _three(env)
    src = F.speech_clip(env, name="leak", dur=4.0)
    dispatch(st, "add_clip", {"track": "v2", "src": str(src), "in": 0, "out": 4.0, "start": 2.0})
    frames = _turn(st, "fade the overlay in and out")
    got = {k: v for a in _anims(frames) for k, v in a.items() if k in ("in", "out")}
    assert got == {"in": "fade_in", "out": "fade_out"}, (_anims(frames), _text(frames))


# --------------------------------------------------------------------------
# MEDIUM: "remove the silences" keeps a B-roll / product shot with no sound
# --------------------------------------------------------------------------

def _quiet_clip(root: Path, name: str = "a_prod", dur: float = 6.0) -> Path:
    import subprocess
    d = root / "uploads" / name
    d.mkdir(parents=True, exist_ok=True)
    src = d / f"{name}.normalized.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=green:s=320x180:d={dur}:r=30",
                    "-f", "lavfi", "-i", f"sine=f=220:d={dur}", "-af", "volume=0.01",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(src)], check=True, capture_output=True)
    F.write_ingest(src, with_transcript=False)
    return src


def test_remove_the_silences_keeps_a_clip_that_has_no_sound(env):
    src = F.speech_clip(env)
    F.write_ingest(src)
    st = EDLStore(env / "s1")
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 12, "start": 0})
    prod = _quiet_clip(env)
    dispatch(st, "add_clip", {"track": "v1", "src": str(prod), "in": 0, "out": 6.0, "start": 12.0})
    dispatch(st, "add_text", {"text": "Only $49 today", "start": 13.0, "end": 16.0})
    frames = _turn(st, "remove the silences")
    card = _card(frames)
    assert card is not None, _text(frames)
    lines = _lines(card)
    assert not any(ln.startswith("Deleted") and "a_prod" in ln for ln in lines), lines
    assert any(ln.startswith("Deleted") and "Clip 1" in ln for ln in lines), lines    # the pauses still go
    assert not any("Removed Title" in ln for ln in lines), lines
    _apply(st, frames)
    assert any(Path(c.src) == prod for c in _v1(st)), [c.src for c in _v1(st)]
    assert sum(c.effective_duration for c in _v1(st) if Path(c.src) == prod) == pytest.approx(6.0, abs=0.05)
    assert _title_span(st, "Only $49 today")[1] - _title_span(st, "Only $49 today")[0] == pytest.approx(3.0)


def test_the_tool_keeps_a_silent_clip_when_asked(env):
    src = _quiet_clip(env, name="quiet", dur=4.0)
    st = EDLStore(env / "s2")
    dispatch(st, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": 4.0, "start": 0})
    r = dispatch(st, "remove_silences", {"track": "v1", "keep_silent_clips": True})
    assert r.get("cuts", 0) == 0 and "no sound" in r["summary"], r


# --------------------------------------------------------------------------
# MEDIUM: "except …" and "clip2" scopes
# --------------------------------------------------------------------------

def _changed(before, after) -> set[int]:
    """1-based v1 positions whose speed or effects changed."""
    out = set()
    for i, (a, b) in enumerate(zip(before, after)):
        if a.speed_factor != b.speed_factor or [e.model_dump() for e in a.effects] != [e.model_dump() for e in b.effects]:
            out.add(i + 1)
    return out


@pytest.mark.parametrize("message, want, selected", [
    ("make it grayscale except clip 2", {1, 3}, 2),
    ("all clips except the first to 2x", {2, 3}, None),
    ("black and white on all clips but the second", {1, 3}, None),
    ("clip2 1.5x", {2}, None),
    ("clip1 2x", {1}, None),
    ("clip3 black and white", {3}, None),
])
@pytest.mark.parametrize("confirm", [True, False])
def test_except_and_clipN_scopes(env, message, want, selected, confirm):
    st = _three(env)
    ui = {"selection": _v1(st)[selected - 1].id} if selected else None
    before = [c.model_copy(deep=True) for c in _v1(st)]
    frames = _turn(st, message, confirm=confirm, ui=ui)
    if confirm:
        card = _card(frames)
        if card is None:                      # a question is fine; a wrong edit is not
            assert st.edl.hash() and _changed(before, _v1(st)) == set(), _text(frames)
            return
        _turn(st, "yes", ui=ui)
    got = _changed(before, _v1(st))
    assert got in (want, set()), (message, got, _text(frames))
    if confirm:
        assert got == want, (message, got, _text(frames))


# --------------------------------------------------------------------------
# MEDIUM: new-title text read as typed
# --------------------------------------------------------------------------

def test_a_quoted_title_keeps_its_ampersand_and_case(env):
    st = _three(env)
    frames = _turn(st, "put a text 'Like & Subscribe' at the end for 2 seconds")
    args = _added_text(frames)
    assert args["text"] == "Like & Subscribe", args
    assert (args["start"], args["end"]) == (10.0, 12.0), args


def test_a_title_for_this_asks_what_it_should_say(env):
    st = _three(env)
    before = st.edl.hash()
    frames = _turn(st, "write a catchy title for this")
    assert st.edl.hash() == before and _card(frames) is None
    assert not any(s["tool"] == "add_text" and str(s["args"].get("text", "")).lower() == "for this"
                   for s in _plan(frames)["steps"]), _plan(frames)["steps"]
    assert "?" in _text(frames), _text(frames)


@pytest.mark.parametrize("message", ["add title 'Intro' that fades in", "add a title 'Intro' with a fade in"])
def test_a_new_title_that_fades_in_is_a_title(env, message):
    st = _three(env)
    frames = _turn(st, message)
    tools = [s["tool"] for s in _plan(frames)["steps"]]
    assert tools == ["add_text"], (tools, _text(frames))
    args = _added_text(frames)
    assert args["text"] == "Intro" and args.get("anim_in") == "fade", args


# --------------------------------------------------------------------------
# MEDIUM: a clean-up of a reframe is named as both
# --------------------------------------------------------------------------

def test_a_denoise_of_a_reframe_is_labelled_with_both_steps(env):
    from video_ai_editor.agent.media_origin import record_origin
    from video_ai_editor.media_offline import display_name_for
    from video_ai_editor.agent.prompt import changes as C
    st = _one_clip(env)
    up = Path(_v1(st)[0].src)
    cache = Path(st.dir) / "cache"
    (cache / "denoise").mkdir(parents=True)
    reframe = cache / "reframe_7e9ad28206e222.mp4"
    denoise = cache / "denoise" / "denoise_aff3b4f897cfdd.mp4"
    reframe.write_bytes(up.read_bytes())
    denoise.write_bytes(up.read_bytes())
    record_origin(reframe, up)
    record_origin(denoise, reframe)
    assert display_name_for(Path(st.dir), str(denoise)) == "talk.mp4 (reframed, denoised)"
    assert display_name_for(Path(st.dir), str(reframe)) == "talk.mp4 (reframed)"
    before = st.edl.model_copy(deep=True)
    after = st.edl.model_copy(deep=True)
    after.get_track("v1").clips[0].src = str(denoise)
    lines = [c.text for c in C.summarize(before, after, session_dir=Path(st.dir))]
    assert any("reframed" in ln and "denoised" in ln for ln in lines), lines


# --------------------------------------------------------------------------
# LOW: a new prompt over an open card that ends in a question says the card went
# --------------------------------------------------------------------------

def test_a_question_after_a_dropped_card_says_the_card_was_dropped(env):
    st = _three(env)
    assert _card(_turn(st, "mute clip 3")) is not None
    frames = _turn(st, "make clip 3 50% transparent")      # the net answers this one with a question
    text = _text(frames)
    if not [f for f in frames if f["type"] == "clarify"]:
        pytest.skip(f"the phrase no longer ends in a question: {text[:200]}")
    assert "Dropped the earlier preview" in text, text


def test_a_between_range_and_a_speed_change_in_one_sentence(env):
    st = _three(env)
    frames = _turn(st, "cut everything between 5 and 6 seconds and speed up clip 1")
    card = _card(frames)
    assert card is not None, _text(frames)
    assert "Not done" not in (card.get("note") or "") and "Not done" not in _text(frames), _text(frames)
    done = _apply(st, frames)
    assert "✗" not in _text(done), _text(done)
