"""Synthetic Content Graphs for the Editor Brain planner and the planner
goldens (EB1 lane E).

Lane A hand-builds `tests/goldens/brain/graphs/{talking_head,two_cam_podcast}.json`
from the fixtures' truth; until they land, `plan()` is developed and pinned
against the two graphs BUILT HERE, written to
`tests/goldens/brain/planner/graphs/` in the frozen graph shape
(EB1_BRIEF.md "Graph", spec §3.1-3.3) with every layer inline:

    {"version", "id", "analysis_version", "clock", "reference", "sources",
     "speakers", "content_type", "project", "music_hint",
     "layers": {"speech", "audio", "speakers", "semantic"}, "scenes", "angles"}

Both graphs mirror the brief's fixtures so the planner's rules are exercised
on the events the slice measures:

  * TH — talking head, one source, 14 sentences: the quotable claim with a
    strong number and a superlative (the hook truth), the year-and-headcount
    throwaway, one sentence delivered +6 dB and 15 % slower with a comma
    clause before its peak word (the emphasis truth), a retake pair (the
    later is `dup`), one question, six lexical "um"s, three acoustic "uh"
    islands whisper never hears, five 1.5 s pauses, "thanks for watching".
  * P2 — two-camera podcast with a recorder: host (S1, camera A) and guest
    (S2, camera B), 40 scripted turns with 4 backchannels, 2 fillers per
    speaker, one false start, one 2.4 s in-turn pause, two 1.8 s
    turn-boundary silences, one 1.0 s pause after an emotional line (kept),
    one 0.8 s overlap; per-file offsets A +0.35 s, B −0.20 s.

Clock: reference seconds everywhere in the layers (spec §2.4); a file's own
seconds are `t_ref + sync_offset_s` (the graph example's `ref_t0 = −offset`).

Run as a script to (re)write the planner goldens:

    .venv/bin/python tests/gen_brain_goldens.py

`PLANNER_VERSION` must be bumped to regenerate a golden that changed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
GRAPH_DIR = REPO / "tests" / "goldens" / "brain" / "planner" / "graphs"
A_GRAPH_DIR = REPO / "tests" / "goldens" / "brain" / "graphs"
EDP_DIR = REPO / "tests" / "goldens" / "brain" / "edp"

FPS = 30
HZ = 100
SPEECH_DB, SPEECH_EDGE_DB = -20, -24          # inside a word: a shallow triangle
GAP_EDGE_DB, GAP_DB = -38, -44                # between words in a sentence: a V
SILENCE_DB = -58                              # pauses and the ends
ISLAND_DB = -26                               # an acoustic "uh" whisper drops
_WORD = re.compile(r"[\w'’]+")
_TERMINAL = (".", "?", "!", "…")

TH_KEY = "src_e10000000000000000000001"
TH_PATH = "/fixtures/brain/talking_head/talking_head.mp4"
P2_REC_KEY, P2_A_KEY, P2_B_KEY = "src_e20000000000000000000001", "src_e20000000000000000000002", "src_e20000000000000000000003"
P2_REC_PATH = "/fixtures/brain/two_cam_podcast/recorder.wav"
P2_A_PATH = "/fixtures/brain/two_cam_podcast/cam_a_host.mp4"
P2_B_PATH = "/fixtures/brain/two_cam_podcast/cam_b_guest.mp4"
P2_OFFSETS = {P2_REC_KEY: 0.0, P2_A_KEY: 0.35, P2_B_KEY: -0.20}


# --------------------------------------------------------------------------- the script model

@dataclass
class Line:
    """One scripted sentence with the truth flags the scorers read."""
    text: str
    spk: str = "S1"
    claim: bool = False
    conclusion: bool = False
    contrast: int = 0
    strong_number: bool = False
    weak_number: bool = False
    emphasis: bool = False            # +6 dB, 1.15× slower (the punch-in truth)
    emotion: float = 0.0              # arousal set by hand (an emotional line)
    retake_of: int | None = None      # index of the sentence this one repeats
    fillers: tuple[int, ...] = ()     # word indexes that are "um"
    acoustic: tuple[int, ...] = ()    # "uh" islands: a 0.3 s voiced island in a 0.75 s gap after these words
    pause_after: float = 0.5          # silence before the next line
    turn_start: bool = True           # a new speaker turn begins here
    backchannel: bool = False
    false_start: str | None = None    # a cut-off run spoken right before this line
    overlap: float = 0.0              # starts this many seconds before the previous line ends
    answer_of: int | None = None
    imperative: bool = False
    topic_peak: bool = False
    dead_air: tuple[int, float] | None = None   # (word index, seconds): a gap inside the turn before that word
    hook_floor: float = 0.0           # the planted score of a line the truth calls a hook (a hard question)
    stress: int | None = None         # the word the speaker leans on (+2 dB over the sentence's own boost)


@dataclass
class _Built:
    words: list[dict] = field(default_factory=list)
    sentences: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    islands: list[dict] = field(default_factory=list)
    false_starts: list[dict] = field(default_factory=list)
    repeats: list[dict] = field(default_factory=list)
    dead_air: list[dict] = field(default_factory=list)
    end: float = 0.0
    boosts: list[tuple[float, float, float]] = field(default_factory=list)   # (t0, t1, dB)


def _word_dur(token: str, stretch: float) -> float:
    core = _WORD.findall(token)
    n = len(core[0]) if core else 1
    return round(min(0.55, 0.10 + 0.045 * n) * stretch, 3)


def _speak(b: _Built, line: Line, idx: int, t: float, *, dead_air_at: tuple[int, float] | None = None) -> float:
    """Lay the words of one line from `t`; returns the end of its last word."""
    stretch = 1.15 if line.emphasis else 1.0
    gap = round(0.07 * stretch, 3)
    sid = f"s_{idx + 1:05d}"
    tokens = line.text.split()
    start = t
    for k, tok in enumerate(tokens):
        if dead_air_at and dead_air_at[0] == k:
            d0, d1 = t, t + dead_air_at[1]
            b.dead_air.append({"id": f"d_{len(b.dead_air) + 1:04d}", "t0": round(d0, 3), "t1": round(d1, 3)})
            t = d1
        w0, w1 = t, t + _word_dur(tok, stretch)
        b.words.append({"id": f"w_{len(b.words) + 1:06d}", "t0": round(w0, 3), "t1": round(w1, 3), "text": tok,
                        "prob": 1.0, "spk": line.spk, "sent": sid, **({"filler": True} if k in line.fillers else {})})
        if line.stress == k:
            b.boosts.append((w0, w1, 2.0))
        t = w1 + gap + (0.28 if tok.endswith(",") else 0.0)
        if k in line.acoustic:
            i0 = round(t + 0.2, 3)
            b.islands.append({"id": f"af_{len(b.islands) + 1:04d}", "t0": i0, "t1": round(i0 + 0.30, 3), "confidence": 0.82,
                              "evidence": {"pitch_range_st": 0.8, "flux": 0.05, "word_overlap": 0.0}})
            t = round(t + 0.75, 3)
    if line.emphasis:
        b.boosts.append((start, b.words[-1]["t1"], 6.0))
    return b.words[-1]["t1"]


def _features(line: Line, words: list[dict], dur: float) -> dict:
    n = len(words)
    first = _WORD.findall(words[0]["text"].lower())[0] if words else ""
    return {"wpm": int(round(60.0 * n / max(dur, 0.1))), "fillers": len(line.fillers), "has_number": bool(line.strong_number or line.weak_number),
            "strong_number": bool(line.strong_number), "weak_number": bool(line.weak_number), "claim": bool(line.claim),
            "conclusion_marker": bool(line.conclusion), "story_marker": False, "contrast_words": int(line.contrast),
            "anaphora_start": first in ("it", "that", "this", "they", "these", "those", "which"),
            "len_words": n, "_conj": first in ("and", "but", "so", "or", "because")}


def _scores(line: Line, feat: dict, is_q: bool, *, dup: bool, false_start: bool, answer_len_norm: float) -> tuple[dict, dict]:
    """The §3.4 formulas, by hand, from the line's truth flags."""
    rms_z = 1.8 if line.emphasis else 0.0
    stretch = 1.15 if line.emphasis else 1.0
    emotion = max(line.emotion, min(1.0, 0.5 * rms_z + 0.3 * (0.4 if line.emphasis else 0.1) + 0.2 * abs(stretch - 1.0) * 4))
    delivery = max(emotion, min(1.0, rms_z))
    weak_start = feat["_conj"] or feat["anaphora_start"]
    standalone = max(0.0, 1.0 - (1.0 if feat["anaphora_start"] else 0.0) - (0.5 if feat["_conj"] else 0.0))
    candidate = line.claim or line.contrast > 0 or is_q or line.imperative
    hook = 0.0
    if candidate:
        hook = (0.25 * is_q + 0.20 * (line.claim or line.conclusion) + 0.15 * line.strong_number + 0.15 * delivery
                + 0.10 * min(1, line.contrast) + 0.10 * standalone - 0.30 * weak_start - 0.30 * feat["anaphora_start"]
                - 0.20 * (feat["len_words"] > 20))
        if delivery < 0.3 and line.contrast == 0:
            hook *= 0.7
    filler_rate = feat["fillers"] / max(1, feat["len_words"])
    importance = (0.30 * answer_len_norm + 0.22 * line.claim + 0.08 * line.strong_number + 0.15 * line.topic_peak
                  + 0.10 * line.conclusion + 0.15 * min(1.0, rms_z) - 0.25 * dup - 0.20 * filler_rate - 0.20 * false_start)
    quotable = 1.0 if (feat["len_words"] <= 18 and (line.claim or line.strong_number) and not feat["anaphora_start"]) else 0.4
    virality = 0.30 * hook + 0.25 * importance + 0.10 * emotion + 0.10 * quotable + 0.10 * standalone
    clip = lambda v: round(max(0.0, min(1.0, v)), 3)  # noqa: E731
    hook = max(hook, line.hook_floor)
    scores = {"hook": clip(hook), "importance": clip(importance), "standalone": clip(standalone), "quotable": clip(quotable),
              "emotion": clip(emotion), "virality": clip(virality),
              "evidence": {"hook": [k for k, v in (("question", is_q), ("claim", line.claim), ("strong_number", line.strong_number),
                                                   ("contrast_words", line.contrast > 0), ("delivery", delivery >= 0.3)) if v],
                           "importance": [k for k, v in (("claim", line.claim), ("topic_peak", line.topic_peak),
                                                         ("rms_z", rms_z > 0), ("repeat", dup)) if v]}}
    return scores, {}


def _build(script: list[Line], *, speakers: dict[str, dict]) -> _Built:
    b = _Built()
    t = 0.6
    prev_end = 0.0
    turn: dict | None = None
    q_index: dict[int, int] = {}
    for i, line in enumerate(script):
        sid = f"s_{i + 1:05d}"
        if line.overlap:
            t = max(0.0, prev_end - line.overlap)
        if line.false_start:
            f0 = t
            for tok in line.false_start.split():
                b.words.append({"id": f"w_{len(b.words) + 1:06d}", "t0": round(t, 3), "t1": round(t + _word_dur(tok, 1.0), 3),
                                "text": tok, "prob": 1.0, "spk": line.spk, "sent": sid})
                t = b.words[-1]["t1"] + 0.07
            b.false_starts.append({"id": f"f_{len(b.false_starts) + 1:04d}", "t0": round(f0, 3), "t1": round(t, 3),
                                   "kept": sid, "text": line.false_start})
            t += 0.35
        dead = line.dead_air
        w_before = len(b.words)
        end = _speak(b, line, i, t, dead_air_at=dead)
        words = b.words[w_before:]
        is_q = line.text.rstrip().endswith("?")
        feat = _features(line, words, end - words[0]["t0"])
        dup = line.retake_of is not None
        if dup:
            b.repeats.append({"id": f"r_{len(b.repeats) + 1:04d}", "dup": sid, "of": f"s_{line.retake_of + 1:05d}", "similarity": 1.0})
        answer_norm = min(1.0, (end - words[0]["t0"]) / 8.0) if line.answer_of is not None else 0.0
        scores, extras = _scores(line, feat, is_q, dup=dup, false_start=bool(line.false_start), answer_len_norm=answer_norm)
        b.sentences.append({"id": sid, "t0": words[0]["t0"], "t1": end, "spk": line.spk, "text": line.text,
                            "kind": "question" if is_q else "statement", "is_question": is_q,
                            "answer_of": f"s_{line.answer_of + 1:05d}" if line.answer_of is not None else None,
                            "complete": line.text.rstrip().endswith(_TERMINAL), "weak_start": feat["_conj"],
                            "topic": "t_001", "features": {k: v for k, v in feat.items() if not k.startswith("_")},
                            "_scores": scores,
                            "_backchannel": line.backchannel})
        if line.turn_start or turn is None or turn["spk"] != line.spk:
            turn = {"id": f"u_{len(b.turns) + 1:04d}", "spk": line.spk, "t0": words[0]["t0"], "t1": end, "sents": [sid]}
            b.turns.append(turn)
        else:
            turn["t1"] = end
            turn["sents"].append(sid)
        if is_q:
            q_index[i] = i
        prev_end = end
        t = end + line.pause_after
    b.end = round(prev_end + 1.0, 3)
    return b


# --------------------------------------------------------------------------- layers

def _env(b: _Built) -> str:
    n = int(math.ceil(b.end * HZ)) + 1
    env = [SILENCE_DB] * n
    words = sorted(b.words, key=lambda w: w["t0"])
    for w in words:
        f0, f1 = int(round(w["t0"] * HZ)), int(round(w["t1"] * HZ))
        mid = (f0 + f1) / 2.0
        half = max(1.0, (f1 - f0) / 2.0)
        for f in range(f0, min(n, f1)):
            env[f] = int(round(SPEECH_DB + (SPEECH_EDGE_DB - SPEECH_DB) * abs(f - mid) / half))
    for a, c in zip(words, words[1:]):
        f0, f1 = int(round(a["t1"] * HZ)), int(round(c["t0"] * HZ))
        if f1 <= f0:
            continue
        gap = f1 - f0
        for f in range(f0, min(n, f1)):
            if gap <= 40:                       # inside a sentence: a V between the words
                x = abs((f - f0) - gap / 2.0) / (gap / 2.0)
                env[f] = int(round(GAP_DB + (GAP_EDGE_DB - GAP_DB) * x))
            else:                               # a pause: 60 ms of decay/onset on either side
                d = min(f - f0, f1 - 1 - f)
                env[f] = SILENCE_DB if d >= 6 else int(round(GAP_EDGE_DB + (SILENCE_DB - GAP_EDGE_DB) * d / 6))
    for isl in b.islands:
        for f in range(int(round(isl["t0"] * HZ)), min(n, int(round(isl["t1"] * HZ)))):
            env[f] = ISLAND_DB
    for t0, t1, db in b.boosts:
        for f in range(int(round(t0 * HZ)), min(n, int(round(t1 * HZ)))):
            if env[f] > GAP_EDGE_DB:
                env[f] = int(round(env[f] + db))
    return base64.b64encode(bytes((v + 256) % 256 for v in env)).decode("ascii")


def _silences(b: _Built) -> list[dict]:
    out = []
    words = sorted(b.words, key=lambda w: w["t0"])
    for a, c in zip(words, words[1:]):
        if c["t0"] - a["t1"] >= 0.4:
            out.append({"id": f"sil_{len(out) + 1:04d}", "t0": round(a["t1"] + 0.06, 3), "t1": round(c["t0"] - 0.06, 3)})
    return out


def _scenes(b: _Built) -> list[dict]:
    """§3.3 steps 1-2: same-speaker sentences merged forward while ≤ 12 s,
    split at pauses ≥ 1.2 s; a pause ≥ 1.5 s is a scene of its own."""
    scenes: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        if cur:
            scenes.append({"id": f"sc_{len(scenes) + 1:04d}", "kind": "speech", "t0": cur[0]["t0"], "t1": cur[-1]["t1"],
                           "spk": cur[0]["spk"], "topic": "t_001", "sents": [s["id"] for s in cur],
                           "text": " ".join(s["text"] for s in cur)})
            cur.clear()
    for s in b.sentences:
        if cur and (s["spk"] != cur[0]["spk"] or s["t0"] - cur[-1]["t1"] >= 1.2 or s["t1"] - cur[0]["t0"] > 12.0):
            gap0, gap1 = cur[-1]["t1"], s["t0"]
            flush()
            if gap1 - gap0 >= 1.5:
                scenes.append({"id": f"sc_{len(scenes) + 1:04d}", "kind": "pause", "t0": gap0, "t1": gap1, "spk": None,
                               "topic": "t_001", "sents": [], "text": ""})
        cur.append(s)
    flush()
    return scenes


def _graph(name: str, b: _Built, *, sources: list[dict], speakers: list[dict], content_type: dict,
           angles: dict, reference: str, music_mood: str) -> dict:
    sentences = [{k: v for k, v in s.items() if not k.startswith("_")} for s in b.sentences]
    speech = {"params": {"backend": "hand", "model": "truth", "language": "en", "prompt": "none", "analysis_version": 1},
              "words": b.words, "acoustic_fillers": b.islands, "words_to_check": [], "sentences": sentences,
              "turns": b.turns, "flags": {"false_starts": b.false_starts, "repeats": b.repeats, "weak_questions": [],
                                          "dead_air": b.dead_air, "technical": []}}
    audio = {"hz": HZ, "env_10ms": _env(b), "vad": [[w["t0"], w["t1"]] for w in b.words], "silences": _silences(b),
             "loudness_i": -19.0, "noise_floor_db": -58.0, "clipping": [], "own_mic_energy": {}, "events": []}
    spk_layer = {"engine": "hand", "k": len(speakers), "k_method": "truth", "silhouette": 0.4,
                 "utterances": [{"t0": t["t0"], "t1": t["t1"], "spk": t["spk"]} for t in b.turns], "turns": b.turns,
                 "speakers": [{k: v for k, v in s.items() if k != "angle_hint"} for s in speakers], "overlaps": [], "flip_risk": []}
    semantic = {"scores": {s["id"]: s["_scores"] for s in b.sentences}, "annotations": [], "topics": [],
                "budget": {"calls": 0, "spent_s": 0.0, "partial_from": None}}
    layers = {"speech": speech, "audio": audio, "speakers": spk_layer, "semantic": semantic}
    digests = {k: "sha256:" + hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest() for k, v in layers.items()}
    gid = "g_" + hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()[:12]
    header = {"version": 1, "id": gid, "analysis_version": 1, "clock": "reference", "reference": reference,
              "sources": sources, "speakers": speakers, "content_type": content_type,
              "layers": {k: f"{k}/hand-truth-v1.json" for k in layers}, "digests": digests, "scenes": "scenes.json",
              "topics": [{"id": "t_001", "t0": 0.0, "t1": b.end, "title": name, "by": "rules", "sents": [s["id"] for s in sentences]}],
              "music_hint": {"mood": music_mood, "energy": 0.5, "evidence": [content_type["guess"]]},
              "project": {"canvas": [1920, 1080], "fps": FPS, "session_language": "en", "controls_seen": []},
              "timings_s": {}}
    # the golden envelope: lane C's header + every layer inline + scenes + angles (planner/graph_view.py reads it)
    return {"graph": header, "layers": layers, "scenes": _scenes(b), "angles": angles}


# --------------------------------------------------------------------------- TH: the talking head

def th_script() -> list[Line]:
    return [
        Line("Hey everyone, today we are, um, looking at the lens I have used all year.", fillers=(5,)),
        Line("It is not the one the reviews told you to buy.", contrast=1, acoustic=(4,), pause_after=1.5),
        Line("We started in 2019 with three people and one, um, borrowed camera.", weak_number=True, fillers=(9,)),
        Line("Most of that first year was spent, um, returning gear that did not survive a shoot.", fillers=(7,)),
        Line("The cheapest lens here outperforms the ten thousand dollar one.", claim=True, strong_number=True, contrast=1,
             emotion=0.4, pause_after=1.5),
        Line("I know how that sounds, so, um, let me show you the frames.", fillers=(6,), acoustic=(9,)),
        Line("So what changed?"),
        Line("The coating on the new glass handles backlight without flaring."),
        Line("The coating on the new glass handles backlight without flaring.", retake_of=7, pause_after=1.5),
        Line("Look at the edges of this frame, um, and then at the same edge on the expensive one.", fillers=(7,), acoustic=(11,)),
        Line("And when the light drops, this is where it earns its price.", claim=True, emphasis=True, stress=9, topic_peak=True,
             pause_after=1.5),
        Line("So the takeaway is: buy the glass, not the body.", conclusion=True, claim=True, imperative=True),
        Line("It is the only piece of kit I have never, um, regretted.", contrast=1, fillers=(10,), pause_after=1.5),
        Line("Thanks for watching."),
    ]


def _single_source_graph(name: str, script: list[Line], *, key: str, path: str, content_type: str,
                         music_mood: str, questions: int = 1) -> dict:
    """A one-camera graph (its own file is the dialogue source and the clock)."""
    speakers = [{"id": "S1", "label": "SPEAKER_00", "name": None, "role_guess": "host", "share": 1.0,
                 "questions": questions, "angle_hint": "A"}]
    b = _build(script, speakers={"S1": speakers[0]})
    sources = [{"key": key, "role": "angle", "angle": "A", "leaf": Path(path).name, "path": path, "duration": b.end,
                "sync_offset_s": 0.0, "has_video": True, "has_audio": True, "fps": "30/1", "dialogue": True,
                "layers": {"speech": "ok", "audio": "ok", "speakers": "ok", "semantic": "ok"},
                "angle_guess": {"kind": "close", "sees": ["S1"], "confidence": 1.0, "by": "single"}}]
    angles = {"reference": key, "dialogue": key,
              "members": [{"angle": "A", "src_key": key, "path": path, "sync_offset_s": 0.0, "confidence": 1.0,
                           "sees": ["S1"], "by": "single"}]}
    return _graph(name, b, sources=sources, speakers=speakers, reference=key, angles=angles,
                  content_type={"guess": content_type, "confidence": 0.95, "evidence": ["1 speaker"]}, music_mood=music_mood)


def simple_graph(script: list[Line], *, name: str = "hand-built", path: str = "/fixtures/brain/hand/hand.mp4",
                 content_type: str = "talking_head") -> dict:
    """A hand-built one-camera graph for a unit test (spec §13.2 "hand-built scene lists")."""
    return _single_source_graph(name, script, key="src_e90000000000000000000001", path=path, content_type=content_type,
                                music_mood="chill")


def encode_env(values_db: list[int]) -> str:
    """A bespoke 100 Hz envelope (int8 dBFS per frame) in the layer's base64 form."""
    return base64.b64encode(bytes((int(v) + 256) % 256 for v in values_db)).decode("ascii")


def th_graph() -> dict:
    script = th_script()
    script[7].pause_after = 0.8                      # the retake: sentence, 0.8 s, the same sentence again
    return _single_source_graph("talking head", script, key=TH_KEY, path=TH_PATH, content_type="talking_head",
                                music_mood="upbeat")


# --------------------------------------------------------------------------- P2: the two-camera podcast

def p2_script() -> list[Line]:
    H, G = "S1", "S2"
    lines = [
        Line("Welcome back to the show, where today we are talking about raising money for a hardware company.", H),
        Line("Thanks for having me, it is a strange business to fund.", G, answer_of=0),
        Line("So let's start at the beginning, why hardware?", H),
        Line("Because nobody else wanted to do it, and the margins in hardware were, um, terrible.", G,
             contrast=1, fillers=(13,), answer_of=2),
        Line("mm-hm", H, backchannel=True, pause_after=0.3),
        Line("So we built the first prototype in a garage and shipped it to twelve customers.", G, strong_number=True, claim=True),
        Line("Twelve customers, how did you find them?", H),
        Line("The thing nobody tells you about raising money is that the first no is the useful one.", G, claim=True, contrast=1,
             topic_peak=True, answer_of=6),
        Line("It nearly broke us, honestly.", G, emotion=0.8, turn_start=False, pause_after=1.0),
        Line("But it also told us exactly what was missing.", G, turn_start=False),
        Line("yeah", H, backchannel=True, pause_after=0.3),
        Line("We rewrote the whole pitch in a weekend.", G, pause_after=1.8),
        Line("So what did the second pitch say that the first one did not?", H),
        Line("It said, uh, we had a customer who paid twice.", G, fillers=(2,), answer_of=12),
        Line("Right.", H, backchannel=True, pause_after=0.3),
        Line("That single number did more than the whole deck.", G, claim=True),
        Line("Um, let me ask about the team.", H, fillers=(0,)),
        Line("Sure, go ahead.", G),
        Line("How many people were you at that point?", H),
        Line("Three of us in 2019, and then eight by the end of the year.", G, weak_number=True, answer_of=18),
        Line("When did the first hire come in?", H, false_start="And when did the—"),
        Line("About six months later we hired a manufacturing lead first, which everybody said was backwards.", G, answer_of=20,
             dead_air=(10, 2.4)),
        Line("But the product was the bottleneck, not the sales.", G, contrast=1, turn_start=False),
        Line("okay", H, backchannel=True, pause_after=0.3),
        Line("And that decision is the reason we are still here.", G, conclusion=True, claim=True),
        Line("Let me push back on that, because most founders say hire sales first.", H, contrast=1, overlap=0.8),
        Line("They do, and most of them are selling something that does not exist yet.", G, contrast=1),
        Line("That is fair enough.", H),
        Line("What was the hardest month?", H, turn_start=False, hook_floor=0.7, pause_after=0.9),
        Line("March of the second year, we had four weeks of cash and a factory asking for a deposit.", G, answer_of=28),
        Line("What did you do?", H),
        Line("We called every customer and asked them to prepay, and nine of them did.", G, answer_of=30),
        Line("Nine out of how many?", H),
        Line("Twelve, the same twelve from the garage.", G, strong_number=True, answer_of=32, pause_after=1.8),
        Line("That is a remarkable retention number.", H),
        Line("It is the only number we put on the first slide now.", G, claim=True),
        Line("Um, before we wrap, what would you tell someone starting today?", H, fillers=(0,)),
        Line("Ship to real customers before you raise, because the money follows the proof, never the other way around.", G,
             claim=True, contrast=1, imperative=True, answer_of=36),
        Line("Ship before you raise.", H),
        Line("Exactly that, and do it early.", G),
        Line("Where can people find you?", H),
        Line("On the website, and the product is in stores next month.", G, answer_of=40),
        Line("Thank you for coming on, that is the show.", H),
    ]
    return lines


def p2_graph() -> dict:
    speakers = [{"id": "S1", "label": "SPEAKER_00", "name": None, "role_guess": "host", "share": 0.42, "questions": 11, "angle_hint": "A"},
                {"id": "S2", "label": "SPEAKER_01", "name": None, "role_guess": "guest", "share": 0.58, "questions": 0, "angle_hint": "B"}]
    b = _build(p2_script(), speakers={s["id"]: s for s in speakers})
    dur = b.end
    sources = [
        {"key": P2_REC_KEY, "role": "reference_audio", "leaf": "recorder.wav", "path": P2_REC_PATH, "duration": dur,
         "sync_offset_s": 0.0, "has_video": False, "has_audio": True, "dialogue": True,
         "layers": {"speech": "ok", "speakers": "ok", "audio": "ok", "semantic": "ok"}},
        {"key": P2_A_KEY, "role": "angle", "angle": "A", "leaf": "cam_a_host.mp4", "path": P2_A_PATH, "duration": dur + 1.0,
         "sync_offset_s": P2_OFFSETS[P2_A_KEY], "has_video": True, "has_audio": True, "fps": "30/1",
         "layers": {"audio": "ok", "visual": "lazy"},
         "angle_guess": {"kind": "close", "sees": ["S1"], "confidence": 0.9, "by": "own_mic"}},
        {"key": P2_B_KEY, "role": "angle", "angle": "B", "leaf": "cam_b_guest.mp4", "path": P2_B_PATH, "duration": dur + 1.0,
         "sync_offset_s": P2_OFFSETS[P2_B_KEY], "has_video": True, "has_audio": True, "fps": "30/1",
         "layers": {"audio": "ok", "visual": "lazy"},
         "angle_guess": {"kind": "close", "sees": ["S2"], "confidence": 0.9, "by": "own_mic"}},
    ]
    angles = {"reference": P2_REC_KEY, "dialogue": P2_REC_KEY,
              "members": [{"angle": "A", "src_key": P2_A_KEY, "path": P2_A_PATH, "sync_offset_s": P2_OFFSETS[P2_A_KEY],
                           "confidence": 0.9, "sees": ["S1"], "by": "own_mic"},
                          {"angle": "B", "src_key": P2_B_KEY, "path": P2_B_PATH, "sync_offset_s": P2_OFFSETS[P2_B_KEY],
                           "confidence": 0.9, "sees": ["S2"], "by": "own_mic"}]}
    return _graph("raising money for hardware", b, sources=sources, speakers=speakers, reference=P2_REC_KEY,
                  angles=angles, content_type={"guess": "podcast", "confidence": 0.88, "evidence": ["2 speakers", "shares 0.42/0.58"]},
                  music_mood="chill")


# --------------------------------------------------------------------------- truth helpers the tests read

def th_truth(graph: dict) -> dict:
    sents = graph["layers"]["speech"]["sentences"]
    words = graph["layers"]["speech"]["words"]
    return {"quotable": "s_00005", "throwaway": "s_00003", "emphasis": "s_00011", "retake_dup": "s_00009", "retake_of": "s_00008",
            "question": "s_00007", "closing": "s_00014",
            "peak_word": next(w["id"] for w in words if w["sent"] == "s_00011" and w["text"].startswith("earns")),
            "clause_start_word": next(w["id"] for w in words if w["sent"] == "s_00011" and w["text"] == "this"),
            "sentences": {s["id"]: (s["t0"], s["t1"]) for s in sents}}


def p2_truth(graph: dict) -> dict:
    sp = graph["layers"]["speech"]
    backchannel_sents = {"s_00005", "s_00011", "s_00015", "s_00024"}
    bc_turns = [t["id"] for t in sp["turns"] if set(t["sents"]) <= backchannel_sents]
    return {"backchannel_turns": bc_turns, "emotional_sent": "s_00009", "hard_question": "s_00029",
            "dead_air": sp["flags"]["dead_air"][0], "false_start": sp["flags"]["false_starts"][0],
            "turns": sp["turns"], "offsets": {P2_A_PATH: P2_OFFSETS[P2_A_KEY], P2_B_PATH: P2_OFFSETS[P2_B_KEY], P2_REC_PATH: 0.0}}


# --------------------------------------------------------------------------- writing

def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


SYNTH = {"synth_talking_head": th_graph, "synth_two_cam_podcast": p2_graph}
CONTROLS = {"synth_talking_head": {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"},
            "synth_two_cam_podcast": {"content_type": "podcast"},
            "talking_head": {"duration_s": 45.0, "platform": "reels", "ratio": "9:16"},
            "two_cam_podcast": {"content_type": "podcast"}}


def golden_graphs() -> dict[str, dict]:
    """Every graph the planner goldens pin: A's hand-built ones when present,
    plus the two synthetic ones written here."""
    out: dict[str, dict] = {}
    for p in sorted(A_GRAPH_DIR.glob("*.json")) if A_GRAPH_DIR.exists() else []:
        out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    for name, fn in SYNTH.items():
        out[name] = fn()
    return out


def write_graphs() -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    for name, fn in SYNTH.items():
        (GRAPH_DIR / f"{name}.json").write_text(json.dumps(fn(), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")


def write_edps() -> None:
    from video_ai_editor.brain.planner import plan
    EDP_DIR.mkdir(parents=True, exist_ok=True)
    for name, graph in golden_graphs().items():
        edp = plan(graph, CONTROLS.get(name, {}))
        (EDP_DIR / f"{name}.json").write_text(canonical(edp), encoding="utf-8")
        print(f"{name}: {len(edp['decisions'])} decisions → {EDP_DIR / (name + '.json')}")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO / "src"))
    write_graphs()
    write_edps()
