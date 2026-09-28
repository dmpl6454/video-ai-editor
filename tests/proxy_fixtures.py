"""Frame-barcode media for the wave D proxy tests (INSTANT_PREVIEW_SPEC §13).

A barcode master burns its own frame index into every frame: a 12-cell strip
across the top, cell b white when bit b of the index is set. The bar survives
scaling, chroma subsampling and crf 23 easily (cells are tens of pixels wide
and the levels are 16 vs 235), so reading it back from a DECODED proxy frame
proves which master frame the proxy put there — the only honest test of frame
identity (R5).

Masters are encoded like ingest's normalised masters (x264 default GOP with
B-frames, hence an mp4 edit list), or in any pix_fmt/profile a test asks for.
"""
from __future__ import annotations

import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import numpy as np

BITS = 12
BAR_H = 32


def barcode_frame(i: int, w: int, h: int) -> np.ndarray:
    """A w×h grey frame (uint8) carrying index i in its top bar."""
    img = np.full((h, w), 110, dtype=np.uint8)
    # A little texture below the bar so the encoder has real work to do.
    yy, xx = np.mgrid[BAR_H:h, 0:w]
    img[BAR_H:, :] = ((xx * 3 + yy * 2 + i * 7) % 180 + 40).astype(np.uint8)
    cell = w // BITS
    for b in range(BITS):
        img[:BAR_H, b * cell:(b + 1) * cell] = 235 if (i >> b) & 1 else 16
    return img


def read_barcode(gray: np.ndarray, src_h: int | None = None) -> int:
    """The index in a decoded frame's bar; `src_h` = the master's height when
    the frame was scaled (the bar scales with it)."""
    h, w = gray.shape
    bar_h = max(2, int(round(BAR_H * h / (src_h or h))))
    cell = w / BITS
    v = 0
    for b in range(BITS):
        x0, x1 = int(b * cell + cell * 0.3), int((b + 1) * cell - cell * 0.3)
        y1 = max(1, int(bar_h * 0.7))
        if gray[1:y1, x0:x1].mean() > 125:
            v |= 1 << b
    return v


def make_barcode_master(path: Path, *, frames: int, rate: str = "30", w: int = 640,
                        h: int = 360, pix_fmt: str = "yuv420p", profile: str | None = None,
                        audio: bool = True, audio_seconds: float | None = None,
                        extra: list[str] | None = None) -> Path:
    """Encode `frames` barcode frames at `rate` into an mp4 master."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fr = Fraction(rate)
    dur = audio_seconds if audio_seconds is not None else frames / float(fr)
    args = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
            "-s", f"{w}x{h}", "-framerate", rate, "-i", "pipe:0"]
    if audio:
        args += ["-f", "lavfi", "-i",
                 f"aevalsrc=0.4*sin(2*PI*440*t)+0.2*sin(2*PI*1031*t)|0.3*sin(2*PI*660*t):s=48000:d={dur}"]
    args += ["-map", "0:v"] + (["-map", "1:a", "-c:a", "aac", "-b:a", "128k"] if audio else [])
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", pix_fmt]
    if profile:
        args += ["-profile:v", profile]
    args += (extra or []) + ["-shortest" if audio and audio_seconds is None else "-y", str(path)]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    for i in range(frames):
        proc.stdin.write(barcode_frame(i, w, h).tobytes())
    proc.stdin.close()
    err = proc.stderr.read() if proc.stderr else b""
    assert proc.wait() == 0, err.decode(errors="replace")
    return path


def decoded_frame_count(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip().split(",")[0])


def avcc_parameter_sets(avcc: bytes) -> list[bytes]:
    """SPS and PPS NAL units out of an avcC record."""
    out: list[bytes] = []
    n_sps = avcc[5] & 0x1F
    p = 6
    for _ in range(n_sps):
        ln = int.from_bytes(avcc[p:p + 2], "big")
        out.append(avcc[p + 2:p + 2 + ln])
        p += 2 + ln
    n_pps = avcc[p]
    p += 1
    for _ in range(n_pps):
        ln = int.from_bytes(avcc[p:p + 2], "big")
        out.append(avcc[p + 2:p + 2 + ln])
        p += 2 + ln
    return out


def sample_nals(sample: bytes) -> list[bytes]:
    nals, p = [], 0
    while p + 4 <= len(sample):
        ln = int.from_bytes(sample[p:p + 4], "big")
        nals.append(sample[p + 4:p + 4 + ln])
        p += 4 + ln
    assert p == len(sample), "AVCC sample lengths do not add up"
    return nals


def annexb(avcc: bytes, samples: list[bytes]) -> bytes:
    """An Annex-B elementary stream: the init's SPS/PPS, then every sample.
    Decoding it proves the samples decode with THAT init's parameter sets."""
    sc = b"\x00\x00\x00\x01"
    out = [sc + ps for ps in avcc_parameter_sets(avcc)]
    for s in samples:
        out.extend(sc + nal for nal in sample_nals(s))
    return b"".join(out)


def decode_gray(avcc: bytes, samples: list[bytes], w: int, h: int) -> np.ndarray:
    """Decode samples (with the init's SPS/PPS) to grey frames [n, h, w]."""
    # Through a temp FILE, not stdin: `subprocess.run(input=…)` into ffmpeg's
    # raw-h264 demuxer stalls on this macOS (measured: a 1.5 MB stream never
    # finished in 40 s, while the same bytes from a file decode in 0.5 s).
    with tempfile.NamedTemporaryFile(suffix=".h264") as fh:
        fh.write(annexb(avcc, samples))
        fh.flush()
        out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "h264", "-i", fh.name,
                              "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                             capture_output=True, check=True)
    arr = np.frombuffer(out.stdout, dtype=np.uint8)
    return arr.reshape(-1, h, w)


def y_psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def decode_audio_f32(path: Path) -> np.ndarray:
    """ffmpeg's decode of the first audio stream at 48 kHz stereo float, on
    the file clock (`proxy.AUDIO_FILTER`, the proxy's own decode)."""
    from video_ai_editor.ingest.proxy import AUDIO_FILTER
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
                          "-af", AUDIO_FILTER,
                          "-f", "f32le", "pipe:1"], capture_output=True, check=True)
    return np.frombuffer(out.stdout, dtype="<f4").reshape(-1, 2)


def make_flat_master(path: Path, *, y: int, u: int, v: int, frames: int = 6, w: int = 64,
                     h: int = 64, rate: str = "30", left_y: int | None = None,
                     tags: list[str] | None = None, pix_fmt: str = "yuv420p") -> Path:
    """A flat-patch master written LOSSLESSLY (qp 0) straight from raw
    yuv420p planes, so its decoded values are exactly (y, u, v) (the left
    half of the luma is `left_y` when given). `tags` are extra output args
    (colour range / matrix tags): the colour tests need a master whose
    stored values and tags are both known."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Y = np.full((h, w), y, dtype=np.uint8)
    if left_y is not None:
        Y[:, : w // 2] = left_y
    U = np.full((h // 2, w // 2), u, dtype=np.uint8)
    V = np.full((h // 2, w // 2), v, dtype=np.uint8)
    frame = Y.tobytes() + U.tobytes() + V.tobytes()
    args = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix_fmt,
            "-s", f"{w}x{h}", "-framerate", rate, "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", pix_fmt,
            *(tags or []), str(path)]
    subprocess.run(args, input=frame * frames, capture_output=True, check=True)
    return path


def decode_yuv420(avcc: bytes, samples: list[bytes], w: int, h: int) -> list[tuple]:
    """Decode samples to their stored 8-bit 4:2:0 planes, per frame (Y, U, V)
    — no range or matrix conversion, the numbers a decoder hands WebKit."""
    with tempfile.NamedTemporaryFile(suffix=".h264") as fh:
        fh.write(annexb(avcc, samples))
        fh.flush()
        out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "h264", "-i", fh.name,
                              "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"],
                             capture_output=True, check=True).stdout
    fs = w * h * 3 // 2
    res = []
    for k in range(len(out) // fs):
        b = np.frombuffer(out[k * fs:(k + 1) * fs], dtype=np.uint8)
        res.append((b[:w * h].reshape(h, w), b[w * h:w * h * 5 // 4].reshape(h // 2, w // 2),
                    b[w * h * 5 // 4:].reshape(h // 2, w // 2)))
    return res


def sps_vui(avcc: bytes, samples: list[bytes], tmp: Path) -> str:
    """ffprobe's reading of the SPS VUI (colour tags, SAR) of an avcC."""
    es = tmp / "vui.h264"
    es.write_bytes(annexb(avcc, samples[:2]))
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=color_space,color_primaries,color_transfer,color_range,"
                           "sample_aspect_ratio,width,height", "-of", "default=nw=1", str(es)],
                          capture_output=True, text=True).stdout
