"""What a previewed prompt WOULD change, as lines an editor reads (0.8.0,
"Preview, then apply").

`summarize(before, after)` compares two EDLs and returns `Change`s: one plain
line each, in the words an editor uses, with clip names and timecodes on the
project ruler and before -> after values ("Clip 2 'beach.mp4': speed 1x ->
0.75x (4.0 s -> 5.3 s)", "Deleted 00:00:00:00-00:00:10:00 of the video (2
clips)", "Added transition Fade 0.5 s at 00:00:04:12").

THE ONE PROPERTY THIS MODULE MUST KEEP: IT NEVER OMITS A CHANGE. The diff is
not a list of rules that each look for something they know — a change no
rule knows would be silently missing from the card, and the person would
press Apply on an edit nobody showed them. Instead:

  1. `flat_state(edl)` flattens EVERY user-visible fact of the tree under a
     stable key (`clip:<id>.audio.gain_db`, `track:music.duck.to_db`,
     `trans:v1@4.0`, `canvas.w`, …) — the whole model dump, nothing picked;
  2. `diff_keys(before, after)` is the set of keys whose value differs;
  3. every rule that writes a line CLAIMS the keys it describes, and a
     generic rule writes a line for every key still unclaimed ("Clip 2
     'beach.mp4': blend mode normal -> screen").

The rules live in change_rules.py (`Summary`), the words in change_words.py.
So `covered(summarize(b, a)) ⊇ diff_keys(b, a)` holds by construction, and
tests/test_prompt_preview_changes.py asserts it op by op and on random
mutations. Only two derived fields are outside the diff: `EDL.duration`
(recomputed from the clips on every commit) and `EDL.version` (the schema
version) — both are consequences of the other keys, never an edit.

`canonical(after, before, path_map)` is the dry run's fingerprint: the tree
with the ids a run CREATED renamed in document order and the scratch
directory mapped back to the session's, so the same plan re-run on the live
store (with fresh random clip ids) fingerprints the same.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ...edl.schema import EDL
from .change_words import MISSING, Names, split_parent

#: Lines the card shows before "and N more changes".
DEFAULT_CAP = 12

#: The order groups appear in, the way an editor reads a timeline.
GROUP_ORDER: tuple[str, ...] = ("Video", "Clips", "Look", "Transitions", "Canvas", "Captions",
                                "Text", "Stickers", "Audio", "Tracks", "Markers", "Project", "Follow",
                                "Other")

#: Derived fields, never an edit (see the module docstring).
_DERIVED_TOP = frozenset({"duration", "version"})

#: Fields that are one value however they are shaped (a keyframed number, a
#: speed curve, a mask) — compared and described whole.
_LEAF_FIELDS = frozenset({"speed", "audio.gain_env", "canvas_bg", "framing", "mask", "chromakey",
                          "style.shadow", "effects"})

#: Seconds below which two times are the same (a frame at 60 fps is 0.017).
EPS = 0.01


@dataclass(frozen=True)
class Change:
    group: str
    text: str
    keys: frozenset[str] = frozenset()


@dataclass
class ChangeList:
    """What the card shows: the lines (capped), how many more there are, and
    the full list (what Apply is checked against)."""
    lines: list[str]
    more: int
    total: int
    all_lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"lines": list(self.lines), "more": self.more, "total": self.total}


# --------------------------------------------------------------------------
# 1. the flat state
# --------------------------------------------------------------------------

def _is_leaf(path: str, value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    if "keyframes" in value:
        return True
    return path in _LEAF_FIELDS


def _flatten(prefix: str, value: Any, out: dict[str, Any], rel: str = "") -> None:
    # None is "not set", the same as absent: a caption look going from None
    # to {color: yellow, font: None, …} is ONE change (the colour), not nine.
    if value is None:
        return
    if (_is_leaf(rel, value) and rel) or not isinstance(value, dict):
        out[prefix] = value
        return
    for k, v in value.items():
        sub = f"{rel}.{k}" if rel else k
        _flatten(f"{prefix}.{k}", v, out, sub)


def _round(value: Any) -> Any:
    """Floats rounded to 1e-4: a re-quantised 4.0000000001 is not an edit."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, list):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def flat_state(edl: EDL) -> dict[str, Any]:
    """Every user-visible fact of `edl` under a stable key. Clips, tracks and
    markers are keyed by id (so a moved clip is the same clip), transitions
    by their track and seam."""
    dump = _round(edl.model_dump(mode="json", by_alias=True))
    out: dict[str, Any] = {}
    for k, v in dump.items():
        if k in _DERIVED_TOP or k in ("tracks", "markers"):
            continue
        if k == "canvas":
            _flatten("canvas", v, out)
        elif v is not None:
            out[f"edl.{k}"] = v
    for t in dump.get("tracks") or []:
        tid = t["id"]
        out[f"track:{tid}"] = t.get("type")
        for k, v in t.items():
            if k in ("id", "clips", "transitions"):
                continue
            _flatten(f"track:{tid}.{k}", v, out)
        for tr in t.get("transitions") or []:
            key = f"trans:{tid}@{round(float(tr.get('at', 0.0)), 3)}"
            out[key] = {k: v for k, v in tr.items() if k != "at"}
        for c in t.get("clips") or []:
            cid = c["id"]
            out[f"clip:{cid}"] = tid
            for k, v in c.items():
                if k == "speed" and v == 1.0:
                    continue       # 1x is "no speed change", the same as unset
                if k != "id":
                    _flatten(f"clip:{cid}.{k}", v, out, k)
    for m in dump.get("markers") or []:
        mid = m.get("id") or f"@{m.get('time')}"
        out[f"marker:{mid}"] = True
        for k, v in m.items():
            if k != "id":
                out[f"marker:{mid}.{k}"] = v
    return out


def diff_keys(before: EDL | dict[str, Any], after: EDL | dict[str, Any]) -> set[str]:
    """Every key whose value differs (added, removed or changed)."""
    b = before if isinstance(before, dict) else flat_state(before)
    a = after if isinstance(after, dict) else flat_state(after)
    return {k for k in set(b) | set(a) if b.get(k, MISSING) != a.get(k, MISSING)}


def covered(changes: Iterable[Change]) -> set[str]:
    out: set[str] = set()
    for c in changes:
        out |= set(c.keys)
    return out


# --------------------------------------------------------------------------
# 4. the public API
# --------------------------------------------------------------------------

def summarize(before: EDL, after: EDL, *, session_dir: Path | None = None) -> list[Change]:
    """Every change from `before` to `after`, as editor lines (see the module
    docstring for why nothing can be left out)."""
    from .change_rules import Summary
    return Summary(before, after, session_dir).run()


def change_list(changes: list[Change], cap: int = DEFAULT_CAP) -> ChangeList:
    lines = [c.text for c in changes]
    shown = lines if len(lines) <= cap else lines[: cap - 1]
    return ChangeList(lines=shown, more=len(lines) - len(shown), total=len(lines), all_lines=lines)


def more_line(n: int) -> str:
    return f"and {n} more change{'s' if n != 1 else ''}"


def _new_ids(after: EDL, before: EDL) -> list[str]:
    known = {c.id for t in before.tracks for c in t.clips} | {m.id for m in before.markers}
    seen: list[str] = []
    for t in after.tracks:
        for c in t.clips:
            if c.id not in known:
                seen.append(c.id)
    seen.extend(m.id for m in after.markers if m.id not in known)
    return seen


def canonical(after: EDL, before: EDL, path_map: dict[str, str] | None = None) -> str:
    """A fingerprint of `after` that a re-run of the same plan on `before`
    reproduces: the ids the run created are renamed in document order and
    every path under a scratch directory is mapped to the session's."""
    rename = {cid: f"#new{i}" for i, cid in enumerate(_new_ids(after, before))}
    prefixes = sorted((path_map or {}).items(), key=lambda kv: -len(kv[0]))

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            if v in rename:
                return rename[v]
            for src, dst in prefixes:
                if v.startswith(src):
                    return dst + v[len(src):]
            return v
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    body = walk(after.model_dump(mode="json", by_alias=True))
    body.pop("duration", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:24]


__all__ = ["Change", "ChangeList", "MISSING", "DEFAULT_CAP", "GROUP_ORDER", "flat_state", "diff_keys",
           "covered", "summarize", "change_list", "more_line", "canonical", "split_parent", "Names"]
