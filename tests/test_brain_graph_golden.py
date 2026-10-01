"""EB1-D: the assembled graphs of both fixtures against their truth and lane
A's hand-built goldens, byte-identical rebuilds, and the analyse() contract
lane F's route relies on."""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "brain" / "graphs"
#: planted-score tolerance of the brief (+ float slack: 0.4 − 0.3 is 0.1000…03)
SCORE_TOL = 0.1 + 1e-9
TIME_TOL = 0.05


@pytest.fixture(scope="module")
def workdir(tmp_path_factory, request):
    from video_ai_editor import config
    wd = tmp_path_factory.mktemp("brain-work")
    old = config.WORKDIR
    config.WORKDIR = wd
    request.addfinalizer(lambda: setattr(config, "WORKDIR", old))
    return wd


@pytest.fixture(scope="module")
def th_graph(workdir, tmp_path_factory):
    from video_ai_editor.brain import graph
    fx = F.th_or_skip()
    sess = tmp_path_factory.mktemp("th-session")
    return fx, sess, graph.analyse(sess, F.th_sources(fx), gateway=None)


@pytest.fixture(scope="module")
def p2_graph(workdir, tmp_path_factory):
    from video_ai_editor.brain import graph
    fx = F.p2_or_skip()
    sess = tmp_path_factory.mktemp("p2-session")
    return fx, sess, graph.analyse(sess, F.p2_sources(fx), gateway=None)


def _golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _golden_is_current(golden: dict, truth) -> bool:
    """A's goldens are generated from the fixture's truth; a golden whose
    sentence times differ from the truth was made for another build."""
    got = [(s["id"], round(s["t0"], 3)) for s in golden["layers"]["speech"]["sentences"]]
    want = [(s.id, round(s.t0, 3)) for s in truth.sentences]
    return len(got) == len(want) and all(a[0] == b[0] and abs(a[1] - b[1]) < 0.01 for a, b in zip(got, want))


def test_th_and_p2_graphs_match_hand_built_within_tolerance(th_graph, p2_graph):
    from video_ai_editor.brain import graph
    fx, sess, gid = th_graph
    th = F.check_th_graph(fx, sess, gid)
    gold = _golden("talking_head.json")
    if not _golden_is_current(gold, fx.th.truth):
        pytest.fail("tests/goldens/brain/graphs/talking_head.json is stale against the fixture's truth: lane A "
                    "regenerates it with `cd tests && ../.venv/bin/python gen_brain_fixture_graphs.py`")
    gspeech, gsem = gold["layers"]["speech"], gold["layers"]["semantic"]
    # ids exact
    assert [s["id"] for s in th["speech"]["sentences"]] == [s["id"] for s in gspeech["sentences"]]
    assert [f["id"] for f in th["speech"]["acoustic_fillers"]] == [f["id"] for f in gspeech["acoustic_fillers"]]
    assert [(r["id"], r["dup"], r["of"]) for r in th["speech"]["flags"]["repeats"]] == \
        [(r["id"], r["dup"], r["of"]) for r in gspeech["flags"]["repeats"]]
    assert [s["kind"] for s in th["scenes"]] == [s["kind"] for s in gold["scenes"]]
    assert [s["id"] for s in th["scenes"]] == sorted(s["id"] for s in th["scenes"])
    # times: the three schwas within 60 ms of the golden's; pause scenes within the VAD's tail slack
    for f, gf in zip(th["speech"]["acoustic_fillers"], gspeech["acoustic_fillers"]):
        assert abs(f["t0"] - gf["t0"]) <= F.ACOUSTIC_TOL_S and abs(f["t1"] - gf["t1"]) <= F.ACOUSTIC_TOL_S
    # planted scores within 0.1: the quotable line, the throwaway and the closing on hook/quotable/standalone,
    # the emphasised sentence's emotion (hand-annotated `claim` on the others is a judgement, see the report)
    t = fx.th.truth
    for sid in (t.quotable, t.throwaway, t.closing):
        for k in ("hook", "quotable", "standalone"):
            assert abs(th["semantic"]["scores"][sid][k] - gsem["scores"][sid][k]) <= SCORE_TOL, (sid, k)
    assert abs(th["semantic"]["scores"][t.emphasis.sent]["emotion"] - gsem["scores"][t.emphasis.sent]["emotion"]) <= SCORE_TOL
    # P2
    fx2, sess2, gid2 = p2_graph
    p2 = F.check_p2_graph(fx2, sess2, gid2)
    gold2 = _golden("two_cam_podcast.json")
    assert [s["id"] for s in p2["graph"]["speakers"]] == [s["id"] for s in gold2["graph"]["speakers"]]
    assert [s["role_guess"] for s in p2["graph"]["speakers"]] == [s["role_guess"] for s in gold2["graph"]["speakers"]]
    assert [(m["angle"], round(m["sync_offset_s"], 3)) for m in json.loads(
        (Path(sess2) / "brain" / "angles.json").read_text())["members"]] == \
        [(m["angle"], round(m["sync_offset_s"], 3)) for m in gold2["angles"]["members"]]
    assert [s["role"] for s in p2["graph"]["sources"]] == [s["role"] for s in gold2["graph"]["sources"]]
    assert p2["graph"]["content_type"]["guess"] == gold2["graph"]["content_type"]["guess"]


def test_p2_planted_quotable_is_importance_top1(p2_graph):
    from video_ai_editor.brain import graph
    fx, sess, gid = p2_graph
    g = graph.load_graph(sess, gid)
    speech, sem = graph.load_layer(g, g["reference"], "speech"), graph.load_layer(g, g["reference"], "semantic")
    t = fx.p2.truth
    q = F.sentence_at(speech["sentences"], t.sentence(t.quotable), slack=0.3)
    assert q is not None
    assert max(sem["scores"], key=lambda k: sem["scores"][k]["importance"]) == q["id"]
    assert sem["scores"][q["id"]]["quotable"] == 1.0
    th = F.sentence_at(speech["sentences"], t.sentence(t.throwaway), slack=0.3)
    assert sem["scores"][q["id"]]["importance"] - sem["scores"][th["id"]]["importance"] >= 0.2


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
            and p.name not in ("timings.jsonl", "refs.json")}


def test_two_builds_identical(th_graph, workdir, tmp_path):
    """A cold rebuild in a fresh WORKDIR and session gives the same gid and the
    same bytes for the header, scenes, angles and every layer file."""
    from video_ai_editor import config
    from video_ai_editor.brain import graph
    fx, sess, gid = th_graph
    old = config.WORKDIR
    config.WORKDIR = tmp_path / "cold-work"
    try:
        other = tmp_path / "session2"
        gid2 = graph.analyse(other, F.th_sources(fx), gateway=None)
        assert gid2 == gid
        assert _tree(Path(sess) / "brain") == _tree(other / "brain")
        key = graph.load_graph(sess, gid)["reference"].removeprefix("src_")
        assert _tree(workdir / "analysis" / key) == _tree(config.WORKDIR / "analysis" / key)
    finally:
        config.WORKDIR = old


def test_layers_carry_no_absolute_paths_and_the_header_carries_one_per_source(th_graph, p2_graph):
    from video_ai_editor.brain import store
    for fx, sess, gid in (th_graph, p2_graph):
        assert "/Users/" not in (store.brain_dir(sess) / "scenes.json").read_text(encoding="utf-8")
        g = json.loads(store.graph_path(sess, gid).read_text(encoding="utf-8"))
        blob = json.dumps({**g, "sources": [{k: v for k, v in s.items() if k != "path"} for s in g["sources"]]})
        assert "/Users/" not in blob and all(os.path.isabs(s["path"]) for s in g["sources"])
        for layer_rel in g["layers"].values():
            assert "/Users/" not in (store.analysis_dir(g["reference"]) / layer_rel).read_text(encoding="utf-8")


def test_analyse_signature_is_the_one_the_route_calls():
    from video_ai_editor.brain import graph
    params = inspect.signature(graph.analyse).parameters
    names = list(params)
    assert names[:2] == ["session_dir", "sources"]
    for kw in ("set_progress", "cancel_event", "layers", "force"):
        assert params[kw].kind is inspect.Parameter.KEYWORD_ONLY, kw
    assert params["set_progress"].default is None and params["cancel_event"].default is None
    assert params["layers"].default is None and params["force"].default is False


def test_every_kwarg_the_route_offers_is_accepted():
    """`api/brain_routes.py` passes only what the signature accepts (inspect)."""
    from video_ai_editor.api import brain_routes
    from video_ai_editor.brain import graph
    offered = {"set_progress": print, "cancel_event": threading.Event(), "layers": ["audio"], "force": True}
    assert brain_routes._accepted_kwargs(graph.analyse, offered) == offered


def test_progress_is_monotone_ends_at_1_and_cancel_stops_it(th_graph, workdir, tmp_path):
    from video_ai_editor.brain import graph
    fx, _sess, _gid = th_graph
    seen: list[float] = []
    graph.analyse(tmp_path / "s1", F.th_sources(fx), set_progress=seen.append, gateway=None)
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) >= 5 and seen[0] < 0.2
    ev = threading.Event()
    ev.set()
    exc = pytest.raises(Exception, graph.analyse, tmp_path / "s2", F.th_sources(fx), cancel_event=ev, gateway=None)
    assert "cancel" in type(exc.value).__name__.lower()
    assert not (tmp_path / "s2" / "brain" / "graph").exists()


def test_layers_and_force_rebuild_only_what_is_asked(th_graph, workdir, tmp_path):
    from video_ai_editor.brain import graph, store
    fx, sess, gid = th_graph
    g = graph.load_graph(sess, gid)
    audio_p = store.analysis_dir(g["reference"]) / g["layers"]["audio"]
    speech_p = store.analysis_dir(g["reference"]) / g["layers"]["speech"]
    stamp = lambda p: p.stat().st_mtime_ns   # noqa: E731
    a0, s0 = stamp(audio_p), stamp(speech_p)
    graph.analyse(tmp_path / "again", F.th_sources(fx), gateway=None)
    assert (stamp(audio_p), stamp(speech_p)) == (a0, s0), "a cached layer was rewritten"
    graph.analyse(tmp_path / "forced", F.th_sources(fx), layers=["audio"], force=True, gateway=None)
    assert stamp(audio_p) > a0 and stamp(speech_p) == s0
    assert graph.analyse(tmp_path / "third", F.th_sources(fx), force=True, gateway=None) == gid


def test_roles_are_inferred_from_bare_paths(p2_graph, tmp_path):
    """The route passes media paths only: cam A on v1, cam B, then the
    recorder — roles, offsets and the reference come from measurement."""
    from video_ai_editor.brain import graph
    fx, _sess, gid = p2_graph
    rec_dir = tmp_path / "recorder"
    rec_dir.mkdir()
    rec = rec_dir / Path(fx.p2.recorder_wav).name
    os.symlink(fx.p2.recorder_wav, rec)
    shutil.copy(F.transcript_path(fx.p2.recorder_wav), rec_dir / "ingest.json")
    sess = tmp_path / "bare"
    gid2 = graph.analyse(sess, [fx.p2.cam_a, Path(fx.p2.cam_b), str(rec)], gateway=None)
    g = graph.load_graph(sess, gid2)
    assert [s["role"] for s in g["sources"]] == ["reference_audio", "angle", "angle"]
    assert [s.get("angle") for s in g["sources"]] == [None, "A", "B"]
    assert [round(s["sync_offset_s"], 2) for s in g["sources"]] == [0.0, 0.35, -0.2]
    assert g["sources"][0]["path"] == str(rec)
    assert gid2 == gid      # the gid names the footage's content, not where the files sit


def test_unrelated_files_are_broll_or_other_and_never_the_reference(th_graph, p2_graph, tmp_path):
    from video_ai_editor.brain import graph
    fx, _s, _g = th_graph
    fx2, _s2, _g2 = p2_graph
    sess = tmp_path / "mixed"
    gid = graph.analyse(sess, [{"path": fx.th.video_16x9, "transcript": str(F.transcript_path(fx.th.video_16x9))},
                               fx2.p2.cam_b, fx2.p2.recorder_wav], gateway=None)
    g = graph.load_graph(sess, gid)
    roles = {s["leaf"]: s["role"] for s in g["sources"]}
    assert roles[Path(fx.th.video_16x9).name] == "angle" and g["reference"] == g["sources"][0]["key"]
    assert roles[Path(fx2.p2.cam_b).name] == "broll" and roles[Path(fx2.p2.recorder_wav).name] == "other"
    assert not any(s["layers"] for s in g["sources"][1:])
    angles = json.loads((Path(sess) / "brain" / "angles.json").read_text())
    assert angles["members"] == []


def test_a_missing_file_is_skipped_and_nothing_at_all_is_an_error(th_graph, tmp_path):
    from video_ai_editor.brain import graph
    fx, _s, _g = th_graph
    gid = graph.analyse(tmp_path / "s", [str(tmp_path / "gone.mp4"), {"path": fx.th.video_16x9,
                        "transcript": str(F.transcript_path(fx.th.video_16x9))}], gateway=None)
    assert len(graph.load_graph(tmp_path / "s", gid)["sources"]) == 1
    with pytest.raises(ValueError):
        graph.analyse(tmp_path / "none", [str(tmp_path / "gone.mp4")], gateway=None)


def test_a_source_without_a_transcript_gets_audio_and_speakers_only(th_graph, tmp_path):
    from video_ai_editor.brain import graph, store
    fx, _s, _g = th_graph
    solo = tmp_path / "solo"
    solo.mkdir()
    os.symlink(fx.th.video_16x9, solo / "th.mp4")
    gid = graph.analyse(tmp_path / "s", [solo / "th.mp4"], gateway=None)
    g = graph.load_graph(tmp_path / "s", gid)
    assert g["sources"][0]["layers"] == {"speech": "missing", "speakers": "ok", "audio": "ok", "semantic": "missing"}
    assert set(g["layers"]) == {"audio", "speakers"} and g["content_type"]["guess"] == "unknown"
    assert graph.load_scenes(tmp_path / "s", gid) == [] and store.current_graph_id(tmp_path / "s") == gid


def _shift_transcript(tr: dict, by: float) -> dict:
    """The recorder's transcript as a file that runs `by` seconds late would carry it."""
    out = json.loads(json.dumps(tr))
    for seg in out["segments"]:
        seg["start"], seg["end"] = round(seg["start"] + by, 3), round(seg["end"] + by, 3)
        for w in seg.get("words") or []:
            w["start"], w["end"] = round(w["start"] + by, 3), round(w["end"] + by, 3)
    return out


def test_recorder_without_a_transcript_reads_v1s_in_reference_seconds(p2_graph, tmp_path):
    """A real session: the recorder is an audio upload (`uploads/audio/`, no
    ingest.json); the transcript of record is the v1 upload's, in cam A's OWN
    seconds. The speech layer must carry it in REFERENCE seconds
    (ref_t = file_t − offset[file]) — the same sentences as the recorder's own."""
    from video_ai_editor.brain import graph
    fx, sess0, gid0 = p2_graph
    offset = fx.p2.truth.offsets["cam_a"]
    cam_dir, rec_dir = tmp_path / "up" / "cam_a", tmp_path / "up" / "audio"
    cam_dir.mkdir(parents=True)
    rec_dir.mkdir(parents=True)
    cam = cam_dir / Path(fx.p2.cam_a).name
    rec = rec_dir / Path(fx.p2.recorder_wav).name
    os.symlink(fx.p2.cam_a, cam)
    os.symlink(fx.p2.recorder_wav, rec)
    tr = _shift_transcript(F.ensure_transcript(fx.p2.recorder_wav), offset)
    (cam_dir / "ingest.json").write_text(json.dumps({"src": cam.name, "transcript": tr}), encoding="utf-8")
    sess = tmp_path / "real"
    gid = graph.analyse(sess, [str(cam), fx.p2.cam_b, str(rec)], gateway=None)
    g, g0 = graph.load_graph(sess, gid), graph.load_graph(sess0, gid0)
    assert g["sources"][0]["role"] == "reference_audio" and g["sources"][0]["layers"]["speech"] == "ok"
    speech, speech0 = graph.load_layer(g, g["reference"], "speech"), graph.load_layer(g0, g0["reference"], "speech")
    assert speech["params"]["transcript_of"] == "v1_angle"
    assert [(s["id"], s["text"], s["spk"]) for s in speech["sentences"]] == \
        [(s["id"], s["text"], s["spk"]) for s in speech0["sentences"]]
    for a, b in zip(speech["sentences"], speech0["sentences"]):
        assert abs(a["t0"] - b["t0"]) <= 0.002 and abs(a["t1"] - b["t1"]) <= 0.002, (a["id"], a["t0"], b["t0"])
    assert len(speech["flags"]["false_starts"]) == 1
    assert g["sources"][0]["layers"]["semantic"] == "ok"


def test_words_shifted_outside_the_reference_are_dropped():
    from video_ai_editor.brain.analysis import transcripts as T
    tr = {"language": "en", "duration": 5.0, "segments": [
        {"id": 0, "start": 0.1, "end": 4.9, "text": "a b c", "words": [
            {"start": 0.1, "end": 0.3, "word": "a", "prob": 1.0}, {"start": 1.0, "end": 1.2, "word": "b", "prob": 0.4},
            {"start": 4.6, "end": 4.9, "word": "c", "prob": 1.0}]}]}
    out = T.to_reference(tr, 0.5, 4.2)
    (seg,) = out["segments"]
    assert [(w["word"], w["start"], w["end"], w["prob"]) for w in seg["words"]] == [("b", 0.5, 0.7, 0.4), ("c", 4.1, 4.2, 1.0)]
    assert (seg["start"], seg["end"], seg["text"]) == (0.5, 4.2, "b c")
    assert tr["segments"][0]["words"][0]["start"] == 0.1, "the input is never mutated"
    assert T.to_reference(tr, 0.0, 5.0) == tr
