"""Loudness-matched PREVIEW audio (QA-082).

The export normalises to `canvas.loudness_lufs` with `loudnorm`; the preview
cannot (its 192 kHz internal rate yields 96 kHz AAC that Safari refuses in
mp4 — see audio_mix.build_audio_mix), so it played the raw mix: quiet
dialogue monitored at −36.6 LUFS against a −15.7 export, a 21 LU gap, and an
editor mixing by ear was mixing a different film.

The preview now applies a STATIC gain `target − I` in the graph, where I is
the pre-gain integrated loudness, measured by an in-graph `ebur128` tap on the
very render that plays (no extra pass):

  * the gain comes from the session's last measurement (a split or a volume
    nudge moves I by a fraction of a LU, so steady-state edits are right the
    first time and cost nothing);
  * after the render the tap's I is read back; when the gain it implies differs
    from the one applied by more than `TOLERANCE_LU` (the first render of a
    session, a new music bed…) the audio is re-gained in place — one AAC
    encode, the same cost as the audio-only remux fast path;
  * unless the limiter below could have engaged at the gain applied (the
    tap's pre-gain sample peak × that gain reaching −1 dBFS): a re-gain of a
    limited file only turns the SQUASH up or down (two overlapping 0.8 sines
    came out 4.8 dB low in their overlap on a session's first render, Final
    QA), so the audio is rendered again at the wanted gain instead — the
    audio-only remux, one pass through gain and limiter like the export.

A brick-wall limiter at −1 dBFS follows the gain, the same true-peak ceiling
the export's loudnorm is given (`TP=-1`), so a +20 dB lift cannot clip.

The Instant preview (client engine) plays the same master stage in the
browser (audioPlan.ts: gain × 10^(gain_db/20), then its limiter), so it needs
the gain each server preview really applied: `matched` records it per
`audio_key` — the sound of the render, not its whole hash, so a title or a
sticker (no sound) keeps it current — and `preview_gain` answers
GET /preview_loudness (Final QA r3: the Instant preview played the raw mix,
~11 dB under the export, and said EXACT).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Iterator

from .. import platformutil as _pu
from ..edl import EDL
from . import audio_mix
from . import cancel as _cancel

#: A measured miss larger than this triggers the in-place re-gain.
TOLERANCE_LU = 1.0
#: Never lift or cut a preview by more than this (a near-silent timeline
#: must not be amplified into its noise floor).
MAX_GAIN_DB = 30.0
_STATE = "preview_loudness.json"
_I_RE = re.compile(r"lavfi\.r128\.I=(-?[\d.]+)")
_PEAK_RE = re.compile(r"lavfi\.r128\.sample_peak=(-?[\d.]+)")
#: The preview limiter's ceiling (audio_mix.PREVIEW_LIMITER, −1 dBFS), less
#: the tap's print precision (3 decimals) so a peak AT the ceiling counts.
_LIMIT = 0.891251 - 0.002
#: Applied gains kept per session (one per distinct sound; oldest dropped).
GAINS_KEPT = 64
#: Track types whose clips make sound (a text, sticker, effect or captions
#: track never does).
_SOUND_TRACK_TYPES = ("video", "audio", "music", "vo")
#: What of an overlay (non-v1 video) clip reaches the SOUND — the same
#: reduction the frontend's preview fingerprint (lib/previewFingerprint.ts)
#: makes, so an edit that changes this key always fires the background
#: render that measures it (a moved PiP must not leave the gain stale for
#: good).
_PIP_SOUND_FIELDS = ("id", "src", "start", "in", "out", "speed", "audio")
_STATE_LOCK = threading.Lock()


def _state_path(session_dir: Path) -> Path:
    return Path(session_dir) / "cache" / _STATE


def _read_state(session_dir: Path) -> dict:
    try:
        d = json.loads(_state_path(session_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state(session_dir: Path, d: dict) -> None:
    p = _state_path(session_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    _pu.replace_with_retry(tmp, p)


def _last_pre_gain_lufs(session_dir: Path) -> float | None:
    try:
        v = _read_state(session_dir).get("pre_gain_lufs")
        return float(v) if v is not None else None
    except Exception:
        return None


def _remember(session_dir: Path, pre_gain_lufs: float) -> None:
    with _STATE_LOCK:
        d = _read_state(session_dir)
        d["pre_gain_lufs"] = round(pre_gain_lufs, 2)
        _write_state(session_dir, d)


def _record_gain(session_dir: Path, key: str, gain_db: float) -> None:
    """The gain a preview of the sound `key` was rendered with."""
    with _STATE_LOCK:
        d = _read_state(session_dir)
        gains = d.get("gains") if isinstance(d.get("gains"), dict) else {}
        gains.pop(key, None)
        gains[key] = round(float(gain_db), 2)
        while len(gains) > GAINS_KEPT:
            gains.pop(next(iter(gains)))
        d["gains"] = gains
        _write_state(session_dir, d)


def audio_key(edl: EDL) -> str:
    """A hash of what the preview's SOUND is made of: the canvas (its
    loudness target, its rate) and every sound-bearing track — track-level
    id, z, mute, solo, duck and transitions, v1 and lane clips whole, overlay
    clips reduced to `_PIP_SOUND_FIELDS`. Text, stickers, effects and
    captions are not in it: a title does not change the gain."""
    from ..edl.schema import RENDER_BEHAVIOR_VERSION
    d = edl.model_dump(by_alias=True, mode="json")
    tracks = []
    for t in d.get("tracks") or []:
        if t.get("type") not in _SOUND_TRACK_TYPES:
            continue
        clips = t.get("clips") or []
        if t.get("type") == "video" and t.get("id") != "v1":
            clips = [{k: c.get(k) for k in _PIP_SOUND_FIELDS} for c in clips]
        tracks.append({k: t.get(k) for k in ("id", "type", "z", "muted", "solo", "duck", "transitions")}
                      | {"clips": clips})
    blob = {"v": RENDER_BEHAVIOR_VERSION, "canvas": d.get("canvas"), "tracks": tracks}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


def preview_gain(edl: EDL, session_dir: Path) -> dict:
    """What GET /preview_loudness answers for `edl` (the session's preview
    EDL): `gain_db` — the master gain the server preview of this sound was
    rendered with (`current` true), else the session's last-known gain
    (`current` false; None before any measurement) — and `target_lufs`.
    No target: nothing to apply (`gain_db` None) and nothing to wait for."""
    target = getattr(edl.canvas, "loudness_lufs", None)
    if target is None:
        return {"gain_db": None, "current": True, "target_lufs": None}
    state = _read_state(Path(session_dir))
    gains = state.get("gains") if isinstance(state.get("gains"), dict) else {}
    g = gains.get(audio_key(edl))
    if isinstance(g, (int, float)):
        return {"gain_db": float(g), "current": True, "target_lufs": float(target)}
    pre = _last_pre_gain_lufs(Path(session_dir))
    last = round(_gain_for(target, pre), 2) if pre is not None else None
    return {"gain_db": last, "current": False, "target_lufs": float(target)}


def _gain_for(target: float, pre_gain_lufs: float | None) -> float:
    if pre_gain_lufs is None:
        return 0.0
    return max(-MAX_GAIN_DB, min(MAX_GAIN_DB, float(target) - pre_gain_lufs))


def read_integrated(meas: Path) -> float | None:
    """The last integrated-loudness value the tap printed, or None (no file,
    or a gated-silent programme: ebur128 reports −70)."""
    try:
        hits = _I_RE.findall(meas.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not hits:
        return None
    v = float(hits[-1])
    return None if v <= -69.0 else v


def read_peak(meas: Path) -> float | None:
    """The pre-gain sample peak (linear, the tap's running max), or None."""
    try:
        hits = _PEAK_RE.findall(meas.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return float(hits[-1]) if hits else None


def limiter_could_engage(peak: float | None, gain_db: float) -> bool:
    """Whether the −1 dBFS preview limiter could have touched a mix of
    pre-gain sample `peak` at `gain_db` (unknown peak: assume it could)."""
    if peak is None:
        return True
    return peak * 10.0 ** (gain_db / 20.0) >= _LIMIT


def regain(path: Path, delta_db: float) -> None:
    """Re-encode `path`'s audio with `delta_db` more gain (picture copied)."""
    from .compositor import _part_path, _preview_aac_out
    tmp = _part_path(path)
    args = [_pu.FFMPEG, "-y", "-v", "error", "-i", str(path), "-map", "0:v?", "-map", "0:a",
            "-c:v", "copy", "-af", f"volume={delta_db:.2f}dB,{audio_mix.PREVIEW_LIMITER}",
            *_preview_aac_out(), "-movflags", "+faststart", str(tmp)]
    try:
        proc = _cancel.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", **_pu.SUBPROCESS_FLAGS)
    except BaseException:
        _pu.unlink_with_retry(tmp)
        raise
    if proc.returncode != 0:
        _pu.unlink_with_retry(tmp)
        raise RuntimeError(f"preview re-gain failed: {proc.stderr[-800:]}")
    _pu.replace_with_retry(tmp, path)


@contextlib.contextmanager
def matched(edl: EDL, session_dir: Path, dst: Path, *, fps: int | None = None) -> Iterator[None]:
    """Run a preview render of `edl` into `dst` loudness-matched to the
    export. No-op when the project has no loudness target. `fps` (the
    render's) lets a stale gain whose pass hit the limiter be re-rendered
    in one pass (`_rerender_audio`); without it the file is re-gained."""
    target = getattr(edl.canvas, "loudness_lufs", None)
    if target is None:
        yield
        return
    session_dir = Path(session_dir)
    meas = session_dir / "cache" / f"loudness_{dst.stem}.txt"
    meas.parent.mkdir(parents=True, exist_ok=True)
    peak_meas = audio_mix.preview_peak_path(meas)
    _pu.unlink_with_retry(meas)
    _pu.unlink_with_retry(peak_meas)
    applied = _gain_for(target, _last_pre_gain_lufs(session_dir))
    try:
        with audio_mix.preview_loudness_scope(audio_mix.PreviewLoudness(gain_db=applied, meas_path=meas)):
            yield
    except BaseException:
        # A failed or superseded render: nothing was measured that means anything.
        _pu.unlink_with_retry(meas)
        _pu.unlink_with_retry(peak_meas)
        raise
    try:
        pre = read_integrated(meas)
        if not dst.exists():
            return
        effective = applied
        if pre is not None:
            _remember(session_dir, pre)
            wanted = _gain_for(target, pre)
            if abs(wanted - applied) > TOLERANCE_LU:
                if (fps is not None and limiter_could_engage(read_peak(peak_meas), applied)
                        and _rerender_audio(edl, session_dir, dst, fps=fps, gain_db=wanted)):
                    effective = round(wanted, 2)
                else:
                    regain(dst, wanted - applied)
                    effective = round(applied, 2) + round(wanted - applied, 2)
        # what this preview's sound really carries (the Instant preview
        # plays the same master gain: GET /preview_loudness)
        _record_gain(session_dir, audio_key(edl), effective)
    finally:
        _pu.unlink_with_retry(meas)
        _pu.unlink_with_retry(peak_meas)


def _rerender_audio(edl: EDL, session_dir: Path, dst: Path, *, fps: int, gain_db: float) -> bool:
    """Replace `dst`'s audio with the timeline's sound rendered at `gain_db`
    in one pass (the audio-only remux, picture copied from `dst` itself).
    False when it failed (the caller falls back to `regain`); a cancel
    propagates."""
    from .compositor import _remux_with_new_audio
    meas = Path(session_dir) / "cache" / f"loudness_{dst.stem}_rr.txt"
    try:
        with audio_mix.preview_loudness_scope(audio_mix.PreviewLoudness(gain_db=gain_db, meas_path=meas)):
            _remux_with_new_audio(edl, dst, dst, fps=fps, cache_dir=Path(session_dir) / "cache")
        return True
    except _cancel.RenderCancelled:
        raise
    except Exception as e:
        logging.getLogger(__name__).warning(
            "preview loudness: one-pass audio re-render failed, re-gaining instead: %s", e)
        return False
    finally:
        _pu.unlink_with_retry(meas)
        _pu.unlink_with_retry(audio_mix.preview_peak_path(meas))
