"""EDP → Plan (spec §5.6), pure.

1. group the decisions by sentinel kind and emit ONE step per kind present
   plus the literal steps; the stage-2 order is pinned (`STAGE2_ORDER`,
   `stage2_rank`): keep → cuts → story_splits → story_order → camera →
   sync_dialogue_lane, the dialogue lane always LAST so it sees the final
   v1 layout; the validator's stage sort is stable, so the order survives;
2. `Step.why` is the group's aggregate ("Removed 14 stretches: 9 silences,
   4 fillers, 1 false start");
3. postconditions ≤ 20, the two blocking checks first (when lane C's
   `CHECK_SPECS` carries them), then the headline ones;
4. no questions of its own: the analysis gate is `_x_edit`'s and the `go`
   confirm is `planner.compose`'s (from `estimated_seconds`);
5. `title` from the summary; `estimated_seconds` from `costs.py`.

More than 2,000 cut ranges fan out inside the one step (`brain/resolve.py`
chunks a dispatch at 2,000), so a long episode needs no second step this
wave; ≤ 24 steps is asserted.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.prompt.costs import estimate_seconds
from ..agent.prompt.facts import TimelineFacts
from ..agent.prompt.presets import bed_for_mood
from ..agent.prompt.recipes import Expansion, audit_expansion, pc, step
from ..agent.prompt.schema import CHECK_SPECS, STAGE_CAPTIONS, STAGE_CUTS, STAGE_EXPORT, STAGE_LOOK, STAGE_MUSIC, STAGE_PREREQ, STAGE_REFRAME, NeedsInput, Postcondition, Step
from ..agent.tools import CUT_RANGE_MAX_S
from . import energy as E
from . import resolve as _resolve
from .planner.graph_view import Graph
from .resolve import STAGE2_ORDER

MAX_STEPS = 24
MAX_POSTCONDITIONS = 20
MAX_RANGE_S = CUT_RANGE_MAX_S        # `cut_source_ranges`: one range is at most this long (the tool's own cap)
SENTINEL = "$brain:"
_STAGE_PUNCH = STAGE_LOOK
_CODE_NOUN = {"silence": "silence", "filler": "filler", "filler_acoustic": "acoustic filler", "false_start": "false start",
              "dead_air": "stretch of dead air", "repeat": "repeat"}


@dataclass
class Compiled:
    steps: list[Step] = field(default_factory=list)
    postconditions: list[Postcondition] = field(default_factory=list)
    questions: list[NeedsInput] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sentinels: list[str] = field(default_factory=list)
    estimated_seconds: float = 0.0
    title: str = ""


def stage2_rank(s: Step) -> int:
    """Where a compiled stage-2 step sits: the resolver's own order (`resolve.stage2_rank`), the literal "angles off
    the main lane" step first, any other stage-2 step before the dialogue lane."""
    if s.tool == "cut_source_ranges" and isinstance(s.args.get("ranges"), list):
        return -1                                     # the literal "angles off the main lane" step leads
    rank = _resolve.stage2_rank(s)
    return rank if rank >= 0 else len(STAGE2_ORDER) - 1


def _plural(n: int, noun: str) -> str:
    if n == 1:
        return f"1 {noun}"
    if noun.endswith("air"):
        return f"{n} stretches of dead air"
    return f"{n} {noun}es" if noun.endswith(("ch", "sh", "s", "x")) else f"{n} {noun}s"


def _cuts_why(decisions: list[dict]) -> str:
    counts = Counter(d["reason"]["code"] for d in decisions)
    parts = [_plural(n, _CODE_NOUN.get(code, code)) for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return f"Removed {_plural(len(decisions), 'stretch')}: " + ", ".join(parts)


def _accepts(tool: str, arg: str) -> bool:
    from ..agent.prompt.validate import plan_schema_for
    schema = plan_schema_for(tool) or {}
    return arg in (schema.get("properties") or {})


def _whole_file_ranges(path: str, duration: float) -> list[dict[str, Any]]:
    """Consecutive `{src, start, end}` ranges of at most MAX_RANGE_S that
    together cover [0, duration] of `path` (`cut_source_ranges` caps one
    range at 600 s; the handler merges the ranges of one source, so a
    45-minute angle is five ranges of one step, never a truncated first
    ten minutes with the rest left on the main lane — review SC-03)."""
    total = max(0.0, float(duration))
    out: list[dict[str, Any]] = []
    start = 0.0
    while start < total - 1e-9:
        end = min(total, start + MAX_RANGE_S)
        out.append({"src": path, "start": round(start, 4), "end": round(end, 4)})
        start = end
    return out


def _angles_off_v1(g: Graph, facts: TimelineFacts, out: Compiled) -> None:
    """Two cameras dropped on the timeline land one after the other on the
    main lane. The edit plays ONE programme whose picture changes angle, so
    the other angles' own clips leave the main lane first (their footage
    returns through the camera plan) — a literal step, before the keep."""
    on_v1 = {str(c.name) for c in facts.clips if c.track == "v1" and c.name}
    ranges, names = [], []
    for m in g.members:
        path = m.get("path") or g.path_of(str(m["src_key"]))
        if not path or g.bare(str(m["src_key"])) == g.bare(g.primary) or Path(str(path)).stem not in on_v1 \
                or str(path) not in facts.allowed_paths:
            continue
        ranges += _whole_file_ranges(str(path), g.duration_of(str(m["src_key"])))
        names.append(f"camera {m.get('angle') or g.angle_of_key(str(m['src_key'])) or '?'}")
    if ranges:
        many = len(names) > 1
        lead = ", ".join(names)[0].upper() + ", ".join(names)[1:]
        out.steps.append(step("cut_source_ranges", STAGE_CUTS, f"{lead} leave{'' if many else 's'} the main lane: "
                              f"{'they are angles' if many else 'it is an angle'} of the same take", track="v1", ranges=ranges))
        who = f"The clips of {', '.join(names)} were" if many else f"{lead}’s clip was"
        out.notes.append(f"{who} moved off the main lane; {'their pictures come' if many else 'its picture comes'} back "
                         f"wherever the edit cuts to {'them' if many else 'it'}")


def _stage2(edp: dict, g: Graph, out: Compiled) -> None:
    did = edp["id"]
    by = _by_kind(edp)
    if by["keep_window"]:
        n = len(by["keep_window"])
        out.steps.append(step("cut_source_ranges", STAGE_CUTS, f"keep the hook and the best {_plural(n, 'window')} of whole sentences",
                              track="v1", ranges=f"{SENTINEL}keep", plan_ref=did))
        out.sentinels.append("keep")
    if by["cut_range"]:
        out.steps.append(step("cut_source_ranges", STAGE_CUTS, _cuts_why(by["cut_range"]), track="v1", ranges=f"{SENTINEL}cuts", plan_ref=did))
        out.sentinels.append("cuts")
    if by["open_on"]:
        quote = (edp["summary"].get("hook") or {}).get("quote") or "the opening statement"
        out.steps.append(step("split_at", STAGE_CUTS, "isolate the hook statement", track="v1", time=f"{SENTINEL}story_splits", plan_ref=did))
        out.steps.append(step("reorder_clips", STAGE_CUTS, f"open on “{quote[:60]}”", track="v1", order=f"{SENTINEL}story_order", plan_ref=did))
        out.sentinels += ["story_splits", "story_order"]
    if by["switch_angle"]:
        offs = {p: float(v) for k, v in _dialogue_offsets(edp, g).items() if (p := g.path_of(k))}
        n = len(by["switch_angle"])
        at_cut = sum(1 for d in by["switch_angle"] if d["reason"]["code"] == "at_cut")
        out.steps.append(step("apply_camera_plan", STAGE_CUTS, f"{_plural(n, 'camera change')}, {at_cut} on jump cuts",
                              switches=f"{SENTINEL}camera", offsets=offs, plan_ref=did))
        out.sentinels.append("camera")
    for d in by["dialogue"]:
        src_key = str(d["params"]["src"])
        src_path = g.path_of(src_key)
        if not src_path:
            out.notes.append("dialogue lane skipped: the dialogue source has no file path in the graph")
            continue
        offs = {p: float(v) for k, v in d["params"]["offsets"].items() if (p := g.path_of(k))}
        offs.setdefault(src_path, float(g.offset(src_key)))
        out.steps.append(step("sync_dialogue_lane", STAGE_CUTS, f"dialogue on its own lane; 5 ms fades at {d['params'].get('seams', 0)} seams",
                              src=src_path, lane="a1", offsets=offs, seam_fade_s=E.SEAM_FADE_S, mute_camera_mics=True))


def _dialogue_offsets(edp: dict, g: Graph) -> dict[str, float]:
    for d in edp["decisions"]:
        if d["kind"] == "dialogue":
            return dict(d["params"].get("offsets") or {})
    return {str(m["src_key"]): float(m.get("sync_offset_s") or 0.0) for m in g.members}


def _later(edp: dict, g: Graph, facts: TimelineFacts, out: Compiled) -> None:
    did = edp["id"]
    by = _by_kind(edp)
    keyed = by["punch_in"] + by["jump_cut_hide"]
    if keyed:
        why = f"{_plural(len(by['punch_in']), 'punch-in')} (clause start → step release at the next seam)"
        if by["jump_cut_hide"]:
            why += f" and {_plural(len(by['jump_cut_hide']), 'jump-cut hide')}"
        out.steps.append(step("add_keyframe", _STAGE_PUNCH, why, optional=True, clip_id=f"{SENTINEL}punch_ins", plan_ref=did))
        out.sentinels.append("punch_ins")
    for d in by["reframe"]:
        ratio = d["params"]["ratio"]
        out.steps.append(step("auto_reframe", STAGE_REFRAME, f"{ratio} canvas, no re-encode", ratio=ratio, subject_track=False))
        out.steps.append(step("set_clip_fit", STAGE_REFRAME, "fill the frame", clip_id="$v1_all", fit="cover"))
        out.postconditions += [pc("canvas_aspect", "the canvas has the requested aspect", ratio=ratio),
                               pc("no_letterbox", "no black bars")]
    for d in by["captions"]:
        p = d["params"]
        args: dict[str, Any] = {"style": p["style"], "position": p["position"]}
        why = f"Captions: {p['mode'].capitalize()}"
        if p.get("cues") and _accepts("add_caption_track", "cues"):
            # the plan's own cues, laid from every speaker's words and mapped through the live layout by source
            args.update(cues=f"{SENTINEL}captions", plan_ref=did)
            why += f", {_plural(len(p['cues']), 'cue')} from the analysed speech of every speaker"
            out.sentinels.append("captions")
        elif p.get("max_chars") and _accepts("add_caption_track", "max_chars"):
            args["max_chars"] = p["max_chars"]
        elif p.get("max_chars"):
            out.notes.append("caption line length (max_chars) lands with the caption tools' next wave")
        out.steps.append(step("add_caption_track", STAGE_CAPTIONS, why, **args))
        out.steps.append(step("set_caption_style", STAGE_CAPTIONS, f"{p['mode']} look", **dict(p.get("look") or {})))
        out.postconditions += [pc("captions_cover", "captions cover the speech", min_ratio=0.9),
                               pc("captions_nonempty", "captions were laid"),
                               pc("captions_within_extent", "no caption runs past the video"),
                               pc("captions_style", "captions use the requested style", style=p["style"])]
    for d in by["music"]:
        _music(d, facts, out)
    for d in by["export_preset"]:
        name = d["params"]["platform"]
        out.steps.append(step("apply_export_preset", STAGE_EXPORT, f"{name} canvas, bitrate and loudness", name=name))
        out.postconditions.append(pc("export_preset_applied", "the export preset is set", name=name))


def _music(d: dict, facts: TimelineFacts, out: Compiled) -> None:
    p = d["params"]
    bed = bed_for_mood(p.get("mood"))
    if bed is None or str(bed.path) not in facts.allowed_paths:
        out.notes.append(f"{p.get('mood', 'the')} bed is not installed; no music laid")
        return
    if facts.has_music:
        out.notes.append("music is already on the timeline; the brain left it and added no bed")
        return
    out.steps.append(step("add_music", STAGE_MUSIC, f"subtle {p.get('mood')} bed under the reel", src=str(bed.path), start=0.0,
                          volume_db=float(p["volume_db"]), duck=True, loop=True))
    out.steps.append(step("set_duck", STAGE_MUSIC, "duck under speech", track="music", enabled=True, to_db=float(p["duck_db"])))
    out.steps.append(step("fit_music_to_video", STAGE_MUSIC, "end the bed with the video", fade_out=2.0))
    out.postconditions += [pc("music_present", "music is on the timeline", ducked=True),
                           pc("music_covers", "music runs under the whole video", min_ratio=0.95),
                           pc("music_within_video_extent", "music does not outlast the video")]


def _by_kind(edp: dict) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {k: [] for k in ("keep_window", "cut_range", "keep_pause", "open_on", "switch_angle", "punch_in",
                                                 "jump_cut_hide", "captions", "music", "reframe", "dialogue", "export_preset")}
    for d in edp["decisions"]:
        by.setdefault(d["kind"], []).append(d)
    return by


def _blocking_first(pcs: list[Postcondition], has_cuts: bool, has_dialogue: bool) -> list[Postcondition]:
    front: list[Postcondition] = []
    if has_cuts and "no_cut_mid_word" in CHECK_SPECS:
        front.append(pc("no_cut_mid_word", "no cut lands inside a word", tol=0.02))
    if has_dialogue and "dialogue_in_sync" in CHECK_SPECS:
        front.append(pc("dialogue_in_sync", "the dialogue lane is in sync with the picture"))
    if has_cuts:
        front.append(pc("duration_shrank", "the video got shorter", min_seconds=0.1))
    seen: set[str] = set()
    out: list[Postcondition] = []
    for p in front + pcs:
        key = p.check + repr(sorted(p.args.items()))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out[:MAX_POSTCONDITIONS]


def _title(edp: dict) -> str:
    s = edp["summary"]
    by = _by_kind(edp)
    bits = []
    if by["cut_range"]:
        bits.append(_plural(len(by["cut_range"]), "cut"))
    if by["switch_angle"]:
        bits.append(_plural(len(by["switch_angle"]), "angle change"))
    if by["punch_in"]:
        bits.append(_plural(len(by["punch_in"]), "punch-in"))
    bits += [k for k in ("captions", "music") if by[k]]
    target = f"{s['duration_s']:.0f} s reel" if s["target"] == "reel" and s.get("duration_s") else s["target"]
    return f"{s['project_type'].replace('_', ' ')} → {target}: {', '.join(bits)}"[:80]


def _conform_step(edp: dict, facts: TimelineFacts, out: Compiled) -> None:
    """A project below what any platform takes (20 fps) that ends in an export preset is conformed to the
    preset's rate by that preset. The planner put every edge on THAT grid, so the canvas is conformed before
    the first cut (an explicit stage: the prerequisites) — the cuts, the camera plan and the dialogue lane all
    run on the grid the export has, and no piece starts between two output frames (closer review, th9_p20)."""
    from ..agent.dispatch import conformed_fps
    for d in edp["decisions"]:
        if d["kind"] != "export_preset":
            continue
        rate = float(conformed_fps(float(facts.fps), str(d["params"]["platform"])))
        if abs(rate - float(facts.fps)) > 1e-3:
            out.steps.append(step("set_canvas", STAGE_PREREQ, f"conform the project to {rate:g} fps first: the "
                                  f"{d['params']['platform'].replace('_', ' ')} preset needs it, and every cut then lands on a frame",
                                  fps=rate))
        return


def compile_edp(edp: dict, facts: TimelineFacts, graph: dict | Graph) -> Compiled:
    g = graph if isinstance(graph, Graph) else Graph(graph)
    out = Compiled()
    _conform_step(edp, facts, out)
    if any(d["kind"] in ("switch_angle", "dialogue") for d in edp["decisions"]):
        _angles_off_v1(g, facts, out)
    _stage2(edp, g, out)
    _later(edp, g, facts, out)
    audit = audit_expansion() if edp["summary"].get("target") == "reel" else Expansion()   # the audit rates a HOOK: an episode has none
    out.steps += list(audit.steps)
    out.postconditions = _blocking_first(out.postconditions + list(audit.postconditions),
                                         has_cuts=any(d["kind"] in ("cut_range", "keep_window") for d in edp["decisions"]),
                                         has_dialogue=any(s.tool == "sync_dialogue_lane" for s in out.steps))
    stage2 = sorted((s for s in out.steps if s.stage == STAGE_CUTS), key=stage2_rank)
    out.steps = stage2 + [s for s in out.steps if s.stage != STAGE_CUTS]
    if len(out.steps) > MAX_STEPS:
        raise ValueError(f"compiled plan has {len(out.steps)} steps (max {MAX_STEPS})")
    for d in edp["summary"].get("deferred") or []:
        out.notes.append(f"not done this time — {d['asked']}: {d['why']}")
    out.estimated_seconds = estimate_seconds(out.steps, facts)
    out.title = _title(edp)
    return out


__all__ = ["Compiled", "compile_edp", "stage2_rank", "STAGE2_ORDER", "MAX_STEPS"]
