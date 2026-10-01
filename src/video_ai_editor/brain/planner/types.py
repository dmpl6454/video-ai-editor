"""The planner's working types: the decision dict in the frozen EDP shape
(EB1 brief "EDP"; lane C's `schema.Decision` validates it on write) and the
pass context `Ctx` every pass reads and writes.

A decision is a plain dict so a pass stays a pure function over JSON-able
data; ids are assigned once, after the passes ran, in the total order
`(kind_rank, ref.src, ref.t0, id)` of spec §4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .graph_view import Graph

KIND_RANK: dict[str, int] = {
    "keep_window": 0, "cut_range": 1, "keep_pause": 2, "open_on": 3, "switch_angle": 4, "punch_in": 5,
    "jump_cut_hide": 6, "captions": 7, "music": 8, "reframe": 9, "dialogue": 10, "export_preset": 11,
}


#: A time this close to a frame boundary (in frames) IS that boundary: times
#: are stored to 4 decimals, so a gridded instant can sit 1e-4 s off it.
GRID_TOL_FRAMES = 0.01

#: A cut edge this close to a word's own edge is not inside the word (`brain/checks.DEFAULT_MID_WORD_TOL_S`).
WORD_EDGE_TOL_S = 0.02


def r4(v: float) -> float:
    out = round(float(v), 4)
    return 0.0 if out == 0 else out


def ref(src: str, t0: float, t1: float) -> dict[str, Any]:
    """`{src, t0, t1}` in `src`'s OWN file seconds (the source key)."""
    return {"src": str(src), "t0": r4(t0), "t1": r4(t1)}


def decision(kind: str, *, reason: dict[str, Any], ref: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None, score: float = 1.0, confidence: float = 1.0,
             optional: bool = False, by: str = "recipes") -> dict[str, Any]:
    if kind not in KIND_RANK:
        raise KeyError(f"unknown decision kind {kind!r}")
    return {"id": "", "kind": kind, "ref": ref, "params": dict(params or {}), "reason": reason,
            "score": r4(max(0.0, min(1.0, score))), "confidence": r4(max(0.0, min(1.0, confidence))),
            "optional": bool(optional), "by": by, "produced": None}


def sort_key(d: dict[str, Any]) -> tuple:
    r = d.get("ref") or {}
    return (KIND_RANK[d["kind"]], str(r.get("src", "")), float(r.get("t0", -1.0)), float(r.get("t1", -1.0)),
            str(d["reason"]["code"]), str(d["params"]))


def number(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Total order, then `k_0001…` in that order."""
    out = sorted(decisions, key=sort_key)
    for i, d in enumerate(out, 1):
        d["id"] = f"k_{i:04d}"
    return out


@dataclass
class Ctx:
    """What a pass may know beyond the graph. `state` carries results
    between passes (cuts → seams → camera → emphasis …) and nothing else."""
    graph: Graph
    controls: dict[str, Any]
    style: str
    energy: dict[str, Any]               # brain/energy.knobs(level)
    project_type: str = "talking_head"
    target: str = "episode"               # reel | episode
    primary: str = ""                     # the v1 source key
    dialogue: str = ""                    # the dialogue source key
    fps: float = 30.0                     # the PROJECT's rate (Graph.fps: the session canvas when known, else the analysis's)
    asked_s: float | None = None
    platform: str | None = None
    ratio: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    deferred: list[dict[str, str]] = field(default_factory=list)

    @property
    def level(self) -> int:
        return int(self.energy["energy"])

    @property
    def vertical(self) -> bool:
        return self.ratio == "9:16" or (self.platform or "") in ("reels", "tiktok", "shorts", "story")

    @property
    def reel(self) -> bool:
        return self.target == "reel"

    def defer(self, asked: str, why: str) -> None:
        if not any(d["asked"] == asked for d in self.deferred):
            self.deferred.append({"asked": asked, "why": why})

    def frame(self) -> float:
        return 1.0 / max(1.0, self._rate())

    # The tools round a removal OUTWARD on the frame grid (`floor` its start,
    # `ceil` its end — dispatch._cut_source_ranges), which would move an edge
    # the planner put on a trough by up to a frame INTO the kept sound and
    # make the programme shorter than planned. So the planner puts every
    # removal's edges on the grid itself, rounded INWARD (the removal only
    # ever shrinks), and writes them so that 4-decimal rounding cannot push
    # the tool's floor/ceil onto the neighbouring frame.
    def _rate(self) -> float:
        """The project's EXACT rate (29.97 is 30000/1001 — `edl/timebase`)."""
        from ...edl import timebase as _tb
        return float(_tb.rate_of(self.fps))

    def removal_start(self, t: float) -> float:
        """The first frame boundary at or after `t`, written ≥ the exact instant."""
        rate = self._rate()
        n = math.ceil(t * rate - GRID_TOL_FRAMES)
        return math.ceil(n / rate * 1e4 - 1e-6) / 1e4

    def removal_end(self, t: float) -> float:
        """The last frame boundary at or before `t`, written ≤ the exact instant."""
        rate = self._rate()
        n = math.floor(t * rate + GRID_TOL_FRAMES)
        return math.floor(n / rate * 1e4 + 1e-6) / 1e4

    def word_safe(self, f: float, *, start: bool, whole: Callable[[dict], bool]) -> float:
        """A removal edge `f` (the primary file's seconds, already on the frame grid) that lies strictly inside a
        word moves, on the grid, to the word's far side: a word the removal does not mean to take (`whole(w)`
        false) stays whole — the removal's start moves after it, its end before it — and one it does mean to take
        goes whole. The edge stays where it was when the move would land inside another word: the safety net's
        `no_cut_mid_word` then says so (closer N-06: a frame grid under words that are not on it left the tail of
        an 'Um,' and refused the reel)."""
        g = self.graph
        w = g.word_straddling(g.to_ref(f, self.primary))
        if w is None:
            return f
        later = start != bool(whole(w))
        for after in (later, not later):                       # the side the removal means first, else the other one
            edge = g.to_file(float(w["t1"]) if after else float(w["t0"]), self.primary)
            moved = self.removal_start(edge) if after else self.removal_end(edge)
            if g.word_straddling(g.to_ref(moved, self.primary)) is None:
                return moved
        return f


__all__ = ["KIND_RANK", "r4", "ref", "decision", "sort_key", "number", "Ctx"]
