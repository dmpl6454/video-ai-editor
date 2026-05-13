"""House-style audit — pre-export quality gate.

For M2 this is informational (returns checks). M3+ wires it into the export
pipeline to block on critical failures.
"""
from __future__ import annotations
from ..edl import EDL
from ..edl.schema import TextClip, Clip


def audit(edl: EDL) -> dict:
    edl.recompute_duration()
    duration = edl.duration
    short_form = duration <= 90
    issues: list[dict] = []

    def add(level: str, key: str, message: str, fix_tool: str | None = None) -> None:
        issues.append({"level": level, "key": key, "message": message, "fix_tool": fix_tool})

    # Hook in first 3s
    has_hook = False
    for t in edl.tracks:
        if t.type != "text":
            continue
        for c in t.clips:
            if isinstance(c, TextClip) and c.role in ("hook", "super") and c.start < 3.0:
                has_hook = True
                break
    if not has_hook:
        add("error", "hook_missing", "No hook overlay present in the first 3 seconds.",
            fix_tool="generate_hook")

    # Captions track present (for short-form formats)
    has_captions = False
    for t in edl.tracks:
        if t.type == "captions":
            if t.config and t.config.enabled:
                has_captions = True
        if t.type == "text" and any(isinstance(c, TextClip) and c.role == "caption" for c in t.clips):
            has_captions = True
    if short_form and not has_captions:
        add("warn", "no_captions", "Short-form video has no caption track. Most viewers watch muted.",
            fix_tool="add_caption_track")

    # Brand kit applied
    if not edl.brand_kit or not edl.brand_kit.handle:
        add("warn", "no_brand_kit", "No brand kit applied — handle/end-card missing.",
            fix_tool="apply_brand_kit")

    # Max shot length on V1 (short-form: 6s; long-form: 8s)
    v1 = edl.get_track("v1")
    if v1:
        cap = 6.0 if short_form else 8.0
        for c in v1.clips:
            if isinstance(c, Clip) and c.duration > cap:
                add("warn", "long_shot",
                    f"Clip {c.id} runs {c.duration:.1f}s with no cut/beat (cap {cap:.0f}s).")

    # Has any video content
    if duration <= 0.5:
        add("error", "empty", "Project has no video content.")

    score = 100
    for i in issues:
        score -= 20 if i["level"] == "error" else 5
    score = max(0, score)
    return {"score": score, "duration": duration, "issues": issues, "ok": all(i["level"] != "error" for i in issues)}
