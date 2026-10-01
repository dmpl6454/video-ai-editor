"""Reframe, this wave (spec §4.5 canvas only): when the platform is vertical
and the canvas is not, the literal `auto_reframe(subject_track=false)` +
`set_clip_fit(cover)` pair — a canvas change, no re-encode, no pans (the
dead-zone follower is EB2).
"""
from __future__ import annotations

from .. import reasons as R
from .graph_view import Graph
from .types import Ctx, decision

PLATFORM_RATIO = {"reels": "9:16", "tiktok": "9:16", "shorts": "9:16", "story": "9:16", "youtube_16x9": "16:9",
                  "youtube_4k": "16:9", "ig_feed_1x1": "1:1", "ig_feed_4x5": "4:5"}


def _canvas_ratio(g: Graph) -> str:
    w, h = g.canvas
    if h > w:
        return "9:16" if h / max(1, w) > 1.6 else "4:5"
    return "1:1" if w == h else "16:9"


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    ratio = ctx.ratio or PLATFORM_RATIO.get(ctx.platform or "")
    if ratio is None or ratio == _canvas_ratio(g):
        ctx.state["reframe"] = None
        return decisions
    ctx.state["reframe"] = ratio
    if ratio == "9:16":
        ctx.defer("face-follow reframe pans", "next wave; centred crop")
    decisions.append(decision("reframe", params={"ratio": ratio, "subject_track": False},
                              reason=R.reason("control", [], control="Reframe", value=ratio, platform=ctx.platform or "")))
    return decisions


__all__ = ["run", "PLATFORM_RATIO"]
