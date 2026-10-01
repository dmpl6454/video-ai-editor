"""Hand-built golden Content Graphs for the two brain fixtures (EB1-A).

    cd tests && ../.venv/bin/python gen_brain_fixture_graphs.py

writes `tests/goldens/brain/graphs/talking_head.json` and `two_cam_podcast.json`
from the fixtures' TRUTH (`brain_fixtures.build_brain_fixtures()`), in the
frozen shapes of EB1_BRIEF.md "Graph" / spec §3.1-3.3, so lane E can run
`plan()` before lane D's analysers exist and lane D can compare its graphs
against a known answer (ids exact, times ± 0.05 s, planted scores ± 0.1).

What is truth and what is formula, per layer:

  speech    words (approximate timing — neither voice exposes alignments;
            `prob` 1.0 as the unprompted transcript carries today), sentences
            with `features` from the script annotations plus measured `wpm`,
            turns (utterances merged per speaker across gaps < 0.6 s),
            `acoustic_fillers` (the three islands), `flags` (the retake as a
            repeat, the P2 false start, in-turn dead air ≥ 1.2 s podcast /
            0.6 s reel), `words_to_check` (digit runs).
  audio     `env_10ms` MEASURED from the reference file (int8 dBFS, base64),
            `vad` = the voiced spans, `silences` = gaps ≥ 0.4 s, `loudness_i`
            by ffmpeg ebur128, `own_mic_energy` per angle = RMS dB per truth
            turn measured in that camera file.
  speakers  from the truth turn table (engine "truth").
  semantic  every score by the §3.4 formulas over the truth features, with two
            measured inputs — `rms_z` (per-sentence RMS z-scored within the
            speaker; the formulas use `rms_z⁺ / 2` clipped to 0..1) and
            `stretch` (speaker median wpm / sentence wpm) — pitch is not
            measured (`pitch_range_norm` = 0.3 flat), humour 0, quality 0.5
            (no visual layer), `topic_peak` = the emphasised sentence (TH) /
            the quotable line (P2). `evidence` lists the non-zero terms.
  scenes    §3.3 steps 1-2 over the sentences (merge forward while the same
            speaker continues and ≤ 12 s; split at pauses ≥ 1.2 s; pause
            scenes for gaps ≥ 1.5 s), scores = the max over the scene's
            sentences.
  angles    one member per camera with the planted offsets, `by: "truth"`.

Source keys are CONTENT keys (`src_` + sha256(file bytes)[:24]) — stable on
any machine — never the identity key. All times are 4-decimal seconds.
`tests/test_brain_fixtures.py::test_golden_graphs_match_truth` regenerates in
memory and pins byte-equality with the checked-in files.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark.media import integrated_lufs  # noqa: E402
from brain_fixture_truth import P2Truth, THTruth, env_10ms_db, rms_db  # noqa: E402
from timing_fixtures import audio_samples  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO / "tests" / "goldens" / "brain" / "graphs"
ANALYSIS_VERSION = 1
STOP = set("a an the of to in on at for and or but so is are was were did do does how what why who which "
           "this that it we you i our your they he she them us me my be been not no".split())
CONTRAST = ("never", "nobody", "secret", "mistake", "wrong", "best", "worst", "only", "stop")


def r4(x: float) -> float:
    return round(float(x), 4)


def content_key(path: str) -> str:
    return "src_" + hashlib.sha256(Path(path).read_bytes()).hexdigest()[:24]


def digest(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stem(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())[:4]


# --------------------------------------------------------------------------
# measured inputs
# --------------------------------------------------------------------------

def _pcm(path: str) -> tuple[np.ndarray, int]:
    return np.asarray(audio_samples(Path(path), rate=48000), dtype=np.float32), 48000


def _n_words(s) -> int:
    return int(s.features.get("len_words") or len(s.text.split()))


def measured(truth, ref_path: str) -> dict:
    """rms_z, stretch and wpm per sentence (within speaker), plus the PCM."""
    pcm, sr = _pcm(ref_path)
    out: dict[str, dict] = {}
    for spk in sorted({s.speaker for s in truth.sentences}):
        sents = [s for s in truth.sentences if s.speaker == spk and s.kind != "backchannel"]
        db = np.array([rms_db(pcm[int(s.t0 * sr):int(s.t1 * sr)]) for s in sents])
        wpm = np.array([60.0 * _n_words(s) / (s.t1 - s.t0) for s in sents])
        z = (db - db.mean()) / (db.std() or 1.0)
        med = float(np.median(wpm))
        for s, zi, wi in zip(sents, z, wpm):
            out[s.id] = {"rms_z": r4(zi), "wpm": int(round(wi)), "stretch": r4(med / wi)}
    for s in truth.sentences:
        out.setdefault(s.id, {"rms_z": 0.0, "wpm": int(round(60.0 * _n_words(s) / (s.t1 - s.t0))), "stretch": 1.0})
    return {"per_sent": out, "pcm": pcm, "sr": sr}


# --------------------------------------------------------------------------
# §3.4 scores
# --------------------------------------------------------------------------

def _standalone(s, sents_by_id) -> float:
    v = 1.0 - (1.0 if s.features.get("anaphora_start") else 0.0) - 0.5 * (1.0 if s.features.get("conjunction_start") else 0.0)
    if s.answer_of:
        q = sents_by_id[s.answer_of]
        nouns = {_stem(w) for w in q.text.split() if w.lower().strip("?.,") not in STOP and len(w) >= 4}
        head = {_stem(w) for w in s.text.split()[:3]}
        if nouns and not nouns & head:
            v -= 0.3
    return r4(min(1.0, max(0.0, v)))


def _quotable(s) -> float:
    f = s.features
    v = 1.0 if (f.get("claim") or f.get("strong_number")) else 0.3
    if _n_words(s) > 18:
        v *= 0.5
    if f.get("anaphora_start"):
        v *= 0.5
    return r4(v)


def score_sentence(s, ctx: dict) -> tuple[dict, dict]:
    f = s.features
    m = ctx["measured"][s.id]
    ev: dict[str, list[str]] = {"hook": [], "importance": [], "emotion": []}
    rms_z_norm = min(1.0, max(0.0, m["rms_z"] / 2.0))
    stretch_dev = min(1.0, max(0.0, (m["stretch"] - 1.0) / 0.5))
    emotion = 0.5 * rms_z_norm + 0.3 * 0.3 + 0.2 * stretch_dev
    ev["emotion"] += [k for k, v in (("rms_z", rms_z_norm), ("stretch", stretch_dev)) if v > 0]
    delivery = max(emotion, rms_z_norm)
    standalone = _standalone(s, ctx["sents_by_id"])
    quotable = _quotable(s)
    contrast = min(1, int(f.get("contrast_words", 0)))
    candidate = bool(f.get("claim") or contrast or s.is_question or f.get("imperative"))
    hook = 0.0
    if candidate:
        terms = [("is_question", 0.25 * s.is_question), ("claim", 0.20 * bool(f.get("claim") or f.get("conclusion_marker"))),
                 ("strong_number", 0.15 * bool(f.get("strong_number"))), ("delivery", 0.15 * delivery),
                 ("contrast_words", 0.10 * contrast), ("standalone", 0.10 * standalone),
                 ("weak_start", -0.30 * bool(f.get("weak_start"))), ("anaphora_start", -0.30 * bool(f.get("anaphora_start"))),
                 ("len>20", -0.20 * (_n_words(s) > 20))]
        hook = sum(v for _, v in terms)
        ev["hook"] = [k for k, v in terms if v]
        if delivery < 0.3 and not contrast:
            hook *= 0.7
            ev["hook"].append("flat_delivery")
        hook = min(1.0, max(0.0, hook))
    answer_len = min(1.0, _n_words(s) / 20.0) if s.answer_of else 0.0
    terms = [("answer", 0.30 * answer_len), ("claim", 0.22 * bool(f.get("claim"))), ("strong_number", 0.08 * bool(f.get("strong_number"))),
             ("topic_peak", 0.15 * (s.id == ctx["topic_peak"])), ("conclusion", 0.10 * bool(f.get("conclusion_marker"))),
             ("rms_z", 0.15 * rms_z_norm), ("repeat", -0.25 * (s.kind == "retake_dup")),
             ("fillers", -0.20 * (ctx["fillers"].get(s.id, 0) / _n_words(s))), ("false_start", -0.20 * (s.id in ctx["false_starts"]))]
    importance = min(1.0, max(0.0, sum(v for _, v in terms)))
    ev["importance"] = [k for k, v in terms if v]
    virality = 0.30 * hook + 0.25 * importance + 0.10 * emotion + 0.10 * quotable + 0.10 * standalone
    scores = {"hook": r4(hook), "importance": r4(importance), "standalone": standalone, "quotable": quotable,
              "humour": 0.0, "emotion": r4(emotion), "virality": r4(virality), "quality": 0.5}
    return scores, {k: v for k, v in ev.items() if v}


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def _turns_by_gap(truth) -> list[dict]:
    """Utterances merged per speaker across gaps < 0.6 s (spec §3.4)."""
    turns: list[dict] = []
    for u in truth.utts:
        if u.kind in ("pause", "gap", "silence"):
            continue
        v0, v1 = u.voiced
        if turns and turns[-1]["spk"] == u.speaker and v0 - turns[-1]["t1"] < 0.6:
            turns[-1]["t1"] = r4(v1)
            if u.sent and u.sent not in turns[-1]["sents"]:
                turns[-1]["sents"].append(u.sent)
        else:
            turns.append({"id": f"u_{len(turns) + 1:04d}", "spk": u.speaker, "t0": r4(v0), "t1": r4(v1),
                          "sents": [u.sent] if u.sent else []})
    return turns


def _words(truth, ctx) -> list[dict]:
    out = []
    next_sent = {}
    utts = list(truth.utts)
    for i, u in enumerate(utts):
        if u.kind == "filler":
            next_sent[u.id] = next((x.sent for x in utts[i + 1:] if x.sent), None)
    for s in truth.sentences:
        for w in s.words:
            out.append({"id": w.id, "t0": r4(w.t0), "t1": r4(w.t1), "text": w.text, "prob": 1.0, "spk": s.speaker, "sent": s.id})
    for u in truth.utts:
        if u.kind == "filler":
            v0, v1 = u.voiced
            out.append({"id": f"w_{u.id[2:]}_f", "t0": r4(v0), "t1": r4(v1), "text": u.text, "prob": 1.0, "spk": u.speaker,
                        "sent": next_sent[u.id], "filler": True})
    return sorted(out, key=lambda w: (w["t0"], w["id"]))


def _sentence_rows(truth, ctx) -> list[dict]:
    rows = []
    for s in truth.sentences:
        f = s.features
        m = ctx["measured"][s.id]
        rows.append({"id": s.id, "t0": r4(s.t0), "t1": r4(s.t1), "spk": s.speaker, "text": s.text,
                     "kind": "question" if s.is_question else "statement", "is_question": s.is_question,
                     "answer_of": s.answer_of, "complete": True, "weak_start": bool(f.get("weak_start")), "topic": "t_0001",
                     "features": {"wpm": m["wpm"], "fillers": ctx["fillers"].get(s.id, 0),
                                  "has_number": bool(f.get("strong_number") or f.get("weak_number")),
                                  "strong_number": bool(f.get("strong_number")), "weak_number": bool(f.get("weak_number")),
                                  "claim": bool(f.get("claim")), "conclusion_marker": bool(f.get("conclusion_marker")),
                                  "story_marker": False, "contrast_words": int(f.get("contrast_words", 0)),
                                  "anaphora_start": bool(f.get("anaphora_start")), "len_words": int(_n_words(s))}})
    return rows


def _flags(truth, ctx, dead_air_s: float) -> dict:
    flags: dict = {"false_starts": [], "repeats": [], "weak_questions": [], "dead_air": [], "technical": []}
    if isinstance(truth, THTruth):
        flags["repeats"].append({"id": "r_0001", "dup": truth.retake.dup, "of": truth.retake.of, "similarity": 1.0})
    else:
        fs = truth.false_start
        flags["false_starts"].append({"id": "f_0001", "t0": r4(fs.t0), "t1": r4(fs.t1), "kept": fs.kept_sent, "text": fs.text})
    n = 0
    for p in truth.pauses:
        gap = p.t1 - p.t0
        in_turn = p.kind in ("in_turn", "retake_gap", "island_gap")
        if in_turn and gap >= dead_air_s and not p.protection:
            n += 1
            flags["dead_air"].append({"id": f"d_{n:04d}", "t0": r4(p.t0), "t1": r4(p.t1)})
    return flags


def speech_layer(truth, ctx, *, dead_air_s: float) -> dict:
    words = _words(truth, ctx)
    checks = [{"word": w["id"], "why": "digit"} for w in words if re.search(r"\d", w["text"])]
    acoustic = []
    for i, uid in enumerate(getattr(truth, "fillers_acoustic", ())):
        u = truth.utt(uid)
        acoustic.append({"id": f"af_{i + 1:04d}", "t0": r4(u.voiced[0]), "t1": r4(u.voiced[1]), "confidence": 0.9,
                         "evidence": {"pitch_range_st": 0.2, "flux": 0.05, "word_overlap": 0.0}})
    return {"params": {"backend": "truth", "model": "fixture", "language": "en", "prompt": "none",
                       "analysis_version": ANALYSIS_VERSION},
            "words": words, "acoustic_fillers": acoustic, "words_to_check": checks,
            "sentences": _sentence_rows(truth, ctx), "turns": _turns_by_gap(truth), "flags": _flags(truth, ctx, dead_air_s)}


def _vad(truth) -> list[list[float]]:
    spans = sorted(u.voiced for u in truth.utts if u.kind not in ("pause", "gap", "silence"))
    out: list[list[float]] = []
    for a, b in spans:
        if out and a - out[-1][1] < 0.1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[r4(a), r4(b)] for a, b in out]


def audio_layer(truth, ctx, own_mic: dict[str, list[float]] | None) -> dict:
    pcm, sr = ctx["pcm"], ctx["sr"]
    vad = _vad(truth)
    silences = []
    for (a0, a1), (b0, b1) in zip(vad, vad[1:]):
        if b0 - a1 >= 0.4:
            silences.append({"id": f"sil_{len(silences) + 1:04d}", "t0": a1, "t1": b0})
    env = np.clip(np.round(env_10ms_db(pcm, sr)), -127, 0).astype(np.int8)
    return {"hz": 100, "env_10ms": base64.b64encode(env.tobytes()).decode("ascii"), "vad": vad, "silences": silences,
            "loudness_i": r4(ctx["loudness_i"]), "noise_floor_db": -120.0, "clipping": [],
            "own_mic_energy": own_mic or {}, "events": []}


def speakers_layer(truth, ctx) -> dict:
    spks = sorted({s.speaker for s in truth.sentences})
    total = sum(s.t1 - s.t0 for s in truth.sentences)
    rows = []
    for i, spk in enumerate(spks):
        share = sum(s.t1 - s.t0 for s in truth.sentences if s.speaker == spk) / total
        q = sum(1 for s in truth.sentences if s.speaker == spk and s.is_question)
        role = truth.speakers[spk]["role"] if isinstance(truth, P2Truth) else "host"
        rows.append({"id": spk, "label": f"SPEAKER_{i:02d}", "share": r4(share), "questions": q, "role_guess": role})
    turns = ([{"id": t.id, "spk": t.speaker, "t0": r4(t.t0), "t1": r4(t.t1), "sents": list(t.sents)} for t in truth.turns]
             if isinstance(truth, P2Truth) else _turns_by_gap(truth))
    overlaps = [[r4(truth.overlap.t0), r4(truth.overlap.t1)]] if isinstance(truth, P2Truth) else []
    return {"engine": "truth", "k": len(spks), "k_method": "truth", "silhouette": None,
            "utterances": [{"t0": r4(u.voiced[0]), "t1": r4(u.voiced[1]), "spk": u.speaker}
                           for u in truth.utts if u.kind not in ("pause", "gap", "silence")],
            "turns": turns, "speakers": rows, "overlaps": overlaps, "flip_risk": []}


def semantic_layer(truth, ctx) -> dict:
    scores = {}
    for s in truth.sentences:
        sc, ev = score_sentence(s, ctx)
        scores[s.id] = {**sc, "evidence": ev}
    topic = {"id": "t_0001", "t0": r4(truth.sentences[0].t0), "t1": r4(truth.sentences[-1].t1),
             "title": ctx["topic_title"], "by": "truth", "sents": [s.id for s in truth.sentences]}
    return {"scores": scores, "annotations": [], "topics": [topic], "budget": {"calls": 0, "spent_s": 0.0, "partial_from": None}}


# --------------------------------------------------------------------------
# scenes and the header
# --------------------------------------------------------------------------

def _scene_record(i: int, sents: list, scores: dict, ctx: dict, angles: dict) -> dict:
    first = sents[0]
    keys = ("hook", "importance", "humour", "virality", "emotion", "quality", "quotable")
    agg = {k: max(scores[s.id][k] for s in sents) for k in keys}
    agg["standalone"] = scores[first.id]["standalone"]
    m = [ctx["measured"][s.id] for s in sents]
    f = {"wpm": int(round(sum(x["wpm"] for x in m) / len(m))), "fillers": sum(ctx["fillers"].get(s.id, 0) for s in sents),
         "false_start": any(s.id in ctx["false_starts"] for s in sents),
         "repeat_of": next((ctx["repeat_of"][s.id] for s in sents if s.id in ctx["repeat_of"]), None),
         "is_question": any(s.is_question for s in sents), "answer_of": first.answer_of,
         "has_number": any(s.features.get("strong_number") or s.features.get("weak_number") for s in sents),
         "strong_number": any(s.features.get("strong_number") for s in sents),
         "weak_number": any(s.features.get("weak_number") for s in sents),
         "contrast_words": sum(int(s.features.get("contrast_words", 0)) for s in sents),
         "anaphora_start": bool(first.features.get("anaphora_start")),
         "len_words": sum(int(s.features.get("len_words", 0)) for s in sents),
         "claim": any(s.features.get("claim") for s in sents), "conclusion_marker": any(s.features.get("conclusion_marker") for s in sents),
         "story_marker": False, "rms_z": r4(max(x["rms_z"] for x in m)), "pitch_range_st": 0.0,
         "stretch": r4(max(x["stretch"] for x in m)), "dead_air_s": 0.0, "laughter": False, "overlap_s": 0.0}
    active = angles.get(first.speaker)
    ev = {}
    for s in sents:
        for k, v in scores[s.id]["evidence"].items():
            ev.setdefault(k, [])
            ev[k] += [x for x in v if x not in ev[k]]
    return {"id": f"sc_{i:04d}", "kind": "speech", "t0": r4(first.t0), "t1": r4(sents[-1].t1), "spk": first.speaker,
            "topic": "t_0001", "sents": [s.id for s in sents], "text": " ".join(s.text for s in sents), "features": f,
            "shot": {"angles": {}, "active_angle": active, "motion": 0.0, "face_lost_frac": 0.0},
            "quality": {"sharpness": 0.5, "exposure": 0.5, "shake": 0.0, "noise_db": -120.0, "clipping": 0.0},
            "scores": agg, "evidence": ev,
            "labels": {"emotion": "emphatic" if agg["emotion"] >= 0.6 else "calm",
                       "function": "question" if f["is_question"] else ("conclusion" if f["conclusion_marker"] else "statement")},
            "by": {"scores": "truth", "annotations": []}}


def scenes(truth, semantic: dict, ctx: dict, angles: dict) -> list[dict]:
    groups: list[list] = []
    for s in truth.sentences:
        if groups:
            prev = groups[-1]
            gap = s.t0 - prev[-1].t1
            if s.speaker == prev[0].speaker and gap < 1.2 and (s.t1 - prev[0].t0) <= 12.0:
                prev.append(s)
                continue
        groups.append([s])
    out = [_scene_record(i + 1, g, semantic["scores"], ctx, angles) for i, g in enumerate(groups)]
    n = len(out)
    for a, b in zip(list(out), list(out)[1:]):
        if b["t0"] - a["t1"] >= 1.5:
            n += 1
            out.append({"id": f"sc_{n:04d}", "kind": "pause", "t0": a["t1"], "t1": b["t0"], "spk": None, "topic": "t_0001",
                        "sents": [], "text": "", "features": {}, "shot": {}, "quality": {}, "scores": {}, "evidence": {},
                        "labels": {}, "by": {"scores": "truth", "annotations": []}})
    return sorted(out, key=lambda sc: sc["t0"])


#: What C's frozen `brain.schema.Scene` accepts under `features` (its
#: `Features` model). The spec's Appendix A scene also carries rms_z,
#: pitch_range_st, stretch, dead_air_s, laughter, overlap_s, false_start,
#: repeat_of, is_question, answer_of and a `by` provenance dict; the models
#: as frozen reject those (`extra="forbid"`), so the golden's `scenes` hold
#: only the accepted keys and the rest travels in `fixture.scene_measured`.
SCENE_FEATURE_KEYS = ("wpm", "fillers", "has_number", "strong_number", "weak_number", "claim", "conclusion_marker",
                      "story_marker", "contrast_words", "anaphora_start", "len_words")


def split_scenes(raw: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """(scenes conforming to the frozen model, {scene id: measured extras})."""
    clean, measured = [], {}
    for sc in raw:
        feats = sc.get("features") or {}
        extras = {k: v for k, v in feats.items() if k not in SCENE_FEATURE_KEYS}
        rec = {k: v for k, v in sc.items() if k != "by"}
        rec["features"] = {k: v for k, v in feats.items() if k in SCENE_FEATURE_KEYS} if feats else {}
        clean.append(rec)
        if extras:
            measured[sc["id"]] = extras
    return clean, measured


def header(*, truth, sources: list[dict], layers: dict, speakers_rows: list[dict], content_type: dict, fps: int,
           canvas: list[int], music_hint: dict, topic_title: str) -> dict:
    digests = {k: digest(v) for k, v in layers.items()}
    ref = next(s for s in sources if s.get("dialogue"))
    gid_material = sorted(digests.values()) + [str(s["sync_offset_s"]) for s in sources] + [s["role"] for s in sources] + [str(ANALYSIS_VERSION)]
    gid = "g_" + hashlib.sha256("|".join(gid_material).encode()).hexdigest()[:12]
    return {"version": 1, "id": gid, "analysis_version": ANALYSIS_VERSION, "clock": "reference", "reference": ref["key"],
            "sources": sources, "speakers": speakers_rows, "content_type": content_type,
            "layers": {k: f"{k}/truth-v{ANALYSIS_VERSION}.json" for k in layers}, "digests": digests, "scenes": "scenes.json",
            "topics": [{"id": "t_0001", "t0": r4(truth.sentences[0].t0), "t1": r4(truth.sentences[-1].t1), "title": topic_title,
                        "by": "truth", "sents": [s.id for s in truth.sentences]}],
            "music_hint": music_hint,
            "project": {"canvas": canvas, "fps": fps, "session_language": "en", "controls_seen": []},
            "timings_s": {"transcript": 0.0, "audio": 0.0, "speakers": 0.0, "visual": 0.0, "semantic": 0.0}}


def _header_speakers(truth, spk_layer: dict, angle_of: dict) -> list[dict]:
    names = {"S1": "Host", "S2": "Guest"}
    return [{"id": r["id"], "label": r["label"], "name": None if isinstance(truth, P2Truth) else names["S1"],
             "role_guess": r["role_guess"], "share": r["share"], "questions": r["questions"],
             "angle_hint": angle_of.get(r["id"])} for r in spk_layer["speakers"]]


def _ctx(truth, ref_path: str, *, topic_peak: str, topic_title: str) -> dict:
    m = measured(truth, ref_path)
    fillers: dict[str, int] = {}
    utts = list(truth.utts)
    for i, u in enumerate(utts):
        if u.kind == "filler":
            nxt = next((x.sent for x in utts[i + 1:] if x.sent), None)
            if nxt:
                fillers[nxt] = fillers.get(nxt, 0) + 1
    ctx = {"measured": m["per_sent"], "pcm": m["pcm"], "sr": m["sr"], "sents_by_id": {s.id: s for s in truth.sentences},
           "fillers": fillers, "topic_peak": topic_peak, "topic_title": topic_title,
           "false_starts": {truth.false_start.kept_sent} if isinstance(truth, P2Truth) else set(),
           "repeat_of": {truth.retake.dup: truth.retake.of} if isinstance(truth, THTruth) else {},
           "loudness_i": integrated_lufs(Path(ref_path))}
    return ctx


# --------------------------------------------------------------------------
# the two goldens
# --------------------------------------------------------------------------

def th_golden(fx) -> dict:
    t: THTruth = fx.th.truth
    ref = fx.th.video_16x9
    ctx = _ctx(t, fx.th.audio_wav, topic_peak=t.emphasis.sent, topic_title="the index card habit")
    layers = {"speech": speech_layer(t, ctx, dead_air_s=0.6), "audio": audio_layer(t, ctx, None),
              "speakers": speakers_layer(t, ctx), "semantic": semantic_layer(t, ctx)}
    key = content_key(ref)
    sources = [{"key": key, "role": "angle", "angle": "A", "leaf": Path(ref).name, "duration": r4(t.duration),
                "sync_offset_s": 0.0, "has_video": True, "has_audio": True, "fps": str(t.fps), "dialogue": True,
                "layers": {k: "ok" for k in layers}, "angle_guess": {"kind": "close", "sees": ["S1"], "confidence": 1.0, "by": "truth"}}]
    hdr = header(truth=t, sources=sources, layers=layers, speakers_rows=_header_speakers(t, layers["speakers"], {"S1": "A"}),
                 content_type={"guess": "talking_head", "confidence": 1.0, "evidence": ["1 speaker"]}, fps=t.fps,
                 canvas=[1920, 1080], music_hint={"mood": "upbeat", "energy": 0.6, "evidence": ["talking_head", "reel"]},
                 topic_title="the index card habit")
    th_scenes, th_measured = split_scenes(scenes(t, layers["semantic"], ctx, {"S1": key}))
    return {"fixture": {"name": "talking_head", "files": {"reference": Path(ref).name, "vertical": Path(fx.th.video_9x16).name},
                        "notes": ["scores by the §3.4 formulas over truth features; rms_z and stretch measured; see gen_brain_fixture_graphs.py",
                                  "scene provenance is `truth` for every score (no annotations); the frozen Scene model has no `by`",
                                  "the 9:16 render is the same programme with bar-code sid 2"],
                        "scene_measured": th_measured},
            "graph": hdr, "layers": layers, "scenes": th_scenes,
            "angles": {"reference": key, "dialogue": key,
                       "members": [{"angle": "A", "src_key": key, "path": Path(ref).name, "sync_offset_s": 0.0, "confidence": 1.0,
                                    "sees": ["S1"], "by": "truth"}]}}


def _own_mic(t: P2Truth, path: str, offset: float) -> list[float]:
    pcm, sr = _pcm(path)
    out = []
    for tr in t.turns:
        a, b = int((tr.t0 + offset + 0.05) * sr), int((tr.t1 + offset - 0.05) * sr)
        out.append(r4(rms_db(pcm[a:b])) if b > a else -120.0)
    return out


def p2_golden(fx) -> dict:
    t: P2Truth = fx.p2.truth
    ctx = _ctx(t, fx.p2.recorder_wav, topic_peak=t.quotable, topic_title="a camera from a garage")
    keys = {"recorder": content_key(fx.p2.recorder_wav), "cam_a": content_key(fx.p2.cam_a), "cam_b": content_key(fx.p2.cam_b)}
    own = {keys["cam_a"]: _own_mic(t, fx.p2.cam_a, t.offsets["cam_a"]), keys["cam_b"]: _own_mic(t, fx.p2.cam_b, t.offsets["cam_b"])}
    layers = {"speech": speech_layer(t, ctx, dead_air_s=1.2), "audio": audio_layer(t, ctx, own),
              "speakers": speakers_layer(t, ctx), "semantic": semantic_layer(t, ctx)}
    sources = [{"key": keys["recorder"], "role": "reference_audio", "leaf": Path(fx.p2.recorder_wav).name, "duration": r4(t.duration),
                "sync_offset_s": 0.0, "has_video": False, "has_audio": True, "dialogue": True, "layers": {k: "ok" for k in layers}}]
    for cam, angle in (("cam_a", "A"), ("cam_b", "B")):
        path = getattr(fx.p2, cam)
        sees = t.own_mic[cam]
        sources.append({"key": keys[cam], "role": "angle", "angle": angle, "leaf": Path(path).name,
                        "duration": r4(t.duration + t.offsets[cam]), "sync_offset_s": t.offsets[cam], "has_video": True,
                        "has_audio": True, "fps": str(t.fps), "layers": {"audio": "ok", "visual": "lazy"},
                        "angle_guess": {"kind": "close", "sees": [sees], "confidence": 1.0, "by": "truth"}})
    angle_of = {t.own_mic["cam_a"]: "A", t.own_mic["cam_b"]: "B"}
    hdr = header(truth=t, sources=sources, layers=layers, speakers_rows=_header_speakers(t, layers["speakers"], angle_of),
                 content_type={"guess": "podcast", "confidence": 1.0, "evidence": ["2 speakers", "balanced shares"]}, fps=t.fps,
                 canvas=[320, 180], music_hint={"mood": "chill", "energy": 0.4, "evidence": ["podcast"]},
                 topic_title="a camera from a garage")
    members = [{"angle": "A", "src_key": keys["cam_a"], "path": Path(fx.p2.cam_a).name, "sync_offset_s": t.offsets["cam_a"],
                "confidence": 1.0, "sees": [t.own_mic["cam_a"]], "by": "truth"},
               {"angle": "B", "src_key": keys["cam_b"], "path": Path(fx.p2.cam_b).name, "sync_offset_s": t.offsets["cam_b"],
                "confidence": 1.0, "sees": [t.own_mic["cam_b"]], "by": "truth"}]
    p2_scenes, p2_measured = split_scenes(scenes(t, layers["semantic"], ctx, {t.own_mic[c]: keys[c] for c in ("cam_a", "cam_b")}))
    return {"fixture": {"name": "two_cam_podcast", "files": {k: Path(getattr(fx.p2, k if k != "recorder" else "recorder_wav")).name
                                                              for k in ("recorder", "cam_a", "cam_b")},
                        "notes": ["scores by the §3.4 formulas over truth features; rms_z and stretch measured; see gen_brain_fixture_graphs.py",
                                  "scene provenance is `truth` for every score (no annotations); the frozen Scene model has no `by`",
                                  "own_mic_energy: RMS dBFS per truth turn measured in each camera file, in truth turn order"],
                        "scene_measured": p2_measured},
            "graph": hdr, "layers": layers, "scenes": p2_scenes,
            "angles": {"reference": keys["recorder"], "dialogue": keys["recorder"], "members": members}}


def render(obj: dict) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def generate(fx) -> dict[str, str]:
    out = {"talking_head.json": render(th_golden(fx))}
    if fx.p2 is not None:
        out["two_cam_podcast.json"] = render(p2_golden(fx))
    return out


def main() -> int:
    from brain_fixtures import build_brain_fixtures
    fx = build_brain_fixtures()
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in generate(fx).items():
        (GOLDEN_DIR / name).write_text(text, encoding="utf-8")
        print(GOLDEN_DIR / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
