"""V2 picture-in-picture overlay path.

V1 is the base layer (concatenated full-screen). Each clip on V2 (or any
non-V1 video track) is overlaid on top with its transform (scale, x, y,
rotation, opacity) applied, and only visible during its timeline range
(`enable=between(t,start,end)`).

Audio from V2 clips also gets mixed into the final audio output so PiP
clips with sound (talking-head over screen recording, etc.) play correctly.
"""
from __future__ import annotations
from pathlib import Path
from ..edl import EDL
from ..edl.schema import Clip
from ..edl.keyframes import is_keyframed, to_ffmpeg_expr
from .effects import build_chromakey_filter


def collect_pip_clips(edl: EDL) -> list[tuple[str, Clip]]:
    """Return [(track_id, clip), ...] for every clip on a non-V1 video track."""
    out: list[tuple[str, Clip]] = []
    for t in edl.tracks:
        if t.type != "video" or t.id == "v1" or t.muted:
            continue
        for c in t.clips:
            if isinstance(c, Clip):
                out.append((t.id, c))
    out.sort(key=lambda p: p[1].start)
    return out


def _scalar_or_last(v, default: float = 0.0) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return default
    if isinstance(v, dict):
        kfs = v.get("keyframes") or []
    else:
        kfs = getattr(v, "keyframes", []) or []
    if not kfs:
        return default
    return float(sorted(kfs, key=lambda p: p[0])[-1][1])


def build_pip_overlay_chain(
    edl: EDL,
    *,
    source_label: str,
    out_label: str,
    first_input_index: int,
    out_w: int,
    out_h: int,
) -> tuple[str, list[str], str, list[Clip]]:
    """Return (filter_chain, extra_inputs, final_video_label, audio_clips).

    Each PiP clip is added as a new ffmpeg input (decoded from its src). The
    chain scales it relative to the canvas (default 35% of canvas long side),
    optionally rotates, then overlays at its timeline position with
    `enable=between(t,start,end)`. Audio for each clip is returned separately
    so the audio mixer can fold it in with the same timing.
    """
    pips = collect_pip_clips(edl)
    if not pips:
        return "", [], source_label, []

    canvas = edl.canvas
    extra_inputs: list[str] = []
    parts: list[str] = []
    audio_clips: list[Clip] = []
    cur = source_label

    for i, (_tid, c) in enumerate(pips):
        idx = first_input_index + i
        # Trim source on input side so we only decode what's needed.
        extra_inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]

        tx = c.transform
        # Scale relative to canvas long edge. Default size = 35% of canvas long edge.
        sc_static = _scalar_or_last(tx.scale, 1.0)
        # Default PiP "1.0" = 35% of canvas. >1 = larger PiP.
        canvas_long = max(canvas.w, canvas.h)
        # Translate canvas-space scale to output-pixel scale
        out_long = max(out_w, out_h)
        target_long = max(40, int(out_long * 0.35 * sc_static))
        # We don't know the source aspect; -1 preserves it
        scaled_label = f"[pip{i}]"
        parts.append(f"[{idx}:v]scale=w={target_long}:h=-1{scaled_label}")

        # Optional chroma key BEFORE rotate/opacity so transparency survives.
        if getattr(c, "chromakey", None) is not None:
            keyed_label = f"[pipk{i}]"
            parts.append(f"{scaled_label}{build_chromakey_filter(c.chromakey)}{keyed_label}")
            scaled_label = keyed_label

        # Optional rotation
        rot_static = _scalar_or_last(tx.rotation, 0.0)
        if abs(rot_static) > 0.01:
            rad = rot_static * 3.14159265 / 180.0
            rotated = f"[pipr{i}]"
            parts.append(f"{scaled_label}rotate={rad}:c=black@0:ow=rotw({rad}):oh=roth({rad}){rotated}")
            scaled_label = rotated

        # Optional opacity
        opa_static = _scalar_or_last(tx.opacity, 1.0)
        if opa_static < 0.999:
            faded = f"[pipo{i}]"
            parts.append(f"{scaled_label}format=yuva420p,colorchannelmixer=aa={opa_static:.3f}{faded}")
            scaled_label = faded

        # Position: x/y are CANVAS-space pixels of the clip's center.
        # Translate to OUTPUT-space top-left.
        sx = out_w / max(1, canvas.w)
        sy = out_h / max(1, canvas.h)
        x_kf = tx.x
        y_kf = tx.y
        if is_keyframed(x_kf):
            xe = to_ffmpeg_expr(x_kf, time_var=f"(t-{c.start:.4f})")
            x_expr = f"({xe})*{sx:.6f}-overlay_w/2"
        else:
            xc = float(getattr(tx, "x", 0)) if isinstance(tx.x, (int, float)) else canvas.w / 2
            x_expr = f"({xc * sx:.2f})-overlay_w/2"
        if is_keyframed(y_kf):
            ye = to_ffmpeg_expr(y_kf, time_var=f"(t-{c.start:.4f})")
            y_expr = f"({ye})*{sy:.6f}-overlay_h/2"
        else:
            yc = float(getattr(tx, "y", 0)) if isinstance(tx.y, (int, float)) else canvas.h / 2
            y_expr = f"({yc * sy:.2f})-overlay_h/2"

        is_last = i == len(pips) - 1
        next_label = out_label if is_last else f"[pip_post{i}]"
        parts.append(
            f"{cur}{scaled_label}overlay=x='{x_expr}':y='{y_expr}'"
            f":enable='between(t\\,{c.start:.3f}\\,{c.start + c.duration:.3f})'{next_label}"
        )
        cur = next_label
        audio_clips.append(c)

    return ";".join(parts), extra_inputs, cur, audio_clips
