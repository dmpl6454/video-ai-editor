"""Hand-built Editor Brain contract fixtures (wave EB1, lane C).

Until lane A's `tests/brain_fixtures.py` lands, the talking-head stand-in is
`prompt_fixtures.speech_clip` (a 12 s lavfi clip with a hand-written
word-level transcript, `prompt_fixtures.WORDS`) and a HAND-BUILT Content
Graph whose ids and times are derived from those words:

    w_0001 so 0.2-0.5 | w_0002 um 0.6-0.9 (filler) | w_0003 hello | w_0004 there | w_0005 friends 2.2-2.8
    w_0006 uh 5.5-5.8 (filler) | w_0007 today 6.0-6.5 | w_0008 we | w_0009 start 7.0-7.6
    w_0010 um 10.5-10.8 (filler) | w_0011 goodbye 11.0-11.6
    sentences s_0001 [0.2, 2.8]  s_0002 [5.5, 7.6] (the hook)  s_0003 [10.5, 11.6]
    silences  sil_0001 [2.8, 5.5]  sil_0002 [7.6, 10.5]

and a HAND-WRITTEN Edit Decision Plan over that graph (fillers + silences
cut, `open_on` the hook sentence, one punch-in on it, captions, a bed, the
dialogue lane). `write_brain_files` lays the graph, its layers and the EDP
under `<session>/brain/` (and the layers under `WORKDIR/analysis/`) exactly
where `brain.store` puts them, so the resolver, the validator and the
versions helper read real files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import prompt_fixtures as F

SRC_KEY = "a" * 24
GID = "g_0123456789ab"
DID = "d_0badf00d"
HOOK = (6.0, 7.6)          # s_0002 "uh today we start" minus its filler head

_WORDS = F.WORDS
_SENT_OF = {1: "s_0001", 2: "s_0001", 3: "s_0001", 4: "s_0001", 5: "s_0001",
            6: "s_0002", 7: "s_0002", 8: "s_0002", 9: "s_0002", 10: "s_0003", 11: "s_0003"}
_FILLERS = {2, 6, 10}


def _feat(**over: Any) -> dict[str, Any]:
    base = dict(wpm=150, fillers=0, has_number=False, strong_number=False, weak_number=False, claim=False,
                conclusion_marker=False, story_marker=False, contrast_words=0, anaphora_start=False, len_words=3)
    return {**base, **over}


def speech_layer() -> dict[str, Any]:
    words = [{"id": f"w_{i:04d}", "t0": s, "t1": e, "text": w, "prob": 1.0, "spk": "S1",
              "sent": _SENT_OF[i], **({"filler": True} if i in _FILLERS else {})}
             for i, (w, s, e) in enumerate(_WORDS, start=1)]
    sentences = [
        {"id": "s_0001", "t0": 0.2, "t1": 2.8, "spk": "S1", "text": "so um hello there friends",
         "kind": "statement", "is_question": False, "answer_of": None, "complete": True, "weak_start": True,
         "topic": "t_001", "features": _feat(fillers=1, len_words=5)},
        {"id": "s_0002", "t0": 5.5, "t1": 7.6, "spk": "S1", "text": "uh today we start",
         "kind": "statement", "is_question": False, "answer_of": None, "complete": True, "weak_start": False,
         "topic": "t_001", "features": _feat(fillers=1, claim=True, len_words=4)},
        {"id": "s_0003", "t0": 10.5, "t1": 11.6, "spk": "S1", "text": "um goodbye",
         "kind": "statement", "is_question": False, "answer_of": None, "complete": True, "weak_start": False,
         "topic": "t_001", "features": _feat(fillers=1, conclusion_marker=True, len_words=2)},
    ]
    return {"params": {"backend": "hand", "model": "none", "language": "en", "prompt": "none", "analysis_version": 1},
            "words": words, "sentences": sentences,
            "turns": [{"id": "u_0001", "spk": "S1", "t0": 0.2, "t1": 11.6, "sents": ["s_0001", "s_0002", "s_0003"]}],
            "acoustic_fillers": [], "words_to_check": [],
            "flags": {"false_starts": [], "repeats": [], "weak_questions": [], "dead_air": [], "technical": []}}


def audio_layer() -> dict[str, Any]:
    import base64
    env = bytes([0x9C] * 1200)         # 12 s at 100 Hz of -100 dBFS int8 (as unsigned bytes)
    return {"hz": 100, "env_10ms": base64.b64encode(env).decode("ascii"),
            "vad": [[0.2, 2.8], [5.5, 7.6], [10.5, 11.6]],
            "silences": [{"id": "sil_0001", "t0": 2.8, "t1": 5.5}, {"id": "sil_0002", "t0": 7.6, "t1": 10.5}],
            "loudness_i": -19.0, "noise_floor_db": -60.0, "clipping": [], "own_mic_energy": {}, "events": []}


def speakers_layer() -> dict[str, Any]:
    return {"engine": "hand", "k": 1, "k_method": "single", "silhouette": None,
            "utterances": [{"t0": 0.2, "t1": 2.8, "spk": "S1"}, {"t0": 5.5, "t1": 7.6, "spk": "S1"},
                           {"t0": 10.5, "t1": 11.6, "spk": "S1"}],
            "turns": [{"id": "u_0001", "spk": "S1", "t0": 0.2, "t1": 11.6}],
            "speakers": [{"id": "S1", "label": "SPEAKER_00", "name": None, "role_guess": "host", "share": 1.0,
                          "questions": 0, "angle_hint": {"angle": "A", "by": "single", "confidence": 1.0}}],
            "overlaps": [], "flip_risk": []}


def semantic_layer() -> dict[str, Any]:
    def sc(hook: float, imp: float, ev: list[str]) -> dict[str, Any]:
        return {"hook": hook, "importance": imp, "standalone": 0.8, "quotable": hook, "emotion": 0.1,
                "virality": hook, "evidence": {"hook": ev}}
    return {"scores": {"s_0001": sc(0.2, 0.3, ["weak_start"]), "s_0002": sc(0.9, 0.8, ["claim"]),
                       "s_0003": sc(0.3, 0.4, ["conclusion_marker"])},
            "annotations": [], "budget": {"calls": 0, "spent_s": 0.0, "partial_from": None}}


def scenes() -> list[dict[str, Any]]:
    return [{"id": "sc_0001", "kind": "speech", "t0": 0.2, "t1": 2.8, "spk": "S1", "topic": "t_001",
             "sents": ["s_0001"], "text": "so um hello there friends"},
            {"id": "sc_0002", "kind": "pause", "t0": 2.8, "t1": 5.5, "spk": None, "topic": None, "sents": [], "text": ""},
            {"id": "sc_0003", "kind": "speech", "t0": 5.5, "t1": 7.6, "spk": "S1", "topic": "t_001",
             "sents": ["s_0002"], "text": "uh today we start"},
            {"id": "sc_0004", "kind": "pause", "t0": 7.6, "t1": 10.5, "spk": None, "topic": None, "sents": [], "text": ""},
            {"id": "sc_0005", "kind": "speech", "t0": 10.5, "t1": 11.6, "spk": "S1", "topic": "t_001",
             "sents": ["s_0003"], "text": "um goodbye"}]


def graph_header(src_path: str, *, gid: str = GID) -> dict[str, Any]:
    return {"version": 1, "id": gid, "analysis_version": 1, "clock": "reference", "reference": SRC_KEY,
            "sources": [{"key": SRC_KEY, "role": "angle", "angle": "A", "leaf": Path(src_path).name,
                         "path": src_path, "duration": 12.0, "sync_offset_s": 0.0, "has_video": True,
                         "has_audio": True, "fps": "30", "dialogue": True,
                         "layers": {"speech": "ok", "speakers": "ok", "audio": "ok", "semantic": "ok"}}],
            "speakers": [{"id": "S1", "label": "SPEAKER_00", "name": None, "role_guess": "host", "share": 1.0,
                          "questions": 0, "angle_hint": "A"}],
            "content_type": {"guess": "talking_head", "confidence": 0.9, "evidence": ["1 speaker"]},
            "layers": {"speech": "speech/hand-v1.json", "speakers": "speakers/hand-v1.json",
                       "audio": "audio/hand-v1.json", "semantic": "semantic/hand-v1.json"},
            "digests": {}, "scenes": "scenes.json",
            "topics": [{"id": "t_001", "t0": 0.2, "t1": 11.6, "title": "the start", "by": "recipes",
                        "sents": ["s_0001", "s_0002", "s_0003"]}],
            "music_hint": {"mood": "chill", "energy": 0.3, "evidence": ["talking_head"]},
            "project": {"canvas": [1920, 1080], "fps": 30, "session_language": "en", "controls_seen": []},
            "timings_s": {}}


def angles(src_path: str) -> dict[str, Any]:
    return {"reference": SRC_KEY, "dialogue": SRC_KEY,
            "members": [{"angle": "A", "src_key": SRC_KEY, "path": src_path, "sync_offset_s": 0.0,
                         "confidence": 1.0, "sees": ["S1"], "by": "single"}]}


def _decision(i: int, kind: str, code: str, facts: list[str], text: str, *, ref: tuple[float, float] | None,
              params: dict[str, Any] | None = None, score: float = 1.0, optional: bool = False) -> dict[str, Any]:
    return {"id": f"k_{i:04d}", "kind": kind,
            "ref": ({"src": SRC_KEY, "t0": ref[0], "t1": ref[1]} if ref else None),
            "params": params or {}, "reason": {"code": code, "facts": facts, "text": text},
            "score": score, "confidence": 0.9, "optional": optional, "by": "recipes", "produced": None}


def edp(src_path: str, *, did: str = DID, gid: str = GID, bed: str | None = None) -> dict[str, Any]:
    decisions = [
        _decision(1, "cut_range", "filler", ["w_0002"], "filler “um” at 00:00:00:18", ref=(0.6, 0.9)),
        _decision(2, "cut_range", "filler", ["w_0006"], "filler “uh” at 00:00:05:15", ref=(5.5, 5.8)),
        _decision(3, "cut_range", "silence", ["sil_0001"], "silence of 2.3 s at 00:00:03:00", ref=(3.0, 5.3)),
        _decision(4, "cut_range", "silence", ["sil_0002"], "silence of 2.5 s at 00:00:07:24", ref=(7.8, 10.3)),
        _decision(5, "open_on", "hook_strongest_opening", ["s_0002"], "strongest opening: claim (0.9)", ref=HOOK,
                  score=0.9),
        _decision(6, "punch_in", "emphasis_peak", ["s_0002", "w_0007"],
                  "speaker delivers a key point (1.6σ louder)", ref=HOOK,
                  params={"scale": 1.10, "anchor": [0.5, 0.42], "interp": "ease-out"}, score=0.8, optional=True),
        _decision(7, "keep_pause", "pause_kept:conclusion", ["sil_0002", "s_0003"],
                  "kept 0.2 s of the pause at 00:00:07:18: after the conclusion", ref=(7.6, 7.8)),
        _decision(8, "captions", "caption_mode", [], "Captions: Dynamic (talking head, energy 5)", ref=None,
                  params={"style": "ig_chunky", "position": "bottom"}),
        _decision(9, "dialogue", "dialogue_lane", [SRC_KEY],
                  f"dialogue from {Path(src_path).name} on lane a1; camera microphones muted; 5 seams faded",
                  ref=None, params={"src": src_path, "lane": "a1", "offsets": {src_path: 0.0}, "seam_fade_s": 0.005}),
    ]
    if bed:
        decisions.append(_decision(10, "music", "music_mood", ["music_hint"],
                                   "chill bed: talking head, arousal 0.3, Music: Subtle", ref=None,
                                   params={"bed": bed, "volume_db": -20.0, "duck_lu": 6.0, "loop": True}))
    return {"version": 1, "id": did, "planner_version": 1, "created": "2026-09-29T20:00:00Z",
            "graph": {"id": gid, "digest": "sha256:" + "0" * 64},
            "controls": {"content_type": "auto", "energy": 5, "captions": "dynamic", "music": "subtle",
                         "duration_s": 45.0, "platform": "instagram_reels", "ratio": "9:16", "count": 1},
            "style": "viral_reel", "seed": 0, "previous": None, "scope": None,
            "brain": "recipes", "content_brain": None,
            "summary": {"project_type": "talking_head", "target": "reel", "duration_s": 6.7,
                        "hook": {"sent": "s_0002", "src": SRC_KEY, "t0": HOOK[0], "t1": HOOK[1], "quote": "today we start"},
                        "story": [{"beat": "hook", "sents": ["s_0002"]}, {"beat": "context", "sents": ["s_0001"]},
                                  {"beat": "payoff", "sents": ["s_0003"]}],
                        "dialogue": {"src": src_path, "lane": "a1", "offsets": {src_path: 0.0}, "seams": 5},
                        "camera": {"angles": 1, "switches": 0, "at_cut": 0}, "pauses_kept": 1,
                        "music": ({"bed": bed, "shape": "bed", "rel_lu": -24.0, "duck_lu": 6.0} if bed else None),
                        "captions": {"mode": "dynamic", "style": "ig_chunky", "position": "bottom"},
                        "estimated_seconds": 4.0,
                        "deferred": [{"asked": "highlight", "why": "per-word caption highlight arrives in EB2"}]},
            "decisions": decisions, "children": [], "compiled": None, "score": None}


def write_brain_files(session_dir: Path, src_path: str, *, workdir: Path, did: str = DID, gid: str = GID,
                      current: bool = True, bed: str | None = None) -> dict[str, Any]:
    """Lay the hand graph + EDP through `brain.store` exactly as D and E will.
    Returns the EDP dict as written."""
    from video_ai_editor.brain import store as BS
    from video_ai_editor.brain.schema import EDP, Graph
    layers = {"speech": speech_layer(), "speakers": speakers_layer(), "audio": audio_layer(),
              "semantic": semantic_layer()}
    for name, data in layers.items():
        BS.write_layer(SRC_KEY, name, "hand-v1", data, workdir=workdir)
    BS.write_source(SRC_KEY, {"src_key": SRC_KEY, "content_key": "c" * 64, "leaf": Path(src_path).name,
                              "duration": 12.0, "fps": 30, "has_audio": True, "has_video": True, "proxy_key": None},
                    workdir=workdir)
    graph = Graph.model_validate(graph_header(src_path, gid=gid))
    BS.write_graph(session_dir, graph)
    BS.write_json(BS.brain_dir(session_dir) / "scenes.json", scenes())
    BS.write_json(BS.brain_dir(session_dir) / "angles.json", angles(src_path))
    if current:
        BS.set_current_graph(session_dir, gid)
    doc = edp(src_path, did=did, gid=gid, bed=bed)
    BS.write_edp(session_dir, EDP.model_validate(doc))
    return doc


# --------------------------------------------------------------------------
# the two-camera podcast on the real P2 media (FX-B: the re-run tests)
# --------------------------------------------------------------------------

def podcast_session(root: Path, p2: Any, *, name: str = "s_pod"):
    """The demo's starting point: cam A and cam B dropped one after the other
    on v1, the recorder on the music lane (the upload handoff), the hand-built
    two-camera graph (`goldens/brain/graphs/two_cam_podcast.json`, ids and
    offsets of the P2 fixture) laid under `<session>/brain/` with the paths of
    the copies in the session's uploads. Returns `(store, session_dir)`."""
    import importlib
    import json
    import shutil

    from video_ai_editor.brain import schema as S
    from video_ai_editor.brain import store as ST
    from video_ai_editor.edl.snapshot import EDLStore
    D = importlib.import_module("video_ai_editor.agent.dispatch")
    sd = root / name
    store = EDLStore(sd)
    paths: dict[str, str] = {}
    for key, src in (("cam_a", p2.cam_a), ("cam_b", p2.cam_b), ("recorder", p2.recorder_wav)):
        d = sd / "uploads" / key
        d.mkdir(parents=True)
        shutil.copy(src, d / Path(src).name)
        paths[Path(src).name] = str((d / Path(src).name).resolve())
    a, b = paths["p2_cam_a.mp4"], paths["p2_cam_b.mp4"]
    da, db = D._source_duration(a), D._source_duration(b)
    D.dispatch(store, "add_clip", {"track": "v1", "src": a, "in": 0, "out": da, "start": 0})
    D.dispatch(store, "add_clip", {"track": "v1", "src": b, "in": 0, "out": db, "start": da})
    D.dispatch(store, "add_music", {"src": paths["p2_recorder.wav"], "start": 0.0})
    g = json.loads((Path(__file__).parent / "goldens" / "brain" / "graphs" / "two_cam_podcast.json").read_text(encoding="utf-8"))
    for s_ in g["graph"]["sources"]:
        s_["path"] = paths[s_["leaf"]]
    for m in g["angles"]["members"]:
        m["path"] = paths[Path(m["path"]).name]
    ST.write_graph(sd, S.Graph.model_validate(g["graph"]))
    ST.write_json(ST.brain_dir(sd) / "angles.json", g["angles"])
    ST.write_json(ST.brain_dir(sd) / "scenes.json", g["scenes"])
    for layer_name, rel in g["graph"]["layers"].items():
        layer, params = rel.split("/")
        ST.write_layer(g["graph"]["reference"], layer, params.removesuffix(".json"), g["layers"][layer_name])
    ST.set_current_graph(sd, g["graph"]["id"])
    return store, sd
