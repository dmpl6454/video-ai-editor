"""The Hook Engine (spec §4.2): nine axes per candidate sentence, a number
counted ONCE across the stack, candidacy before scoring, flat delivery
penalised. The pass ranks; `story.py` decides whether the top scene opens
the reel. The ranking lands in `ctx.state["hooks"]` as
`[(score, sent_id, axes_named)]` best first.
"""
from __future__ import annotations

import re

from .graph_view import CONJUNCTIONS, CONTRAST, IMPERATIVES, PRONOUNS, SUPERLATIVE, Graph, tokens
from .types import Ctx

HOOK_MIN = 0.5
MAX_WORDS = 20
STANDALONE_MIN = 0.6
DELIVERY_MIN = 0.3
FIRST_SECONDS = 3.0
_VALUE = re.compile(r"\bhow to\b|\bthe trick\b|\bthe secret\b|\bhere is how\b", re.I)


def standalone(g: Graph, s: dict, prev: dict | None, *, adjacent: bool) -> float:
    """`1 − anaphora_start − 0.5·conjunction_start − 0.3·(answer lacking the
    question's noun)`, recomputed against `prev` when the join is not
    source-adjacent (the antecedent tests read the NEW predecessor)."""
    first = g.first_token(s)
    score = 1.0
    anaphora = bool(g.feature(s, "anaphora_start")) or first in PRONOUNS
    conj = first in CONJUNCTIONS
    if anaphora and not adjacent:
        score -= 1.0
    elif anaphora and prev is None:
        score -= 1.0
    if conj and not adjacent:
        score -= 0.5
    if s.get("answer_of"):
        q = g.sentence(str(s["answer_of"]))
        if q is not None and (prev is None or prev["id"] != q["id"]):
            nouns = {t for t in tokens(q.get("text", "")) if len(t) > 3}
            head = set(tokens(s.get("text", ""))[:3])
            if nouns and not (nouns & head):
                score -= 0.3
    return max(0.0, round(score, 4))


def _delivery(g: Graph, s: dict) -> float:
    return max(float(g.scores(s["id"]).get("emotion") or 0.0), min(1.0, max(0.0, g.rms_z(s))))


delivery = _delivery


def candidate(g: Graph, s: dict) -> bool:
    if float(g.scores(s["id"]).get("hook") or 0.0) < HOOK_MIN:
        return False
    if g.n_words(s) > MAX_WORDS or s["t0"] < FIRST_SECONDS:
        return False
    has_contrast = int(g.feature(s, "contrast_words", 0) or 0) > 0 or bool(set(tokens(s.get("text", ""))) & CONTRAST)
    imperative = g.first_token(s) in IMPERATIVES
    if not (g.feature(s, "claim") or has_contrast or s.get("is_question") or imperative):
        return False
    if float(g.scores(s["id"]).get("standalone") or standalone(g, s, None, adjacent=True)) < STANDALONE_MIN:
        return False
    return _delivery(g, s) >= DELIVERY_MIN or has_contrast


def axes(g: Graph, s: dict, prev: dict | None) -> dict[str, float]:
    sc = g.scores(s["id"])
    toks = set(tokens(s.get("text", "")))
    strong = bool(g.feature(s, "strong_number"))
    paired = strong and bool(SUPERLATIVE.search(s.get("text", "")))
    has_contrast = int(g.feature(s, "contrast_words", 0) or 0) > 0 or bool(toks & CONTRAST)
    arousal = float(sc.get("emotion") or 0.0)
    prev_z = g.rms_z(prev) if prev is not None else 0.0
    guest = any(sp.get("id") == s.get("spk") and sp.get("role_guess") == "guest" for sp in g.speakers)
    claim = bool(g.feature(s, "claim"))
    n = g.n_words(s)
    return {
        "curiosity": 1.0 if (s.get("is_question") or has_contrast) else 0.0,
        "surprise": max(1.0 if paired else 0.0, 1.0 if toks & {"nobody", "never"} else 0.0,
                        1.0 if g.rms_z(s) - prev_z >= 1.0 else 0.0),
        "emotion": min(1.0, arousal),
        "authority": min(1.0, 0.5 * claim + 0.3 * (strong and not paired) + 0.2 * guest),
        "conflict": 1.0 if toks & {"but", "wrong", "disagree", "no"} else 0.0,
        "value": 1.0 if (_VALUE.search(s.get("text", "")) or g.first_token(s) in IMPERATIVES) else 0.0,
        "question": 1.0 if s.get("is_question") else 0.0,
        "strong_statement": 1.0 if (n <= 12 and claim and g.feature(s, "conclusion_marker")) else (0.6 if n <= 12 and claim else 0.0),
        "visual_impact": 0.0,
    }


def score(g: Graph, s: dict, prev: dict | None) -> tuple[float, list[str]]:
    ax = axes(g, s, prev)
    top = sorted(ax.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    sa = float(g.scores(s["id"]).get("standalone") or standalone(g, s, prev, adjacent=True))
    value = round(sum(v for _, v in top) / 4.0 * sa, 4)
    return value, [k for k, v in top if v > 0]


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    ranked: list[tuple[float, str, list[str]]] = []
    prev: dict | None = None
    for s in g.sentences:
        if candidate(g, s):
            v, names = score(g, s, prev)
            ranked.append((v, s["id"], names))
        prev = s
    ranked.sort(key=lambda r: (-round(r[0], 6), g.sentence(r[1])["t0"], r[1]))
    ctx.state["hooks"] = ranked
    return decisions


__all__ = ["run", "candidate", "axes", "score", "standalone", "delivery"]
