"""Scenes and the graph header's guesses (spec §3.3 steps 1–3), split out of
`graph.py`: speech scenes (merge forward while the same speaker continues
and the scene stays ≤ 12 s, split at pauses ≥ 1.2 s), pause scenes (gaps
≥ 1.5 s between speech scenes), one topic this wave, the content-type guess,
the music hint, the header's speaker rows and the angle group's `angles.json`.

Every dict here is in lane C's frozen shapes (`schema.Scene`, `Topic`,
`GraphSpeaker`, `Angles`); nothing is stored that the models forbid.
"""
from __future__ import annotations

from .analysis import semantic as _semantic
from .analysis import speech as _speech

SCENE_MAX_S = 12.0
SCENE_SPLIT_PAUSE_S = 1.2
PAUSE_SCENE_S = 1.5
#: The spec's 1.5 s is a gap between voiced spans at −34 dBFS; this layer's
#: sentence spans come from a VAD ~20 dB more sensitive (their tails are
#: longer), so a planted 1.5 s pause reads 1.35–1.45 s between them. MEASURED
#: on the fixtures: 5 of 5 planted pauses read ≥ 1.35 s, no other gap ≥ 1.2 s.
PAUSE_SLACK_S = 0.15
TOPIC_ID = "t_0001"
SCORE_KEYS = ("hook", "importance", "standalone", "quotable", "emotion", "virality")
HOST_NAME, GUEST_NAME = "Host", "Guest"


def _group_sentences(sentences: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for s in sentences:
        last = groups[-1] if groups else None
        if last and last[0]["spk"] == s["spk"] and s["t0"] - last[-1]["t1"] < SCENE_SPLIT_PAUSE_S \
                and s["t1"] - last[0]["t0"] <= SCENE_MAX_S:
            last.append(s)
        else:
            groups.append([s])
    return groups


def _scene_features(rows: list[dict]) -> dict:
    """The sentence features of a scene, merged (C's `Features` model — the
    delivery numbers live in `semantic` evidence and are recomputed by
    `analysis/delivery.py`, not stored on the scene)."""
    f = [r.get("features") or {} for r in rows]
    return {
        "wpm": int(round(sum(x.get("wpm", 0) for x in f) / len(f))), "fillers": sum(x.get("fillers", 0) for x in f),
        "has_number": any(x.get("has_number") for x in f), "strong_number": any(x.get("strong_number") for x in f),
        "weak_number": any(x.get("weak_number") for x in f), "claim": any(x.get("claim") for x in f),
        "conclusion_marker": any(x.get("conclusion_marker") for x in f),
        "story_marker": any(x.get("story_marker") for x in f), "contrast_words": sum(x.get("contrast_words", 0) for x in f),
        "anaphora_start": bool(f[0].get("anaphora_start")), "len_words": sum(x.get("len_words", 0) for x in f),
    }


def _scene(rows: list[dict], *, scores: dict, active: str | None, noise_db: float | None) -> dict:
    sc = [scores[r["id"]] for r in rows if r["id"] in scores]
    agg = {k: round(max((x[k] for x in sc), default=0.0), 4) for k in SCORE_KEYS}
    agg["standalone"] = scores.get(rows[0]["id"], {}).get("standalone", 0.0)
    ev = {k: sorted({e for x in sc for e in x["evidence"].get(k, [])}) for k in ("hook", "importance", "quotable", "emotion")}
    text = " ".join(r["text"] for r in rows)
    question = any(r["is_question"] for r in rows)
    return {"kind": "speech", "t0": rows[0]["t0"], "t1": rows[-1]["t1"], "spk": rows[0]["spk"], "topic": TOPIC_ID,
            "sents": [r["id"] for r in rows], "text": text, "features": _scene_features(rows),
            "shot": {"angles": {}, "active_angle": active, "motion": 0.0, "face_lost_frac": 0.0},
            "quality": {"noise_db": noise_db, "clipping": 0.0},
            "scores": agg, "evidence": {k: v for k, v in ev.items() if v},
            "labels": {"emotion": _semantic.emotion_label(agg["emotion"], text.lower().split()),
                       "function": "question" if question else "statement"}}


def _pause(t0: float, t1: float) -> dict:
    return {"kind": "pause", "t0": t0, "t1": t1, "spk": None, "topic": TOPIC_ID, "sents": [], "text": "",
            "features": None, "scores": None}


def build_scenes(speech: dict | None, semantic: dict | None, audio: dict | None,
                 active_by_speaker: dict[str, str]) -> list[dict]:
    """Speech scenes then pause scenes, in time order, ids `sc_0001…`."""
    if not speech or not semantic or not speech.get("sentences"):
        return []
    scores = semantic.get("scores") or {}
    noise = (audio or {}).get("noise_floor_db")
    scenes = [_scene(g, scores=scores, active=active_by_speaker.get(g[0]["spk"]), noise_db=noise)
              for g in _group_sentences(speech["sentences"])]
    pauses = [_pause(a["t1"], b["t0"]) for a, b in zip(scenes, scenes[1:]) if b["t0"] - a["t1"] >= PAUSE_SCENE_S - PAUSE_SLACK_S]
    out = sorted(scenes + pauses, key=lambda s: (s["t0"], s["kind"] == "pause"))
    for i, sc in enumerate(out):
        sc["id"] = f"sc_{i + 1:04d}"
    return out


def classify_content(speakers: list[dict], sentences: list[dict], n_angles: int, has_speech: bool) -> dict:
    """A first guess for the header (`classify` proper is the planner's)."""
    if not has_speech:
        return {"guess": "unknown", "confidence": 0.0, "evidence": ["no transcript"]}
    n = len(speakers)
    ev = [f"{n} speaker{'s' if n != 1 else ''}"] + ([f"{n_angles} angles"] if n_angles > 1 else [])
    if n <= 1:
        return {"guess": "talking_head", "confidence": 0.8, "evidence": ev}
    host = next((s for s in speakers if s["role_guess"] == "host"), None)
    total_q = sum(s["questions"] for s in speakers)
    if host is not None:
        n_sent = sum(1 for s in sentences if s["spk"] == host["id"]) or 1
        ratio = host["questions"] / n_sent
        ev.append(f"question ratio {ratio:.2f} on {host['id']}")
        if ratio >= 0.5 and total_q >= 6:
            return {"guess": "interview", "confidence": 0.7, "evidence": ev}
    return {"guess": "podcast", "confidence": 0.6, "evidence": ev}


def music_hint(semantic: dict | None, guess: str) -> dict | None:
    scores = (semantic or {}).get("scores") or {}
    emo = [v["emotion"] for v in scores.values()]
    if not emo:
        return None
    share = sum(1 for e in emo if e >= 0.5) / len(emo)
    mood = "cinematic" if share >= 0.3 else ("chill" if guess in ("podcast", "interview") else "lofi")
    return {"mood": mood, "energy": round(sum(emo) / len(emo), 3), "evidence": [guess, f"emotion share {share:.2f}"]}


def header_speakers(speakers_layer: dict, speech: dict | None) -> list[dict]:
    """The header's `speakers[]` (C's `GraphSpeaker`): id, label, name
    (Host/Guest by role; a lone speaker is the Host), role, share, questions
    and the angle hint."""
    rows = _speech.speaker_roles(speech, speakers_layer) if speech else [dict(s) for s in speakers_layer["speakers"]]
    lone = len(rows) == 1
    out = []
    for r in rows:
        role = "host" if lone and r.get("role_guess") in (None, "unknown") else r.get("role_guess") or "unknown"
        out.append({"id": r["id"], "label": r.get("label"),
                    "name": HOST_NAME if role == "host" else GUEST_NAME if role == "guest" else None,
                    "role_guess": role, "share": r.get("share"), "questions": r.get("questions"),
                    "angle_hint": r.get("angle_hint")})
    return out


def angles_json(ref: dict, members: list[dict]) -> dict:
    """`angles.json` (C's `Angles`): the group in angle order; the dialogue is
    the recorder when one exists, else the reference angle's own file. With
    no recorder the reference IS camera A: when other cameras exist it is
    listed among the members (offset 0, its own clock), so the primary angle
    is the first camera and not whichever camera was synced against it."""
    rows = [{"angle": m["angle"], "src_key": m["key"], "path": m["path"],
             "sync_offset_s": m["sync_offset_s"], "confidence": m["sync"]["confidence"],
             "sees": m["sees"], "by": m["sync"]["engine"]} for m in members]
    if members and ref.get("role") == "angle" and ref.get("has_video"):
        rows.append({"angle": ref.get("angle") or "A", "src_key": ref["key"], "path": ref["path"], "sync_offset_s": 0.0,
                     "confidence": 1.0, "sees": list(ref.get("sees") or []), "by": "reference"})
    return {"reference": ref["key"], "dialogue": ref["key"], "members": sorted(rows, key=lambda r: str(r["angle"]))}


__all__ = ["SCENE_MAX_S", "PAUSE_SCENE_S", "build_scenes", "classify_content", "music_hint", "header_speakers",
           "angles_json"]
