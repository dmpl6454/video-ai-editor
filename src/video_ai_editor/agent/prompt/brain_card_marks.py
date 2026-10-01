"""Which decisions a change card may claim, and which line each one explains
(Editor Brain, EB1-F; the FX-E wave's honesty fixes UX-06 / E8).

Two questions, both answered from the DIFF and the frozen footprint — never
from a decision's own say-so:

  * `mark_applied` — a decision is `applied: False` ONLY when its footprint is
    truly absent from the diff. A clip the run created (a split piece), removed
    or changed is in the diff; an opening decision is proven from the tree the
    run leaves (the first clip of the main lane is the hook's clip or plays the
    hook's source seconds). A kept pause is the absence of a cut, never marked.
  * `attribute` — a line's why is the reason of a decision whose footprint the
    line came from: the decision's KIND must be one the line's keys can be the
    work of (a `fit` key is a reframe, an `audio.mute` key is the dialogue
    lane, a `transform` key is a punch-in or a hidden jump cut ...) and, where
    the decision addressed clips, at least one of them must be a clip the line
    names. Repeated words are said once. A line no decision can be tied to is
    said to be unexplained rather than given a borrowed why.

The kind of a key: a line is read by its MOST SPECIFIC key class — a camera
swap also mutes the angle's own sound, and the line is still the camera's.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .changes import Change

_CLIP_KEY = re.compile(r"^clip:([^.]+)")
EPS = 1e-6
#: Decision kinds that address the main lane's structure (cuts, the kept window, the order).
STRUCTURE = frozenset({"cut_range", "keep_window", "open_on"})
_PUNCH = frozenset({"punch_in", "jump_cut_hide"})
#: Canvas keys a reframe owns; the other canvas keys (bitrate, loudness ...) are the export preset's.
_FRAME_KEYS = frozenset({"w", "h", "aspect", "fps", "ratio"})
#: The line's own words override the keys where they say more ("Reordered …" touches every clip's start).
_TEXT_KINDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Camera:", frozenset({"switch_angle"})),
    ("Reordered", frozenset({"open_on"})),
    ("Camera sound muted", frozenset({"dialogue"})),
    ("Dialogue from", frozenset({"dialogue"})),
)


def clip_ids_in(keys: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for k in keys:
        m = _CLIP_KEY.match(k)
        if m:
            out.add(m.group(1))
    return out


def claimed_clip_ids(entry: dict[str, Any]) -> set[str]:
    ids = entry.get("clip_ids")
    if ids is None:
        ids = entry.get("clips")
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 - EPS and b0 < a1 - EPS


def clips_for_entry(entry: dict[str, Any], before: Any, src: str | None) -> set[str]:
    """The v1 clips of the pre-run tree a footprint entry addressed: its
    `clip_ids` when it names them; else the clips whose SOURCE span meets
    `src_range` (a cut, in the source's own seconds — the one clock); else
    the clips under its `timeline` ranges."""
    ids = claimed_clip_ids(entry)
    if ids:
        return ids
    try:
        from ..timemap import media_clips
        clips = list(media_clips(before, "v1"))
    except Exception:  # noqa: BLE001 — no main lane, nothing to tie
        return set()
    rng = entry.get("src_range") or entry.get("kept")
    if isinstance(rng, list) and len(rng) == 2:
        t0, t1 = float(rng[0]), float(rng[1])
        return {c.id for c in clips if (src is None or str(c.src) == src)
                and _overlaps(t0, t1, float(c.in_), float(c.out))}
    spans = entry.get("timeline") or [[t, t] for t in (entry.get("times") or []) if isinstance(t, (int, float))]
    out: set[str] = set()
    for span in spans if isinstance(spans, list) else []:
        if isinstance(span, list) and len(span) == 2:
            a, b = float(span[0]), float(span[1])
            out |= {c.id for c in clips if _overlaps(a, max(b, a + 1e-3), c.start, c.start + c.effective_duration)}
    return out


@dataclass(frozen=True)
class Evidence:
    """What the run left behind: the two trees, the diff's lines, the clips
    the diff touched, and which lane each clip lives on."""
    before: Any
    after: Any
    changes: list[Change]
    changed: frozenset[str]
    owners: dict[str, tuple[str, str]] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    offsets: dict[str, float] = field(default_factory=dict)


def _clip_ids_of(edl: Any) -> set[str]:
    return {c.id for t in edl.tracks for c in t.clips}


def evidence(before: Any, after: Any, changes: list[Change], diff_keys: Iterable[str],
             paths: dict[str, str] | None = None, offsets: dict[str, float] | None = None) -> Evidence:
    """The diff's clip footprint: a clip a diff key names, one the run
    CREATED (a split piece) and one it REMOVED are all clips the diff shows."""
    b, a = _clip_ids_of(before), _clip_ids_of(after)
    owners: dict[str, tuple[str, str]] = {}
    for edl in (before, after):
        for t in edl.tracks:
            for c in t.clips:
                owners[c.id] = (t.id, t.type)
    return Evidence(before, after, list(changes), frozenset(clip_ids_in(diff_keys) | (a - b) | (b - a)),
                    owners, dict(paths or {}), dict(offsets or {}))


# --------------------------------------------------------------------------
# what a line can be the work of
# --------------------------------------------------------------------------

def _key_class(key: str, ev: Evidence) -> tuple[int, frozenset[str]]:
    """(specificity, decision kinds) of one diff key."""
    if key.startswith("canvas."):
        leaf = key.split(".", 1)[1].split(".", 1)[0]
        return 3, frozenset({"reframe"} if leaf in _FRAME_KEYS else {"export_preset"})
    if key.startswith("track:"):
        lane = key[len("track:"):].split(".", 1)[0]
        if lane == "captions":
            return 3, frozenset({"captions"})
        if lane == "music":
            return 3, frozenset({"music"})
        return (3, frozenset({"dialogue"})) if lane.startswith("a") else (0, frozenset())
    m = _CLIP_KEY.match(key)
    if not m:
        return 0, frozenset()
    attr = key[m.end():]
    lane, kind = ev.owners.get(m.group(1), ("", ""))
    if lane == "captions" or kind == "captions":
        return 3, frozenset({"captions"})
    if kind == "music":
        return 3, frozenset({"music"})
    if kind == "audio":
        return 3, frozenset({"dialogue"})
    if attr.startswith(".src"):
        return 4, frozenset({"switch_angle"})
    if attr.startswith(".transform"):
        return 3, _PUNCH
    if attr.startswith(".fit"):
        return 3, frozenset({"reframe"})
    if attr.startswith(".audio.mute"):
        return 2, frozenset({"dialogue"})
    return 1, STRUCTURE


def line_kinds(ch: Change, ev: Evidence) -> frozenset[str]:
    """The decision kinds a line can be explained by; empty = unknown."""
    for prefix, kinds in _TEXT_KINDS:
        if ch.text.startswith(prefix):
            return kinds
    best = 0
    kinds: set[str] = set()
    for key in ch.keys:
        spec, ks = _key_class(key, ev)
        if spec > best:
            best, kinds = spec, set(ks)
        elif spec == best and spec:
            kinds |= ks
    return frozenset(kinds)


def _fits(row: dict[str, Any], kinds: frozenset[str], line_clips: set[str]) -> bool:
    """Does a decision's footprint tie it to this line?"""
    if row["kind"] not in kinds:
        return False
    clips = row.get("_clips") or set()
    return not clips or bool(clips & line_clips)


# --------------------------------------------------------------------------
# E8: applied only with evidence
# --------------------------------------------------------------------------

def _first_v1(after: Any) -> Any | None:
    try:
        from ..timemap import media_clips
        clips = list(media_clips(after, "v1"))
    except Exception:  # noqa: BLE001 — no main lane, no opening
        return None
    return min(clips, key=lambda c: c.start) if clips else None


def _opens_on(row: dict[str, Any], ev: Evidence, path: str | None) -> bool | None:
    """The reel really opens on the decision's moment: the first clip of the
    main lane is a clip the footprint names, or plays the hook's own source
    seconds (reference seconds + the file's sync offset). None when neither
    the footprint nor the source can be read — nothing to prove it with."""
    first = _first_v1(ev.after)
    if first is None:
        return False
    if first.id in row["_clips"]:
        return True
    ref = row.get("_ref") if isinstance(row.get("_ref"), dict) else None
    if not ref or not path:
        return None if not row["_clips"] else False
    if str(first.src) != path:
        return False
    off = ev.offsets.get(path, 0.0)
    t0, t1 = float(ref.get("t0", 0.0)) + off, float(ref.get("t1", 0.0)) + off
    lo, hi = max(t0, float(first.in_)), min(t1, float(first.out))
    return hi - lo >= 0.5 * max(t1 - t0, EPS)


def mark_applied(rows: list[dict[str, Any]], fp: dict[str, dict[str, Any]], ev: Evidence,
                 path_of: Any, kept_kinds: frozenset[str]) -> list[str]:
    """Set `applied` / `note` / `_clips` on every row; return the over-claims."""
    over: list[str] = []
    dropped = fp.get("__dropped__", {})
    present = frozenset().union(*(line_kinds(ch, ev) for ch in ev.changes))     # kinds some diff line can be the work of
    for row in rows:
        entry = fp.get(row["id"], {})
        path = path_of(row)
        row["_clips"] = clips_for_entry(entry, ev.before, path)
        if row["kind"] in kept_kinds:
            row["applied"] = None
            continue
        if row["id"] in dropped:
            row["applied"], row["note"] = False, str(dropped[row["id"]].get("why") or "dropped")
        elif row["kind"] == "open_on":
            proved = _opens_on(row, ev, path)
            row["applied"] = row["kind"] in present if proved is None else proved
            row["note"] = None if row["applied"] else "the video does not open on it"
        elif row["_clips"]:
            row["applied"] = bool(row["_clips"] & ev.changed)
        else:
            row["applied"] = row["kind"] in present
        if row["applied"] is False:
            over.append(row["id"])
    return over


# --------------------------------------------------------------------------
# a why per line
# --------------------------------------------------------------------------

def join_reasons(texts: list[str]) -> str:
    seen = list(dict.fromkeys(t for t in texts if t))
    return "; ".join(seen[:3]) + (f" (+{len(seen) - 3} more)" if len(seen) > 3 else "")


def why_rows(ch: Change, rows: list[dict[str, Any]], ev: Evidence) -> list[dict[str, Any]]:
    """The applied decisions whose footprint this line came from."""
    kinds = line_kinds(ch, ev)
    if not kinds:
        return []
    line_clips = clip_ids_in(ch.keys)
    return [r for r in rows if r["applied"] and _fits(r, kinds, line_clips)]


def attribute(changes: list[Change], rows: list[dict[str, Any]], ev: Evidence,
              unexplained_text: str) -> tuple[list[Change], list[int], list[list[str]]]:
    """(display lines with their why, indices of lines nothing explains, the
    decision ids each why was written from)."""
    out: list[Change] = []
    unexplained: list[int] = []
    ids: list[list[str]] = []
    for i, ch in enumerate(changes):
        hits = why_rows(ch, rows, ev)
        if not hits:
            unexplained.append(i)
            out.append(Change(group=ch.group, text=ch.text, keys=ch.keys, why=unexplained_text))
            ids.append([])
            continue
        out.append(Change(group=ch.group, text=ch.text, keys=ch.keys,
                          why=join_reasons([h["text"] or h["code"] for h in hits])))
        ids.append([h["id"] for h in hits])
    return out, unexplained, ids


def offsets_for(session_dir: Path | None) -> dict[str, float]:
    """{file path: sync offset} from the session's angle group (none when single-camera)."""
    if not session_dir:
        return {}
    try:
        from ...brain import resolve as _resolve
        return dict(_resolve.angle_offsets(Path(session_dir) / "brain"))
    except Exception:  # noqa: BLE001 — a single-camera project has none
        return {}
