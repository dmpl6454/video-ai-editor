"""Multi-track audio mixer for the renderer.

Builds an ffmpeg filter chain that mixes:
  - main audio (already produced by V1 concat at `[aout]`)
  - music track clips (ducked under speech if track.duck is set)
  - voiceover track clips

Returns the extra inputs to add to the ffmpeg command line, the additional
filter chain text, and the final audio label to map.
"""
from __future__ import annotations
from pathlib import Path
from ..edl import EDL
from ..edl.schema import Clip


def _esc_path(p: str) -> str:
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _audio_clip_filter(in_label: str, clip: Clip, out_label: str) -> str:
    """Single-clip transform: resample → delay → gain → fade in/out."""
    parts = [
        "aresample=async=1:first_pts=0",
        "aformat=channel_layouts=stereo:sample_rates=48000",
    ]
    # Position on timeline via adelay (ms, per channel)
    delay_ms = max(0, int(round(clip.start * 1000)))
    if delay_ms > 0:
        parts.append(f"adelay=delays={delay_ms}|{delay_ms}:all=1")
    # Gain from clip.audio.gain_db
    gain = clip.audio.gain_db if clip.audio else 0.0
    if abs(gain) > 0.01:
        parts.append(f"volume={gain:.2f}dB")
    # Fades
    if clip.audio and clip.audio.fade_in > 0.001:
        parts.append(f"afade=t=in:st={clip.start:.3f}:d={clip.audio.fade_in:.3f}")
    if clip.audio and clip.audio.fade_out > 0.001:
        end = clip.start + clip.duration
        st = max(0.0, end - clip.audio.fade_out)
        parts.append(f"afade=t=out:st={st:.3f}:d={clip.audio.fade_out:.3f}")
    if clip.audio and clip.audio.mute:
        parts.append("volume=0")
    return f"{in_label}{','.join(parts)}{out_label}"


def build_audio_mix(
    edl: EDL,
    *,
    main_audio_label: str,
    first_input_index: int,
    out_label: str = "[afinal]",
    apply_loudnorm: bool = True,
) -> tuple[str, list[str], str]:
    """Mix main audio with music + voiceover tracks.

    Returns (filter_chain, extra_inputs, final_label). If no music/vo present
    AND no loudnorm requested, returns ("", [], main_audio_label) — caller
    maps main_audio_label directly.

    `apply_loudnorm`: pass False during preview renders. Single-pass loudnorm
    pushes the sample rate up to 192k internally, which many players (and the
    AAC encoder) round-trip through 96k — Safari sometimes refuses to play
    96k AAC inside an mp4. Export renders keep loudnorm on for the LUFS
    target; preview renders skip it for compatibility + speed.
    """
    music_track = edl.get_track("music")
    vo_track = edl.get_track("vo")
    music_clips = [c for c in (music_track.clips if music_track and not music_track.muted else []) if isinstance(c, Clip)]
    vo_clips = [c for c in (vo_track.clips if vo_track and not vo_track.muted else []) if isinstance(c, Clip)]

    if not music_clips and not vo_clips:
        # Still apply loudnorm on the speech-only path if a target is set
        # AND we're in export mode. Preview skips it (see docstring).
        lufs = getattr(edl.canvas, "loudness_lufs", None)
        if lufs is not None and apply_loudnorm:
            return (
                f"{main_audio_label}loudnorm=I={float(lufs):.1f}:TP=-1:LRA=11,"
                f"aresample=48000:async=1{out_label}",
                [], out_label,
            )
        return "", [], main_audio_label

    extra_inputs: list[str] = []
    parts: list[str] = []
    next_idx = first_input_index

    music_labels: list[str] = []
    for c in music_clips:
        # Read source from `c.in` to `c.out`
        extra_inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        in_label = f"[{next_idx}:a]"
        out = f"[m{next_idx}]"
        parts.append(_audio_clip_filter(in_label, c, out))
        music_labels.append(out)
        next_idx += 1

    vo_labels: list[str] = []
    for c in vo_clips:
        extra_inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        in_label = f"[{next_idx}:a]"
        out = f"[vo{next_idx}]"
        parts.append(_audio_clip_filter(in_label, c, out))
        vo_labels.append(out)
        next_idx += 1

    # Mix music clips together → [music_mix]
    music_mix_label: str | None = None
    if music_labels:
        if len(music_labels) == 1:
            music_mix_label = music_labels[0]
        else:
            music_mix_label = "[music_mix]"
            parts.append(f"{''.join(music_labels)}amix=inputs={len(music_labels)}:duration=longest:dropout_transition=0:normalize=0{music_mix_label}")

    # Apply ducking to music if requested
    if music_mix_label and music_track and music_track.duck:
        ducked = "[music_ducked]"
        # Sidechain compressor: music as input, main_audio as sidechain key.
        # Music gets attenuated whenever the main (speech) audio is above threshold.
        # The main audio is duplicated upstream because sidechaincompress consumes
        # its sidechain input — we tee it via asplit so we can still mix it later.
        # NOTE: ffmpeg 8 dropped the `makeup` parameter name; use defaults.
        parts.append(
            f"{main_audio_label}asplit=2[main_for_mix][main_for_sc];"
            f"{music_mix_label}[main_for_sc]"
            f"sidechaincompress=threshold=0.05:ratio=8:attack=5:release=400"
            f"{ducked}"
        )
        music_mix_label = ducked
        # Replace main audio label so the final mix uses the tee'd copy.
        main_audio_label = "[main_for_mix]"

    # Mix voiceover clips → [vo_mix]
    vo_mix_label: str | None = None
    if vo_labels:
        if len(vo_labels) == 1:
            vo_mix_label = vo_labels[0]
        else:
            vo_mix_label = "[vo_mix]"
            parts.append(f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=longest:dropout_transition=0:normalize=0{vo_mix_label}")

    # Final mix: [main] + [music] + [vo]
    final_inputs = [main_audio_label]
    if music_mix_label:
        final_inputs.append(music_mix_label)
    if vo_mix_label:
        final_inputs.append(vo_mix_label)
    # Optional loudness normalisation (single-pass loudnorm). Cheap on the
    # CPU and gets us close to broadcast-style LUFS targets (-16 for Reels,
    # -14 for YouTube). Two-pass is more accurate but doubles render cost.
    # The trailing aresample pulls the rate back to 48k — loudnorm internally
    # works at 192k and the AAC encoder otherwise persists 96k, which Safari
    # and a couple of phone browsers reject inside mp4 containers.
    lufs = getattr(edl.canvas, "loudness_lufs", None)
    norm_chain = ""
    if lufs is not None and apply_loudnorm:
        norm_chain = (f",loudnorm=I={float(lufs):.1f}:TP=-1:LRA=11"
                      f",aresample=48000:async=1")

    if len(final_inputs) == 1:
        if norm_chain:
            # Apply loudnorm to the single source so we still hit the target.
            parts.append(f"{final_inputs[0]}{norm_chain.lstrip(',')}{out_label}")
            return ";".join(parts), extra_inputs, out_label
        return ";".join(parts), extra_inputs, final_inputs[0]
    parts.append(
        f"{''.join(final_inputs)}amix=inputs={len(final_inputs)}:duration=first:dropout_transition=0:normalize=0"
        f"{norm_chain},alimiter=limit=0.97{out_label}"
    )
    return ";".join(parts), extra_inputs, out_label
