"""Full-quality export masters for clamped sources — QA-089.

Ingest clamps any source whose short side is above 1080 to a 1080p editing
proxy so the timeline, thumbnails and previews stay fast (QA-008 made the
clamp short-side aware). The clip's `src` IS that proxy, and export rendered
from `src` — so a 4K original exported at "2160p" was the 1080p proxy scaled
back up (SSIM 0.938 against the source), with the real 4K file sitting unused
in the upload directory.

A proxy is now only a proxy. When an export's short side is larger than a
clip's proxy, `export_edl` points that clip at a MASTER: the original upload
normalised exactly like the proxy was (same frame rate string, same colour
handling, same frame count — `normalize` is deterministic in both) but only
down to the export's short side instead of 1080. It is made on first need and
kept beside the proxy (`<stem>.master<short>.mp4`), so a second 4K export
reuses it. Timing is identical, so every in/out on the timeline means the
same frame in either file.

Derived renders (a reframe or stabilise of the proxy) are left alone: they
were made from the proxy and have no master.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

_VIDEO_LANES = frozenset({"video"})


def _ingest_for(src: str) -> dict | None:
    p = Path(src)
    ij = p.parent / "ingest.json"
    if not ij.is_file():
        return None
    try:
        data = json.loads(ij.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or Path(str(data.get("normalized", ""))).name != p.name:
        return None
    return data


def _proxy_short_side(data: dict) -> int | None:
    probe = data.get("probe") or {}
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v or not v.get("width") or not v.get("height"):
        return None
    return int(min(v["width"], v["height"]))


def master_plan(edl, out_short_side: int) -> dict[str, tuple[Path, Path, str, int]]:
    """proxy src → (original, master path, fps, master short side) for every
    video-lane clip whose proxy is smaller than `out_short_side`."""
    plan: dict[str, tuple[Path, Path, str, int]] = {}
    for track in edl.tracks:
        if track.type not in _VIDEO_LANES:
            continue
        for clip in track.clips:
            src = getattr(clip, "src", None)
            if not src or src in plan:
                continue
            data = _ingest_for(src)
            if not data or not data.get("clamped_from"):
                continue
            proxy_short = _proxy_short_side(data)
            orig_short = int(min(data["clamped_from"]))
            if proxy_short is None or out_short_side <= proxy_short:
                continue
            original = Path(str(data.get("src", "")))
            if not original.is_file():
                original = Path(src).parent / original.name
                if not original.is_file():
                    continue              # nothing better to render from
            if original.resolve() == Path(src).resolve():
                continue                  # a reopened .vae bundles the proxy only
            short = min(orig_short, int(out_short_side))
            proxy = Path(src)
            stem = proxy.name[: -len(".normalized.mp4")] if proxy.name.endswith(".normalized.mp4") else proxy.stem
            plan[src] = (original, proxy.with_name(f"{stem}.master{short}.mp4"),
                         str(data.get("fps") or ""), short)
    return plan


def export_edl(edl, out_short_side: int, *,
               on_progress: Callable[[float], None] | None = None,
               cancel_event: "threading.Event | None" = None):
    """The EDL an export at `out_short_side` should render: `edl` itself when
    no clip needs a master, else a deep copy pointing at masters (made now if
    missing). `on_progress` reports 0..1 across the masters being made."""
    plan = master_plan(edl, out_short_side)
    if not plan:
        return edl
    from .normalize import normalize
    todo = [(src, spec) for src, spec in plan.items() if not spec[1].is_file()]
    for i, (_src, (original, master, fps, short)) in enumerate(todo):
        def _sub(p: float, i=i) -> None:
            if on_progress:
                on_progress((i + max(0.0, min(1.0, p))) / len(todo))
        normalize(original, master, fps=fps or None, height=short,
                  on_progress=_sub, cancel_event=cancel_event)
    view = edl.model_copy(deep=True)
    for track in view.tracks:
        if track.type not in _VIDEO_LANES:
            continue
        for clip in track.clips:
            spec = plan.get(getattr(clip, "src", None))
            if spec is not None and spec[1].is_file():
                clip.src = str(spec[1])
    return view


__all__ = ["export_edl", "master_plan"]
