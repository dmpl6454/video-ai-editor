"""Prompt-bar readings — final sweep 3 (prompt-assistant + editor-ux findings).

Every phrase runs through the real key-free service (`prompt_turn(brain=
"recipes")`) on the capcut-sweep session: three 4 s clips A / B / C on the
main track, a 16:9 canvas, a 12 s music bed at -14 dB.

  * a picture-in-picture that FOLLOWS the main lane (P3 layer-follow) is not
    an unasked change: cuts, deletes, speed, freezes and reorders run on a
    project with an overlay (they were rolled back — "it changed the overlay
    clip, which the request did not ask for");
  * a sticker called "the logo" or "the gif" is the sticker;
  * words INSIDE a quoted title are the title, not an edit ("add a title
    'Chop the onions'" asked "Which part should I cut?");
  * two titles in one prompt are two titles;
  * "every clip" means every clip for speed and fades;
  * captions come from every clip that speaks, not only the first;
  * a clause the fallback could not read is named as not done.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401
from test_prompt_capcut_sweep import _question_text, _session, _turn, media  # noqa: E402,F401

from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl.schema import Clip, Sticker, TextClip  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_downloads")
UI = {"playhead": 5.5}


def _v1(e) -> list[Clip]:
    return sorted((c for c in e.get_track("v1").clips if isinstance(c, Clip)), key=lambda c: c.start)


def _texts(e) -> list[TextClip]:
    return [c for t in e.tracks if t.type == "text" for c in t.clips if isinstance(c, TextClip)]


def _reply(events: list[dict]) -> str:
    return "".join(x.get("text", "") for x in events if x["type"] == "text_delta")


def _rolled_back(events: list[dict]) -> bool:
    return any(x["type"] == "step" and x.get("tool") == "safety_net" and x.get("status") == "failed"
               for x in events)


# ------------------------------------------------------------------ overlays follow the main lane


def _overlay(st, at: float = 6.0) -> str:
    src = st.edl.get_track("v1").clips[0].src
    dispatch(st, "add_clip", {"track": "v2", "src": src, "in": 0, "out": 2.0, "start": at})
    return st.edl.get_track("v2").clips[0].id


@pytest.mark.parametrize("phrase", [
    "cut the first 2 seconds", "delete the first clip", "remove the ums and dead air", "delete clip 1",
    "speed up clip 1 2x", "swap clip 1 and 2", "freeze the frame at 2s for 1 second",
])
def test_a_main_lane_edit_runs_on_a_project_with_an_overlay(media, phrase, request):
    """HIGH (editor-ux) + LOW (prompt-assistant): the key-free bar could not
    cut, delete, speed or reorder any project with a PIP or B-roll overlay."""
    st, ids = _session(media, f"s_ov{abs(hash(phrase)) % 10**8}")
    ov = _overlay(st)
    before = st.edl.hash()
    b_ov = st.edl.get_clip(ov)[1].model_copy(deep=True)
    events = _turn(st, phrase, UI)
    assert not _rolled_back(events), _reply(events)
    assert st.edl.hash() != before, _reply(events)
    found = st.edl.get_clip(ov)
    assert found, "the overlay is gone"
    a_ov = found[1]
    # it only moved in time with its picture: its look is untouched
    assert a_ov.transform == b_ov.transform and a_ov.blend == b_ov.blend and a_ov.effects == b_ov.effects


def test_an_overlay_change_nobody_asked_for_is_still_refused():
    """The licence is for TIMING that follows the main lane, nothing else."""
    from video_ai_editor.agent.prompt import contract_diff as CD
    from video_ai_editor.edl.schema import EDL
    from video_ai_editor.edl.schema import Track
    e = EDL(tracks=[Track(id="v1", type="video", clips=[Clip(src="/a.mp4", in_=0.0, out=4.0, start=0.0)]),
                    Track(id="v2", type="video", clips=[Clip(src="/b.mp4", in_=0.0, out=2.0, start=2.0)])])
    moved = e.model_copy(deep=True)
    moved.get_track("v2").clips[0].start = 1.0
    assert "overlay:time" in CD.diff(e, moved).categories
    assert "overlay:change" not in CD.diff(e, moved).categories
    zoomed = e.model_copy(deep=True)
    zoomed.get_track("v2").clips[0].start = 1.0
    zoomed.get_track("v2").clips[0].transform.scale = 2.0
    assert "overlay:change" in CD.diff(e, zoomed).categories


# ------------------------------------------------------------------ "the logo", "the gif"


@pytest.fixture(scope="module")
def logo_png(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("logo") / "logo.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1",
                    str(p)], check=True, capture_output=True)
    return p


@pytest.mark.parametrize("phrase", ["add a pop in animation to the logo", "add a zoom in animation to the gif",
                                    "add a pop in animation to the sticker"])
def test_a_logo_or_gif_is_the_sticker(media, logo_png, phrase, request):
    """MEDIUM: the planner read 'the logo' as the sticker; the contract did
    not, and rolled the animation back."""
    st, ids = _session(media, f"s_lg{abs(hash(phrase)) % 10**8}")
    dispatch(st, "add_sticker", {"src": str(logo_png), "start": 1.0, "end": 5.0})
    sid = next(c.id for t in st.edl.tracks for c in t.clips if isinstance(c, Sticker))
    events = _turn(st, phrase, UI)
    assert not _rolled_back(events), _reply(events)
    s = st.edl.get_clip(sid)[1]
    assert s.anim_in, (phrase, _reply(events))


# ------------------------------------------------------------------ quoted titles


@pytest.mark.parametrize("words", ["Chop the onions", "Cut the bread", "Remove the seeds", "Drop the pasta",
                                   "Trim the fat", "Step 1: Chop the veg"])
def test_words_inside_a_quoted_title_are_the_title(media, words, request):
    st, ids = _session(media, f"s_qt{abs(hash(words)) % 10**6}")
    v1_before = [(c.in_, c.out, c.start) for c in _v1(st.edl)]
    events = _turn(st, f"add a title '{words}' at the start for 3 seconds", UI)
    got = [t.text for t in _texts(st.edl)]
    assert words in got, (got, _question_text(events))
    assert [(c.in_, c.out, c.start) for c in _v1(st.edl)] == v1_before


def test_grammar_scores_the_clause_without_the_quoted_words():
    from video_ai_editor.agent.prompt import grammar as G
    for words in ("Chop the onions", "Cut the bread", "Remove the seeds", "Drop the pasta", "Trim the fat"):
        d = G.detect(f"add a title '{words}' at 0:00")
        assert [h.intent for h in d.hits] == ["title"], (words, d.hits)
    # an apostrophe is not a quote
    assert "mute" in [h.intent for h in G.detect("don't cut clip 2, mute it").hits]
    assert G.detect("cut the first 2 seconds, it's too slow").hits[0].intent == "trim"


# ------------------------------------------------------------------ two titles


def test_two_titles_in_one_prompt_are_two_titles(media, request):
    st, ids = _session(media, "s_2titles")
    events = _turn(st, "add a title 'Step 2: Pour the sauce' at 2s for 3 seconds and a title "
                       "'Step 3: Plate it' at 6s for 3 seconds", UI)
    got = sorted((t.text, round(t.start, 2), round(t.end, 2)) for t in _texts(st.edl))
    assert got == [("Step 2: Pour the sauce", 2.0, 5.0), ("Step 3: Plate it", 6.0, 9.0)], (got, _reply(events))


def test_two_titles_with_two_verbs_are_two_titles(media):
    st, ids = _session(media, "s_2titles_b")
    events = _turn(st, "add a title 'Hello' at 2s and add a title 'World' at 6s", UI)
    got = sorted(t.text for t in _texts(st.edl))
    assert got == ["Hello", "World"], (got, _reply(events))


# ------------------------------------------------------------------ every clip


@pytest.mark.parametrize("phrase,check", [
    ("make every clip 1.5x", lambda cs: all(c.speed == 1.5 for c in cs)),
    ("make evry clip 1.2x", lambda cs: all(c.speed == 1.2 for c in cs)),
    ("fade out every clip", lambda cs: all((c.video_fade_out or 0) > 0 for c in cs)),
])
def test_every_clip_means_every_clip(media, phrase, check, request):
    st, ids = _session(media, f"s_ev{abs(hash(phrase)) % 10**8}")
    events = _turn(st, phrase, UI)
    cs = _v1(st.edl)
    assert len(cs) == 3 and check(cs), ([(c.speed, c.video_fade_out) for c in cs], _reply(events))


# ------------------------------------------------------------------ not done


def test_a_clause_the_fallback_could_not_read_is_named(media):
    """MEDIUM: 'set the overlay to screen blend and make it full screen'
    planned only the blend and said nothing about the rest."""
    st, ids = _session(media, "s_notdone")
    _overlay(st, at=2.0)
    events = _turn(st, "set the overlay to screen blend and make it full screen", UI)
    assert "make it full screen" in _reply(events), _reply(events)


def test_the_preview_card_names_the_clause_it_did_not_do(media, monkeypatch):
    monkeypatch.setenv("VAI_PROMPT_CONFIRM", "1")
    st, ids = _session(media, "s_notdone_pv")
    _overlay(st, at=2.0)
    events = _turn(st, "set the overlay to screen blend and make it full screen", UI)
    card = next(x for x in events if x["type"] == "clarify" and x.get("preview"))
    assert "make it full screen" in (card["preview"]["note"] or ""), card["preview"]
    from video_ai_editor.agent.prompt import pending
    pending.clear_pending(Path(st.dir))


# ------------------------------------------------------------------ captions from every clip that speaks


def test_captions_come_from_every_clip_that_speaks(media, monkeypatch, tmp_path):
    """HIGH: v1 = a tone-only product shot, then a talking clip. 'add captions'
    transcribed only clip 1 and laid 0 captions; auto_caption gave 8."""
    import importlib
    D = importlib.import_module("video_ai_editor.agent.dispatch")
    from video_ai_editor.ingest import transcribe as T
    st, ids = _session(media, "s_caps2")
    tone = tmp_path / "prod" / "prod.mp4"
    tone.parent.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=4:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(tone)], check=True, capture_output=True)
    F.write_ingest(tone, {"language": "en", "duration": 4.0, "segments": []})
    dispatch(st, "add_clip", {"track": "v1", "src": str(tone), "in": 0, "out": 4.0, "start": 0.0})
    v1 = _v1(st.edl)
    assert Path(v1[0].src) == tone, [c.src for c in v1]
    talk = str(media["src"])

    def fake_transcribe(src, **kw):
        from video_ai_editor.ingest.transcribe import Transcript
        body = F.transcript() if str(src) == talk else {"language": "en", "duration": 4.0, "segments": []}
        return Transcript.model_validate(body)

    monkeypatch.setattr(T, "transcribe", fake_transcribe)
    monkeypatch.setattr(D, "whisper_model_on_disk", lambda m: True)
    from video_ai_editor.agent.prompt import facts as FA
    monkeypatch.setattr(FA.TimelineFacts, "is_cached", lambda self, key: True)
    events = _turn(st, "add captions", UI)
    cues = st.edl.get_track("captions").clips if st.edl.get_track("captions") else []
    assert cues, _reply(events)
    assert all(c.start >= 4.0 - 1e-6 for c in cues), [(c.start, c.text) for c in cues]


# ------------------------------------------------------------------ amounts, targets, scope (MEDIUM)


def _music(e):
    return [c for c in e.get_track("music").clips]


@pytest.mark.parametrize("phrase,check", [
    # "like 3db" after a comma was a clause of its own: the music went up 6 dB
    ("make the background music louder, like 3db", lambda e: abs(_music(e)[0].audio.gain_db - (-11.0)) < 0.05),
    # only the first time was split
    ("split at 2 and 10 seconds", lambda e: {round(c.start, 2) for c in _v1(e)} >= {0.0, 2.0, 10.0}),
    # "brighter" graded every clip
    ("make clip 1 black and white and brighter",
     lambda e: not _v1(e)[1].effects and not _v1(e)[2].effects and len(_v1(e)[0].effects) == 2),
    # cut the pauses instead of muting
    ("silence the original audio but keep the music",
     lambda e: all(c.audio.mute for c in _v1(e)) and len(_v1(e)) == 3 and not _music(e)[0].audio.mute),
    # 1.25x gave 9.6 s
    ("speed up the whole thing so it's 9 seconds",
     lambda e: abs(sum(c.effective_duration for c in _v1(e)) - 9.0) < 0.05),
])
def test_amounts_targets_and_scope_are_read(media, phrase, check):
    st, ids = _session(media, f"s_am{abs(hash(phrase)) % 10**8}")
    events = _turn(st, phrase, UI)
    assert not _rolled_back(events), _reply(events)
    assert check(st.edl), ([(c.start, c.speed, [x.type for x in c.effects], c.audio.mute) for c in _v1(st.edl)],
                           [(c.audio.gain_db, c.audio.mute) for c in _music(st.edl)], _reply(events))


@pytest.mark.parametrize("phrase,start,words", [
    ("add a title here saying Look!", 5.5, "Look"),           # 'here' went to 0:00
    ("add a title saying here we go", 0.0, "here we go"),     # the words are not a place
])
def test_here_means_the_playhead(media, phrase, start, words):
    st, ids = _session(media, f"s_here{abs(hash(phrase)) % 10**8}")
    events = _turn(st, phrase, UI)
    got = [(t.text, round(t.start, 2)) for t in _texts(st.edl)]
    assert (words, start) in got, (got, _reply(events))
