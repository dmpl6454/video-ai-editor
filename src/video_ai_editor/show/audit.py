"""House-style audit — pre-export quality gate.

The 3-axis hook stack is the lead check: short-form viewers decide whether
to keep watching in ~3 seconds. Three independent signals work in concert:

  👁 Visual — bold/unexpected opening. Motion or a striking frame, not a
              static talking-head hold.
  ✍ Text   — overlay in the first frames so silent-autoplay viewers get
              the topic instantly.
  🎧 Audio  — strong opening line, trending sound, or audio cue that grabs
              attention before they look at the screen.

`apply_hook_stack()` (in dispatch) installs all three; this audit reports
which are present and flags missing ones. Zero axes blocks export.
"""
from __future__ import annotations
from ..edl import EDL
from ..edl.schema import TextClip, Clip, Keyframe


def _is_keyframed(v) -> bool:
    return isinstance(v, Keyframe) and len(v.keyframes) >= 2


def hook_axes(edl: EDL, window: float = 3.0) -> dict:
    """Independently score each of the three hook axes for the first `window` s.

    Exported so dispatch / UI can call it without re-running the full audit.
    """
    # TEXT axis: any text overlay with role hook/super/label starting < window.
    text_axis = False
    for t in edl.tracks:
        if t.type != "text":
            continue
        for c in t.clips:
            if isinstance(c, TextClip) and c.role in ("hook", "super", "label") \
                    and c.start < window:
                text_axis = True
                break
        if text_axis:
            break

    # VISUAL axis: V1's first clip either has motion keyframed during the
    # hook window OR is short enough that a cut lands inside it OR carries
    # a reverse/speed change.
    visual_axis = False
    v1 = edl.get_track("v1")
    v1_clips = sorted(
        [c for c in (v1.clips if v1 else []) if isinstance(c, Clip)],
        key=lambda c: c.start,
    )
    if v1_clips:
        first = v1_clips[0]
        tx = first.transform
        if any(_is_keyframed(getattr(tx, k))
               for k in ("scale", "x", "y", "rotation", "opacity")):
            visual_axis = True
        if len(v1_clips) >= 2 and v1_clips[1].start < window:
            visual_axis = True
        if first.reverse or (first.speed and first.speed != 1.0):
            visual_axis = True

    # AUDIO axis: V1's first clip has audio shaping (fade-in / gain) OR
    # there's a music / vo clip starting within the hook window.
    audio_axis = False
    if v1_clips and v1_clips[0].audio:
        if v1_clips[0].audio.fade_in >= 0.1 or v1_clips[0].audio.gain_db != 0:
            audio_axis = True
    for tid in ("music", "vo"):
        tr = edl.get_track(tid)
        if tr and any(isinstance(c, Clip) and c.start <= window
                      for c in tr.clips):
            audio_axis = True

    score = int(visual_axis) + int(text_axis) + int(audio_axis)
    missing = [k for k in ("visual", "text", "audio")
               if not {"visual": visual_axis, "text": text_axis, "audio": audio_axis}[k]]
    return {"visual": visual_axis, "text": text_axis, "audio": audio_axis,
            "hook_score": score, "missing": missing}


def audit(edl: EDL) -> dict:
    edl.recompute_duration()
    duration = edl.duration
    short_form = duration <= 90
    issues: list[dict] = []

    def add(level: str, key: str, message: str, fix_tool: str | None = None) -> None:
        issues.append({"level": level, "key": key, "message": message, "fix_tool": fix_tool})

    # 3-axis hook stack — the highest-leverage signal for short-form retention.
    hook = hook_axes(edl)
    if hook["hook_score"] == 0:
        add("error", "hook_missing",
            "Hook stack empty: no visual motion, no text overlay, no audio "
            "cue in the first 3 seconds. Viewers scroll past in those 3s.",
            fix_tool="apply_hook_stack")
    elif hook["hook_score"] == 1:
        add("warn", "hook_partial",
            f"Hook stack thin — only 1 of 3 axes present. "
            f"Missing: {', '.join(hook['missing'])}. "
            f"Each missing axis ~halves stop-scroll rate.",
            fix_tool="apply_hook_stack")
    elif hook["hook_score"] == 2:
        add("warn", "hook_two_of_three",
            f"Hook stack at 2/3. Missing: {hook['missing'][0]}. "
            f"Cheap to fix — call apply_hook_stack to add the third axis.",
            fix_tool="apply_hook_stack")

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
    return {
        "score": score,
        "duration": duration,
        "issues": issues,
        "ok": all(i["level"] != "error" for i in issues),
        "hook": hook,   # {visual, text, audio, hook_score (0-3), missing}
    }
