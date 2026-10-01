"""The Editor Brain's part of a `.vae` (EB1 fix EX-03).

A brain project is more than its timeline: `<session>/brain/` holds the
versions list, the frozen decisions (EDPs) every `prompt` op names, the
current graph's header, the angle group, the footprint the change card reads,
and `snapshots/pinned/` holds the snapshots a pinned version parked there.
Without them a saved-then-opened project has no versions, a prompt op that
points at a missing EDP, a Plan tab that 404s and a V1 that lost its pin.

WHAT TRAVELS (a whitelist, by exact shape — anything else in `brain/` is not
ours and is neither written nor restored):

    brain/versions.json  footprint.json  scenes.json  angles.json  timings.jsonl
    brain/decisions/d_<8 hex>.json
    brain/graph/g_<12 hex>.json  and  brain/graph/current.json
    snapshots/pinned/<NNNNN>_<hash>.json

WHAT IS LEFT OUT, and said in the manifest (`manifest["brain"]`): the
per-source analysis layers under `WORKDIR/analysis/<source key>/` (speech,
speakers, audio, semantic — large, shared by every session, and re-derived
from the media on the next analysis), and any file over the per-file cap or
past the total cap. The reopened project therefore READS (Plan tab, versions,
restore, the EDP resolving against its own graph header) and asks for the
footage to be read again only when a NEW edit needs the layers.

ON OPEN every entry is confined the way media is: a symlink entry is refused,
names outside the whitelist are ignored, each file is size-capped and must be
JSON of the shape this app writes (a decisions/graph file names its own id),
every absolute path inside a brain file that is not the new session's own
(after the bundle's media was re-pointed) or an app asset becomes an offline
placeholder under `uploads/imported/_missing/`, and the versions' snapshot
hashes are recomputed for the new paths (the EDL hash covers `src`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import zipfile
from pathlib import Path
from typing import Any, Callable

from .agent.prompt.schema import PLAN_REF_PATTERN
from .brain.schema import MAX_GRAPH_BYTES

_log = logging.getLogger("video_ai_editor")

#: One brain file may be at most one graph's cap, all of them together twice that.
BRAIN_FILE_CAP = MAX_GRAPH_BYTES
BRAIN_TOTAL_CAP = 2 * MAX_GRAPH_BYTES

_TOP = ("versions.json", "footprint.json", "scenes.json", "angles.json", "timings.jsonl")
_EDP_RE = re.compile(rf"^decisions/({PLAN_REF_PATTERN.strip('^$')})\.json$")
_GRAPH_RE = re.compile(r"^graph/(g_[0-9a-f]{12})\.json$")
_PINNED_RE = re.compile(r"^\d{5}_[0-9a-f]{1,64}\.json$")
LEFT_OUT_LAYERS = ("the per-source analysis layers (speech, speakers, audio, semantic) are not bundled: "
                   "the footage is read again when a new edit needs them")


def _rel_ok(rel: str) -> bool:
    return rel in _TOP or rel == "graph/current.json" or bool(_EDP_RE.match(rel) or _GRAPH_RE.match(rel))


def _brain_files(bdir: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for p in sorted(bdir.rglob("*")) if bdir.is_dir() else []:
        rel = p.relative_to(bdir).as_posix()
        if p.is_file() and not p.is_symlink() and _rel_ok(rel):
            out.append((rel, p))
    return out


def brain_entries(sd: Path) -> tuple[list[tuple[Path, str]], dict[str, Any]]:
    """`[(file, arcname)]` for the session's brain state, and the manifest
    section saying what went in and what did not."""
    entries: list[tuple[Path, str]] = []
    left_out: list[dict[str, str]] = []
    total = 0
    for rel, p in _brain_files(sd / "brain"):
        size = p.stat().st_size
        if size > BRAIN_FILE_CAP or total + size > BRAIN_TOTAL_CAP:
            left_out.append({"name": f"brain/{rel}", "why": "over the size cap"})
            continue
        total += size
        entries.append((p, f"brain/{rel}"))
    pinned = sd / "snapshots" / "pinned"
    for p in sorted(pinned.glob("*.json")) if pinned.is_dir() else []:
        if _PINNED_RE.match(p.name) and p.is_file() and not p.is_symlink():
            entries.append((p, f"snapshots/pinned/{p.name}"))
    report = {"files": len(entries), "bytes": total, "left_out": left_out, "not_bundled": [LEFT_OUT_LAYERS]}
    return entries, report


# --------------------------------------------------------------------------
# on open
# --------------------------------------------------------------------------

def refuse_unsafe_entries(infos: list[zipfile.ZipInfo]) -> None:
    """A symlink entry under `brain/` or `snapshots/pinned/` is refused: a
    project file never carries one, and `zipfile` would write its target text
    as a plain file the state restore then trusted."""
    for i in infos:
        name = i.filename.replace("\\", "/")
        if name.startswith(("brain/", "snapshots/pinned/")) and stat.S_ISLNK(i.external_attr >> 16):
            raise ValueError(f"unsafe entry in project file: {i.filename!r} is a symbolic link")


def _shape_ok(rel: str, data: Any) -> bool:
    if rel == "versions.json":
        from .edl.snapshot import version_row_ok
        return isinstance(data, dict) and isinstance(data.get("versions"), list) \
            and all(version_row_ok(r) for r in data["versions"])
    if rel in ("footprint.json", "angles.json", "graph/current.json") or _GRAPH_RE.match(rel) or _EDP_RE.match(rel):
        ok = isinstance(data, dict)
    else:
        ok = isinstance(data, (dict, list))
    m = _EDP_RE.match(rel) or _GRAPH_RE.match(rel)
    return ok and (m is None or data.get("id") == m.group(1))


def _confine(node: Any, inside: Callable[[str], bool], placeholder: Callable[[str], str]) -> Any:
    """Every absolute path (string value or dict key) that `inside` refuses
    becomes `placeholder(path)`; the rest is returned as it was."""
    def fix(v: str) -> str:
        return v if (not os.path.isabs(v) or "\n" in v or len(v) > 4096 or inside(v)) else placeholder(v)
    if isinstance(node, str):
        return fix(node)
    if isinstance(node, list):
        return [_confine(x, inside, placeholder) for x in node]
    if isinstance(node, dict):
        return {fix(k) if isinstance(k, str) else k: _confine(v, inside, placeholder) for k, v in node.items()}
    return node


def restore_brain(unpack: Path, sd: Path, remap: Callable[[str], str], inside: Callable[[str], bool],
                  placeholder: Callable[[str], str]) -> list[str]:
    """Write the archive's brain files into the new session. Returns what was
    skipped (for the log). `remap` re-points bundled media paths, `inside`
    says whether an absolute path belongs to the new session / app assets."""
    skipped: list[str] = []
    total = 0
    for rel, src in _brain_files(unpack / "brain"):
        size = src.stat().st_size
        if size > BRAIN_FILE_CAP or total + size > BRAIN_TOTAL_CAP:
            skipped.append(f"brain/{rel}: over the size cap")
            continue
        text = remap(src.read_text(encoding="utf-8", errors="replace"))
        if rel.endswith(".jsonl"):
            body = text
        else:
            try:
                data = json.loads(text)
            except ValueError:
                skipped.append(f"brain/{rel}: not JSON")
                continue
            if not _shape_ok(rel, data):
                skipped.append(f"brain/{rel}: not the shape this app writes")
                continue
            body = json.dumps(_confine(data, inside, placeholder), separators=(",", ":"), ensure_ascii=False)
        total += size
        out = sd / "brain" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
    _drop_dangling_current(sd)
    for msg in skipped:
        _log.warning("load_project: %s — left out", msg)
    return skipped


def _drop_dangling_current(sd: Path) -> None:
    """`graph/current.json` naming a graph file that did not travel points at nothing."""
    cur = sd / "brain" / "graph" / "current.json"
    if not cur.is_file():
        return
    try:
        gid = str(json.loads(cur.read_text(encoding="utf-8")).get("id"))
    except ValueError:
        gid = ""
    if not (sd / "brain" / "graph" / f"{gid}.json").is_file():
        cur.unlink(missing_ok=True)


def remap_text(text: str, src_remap: dict[str, str]) -> str:
    """`text` with every bundled media path re-pointed to the new session's."""
    for old, new in src_remap.items():
        # Both escapings: json.dumps writes \uXXXX for non-ASCII while
        # pydantic's model_dump_json (edl.json, snapshots) writes UTF-8.
        for ascii_only in (True, False):
            text = text.replace(json.dumps(old, ensure_ascii=ascii_only)[1:-1],
                                json.dumps(new, ensure_ascii=ascii_only)[1:-1])
    return text


def restore_all(unpack: Path, sd: Path, src_remap: dict[str, str], *, app_asset: Callable[[Path], bool],
                vet: Callable[[str, str], str | None], edl_of: Callable[[str], Any]) -> None:
    """Brain files, pinned snapshots, then the versions' hashes."""
    root = sd.resolve()

    def inside(v: str) -> bool:
        try:
            p = Path(v).resolve()
        except (OSError, ValueError):
            return False
        return p.is_relative_to(root) or app_asset(p)

    def placeholder(v: str) -> str:
        leaf = re.split(r"[\\/]", v.rstrip("\\/"))[-1] or "media"
        return str(sd / "uploads" / "imported" / "_missing" / leaf)

    def remap(text: str) -> str:
        return remap_text(text, src_remap)

    def readable(name: str, text: str) -> str | None:
        try:
            edl_of(text)
        except Exception:  # noqa: BLE001 — any unreadable timeline is dropped, never trusted
            _log.warning("load_project: dropping %s — not a readable timeline", name)
            return None
        return vet(name, text)

    def hash_of(text: str) -> str | None:
        try:
            return edl_of(text).hash()
        except Exception:  # noqa: BLE001
            return None

    restore_brain(unpack, sd, remap, inside, placeholder)
    restore_pinned(unpack, sd, remap, readable)
    rehash_versions(sd, hash_of)


def restore_pinned(unpack: Path, sd: Path, remap: Callable[[str], str], vet: Callable[[str, str], str | None]) -> None:
    """`snapshots/pinned/*.json`, remapped and vetted like any snapshot; an
    unreadable one is dropped (its version then says 'no longer restorable')."""
    src_dir = unpack / "snapshots" / "pinned"
    for p in sorted(src_dir.glob("*.json")) if src_dir.is_dir() else []:
        if not _PINNED_RE.match(p.name) or p.is_symlink() or not p.is_file():
            continue
        text = vet(f"snapshots/pinned/{p.name}", remap(p.read_text(encoding="utf-8")))
        if text is None:
            continue
        out = sd / "snapshots" / "pinned" / p.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def rehash_versions(sd: Path, hash_of: Callable[[str], str | None]) -> None:
    """The EDL hash covers every `src`, so after the media was re-pointed each
    version's `edl_hash` (and its snapshot's file name, which `versions.py`
    builds from it) is recomputed from the restored snapshot. `hash_of(text)`
    is None for an unreadable snapshot: that row is left as it is."""
    vp = sd / "brain" / "versions.json"
    if not vp.is_file():
        return
    try:
        doc = json.loads(vp.read_text(encoding="utf-8"))
        rows = doc["versions"]
    except (ValueError, KeyError, TypeError):
        return
    changed = False
    for r in rows:
        try:
            name = f"{int(r['op_seq']) + 1:05d}_{r['edl_hash']}.json"
        except (KeyError, TypeError, ValueError):
            continue
        for folder in (sd / "snapshots", sd / "snapshots" / "pinned"):
            snap = folder / name
            if not snap.is_file():
                continue
            new = hash_of(snap.read_text(encoding="utf-8"))
            if new and new != r["edl_hash"]:
                target = folder / f"{int(r['op_seq']) + 1:05d}_{new}.json"
                os.replace(snap, target)
                r["edl_hash"] = new
                changed = True
            break
    if changed:
        vp.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False), encoding="utf-8")


__all__ = ["BRAIN_FILE_CAP", "BRAIN_TOTAL_CAP", "LEFT_OUT_LAYERS", "brain_entries", "refuse_unsafe_entries",
           "remap_text", "restore_all", "restore_brain", "restore_pinned", "rehash_versions"]
