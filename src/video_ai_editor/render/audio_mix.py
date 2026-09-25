"""Multi-track audio mixer for the renderer.

Builds an ffmpeg filter chain that mixes:
  - main audio (already produced by V1 concat at `[aout]`)
  - music track clips (ducked under speech if track.duck is set)
  - voiceover track clips

Returns the extra inputs to add to the ffmpeg command line, the additional
filter chain text, and the final audio label to map.
"""
from __future__ import annotations
import contextlib
import contextvars
from pathlib import Path
from typing import Iterator
from ..edl import EDL
from ..edl.schema import Clip
from . import clock


# ---------------------------------------------------------------- ducking
#
# QA-079. The duck used to be `sidechaincompress=threshold=0.05:ratio=8`: a
# COMPRESSOR, whose gain reduction is (key level − threshold) × (1 − 1/ratio).
# So the dip depended on how loud the talker was, never on `MusicDuck.to_db`
# (to_db −6 / −18 / −40 rendered byte-identical beds), and a quiet talker
# (−33 dBFS peaks, 0.022) never crossed the fixed 0.05 threshold at all.
#
# The duck is now a GAIN ENVELOPE: "is anyone speaking" is decided on a
# level-normalised key, and while they are the bed is multiplied by exactly
# 10^(to_db/20). The key chain (all in-graph, ~8 kHz so it is cheap):
#   1. mono, band-limited to the voice band (rumble and hiss are not speech);
#   2. `dynaudnorm` normalises the KEY's level over a few seconds (boundary
#      mode so the first word is not missed) — a −33 dBFS talker keys like a
#      −3 dBFS one; frames below DUCK_KEY_FLOOR (room tone, silence) are left
#      alone, so a noise-only stretch never looks like speech;
#   3. a 20 ms mean-|x| envelope, thresholded at DUCK_THRESHOLD_DB;
#   4. HOLD (≈1 s) so the bed does not pump between words, then an
#      asymmetric ramp: ~25 ms attack, ~160 ms release time constant;
#   5. advanced by DUCK_LOOKAHEAD_S so the dip is already down when the first
#      syllable lands, padded with "no speech" past the key's end;
#   6. gain = 1 − (1 − 10^(to_db/20)) × activity, back to 48 kHz stereo, and
#      `amultiply`'d into the bed.
# Every `aeval` names `c=same`/`c=mono`: an aeval with an unset layout makes
# the biquad after it silently pass through (measured: lowpass did nothing).
DUCK_KEY_RATE = 8000
DUCK_ENV_RATE = 1000
DUCK_KEY_FLOOR = 0.003          # dynaudnorm threshold: frames quieter than ≈ −50 dBFS peak stay put
DUCK_THRESHOLD_DB = -40.0       # speech-activity threshold on the normalised key's envelope
DUCK_ABS_THRESHOLD_DB = -55.0   # …and on the RAW key's 20 ms mean-|x| (a −33 dBFS-peak talker reads ≈ −43)
DUCK_LOOKAHEAD_S = 0.06
_DUCK_HOLD_HZ = 0.5             # 1-pole; 1 % decay ≈ 1.5 s after sustained speech
_DUCK_ATTACK_HZ = 6.0
_DUCK_RELEASE_HZ = 1.0

#: A render-scoped request for a STEM instead of the mix (see `stem_scope`).
_STEM: contextvars.ContextVar[str | None] = contextvars.ContextVar("vai_audio_stem", default=None)
#: The one stem there is: a 1 kHz carrier multiplied by the duck gain the
#: renderer computed — what the prompt verifier measures (QA-079: it used to
#: "verify" a −18 dB duck by reading `to_db` back from the EDL).
STEM_DUCK_PROBE = "duck_probe"
DUCK_PROBE_AMPLITUDE = 0.5


@contextlib.contextmanager
def stem_scope(stem: str | None) -> Iterator[None]:
    """Within this scope `build_audio_mix` outputs `stem` instead of the mix."""
    tok = _STEM.set(stem)
    try:
        yield
    finally:
        _STEM.reset(tok)


# ------------------------------------------------------ preview loudness
#
# QA-082: see render/preview_loudness.py. A preview render in this scope ends
# its mix with an `ebur128` tap (pre-gain integrated loudness, printed to
# `meas_path`), a static gain, and a −1 dBFS brick-wall limiter.

#: −1 dBFS ceiling, no auto-level (alimiter's default `level=1` would scale
#: the output back up to 0 dBFS), latency-compensated like the mix limiter.
PREVIEW_LIMITER = "alimiter=limit=0.891251:level=0:latency=1,aresample=48000"


class PreviewLoudness:
    __slots__ = ("gain_db", "meas_path")

    def __init__(self, gain_db: float, meas_path: Path):
        self.gain_db = float(gain_db)
        self.meas_path = Path(meas_path)


_PREVIEW_LOUDNESS: contextvars.ContextVar[PreviewLoudness | None] = contextvars.ContextVar(
    "vai_preview_loudness", default=None)


@contextlib.contextmanager
def preview_loudness_scope(pl: PreviewLoudness | None) -> Iterator[None]:
    tok = _PREVIEW_LOUDNESS.set(pl)
    try:
        yield
    finally:
        _PREVIEW_LOUDNESS.reset(tok)


def _preview_norm_chain(edl: EDL) -> str:
    """`,ebur128…,volume…,alimiter…` for a preview inside a loudness scope
    (the leading comma included), else ""."""
    pl = _PREVIEW_LOUDNESS.get()
    if pl is None or getattr(edl.canvas, "loudness_lufs", None) is None:
        return ""
    from .. import platformutil as _pu
    meas = _pu.ffmpeg_filter_path(pl.meas_path)
    return (f",ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.I:file={meas}"
            f",volume={pl.gain_db:.2f}dB,{PREVIEW_LIMITER}")


def duck_gain_chain(key_label: str, to_db: float, out_label: str) -> str:
    """Filter chain turning the speech key at `key_label` into the duck gain
    (48 kHz stereo, 1.0 = untouched, 10^(to_db/20) while speech plays)."""
    depth = 1.0 - 10.0 ** (min(0.0, float(to_db)) / 20.0)
    thr = 10.0 ** (DUCK_THRESHOLD_DB / 20.0)
    abs_thr = 10.0 ** (DUCK_ABS_THRESHOLD_DB / 20.0)
    b = out_label.strip("[]") or "duck"
    envelope = f"aeval='abs(val(0))':c=same,lowpass=f=8:p=1,aresample={DUCK_ENV_RATE}"
    return (
        f"{key_label}pan=mono|c0=0.5*c0+0.5*c1,highpass=f=100,lowpass=f=6000,"
        f"aresample={DUCK_KEY_RATE},asplit=2[{b}_kn][{b}_kr];"
        # Activity on the NORMALISED key (a quiet talker still reads as speech)…
        f"[{b}_kn]dynaudnorm=f=250:g=15:p=0.9:m=100:t={DUCK_KEY_FLOOR}:b=1,"
        f"{envelope},aeval='gte(val(0),{thr:.6g})':c=same[{b}_gn];"
        # …AND on the raw key's absolute level: dynaudnorm's boundary mode
        # (b=1, kept so the first word is caught) inflates the first ~300 ms
        # of a room-tone key ~70x, which read as speech and dipped the bed at
        # the head of ~25 % of renders. Room tone never clears DUCK_ABS_THRESHOLD_DB.
        f"[{b}_kr]{envelope},aeval='gte(val(0),{abs_thr:.6g})':c=same[{b}_gr];"
        f"[{b}_gn][{b}_gr]amultiply,"
        f"lowpass=f={_DUCK_HOLD_HZ}:p=1,aeval='gte(val(0),0.01)':c=same,"
        f"pan=stereo|c0=c0|c1=c0,"
        f"lowpass=f={_DUCK_ATTACK_HZ}:p=1:c=FL,lowpass=f={_DUCK_RELEASE_HZ}:p=1:c=FR,"
        f"aeval='max(val(0),val(1))':c=mono,"
        f"atrim=start={DUCK_LOOKAHEAD_S},asetpts=PTS-STARTPTS,apad,"
        f"aeval='1-{depth:.6f}*val(0)':c=same,"
        f"aresample=48000,pan=stereo|c0=c0|c1=c0{out_label}"
    )


# ------------------------------------------------------- pro audio tools
#
# QA-086: volume automation, solo, varispeed. Each helper is the ONE place its
# rule lives; the v1 chain, the PIP fold and the audio lanes all call it.

def gain_env_filter(audio) -> str:
    """`volume=…:eval=frame` for a clip's volume automation (`AudioProps.
    gain_env`, dB offsets on clip-local timeline seconds), or "" when there is
    none. Must sit where the stream's `t` IS clip-local time (before any
    `adelay` that places it on the timeline)."""
    env = getattr(audio, "gain_env", None) if audio is not None else None
    if env is None or not getattr(env, "keyframes", None):
        return ""
    from ..edl.keyframes import to_ffmpeg_expr
    return f"volume=volume=pow(10\\,({to_ffmpeg_expr(env)})/20):eval=frame"


def varispeed_filter(speed: float) -> str:
    """Sample-exact speed change whose pitch follows the speed (tape
    varispeed): the samples are re-clocked, not time-stretched, so every
    transient lands exactly at source_time / speed. The re-clock rate is an
    integer (asetrate), so the speed is exact to 1/48000 — under 1 ms of drift
    across 100 s of source."""
    rate = max(1, int(round(48000 * float(speed))))
    return f"asetrate={rate},aresample=48000"


def apply_solo(edl: EDL) -> EDL:
    """The EDL the audio renders hear: while any track is soloed, every clip on
    an audio-bearing track that is NOT soloed is muted (clip `audio.mute`,
    which every render path honours). Returns `edl` itself when nothing is
    soloed — the common case costs nothing and changes nothing."""
    if not any(getattr(t, "solo", False) for t in edl.tracks):
        return edl
    out = edl.model_copy(deep=True)
    for t in out.tracks:
        if t.solo or t.type not in ("video", "audio", "music", "vo"):
            continue
        for c in t.clips:
            if isinstance(c, Clip):
                c.audio.mute = True
    return out


def _esc_path(p: str) -> str:
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _audio_clip_filter(in_label: str, clip: Clip, out_label: str,
                       *, window: tuple[float, float] | None = None) -> str:
    """Single-clip transform: resample → trim → delay → gain → fade in/out.

    `window` is the clip's `[start, end)` on the RENDER clock (render/clock.py);
    None means "no transitions", where render time is layout time. Every
    absolute instant here — the delay, both fade edges — is a render instant:
    a bed authored under a word must dip and swell where the word is HEARD,
    and the v1 speech it mixes with has already been pulled left by the
    cross-fades before it.
    """
    rs, re = window if window is not None else (float(clip.start), float(clip.start) + clip.duration)
    parts = [
        "aresample=async=1:first_pts=0",
        "aformat=channel_layouts=stereo:sample_rates=48000",
    ]
    # A clip straddling a seam is SHORTER on the render clock by what the
    # seam consumed — its end must land where the v1 frame at its layout end
    # lands, not run on past it. Trimmed before the delay so the cut is
    # measured from the clip's own first sample.
    if re - rs < clip.duration - 0.0005:
        parts.append(f"atrim=duration={max(0.0, re - rs):.3f}")
    # Volume automation in the clip's OWN time — before the adelay below
    # moves `t` onto the render clock (QA-086).
    env = gain_env_filter(clip.audio)
    if env:
        parts.append(env)
    # Position on the render clock via adelay (ms, per channel)
    delay_ms = max(0, int(round(rs * 1000)))
    if delay_ms > 0:
        parts.append(f"adelay=delays={delay_ms}|{delay_ms}:all=1")
    # Gain from clip.audio.gain_db
    gain = clip.audio.gain_db if clip.audio else 0.0
    if abs(gain) > 0.01:
        parts.append(f"volume={gain:.2f}dB")
    # Fades
    if clip.audio and clip.audio.fade_in > 0.001:
        parts.append(f"afade=t=in:st={rs:.3f}:d={clip.audio.fade_in:.3f}")
    if clip.audio and clip.audio.fade_out > 0.001:
        # From the RENDER end: a bed that ran to the layout end of a timeline
        # with transitions used to start its fade-out past the file's end and
        # was simply cut off — its fade never played.
        st = max(0.0, re - clip.audio.fade_out)
        parts.append(f"afade=t=out:st={st:.3f}:d={clip.audio.fade_out:.3f}")
    if clip.audio and clip.audio.mute:
        parts.append("volume=0")
    return f"{in_label}{','.join(parts)}{out_label}"


def _on_render_clock(clips: list[Clip], seams: clock.SeamTable
                     ) -> list[tuple[Clip, tuple[float, float]]]:
    """`(clip, render_window)` for the clips the seams leave audible, in the
    order given. The layout window is `[start, start + duration)`: audio lanes
    apply no speed, so source seconds are timeline seconds here."""
    placed: list[tuple[Clip, tuple[float, float]]] = []
    for c in clips:
        win = clock.render_window(seams, c.start, c.start + c.duration)
        if win is not None:
            placed.append((c, win))
    return placed


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
    # Plain audio lanes (the stock `a1` "Main audio" track, plus any other
    # type=="audio" track) were read by NO render path: clips dropped there were
    # silent, yet still extended `edl.duration`. The UI shows the lane and
    # `laneAcceptsMediaClip` happily accepts drops on it, so it has to render.
    # Folded in with the voiceover group — same per-clip filter, same mix stage.
    for t in edl.tracks:
        if t.type == "audio" and not t.muted:
            vo_clips += [c for c in t.clips if isinstance(c, Clip)]

    # Every lane below is positioned on the RENDER clock. A clip the v1
    # cross-fades consumed entirely is left out here — no input, no filter —
    # so an all-consumed lane is the same as an empty one.
    seams = clock.seam_table(edl)
    music_placed = _on_render_clock(music_clips, seams)
    vo_placed = _on_render_clock(vo_clips, seams)

    if not music_placed and not vo_placed:
        # Still apply loudnorm on the speech-only path if a target is set
        # AND we're in export mode. Preview skips it (see docstring).
        lufs = getattr(edl.canvas, "loudness_lufs", None)
        if lufs is not None and apply_loudnorm:
            return (
                f"{main_audio_label}loudnorm=I={float(lufs):.1f}:TP=-1:LRA=11,"
                f"aresample=48000:async=1{out_label}",
                [], out_label,
            )
        preview_norm = "" if apply_loudnorm else _preview_norm_chain(edl)
        if preview_norm:
            return f"{main_audio_label}{preview_norm.lstrip(',')}{out_label}", [], out_label
        return "", [], main_audio_label

    extra_inputs: list[str] = []
    parts: list[str] = []
    next_idx = first_input_index

    music_labels: list[str] = []
    for c, win in music_placed:
        # Read source from `c.in` to `c.out`
        extra_inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        in_label = f"[{next_idx}:a]"
        out = f"[m{next_idx}]"
        parts.append(_audio_clip_filter(in_label, c, out, window=win))
        music_labels.append(out)
        next_idx += 1

    vo_labels: list[str] = []
    for c, win in vo_placed:
        extra_inputs += ["-ss", f"{c.in_:.3f}", "-to", f"{c.out:.3f}", "-i", c.src]
        in_label = f"[{next_idx}:a]"
        out = f"[vo{next_idx}]"
        parts.append(_audio_clip_filter(in_label, c, out, window=win))
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

    # Mix voiceover + plain audio-lane clips → [vo_mix]. Built BEFORE the duck
    # stage because it is part of the duck key (QA-029).
    vo_mix_label: str | None = None
    if vo_labels:
        if len(vo_labels) == 1:
            vo_mix_label = vo_labels[0]
        else:
            vo_mix_label = "[vo_mix]"
            parts.append(f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=longest:dropout_transition=0:normalize=0{vo_mix_label}")

    # Apply ducking to music if requested
    if music_mix_label and music_track and music_track.duck:
        ducked = "[music_ducked]"
        # The duck KEY is EVERY speech-bearing lane — v1 (+ folded PiP audio)
        # AND the voiceover / a1 audio lanes. QA-029: the key used to be v1
        # only, so the textbook case (a bed under a voiceover) never ducked at
        # all: the VO was mixed in after the ducker and never reached its key.
        # (MusicDuck.track_ref is still not read — "duck under all speech" is
        # what the UI's "Duck under speech" checkbox promises.)
        # Each keyed stream is tee'd via asplit because the key chain consumes
        # its input and the stream is still mixed below.
        key = "[main_for_sc]"
        parts.append(f"{main_audio_label}asplit=2[main_for_mix][main_for_sc]")
        main_audio_label = "[main_for_mix]"
        if vo_mix_label:
            parts.append(f"{vo_mix_label}asplit=2[vo_for_mix][vo_for_sc]")
            vo_mix_label = "[vo_for_mix]"
            # duration=first: the key must span the main audio, which spans
            # the timeline; normalize=0 so a quiet VO is not halved.
            parts.append("[main_for_sc][vo_for_sc]amix=inputs=2:duration=first:"
                         "dropout_transition=0:normalize=0[duck_key]")
            key = "[duck_key]"
        # QA-079: a gain envelope at exactly `to_db`, not a compressor whose
        # dip followed the talker's level (see `duck_gain_chain`).
        parts.append(duck_gain_chain(key, music_track.duck.to_db, "[duck_gain]"))
        if _STEM.get() == STEM_DUCK_PROBE:
            # The verifier's stem: the gain itself, riding a steady carrier
            # that spans the main audio (so the render ends with the timeline).
            parts.append(f"{main_audio_label}aeval='{DUCK_PROBE_AMPLITUDE}*sin(2*PI*1000*t)'"
                         f":c=same[duck_carrier]")
            parts.append("[duck_carrier][duck_gain]amultiply[duck_probe]")
            parts.append(f"{music_mix_label}anullsink")
            if vo_mix_label:
                parts.append(f"{vo_mix_label}anullsink")
            return ";".join(parts), extra_inputs, "[duck_probe]"
        parts.append(f"{music_mix_label}[duck_gain]amultiply{ducked}")
        music_mix_label = ducked

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
    # QA-082: the preview's static loudness match (empty outside its scope).
    preview_norm = "" if apply_loudnorm else _preview_norm_chain(edl)

    if len(final_inputs) == 1:
        if norm_chain or preview_norm:
            # Apply loudnorm to the single source so we still hit the target.
            parts.append(f"{final_inputs[0]}{(norm_chain or preview_norm).lstrip(',')}{out_label}")
            return ";".join(parts), extra_inputs, out_label
        return ";".join(parts), extra_inputs, final_inputs[0]
    parts.append(
        f"{''.join(final_inputs)}amix=inputs={len(final_inputs)}:duration=first:dropout_transition=0:normalize=0"
        # `latency=1`: alimiter looks ahead by its 5 ms attack and, without
        # compensation, DELAYS everything it passes by that much — every
        # timeline with a music or VO lane played 5 ms late against the
        # picture (measured on a VO click placed on a flash; QA-002 lane).
        f"{norm_chain},alimiter=limit=0.97:latency=1{preview_norm}{out_label}"
    )
    return ";".join(parts), extra_inputs, out_label
