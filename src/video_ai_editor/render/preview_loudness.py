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
    encode, the same cost as the audio-only remux fast path.

A brick-wall limiter at −1 dBFS follows the gain, the same true-peak ceiling
the export's loudnorm is given (`TP=-1`), so a +20 dB lift cannot clip.
"""
from __future__ import annotations

import contextlib
import json
import re
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


def _state_path(session_dir: Path) -> Path:
    return Path(session_dir) / "cache" / _STATE


def _last_pre_gain_lufs(session_dir: Path) -> float | None:
    try:
        v = json.loads(_state_path(session_dir).read_text(encoding="utf-8")).get("pre_gain_lufs")
        return float(v) if v is not None else None
    except Exception:
        return None


def _remember(session_dir: Path, pre_gain_lufs: float) -> None:
    p = _state_path(session_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pre_gain_lufs": round(pre_gain_lufs, 2)}), encoding="utf-8")
    _pu.replace_with_retry(tmp, p)


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
def matched(edl: EDL, session_dir: Path, dst: Path) -> Iterator[None]:
    """Run a preview render of `edl` into `dst` loudness-matched to the
    export. No-op when the project has no loudness target."""
    target = getattr(edl.canvas, "loudness_lufs", None)
    if target is None:
        yield
        return
    session_dir = Path(session_dir)
    meas = session_dir / "cache" / f"loudness_{dst.stem}.txt"
    meas.parent.mkdir(parents=True, exist_ok=True)
    _pu.unlink_with_retry(meas)
    applied = _gain_for(target, _last_pre_gain_lufs(session_dir))
    try:
        with audio_mix.preview_loudness_scope(audio_mix.PreviewLoudness(gain_db=applied, meas_path=meas)):
            yield
    except BaseException:
        # A failed or superseded render: nothing was measured that means anything.
        _pu.unlink_with_retry(meas)
        raise
    try:
        pre = read_integrated(meas)
        if pre is None or not dst.exists():
            return
        _remember(session_dir, pre)
        wanted = _gain_for(target, pre)
        if abs(wanted - applied) > TOLERANCE_LU:
            regain(dst, wanted - applied)
    finally:
        _pu.unlink_with_retry(meas)
