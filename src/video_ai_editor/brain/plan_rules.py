"""The validator's Editor Brain rules (EB1; called per step from
`agent/prompt/validate.py`, which keeps only the shape-level part):

* `check_brain_step` — one `$brain:<kind>` sentinel per step, never nested;
  `plan_ref` well-formed (`d_[0-9a-f]{8}`), a decisions file of THIS session
  (`<WORKDIR>/<session_id>/brain/decisions/<did>.json`), planned on the
  session's CURRENT graph — a stale one is refused with "replan" in the
  reason, never run;
* `check_brain_tool_args` — the nested path rule and bounds no JSON schema
  expresses for the three EB1 tools' LITERAL args (spec §5.4): every
  `ranges[].src`, `switches[].src/angle_src`, `sync_dialogue_lane.src` and
  `offsets` key resolves into `facts.allowed_paths` or is a file already
  on the timeline (`facts.timeline_paths`) — offered or refused, never a
  question: these files come from the brain, not a model's guess;
  ≤ 2,000 ranges of ≤ 600 s each, `end > start`; ≤ 600 switches with
  `until_src > at_src`; ≤ 16 offsets, each a finite number; `lane` a track.

A sentinel arg's LITERAL value does not exist yet, so `check_brain_step`
checks the files its decisions DECLARE (`resolve.declared_paths`) against
the same rule; the resolver fills the arg at run time and
`executor.guard_step` runs `check_brain_tool_args` on every RESOLVED dispatch
(SC-05) — a tampered EDP or graph file never reaches a handler.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..agent.prompt.schema import PLAN_REF_ARG, PLAN_REF_PATTERN
from ..agent.tools import CAMERA_SWITCHES_MAX, CUT_RANGE_MAX_S, CUT_RANGES_MAX, OFFSETS_MAX
from .resolve import BRAIN_SENTINELS, SENTINEL_FILLS, has_brain_value, sentinel_kind

#: The tools whose file args may name a file already on the timeline.
BRAIN_TOOLS = frozenset({"cut_source_ranges", "apply_camera_plan", "sync_dialogue_lane"})
#: The tools' own caps (`agent/tools.py`), never a second copy of the number.
MAX_CUT_RANGES = CUT_RANGES_MAX
MAX_CUT_RANGE_S = CUT_RANGE_MAX_S
MAX_CAMERA_SWITCHES = CAMERA_SWITCHES_MAX
MAX_OFFSETS = OFFSETS_MAX

Reasons = list[str]


def session_brain_dir(facts: Any) -> Path | None:
    sid = str(getattr(facts, "session_id", "") or "")
    if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
        return None
    return Path(config.WORKDIR) / sid / "brain"


def check_brain_step(tool: str, args: dict[str, Any], facts: Any, reasons: Reasons) -> None:
    kinds = {k: kind for k, v in args.items() if (kind := sentinel_kind(v)) is not None}
    nested = [k for k, v in args.items() if k not in kinds and has_brain_value(v)]
    if nested:
        reasons.append(f"{tool}: a $brain sentinel may not be nested inside {nested}")
    ref = args.get(PLAN_REF_ARG)
    if not kinds:
        if ref is not None and not (isinstance(ref, str) and re.fullmatch(PLAN_REF_PATTERN, ref)):
            reasons.append(f"{tool}.{PLAN_REF_ARG}: {ref!r} is not a decisions id (d_xxxxxxxx)")
        return
    if len(kinds) > 1:
        reasons.append(f"{tool}: one $brain sentinel per step, got {sorted(kinds)}")
    if not isinstance(ref, str) or not re.fullmatch(PLAN_REF_PATTERN, ref):
        reasons.append(f"{tool}: a $brain step needs {PLAN_REF_ARG} d_xxxxxxxx (got {ref!r})")
        return
    from . import store as _store
    bdir = session_brain_dir(facts)
    edp = None
    if bdir is not None:
        try:
            edp = _store.read_edp(bdir.parent, ref)
        except (OSError, ValueError) as e:
            reasons.append(f"{tool}: {PLAN_REF_ARG} {ref}: {e}")
            return
    if edp is None:
        reasons.append(f"{tool}: {PLAN_REF_ARG} {ref} is not a decisions file of this session")
        return
    current = _store.current_graph_id(bdir.parent)
    if current is None or edp.graph.id != current:
        reasons.append(f"{tool}: {PLAN_REF_ARG} {ref} was planned on graph {edp.graph.id}; the session's current "
                       f"graph is {current or 'none'} — the plan is stale, replan")
        return
    _declared_sources(tool, kinds, bdir, edp, facts, reasons)


def _resolve_path(v: str) -> str | None:
    try:
        return str(Path(v).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def _declared_sources(tool: str, kinds: dict[str, str], bdir: Path, edp: Any, facts: Any, reasons: Reasons) -> None:
    """SC-05: the files the sentinel's decisions name go through the same
    offered-or-on-the-timeline rule as a literal `ranges[].src`, so an EDP or
    graph file naming `/etc/passwd` is refused when the plan is validated."""
    from .resolve import declared_paths
    rules = _Rules(tool, facts, reasons, _resolve_path, set(), lambda _v: False)
    for arg, kind in kinds.items():
        for path in declared_paths(bdir, edp, kind):
            rules.path(f"{arg}[$brain:{kind}]", path)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


class _Rules:
    def __init__(self, tool: str, facts: Any, reasons: Reasons, resolve_path: Callable[[str], str | None],
                 known_tracks: set[str], is_placeholder: Callable[[Any], bool]):
        self.tool, self.facts, self.reasons = tool, facts, reasons
        self.resolve_path, self.known_tracks, self.is_placeholder = resolve_path, known_tracks, is_placeholder

    def path(self, label: str, v: Any) -> None:
        if not isinstance(v, str) or not v.strip():
            self.reasons.append(f"{self.tool}.{label}: path must be a non-empty string")
            return
        resolved = self.resolve_path(v)
        on_timeline = getattr(self.facts, "timeline_paths", None) or set()
        if resolved is None or (resolved not in self.facts.allowed_paths and resolved not in on_timeline):
            self.reasons.append(f"{self.tool}.{label}: path not offered ({Path(v).name})")
            return
        if config.restrict_paths_active():
            try:
                config.assert_path_allowed(resolved)
            except ValueError as e:
                self.reasons.append(f"{self.tool}.{label}: {e}")

    def offsets(self, offsets: Any) -> None:
        if offsets is None or self.is_placeholder(offsets):
            return
        if not isinstance(offsets, dict):
            self.reasons.append(f"{self.tool}.offsets: must be {{path: seconds}}")
            return
        if len(offsets) > MAX_OFFSETS:
            self.reasons.append(f"{self.tool}.offsets: {len(offsets)} files; the limit is {MAX_OFFSETS}")
        for path, seconds in list(offsets.items())[:MAX_OFFSETS]:
            self.path(f"offsets[{Path(str(path)).name}]", path)
            if not _num(seconds):
                self.reasons.append(f"{self.tool}.offsets[{Path(str(path)).name}]: offset must be a finite number")

    def ranges(self, ranges: Any) -> None:
        if not isinstance(ranges, list):
            self.reasons.append(f"{self.tool}.ranges: must be a list of {{src, start, end}}")
            return
        if len(ranges) > MAX_CUT_RANGES:
            self.reasons.append(f"{self.tool}.ranges: {len(ranges)} ranges; the limit is {MAX_CUT_RANGES} per step")
        for i, r in enumerate(ranges[:MAX_CUT_RANGES]):
            if not isinstance(r, dict):
                self.reasons.append(f"{self.tool}.ranges[{i}]: must be {{src, start, end}}")
                continue
            self.path(f"ranges[{i}].src", r.get("src"))
            s, e = r.get("start"), r.get("end")
            if not _num(s) or not _num(e) or float(e) <= float(s):
                self.reasons.append(f"{self.tool}.ranges[{i}]: end must be after start")
            elif float(e) - float(s) > MAX_CUT_RANGE_S:
                self.reasons.append(f"{self.tool}.ranges[{i}]: a range may not exceed {MAX_CUT_RANGE_S:g} s")

    def switches(self, switches: Any) -> None:
        if not isinstance(switches, list):
            self.reasons.append(f"{self.tool}.switches: must be a list of {{src, at_src, until_src, angle_src}}")
            return
        if len(switches) > MAX_CAMERA_SWITCHES:
            self.reasons.append(f"{self.tool}.switches: {len(switches)} switches; the limit is "
                                f"{MAX_CAMERA_SWITCHES} per step")
        for i, sw in enumerate(switches[:MAX_CAMERA_SWITCHES]):
            if not isinstance(sw, dict):
                self.reasons.append(f"{self.tool}.switches[{i}]: must be {{src, at_src, until_src, angle_src}}")
                continue
            self.path(f"switches[{i}].src", sw.get("src"))
            self.path(f"switches[{i}].angle_src", sw.get("angle_src"))
            a, b = sw.get("at_src"), sw.get("until_src")
            if not _num(a) or not _num(b) or float(b) <= float(a):
                self.reasons.append(f"{self.tool}.switches[{i}]: until_src must be after at_src")


def check_brain_tool_args(tool: str, args: dict[str, Any], facts: Any, reasons: Reasons, *,
                          resolve_path: Callable[[str], str | None], known_tracks: set[str],
                          is_placeholder: Callable[[Any], bool]) -> None:
    """The literal-arg rules of the three EB1 tools; a sentinel arg is skipped."""
    if tool not in ("cut_source_ranges", "apply_camera_plan", "sync_dialogue_lane"):
        return
    r = _Rules(tool, facts, reasons, resolve_path, known_tracks, is_placeholder)
    if tool == "cut_source_ranges":
        if "ranges" in args and sentinel_kind(args["ranges"]) is None:
            r.ranges(args["ranges"])
    elif tool == "apply_camera_plan":
        if "switches" in args and sentinel_kind(args["switches"]) is None:
            r.switches(args["switches"])
        r.offsets(args.get("offsets"))
    else:
        if "src" in args and not is_placeholder(args["src"]):
            r.path("src", args["src"])
        r.offsets(args.get("offsets"))
        lane = args.get("lane")
        if lane is not None and lane not in known_tracks:
            reasons.append(f"{tool}.lane: {lane!r} is not a track")


__all__ = ["MAX_CUT_RANGES", "MAX_CUT_RANGE_S", "MAX_CAMERA_SWITCHES", "MAX_OFFSETS", "BRAIN_SENTINELS",
           "SENTINEL_FILLS", "session_brain_dir", "check_brain_step", "check_brain_tool_args"]
