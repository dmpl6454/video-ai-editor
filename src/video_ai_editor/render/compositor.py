"""Renderer — video tracks composited through ffmpeg filter_complex.

M1 capability:  cut/trim/concat/reorder on V1 + A1, scaled to canvas, hash-keyed
                preview cache, preview-vs-export quality split.
M2 addition:    text + captions tracks composited as PNG overlays.

GPU encoding: when VideoToolbox is available (Apple Silicon), preview + export
encode via h264_videotoolbox for ~5–10× the throughput of libx264.
"""
from __future__ import annotations
import shutil
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from ..config import FONTS_DIR
from ..edl import EDL
from ..edl.schema import Clip, Track
from .text_overlay import build_overlay_chain
from .audio_mix import build_audio_mix
from .effects import effect_chain, render_mask_png, build_chromakey_filter
from .pip import build_pip_overlay_chain
from ..edl.keyframes import is_keyframed, to_ffmpeg_expr


@lru_cache(maxsize=1)
def _has_videotoolbox() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, check=True)
        return " h264_videotoolbox " in out.stdout
    except Exception:
        return False


def _video_encoder_args(*, preview: bool) -> list[str]:
    """Pick the fastest H.264 encoder available; fall back to libx264."""
    if _has_videotoolbox():
        # VideoToolbox quality scale: lower = better. Preview ~60, export ~50.
        # Allow_sw=1 lets it transparently fall back if hw queue is busy.
        q = "60" if preview else "48"
        return [
            "-c:v", "h264_videotoolbox",
            "-q:v", q,
            "-allow_sw", "1",
            "-realtime", "1" if preview else "0",
            "-pix_fmt", "yuv420p",
        ]
    # Software fallback
    crf = "30" if preview else "20"
    preset = "ultrafast" if preview else "medium"
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]


# In-flight render dedup: identical EDL hash → one ffmpeg job, shared result.
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()


@dataclass
class RenderResult:
    path: Path
    cached: bool
    edl_hash: str


def _video_clips(edl: EDL) -> list[Clip]:
    """Return the V1 (main video) clips sorted by `start` time."""
    v1 = edl.get_track("v1")
    if not v1:
        return []
    clips: list[Clip] = [c for c in v1.clips if isinstance(c, Clip)]
    clips.sort(key=lambda c: c.start)
    return clips


def _build_clip_video_chain(c: Clip, *, input_label: str, label_out: str,
                            canvas_w: int, canvas_h: int) -> str:
    """Build the per-clip video filter chain (scale + transform + effects + speed),
    starting from `input_label` (e.g. "[0:v]") and ending at `label_out`.

    Used by both the monolithic renderer (where input_label = [N:v] for the
    Nth input) and the chunk renderer (where input_label = [0:v]).
    """
    v_chain = (f"{input_label}"
               f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
               f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    tx = c.transform
    rot_static = float(tx.rotation) if isinstance(tx.rotation, (int, float)) else 0.0
    sc_static = float(tx.scale) if isinstance(tx.scale, (int, float)) else 1.0
    rot_animated = is_keyframed(tx.rotation)
    sc_animated = is_keyframed(tx.scale)
    x_animated = is_keyframed(tx.x)
    y_animated = is_keyframed(tx.y)
    tvar = f"(t-{c.start:.4f})"

    if rot_animated:
        re = to_ffmpeg_expr(tx.rotation, time_var=tvar)
        re_rad = f"({re})*PI/180"
        v_chain += (f",rotate=a='{re_rad}':c=black:"
                    f"ow='rotw({re_rad})':oh='roth({re_rad})'")
        v_chain += (f",scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                    f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black")
    elif abs(rot_static) > 0.001:
        rad = rot_static * 3.14159265 / 180.0
        v_chain += f",rotate={rad}:c=black:ow=rotw({rad}):oh=roth({rad})"
        v_chain += (f",scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                    f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black")

    if sc_animated or x_animated or y_animated:
        sexpr = to_ffmpeg_expr(tx.scale, time_var=tvar) if sc_animated else f"{sc_static:.4f}"
        zoom = f"max(1\\,{sexpr})"
        if x_animated:
            xe = to_ffmpeg_expr(tx.x, time_var=tvar)
            cx_expr = f"(iw-{canvas_w})/2 + ({xe})"
        else:
            cx_expr = f"(iw-{canvas_w})/2 + {float(tx.x) if isinstance(tx.x, (int, float)) else 0:.2f}"
        if y_animated:
            ye = to_ffmpeg_expr(tx.y, time_var=tvar)
            cy_expr = f"(ih-{canvas_h})/2 + ({ye})"
        else:
            cy_expr = f"(ih-{canvas_h})/2 + {float(tx.y) if isinstance(tx.y, (int, float)) else 0:.2f}"
        v_chain += (
            f",scale=w='{canvas_w}*{zoom}':h='{canvas_h}*{zoom}':eval=frame"
            f",crop={canvas_w}:{canvas_h}:'{cx_expr}':'{cy_expr}'"
        )
    elif abs(sc_static - 1.0) > 0.001 and sc_static > 0:
        sw = max(2, int(canvas_w * sc_static) // 2 * 2)
        sh = max(2, int(canvas_h * sc_static) // 2 * 2)
        v_chain += f",scale={sw}:{sh}"
        if sc_static < 1.0:
            v_chain += f",pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        else:
            v_chain += f",crop={canvas_w}:{canvas_h}:(iw-{canvas_w})/2:(ih-{canvas_h})/2"

    ec = effect_chain(c.effects or [])
    if ec:
        v_chain += "," + ec

    # Chroma key (green/blue screen) — produces transparent regions; on V1 they
    # show through to canvas bg colour, on PiP they show through to the layer below.
    if getattr(c, "chromakey", None) is not None:
        v_chain += "," + build_chromakey_filter(c.chromakey)

    if isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0:
        v_chain += f",setpts=PTS/{float(c.speed)}"

    # Normalize sample aspect ratio at the end. rotate / scale-with-eval=frame
    # can produce SAR like 86519:86488 which makes concat fail with
    # "Input link parameters do not match" when neighbours have SAR 1:1.
    v_chain += ",setsar=1"
    v_chain += label_out
    return v_chain


def _build_clip_audio_chain(c: Clip, *, input_label: str, label_out: str) -> str:
    """Per-clip audio chain: resample + atempo for speed + gain/fade/mute.

    Gain/fade/mute were previously only applied to music + vo clips (via
    audio_mix._audio_clip_filter); V1 clips lost those properties silently
    on render. The fade is positioned relative to the clip's LOCAL audio
    (which is `-ss/-to`-trimmed and starts at t=0), since concat then
    sequences these clips into the timeline absolute time.
    """
    a_chain = (f"{input_label}aresample=async=1:first_pts=0,"
               f"aformat=channel_layouts=stereo:sample_rates=48000")
    if isinstance(c.speed, (int, float)) and c.speed and c.speed != 1.0 and c.speed > 0:
        remaining = float(c.speed)
        while remaining > 2.0:
            a_chain += ",atempo=2.0"
            remaining /= 2.0
        while remaining < 0.5:
            a_chain += ",atempo=0.5"
            remaining /= 0.5
        if abs(remaining - 1.0) > 0.001:
            a_chain += f",atempo={remaining:.4f}"
    if c.audio:
        if abs(c.audio.gain_db) > 0.01:
            a_chain += f",volume={c.audio.gain_db:.2f}dB"
        if c.audio.fade_in > 0.001:
            # fade-in starts at local t=0 and runs for fade_in seconds.
            a_chain += f",afade=t=in:st=0:d={c.audio.fade_in:.3f}"
        if c.audio.fade_out > 0.001:
            # fade-out ends at clip duration; start at duration - fade_out.
            fade_out_start = max(0.0, c.duration - c.audio.fade_out)
            a_chain += (f",afade=t=out:st={fade_out_start:.3f}"
                        f":d={c.audio.fade_out:.3f}")
        if c.audio.mute:
            a_chain += ",volume=0"
    a_chain += label_out
    return a_chain


def _build_filter_complex(clips: list[Clip], canvas_w: int, canvas_h: int,
                          *, transitions: list | None = None,
                          cache_dir: Path | None = None,
                          chunk_paths: list[Path] | None = None,
                          ) -> tuple[str, list[str], list[str], list[str]]:
    """Build the video+audio filter chain for a list of V1 clips.

    Returns (filter_str, input_args, [v_label, a_label], extra_inputs_for_masks).
    Each clip is decoded with input-side seeking, scaled+padded to canvas,
    then runs through any per-clip effect chain, then mask alphamerge if a
    mask is set, then enters the timeline assembly (concat OR xfade).
    """
    inputs: list[str] = []
    extra_inputs: list[str] = []
    fc_parts: list[str] = []
    v_labels: list[str] = []
    a_labels: list[str] = []

    transitions = transitions or []
    # transitions are between adjacent clip joins, indexed by left clip's index
    trans_at: dict[int, tuple[str, float]] = {}
    if transitions and clips:
        # Build a map: clip i (0-based) → transition that bridges to i+1
        running = 0.0
        for idx, c in enumerate(clips[:-1]):
            running += c.duration
            for tr in transitions:
                if abs(tr.at - running) < 0.05:
                    trans_at[idx] = (tr.type, tr.duration)

    use_chunks = chunk_paths is not None and len(chunk_paths) == len(clips)

    # Pass 1: assign clip indices [0..N-1]; emit clip-side filter chains and
    # remember which clips need masks (their PNGs are added as inputs in
    # pass 2 so their input indices land AFTER all clip inputs — that's the
    # actual ordering ffmpeg sees on the command line).
    pending_masks: list[tuple[int, Path]] = []  # (clip_index, mask_path)
    for i, c in enumerate(clips):
        if use_chunks:
            inputs += ["-i", str(chunk_paths[i])]
            fc_parts.append(f"[{i}:v]null[ve{i}]")
            fc_parts.append(f"[{i}:a]anull[a{i}]")
            v_labels.append(f"[ve{i}]")
            a_labels.append(f"[a{i}]")
            continue

        inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        fc_parts.append(_build_clip_video_chain(
            c, input_label=f"[{i}:v]", label_out=f"[ve{i}]",
            canvas_w=canvas_w, canvas_h=canvas_h,
        ))
        if c.mask is not None and cache_dir is not None:
            mask_path = cache_dir / f"mask_{c.id}_{c.mask.type}_{int(c.mask.feather)}_{canvas_w}x{canvas_h}.png"
            if not mask_path.exists():
                render_mask_png(c.mask, canvas_w, canvas_h, mask_path)
            pending_masks.append((i, mask_path))

        fc_parts.append(_build_clip_audio_chain(
            c, input_label=f"[{i}:a]", label_out=f"[a{i}]",
        ))
        a_labels.append(f"[a{i}]")
        v_labels.append(f"[ve{i}]")  # tentative; rewritten below if mask present

    # Pass 2: append mask inputs (so their ffmpeg indices are N + k) and emit
    # alphamerge filter chunks; rewrite the corresponding v_label to the masked one.
    n_clips = len(clips)
    for k, (clip_i, mask_path) in enumerate(pending_masks):
        extra_inputs += ["-i", str(mask_path)]
        mask_idx = n_clips + k
        v_masked = f"[vm{clip_i}]"
        fc_parts.append(
            f"[ve{clip_i}][{mask_idx}:v]"
            f"alphamerge,format=yuva420p[vmrgba{clip_i}];"
            f"color=c=black:s={canvas_w}x{canvas_h}:r=30[bg{clip_i}];"
            f"[bg{clip_i}][vmrgba{clip_i}]overlay=format=auto:shortest=1{v_masked}"
        )
        v_labels[clip_i] = v_masked

    if not clips:
        return "", [], [], []

    # ---- Timeline assembly ----
    if not trans_at:
        # Plain concat (interleaved [v0][a0][v1][a1]...)
        interleaved = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
        fc_parts.append(f"{interleaved}concat=n={len(clips)}:v=1:a=1[vout][aout]")
    else:
        # Chain xfade for video, acrossfade for audio between adjacent clips.
        cur_v = v_labels[0]
        cur_a = a_labels[0]
        cur_dur = clips[0].duration
        for i in range(1, len(clips)):
            tr_for_left = trans_at.get(i - 1)
            new_v = f"[xv{i}]"
            new_a = f"[xa{i}]"
            if tr_for_left:
                ttype, tdur = tr_for_left
                offset = max(0.0, cur_dur - tdur)
                fc_parts.append(
                    f"{cur_v}{v_labels[i]}xfade=transition={ttype}:duration={tdur}:offset={offset:.3f}{new_v}"
                )
                fc_parts.append(
                    f"{cur_a}{a_labels[i]}acrossfade=d={tdur}{new_a}"
                )
                cur_dur = cur_dur + clips[i].duration - tdur
            else:
                fc_parts.append(
                    f"{cur_v}{v_labels[i]}concat=n=2:v=1:a=0{new_v}"
                )
                fc_parts.append(
                    f"{cur_a}{a_labels[i]}concat=n=2:v=0:a=1{new_a}"
                )
                cur_dur += clips[i].duration
            cur_v = new_v
            cur_a = new_a
        # Rename the final accumulators to [vout]/[aout] for downstream code
        fc_parts.append(f"{cur_v}null[vout]")
        fc_parts.append(f"{cur_a}anull[aout]")

    return ";".join(fc_parts), inputs, ["[vout]", "[aout]"], extra_inputs


def _render(edl: EDL, dst: Path, *, height: int, fps: int, preview: bool,
            cache_dir: Path | None = None) -> Path:
    canvas = edl.canvas
    h_out = height
    w_out = int(round(canvas.w * (h_out / canvas.h) / 2) * 2)
    enc_args = _video_encoder_args(preview=preview)

    clips = _video_clips(edl)
    if not clips:
        dur = max(1.0, edl.duration)
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={w_out}x{h_out}:r={fps}:d={dur}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
            "-shortest", *enc_args, "-c:a", "aac", str(dst),
        ], check=True, capture_output=True)
        return dst

    # Pull transitions for V1 from the EDL (transitions live on the v1 track in M4)
    v1 = edl.get_track("v1")
    transitions = (v1.transitions if v1 else []) or []

    # Chunk cache: when no V1 transitions, render each clip ONCE to a
    # canvas-resolution mp4 and cache by fingerprint. Subsequent renders that
    # don't change the clip skip straight to a fast concat. Disabled when
    # transitions are present (xfade needs both streams in one filter graph).
    chunk_paths: list[Path] | None = None
    if cache_dir is not None and not transitions:
        try:
            from .chunks import get_or_build_chunks
            chunk_paths = get_or_build_chunks(
                clips,
                cache_dir=cache_dir / "chunks",
                canvas_w=canvas.w, canvas_h=canvas.h, fps=fps,
                encoder_args=enc_args,
                build_video_chain=_build_clip_video_chain,
                build_audio_chain=_build_clip_audio_chain,
            )
        except Exception:
            # Cache miss / chunk render failure → fall back to monolithic.
            chunk_paths = None

    fc, inputs, labels, mask_inputs = _build_filter_complex(
        clips, w_out, h_out, transitions=transitions, cache_dir=cache_dir,
        chunk_paths=chunk_paths,
    )
    v_label = labels[0]
    a_label = labels[1]

    extra_inputs: list[str] = list(mask_inputs)
    # Each clip + each mask are separate inputs; track the running input index
    next_idx = len(clips) + (len(mask_inputs) // 2)

    # V2 picture-in-picture overlays. Each PIP clip is added as a new -i and
    # composited on top of V1 with its transform; audio comes back as a list
    # we feed into the audio mixer below.
    pip_chain, pip_inputs, pip_v_label, pip_audio_clips = build_pip_overlay_chain(
        edl,
        source_label=v_label,
        out_label="[vpip_final]",
        first_input_index=next_idx,
        out_w=w_out, out_h=h_out,
    )
    if pip_chain:
        fc = fc + ";" + pip_chain
        v_label = pip_v_label
    extra_inputs += pip_inputs
    # Each PIP clip used 4 args (-ss .. -to .. -i path) → +4 indices each.
    pip_inputs_count = len(pip_inputs) // 4
    next_idx += pip_inputs_count

    # Composite text overlay PNGs (rendered by Pillow) via ffmpeg overlay= filter.
    if cache_dir is not None:
        chain, txt_inputs, after_label = build_overlay_chain(
            edl, cache_dir,
            source_label=v_label,
            out_label="[vtxt_final]",
            first_input_index=next_idx,
            out_w=w_out, out_h=h_out,
        )
        if chain:
            fc = fc + ";" + chain
            v_label = after_label
        extra_inputs += txt_inputs
        next_idx += len(txt_inputs) // 2  # each overlay PNG adds two args (-i path)

    # Fold V2 PiP audio into the V1 main audio before the music+vo mixer runs.
    # Each PIP clip's audio is positioned at its timeline start via adelay and
    # amix'd with the main concat audio.
    if pip_audio_clips:
        # PIP video inputs were added at indices [pre_pip..pre_pip+N-1] where
        # pre_pip = original next_idx BEFORE pip_inputs_count was added.
        pre_pip = next_idx - pip_inputs_count
        pa_parts: list[str] = []
        pa_labels: list[str] = []
        for j, c in enumerate(pip_audio_clips):
            input_idx = pre_pip + j
            delay_ms = max(0, int(round(c.start * 1000)))
            chain = (f"[{input_idx}:a]aresample=async=1:first_pts=0,"
                     f"aformat=channel_layouts=stereo:sample_rates=48000")
            if delay_ms > 0:
                chain += f",adelay=delays={delay_ms}|{delay_ms}:all=1"
            pa_label = f"[pa{j}]"
            chain += pa_label
            pa_parts.append(chain)
            pa_labels.append(pa_label)
        # Mix pip audio with main audio
        mix_inputs = a_label + "".join(pa_labels)
        mixed_label = "[a_with_pip]"
        pa_parts.append(
            f"{mix_inputs}amix=inputs={1 + len(pa_labels)}:duration=first"
            f":dropout_transition=0:normalize=0{mixed_label}"
        )
        fc = fc + ";" + ";".join(pa_parts)
        a_label = mixed_label

    # Mix in music + voiceover tracks (with optional ducking against main audio).
    # Loudnorm only runs on export — preview skips it (see audio_mix docstring).
    audio_chain, audio_inputs, final_audio_label = build_audio_mix(
        edl,
        main_audio_label=a_label,
        first_input_index=next_idx,
        apply_loudnorm=not preview,
    )
    if audio_chain:
        fc = fc + ";" + audio_chain
    extra_inputs += audio_inputs

    args = ["ffmpeg", "-y", *inputs, *extra_inputs,
            "-filter_complex", fc,
            "-map", v_label, "-map", final_audio_label,
            "-r", str(fps),
            *enc_args,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed (rc={proc.returncode}):\n{proc.stderr[-2000:]}")
    return dst


def _video_only_fingerprint(edl: EDL) -> str:
    """Hash everything that affects the visual frame (V1 + V2/PIP + text +
    stickers + transitions + canvas + brand kit) but NOT music/vo/captions
    audio gain. Used to cache the encoded video so audio-only edits skip the
    video re-encode."""
    import hashlib, json
    blob = {
        "canvas": edl.canvas.model_dump(),
        "brand": edl.brand_kit.model_dump() if edl.brand_kit else None,
        "tracks": [
            t.model_dump(by_alias=True, mode="json")
            for t in edl.tracks
            if t.type in ("video", "text", "sticker")  # video covers v1 AND v2
        ],
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


def render_preview(edl: EDL, session_dir: Path, *, height: int = 540, fps: int = 30) -> RenderResult:
    """Render a preview keyed by EDL hash, with an audio-only-remux fast path.

    Strategy:
      1. Full hash hit → return cached preview (instant).
      2. Else compute video-only fingerprint. If a cached video at that fp
         exists, ffmpeg-mux it against the new audio mix (`-c:v copy`). This
         skips the expensive video re-encode when only music/vo changed.
      3. Else do the full render and cache by both video-fp and full hash.
    """
    h = edl.hash()
    out_dir = session_dir / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{h}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return RenderResult(path=dst, cached=True, edl_hash=h)

    key = f"{session_dir.name}/{h}"
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(key)
        if existing is None:
            event = threading.Event()
            _INFLIGHT[key] = event
            owner = True
        else:
            event = existing
            owner = False

    if not owner:
        event.wait(timeout=120)
        if dst.exists() and dst.stat().st_size > 0:
            return RenderResult(path=dst, cached=True, edl_hash=h)

    try:
        # Audio-only-remux fast path
        cache_dir = session_dir / "cache"
        videos_dir = cache_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        video_fp = _video_only_fingerprint(edl)
        cached_video = videos_dir / f"video_{video_fp}_{height}.mp4"

        # Same chunk_is_valid check we use for per-clip chunks: rejects
        # mp4s left over from killed renders that lack a moov atom.
        from .chunks import chunk_is_valid as _valid
        if _valid(cached_video):
            # Just remux the cached video against a freshly-rendered audio mix.
            try:
                _remux_with_new_audio(edl, cached_video, dst, fps=fps,
                                      cache_dir=cache_dir)
                return RenderResult(path=dst, cached=False, edl_hash=h)
            except Exception:
                # Remux failed → fall through to full render
                pass

        _render(edl, dst, height=height, fps=fps, preview=True,
                cache_dir=cache_dir)
        # Also cache the video-only version (extract from the just-rendered
        # full preview — `-c:v copy -an` is essentially free).
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(dst), "-c:v", "copy", "-an",
                 "-movflags", "+faststart", str(cached_video)],
                capture_output=True, check=True,
            )
        except Exception:
            pass

        files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        for old in files[:-10]:
            old.unlink(missing_ok=True)
        # Cap the video-only cache too
        vfiles = sorted(videos_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        for old in vfiles[:-15]:
            old.unlink(missing_ok=True)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
        event.set()
    return RenderResult(path=dst, cached=False, edl_hash=h)


def _remux_with_new_audio(edl: EDL, video_only: Path, dst: Path,
                          *, fps: int, cache_dir: Path) -> None:
    """Take a cached video-only mp4 and mux a fresh audio mix onto it.

    The audio mix is built the same way the main renderer does it (V1 source
    audio + music ducking + voiceover) but only the audio is encoded.
    Video is `-c:v copy` so this is essentially I/O bound.
    """
    clips = _video_clips(edl)
    if not clips:
        # No V1 audio source to feed the mixer — copy video, generate silence.
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_only),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            "-movflags", "+faststart", str(dst),
        ], capture_output=True, check=True)
        return

    # Build per-clip audio chains with the same input order as the main render.
    inputs: list[str] = ["-i", str(video_only)]  # idx 0 = video-only file
    fc_parts: list[str] = []
    a_labels: list[str] = []
    for i, c in enumerate(clips):
        idx = i + 1  # +1 because video_only is input 0
        inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        fc_parts.append(_build_clip_audio_chain(
            c, input_label=f"[{idx}:a]", label_out=f"[a{i}]"
        ))
        a_labels.append(f"[a{i}]")
    # Concat per-clip audio
    if len(a_labels) == 1:
        fc_parts.append(f"{a_labels[0]}anull[aout]")
    else:
        # Pair-wise concat with identical sample format
        fc_parts.append(
            "".join(a_labels) + f"concat=n={len(a_labels)}:v=0:a=1[aout]"
        )

    # Mix in music + vo on top of [aout]. _remux_with_new_audio is only called
    # from the preview fast-path, so loudnorm stays off here.
    next_idx = 1 + len(clips)
    audio_chain, audio_inputs, final_audio_label = build_audio_mix(
        edl, main_audio_label="[aout]", first_input_index=next_idx,
        apply_loudnorm=False,
    )
    fc = ";".join(fc_parts)
    if audio_chain:
        fc = fc + ";" + audio_chain

    args = ["ffmpeg", "-y", *inputs, *audio_inputs,
            "-filter_complex", fc,
            "-map", "0:v", "-map", final_audio_label,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-r", str(fps),
            "-movflags", "+faststart",
            str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"audio remux failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")


def render_export(edl: EDL, session_dir: Path, *, height: int | None = None,
                  fps: int | None = None, crf: int = 18, preset: str = "medium",
                  filename: str | None = None) -> RenderResult:
    """Final export at canvas resolution (or override) with higher quality."""
    h = edl.hash()
    out_dir = session_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = filename or f"export_{h}.mp4"
    dst = out_dir / name
    h_out = height or edl.canvas.h
    f_out = fps or edl.canvas.fps
    _render(edl, dst, height=h_out, fps=f_out, preview=False,
            cache_dir=session_dir / "cache")
    return RenderResult(path=dst, cached=False, edl_hash=h)
