"""Local b-roll bin search.

Indexes a folder of stock/saved clips by filename + folder name + (optional)
sidecar `.txt` description. Ranking is keyword-based — fast, no embeddings,
no model downloads. Vision-based ranking can be layered on later via the
`ai/vision.py` Claude path for the top-K candidates.

Index is cached at `~/.cache/video-ai-editor/broll_index_<hash>.json` and
re-built when any indexed file's mtime changes.
"""
from __future__ import annotations
import hashlib
import json
import re
import os
from pathlib import Path
from typing import Iterable

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}


def _slug(s: str) -> list[str]:
    """Crude tokenizer: lower, split on non-alphanum AND underscore, drop empties.

    Underscores must split — otherwise "sunset_beach_drone" stays one token
    and a search for "beach" never matches.
    """
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _index_path(bin_dir: Path) -> Path:
    h = hashlib.sha256(str(bin_dir.resolve()).encode()).hexdigest()[:12]
    cache = Path.home() / ".cache" / "video-ai-editor"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"broll_index_{h}.json"


def _build_index(bin_dir: Path) -> dict:
    entries = []
    for root, _dirs, files in os.walk(bin_dir):
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            sidecar_txt = p.with_suffix(".txt")
            tags: list[str] = []
            tags += _slug(p.stem)
            tags += _slug(p.parent.name)
            if sidecar_txt.exists():
                try:
                    tags += _slug(sidecar_txt.read_text(encoding="utf-8")[:2000])
                except Exception:
                    pass
            try:
                stat = p.stat()
                size_mb = stat.st_size / (1024 * 1024)
                mtime = stat.st_mtime
            except Exception:
                size_mb, mtime = 0.0, 0.0
            entries.append({
                "path": str(p),
                "tags": tags,
                "size_mb": round(size_mb, 1),
                "mtime": mtime,
            })
    return {"bin": str(bin_dir.resolve()), "entries": entries}


def _load_or_build(bin_dir: Path, *, force: bool = False) -> dict:
    idx_p = _index_path(bin_dir)
    if force or not idx_p.exists():
        idx = _build_index(bin_dir)
        idx_p.write_text(json.dumps(idx))
        return idx
    try:
        idx = json.loads(idx_p.read_text())
    except Exception:
        idx = _build_index(bin_dir)
        idx_p.write_text(json.dumps(idx))
        return idx
    # Lightweight staleness check: if any entry's file is gone or has a newer mtime, rebuild.
    needs_rebuild = False
    for e in idx.get("entries", []):
        try:
            mt = Path(e["path"]).stat().st_mtime
        except FileNotFoundError:
            needs_rebuild = True; break
        if mt > e.get("mtime", 0) + 0.5:
            needs_rebuild = True; break
    if needs_rebuild:
        idx = _build_index(bin_dir)
        idx_p.write_text(json.dumps(idx))
    return idx


def search_broll(bin_dir: Path, query: str, *,
                 top_k: int = 8, max_duration: float | None = None) -> list[dict]:
    """Return up to `top_k` candidates ranked by tag overlap with `query`.

    Output rows: {path, score, tags_matched, size_mb}. `max_duration` is a
    hint only — without ffprobe per file (slow), we can't filter precisely;
    the agent can call get_clip after picking.
    """
    if not bin_dir.exists():
        return []
    idx = _load_or_build(bin_dir)
    q_tokens = set(_slug(query))
    if not q_tokens:
        return []
    ranked: list[dict] = []
    for e in idx["entries"]:
        et = set(e["tags"])
        common = q_tokens & et
        if not common:
            continue
        # Score: |hits| / sqrt(|tags|+1) so generic catch-all files don't dominate.
        score = len(common) / max(1.0, (len(et) ** 0.5))
        ranked.append({
            "path": e["path"],
            "score": round(score, 3),
            "matched": sorted(common),
            "size_mb": e.get("size_mb", 0.0),
        })
    ranked.sort(key=lambda r: -r["score"])
    return ranked[:top_k]
