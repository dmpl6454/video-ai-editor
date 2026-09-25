"""Measurement fixtures for frame-accuracy and A/V-sync tests (QA-002/030/038/039/040).

Two synthetic sources, both produced with ffmpeg lavfi so a test never needs
real footage, and two decoders that read back what a render ACTUALLY contains
— frame by frame, sample by sample — rather than trusting a container
duration.

* ``make_frame_counter`` — every frame carries its own index as ten luma
  bit-bands, so decoding an export says exactly which source frame each output
  frame is: a duplicated, dropped or shifted frame is visible as a repeated,
  missing or offset number.
* ``make_clap`` — a one-frame white flash and a 4 ms 1 kHz click at every whole
  second. The flash/click offset in an output is the A/V sync error.
"""
from __future__ import annotations

import subprocess
from array import array
from pathlib import Path

BANDS = 10          # bits encoded per frame (1024 distinct indices)
FC_W, FC_H = 320, 180
_BAND_W = FC_W // BANDS


def make_frame_counter(path: Path, *, frames: int = 600, fps: int = 30,
                       audio_extra_s: float = 0.0, tone_hz: int = 440) -> Path:
    """A ``frames``-frame clip whose frame N shows N in binary bands.

    ``audio_extra_s`` makes the audio stream LONGER than the video, which is
    what an AAC-normalised import looks like (container duration 20.01 s for
    a 600-frame, 20.000 s picture) — the case QA-002's import bug lived in.
    """
    dur_v = frames / fps
    dur_a = dur_v + audio_extra_s
    lum = f"if(mod(floor(N/pow(2\\,floor(X/{_BAND_W})))\\,2)\\,235\\,16)"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=black:s={FC_W}x{FC_H}:r={fps}:d={dur_v}",
         "-f", "lavfi", "-i", f"sine=f={tone_hz}:sample_rate=48000:duration={dur_a}",
         "-vf", f"format=gray,geq=lum='{lum}',format=yuv420p",
         "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-g", "30",
         "-c:a", "aac", "-b:a", "128k",
         str(path)],
        check=True, capture_output=True,
    )
    return path


def _raw_gray_frames(path: Path, w: int, h: int) -> list[bytes]:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-vf", f"scale={w}:{h}:flags=neighbor,format=gray",
         "-vsync", "passthrough", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    )
    buf = proc.stdout
    n = len(buf) // (w * h)
    return [buf[i * w * h:(i + 1) * w * h] for i in range(n)]


def decode_frame_numbers(path: Path) -> list[int]:
    """The source frame index shown by every decoded frame of ``path``.

    Rendered at any canvas size, the bands are rescaled back to 320x180 with
    nearest-neighbour sampling and read at the centre of each band. A frame
    that is not a counter frame (black filler) decodes as 0.
    """
    out: list[int] = []
    for fr in _raw_gray_frames(path, FC_W, FC_H):
        n = 0
        for b in range(BANDS):
            x0 = b * _BAND_W + _BAND_W // 4
            x1 = b * _BAND_W + 3 * _BAND_W // 4
            total = cnt = 0
            for y in range(FC_H // 4, 3 * FC_H // 4, 6):
                row = fr[y * FC_W:(y + 1) * FC_W]
                seg = row[x0:x1]
                total += sum(seg)
                cnt += len(seg)
            if total / cnt > 128:
                n |= 1 << b
        out.append(n)
    return out


def make_clap(path: Path, *, seconds: int = 12, fps: int = 30,
              audio_extra_s: float = 0.0) -> Path:
    """A flash (one white frame) and a click (4 ms, 1 kHz) at every whole second."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=black:s={FC_W}x{FC_H}:r={fps}:d={seconds}",
         "-f", "lavfi", "-i",
         (f"aevalsrc='if(lt(mod(t\\,1)\\,0.004)\\,0.8*sin(2*PI*1000*t)\\,0)'"
          f":s=48000:d={seconds + audio_extra_s}"),
         "-vf", f"format=gray,geq=lum='if(eq(mod(N\\,{fps})\\,0)\\,235\\,16)',format=yuv420p",
         "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-g", str(fps),
         "-c:a", "pcm_s16le" if path.suffix == ".mov" else "aac", "-b:a", "192k",
         "-ac", "1",
         str(path)],
        check=True, capture_output=True,
    )
    return path


def flash_times(path: Path) -> list[float]:
    """Presentation times (s) of every bright frame in ``path``'s video."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts_time", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True)
    pts = [float(x.strip(",")) for x in proc.stdout.split() if x.strip(",").strip()]
    frames = _raw_gray_frames(path, 32, 18)
    out = []
    for t, fr in zip(pts, frames):
        if sum(fr) / len(fr) > 128:
            out.append(t)
    return out


def audio_samples(path: Path, *, rate: int = 48000) -> array:
    """Mono float32 PCM of ``path``'s first audio stream."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        check=True, capture_output=True)
    a = array("f")
    a.frombytes(proc.stdout)
    return a


def click_times(path: Path, *, rate: int = 48000, thresh: float = 0.2) -> list[float]:
    """Onset times (s) of every click in ``path``'s audio (≥0.3 s apart)."""
    a = audio_samples(path, rate=rate)
    out: list[float] = []
    last = -1e9
    for i, v in enumerate(a):
        if abs(v) > thresh and i - last > 0.3 * rate:
            out.append(i / rate)
            last = i
        elif abs(v) > thresh:
            last = i
    return out


def flash_onsets(path: Path) -> list[float]:
    """The first bright frame of every flash (a slowed clip shows one flash
    on several consecutive frames; only its onset is the sync point)."""
    fl = flash_times(path)
    return [f for i, f in enumerate(fl) if i == 0 or f - fl[i - 1] > 0.05]


def av_offsets_ms(path: Path) -> list[float]:
    """For every flash onset, (nearest click − flash) in milliseconds."""
    fl = flash_onsets(path)
    cl = click_times(path)
    offs = []
    for f in fl:
        if not cl:
            break
        c = min(cl, key=lambda x: abs(x - f))
        if abs(c - f) < 0.4:
            offs.append((c - f) * 1000.0)
    return offs


def rms_db_per_window(path: Path, *, win_s: float = 1.0, rate: int = 48000) -> list[float]:
    """Per-window RMS in dBFS of ``path``'s audio (mono mixdown)."""
    import math
    a = audio_samples(path, rate=rate)
    n = int(win_s * rate)
    out = []
    for i in range(0, len(a) - n + 1, n):
        seg = a[i:i + n]
        ms = sum(x * x for x in seg) / n
        out.append(10 * math.log10(ms) if ms > 1e-24 else -240.0)
    return out


def probe_video(path: Path) -> dict:
    import json
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,duration,r_frame_rate",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True)
    d = json.loads(proc.stdout)
    s = d["streams"][0]
    return {"frames": int(s["nb_read_frames"]),
            "duration": float(s.get("duration") or 0.0),
            "format_duration": float(d["format"]["duration"])}
