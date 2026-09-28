"""Voice effects over HTTP (wave E, F3 — CapCut's voice changer).

  GET  /api/voice/presets                       the ONE table
                                                (`edl/voice_effects.payload`):
                                                id, label, hint, icon, stages
  GET  /api/sessions/{sid}/voice/sound/{clip}   {has_audio}: may this clip take an effect
  POST /api/sessions/{sid}/voice/preview        a short WAV of one clip's sound
       {clip_id, effect, intensity?, at?,        through an effect, NOT committed
        seconds?}                                (the Inspector's preview button)

The table is `edl/voice_effects.py`; `agent/dispatch.set_voice_effect`, the
agent tool schema and the Prompt Editor read the same module, and the
preview engine reads its JSON dump (`lib/voice/voiceFxTable.json`, pinned
equal by tests/test_voice_effects.py).

The preview is server-rendered by the export's own chain (`compositor.
_audio_only_graph` over a one-clip timeline), so what the button plays is
what the export will: the clip's retime, channel mode and gain, the effect,
then nothing else (no fades, no automation, not muted — it is an audition).
The window starts `at` seconds into the clip (clip-local timeline time,
default 0) and lasts `seconds` (default 4, at most 8), within the clip.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from ..edl import voice_effects as _vfx

router = APIRouter(tags=["voice"])

#: Longest audition, seconds (and the default).
PREVIEW_MAX_S = 8.0
PREVIEW_DEFAULT_S = 4.0
#: Wall-clock bound of one audition render.
PREVIEW_TIMEOUT_S = 30.0

_resolve_store: Callable[[str], Any] | None = None


def configure(*, resolve_store: Callable[[str], Any]) -> None:
    global _resolve_store
    _resolve_store = resolve_store


@router.get("/api/voice/presets")
def voice_presets() -> dict:
    """`{presets: [{id, label, hint, icon, aliases, stages}], intensity_range,
    default_intensity, …}` — read-only, immutable for a build."""
    return _vfx.payload()


@router.get("/api/sessions/{sid}/voice/sound/{clip_id}")
def voice_sound(sid: str, clip_id: str) -> dict:
    """`{has_audio}`: whether this clip carries sound a voice effect can
    change — no freeze frame, and a source file with an audio stream (probed
    once per file, `compositor.source_has_audio`). Review RE: the Voice
    effects section was offered on a picture-only overlay, whose "Robot"
    could only play silence; set_voice_effect now refuses such a clip."""
    if _resolve_store is None:          # pragma: no cover - wiring error
        raise HTTPException(500, "voice routes are not configured")
    from ..edl.schema import Clip
    from ..render.compositor import source_has_audio
    store = _resolve_store(sid)
    res = store.edl.get_clip(str(clip_id))
    if not res or not isinstance(res[1], Clip):
        raise HTTPException(404, {"code": "clip_not_found", "message": f"no media clip {clip_id}"})
    c = res[1]
    return {"has_audio": c.freeze is None and source_has_audio(str(c.src))}


class VoicePreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip_id: str
    effect: str | None = None
    intensity: float = _vfx.DEFAULT_INTENSITY
    at: float = 0.0
    seconds: float = PREVIEW_DEFAULT_S


def _bad(msg: str) -> HTTPException:
    return HTTPException(400, {"code": "invalid_voice_preview", "message": msg})


def audition_edl(edl, clip_id: str, effect: str | None, intensity: float, at: float, seconds: float):
    """The one-clip timeline the audition renders: the clip alone on v1 at
    0, its window [at, at + seconds) of its own timeline length, with the
    effect and none of its fades / automation / mute. Returns (edl, window)."""
    from ..edl.schema import Canvas, Clip, empty_edl
    res = edl.get_clip(str(clip_id))
    if not res or not isinstance(res[1], Clip):
        raise _bad(f"clip {clip_id} is not a media clip on this timeline")
    src_clip: Clip = res[1]
    if src_clip.freeze is not None:
        raise _bad("a freeze frame has no sound to preview")
    pid = None if effect in (None, "", "none") else _vfx.preset_id(effect)
    if effect not in (None, "", "none") and pid is None:
        raise _bad(f"unknown voice effect {effect!r} — one of: {', '.join(_vfx.PRESET_IDS)}")
    try:
        inten = _vfx.check_intensity(intensity)
    except ValueError as e:
        raise _bad(str(e)) from None
    total = float(src_clip.effective_duration)
    if not (isinstance(at, (int, float)) and 0.0 <= float(at) < max(total, 1e-6)):
        at = 0.0
    seconds = max(0.25, min(PREVIEW_MAX_S, float(seconds))) if seconds == seconds else PREVIEW_DEFAULT_S
    c = src_clip.model_copy(deep=True)
    c.start = 0.0
    c.audio.mute = False
    c.audio.fade_in = 0.0
    c.audio.fade_out = 0.0
    c.audio.gain_env = None
    c.audio.voice_effect = pid
    c.audio.voice_intensity = inten
    t0 = float(at)
    t1 = min(total, t0 + seconds)
    if c.speed in (None, 1.0) and not c.reverse:
        # A 1x clip: cut the source to the window itself, so a long bed is
        # not decoded from its start to reach it.
        c.in_ = float(src_clip.in_) + t0
        c.out = min(float(src_clip.out), c.in_ + (t1 - t0))
        t0, t1 = 0.0, float(c.out - c.in_)
    e = empty_edl(Canvas(w=edl.canvas.w, h=edl.canvas.h, fps=edl.canvas.fps))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips.append(c)
    e.recompute_duration()
    return e, (t0, t1)


def render_audition(edl, window: tuple[float, float], cache: Path) -> bytes:
    """WAV bytes (16-bit, 48 kHz stereo) of `window` of `edl`'s sound. Written
    to a file first: a piped WAV has no length in its header, which WebKit's
    <audio> refuses."""
    from .. import platformutil as _pu
    from ..render import audio_mix, compositor
    from ..render.reverse import with_reversed_sources
    fps = edl.canvas.fps
    sub = with_reversed_sources(audio_mix.apply_solo(edl), cache, fps)
    inputs, fc, label = compositor._audio_only_graph(sub, fps=fps, first_input=0, apply_loudnorm=False)
    t0, t1 = window
    with tempfile.TemporaryDirectory(dir=cache, prefix="voice-audition-") as tmp:
        dst = Path(tmp) / "audition.wav"
        argv = [_pu.FFMPEG, "-nostdin", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                *(["-ss", f"{t0:.6f}"] if t0 > 0 else []), "-t", f"{max(0.01, t1 - t0):.6f}",
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(dst)]
        try:
            out = subprocess.run(_pu.low_priority_argv(argv), capture_output=True, timeout=PREVIEW_TIMEOUT_S,
                                 **_pu.SUBPROCESS_FLAGS)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, {"code": "voice_preview_timed_out",
                                      "message": "the voice preview took too long"}) from None
        if out.returncode != 0 or not dst.exists():
            raise HTTPException(500, {"code": "voice_preview_failed",
                                      "message": "the voice preview could not be rendered"})
        return dst.read_bytes()


@router.post("/api/sessions/{sid}/voice/preview")
def voice_preview(sid: str, body: VoicePreviewBody) -> Response:
    if _resolve_store is None:                        # pragma: no cover — main.py configures it
        raise HTTPException(503, "voice routes not configured")
    store = _resolve_store(sid)
    edl, window = audition_edl(store.edl, body.clip_id, body.effect, body.intensity, body.at, body.seconds)
    cache = Path(store.dir) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    wav = render_audition(edl, window, cache)
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})
