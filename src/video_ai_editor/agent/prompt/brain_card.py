"""Reasons on the change card and in the reply for an Editor Brain run (EB1-F).

A brain run's card stays DIFF-derived: `changes.summarize` writes the lines
and never omits a change (`covered ⊇ diff_keys`). This module only ADDS to
them, behind `brain.enabled`:

  * a WHY per line — the decision's `reason.text` from the frozen EDP
    (`<session>/brain/decisions/<did>.json`, the `plan_ref` every sentinel
    step carries), tied to the line by the resolver's footprint
    (`<scratch>/brain/footprint.json`: the clip ids each decision addressed)
    and, where a decision has no clip footprint (captions, a bed), by the
    line's group;
  * ONE grouped line for the cuts — "Removed 14 stretches: 9 silences, 4
    fillers, 1 false start" — carrying the union of the keys of the
    "Deleted …" lines it replaces, so the coverage property still holds;
  * E8's rule: a decision NEVER claims more than the diff shows, and is never
    denied more than the diff shows either (brain_card_marks): it is
    `applied: false` — struck on the Plan tab, decorating no line — ONLY when
    its footprint is truly absent from the diff (an opening is proved from
    the tree the run leaves). A line's why always names a decision whose
    footprint the line came from; a line nothing explains is said to be
    unexplained rather than given a made-up why.

`register_plan` / `reply_bits` let `summary.compose_reply` (whose signature
is the executor's) find the EDP of the plan it is describing: the rung
attribution ("via Recipes · moments by Apple Intelligence"), the
`deferred[]` line ("Not done this time: …") and the version label the run
is recorded as. The lines Apply is checked against (`preview.apply_check`)
are always the raw diff lines: grouping and whys are presentation.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Iterable

from ...brain import store as _store
from ...brain.schema import MAX_GRAPH_BYTES
from . import changes as C
from . import brain_card_marks as M
from .brain_card_marks import claimed_clip_ids, clip_ids_in, clips_for_entry  # noqa: F401 — re-exported
from .changes import Change
from .schema import PLAN_REF_PATTERN, Plan

DID_RE = re.compile(PLAN_REF_PATTERN)
DECISIONS_DIR = ("brain", "decisions")
FOOTPRINT_FILE = ("brain", "footprint.json")
#: Decision kinds that are cuts (the grouped line counts them).
CUT_KINDS = frozenset({"cut_range", "keep_window"})
#: A kept pause is the ABSENCE of a cut: on the Plan tab, never a line.
KEPT_KINDS = frozenset({"keep_pause"})
UNEXPLAINED = "part of this run; no single decision names it"
#: Human words per reason code for the grouped tally (singular, plural).
CODE_WORDS: dict[str, tuple[str, str]] = {
    "silence": ("silence", "silences"), "filler": ("filler", "fillers"),
    "filler_acoustic": ("filler", "fillers"), "soft_filler": ("filler", "fillers"),
    "false_start": ("false start", "false starts"), "dead_air": ("stretch of dead air", "stretches of dead air"),
    "repeat": ("repeat", "repeats"), "weak_question": ("weak question", "weak questions"),
    "dead_conversation": ("low-content stretch", "low-content stretches"),
    "technical": ("technical stretch", "technical stretches"),
    "duration_fit": ("trim to fit the length", "trims to fit the length"),
    "best_window": ("trim to the best window", "trims to the best window"),
}
# --------------------------------------------------------------------------
# the registry compose_reply reads (did → where its EDP is, its version label)
# --------------------------------------------------------------------------

#: Least-recently-used, capped: a long session must not keep one entry per plan it ever ran.
_REGISTRY: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_REGISTRY_LOCK = threading.Lock()
REGISTRY_MAX = 64


def _entry(did: str) -> dict[str, Any]:
    """The registry entry for `did` (created if absent), marked most recent, the oldest
    ones dropped over `REGISTRY_MAX`. Call with `_REGISTRY_LOCK` held."""
    entry = _REGISTRY.setdefault(did, {})
    _REGISTRY.move_to_end(did)
    while len(_REGISTRY) > REGISTRY_MAX:
        _REGISTRY.popitem(last=False)
    return entry


def decisions_id_of(plan: Plan) -> str | None:
    """The EDP a plan was compiled from: the `plan_ref` its sentinel steps
    carry (every one names the same EDP)."""
    for step in plan.steps:
        ref = step.args.get("plan_ref") if isinstance(step.args, dict) else None
        if isinstance(ref, str) and DID_RE.match(ref):
            return ref
    return None


def edp_path(session_dir: Path, did: str) -> Path:
    return Path(session_dir).joinpath(*DECISIONS_DIR) / f"{did}.json"


def load_edp(session_dir: Path, did: str) -> dict[str, Any] | None:
    if not DID_RE.match(did or ""):
        return None
    p = edp_path(session_dir, did)
    try:      # capped read (SC-13): the same limit the graph and decisions routes keep
        body = _store.read_json(p, cap=MAX_GRAPH_BYTES, what="decisions")
    except (OSError, ValueError, _store.TooLarge):
        return None
    return body if isinstance(body, dict) and body.get("id") == did else None


def register_plan(plan: Plan, session_dir: Path) -> None:
    """Remember where this plan's EDP lives (and the label its version will
    carry) so the reply can name them."""
    did = decisions_id_of(plan)
    if not did:
        return
    with _REGISTRY_LOCK:
        entry = _entry(did)
        entry["session_dir"] = Path(session_dir)
        entry.setdefault("label", prospective_label(did, Path(session_dir)))


def remember_version(did: str, label: str) -> None:
    with _REGISTRY_LOCK:
        _entry(did)["label"] = label


def version_label(did: str) -> str | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(did, {}).get("label")


def session_for(did: str) -> Path | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(did, {}).get("session_dir")


def prospective_label(did: str, session_dir: Path) -> str:
    """"V1 Reel": the next version number and the EDP's target."""
    from . import brain_seams
    try:
        n = len(brain_seams.versions().list(Path(session_dir)))
    except Exception:  # noqa: BLE001 — a label is a courtesy
        n = 0
    return f"V{n + 1} {version_noun(load_edp(session_dir, did) or {})}"


#: EDP style → the noun an episode's version carries ("V1 Premium Podcast").
_STYLE_NOUN = {"premium_podcast": "Premium Podcast", "clean_professional": "Clean Edit", "luxury": "Luxury Edit",
               "viral_reel": "Reel"}


def version_noun(edp: dict[str, Any]) -> str:
    """What the edit made, as the Versions strip names it: a reel is a
    "Reel"; an episode is named by its style ("Premium Podcast")."""
    target = str((edp.get("summary") or {}).get("target") or "").strip()
    if target == "reel":
        return "Reel"
    noun = _STYLE_NOUN.get(str(edp.get("style") or ""))
    if noun and noun != "Reel":
        return noun
    return target.capitalize() or "Edit"


# --------------------------------------------------------------------------
# footprint, keys and paths
# --------------------------------------------------------------------------

def _merged(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """A decision the resolver recorded at more than one step (an opening is
    both ordered and split): the footprint is what ALL its entries named —
    clip ids united, the first non-empty value of every other field."""
    if old is None:
        return new
    out = {**new, **{k: v for k, v in old.items() if v not in (None, [], {}, "")}}
    ids = claimed_clip_ids(old) | claimed_clip_ids(new)
    if ids:
        out["clip_ids"] = sorted(ids)
    return out


def _entries_of(body: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(entries by decision id, dropped, notices) from either footprint
    shape: C's resolver writes `{plan_ref, steps: {kind: [{decision, kind,
    code, text, clip_ids? | src_range + timeline? | times?}]}, dropped:
    [{decision, kind, why, step}]}`; a `{decisions: {id: {…}} | [{id, …}],
    notices}` file reads the same way."""
    out: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    notices: list[str] = []
    if not isinstance(body, (dict, list)):
        return out, dropped, notices
    if isinstance(body, dict) and isinstance(body.get("steps"), dict):
        for step, entries in body["steps"].items():
            for e in entries or []:
                if isinstance(e, dict) and e.get("decision"):
                    out[str(e["decision"])] = _merged(out.get(str(e["decision"])), {**e, "step": step})
        dropped = [dict(x) for x in body.get("dropped") or [] if isinstance(x, dict)]
    else:
        raw = body.get("decisions") if isinstance(body, dict) else body
        if isinstance(raw, dict):
            out = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        elif isinstance(raw, list):
            out = {str(e.get("id")): dict(e) for e in raw if isinstance(e, dict) and e.get("id")}
    if isinstance(body, dict) and isinstance(body.get("notices"), list):
        notices = [str(n) for n in body["notices"]]
    notices += [f"{x.get('kind') or 'decision'} {x.get('decision')} dropped: {x.get('why')}" for x in dropped]
    return out, dropped, notices


def load_footprint(dirs: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """`{decision id: entry}` from the first footprint file found under
    `dirs` (the scratch copy during a preview, the session on Apply), plus
    `__dropped__` and `__notices__` rows."""
    for d in dirs:
        p = Path(d).joinpath(*FOOTPRINT_FILE)
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out, dropped, notices = _entries_of(body)
        out["__dropped__"] = {str(x.get("decision")): x for x in dropped}
        out["__notices__"] = {"notices": notices}
        return out
    return {}


def leaf(path: Any) -> Any:
    """A path's leaf name; anything that is not an absolute path unchanged."""
    if isinstance(path, str) and (path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path)):
        return Path(path).name or path
    return path


def scrub_paths(obj: Any) -> Any:
    """The same structure with every absolute path (values AND dict keys) reduced to its leaf name — a value that
    IS a path, and one that holds a path inside a sentence — the routes and the card never say where a file lives."""
    if isinstance(obj, dict):
        return {leaf(k) if isinstance(k, str) else k: scrub_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_paths(v) for v in obj]
    if isinstance(obj, str):
        from ...brain.digest import scrub_paths_in_text       # a path INSIDE a sentence too ("Cut at /Users/me/x.mp4")
        return scrub_paths_in_text(leaf(obj))
    return obj


# --------------------------------------------------------------------------
# the tally and the grouped line
# --------------------------------------------------------------------------

def tally(codes: Iterable[str]) -> str:
    """"9 silences, 4 fillers, 1 false start" in first-seen order."""
    counts: Counter[str] = Counter()
    order: list[str] = []
    for code in codes:
        base = str(code).split(":", 1)[0]
        word = CODE_WORDS.get(base, (base.replace("_", " "), base.replace("_", " ") + "s"))
        if word not in counts:
            order.append(word)  # type: ignore[arg-type]
        counts[word] += 1  # type: ignore[index]
    return ", ".join(f"{counts[w]} {w[0] if counts[w] == 1 else w[1]}" for w in order)  # type: ignore[index]


def grouped_cut_line(n: int, codes: Iterable[str]) -> str:
    return f"Removed {n} stretch{'es' if n != 1 else ''}: {tally(codes)}"


# --------------------------------------------------------------------------
# the card
# --------------------------------------------------------------------------

def _decision_rows(edp: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for d in edp.get("decisions") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        reason = d.get("reason") if isinstance(d.get("reason"), dict) else {}
        ref = d.get("ref") if isinstance(d.get("ref"), dict) else None
        rows.append({"id": str(d["id"]), "kind": str(d.get("kind") or ""), "code": str(reason.get("code") or ""),
                     "text": str(reason.get("text") or ""), "optional": bool(d.get("optional")),
                     "by": d.get("by"), "score": d.get("score"), "confidence": d.get("confidence"),
                     "ref": scrub_paths(ref) if ref else None, "applied": None, "note": None, "_ref": ref})
    return rows


def _source_paths(edp: dict[str, Any], session_dir: Path | None) -> dict[str, str]:
    """EDP `ref.src` (a source key this wave; a path in a hand-written EDP)
    → the file, from the graph's sources when C's resolver can read them."""
    gid = str((edp.get("graph") or {}).get("id") or "")
    if not session_dir or not gid:
        return {}
    try:
        from ...brain import resolve as _resolve
        return dict(_resolve.source_paths(Path(session_dir) / "brain", gid))
    except Exception:  # noqa: BLE001 — a hand-written EDP names paths directly
        return {}


def _src_of(row: dict[str, Any], paths: dict[str, str]) -> str | None:
    ref = row.get("_ref") or {}
    src = ref.get("src") if isinstance(ref, dict) else None
    return _path_of_key(src, paths)


def _path_of_key(src: Any, paths: dict[str, str]) -> str | None:
    """A source key (`src_<hex>`; the resolver's table drops the prefix) or an
    absolute path → the file, or None when it cannot be named."""
    if not isinstance(src, str) or not src:
        return None
    for k in (src, src.removeprefix("src_")):
        if k in paths:
            return paths[k]
    return src if src.startswith("/") else None


def display_name(src: Any, paths: dict[str, str]) -> str:
    """What the card calls a source: the file's name as the media bin shows it
    ("th_16x9.mp4", never `src_55e2…` and never the `.normalized` copy); ''
    when the key cannot be resolved (the card then says nothing of it)."""
    path = _path_of_key(src, paths)
    if not path:
        return ""
    name = Path(path).name
    return name.replace(".normalized.", ".") if ".normalized." in name else name


def _group_cuts(changes: list[Change], rows: list[dict[str, Any]]) -> tuple[list[Change], list[int]]:
    """Fold every "Deleted …" line of the main lane into one tallied line
    when the plan cut more than one stretch."""
    cuts = [r for r in rows if r["kind"] in CUT_KINDS and r["applied"]]
    victims = [i for i, ch in enumerate(changes) if ch.group == "Video" and ch.text.startswith("Deleted")]
    if len(cuts) < 2 or not victims:
        return changes, []
    keys = frozenset().union(*(changes[i].keys for i in victims))
    line = Change(group="Video", text=grouped_cut_line(len(cuts), (r["code"] for r in cuts)), keys=keys)
    first = victims[0]
    out = [line if i == first else ch for i, ch in enumerate(changes) if i == first or i not in victims]
    return out, [out.index(line)]


def _timeline_at(before: Any, ref: Any, paths: dict[str, str], offsets: dict[str, float]) -> float | None:
    """Where a decision's reference second plays in the CURRENT timeline (the
    card says "in the current timeline": nothing has moved yet): the event at
    reference second r is at file second r + offsets[file] (the one clock rule),
    tried on the decision's own file and then on every file the main lane plays."""
    if not isinstance(ref, dict) or not isinstance(ref.get("t0"), (int, float)):
        return None
    try:
        from ..timemap import media_clips, source_to_timeline
        own = _path_of_key(ref.get("src"), paths)
        files = list(dict.fromkeys([p for p in [own] if p] + [str(c.src) for c in media_clips(before, "v1")]))
        for path in files:
            t = source_to_timeline(before, "v1", float(ref["t0"]) + offsets.get(path, 0.0), src=path)
            if t is not None:
                return round(float(t), 3)
    except Exception:  # noqa: BLE001 — a seek time is a nicety
        return None
    return None


def _hook_summary(summary: dict[str, Any], before: Any, paths: dict[str, str],
                  offsets: dict[str, float]) -> dict[str, Any] | None:
    """The hook with `timeline_t` (None when its instant is not on the main lane)."""
    hook = summary.get("hook")
    if not isinstance(hook, dict):
        return None
    out = {k: v for k, v in scrub_paths(hook).items() if k != "sent"}
    out["src"] = display_name(hook.get("src"), paths)
    out["timeline_t"] = _timeline_at(before, {"src": hook.get("src"), "t0": hook.get("t0")}, paths, offsets)
    return out


def _summary_for_card(edp: dict[str, Any], before: Any, after: Any, paths: dict[str, str],
                      offsets: dict[str, float]) -> dict[str, Any]:
    """The EDP summary as the card shows it: the hook with a seek time, files
    by display name (never a graph key), and the length the run leaves."""
    summary = {k: v for k, v in (edp.get("summary") or {}).items() if k != "story"}     # graph sentence ids
    summary["hook"] = _hook_summary(summary, before, paths, offsets)
    dialogue = summary.get("dialogue")
    if isinstance(dialogue, dict):
        named = {display_name(k, paths): v for k, v in (dialogue.get("offsets") or {}).items()}
        summary["dialogue"] = {**dialogue, "src": display_name(dialogue.get("src"), paths),
                               "offsets": {k: v for k, v in named.items() if k}}
    summary["result_s"] = round(float(getattr(after, "duration", 0.0) or 0.0), 1)
    summary["before_s"] = round(float(getattr(before, "duration", 0.0) or 0.0), 1)
    return summary


def _rung_line(edp: dict[str, Any], plan: Plan) -> dict[str, Any]:
    from .brains.base import BRAIN_LABELS
    brain = str(edp.get("brain") or plan.brain)
    content = edp.get("content_brain") or plan.content_brain
    line = f"via {BRAIN_LABELS.get(brain, brain)}"
    if content and content != brain:
        line += f" · moments by {BRAIN_LABELS.get(str(content), str(content))}"
    return {"brain": brain, "content_brain": content, "line": line}


def _public_row(row: dict[str, Any], before: Any, paths: dict[str, str], offsets: dict[str, float]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    ref = row.get("_ref")
    if isinstance(ref, dict):
        out["ref"] = {**scrub_paths(ref), "src": display_name(ref.get("src"), paths)}
    out["timeline_t"] = _timeline_at(before, ref, paths, offsets)
    return out


def build(plan: Plan, edp: dict[str, Any], footprint: dict[str, dict[str, Any]], before: Any, after: Any,
          changes: list[Change], session_dir: Path | None = None) -> tuple[list[Change], dict[str, Any]]:
    """The display lines (grouped, each with a why) and the card's `brain`
    payload, from the frozen EDP + footprint and the diff."""
    rows = _decision_rows(edp)
    paths = _source_paths(edp, session_dir)
    offsets = M.offsets_for(session_dir)
    ev = M.evidence(before, after, changes, C.diff_keys(before, after), paths, offsets)
    over = M.mark_applied(rows, footprint, ev, lambda r: _src_of(r, paths), KEPT_KINDS)
    grouped, grouped_at = _group_cuts(changes, rows)
    ev = M.Evidence(ev.before, ev.after, grouped, ev.changed, ev.owners, ev.paths, ev.offsets)
    display, unexplained, why_ids = M.attribute(grouped, rows, ev, UNEXPLAINED)
    for i in grouped_at:                        # the tally line is the cuts' own
        cut_rows = [r for r in rows if r["kind"] in CUT_KINDS and r["applied"]]
        display[i] = Change(group=display[i].group, text=display[i].text, keys=display[i].keys,
                            why=M.join_reasons([r["text"] or r["code"] for r in cut_rows]))
        why_ids[i] = [r["id"] for r in cut_rows]
        unexplained = [u for u in unexplained if u != i]
    summary = _summary_for_card(edp, before, after, paths, offsets)
    payload = {
        "decisions_id": str(edp.get("id")), "tab_default": "plan", "rungs": _rung_line(edp, plan),
        "summary": scrub_paths(summary), "decisions": [_public_row(r, before, paths, offsets) for r in rows],
        "deferred": list(summary.get("deferred") or []),
        "whys": [ch.why for ch in display], "why_ids": why_ids, "grouped": grouped_at, "unexplained": unexplained,
        "overclaims": over, "notices": list((footprint.get("__notices__") or {}).get("notices") or []),
    }
    return display, scrub_paths(payload)


def card_payload(plan: Plan, live: Any, scratch_dir: Path | None, before: Any, after: Any,
                 changes: list[Change]) -> tuple[list[Change], dict[str, Any] | None]:
    """`(display changes, payload)` for a brain run with the flag on; the
    changes untouched and None otherwise (an ordinary prompt, the flag off,
    or an EDP that cannot be read)."""
    from ...brain_setting import is_enabled
    did = decisions_id_of(plan)
    if not did or not is_enabled():
        return changes, None
    session_dir = Path(live.dir)
    edp = load_edp(session_dir, did)
    if edp is None:
        return changes, None
    dirs = [Path(scratch_dir)] if scratch_dir else []
    footprint = load_footprint(dirs + [session_dir])
    return build(plan, edp, footprint, before, after, changes, session_dir)


# --------------------------------------------------------------------------
# the reply
# --------------------------------------------------------------------------

def deferred_line(deferred: Iterable[Any]) -> str | None:
    items = []
    for d in deferred:
        if isinstance(d, dict) and d.get("asked"):
            why = str(d.get("why") or "").strip()
            items.append(f"{d['asked']} ({why})" if why else str(d["asked"]))
        elif isinstance(d, str) and d.strip():
            items.append(d.strip())
    return f"Not done this time: {', '.join(items)}." if items else None


def reply_bits(plan: Plan) -> dict[str, Any] | None:
    """What the reply adds for a brain plan: `moments_by` (a content rung
    that ranked the moments), the `deferred` line and the `version` label —
    None for an ordinary plan or when its EDP is unknown here."""
    from ...brain_setting import is_enabled
    did = decisions_id_of(plan)
    if not did or not is_enabled():
        return None
    session_dir = session_for(did)
    edp = load_edp(session_dir, did) if session_dir else None
    content = (edp or {}).get("content_brain") or plan.content_brain
    brain = str((edp or {}).get("brain") or plan.brain)
    from .brain_reply import result_seconds
    summary = (edp or {}).get("summary") or {}
    hook = summary.get("hook") if isinstance(summary.get("hook"), dict) else {}
    return {"moments_by": content if content and content != brain else None,
            "deferred": deferred_line(summary.get("deferred") or []), "deferred_items": list(summary.get("deferred") or []),
            "version": version_label(did), "hook_quote": str(hook.get("quote") or "") or None,
            "length_s": result_seconds(session_dir)}


__all__ = ["DID_RE", "CUT_KINDS", "KEPT_KINDS", "UNEXPLAINED", "CODE_WORDS", "decisions_id_of", "edp_path",
           "load_edp", "register_plan", "remember_version", "version_label", "session_for", "prospective_label",
           "load_footprint", "claimed_clip_ids", "clips_for_entry", "clip_ids_in", "leaf", "scrub_paths", "tally",
           "grouped_cut_line", "build", "card_payload", "deferred_line", "reply_bits"]
