"""Editor Brain (EB1) facts: the graph, the dialogue lane and "was this
timeline already edited by the brain" — pure reads, split out of facts.py so
both files stay under the 800-line limit (review SC-09).

`brain_facts(store, edl, sdir)` is the one entry `build_facts` calls. With
`brain.enabled` OFF it returns `{}` — every brain field keeps its default,
so a flag-off session's facts are the 0.8.0 ones (review SC-04/SC-12: the
dialogue lane used to be computed before the flag was looked at).

  * `brain_graph_id` / `brain_layers` — the session's CURRENT Content Graph,
    when every source it names is still the file it was analysed as.
  * `timeline_paths` — the files already on the timeline.
  * `dialogue_lane` — the lane `a1` when it holds media: its source, the
    offsets the brain laid it with and whether it still follows the picture
    (`in_sync`, review SC-12: `brain.checks.c_dialogue_in_sync` run on the
    LIVE edl; None when no brain run laid the lane, so there is nothing to
    measure it against — a lane made by `detach_audio` is never "stale").
  * `brain_edit` — "this timeline was already edited by the brain" (review
    EX-02 / UX-04): the recorded version whose state IS the timeline, a
    brain run still in the history with edits after it, or the brain's
    footprint (a dialogue lane of an analysed source, camera pieces of a
    second angle) when no version row survived. None on a fresh import, and
    None again once the run was undone or the original restored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: A v1 piece of a file is "the whole upload" when it starts at the head and
#: ends at the tail within this many seconds (a camera-plan piece does not).
WHOLE_FILE_TOL_S = 0.05

EditState = Literal["brain_edited", "edited_after", "brain_traces"]


class DialogueLaneFact(BaseModel):
    """Editor Brain (EB1, spec §4.6.1): the dialogue lane `a1` when one
    exists — its source, the offsets it was laid with (`{path: seconds}`,
    empty when unknown) and whether it still follows the picture (`None`
    when no brain run laid it: there is no reference to measure against)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    src: str
    in_sync: bool | None = None
    offsets: dict[str, float] = Field(default_factory=dict)


class BrainEditFact(BaseModel):
    """The timeline carries an earlier brain edit: `state` says how sure we
    are and `label` names the version ("V1 Premium Podcast") when one is on
    record."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EditState
    label: str | None = None
    version_id: str | None = None


def brain_on() -> bool:
    """`brain.enabled` (env override, then settings.json). Anything unreadable is OFF."""
    try:
        from ...brain_setting import is_enabled
        return bool(is_enabled())
    except Exception:  # noqa: BLE001 — a broken settings file must not turn the brain on
        return False


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------

def current_graph(sdir: Path) -> tuple[str | None, dict[str, str]]:
    """(graph id, reference layers) of the session's current graph, or
    (None, {}) when there is none or it is stale (a named source moved)."""
    try:
        from ...brain import store as _store
        gid = _store.current_graph_id(sdir)
        if not gid:
            return None, {}
        raw = _store.read_json(_store.graph_path(sdir, gid))
    except Exception:  # noqa: BLE001 — a broken brain dir is "no graph", never a failed prompt
        return None, {}
    if not isinstance(raw, dict):
        return None, {}
    layers: dict[str, str] = {}
    for src in raw.get("sources") or []:
        path = src.get("path")
        if path and not Path(str(path)).exists():
            return None, {}
        if src.get("key") == raw.get("reference"):
            layers = {str(k): str(v) for k, v in (src.get("layers") or {}).items()}
    return str(gid), layers


def _read_header(sdir: Path, gid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from ...brain import store as _store
    header = _store.read_json(_store.graph_path(sdir, gid))
    angles = _store.read_json(_store.brain_dir(sdir) / "angles.json")
    return (header if isinstance(header, dict) else {}), (angles if isinstance(angles, dict) else {})


def _path_of(header: dict[str, Any], key: str) -> str | None:
    """The file a source key names; a key that already IS a path (hand-written decisions) is kept."""
    bare = str(key).removeprefix("src_")
    for s in header.get("sources") or []:
        if str(s.get("key", "")).removeprefix("src_") == bare and s.get("path"):
            return str(s["path"])
    return str(key) if "/" in str(key) or "\\" in str(key) else None


def _angle_members(header: dict[str, Any], angles: dict[str, Any]) -> list[dict[str, Any]]:
    """The angle group in angle order, `{path, duration, primary}` each."""
    from ...brain.planner.graph_view import angle_group
    raw = angle_group(list(header.get("sources") or []), angles or None, str(header.get("reference") or ""))
    out: list[dict[str, Any]] = []
    for i, m in enumerate(raw):
        path = m.get("path") or (_path_of(header, str(m.get("src_key"))) if m.get("src_key") else None)
        src = next((s for s in header.get("sources") or []
                    if str(s.get("key", "")).removeprefix("src_") == str(m.get("src_key", "")).removeprefix("src_")), {})
        if path:
            out.append({"path": str(path), "duration": float(src.get("duration") or 0.0), "primary": i == 0})
    return out


# --------------------------------------------------------------------------
# the dialogue lane
# --------------------------------------------------------------------------

def _lane_clips(edl: Any) -> list[Any]:
    from ...edl.schema import Clip
    lane = edl.get_track("a1")
    if lane is None or getattr(lane, "type", "audio") != "audio":
        return []
    return [c for c in lane.clips if isinstance(c, Clip) and c.src]


def _live_decisions(store: Any) -> str | None:
    """The newest decisions id still in the history: a `prompt` op that
    carries one, or a restored version that does."""
    ops = getattr(getattr(store, "ops", None), "ops", None) or []
    for op in reversed(ops):
        did = (op.args or {}).get("decisions")
        if isinstance(did, str):
            return did
        if op.tool == "restore_version":
            vid = (op.args or {}).get("id")
            from ...brain import versions as _versions
            row = next((v for v in _versions.list_versions(store.dir) if v.id == vid), None)
            if row is not None and row.decisions_id:
                return str(row.decisions_id)
    return None


def _lane_offsets(store: Any, sdir: Path) -> dict[str, float]:
    """`{path: seconds}` the dialogue lane was laid with — the dialogue
    decision of the newest EDP still in the history (the very numbers the
    compiler put in the `sync_dialogue_lane` step), or {}."""
    did = _live_decisions(store)
    if not did:
        return {}
    try:
        from ...brain import store as _store
        edp = _store.read_edp(sdir, did)
        dec = next((d for d in (edp.decisions if edp else []) if d.kind == "dialogue"), None)
        if dec is None:
            return {}
        header, angles = _read_header(sdir, edp.graph.id)
        offs = {p: float(v) for k, v in dict(dec.params.get("offsets") or {}).items() if (p := _path_of(header, k))}
        src_path = _path_of(header, str(dec.params.get("src")))
        member = next((m for m in angles.get("members") or []
                       if str(m.get("src_key", "")).removeprefix("src_") == str(dec.params.get("src")).removeprefix("src_")
                       or m.get("path") == src_path), {})
        if src_path:
            offs.setdefault(src_path, float(member.get("sync_offset_s") or 0.0))
        return offs
    except Exception:  # noqa: BLE001 — unknown offsets are honest: in_sync stays None
        return {}


def _in_sync(edl: Any, src: str, offsets: dict[str, float]) -> bool | None:
    """`dialogue_in_sync` (brain/checks.py) on the live edl, with the lane's own offsets."""
    if not offsets:
        return None
    try:
        from types import SimpleNamespace
        from ...brain import checks as _checks
        from .schema import Postcondition
        pc = Postcondition(check="dialogue_in_sync", human="the dialogue lane is in sync with the picture", args={})
        step = SimpleNamespace(tool="sync_dialogue_lane", args={"src": src, "lane": "a1", "offsets": offsets})
        res = _checks.c_dialogue_in_sync(SimpleNamespace(edl=edl, plan=SimpleNamespace(steps=[step])), pc)
        return None if res.passed is None else bool(res.passed)
    except Exception:  # noqa: BLE001 — the check is another module's; unknown is honest
        return None


def dialogue_lane(store: Any, edl: Any, sdir: Path) -> DialogueLaneFact | None:
    clips = _lane_clips(edl)
    if not clips:
        return None
    src = max({c.src for c in clips}, key=lambda p: sum(1 for c in clips if c.src == p))
    offsets = _lane_offsets(store, sdir)
    return DialogueLaneFact(src=str(src), in_sync=_in_sync(edl, str(src), offsets), offsets=offsets)


# --------------------------------------------------------------------------
# already edited by the brain
# --------------------------------------------------------------------------

def _same_file(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _footprint(edl: Any, gid: str | None, sdir: Path) -> bool:
    """The brain's footprint on the timeline: an unlinked dialogue-lane clip of
    an analysed source, or a v1 piece of a second angle that is not the
    whole upload (a camera-plan piece)."""
    if not gid:
        return False
    try:
        members = _angle_members(*_read_header(sdir, gid))
    except Exception:  # noqa: BLE001
        return False
    paths = [m["path"] for m in members]
    if any(getattr(c, "linked_to", None) is None and any(_same_file(c.src, p) for p in paths) for c in _lane_clips(edl)):
        return True
    from ...edl.schema import Clip
    v1 = edl.get_track("v1")
    for c in (v1.clips if v1 is not None else []):
        m = next((m for m in members if isinstance(c, Clip) and c.src and _same_file(c.src, m["path"])), None)
        if m is None or m["primary"] or not m["duration"]:
            continue
        if c.in_ > WHOLE_FILE_TOL_S or c.out < m["duration"] - WHOLE_FILE_TOL_S:
            return True
    return False


def _live_brain_versions(store: Any) -> list[Any]:
    """Brain versions whose run is still in the history (⌘Z pops the op, so an undone run drops out)."""
    from ...brain import versions as _versions
    ops = store.ops.ops
    live = []
    for v in _versions.list_versions(store.dir):
        if v.kind == "brain" and 0 <= v.op_seq < len(ops) and ops[v.op_seq].edl_hash_after == v.edl_hash:
            live.append(v)
    return live


def brain_edit(store: Any, edl: Any, sdir: Path, gid: str | None) -> BrainEditFact | None:
    try:
        live = _live_brain_versions(store)
    except Exception:  # noqa: BLE001 — a broken versions file is "no versions"
        live = []
    current = edl.hash()
    exact = next((v for v in reversed(live) if v.edl_hash == current), None)
    if exact is not None:
        return BrainEditFact(state="brain_edited", label=exact.label, version_id=exact.id)
    traces = _footprint(edl, gid, sdir)
    if live and traces:
        return BrainEditFact(state="edited_after", label=live[-1].label, version_id=live[-1].id)
    if traces:
        return BrainEditFact(state="brain_traces")
    return None


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def _timeline_paths(edl: Any, resolve: Any) -> set[str]:
    out: set[str] = set()
    for t in edl.tracks:
        for c in t.clips:
            src = getattr(c, "src", None)
            if isinstance(src, str) and src:
                out.add(resolve(Path(src)))
    return out


def brain_facts(store: Any, edl: Any, sdir: Path, *, resolve: Any) -> dict[str, Any]:
    """The TimelineFacts fields the brain owns; `{}` with `brain.enabled` off."""
    if not brain_on():
        return {}
    gid, layers = current_graph(sdir)
    return {
        "timeline_paths": _timeline_paths(edl, resolve),
        "brain_graph_id": gid, "brain_layers": layers,
        "dialogue_lane": dialogue_lane(store, edl, sdir),
        "brain_edit": brain_edit(store, edl, sdir, gid),
    }


__all__ = ["DialogueLaneFact", "BrainEditFact", "brain_on", "current_graph", "dialogue_lane", "brain_edit",
           "brain_facts", "WHOLE_FILE_TOL_S"]
