"""`classify` (spec §4.0): the content type and the target.

Rules: one speaker → `talking_head`; two with a question ratio ≥ 0.25 on
one and an answer share ≥ 0.6 on the other → `interview`; 2-4 with talk
shares within 0.35-0.65 → `podcast`. `controls.content_type` overrides. The
target is `reel` when a duration ≤ 90 s is asked or the platform is
vertical, else `episode`. This wave edits an interview as a podcast
(chronology, no cold open) and says so in `deferred[]`.
"""
from __future__ import annotations

from .. import energy as E
from .. import reasons as R
from . import select
from .graph_view import Graph
from .types import Ctx

KNOWN = ("talking_head", "interview", "podcast")


def _rules(g: Graph) -> tuple[str, float]:
    speakers = g.speakers
    if len(speakers) <= 1:
        return "talking_head", 0.9
    shares = [float(s.get("share") or 0.0) for s in speakers]
    questions = [int(s.get("questions") or 0) for s in speakers]
    total_q = sum(questions) or 1
    if len(speakers) == 2:
        q_ratio = max(questions) / total_q
        answer_share = max(shares)
        if q_ratio >= 0.25 and answer_share >= 0.6 and questions.index(max(questions)) != shares.index(max(shares)):
            return "interview", 0.8
    if 2 <= len(speakers) <= 4 and all(0.35 <= s <= 0.65 for s in shares):
        return "podcast", 0.85
    guess, conf = g.content_type_guess
    return (guess if guess in KNOWN else "podcast"), min(conf, 0.6)


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    R.bind(g)                       # the reasons this plan writes look speakers, times and quotes up in `g`
    select.resolve_retakes(g)       # one answer to "which take stays" (the last), read by every pass after this
    override = ctx.controls.get("content_type")
    if override in KNOWN:
        ctx.project_type = str(override)
    else:
        guess, conf = _rules(g)
        graph_guess, graph_conf = g.content_type_guess
        if graph_guess in KNOWN and graph_conf >= 0.6 and conf < 0.6:
            guess = graph_guess
        ctx.project_type = guess
    asked = ctx.controls.get("duration_s")
    ctx.target = "reel" if (asked is not None and float(asked) <= E.REEL_MAX_S) or ctx.vertical else "episode"
    if ctx.project_type == "interview":
        ctx.defer("interview mode", "Q/A surfacing and weak-question removal arrive next wave; edited as a podcast")
    return decisions


__all__ = ["run", "KNOWN"]
