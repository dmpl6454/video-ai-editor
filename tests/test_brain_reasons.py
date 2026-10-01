"""Plan-tab reasons read like an editor's reasons (review UX-05).

Every decision of both demo edits (the talking-head reel, the two-camera
premium podcast) is rendered and held to the rules a viewer of the card
cares about: a timecode and a name where the decision is about a moment or a
person; never a graph key, a file name, a fallback topic, a σ, an arousal, a
raw 0.xx confidence or a bare number. The closed code vocabulary, the frozen
`{code, facts, text}` shape and the graph facts (C's `check_facts`) stay as
they were.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from gen_brain_goldens import p2_graph, th_graph  # noqa: E402

from video_ai_editor.brain import reasons as R  # noqa: E402
from video_ai_editor.brain.planner import plan  # noqa: E402
from video_ai_editor.brain.planner.graph_view import Graph  # noqa: E402

REEL = {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"}
PODCAST = {"content_type": "podcast"}
TC = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
GRAPH_KEY = re.compile(r"\b(?:src_|s_\d|u_\d|w_\d|sil_\d|af_\d|d_\d|f_\d|r_\d|k_\d|sc_\d|t_\d)")
FILE_NAME = re.compile(r"\.(?:mp4|mov|wav|m4a|mkv)\b|normalized|th_16x9|p2_cam|recorder\.wav", re.I)
#: A number must say what it counts or measures: a count is followed by its noun
#: ("21 cuts"), a measure by its unit ("1.5 s", "5 ms"); a decimal with neither is telemetry.
NUMBER = re.compile(r"(?<![\w:.])(?:\d+\.\d+(?!\d|\s?(?:s|ms|%)\b)|\d+(?![\d.:]|\s?[A-Za-z%]))")


def _graphs():
    out = [("th", th_graph(), REEL), ("p2", p2_graph(), PODCAST)]
    a = Path(__file__).parent / "goldens" / "brain" / "graphs"
    for name, controls in (("talking_head", REEL), ("two_cam_podcast", PODCAST)):
        p = a / f"{name}.json"
        if p.exists():
            import json
            out.append((f"A-{name}", json.loads(p.read_text(encoding="utf-8")), controls))
    return out


@pytest.fixture(params=_graphs(), ids=lambda p: p[0])
def edp_and_graph(request):
    _, graph, controls = request.param
    edp = plan(graph, controls)
    yield edp, Graph(graph)
    R.bind(None)


def _bare(text: str) -> str:
    """The reason without its quotations (a quote is the speaker's own words)."""
    return re.sub(r"“[^”]*”", "“”", text)


def test_no_reason_reads_like_telemetry(edp_and_graph):
    edp, _ = edp_and_graph
    assert edp["decisions"]
    for d in edp["decisions"]:
        text = d["reason"]["text"]
        where = f"{d['id']} {d['reason']['code']}: {text!r}"
        assert text and text == text.strip() and "  " not in text and len(text) <= 200, where
        assert text[0].isupper(), where
        bare = _bare(text)
        assert not GRAPH_KEY.search(bare), where
        assert not FILE_NAME.search(text), where
        assert not re.search(r"σ|sigma|arousal|\benergy \d|\bconfidence\b|\bscore\b|probab", bare, re.I), where
        assert not re.search(r"\b0\.\d\d\b", text), where
        assert not NUMBER.search(TC.sub("", bare)), f"a bare number in {where}"
        assert not re.search(r"topic “|the footage|None|\{|\}", text), where


def test_every_decision_about_a_moment_carries_a_timecode(edp_and_graph):
    edp, _ = edp_and_graph
    timed = {"cut_range", "keep_pause", "switch_angle", "punch_in", "jump_cut_hide", "open_on", "keep_window"}
    for d in edp["decisions"]:
        if d["kind"] in timed:
            assert TC.search(d["reason"]["text"]), f"{d['id']} {d['kind']}: {d['reason']['text']!r}"


def test_every_camera_reason_has_a_timecode_and_a_speaker_name(edp_and_graph):
    edp, g = edp_and_graph
    names = {R._who(R._speaker_name(s)).lower() for s in g.speakers}
    switches = [d for d in edp["decisions"] if d["kind"] == "switch_angle"]
    for d in switches:
        text = d["reason"]["text"]
        assert TC.search(text), text
        assert any(n in text.lower() for n in names), (names, text)
        assert d["reason"]["code"] in ("speaker_turn", "at_cut")
    if len(g.members) > 1:
        assert switches, "the podcast has camera changes to read"


def test_the_vocabulary_shape_and_facts_are_unchanged(edp_and_graph):
    from video_ai_editor.brain import schema as S
    edp, _ = edp_and_graph
    for d in edp["decisions"]:
        assert set(d["reason"]) == {"code", "facts", "text"}
        assert d["reason"]["code"] in S.REASON_CODES or re.fullmatch(r"pause_kept:\w+", d["reason"]["code"])
        assert R.base_code(d["reason"]["code"]) in R.TEMPLATES
    assert set(R.WAVE_CODES) >= {d["reason"]["code"] for d in edp["decisions"]}


def test_facts_still_resolve_in_the_graph():
    from video_ai_editor.brain import schema as S, store as ST
    graph = p2_graph()
    edp = plan(graph, PODCAST)
    ids = S.graph_ids(S.Graph.model_validate(graph["graph"]),
                      {k: ST.LAYER_MODELS[k].model_validate(v) for k, v in graph["layers"].items() if k in ST.LAYER_MODELS},
                      scenes=S.Scenes.model_validate(graph["scenes"]), angles=S.Angles.model_validate(graph["angles"]))
    S.check_facts(S.EDP.model_validate(edp), ids)


# ---------------------------------------------------------------- the wording itself

def test_a_timecode_is_how_an_editor_says_it():
    assert R.tc(82.4) == "1:22" and R.tc(24.9) == "0:24" and R.tc(0) == "0:00" and R.tc(3723) == "1:02:03"
    assert R.dur(1.5) == "1.5 s" and R.dur(12.3) == "12 s"


def test_the_camera_reads_like_an_editor():
    g = Graph(p2_graph())
    R.bind(g)
    try:
        turn = next(t for t in g.turns if t["spk"] == "S2" and t["t0"] > 60)
        text = R.reason("speaker_turn", [str(turn["id"]), "S2"], speaker="Guest")["text"]
        assert text == f"Cut to the guest, who starts speaking at {R.tc(turn['t0'])}"
        cut = R.reason("at_cut", [str(turn["id"]), "S2", "sil_0026"], tc="1:52")["text"]
        assert cut.startswith("Cut to the guest at 1:52") and "jump cut" in cut
        host = R.reason("speaker_turn", [str(turn["id"]), "S1"], speaker="Host", tc="0:05")["text"]
        assert host == "Cut to the host, who starts speaking at 0:05"
    finally:
        R.bind(None)


def test_cuts_and_pauses_read_like_an_editor():
    R.bind(None)
    assert R.reason("filler", ["w_1"], word="umm", tc="0:24")["text"] == "Removed an “umm” at 0:24"
    assert R.reason("filler", ["w_1"], word="like", tc="0:24")["text"] == "Removed a “like” at 0:24"
    assert R.reason("silence", ["sil_1"], dur="1.5 s", tc="0:28")["text"] == "Cut a 1.5 s silence at 0:28"
    kept = R.reason("pause_kept:emotion", ["s_1"], kept="0.7 s", tc="1:00")["text"]
    assert kept.startswith("Kept 0.7 s of the pause after an emotional line at 1:00") and "lets the line land" in kept
    assert R.reason("dead_air", ["d_1"], dur="2.8 s", speaker="Guest", tc="1:25")["text"] == "Cut 2.8 s of dead air in the guest's answer at 1:25"
    rep = R.reason("repeat", ["r_1"], tc="0:15", tc_of="0:20")["text"]
    assert "earlier take" in rep and "retake at 0:20 stays" in rep


def test_reframe_and_export_say_what_was_done():
    R.bind(None)
    reframe = R.reason("control", [], control="Reframe", value="9:16", platform="reels")["text"]
    assert reframe.startswith("Cropped to vertical 9:16 for Instagram Reels")
    export = R.reason("control", [], control="Platform", value="reels")["text"]
    assert export.startswith("Exported with the Instagram Reels preset")
    assert R.reason("control", [], control="Length", value="45 s")["text"] == "Length: 45 s"


def test_a_source_is_named_by_its_job_never_by_its_file():
    g = Graph(p2_graph())
    R.bind(g)
    try:
        rec = next(s["key"] for s in g.sources if s.get("role") == "reference_audio")
        cam = next(s["key"] for s in g.sources if s.get("role") == "angle")
        recorder = R.reason("dialogue_lane", [rec], leaf="p2_recorder.normalized.mp4", n=21)["text"]
        camera = R.reason("dialogue_lane", [cam], leaf="p2_cam_a.normalized.mp4", n=1)["text"]
        assert "recorder" in recorder and "21 cuts get a 5 ms fade" in recorder
        assert "picture's own sound" in camera and "1 cut gets a 5 ms fade" in camera
        assert not FILE_NAME.search(recorder + camera)
    finally:
        R.bind(None)


def test_captions_and_music_name_what_the_viewer_gets():
    R.bind(None)
    cap = R.reason("caption_mode", [], mode="dynamic", content_type="talking head", energy=5)["text"]
    assert cap.startswith("Added Dynamic captions") and "energy" not in cap and "5" not in cap
    music = R.reason("music_mood", [], mood="upbeat", arousal="0.23", control="Music: Subtle")["text"]
    assert music == "Added a quiet upbeat music bed that ducks under speech"


def test_every_template_renders_without_fields_or_graph():
    R.bind(None)
    for code in R.TEMPLATES:
        text = R.reason(code, [])["text"]
        assert text and "{" not in text and "None" not in text, (code, text)
    with pytest.raises(KeyError):
        R.reason("not_a_code", [])


# ------------------------------------------------ finalize: what the decision's own params say (FX-C1 requests)

def test_a_cut_that_spared_speech_says_so():
    for code, base in (("silence", {"dur": "1.5 s"}), ("dead_air", {"dur": "3 s", "speaker": "S1"})):
        plain = R.render(code, **base)
        spared = R.render(code, spared="2", **base)
        assert "the transcript missed" not in plain, plain
        assert spared.endswith("; left the speech the transcript missed in place"), spared


def test_every_hide_reason_states_the_step_its_own_params_hold(edp_and_graph):
    edp, _ = edp_and_graph
    hides = [d for d in edp["decisions"] if d["kind"] == "jump_cut_hide"]
    for d in hides:
        p = d["params"]
        text = d["reason"]["text"]
        assert f"from {p['from_scale']:.0%} to {p['scale']:.0%}" in text, (text, p["from_scale"], p["scale"])
        assert p["step"] >= 0.06 - 1e-9, "the reason would claim a step the picture does not show"
        assert "Nudged" not in text


def test_a_hide_without_scales_keeps_the_older_wording():
    assert R.render("jump_cut_hide", tc="0:24") == "Nudged the zoom at the cut at 0:24 so the jump does not show"
    assert R.render("jump_cut_hide", tc="0:24", from_scale=1.0, scale=1.08) == \
        "Stepped the zoom from 100% to 108% at 0:24 so the jump does not show"
