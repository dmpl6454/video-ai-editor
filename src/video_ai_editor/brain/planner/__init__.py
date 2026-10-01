"""The Editor Brain planner (spec §4): pure passes over a Content Graph → a
frozen Edit Decision Plan (EDP), the shape lane C's `brain/schema.EDP`
validates and `brain/compile.py` turns into a Prompt-Editor `Plan`.

    plan(graph, controls, style=None, seed=0) -> dict

is deterministic: the same graph, controls and style give the same bytes
(tests/test_brain_planner_goldens.py pins them), so `created` defaults to a
constant and the caller (`agent/prompt/brain_expanders.py`) passes the
clock; the EDP id is a content hash, so a re-plan of the same footage names
the same file. Pass order this wave:

    classify → tighten → hooks → story → camera → emphasis → captions → music → reframe → dialogue → finish

(`seams.py` is inside `tighten`; `dialogue` runs after every structural
pass so its seam count and offsets are final; `select.py` is `story`'s
window scorer.) Every threshold comes from `brain/energy.py`; every
decision carries `{code, facts (graph ids), text}` from `brain/reasons.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .. import PLANNER_VERSION
from .. import energy as E
from . import camera, captions, classify, dialogue, emphasis, finish, hooks, music, reframe, story, tighten
from .graph_view import Graph
from .types import Ctx, number, r4

CREATED_DEFAULT = "1970-01-01T00:00:00Z"
STYLES = ("viral_reel", "premium_podcast", "clean_professional", "luxury")
PASSES = (classify, tighten, hooks, story, camera, emphasis, captions, music, reframe, dialogue, finish)


def normalize_controls(controls: dict[str, Any] | None) -> dict[str, Any]:
    c = dict(controls or {})
    energy = int(c.get("energy") or 5)
    dur = c.get("duration_s")
    return {
        "content_type": str(c.get("content_type") or "auto"),
        "energy": max(1, min(10, energy)),
        "captions": str(c.get("captions") or "auto"),
        "music": str(c.get("music") or ("subtle" if _is_reel(c) else "off")),
        "duration_s": r4(float(dur)) if dur else None,
        "platform": c.get("platform") or None,
        "ratio": c.get("ratio") or None,
        "count": max(1, int(c.get("count") or 1)),
        **({"scope": "cleanup"} if c.get("scope") == "cleanup" else {}),
        **({"mood": c["mood"]} if c.get("mood") else {}),
        **({"dialogue": c["dialogue"]} if c.get("dialogue") else {}),
    }


def _is_reel(c: dict[str, Any]) -> bool:
    dur = c.get("duration_s")
    vertical = c.get("ratio") == "9:16" or (c.get("platform") or "") in ("reels", "tiktok", "shorts", "story")
    return (dur is not None and float(dur) <= E.REEL_MAX_S) or vertical


def default_style(controls: dict[str, Any]) -> str:
    return "viral_reel" if _is_reel(controls) else "premium_podcast"


def make_ctx(graph: Graph | dict, controls: dict[str, Any] | None, style: str | None = None) -> Ctx:
    g = graph if isinstance(graph, Graph) else Graph(graph)
    c = normalize_controls(controls)
    st = style if style in STYLES else default_style(c)
    return Ctx(graph=g, controls=c, style=st, energy=E.knobs(c["energy"]), primary=g.primary, dialogue=g.dialogue_key,
               fps=plan_fps(g, c), asked_s=c["duration_s"], platform=c["platform"], ratio=c["ratio"])


def plan_fps(g: Graph, controls: dict[str, Any]) -> float:
    """The frame rate every removal, key and lead is put on: the PROJECT's, or — when the plan ends in an export
    preset that conforms the project to another rate (a 20 fps project and the reels preset: 30) — that rate.
    The compiled plan conforms the canvas FIRST (`compile._conform_step`), so the cuts land on the frames the
    export has."""
    name = finish.preset_for(controls.get("platform"), controls.get("ratio"))
    if not name:
        return g.fps
    from ...agent.dispatch import conformed_fps
    return float(conformed_fps(g.fps, name))


def _prune_cuts(ctx: Ctx, decisions: list[dict]) -> list[dict]:
    """After the story pass: the `cut_range` decisions are re-issued from
    `ctx.state["cuts"]` (a reel keeps only the removals inside a kept
    window, with their air fitted to the asked length) and a kept pause
    outside every window goes with its window."""
    windows = ctx.state.get("kept") or []
    g = ctx.graph
    if not ctx.reel or not windows:
        return decisions

    def inside(t0: float, t1: float) -> bool:
        return any(t1 > a and t0 < b for a, b, _ in windows)
    out = []
    for d in decisions:
        if d["kind"] == "cut_range":
            continue
        if d["kind"] == "keep_pause":
            r = d["ref"]
            if not inside(g.to_ref(r["t0"], ctx.primary), g.to_ref(r["t1"], ctx.primary)):
                continue
        out.append(d)
    return out + tighten.cut_decisions(g, ctx)


def _estimate(ctx: Ctx, decisions: list[dict]) -> float:
    n_cuts = sum(1 for d in decisions if d["kind"] in ("cut_range", "keep_window"))
    n_sw = sum(1 for d in decisions if d["kind"] == "switch_angle")
    n_keys = sum(len(d["params"].get("keys") or []) for d in decisions if d["kind"] in ("punch_in", "jump_cut_hide"))
    groups = len({d["kind"] for d in decisions if d["kind"] in ("captions", "music", "reframe", "export_preset", "dialogue")})
    return r4(3.0 + 0.05 * n_cuts + 0.05 * n_sw + 0.02 * n_keys + 1.5 * groups)


_AT_TIME = re.compile(r"^(?P<what>[a-z][a-z ]*?) at (?P<tc>\d+(?::\d\d){1,2})$")
#: Left-in-place notes of one kind past this many are one line ("40 pauses …"), not forty.
COLLAPSE_FROM = 4


def collapse_deferred(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """The deferred list with every "<thing> at <time>" kind that repeats (`pause at 0:57`, `possible filler at 3:03`
    with the SAME reason) said once, with its count and first and last time: a 45-minute recording left 40 pauses in
    place and the reply and the Plan tab listed all forty."""
    groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for i, d in enumerate(items):
        m = _AT_TIME.match(str(d.get("asked", "")))
        if m:
            groups.setdefault((m["what"], str(d.get("why", ""))), []).append((i, m["tc"]))
    out: list[dict[str, str]] = []
    said: set[tuple[str, str]] = set()
    for i, d in enumerate(items):
        m = _AT_TIME.match(str(d.get("asked", "")))
        key = (m["what"], str(d.get("why", ""))) if m else None
        rows = groups.get(key) if key else None
        if rows is None or len(rows) < COLLAPSE_FROM:
            out.append(d)
        elif key not in said:
            said.add(key)
            out.append({"asked": f"{len(rows)} {key[0]}s between {rows[0][1]} and {rows[-1][1]}", "why": key[1]})
    return out


def _summary(ctx: Ctx, decisions: list[dict]) -> dict[str, Any]:
    g = ctx.graph
    hook = ctx.state.get("hook")
    hook_sum = None
    kept = ctx.state.get("kept_sents") or []
    opens = hook is not None and (ctx.state.get("open_on") or (kept and kept[0] == hook["id"]))
    if hook is not None and opens:
        h0, h1 = ctx.state["hook_span"]
        hook_sum = {"sent": hook["id"], "src": ctx.primary, "t0": g.to_file(h0, ctx.primary),
                    "t1": g.to_file(h1, ctx.primary), "quote": g.spoken(hook)[:120]}
    return {
        "project_type": ctx.project_type, "target": ctx.target, "duration_s": ctx.state.get("duration_s"),
        "hook": hook_sum, "story": ctx.state.get("story") or [],
        "dialogue": ctx.state.get("dialogue"),
        "camera": ctx.state.get("camera") or {"angles": 1, "switches": 0, "at_cut": 0},
        "pauses_kept": sum(1 for d in decisions if d["kind"] == "keep_pause"),
        "music": ctx.state.get("music"), "captions": ctx.state.get("captions"),
        "estimated_seconds": _estimate(ctx, decisions), "deferred": collapse_deferred(ctx.deferred),
    }


HOOK_CODES = ("hook_strongest_opening", "hook_emphasis")


def _provenance(ctx: Ctx, decisions: list[dict]) -> str | None:
    """Who ranked the moments (spec §0.1 rule 3): the semantic layer's
    frozen annotations name the model that re-ranked the hook candidates.
    The decisions that rest on the hook's rank carry its `by`; the EDP's
    `content_brain` is that model. With no annotation — no model, or one
    that timed out — nothing here changes a byte."""
    ann = [a for a in (ctx.graph.layers.get("semantic") or {}).get("annotations") or []
           if a.get("hook") is not None and a.get("by") not in (None, "recipes")]
    if not ann:
        return None
    by = {str(a["sent"]): str(a["by"]) for a in ann}
    hook = ctx.state.get("hook")
    who = by.get(str(hook["id"])) if hook is not None else None
    for d in decisions:
        if who and d["reason"]["code"] in HOOK_CODES:
            d["by"] = who
    return who or str(ann[0]["by"])


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def edp_id(edp: dict[str, Any]) -> str:
    body = {k: v for k, v in edp.items() if k not in ("id", "created", "compiled", "score")}
    return "d_" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()[:8]


def plan(graph: dict | Graph, controls: dict[str, Any] | None = None, style: str | None = None, seed: int = 0,
         created: str = CREATED_DEFAULT) -> dict[str, Any]:
    """The frozen EDP for `graph` under `controls` and `style` (pure)."""
    ctx = make_ctx(graph, controls, style)
    g = ctx.graph
    decisions: list[dict] = []
    for p in PASSES:
        decisions = p.run(g, ctx, decisions)
        if p is story:
            decisions = _prune_cuts(ctx, decisions)
    content_brain = _provenance(ctx, decisions)
    decisions = number(decisions)
    edp: dict[str, Any] = {
        "version": 1, "id": "", "planner_version": PLANNER_VERSION, "created": created,
        "graph": {"id": g.id, "digest": g.digest},
        "controls": {k: v for k, v in ctx.controls.items() if k in ("content_type", "energy", "captions", "music", "duration_s",
                                                                       "platform", "ratio", "count")},
        "style": ctx.style, "seed": int(seed), "previous": None, "scope": None, "brain": "recipes",
        "content_brain": content_brain,
        "summary": _summary(ctx, decisions), "decisions": decisions, "children": [], "compiled": None, "score": None,
    }
    edp["id"] = edp_id(edp)
    return edp


__all__ = ["PLANNER_VERSION", "CREATED_DEFAULT", "STYLES", "PASSES", "Graph", "Ctx", "plan", "make_ctx",
           "normalize_controls", "default_style", "canonical", "edp_id", "tighten", "story", "hooks", "camera",
           "emphasis", "captions", "music", "reframe", "dialogue", "finish", "classify"]
