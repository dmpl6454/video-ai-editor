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


# ------------------------------------------------------- export mastering
#
# QA-121. Every export used to end in single-pass `loudnorm` (dynamic mode,
# which rides the gain and missed −14 by 1.2 LU on a heavy timeline) and then
# `alimiter=limit=0.97` — a SAMPLE-peak limiter at −0.26 dBFS with no
# oversampling, so the inter-sample peaks the AAC decoder reconstructs landed
# at −0.3 to −0.6 dBTP against the −1 dBTP every platform asks for. It also
# ate the first transient: loudnorm's dynamic gain opens over its first
# frames, and a click at t=0 read 17.4 ms late (QA-120's residual).
#
# The export now masters in two passes, like every loudness tool that has to
# land a number:
#   1. `compositor.measure_mix_loudness` renders the timeline's MIX once (audio
#      only, `export_measure_scope` — no gain, no limiter) through `ebur128`;
#   2. the real render applies the static gain `target − I`
#      (`export_gain_scope`) and a TRUE-PEAK limiter: the mix is upsampled 4x,
#      brick-walled at `EXPORT_TP_LIMIT_DB` there (so what it holds is the
#      inter-sample peak, not the sample peak) and brought back to 48 kHz.
# A static gain keeps the mix's dynamics (loudnorm's LRA=11 squeezed them) and
# keeps a duck exactly as deep as `to_db` says.

#: The delivery ceiling (dBTP) every export is measured against.
EXPORT_TRUE_PEAK_DBTP = -1.0
#: What the 4x-oversampled limiter holds: under the ceiling by a typical AAC
#: overshoot. That overshoot is NOT bounded by this margin — dense, limited
#: music re-added 0.8-2.6 dB at 192k with PNS — so the delivered file is
#: measured and, if over, re-limited after the encode
#: (`compositor._hold_delivery_true_peak`, QA-121).
EXPORT_TP_LIMIT_DB = -1.6
_TP_OVERSAMPLE = 192000
#: The most an export lifts or cuts to reach its target. Wider than the
#: preview's ±30 (render/preview_loudness): a delivery target is a spec, and a
#: −53 LUFS recording asked for −14 must arrive at −14 (the single-pass
#: loudnorm this replaces did). A gated-silent mix is never lifted at all.
EXPORT_MAX_GAIN_DB = 60.0


def true_peak_limiter() -> str:
    """The export's final stage (no labels): 4x oversampled brick wall at
    `EXPORT_TP_LIMIT_DB`, latency-compensated (`latency=1`: no delay against
    the picture, QA-120), back to 48 kHz."""
    lin = 10.0 ** (EXPORT_TP_LIMIT_DB / 20.0)
    return (f"aresample={_TP_OVERSAMPLE},"
            f"alimiter=limit={lin:.6f}:level=0:latency=1:attack=1:release=50,"
            f"aresample=48000")


_EXPORT_GAIN: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "vai_export_gain_db", default=None)
_EXPORT_MEASURE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "vai_export_measure", default=False)


@contextlib.contextmanager
def export_gain_scope(gain_db: float | None) -> Iterator[None]:
    """Within this scope an export's master stage is `volume=gain_db` + the
    true-peak limiter (pass 2). None keeps the single-pass fallback."""
    tok = _EXPORT_GAIN.set(None if gain_db is None else float(gain_db))
    try:
        yield
    finally:
        _EXPORT_GAIN.reset(tok)


@contextlib.contextmanager
def export_measure_scope() -> Iterator[None]:
    """Within this scope the export master stage is left out entirely, so the
    graph ends on the raw mix — what pass 1 measures."""
    tok = _EXPORT_MEASURE.set(True)
    try:
        yield
    finally:
        _EXPORT_MEASURE.reset(tok)


def export_gain_for(target_lufs: float, measured_lufs: float | None) -> float:
    """The static gain pass 2 applies: `target − I`, bounded; 0 for a gated-
    silent programme (ebur128 reports −70 — there is nothing to normalise)."""
    if measured_lufs is None or measured_lufs <= -69.0:
        return 0.0
    return max(-EXPORT_MAX_GAIN_DB, min(EXPORT_MAX_GAIN_DB, float(target_lufs) - float(measured_lufs)))


def _export_master(edl: EDL, *, mixed: bool) -> str:
    """The export's master stage (no labels, no leading comma), or "".

      * measure scope (pass 1): nothing — the raw mix is what is measured;
      * a gain from pass 2's scope: static gain + true-peak limiter;
      * a target with no measurement (a caller that did not run pass 1):
        single-pass loudnorm, then the same true-peak limiter;
      * no target: the limiter only where lanes were MIXED (a summed bed and
        voice can pass full scale); a lone v1 track is left exactly as is.
    """
    if _EXPORT_MEASURE.get():
        return ""
    lufs = getattr(edl.canvas, "loudness_lufs", None)
    gain = _EXPORT_GAIN.get()
    if lufs is not None and gain is not None:
        return f"volume={gain:.2f}dB,{true_peak_limiter()}"
    if lufs is not None:
        # loudnorm's own TP=-1 is a sample-peak ride at its 192 kHz rate — the
        # limiter after it is what holds the delivered true peak.
        return f"loudnorm=I={float(lufs):.1f}:TP=-1:LRA=11,{true_peak_limiter()}"
    return true_peak_limiter() if mixed else ""


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


#: 960 samples (20 ms @ 48 kHz) of silence ahead of every atempo stage — see
#: `speed_filters`.
ATEMPO_LAG = "adelay=delays=960S:all=1"

#: Restamp after every atempo stage. With ffmpeg 8.1, `adelay` + `atempo` emits
#: frames whose pts derive from AV_NOPTS_VALUE, and the v1 `concat` then fails
#: the whole export ("Invalid data found when processing input"): a 29.97
#: project with a 0.3x clip and two 0.8x clips in a row could not be exported
#: (tests/test_keep_pitch_export.py). WSOLA output is contiguous, so numbering
#: the samples changes no sample, only the timestamps downstream filters read.
ATEMPO_RESTAMP = "asetpts=N/SR/TB"


#: The keep-pitch (atempo/WSOLA) timing bound this build can promise, in ms:
#: what the Keep pitch tooltip (`lib/audioChannels.KEEP_PITCH_TITLE`) and
#: CLAUDE.md state, and what tests/test_c5_render_audio.py measures (QA-039).
KEEP_PITCH_MAX_OFFSET_MS = 20.0


def speed_filters(clip: Clip) -> str:
    """`,…` fragment retiming a clip's sound to its speed (no labels), or ""
    at 1x. The ONE speed rule for sound — v1, and (QA-086) the audio lanes.

    keep_pitch False → varispeed (sample-exact, pitch follows the speed).
    keep_pitch True → atempo, each stage preceded by `ATEMPO_LAG` of silence:
    ffmpeg's WSOLA emits content ~20 ms of ITS INPUT early (measured on a
    57-click track: −42.4 ms at 0.5x, −24.0 at 0.75x, −15.6 at 1.25x, −12.4 at
    1.5x — a constant 18-21 ms of source time, divided by the tempo). Delaying
    the stage's input by that much centres transients on the retimed picture
    at every tempo (QA-039), leaving WSOLA's own jitter, at the cost of that
    sliver of silence at the head of a retimed clip; the caller cuts the tail
    back to the clip's exact length.

    The render binary (Homebrew `ffmpeg`) has no `rubberband` — probed with a
    real null graph (tests/test_c5_render_audio.py) — and no other phase-
    vocoder stretcher, so WSOLA is the floor of keep-pitch timing on this
    build (CLAUDE.md, the Keep pitch tooltip). Measured on a click track at
    0.5-2x, v1 and the audio lanes: every transient within
    `KEEP_PITCH_MAX_OFFSET_MS` (worst seen −19.8 ms, a music lane at 0.5x;
    −17.3 ms on v1), the clip's first sound up to 20 ms late (the lag has
    nothing before it to centre against), and at 2x a 4 ms click can be
    dropped outright. Varispeed is the sample-exact choice.
    """
    sp = clip.speed
    if not (isinstance(sp, (int, float)) and sp and sp > 0 and sp != 1.0):
        return ""
    if not getattr(clip.audio, "keep_pitch", True):
        return "," + varispeed_filter(float(sp))
    out = ""
    remaining = float(sp)
    while remaining > 2.0:
        out += f",{ATEMPO_LAG},atempo=2.0,{ATEMPO_RESTAMP}"
        remaining /= 2.0
    while remaining < 0.5:
        out += f",{ATEMPO_LAG},atempo=0.5,{ATEMPO_RESTAMP}"
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.001:
        out += f",{ATEMPO_LAG},atempo={remaining:.4f},{ATEMPO_RESTAMP}"
    return out


# ------------------------------------------------------- channel mode
#
# QA-122. A camera recording on one input (a lav into the left jack) exports
# one-sided: L −15 dB, R −240 dB. Every clip chain forces a stereo LAYOUT
# (`aformat=channel_layouts=stereo`), which relabels channels but never moves
# signal between them, and there was no per-clip way to say "this sound is on
# the left only". `AudioProps.channels` is that switch, applied right after the
# layout is fixed, on every lane (v1, PIP, music/vo/audio).

#: `AudioProps.channels` → the pan that realises it (stereo in, stereo out).
CHANNEL_PANS = {
    "left": "pan=stereo|c0=c0|c1=c0",
    "right": "pan=stereo|c0=c1|c1=c1",
    "mono": "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
}


def channel_filter(audio) -> str:
    """`,pan=…` for a clip's channel mode, "" for plain stereo (no labels)."""
    mode = getattr(audio, "channels", "stereo") if audio is not None else "stereo"
    pan = CHANNEL_PANS.get(str(mode))
    return f",{pan}" if pan else ""


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


def input_seek(t: float) -> list[str]:
    """`-ss t` for an input-side seek, or nothing at all when `t` is the very
    start of the file (QA-120 residual).

    `-ss 0` is NOT a no-op on an AAC source — which is every import, since
    ingest normalises sound to AAC: the seek skips the encoder-priming packet
    the first real frame's overlap-add needs, so the first 21 ms (1024
    samples) decode as garbage. Measured on a click at 0.000 s: 0.80 in the
    source, 0.018 in the render, plus a ghost at 17.5 ms that read as the
    click arriving 17 ms late. A mid-file seek is unaffected (the demuxer
    pre-rolls a packet there); only the head needs this."""
    return ["-ss", f"{t:.6f}"] if t > 0 else []


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
    eff = clip.effective_duration
    rs, re = window if window is not None else (float(clip.start), float(clip.start) + eff)
    parts = [
        "aresample=async=1:first_pts=0",
        "aformat=channel_layouts=stereo:sample_rates=48000",
    ]
    # QA-122: the clip's channel mode (left/right to both, mono mix).
    chan = channel_filter(clip.audio)
    if chan:
        parts.append(chan.lstrip(","))
    # QA-086: speed on an audio lane — the v1 rule (`speed_filters`), so the
    # source's `in..out` fills `effective_duration` timeline seconds.
    retime = speed_filters(clip)
    if retime:
        parts.append(retime.lstrip(","))
    # A clip straddling a seam is SHORTER on the render clock by what the
    # seam consumed — its end must land where the v1 frame at its layout end
    # lands, not run on past it. A retimed clip is cut to its exact length
    # too (atempo's lag pads its head). Trimmed before the delay so the cut
    # is measured from the clip's own first sample.
    if retime or re - rs < eff - 0.0005:
        n = max(0, int(round(min(re - rs, eff) * 48000)))
        parts.append(f"atrim=end_sample={n}")
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
    order given. The layout window is `[start, start + effective_duration)`:
    an audio-lane clip is retimed by its speed like v1 (QA-086)."""
    placed: list[tuple[Clip, tuple[float, float]]] = []
    for c in clips:
        win = clock.render_window(seams, c.start, c.start + c.effective_duration)
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
    target; preview renders skip it for compatibility + speed. (Since wave C
    "loudnorm on" means the export MASTER stage — `_export_master`: the
    measured static gain + true-peak limiter, loudnorm only as a fallback.)
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
        # Still master the speech-only path when a target is set AND we're in
        # export mode (QA-121: gain + true-peak limiter). Preview skips it
        # (see docstring).
        master = _export_master(edl, mixed=False) if apply_loudnorm else ""
        if master:
            return f"{main_audio_label}{master}{out_label}", [], out_label
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
        extra_inputs += [*input_seek(float(c.in_)), "-to", f"{c.out:.6f}", "-i", c.src]
        in_label = f"[{next_idx}:a]"
        out = f"[m{next_idx}]"
        parts.append(_audio_clip_filter(in_label, c, out, window=win))
        music_labels.append(out)
        next_idx += 1

    vo_labels: list[str] = []
    for c, win in vo_placed:
        extra_inputs += [*input_seek(float(c.in_)), "-to", f"{c.out:.6f}", "-i", c.src]
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
    # Export: the master stage (QA-121 — static gain from the measuring pass
    # + a 4x-oversampled true-peak limiter; see `_export_master`). Its
    # trailing aresample pulls the rate back to 48k — the AAC encoder would
    # otherwise persist 96k, which Safari and a couple of phone browsers
    # reject inside mp4 containers.
    master = _export_master(edl, mixed=len(final_inputs) > 1) if apply_loudnorm else ""
    # QA-082: the preview's static loudness match (empty outside its scope).
    preview_norm = "" if apply_loudnorm else _preview_norm_chain(edl)

    if len(final_inputs) == 1:
        tail = master or preview_norm.lstrip(",")
        if tail:
            parts.append(f"{final_inputs[0]}{tail}{out_label}")
            return ";".join(parts), extra_inputs, out_label
        return ";".join(parts), extra_inputs, final_inputs[0]
    mix = (f"{''.join(final_inputs)}amix=inputs={len(final_inputs)}:duration=first"
           f":dropout_transition=0:normalize=0")
    if apply_loudnorm:
        # The export's own ceiling is the master stage (empty only while
        # pass 1 measures the raw mix).
        parts.append(f"{mix}{',' + master if master else ''}{out_label}")
        return ";".join(parts), extra_inputs, out_label
    parts.append(
        # `latency=1`: alimiter looks ahead by its 5 ms attack and, without
        # compensation, DELAYS everything it passes by that much — every
        # timeline with a music or VO lane played 5 ms late against the
        # picture (measured on a VO click placed on a flash; QA-002 lane).
        f"{mix},alimiter=limit=0.97:latency=1{preview_norm}{out_label}"
    )
    return ";".join(parts), extra_inputs, out_label
