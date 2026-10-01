"""`finish`: the export preset when a platform is named (the audit step
closes every compiled plan on its own, like every recipe plan)."""
from __future__ import annotations

from .. import reasons as R
from .graph_view import Graph
from .types import Ctx, decision

PRESETS = ("reels", "shorts", "tiktok", "story", "ig_feed_1x1", "ig_feed_4x5", "youtube_16x9", "youtube_4k")


def preset_for(platform: str | None, ratio: str | None) -> str | None:
    if platform in PRESETS:
        return platform
    if ratio == "9:16":
        return "reels"
    if ratio == "16:9":
        return "youtube_16x9"
    if ratio == "1:1":
        return "ig_feed_1x1"
    if ratio == "4:5":
        return "ig_feed_4x5"
    return None


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    name = preset_for(ctx.platform, ctx.ratio)
    ctx.state["export_preset"] = name
    if name:
        decisions.append(decision("export_preset", params={"platform": name},
                                  reason=R.reason("control", [], control="Platform", value=name.replace("_", " "))))
    return decisions


__all__ = ["run", "preset_for"]
