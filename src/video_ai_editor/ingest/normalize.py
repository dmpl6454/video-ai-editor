"""Normalize uploads to constant-framerate, common codec, common sample rate.

Without this, frame-accurate cuts and concat filters break on variable-framerate
sources or mixed sample rates.

HDR (BT.2020 PQ/HLG) sources need tone-mapping to look right in SDR. The cleanest
path uses `zscale` + `tonemap` filters, but those depend on libzimg which the
brew ffmpeg formula doesn't include. We try the zscale path first; on failure
we fall back to `colorspace` (built-in, less accurate but plays); finally we
fall back to a plain yuv420p decode (washed-out but at least viewable).
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from .probe import probe, ProbeResult
from .. import platformutil as _pu


_HDR_TRANSFERS = {"smpte2084", "smpte428", "arib-std-b67"}


def _has_filter(name: str) -> bool:
    """Cheap check: is this filter available in the local ffmpeg build?"""
    try:
        out = subprocess.run([_pu.FFMPEG, "-hide_banner", "-filters"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return any(line.split()[1:2] == [name] for line in out.stdout.splitlines())
    except Exception:
        return False


def _color_meta(src: Path) -> dict:
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer,color_primaries,color_space,pix_fmt",
             "-of", "json", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
        data = json.loads(out.stdout)
        return (data.get("streams") or [{}])[0]
    except Exception:
        return {}


def _is_hdr(meta: dict) -> bool:
    return (meta.get("color_transfer") or "").lower() in _HDR_TRANSFERS


def _vf_for_height(height: int | None) -> str:
    return f"scale=-2:{height}" if height is not None else ""


def _has_audio(src: Path) -> bool:
    """True if `src` has at least one audio stream."""
    try:
        out = subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        return bool(out.stdout.strip())
    except Exception:
        return True  # assume audio; the render has its own fallbacks


def _attempts(src: Path, dst: Path, fps: int, sample_rate: int, channels: int,
              height: int | None, hdr: bool) -> list[list[str]]:
    """Ordered list of ffmpeg arg-sets to try, simplest-likely-to-work last."""
    common_audio = ["-c:a", "aac", "-ar", str(sample_rate), "-ac", str(channels)]
    common_video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-fps_mode", "cfr", "-r", str(fps)]
    tail = ["-movflags", "+faststart", str(dst)]
    has_audio = _has_audio(src)

    def with_vf(vf: str) -> list[str]:
        # When the source has no audio (silent screen recordings, GIF-style
        # clips), inject a silent stereo track so EVERY normalized file has
        # audio. The renderer concats per-clip [i:a] streams; a missing audio
        # stream there fails the whole filtergraph ("':a' matches no streams").
        if has_audio:
            a = [_pu.FFMPEG, "-y", "-i", str(src), *common_video]
        else:
            a = [_pu.FFMPEG, "-y", "-i", str(src),
                 "-f", "lavfi", "-i",
                 f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
                 "-map", "0:v:0", "-map", "1:a:0", "-shortest", *common_video]
        if vf:
            a += ["-vf", vf]
        else:
            a += ["-pix_fmt", "yuv420p"]
        return a + common_audio + tail

    attempts: list[list[str]] = []
    scale = _vf_for_height(height)

    # Attempt 1: HDR-aware via zscale+tonemap (best, but needs libzimg)
    if hdr and _has_filter("zscale"):
        chain = ("zscale=t=linear:npl=100,format=gbrpf32le,"
                 "tonemap=tonemap=hable:desat=0,"
                 "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p")
        attempts.append(with_vf(",".join([chain, scale]) if scale else chain))

    # Attempt 2: HDR-aware via colorspace (built-in, no real tonemap but plays)
    if hdr:
        chain = "colorspace=all=bt709:trc=bt709:fast=1,format=yuv420p"
        attempts.append(with_vf(",".join([chain, scale]) if scale else chain))

    # Attempt 3: plain SDR / pix_fmt yuv420p
    attempts.append(with_vf(scale))

    # Attempt 4: video-only fallback (drop audio entirely; some sources have
    # weird audio codecs we can't transcode)
    fallback = [_pu.FFMPEG, "-y", "-i", str(src), "-an", *common_video,
                "-pix_fmt", "yuv420p"]
    if scale:
        fallback += ["-vf", scale]
    fallback += tail
    attempts.append(fallback)

    return attempts


def normalize(src: Path, dst: Path, fps: int = 30, sample_rate: int = 48000,
              channels: int = 2, height: int | None = None) -> ProbeResult:
    """Re-encode to CFR H.264 yuv420p + AAC stereo. Tries multiple strategies."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    meta = _color_meta(src)
    hdr = _is_hdr(meta)
    last_proc: subprocess.CompletedProcess | None = None
    for args in _attempts(src, dst, fps, sample_rate, channels, height, hdr):
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return probe(dst)
        last_proc = proc

    msg = last_proc.stderr[-1200:] if last_proc else "(no attempts ran)"
    raise RuntimeError(
        f"Couldn't normalize this video. It may use a codec or container we don't "
        f"yet support. Try exporting as standard H.264 .mp4 and re-uploading.\n\n"
        f"ffmpeg said:\n{msg}"
    )


def make_proxy(src: Path, dst: Path, height: int = 540, fps: int = 30) -> ProbeResult:
    """Low-res proxy media for fast preview rendering on long-form sources."""
    return normalize(src, dst, fps=fps, sample_rate=48000, channels=2, height=height)
