"""Where the Editor Brain's files live and how they are written (spec §3.1).

    WORKDIR/analysis/<src_key>/            per SOURCE, shared by every session
      source.json                          {src_key, content_key, leaf, duration, …}
      <layer>/<params>.json                speech | speakers | audio | semantic (| visual | music later)
      refs.json                            the session ids that use this source
    <session>/brain/                       per PROJECT (bundled in a .vae)
      graph/<gid>.json, graph/current.json, scenes.json, angles.json
      decisions/<did>.json                 immutable EDPs, one per brain run
      versions.json, footprint.json        (brain/versions.py, brain/resolve.py)

Keys (brief, frozen): `src_key = sha256(realpath, size, mtime_ns)[:24]` —
the proxy rule without the recipe; `content_key = sha256(size + 1 MiB head
+ 1 MiB tail)` — the artefact rule, so a .vae re-import (new realpath, same
bytes) can find its analysis through `find_by_content`.

Every write is atomic: the text goes to a temp file beside the target (where
ENOSPC lands), then `os.replace`; a failed write leaves the target as it was
and no temp behind. A payload over `MAX_LAYER_BYTES` (32 MB) / `MAX_GRAPH_BYTES`
(64 MB) is refused before anything touches disk (`TooLarge`), and a file
over the cap on disk is refused on read — a layer that fails validation or
the cap is `failed:<reason>`, never partially trusted.

`WORKDIR` is read from `config` on every call (tests monkeypatch it).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .. import config as _config
from . import schema as S
from .schema import MAX_GRAPH_BYTES, MAX_LAYER_BYTES

_SAMPLE_BYTES = 1024 * 1024
_KEY_RE = re.compile(r"^[0-9a-f]{24}$")
_PARAMS_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_EDP_ID_RE = re.compile(S.EDP_ID_RE)
_GID_RE = re.compile(S.GRAPH_ID_RE)
LAYER_MODELS: dict[str, type[BaseModel]] = {
    "speech": S.SpeechLayer, "audio": S.AudioLayer, "speakers": S.SpeakersLayer, "semantic": S.SemanticLayer,
}


_TMP_COUNTER = itertools.count()


def _tmp_name(name: str) -> str:
    """A temp name no other writer of this file can share: process + thread + a counter."""
    return f".{name}.{os.getpid()}.{threading.get_ident()}.{next(_TMP_COUNTER)}.tmp"


class TooLarge(ValueError):
    """A layer over 32 MB or a graph over 64 MB (spec §3.1)."""


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

def src_key(path: str | os.PathLike) -> str:
    """Identity key: realpath + size + mtime_ns, 24 hex. Raises OSError when
    the file is missing (a key of nothing is not a key)."""
    real = os.path.realpath(os.fspath(path))
    st = os.stat(real)
    ident = f"{real}\0{st.st_size}\0{st.st_mtime_ns}"
    return hashlib.sha256(ident.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def content_key(path: str | os.PathLike) -> str:
    """Content key: size + the first and last MiB — never the path and, unlike
    `artefacts.file_identity`, never the mtime, so a .vae re-import (new
    path, new mtime, same bytes) finds its analysis. `missing` / `unreadable`
    when the file cannot be read."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return "missing"
    h = hashlib.sha256(f"{st.st_size}|".encode())
    try:
        with open(p, "rb") as fh:
            h.update(fh.read(_SAMPLE_BYTES))
            if st.st_size > 2 * _SAMPLE_BYTES:
                fh.seek(-_SAMPLE_BYTES, os.SEEK_END)
                h.update(fh.read(_SAMPLE_BYTES))
    except OSError:
        return "unreadable"
    return h.hexdigest()


def bare_key(key: str) -> str:
    """`src_<24hex>` or `<24hex>` → the 24 hex chars the directory is named by."""
    k = key.removeprefix("src_")
    if not _KEY_RE.fullmatch(k):
        raise ValueError(f"not a source key: {key!r}")
    return k


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def analysis_root(*, workdir: Path | None = None) -> Path:
    return Path(workdir if workdir is not None else _config.WORKDIR) / "analysis"


def analysis_dir(key: str, *, workdir: Path | None = None) -> Path:
    return analysis_root(workdir=workdir) / bare_key(key)


def layer_path(key: str, layer: str, params: str, *, workdir: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-z]{2,16}", layer) or not _PARAMS_RE.fullmatch(params):
        raise ValueError(f"bad layer name/params: {layer!r} {params!r}")
    return analysis_dir(key, workdir=workdir) / layer / f"{params}.json"


def brain_dir(session_dir: Path | str) -> Path:
    return Path(session_dir) / "brain"


def edp_path(session_dir: Path | str, did: str) -> Path:
    if not _EDP_ID_RE.fullmatch(str(did)):
        raise ValueError(f"not a decisions id: {did!r}")
    return brain_dir(session_dir) / "decisions" / f"{did}.json"


def graph_path(session_dir: Path | str, gid: str) -> Path:
    if not _GID_RE.fullmatch(str(gid)):
        raise ValueError(f"not a graph id: {gid!r}")
    return brain_dir(session_dir) / "graph" / f"{gid}.json"


# --------------------------------------------------------------------------
# atomic JSON
# --------------------------------------------------------------------------

def _dump(data: Any) -> str:
    if isinstance(data, BaseModel):
        return S.canonical_json(data)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_json(path: Path, data: Any, *, cap: int | None = None, what: str = "file") -> Path:
    """Atomic write. `cap` refuses the payload BEFORE touching disk."""
    text = _dump(data)
    size = len(text.encode("utf-8"))
    if cap is not None and size > cap:
        raise TooLarge(f"{what} is {size / 1048576:.1f} MB; the cap is {cap // 1048576} MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(_tmp_name(path.name))
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_json(path: Path, *, cap: int | None = None, what: str = "file") -> Any:
    """Reads `path`; None when absent. Refuses a file over `cap` on disk."""
    if not path.is_file():
        return None
    if cap is not None and path.stat().st_size > cap:
        raise TooLarge(f"{what} {path.name} is over the {cap // 1048576} MB cap")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# the analysis store (per source)
# --------------------------------------------------------------------------

def write_source(key: str, source: dict[str, Any], *, workdir: Path | None = None) -> Path:
    return write_json(analysis_dir(key, workdir=workdir) / "source.json", source, cap=MAX_LAYER_BYTES, what="source")


def read_source(key: str, *, workdir: Path | None = None) -> dict[str, Any] | None:
    return read_json(analysis_dir(key, workdir=workdir) / "source.json", cap=MAX_LAYER_BYTES)


def find_by_content(ck: str, *, workdir: Path | None = None) -> str | None:
    """The identity key whose `source.json` records content key `ck`, or
    None — how a .vae re-import finds the analysis of the same bytes."""
    root = analysis_root(workdir=workdir)
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir()):
        if not _KEY_RE.fullmatch(d.name):
            continue
        try:
            src = read_json(d / "source.json", cap=MAX_LAYER_BYTES)
        except (OSError, ValueError):
            continue
        if isinstance(src, dict) and src.get("content_key") == ck:
            return d.name
    return None


def write_layer(key: str, layer: str, params: str, data: Any, *, workdir: Path | None = None) -> Path:
    """Validate against the layer's model when one exists, then write."""
    model = LAYER_MODELS.get(layer)
    payload = model.model_validate(data) if model is not None and not isinstance(data, BaseModel) else data
    return write_json(layer_path(key, layer, params, workdir=workdir), payload, cap=MAX_LAYER_BYTES,
                      what=f"{layer} layer")


def read_layer(key: str, layer: str, params: str, *, workdir: Path | None = None) -> Any:
    """The raw JSON of a layer file, None when absent; `TooLarge` over the cap."""
    return read_json(layer_path(key, layer, params, workdir=workdir), cap=MAX_LAYER_BYTES, what=f"{layer} layer")


def load_layer(key: str, layer: str, params: str, *, workdir: Path | None = None) -> BaseModel | None:
    """The validated layer, or None when it is missing or fails (a failed
    layer is treated as missing — `layer_status` says why)."""
    model = LAYER_MODELS.get(layer)
    try:
        raw = read_layer(key, layer, params, workdir=workdir)
    except (OSError, ValueError):
        return None
    if raw is None or model is None:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


def layer_status(key: str, layer: str, params: str, *, workdir: Path | None = None) -> str:
    """`ok` | `missing` | `failed:size` | `failed:json` | `failed:schema`."""
    p = layer_path(key, layer, params, workdir=workdir)
    if not p.is_file():
        return "missing"
    try:
        raw = read_json(p, cap=MAX_LAYER_BYTES)
    except TooLarge:
        return "failed:size"
    except (OSError, ValueError):
        return "failed:json"
    model = LAYER_MODELS.get(layer)
    if model is None:
        return "ok"
    try:
        model.model_validate(raw)
    except ValidationError:
        return "failed:schema"
    return "ok"


def read_refs(key: str, *, workdir: Path | None = None) -> list[str]:
    raw = read_json(analysis_dir(key, workdir=workdir) / "refs.json", cap=MAX_LAYER_BYTES)
    return list(raw.get("sessions", [])) if isinstance(raw, dict) else []


def add_ref(key: str, session_id: str, *, workdir: Path | None = None) -> list[str]:
    refs = read_refs(key, workdir=workdir)
    if session_id not in refs:
        refs = [*refs, session_id]
        write_json(analysis_dir(key, workdir=workdir) / "refs.json", {"sessions": refs})
    return refs


# --------------------------------------------------------------------------
# the session's brain/
# --------------------------------------------------------------------------

def write_graph(session_dir: Path | str, graph: S.Graph) -> Path:
    return write_json(graph_path(session_dir, graph.id), graph, cap=MAX_GRAPH_BYTES, what="graph")


def read_graph(session_dir: Path | str, gid: str) -> S.Graph | None:
    raw = read_json(graph_path(session_dir, gid), cap=MAX_GRAPH_BYTES, what="graph")
    return S.Graph.model_validate(raw) if raw is not None else None


def graph_speech_status(raw: Any) -> str | None:
    """`ok` / `missing` / `empty` — the REFERENCE source's speech layer as a graph header states it; None when the
    header does not say (a hand-made or foreign shape)."""
    if not isinstance(raw, dict):
        return None
    for src in raw.get("sources") or []:
        if isinstance(src, dict) and src.get("key") == raw.get("reference") and isinstance(src.get("layers"), dict):
            v = src["layers"].get("speech")
            return v if v in ("ok", "missing", "partial", "empty") else None
    return None


def set_current_graph(session_dir: Path | str, gid: str) -> Path:
    """The session's CURRENT graph — the one a fresh EDP must name. The pointer also remembers whether the
    graph was made without the speech layer, so `current_graph_id` can retire it when the transcript lands."""
    if not _GID_RE.fullmatch(str(gid)):
        raise ValueError(f"not a graph id: {gid!r}")
    body: dict[str, Any] = {"id": gid}
    try:
        status = graph_speech_status(read_json(graph_path(session_dir, gid), cap=MAX_GRAPH_BYTES, what="graph"))
    except (OSError, ValueError):
        status = None
    if status is not None:
        body["speech"] = status
    return write_json(brain_dir(session_dir) / "graph" / "current.json", body)


def _transcript_landed(session_dir: Path | str, gid: str) -> bool:
    """True when a source the graph read now has a transcript on disk."""
    from .analysis import transcripts as _tr
    try:
        raw = read_json(graph_path(session_dir, gid), cap=MAX_GRAPH_BYTES, what="graph")
    except (OSError, ValueError):
        return False
    for src in (raw or {}).get("sources") or []:
        path = src.get("path") if isinstance(src, dict) else None
        if path and src.get("has_audio", True) and _tr.read_transcript(path) is not None:
            return True
    return False


def current_graph_id(session_dir: Path | str) -> str | None:
    """The pinned graph, or None. A graph pinned WITHOUT its speech layer is retired the moment a transcript
    exists for its footage — it was made before the words were there and would plan from nothing."""
    raw = read_json(brain_dir(session_dir) / "graph" / "current.json")
    gid = raw.get("id") if isinstance(raw, dict) else None
    if not (isinstance(gid, str) and _GID_RE.fullmatch(gid)):
        return None
    speech = raw.get("speech")
    if speech is None:
        try:
            speech = graph_speech_status(read_json(graph_path(session_dir, gid), cap=MAX_GRAPH_BYTES, what="graph"))
        except (OSError, ValueError):
            speech = None
    if speech == "missing" and _transcript_landed(session_dir, gid):
        return None
    return gid


def read_angles(session_dir: Path | str) -> S.Angles | None:
    raw = read_json(brain_dir(session_dir) / "angles.json", cap=MAX_LAYER_BYTES)
    return S.Angles.model_validate(raw) if raw is not None else None


def write_edp(session_dir: Path | str, edp: S.EDP) -> Path:
    """Immutable: writing the same bytes again is fine, different bytes
    under an existing id is refused (`FileExistsError`)."""
    p = edp_path(session_dir, edp.id)
    text = S.canonical_json(edp)
    if p.is_file():
        if p.read_text(encoding="utf-8") == text:
            return p
        raise FileExistsError(f"decisions {edp.id} already exist with different content")
    return write_json(p, edp, cap=MAX_GRAPH_BYTES, what="decisions")


def read_edp(session_dir: Path | str, did: str) -> S.EDP | None:
    raw = read_json(edp_path(session_dir, did), cap=MAX_GRAPH_BYTES, what="decisions")
    return S.EDP.model_validate(raw) if raw is not None else None


def list_edps(session_dir: Path | str) -> list[str]:
    d = brain_dir(session_dir) / "decisions"
    return sorted(p.stem for p in d.glob("d_*.json")) if d.is_dir() else []


__all__ = [
    "TooLarge", "LAYER_MODELS", "src_key", "content_key", "bare_key", "analysis_root", "analysis_dir", "layer_path",
    "brain_dir", "edp_path", "graph_path", "write_json", "read_json", "write_source", "read_source",
    "find_by_content", "write_layer", "read_layer", "load_layer", "layer_status", "read_refs", "add_ref",
    "write_graph", "read_graph", "set_current_graph", "current_graph_id", "graph_speech_status", "read_angles", "write_edp", "read_edp",
    "list_edps",
]
