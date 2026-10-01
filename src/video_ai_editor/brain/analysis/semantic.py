"""The `semantic` layer: every §3.4 score as a pure formula over the speech
and audio layers, with an `evidence` list per score, plus the Gateway's
`rank_moments` over the top quartile by heuristic hook, blended
`0.6 · model + 0.4 · heuristic` and frozen with provenance.

Weights are the named constants below (one docstring line per term). The
goldens pin top-k behaviour and monotonicity, not the exact numbers.

Determinism: a model's answer is written as an annotation keyed by the
sentence id and the prompt hash (the prompt carries every ranked sentence's
text, so a changed sentence changes the hash) and is never re-asked while
both are unchanged (`previous=`); with no gateway, or a gateway that fails,
the layer is the heuristic one byte for byte apart from its `budget` block
(which the graph digest excludes).

Shape: lane C's `schema.SemanticLayer` — `{params, scores, annotations,
topics, budget}`, scores `{hook, importance, standalone, quotable, emotion,
virality, humour, evidence}`, annotations `{sent, by, model, task,
prompt_hash, at, hook}`, budget `{calls, spent_s, partial_from}`. A layer is
`partial` exactly when `budget.partial_from` is set (`layer_status`); who
scored a sentence is its annotation's `by`, else `recipes`. The delivery
inputs are computed by `delivery.sentence_inputs`, not read from the speech
layer.
"""
from __future__ import annotations

import json
import math
import time

import logging

from . import ANALYSIS_VERSION
from .delivery import sentence_inputs
from .speech import standalone_score

_log = logging.getLogger(__name__)

#: rms_z at which the loudness term saturates (`rms_z⁺ / 2`, clipped 0..1 — the
#: convention lane A's hand-built goldens use where the spec only writes ⁺).
RMS_Z_FULL = 2.0
#: pitch range (semitones) that reads as full arousal.
PITCH_RANGE_FULL_ST = 12.0
#: |stretch − 1| that reads as full arousal (the goldens' 0.5).
STRETCH_DEV_FULL = 0.5
#: flat delivery: a candidate below this with no contrast word is scaled by FLAT_SCALE.
FLAT_DELIVERY = 0.3
FLAT_SCALE = 0.7
LONG_SENTENCE_WORDS = 20
QUOTABLE_MAX_WORDS = 18
MODEL_BLEND = 0.6
TOP_QUARTILE_MIN = 2
POSITIVE = frozenset({"love", "great", "beautiful", "stunning", "wonderful", "happy", "thank", "thanks",
                      "amazing", "perfect", "glad"})
NEGATIVE = frozenset({"useless", "never", "worst", "afraid", "hate", "wrong", "mistake", "terrible", "awful"})

W_IMPORTANCE = {"answer_len": 0.30, "claim": 0.22, "strong_number": 0.08, "topic_peak": 0.15, "conclusion": 0.10,
                "rms": 0.15, "repeat": -0.25, "filler_rate": -0.20, "false_start": -0.20}
W_HOOK = {"question": 0.25, "claim_or_conclusion": 0.20, "strong_number": 0.15, "delivery": 0.15,
          "contrast": 0.10, "standalone": 0.10, "weak_start": -0.30, "anaphora": -0.30, "long": -0.20}
W_EMOTION = {"rms": 0.5, "pitch": 0.3, "stretch": 0.2}
W_VIRALITY = {"hook": 0.30, "importance": 0.25, "humour": 0.15, "emotion": 0.10, "quotable": 0.10, "standalone": 0.10}
W_QUOTABLE_SCALED = {"claim_or_number": 0.4, "complete": 0.25, "short": 0.15, "anaphora": -0.3}


def _clip(v: float) -> float:
    return round(max(0.0, min(1.0, v)), 4)


def rms_pos(f: dict) -> float:
    return max(0.0, min(1.0, float(f.get("rms_z", 0.0)) / RMS_Z_FULL))


def emotion_score(f: dict) -> tuple[float, list[str]]:
    rms = rms_pos(f)
    pitch = max(0.0, min(1.0, float(f.get("pitch_range_st", 0.0)) / PITCH_RANGE_FULL_ST))
    stretch = max(0.0, min(1.0, abs(float(f.get("stretch", 1.0)) - 1.0) / STRETCH_DEV_FULL))
    ev = [k for k, v in (("rms_z", rms), ("pitch_range", pitch), ("stretch", stretch)) if v >= 0.3]
    return _clip(W_EMOTION["rms"] * rms + W_EMOTION["pitch"] * pitch + W_EMOTION["stretch"] * stretch), ev


def emotion_label(arousal: float, tokens: list[str]) -> str:
    pos = any(t in POSITIVE for t in tokens)
    neg = any(t in NEGATIVE for t in tokens)
    if arousal >= 0.5:
        return "tense" if neg and not pos else "emphatic"
    return "warm" if pos else "calm"


def hook_score(s: dict, delivery: float, standalone: float) -> tuple[float, list[str]]:
    f = s["features"]
    candidate = f["claim"] or f["contrast_words"] > 0 or s["is_question"] or f.get("imperative", False)
    if not candidate:
        return 0.0, ["not_candidate"]
    terms = {"question": float(s["is_question"]), "claim_or_conclusion": float(f["claim"] or f["conclusion_marker"]),
             "strong_number": float(f["strong_number"]), "delivery": delivery,
             "contrast": float(min(1, f["contrast_words"])), "standalone": standalone,
             "weak_start": float(s.get("weak_start", f.get("weak_start", False))),
             "anaphora": float(f["anaphora_start"]),
             "long": float(f["len_words"] > LONG_SENTENCE_WORDS)}
    score = sum(W_HOOK[k] * v for k, v in terms.items())
    ev = [k for k, v in terms.items() if v > 0 and W_HOOK[k] > 0]
    if delivery < FLAT_DELIVERY and f["contrast_words"] == 0:
        score *= FLAT_SCALE
        ev.append("flat_delivery")
    return _clip(score), ev


def importance_score(f: dict) -> tuple[float, list[str]]:
    terms = {"answer_len": float(f.get("answer_len_norm", 0.0)), "claim": float(f["claim"]),
             "strong_number": float(f["strong_number"]), "topic_peak": float(f.get("topic_peak", False)),
             "conclusion": float(f["conclusion_marker"]), "rms": rms_pos(f), "repeat": float(f.get("repeat", False)),
             "filler_rate": float(f.get("filler_rate", 0.0)), "false_start": float(f.get("false_start", False))}
    score = sum(W_IMPORTANCE[k] * v for k, v in terms.items())
    return _clip(score), [k for k, v in terms.items() if v > 0]


def quotable_score(s: dict) -> tuple[float, list[str]]:
    f = s["features"]
    short = f["len_words"] <= QUOTABLE_MAX_WORDS
    strong = f["claim"] or f["strong_number"]
    if short and s["complete"] and strong and not f["anaphora_start"]:
        return 1.0, ["short", "complete", "claim" if f["claim"] else "strong_number"]
    v = (W_QUOTABLE_SCALED["claim_or_number"] * strong + W_QUOTABLE_SCALED["complete"] * s["complete"]
         + W_QUOTABLE_SCALED["short"] * short + W_QUOTABLE_SCALED["anaphora"] * f["anaphora_start"])
    return min(0.95, _clip(v)), [k for k, ok in (("claim_or_number", strong), ("complete", s["complete"]),
                                                  ("short", short)) if ok]


def score_sentence(s: dict, *, question: dict | None = None) -> dict:
    """All six scores of one sentence dict (speech-layer shape) with evidence."""
    f = s["features"]
    standalone = standalone_score(s, question)
    emotion, ev_emotion = emotion_score(f)
    delivery = max(emotion, rms_pos(f))
    hook, ev_hook = hook_score(s, delivery, standalone)
    importance, ev_importance = importance_score(f)
    quotable, ev_quotable = quotable_score(s)
    humour = 0.0
    virality = _clip(W_VIRALITY["hook"] * hook + W_VIRALITY["importance"] * importance + W_VIRALITY["humour"] * humour
                     + W_VIRALITY["emotion"] * emotion + W_VIRALITY["quotable"] * quotable
                     + W_VIRALITY["standalone"] * standalone)
    return {"hook": hook, "importance": importance, "standalone": standalone, "quotable": quotable,
            "emotion": emotion, "virality": virality, "humour": humour,
            "evidence": {"hook": ev_hook, "importance": ev_importance, "quotable": ev_quotable,
                         "emotion": ev_emotion}}


def top_quartile(sents: list[dict], scores: dict[str, dict]) -> list[dict]:
    cands = [s for s in sents if scores[s["id"]]["hook"] > 0]
    if len(cands) < TOP_QUARTILE_MIN:
        return []
    n = max(TOP_QUARTILE_MIN, math.ceil(len(cands) / 4))
    return sorted(cands, key=lambda s: (-scores[s["id"]]["hook"], s["t0"]))[:n]


def _reuse(previous: dict | None, top: list[dict], prompt_hash: str) -> list[dict] | None:
    """The previous layer's annotations for `top` when every sentence is
    covered under the same prompt hash; else None."""
    if not previous:
        return None
    have = {(a["sent"], a.get("prompt_hash")): a for a in previous.get("annotations") or []}
    found = [have.get((s["id"], prompt_hash)) for s in top]
    return None if any(a is None for a in found) else [dict(a) for a in found]


def _annotate(top: list[dict], scores: dict[str, dict], gateway, budget_s: float, previous: dict | None) -> dict:
    payload_sents = [{"id": s["id"], "t0": s["t0"], "t1": s["t1"], "spk": s["spk"], "text": s["text"],
                      "hook": scores[s["id"]]["hook"]} for s in top]
    prompt_hash = gateway.prompt_hash(payload_sents)
    cached = _reuse(previous, top, prompt_hash)
    if cached is not None:
        return {"annotations": cached, "calls": 0, "spent_s": 0.0, "partial_from": None, "notes": ["cached"]}
    res = gateway.rank_moments(payload_sents, budget_s=budget_s)
    now = int(time.time())
    ann = [{"sent": s["id"], "by": res.provenance["by"], "model": res.provenance.get("model"),
            "task": "rank_moments", "prompt_hash": prompt_hash, "at": now, "hook": res.scores[s["id"]]}
           for s in top if s["id"] in res.scores]
    partial_from = min((s["t0"] for s in top if s["id"] not in res.scores), default=None) if res.partial else None
    return {"annotations": ann, "calls": res.calls, "spent_s": round(res.spent_s, 2), "partial_from": partial_from,
            "notes": list(res.notes)}


def _annotate_or_degrade(top: list[dict], scores: dict[str, dict], gateway, budget_s: float,
                         previous: dict | None) -> dict:
    """`_annotate`, but a payload or provider error (a path guard, a crashed helper) never fails the
    layer: the sentences stay on their heuristic scores and the layer is `partial`, with the reason in
    the notes — the same honesty as a model that ran out of budget."""
    try:
        return _annotate(top, scores, gateway, budget_s, previous)
    except Exception as e:  # noqa: BLE001 — the model is optional; the heuristic scores are complete
        _log.warning("rank_moments skipped: %s: %s", type(e).__name__, e)
        return {"annotations": [], "calls": 0, "spent_s": 0.0, "partial_from": min(s["t0"] for s in top),
                "notes": [f"model step skipped: {type(e).__name__}"]}


def _blend(scores: dict[str, dict], annotations: list[dict]) -> None:
    for a in annotations:
        sc = scores.get(a["sent"])
        if sc is None or a.get("hook") is None:
            continue
        sc["hook"] = round(MODEL_BLEND * float(a["hook"]) + (1 - MODEL_BLEND) * sc["hook"], 4)
        sc["evidence"]["hook"] = [*sc["evidence"]["hook"], f"{a['by']}:rank_moments"]


def _topics(sents: list[dict]) -> list[dict]:
    if not sents:
        return []
    return [{"id": "t_0001", "t0": sents[0]["t0"], "t1": max(sents[-1]["t1"], sents[0]["t0"] + 0.01), "title": None,
             "by": "recipes", "sents": [s["id"] for s in sents]}]


def build_semantic_layer(speech: dict, audio: dict, *, pcm=None, gateway=None, budget_s: float = 60.0,
                         previous: dict | None = None, inputs: dict | None = None) -> dict:
    """Heuristic scores for every sentence; `rank_moments` over the top
    quartile when a gateway is given. `inputs` = `delivery.sentence_inputs`
    when the caller already has them."""
    sents = speech.get("sentences") or []
    inputs = inputs if inputs is not None else sentence_inputs(speech, audio, pcm)
    rich = [{**s, "features": {**(s.get("features") or {}), **inputs[s["id"]]}} for s in sents]
    by_id = {s["id"]: s for s in rich}
    scores = {s["id"]: score_sentence(s, question=by_id.get(s.get("answer_of") or "")) for s in rich}
    budget = {"calls": 0, "spent_s": 0.0, "partial_from": None}
    annotations: list[dict] = []
    top = top_quartile(sents, scores) if gateway is not None else []
    if top:
        res = _annotate_or_degrade(top, scores, gateway, budget_s, previous)
        annotations = res.pop("annotations")
        _log.info("rank_moments: %d of %d answered; %s", len(annotations), len(top), res.pop("notes"))
        budget.update(res)
        _blend(scores, annotations)
    return {"params": {"engine": "heuristic-v1", "analysis_version": ANALYSIS_VERSION}, "scores": scores,
            "annotations": annotations, "topics": _topics(sents), "budget": budget}


RUN_STATE_KEYS = ("budget",)


def layer_status(layer: dict) -> str:
    """`partial` when the model budget ran out before every top sentence was
    ranked (`budget.partial_from`), else `ok`."""
    return "partial" if (layer.get("budget") or {}).get("partial_from") is not None else "ok"


def scored_by(layer: dict) -> dict[str, str]:
    """{sentence id: who set its hook} — the annotation's `by`, else `recipes`."""
    out = {sid: "recipes" for sid in layer.get("scores") or {}}
    for a in layer.get("annotations") or []:
        if a.get("hook") is not None and a["sent"] in out:
            out[a["sent"]] = a["by"]
    return out


def canonical_content(layer: dict) -> bytes:
    """The layer minus its run state (`budget`): what the graph digests, so a
    stubbed model and the heuristic agree byte for byte."""
    return json.dumps({k: v for k, v in layer.items() if k not in RUN_STATE_KEYS}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


__all__ = ["RMS_Z_FULL", "MODEL_BLEND", "score_sentence", "hook_score", "importance_score", "quotable_score",
           "emotion_score", "emotion_label", "top_quartile", "build_semantic_layer", "canonical_content",
           "layer_status", "scored_by", "RUN_STATE_KEYS"]
