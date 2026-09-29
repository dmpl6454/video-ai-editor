"""The dry run's derived artefacts, carried to Apply (final QA r4).

PREVIEW OF A HEAVY PLAN USED TO RUN THE WORK TWICE. "Add captions" on a
10-minute clip transcribed it on the scratch copy for the card, then Apply
transcribed it again for real; a reframe, a denoise or a background removal
did the same. The dry run may not write into the live session (preview.py's
byte-identical rule: files, EDL, history and undo depth unchanged), so what
it derived was thrown away with the scratch copy.

This module is the one place the two runs share: a CONTENT-ADDRESSED cache
under the preview area, `<workdir>/.prompt_preview/artefacts/<kind>/<key>/`,
keyed by (tool, the identity of the input file, the parameters, the model
version). The dry run WRITES there, Apply READS from it when the key matches
and derives again otherwise. Nothing here is referenced from a session: an
entry is a self-contained copy, so an evicted or half-written entry is a
miss, never a dangling path.

Two adapters:

  * `cached_transcribe` — the whisper pass of `transcribe` and `auto_caption`
    (`ingest.transcribe.transcribe`). Key: the source file's identity, the
    resolved model, language, task and the backend's version (faster-whisper
    release + batch size, or the ggml file and the whisper-cli binary). A hit
    hands back the same `Transcript`; the handler then writes ingest.json
    exactly as it would after a real pass.
  * `StepCarry` — the tools that replace a clip's `src` with a render in the
    session cache (`DERIVED_TOOLS`: noise_reduce, remove_background,
    stabilize, smooth_slow_motion, upscale, auto_reframe). Their helpers name
    the output by a content hash and return it when it already exists, so
    the dry run captures the files a step created under `<scratch>/cache/`
    and Apply lays them into `<live>/cache/` at the same relative paths
    before the handler runs. Key: the clip's source identity + the step's
    other args.

WHEN IT IS ON. Only inside a prompt run, through `active()`: the dry run
reads and writes, an Apply only reads. Every other transcription and every
direct `/dispatch` call is untouched (a `force` re-transcribe stays a real
one). Reads touch the entry; `render/cache_budget.enforce_artefacts` trims
the whole area LRU to `VAI_PROMPT_ARTEFACT_CACHE_MB` after every write, so a
cancelled or dropped card leaves entries that age out, and nothing else.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ... import platformutil as _pu

#: The area under `preview.PREVIEW_DIR` (never inside a session).
ARTEFACT_DIR = "artefacts"
#: The whisper adapter's kind and its marker file.
WHISPER_KIND = "whisper"
TRANSCRIPT_FILE = "transcript.json"
#: The derived-files adapter's kind and its marker file (written LAST: an
#: entry without it is incomplete and is a miss).
DERIVED_KIND = "derived"
MANIFEST_FILE = "manifest.json"
#: Tools whose handler replaces `clip.src` with a render under
#: `<session>/cache/` named by a content hash (`agent/dispatch._record_derived`).
DERIVED_TOOLS: frozenset[str] = frozenset({
    "noise_reduce", "remove_background", "stabilize", "smooth_slow_motion", "upscale", "auto_reframe"})
#: Bump when what an entry holds changes shape.
FORMAT_VERSION = "1"
#: Bytes sampled from each end of an input file for its identity.
_SAMPLE_BYTES = 1 << 20
#: A working directory of a helper (`rembg_work_<h>`, `esrgan_work_<h>`) is
#: intermediate frames, never an artefact.
_WORK_MARK = "_work_"


@dataclass(frozen=True)
class Cache:
    root: Path
    write: bool


_ACTIVE: ContextVar[Cache | None] = ContextVar("vai_prompt_artefacts", default=None)


def root_for(live_dir: str | os.PathLike) -> Path:
    """`<workdir>/.prompt_preview/artefacts` for a LIVE session dir."""
    from .preview import PREVIEW_DIR
    return Path(live_dir).parent / PREVIEW_DIR / ARTEFACT_DIR


@contextlib.contextmanager
def active(root: Path, *, write: bool) -> Iterator[Cache]:
    """Turn the cache on for the calling thread's prompt run."""
    cache = Cache(root=Path(root), write=bool(write))
    token = _ACTIVE.set(cache)
    try:
        yield cache
    finally:
        _ACTIVE.reset(token)


def for_mode(live_dir: str | os.PathLike, mode: str):
    """The context a run thread enters: the dry run writes, Apply reads, a
    plain run (setting off) never touches the area."""
    if mode == "preview":
        return active(root_for(live_dir), write=True)
    if mode == "apply":
        return active(root_for(live_dir), write=False)
    return contextlib.nullcontext(None)


def current() -> Cache | None:
    return _ACTIVE.get()


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

def file_identity(path: str | os.PathLike) -> str:
    """What makes an input file "the same file": its size, mtime and a hash
    of its first and last megabyte — never its path (two projects on one
    upload share the artefact) and never a full read of a 2 GB source. A
    re-encode, an append or a replaced upload all change it."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return "missing"
    h = hashlib.sha256()
    h.update(f"{st.st_size}|{st.st_mtime_ns}|".encode())
    try:
        with open(p, "rb") as fh:
            h.update(fh.read(_SAMPLE_BYTES))
            if st.st_size > 2 * _SAMPLE_BYTES:
                fh.seek(-_SAMPLE_BYTES, os.SEEK_END)
                h.update(fh.read(_SAMPLE_BYTES))
    except OSError:
        return "unreadable"
    return h.hexdigest()


def make_key(tool: str, inputs: list[str | os.PathLike], params: dict[str, Any], version: str) -> str:
    body = {"format": FORMAT_VERSION, "tool": tool, "inputs": [file_identity(p) for p in inputs],
            "params": params, "version": version}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def whisper_version(model: str, backend: str | None = None) -> str:
    """The backend that would run `model`, as a string that changes when its
    output could: the faster-whisper release and batch size, or the ggml
    file and the whisper-cli binary. Cheap stats only — never a model load."""
    from ...ingest import transcribe as _T
    be = backend or os.environ.get("WHISPER_BACKEND") or "auto"
    parts: list[str] = []
    try:
        if _T._whisper_cpp_available():
            mp = _T._whisper_cpp_model_path(model)
            if be == "whisper_cpp" or (be == "auto" and mp.exists()):
                parts.append("whisper_cpp")
                for p in (mp, Path(_T._WHISPER_CPP_BIN)):
                    try:
                        st = p.stat()
                        parts.append(f"{p.name}={st.st_size}:{st.st_mtime_ns}")
                    except OSError:
                        parts.append(f"{p.name}=missing")
                return "|".join(parts)
    except Exception:  # noqa: BLE001 — a probe must never fail a transcription
        pass
    try:
        import importlib.metadata as _md
        ver = _md.version("faster-whisper")
    except Exception:  # noqa: BLE001
        ver = "unknown"
    try:
        batch = _T._batch_size()
    except Exception:  # noqa: BLE001
        batch = "?"
    return f"faster_whisper={ver}|batch={batch}"


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------

def entry_dir(cache: Cache, kind: str, key: str) -> Path:
    return cache.root / kind / key


def _marker(kind: str) -> str:
    return TRANSCRIPT_FILE if kind == WHISPER_KIND else MANIFEST_FILE


def _touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _drop_entry(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)


def _write_json_atomic(path: Path, body: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    _pu.replace_with_retry(tmp, path)


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _enforce(cache: Cache, protect: Path) -> None:
    from ...render import cache_budget
    cache_budget.enforce_artefacts(cache.root, protect=[protect])


# --------------------------------------------------------------------------
# the whisper adapter
# --------------------------------------------------------------------------

def transcript_key(src: Path, *, model: str, language: str | None, task: str, backend: str | None) -> str:
    return make_key(WHISPER_KIND, [src],
                    {"model": model, "language": language or "", "task": task},
                    whisper_version(model, backend))


def cached_transcribe(fn: Callable[..., Any], src: Path, *, language: str | None = None,
                      model_size: str | None = None, backend: str | None = None,
                      task: str = "transcribe", on_progress: Callable | None = None,
                      should_cancel: Callable[[], bool] | None = None) -> Any:
    """`fn(src, …)` — `ingest.transcribe.transcribe` or a test's stand-in —
    through the cache when a prompt run has it on. The key uses the model
    `transcribe()` would really run (`model_for`), so the card and Apply
    name the same pass."""
    from ...ingest import transcribe as _T
    cache = current()
    kwargs: dict[str, Any] = dict(language=language, model_size=model_size, backend=backend, task=task,
                                  on_progress=on_progress, should_cancel=should_cancel)
    if cache is None:
        return fn(src, **kwargs)
    model = _T.model_for(model_size, task)
    key = transcript_key(Path(src), model=model, language=language, task=task, backend=backend)
    d = entry_dir(cache, WHISPER_KIND, key)
    marker = d / TRANSCRIPT_FILE
    if marker.is_file():
        try:
            body = json.loads(marker.read_text(encoding="utf-8"))
            tx = _T.Transcript.model_validate(body["transcript"])
        except Exception:  # noqa: BLE001 — a torn or foreign entry (or pydantic's ValidationError): a miss
            _drop_entry(d)
        else:
            _touch(marker)
            if on_progress is not None:
                on_progress(1.0, float(tx.duration), float(tx.duration))
            return tx
    tx = fn(src, **kwargs)
    if cache.write:
        try:
            d.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(marker, {"transcript": tx.model_dump(),
                                        "meta": {"model": model, "language": language or "", "task": task,
                                                 "src": str(src), "at": time.time()}})
            _enforce(cache, d)
        except Exception:  # noqa: BLE001 — a cache that cannot be written must not fail the step
            _drop_entry(d)
    return tx


# --------------------------------------------------------------------------
# the derived-files adapter
# --------------------------------------------------------------------------

def _cache_listing(cache_dir: Path) -> set[str]:
    """Relative paths of the regular files under a session's `cache/`, less
    dot files, `.part` stages and helpers' working directories."""
    out: set[str] = set()
    if not cache_dir.is_dir():
        return out
    for p in cache_dir.rglob("*"):
        rel = p.relative_to(cache_dir)
        if any(part.startswith(".") or _WORK_MARK in part for part in rel.parts):
            continue
        try:
            if p.is_file():
                out.add(rel.as_posix())
        except OSError:
            continue
    return out


def derived_inputs(store: Any, tool: str, args: dict[str, Any]) -> list[str] | None:
    """The source file(s) a derived step reads: the named clip's `src`, or
    every v1 media clip's for `auto_reframe`. None = not a media clip."""
    from ...edl.schema import Clip
    if tool == "auto_reframe":
        v1 = store.edl.get_track("v1")
        srcs = [str(c.src) for c in (v1.clips if v1 else []) if isinstance(c, Clip)]
        return srcs or None
    cid = args.get("clip_id")
    if cid is None:
        return None
    res = store.edl.get_clip(str(cid))
    if not res or not isinstance(res[1], Clip):
        return None
    return [str(res[1].src)]


def derived_key(store: Any, tool: str, args: dict[str, Any]) -> str | None:
    inputs = derived_inputs(store, tool, args)
    if not inputs:
        return None
    params = {k: v for k, v in args.items() if k not in ("clip_id",)}
    return make_key(tool, inputs, params, "1")


def restore_files(cache: Cache, key: str, cache_dir: Path) -> list[str]:
    """Lay a complete entry's files into `cache_dir` (paths that already
    exist are left alone). An incomplete entry is dropped and nothing is
    restored. Returns the relative paths laid down."""
    d = entry_dir(cache, DERIVED_KIND, key)
    marker = d / MANIFEST_FILE
    if not marker.is_file():
        return []
    try:
        files = list(json.loads(marker.read_text(encoding="utf-8")).get("files") or [])
    except (OSError, ValueError, AttributeError):
        _drop_entry(d)
        return []
    held = d / "files"
    if not files or not all((held / rel).is_file() for rel in files):
        _drop_entry(d)
        return []
    laid: list[str] = []
    for rel in files:
        dst = cache_dir / rel
        if dst.exists():
            continue
        try:
            _link_or_copy(held / rel, dst)
            laid.append(rel)
        except OSError:
            continue
    _touch(marker)
    return laid


def put_files(cache: Cache, key: str, tool: str, cache_dir: Path, rels: list[str]) -> Path | None:
    """Copy (hard-link where possible) the files a dry-run step created
    under `cache_dir` into an entry; the manifest is written last."""
    if not rels:
        return None
    d = entry_dir(cache, DERIVED_KIND, key)
    _drop_entry(d)
    try:
        held = d / "files"
        total = 0
        for rel in sorted(rels):
            src = cache_dir / rel
            _link_or_copy(src, held / rel)
            total += src.stat().st_size
        _write_json_atomic(d / MANIFEST_FILE, {"files": sorted(rels), "tool": tool, "bytes": total,
                                               "at": time.time()})
        _enforce(cache, d)
        return d
    except Exception:  # noqa: BLE001 — never fail the step over the cache
        _drop_entry(d)
        return None


@dataclass
class StepCarry:
    """One derived step's carry: `begin` before the handler runs (an Apply
    restores the dry run's files first), `end` after it (a dry run captures
    what the step created). Both are no-ops when the cache is off."""
    cache: Cache
    tool: str
    key: str
    cache_dir: Path
    before: set[str] | None
    restored: list[str]

    @classmethod
    def begin(cls, store: Any, tool: str, args: dict[str, Any], *, dry_run: bool) -> "StepCarry | None":
        cache = current()
        if cache is None or tool not in DERIVED_TOOLS:
            return None
        try:
            key = derived_key(store, tool, args)
        except Exception:  # noqa: BLE001
            return None
        if key is None:
            return None
        cache_dir = Path(store.dir) / "cache"
        restored = [] if dry_run else restore_files(cache, key, cache_dir)
        before = _cache_listing(cache_dir) if (dry_run and cache.write) else None
        return cls(cache=cache, tool=tool, key=key, cache_dir=cache_dir, before=before, restored=restored)

    def end(self) -> Path | None:
        if self.before is None:
            return None
        new = sorted(_cache_listing(self.cache_dir) - self.before)
        return put_files(self.cache, self.key, self.tool, self.cache_dir, new)


__all__ = ["ARTEFACT_DIR", "WHISPER_KIND", "DERIVED_KIND", "TRANSCRIPT_FILE", "MANIFEST_FILE",
           "DERIVED_TOOLS", "Cache", "root_for", "active", "for_mode", "current", "file_identity",
           "make_key", "whisper_version", "entry_dir", "transcript_key", "cached_transcribe",
           "derived_inputs", "derived_key", "restore_files", "put_files", "StepCarry"]
