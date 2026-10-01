"""The dialogue lane (spec §4.6.1): ONE decision, emitted last so it sees
every structural decision — the recorder when a `reference_audio` source
exists, else the primary angle's own file (a single camera lays a1 too,
settled decision §15.2-13). Compiled as the LAST stage-2 step
(`sync_dialogue_lane`), which rebuilds lane a1 from the final v1 layout,
mutes the camera microphones and fades every internal seam 5 ms.
"""
from __future__ import annotations

from .. import energy as E
from .. import reasons as R
from .graph_view import Graph
from .types import Ctx, decision


def seam_count(ctx: Ctx) -> int:
    n = len([c for c in ctx.state.get("cuts") or [] if not c.get("head")]) + (ctx.state.get("camera") or {}).get("switches", 0)   # the opening's removal is no seam
    if ctx.state.get("open_on"):
        n += 2
    if ctx.state.get("kept"):
        n += max(0, len(ctx.state["kept"]) - 1)
    return n


def offsets(g: Graph, ctx: Ctx) -> dict[str, float]:
    out = {str(m["src_key"]): float(m.get("sync_offset_s") or 0.0) for m in g.members}
    out.setdefault(ctx.dialogue, g.offset(ctx.dialogue))
    return dict(sorted(out.items()))


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    if str(ctx.controls.get("dialogue") or "") == "camera_mics" or (
            ctx.controls.get("scope") == "cleanup" and len(g.members) < 2 and ctx.dialogue == ctx.primary):
        ctx.state["dialogue"] = None
        return decisions
    offs = offsets(g, ctx)
    n = seam_count(ctx)
    ctx.state["dialogue"] = {"src": ctx.dialogue, "lane": "a1", "offsets": offs, "seams": n}
    decisions.append(decision(
        "dialogue", params={"src": ctx.dialogue, "lane": "a1", "offsets": offs, "seam_fade_s": E.SEAM_FADE_S,
                            "mute_camera_mics": True},
        reason=R.reason("dialogue_lane", [ctx.dialogue], leaf=g.leaf_of(ctx.dialogue), n=n)))
    return decisions


__all__ = ["run", "offsets", "seam_count"]
