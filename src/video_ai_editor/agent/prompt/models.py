"""Local-model manager for the `local_model` brain (spec §3.3).

Owns four decisions and nothing else:

  * **Which model this Mac should run.** Tiers by RAM: ≥ 24 GB →
    `Qwen2.5-7B-Instruct-4bit`, 12–24 GB → `Qwen2.5-3B-Instruct-4bit`,
    below that no local model (the fix says so). `VAI_MLX_MODEL` overrides.
    The 0.5B model is deliberately absent: on the baseline media it emitted
    degenerate repeated steps (baseline findings), so offering it would only
    produce plans the validator rejects.
  * **Whether a model is really installed.** `installed` means a
    `snapshots/<rev>/config.json` exists AND every `*.safetensors` the index
    lists (or the single file) exists — never "bytes on disk > 0", which a
    cancelled download satisfies. Resolution honours the default HF cache
    (`HF_HOME`, default `~/.cache/huggingface`), so the 7B snapshot already
    on the build Mac is found, not re-downloaded.
  * **The only network path.** `download()` is the single function in the
    prompt feature that may touch the network, reached only through the
    loopback-only `/api/prompt/models/download` route AFTER the user
    confirmed (§1.4). `ALLOW_PATTERNS` has no `*.py`: MLX models ship no
    remote code and a `*.py` in a model repo is executable input.
  * **Loading is offline.** `snapshot_dir()` returns a DIRECTORY for
    `mlx_lm.load`; the brain sets `HF_HUB_OFFLINE=1` before loading so a
    missing tokenizer file can never turn into a fetch.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = ["ModelTier", "MODEL_TIERS", "ModelStatus", "ALLOW_PATTERNS", "DEFAULT_MODEL_ENV",
           "total_ram_bytes", "hf_home", "hub_dir", "pick_tier", "allowed_model_ids", "status",
           "snapshot_dir", "list_models", "download", "delete", "FREE_SPACE_FACTOR"]

DEFAULT_MODEL_ENV = "VAI_MLX_MODEL"
GB = 1024 ** 3
FREE_SPACE_FACTOR = 1.2      # pre-flight: free bytes ≥ expected × 1.2

#: File patterns `snapshot_download` may fetch. NO `*.py` — see module doc.
ALLOW_PATTERNS: tuple[str, ...] = ("*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken")


@dataclass(frozen=True)
class ModelTier:
    id: str
    min_ram_gb: int
    approx_label: str        # what the UI shows before the real size is known
    approx_bytes: int        # pre-flight estimate when no index metadata exists
    params: str


MODEL_TIERS: tuple[ModelTier, ...] = (
    ModelTier("mlx-community/Qwen2.5-7B-Instruct-4bit", 24, "~4.3 GB", int(4.3 * GB), "7B"),
    ModelTier("mlx-community/Qwen2.5-3B-Instruct-4bit", 12, "~1.8 GB", int(1.8 * GB), "3B"),
)

_TIER_BY_ID = {t.id: t for t in MODEL_TIERS}


@dataclass(frozen=True)
class ModelStatus:
    id: str
    installed: bool
    snapshot_path: str | None
    bytes_on_disk: int
    expected_bytes: int
    free_bytes: int
    missing_files: tuple[str, ...] = field(default_factory=tuple)
    approx_label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "installed": self.installed, "snapshot_path": self.snapshot_path,
                "bytes_on_disk": self.bytes_on_disk, "expected_bytes": self.expected_bytes,
                "free_bytes": self.free_bytes, "missing_files": list(self.missing_files),
                "approx_label": self.approx_label}


# --- machine + cache locations -------------------------------------------------

def total_ram_bytes() -> int:
    """Physical RAM; 0 when the platform cannot say (then no tier matches and
    the brain reports an honest fix rather than guessing)."""
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return 0


def hf_home(override: str | os.PathLike | None = None) -> Path:
    if override is not None:
        return Path(override)
    return Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))


def hub_dir(home: str | os.PathLike | None = None) -> Path:
    return hf_home(home) / "hub"


def _repo_dir(model_id: str, home: str | os.PathLike | None = None) -> Path:
    return hub_dir(home) / f"models--{model_id.replace('/', '--')}"


def pick_tier(ram_bytes: int | None = None, *, override: str | None = None) -> ModelTier | None:
    """The tier for this machine, or None (< 12 GB, or unknown RAM). An
    override names any repo — the caller shows it as-is and `status()`
    still decides whether it is installed."""
    override = override if override is not None else os.environ.get(DEFAULT_MODEL_ENV)
    if override:
        known = _TIER_BY_ID.get(override)
        return known or ModelTier(override, 0, "custom", 0, "custom")
    ram = total_ram_bytes() if ram_bytes is None else ram_bytes
    for tier in MODEL_TIERS:
        if ram >= tier.min_ram_gb * GB:
            return tier
    return None


def allowed_model_ids(*, override: str | None = None) -> frozenset[str]:
    """Repos `download()`/`delete()` will touch: the tier table plus the
    override. Anything else is refused — the route must not become a
    general-purpose Hugging Face downloader."""
    override = override if override is not None else os.environ.get(DEFAULT_MODEL_ENV)
    ids = {t.id for t in MODEL_TIERS}
    if override:
        ids.add(override)
    return frozenset(ids)


# --- status --------------------------------------------------------------------

def _config_path(model_id: str, home: str | os.PathLike | None) -> Path | None:
    """`snapshots/<rev>/config.json` via huggingface_hub's own cache lookup
    (so revisions/refs are resolved the way the hub does), with a plain
    directory scan as the fallback when the hub is not importable."""
    try:
        from huggingface_hub import try_to_load_from_cache
        hit = try_to_load_from_cache(model_id, "config.json", cache_dir=str(hub_dir(home)))
        if isinstance(hit, str) and Path(hit).exists():
            return Path(hit)
    except Exception:
        pass
    snaps = _repo_dir(model_id, home) / "snapshots"
    if not snaps.is_dir():
        return None
    candidates = sorted((p for p in snaps.iterdir() if (p / "config.json").exists()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] / "config.json" if candidates else None


def _weight_files(snapshot: Path) -> tuple[list[str], int | None]:
    """Weight files the snapshot must contain and the index's
    `metadata.total_size` when present."""
    index = snapshot / "model.safetensors.index.json"
    if index.exists():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            names = sorted(set(str(v) for v in (data.get("weight_map") or {}).values()))
            total = data.get("metadata", {}).get("total_size")
            if names:
                return names, int(total) if isinstance(total, (int, float)) else None
        except (ValueError, OSError):
            pass
    return ["model.safetensors"], None


def _size(path: Path) -> int:
    try:
        return path.resolve().stat().st_size
    except OSError:
        return 0


def _free_bytes(home: str | os.PathLike | None) -> int:
    probe = hub_dir(home)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def status(model_id: str, *, hf_home_dir: str | os.PathLike | None = None) -> ModelStatus:
    tier = _TIER_BY_ID.get(model_id)
    approx = tier.approx_bytes if tier else 0
    label = tier.approx_label if tier else ""
    free = _free_bytes(hf_home_dir)
    config = _config_path(model_id, hf_home_dir)
    if config is None:
        return ModelStatus(model_id, False, None, 0, approx, free, ("config.json",), label)
    snapshot = config.parent
    weights, total = _weight_files(snapshot)
    missing = tuple(w for w in weights if not (snapshot / w).exists())
    on_disk = sum(_size(p) for p in snapshot.iterdir() if p.is_file() or p.is_symlink())
    expected = total if total else (on_disk if not missing else approx)
    return ModelStatus(model_id, not missing, str(snapshot), on_disk, expected, free, missing, label)


def snapshot_dir(model_id: str, *, hf_home_dir: str | os.PathLike | None = None) -> Path | None:
    """The directory `mlx_lm.load` receives — never a repo id."""
    st = status(model_id, hf_home_dir=hf_home_dir)
    return Path(st.snapshot_path) if st.installed and st.snapshot_path else None


def list_models(*, ram_bytes: int | None = None, hf_home_dir: str | os.PathLike | None = None,
                override: str | None = None) -> dict[str, Any]:
    """`GET /api/prompt/models` payload: the tier for this machine and the
    status of every known model (plus the override, if any)."""
    tier = pick_tier(ram_bytes, override=override)
    ids = [t.id for t in MODEL_TIERS]
    if tier and tier.id not in ids:
        ids.insert(0, tier.id)
    return {"tier": tier.id if tier else None,
            "ram_gb": round((total_ram_bytes() if ram_bytes is None else ram_bytes) / GB, 1),
            "models": [status(i, hf_home_dir=hf_home_dir).as_dict() for i in ids]}


# --- download / delete (the only network path) ------------------------------------

class ModelNotAllowed(ValueError):
    pass


class InsufficientDiskSpace(RuntimeError):
    pass


_DOWNLOAD_LOCK = threading.Lock()


def _progress_tqdm(set_progress: Callable[[float], None] | None, cancel_event: Any | None):
    """A tqdm subclass that reports fraction-done and honours cancellation.
    Built lazily so `tqdm` is only imported when a download actually runs."""
    from tqdm.auto import tqdm as _tqdm

    class _Reporting(_tqdm):
        # WHY an own counter: the hub passes `disable=True` in quiet
        # contexts and a disabled tqdm never advances `self.n`.
        _done = 0

        def update(self, n=1):  # type: ignore[override]
            if cancel_event is not None and cancel_event.is_set():
                from ...api.jobs import JobCancelled
                raise JobCancelled("model download cancelled")
            out = super().update(n)
            self._done += n or 0
            if set_progress is not None and self.total:
                set_progress(min(1.0, self._done / float(self.total)))
            return out

    return _Reporting


def download(model_id: str, *, set_progress: Callable[[float], None] | None = None,
             cancel_event: Any | None = None, hf_home_dir: str | os.PathLike | None = None,
             snapshot_download: Callable[..., str] | None = None,
             override: str | None = None) -> dict[str, Any]:
    """Fetch `model_id` into the HF cache. Refuses unknown repos, refuses to
    start without `expected × FREE_SPACE_FACTOR` free bytes, streams progress
    to `set_progress`, raises `JobCancelled` when `cancel_event` is set."""
    if model_id not in allowed_model_ids(override=override):
        raise ModelNotAllowed(f"{model_id!r} is not a model this app offers")
    before = status(model_id, hf_home_dir=hf_home_dir)
    if before.installed:
        if set_progress:
            set_progress(1.0)
        return {"status": "already_installed", **before.as_dict()}
    need = int(max(before.expected_bytes, 0) * FREE_SPACE_FACTOR)
    if before.free_bytes and before.free_bytes < need:
        raise InsufficientDiskSpace(
            f"need {need / GB:.1f} GB free for {model_id}, have {before.free_bytes / GB:.1f} GB")
    if not _DOWNLOAD_LOCK.acquire(blocking=False):
        raise RuntimeError("another model download is already running")
    try:
        if snapshot_download is None:
            from huggingface_hub import snapshot_download as _sd
            snapshot_download = _sd
        hub_dir(hf_home_dir).mkdir(parents=True, exist_ok=True)
        snapshot_download(model_id, allow_patterns=list(ALLOW_PATTERNS),
                          cache_dir=str(hub_dir(hf_home_dir)),
                          tqdm_class=_progress_tqdm(set_progress, cancel_event))
    finally:
        _DOWNLOAD_LOCK.release()
    after = status(model_id, hf_home_dir=hf_home_dir)
    if not after.installed:
        raise RuntimeError(f"download finished but {model_id} is incomplete: missing {list(after.missing_files)}")
    if set_progress:
        set_progress(1.0)
    return {"status": "installed", **after.as_dict()}


def delete(model_id: str, *, hf_home_dir: str | os.PathLike | None = None,
           override: str | None = None) -> dict[str, Any]:
    """Remove a model's cache directory. Only for models this app offers, and
    only the `models--org--name` directory — never anything else in the hub."""
    if model_id not in allowed_model_ids(override=override):
        raise ModelNotAllowed(f"{model_id!r} is not a model this app offers")
    repo = _repo_dir(model_id, hf_home_dir)
    hub = hub_dir(hf_home_dir).resolve()
    if repo.resolve().parent != hub:
        raise ModelNotAllowed(f"refusing to delete outside the hub cache: {repo}")
    existed = repo.exists()
    if existed:
        shutil.rmtree(repo)
    return {"status": "deleted" if existed else "not_installed", "id": model_id}
