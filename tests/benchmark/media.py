"""Benchmark media: lavfi scene video with KNOWN hard cuts, its 9:16 / b-roll /
200 s / 12-minute variants, and procedural beat beds (spec §6.1).

Everything here is deterministic and cached under `CACHE_ROOT/<key>/` where
`key` hashes the script, the synthesis parameters and `MEDIA_VERSION`, so the
first run pays ~15 s of ffmpeg and every later run pays a manifest read. The
cache lives in the user cache dir (next to the app's own beats/thumbs caches),
never in the repo and never in WORKDIR — a benchmark session is disposable,
its media is not.

Ground truth carried alongside each file:

  * `scene_cuts` — the exact times the picture hard-cuts (six scenes, five
    cuts). `ingest.scenes.detect_shots` must find all five ±0.1 s
    (test_media_synthesis pins it), which is what makes the `transitions`
    and `shorts` cases measurable.
  * each `Bed` — its synthesis grid (kick onsets, exact) AND librosa's own
    `detect_beats` output on the finished file. Case 10 scores splits
    against the DETECTED beats, because that is what `auto_cut_to_beats`
    and the beat_sync recipe can see; the grid is kept so a detector
    regression is distinguishable from an editor regression.

WHY the beds are synthesized here even though P's `scripts/gen_music_beds.py`
owns the shipped presets: the benchmark must run before/without that script,
and it needs a SESSION-UPLOADED bed (`bench_bed_100bpm.wav`) at a tempo the
presets do not ship. `ensure_preset_beds()` runs P's script when it exists
and otherwise writes the same four mood beds (same spec: 48 kHz, 180 s,
−18 LUFS, exact kick grid, JSON sidecar) — only the files that are missing,
never overwriting P's.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from video_ai_editor import config as _config
from video_ai_editor import platformutil as _pu
from video_ai_editor.ingest.beats import detect_beats
from video_ai_editor.ingest.probe import probe

from .narration import (SCRIPT_EN, GAP_S, PAUSE_S, PART_PAD_S, HindiVoiceUnavailable, Narration,
                        load_narration, synthesize_narration)

#: Bump whenever synthesis changes shape; it is part of the cache key.
MEDIA_VERSION = 5

CACHE_ROOT = Path(os.environ.get("VAI_BENCH_CACHE") or
                  _pu.user_cache_dir("Video AI Editor") / "bench")

FPS = 30
#: Where the six scenes cut, as fractions of the narration length — turned
#: into frame-exact seconds once the narration duration is known.
SCENE_CUT_FRACTIONS = (0.13, 0.28, 0.45, 0.60, 0.78)
SCENE_COLORS = ("0x1f6feb", "0xd29922", "0x8957e5", "0x2ea043", "0xda3633", "0x0e7490")

BED_SR = 48_000
BED_SECONDS = 180.0
BED_LUFS = -18.0
BED_GRID_OFFSET = 0.05
BENCH_BED_BPM = 100.0
#: The four mood beds P's generator ships (spec §2.8); same spec here.
PRESET_BEDS: dict[str, tuple[str, float]] = {
    "chill_90bpm": ("chill", 90.0), "upbeat_120bpm": ("upbeat", 120.0),
    "cinematic_70bpm": ("cinematic", 70.0), "lofi_85bpm": ("lofi", 85.0),
}
LOOP_FIXTURE_SECONDS = 200.0
LONG_FIXTURE_SECONDS = 12 * 60.0
BROLL_SECONDS = 20.0


class MediaBuildError(RuntimeError):
    """ffmpeg refused a synthesis step; the message carries its stderr tail."""


@dataclass(frozen=True)
class Bed:
    path: str
    bpm: float
    seconds: float
    grid_offset: float
    grid: tuple[float, ...]        # kick onsets the synthesizer placed
    detected: tuple[float, ...]    # librosa's beats on the finished file
    lufs: float                    # integrated loudness measured after normalisation

    @property
    def period(self) -> float:
        return 60.0 / self.bpm


@dataclass(frozen=True)
class MediaSet:
    root: str
    narration: Narration
    video_16x9: str
    video_9x16: str
    broll: str
    loop_fixture: str              # 200 s, for add_music(loop=true) coverage
    scene_cuts: tuple[float, ...]
    bench_bed: Bed

    @property
    def duration(self) -> float:
        return self.narration.duration

    def to_json(self) -> str:
        data = asdict(self)
        data["narration"] = json.loads(self.narration.to_json())
        return json.dumps(data, indent=1, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "MediaSet":
        data = json.loads(text)
        data["narration"] = Narration.from_json(json.dumps(data["narration"]))
        data["bench_bed"] = Bed(**{**data["bench_bed"],
                                   "grid": tuple(data["bench_bed"]["grid"]),
                                   "detected": tuple(data["bench_bed"]["detected"])})
        data["scene_cuts"] = tuple(data["scene_cuts"])
        return cls(**data)

    # --- lazy, slow-tier extras ----------------------------------------------
    def long_fixture(self) -> Path:
        """12-minute 360p clip with the narration looped underneath (case 23's
        run-time gate). Built on first request; ~15 s of ffmpeg."""
        dst = Path(self.root) / "long_12min.mp4"
        if not dst.exists():
            render_looped_video(Path(self.narration.wav), dst, seconds=LONG_FIXTURE_SECONDS)
        return dst

    def hindi(self) -> tuple[Path, Narration]:
        """Hindi-narrated 16:9 variant (cases 2 / 20-hi). Raises
        `HindiVoiceUnavailable` when no local Hindi voice exists."""
        root = Path(self.root)
        nar = load_narration(root, lang="hi") or synthesize_narration(root, lang="hi")
        dst = root / "scene_hi_16x9.mp4"
        if not dst.exists():
            render_scene_video(Path(nar.wav), dst, size=(1920, 1080), cuts=scene_cuts_for(nar.duration))
        return dst, nar


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------

def _run(args: list[str], what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **_pu.SUBPROCESS_FLAGS)
    if proc.returncode != 0:
        raise MediaBuildError(f"{what}: ffmpeg exit {proc.returncode}: {proc.stderr[-600:]}")
    return proc


def _frame(t: float) -> float:
    return round(round(t * FPS) / FPS, 4)


def scene_cuts_for(duration: float) -> tuple[float, ...]:
    return tuple(_frame(duration * f) for f in SCENE_CUT_FRACTIONS)


def _scene_filter(duration: float, cuts: tuple[float, ...]) -> str:
    """A `testsrc2` picture with six visibly different scenes. At every cut
    the picture toggles `negate` AND a solid colour block over the top 40 %
    switches colour, so ffmpeg's `scene` score at each cut is far above the
    0.3 `detect_shots` threshold (measured: a colour block alone scored
    0.06–0.26 — under it — because the metric is a whole-frame mean
    difference). `testsrc2`'s own motion keeps every shot non-static, so
    hook punch-ins and reframes change real pixels. No `drawtext`: the
    Homebrew ffmpeg on the build Mac lacks it, and a label adds nothing a
    measurement reads."""
    bounds = (0.0, *cuts, duration + 1.0)
    parts: list[str] = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        enable = f"enable='between(t,{a:.4f},{b:.4f})'"
        if i % 2:
            parts.append(f"negate={enable}")
        parts.append(f"drawbox=x=0:y=0:w=iw:h=ih*0.4:color={SCENE_COLORS[i]}@1:t=fill:{enable}")
    return ",".join(parts)


def render_scene_video(narration_wav: Path, dst: Path, *, size: tuple[int, int],
                       cuts: tuple[float, ...]) -> Path:
    """Six-scene video at `size`, hard cuts at `cuts`, the narration as AAC."""
    w, h = size
    duration = probe(narration_wav).duration
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([_pu.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={FPS}:d={duration:.3f}",
          "-i", str(narration_wav),
          "-filter_complex", f"[0:v]{_scene_filter(duration, cuts)}[v]",
          "-map", "[v]", "-map", "1:a",
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
          "-shortest", "-movflags", "+faststart", str(dst)], f"scene video {dst.name}")
    return dst


# --------------------------------------------------------------------------
# bar-coded angles with click tracks (EB1-A; the brain fixtures)
# --------------------------------------------------------------------------

#: The bar-code layout is `tests/frame_map_golden_lib.py`'s read geometry:
#: 16 bands of 20 px on a 320×180 base, code bits read from columns
#: [b·20+5, b·20+15) of rows 24-84 (top) and 100-172 (bottom). Only those
#: cells carry the code here; everything else stays dark (Y 16) so a
#: one-frame FLASH (everything outside the cells → Y 235) reads as a bright
#: frame to `timing_fixtures.flash_times` (whole-frame mean > 128 at 32×18)
#: while the code stays readable on that frame, and no non-flash frame can
#: cross 128 (≤ 13 set bits × 10/20 columns × 140/180 rows → mean ≤ 101).
BAR_W, BAR_H = 320, 180
BAR_BANDS, BAR_BAND_W = 16, 20
BAR_TOP_ROWS, BAR_BOT_ROWS = (22, 86), (98, 174)
BAR_STRIP_ROWS = (88, 96)
BAR_FRAME_BITS = 12
#: The click: 4 ms of 1 kHz at 0.95 full scale (`timing_fixtures.make_clap`'s
#: shape, louder). Speech in these fixtures peaks ≤ 0.5, so
#: `click_times(thresh=0.7)` sees only clicks.
CLICK_AMP, CLICK_S, CLICK_HZ = 0.95, 0.004, 1000.0
ECHO_DB = -6.0


def click_pcm(sr: int) -> np.ndarray:
    t = np.arange(int(round(CLICK_S * sr))) / sr
    return (CLICK_AMP * np.sin(2 * np.pi * CLICK_HZ * t)).astype(np.float32)


def add_clicks(pcm: np.ndarray, sr: int, times) -> np.ndarray:
    """A copy of `pcm` with a click starting at every time (seconds); a click
    past the end is dropped, one that straddles the end is truncated."""
    out = np.array(pcm, dtype=np.float32, copy=True)
    burst = click_pcm(sr)
    for t in times:
        i = int(round(t * sr))
        if 0 <= i < len(out):
            n = min(len(burst), len(out) - i)
            out[i:i + n] += burst[:n]
    return out


def room_mic(pcm: np.ndarray, sr: int, *, gain_db: float, delay_ms: float, echo_db: float = ECHO_DB,
             noise_dbfs: float | None = None, seed: int = 0) -> np.ndarray:
    """A camera microphone hearing `pcm`: the direct path at `gain_db` plus a
    reflection `delay_ms` later at `echo_db` relative, plus white room noise
    at `noise_dbfs` RMS (deterministic by `seed`). The direct path keeps the
    file's offset unambiguous — the reflection is a second, quieter copy, so
    a cross-correlation peaks at the true offset and `click_times` (on the
    undelayed clicks added afterwards) agrees with it."""
    x = np.asarray(pcm, dtype=np.float32)
    out = x * float(10 ** (gain_db / 20.0))
    d = int(round(delay_ms * sr / 1000.0))
    if d > 0:
        out[d:] += out[:-d] * float(10 ** (echo_db / 20.0))
    if noise_dbfs is not None:
        rng = np.random.default_rng(seed)
        out = out + rng.standard_normal(len(out)).astype(np.float32) * float(10 ** (noise_dbfs / 20.0))
    return out.astype(np.float32)


def _yuv_frame(base: np.ndarray, code: int, strip_color: tuple[int, int, int] | None, flash: bool) -> bytes:
    y = base.copy()
    if flash:
        y[:, :] = 235
    for b in range(BAR_BANDS):
        v = 235 if (code >> b) & 1 else 16
        x0, x1 = b * BAR_BAND_W + 5, b * BAR_BAND_W + 15
        y[BAR_TOP_ROWS[0]:BAR_TOP_ROWS[1], x0:x1] = v
        y[BAR_BOT_ROWS[0]:BAR_BOT_ROWS[1], x0:x1] = v
    u = np.full((BAR_H // 2, BAR_W // 2), 128, np.uint8)
    vv = np.full((BAR_H // 2, BAR_W // 2), 128, np.uint8)
    if strip_color is not None and not flash:
        r0, r1 = BAR_STRIP_ROWS
        y[r0:r1, :] = strip_color[0]
        u[r0 // 2:r1 // 2, :] = strip_color[1]
        vv[r0 // 2:r1 // 2, :] = strip_color[2]
    return y.tobytes() + u.tobytes() + vv.tobytes()


def _strip_at(strip: list[tuple[float, float, tuple[int, int, int]]], t: float):
    for t0, t1, color in strip:
        if t0 <= t < t1:
            return color
    return None


def _write_mono_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    import wave
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())


def _angle_audio(narration_wav: Path, *, offset_s: float, mic_gain_db: float, mic_delay_ms: float,
                 noise_dbfs: float | None, seed: int, clicks: list[float], click: bool) -> tuple[np.ndarray, int]:
    """The file's audio on its OWN clock: `file_t = ref_t + offset_s`."""
    from .narration import _read_wav
    x, sr = _read_wav(narration_wav)
    lead = int(round(offset_s * sr))
    if lead >= 0:
        x = np.concatenate([np.zeros(lead, np.float32), x])
    else:
        x = x[-lead:]
    if mic_gain_db or mic_delay_ms or noise_dbfs is not None:
        x = room_mic(x, sr, gain_db=mic_gain_db, delay_ms=mic_delay_ms, noise_dbfs=noise_dbfs, seed=seed)
    if click:
        x = add_clicks(x, sr, [t + offset_s for t in clicks])
    return x, sr


def _angle_cmd(tmp_wav: Path, dst: Path, size: tuple[int, int], fps: int) -> list[str]:
    w, h = size
    vf = "format=yuv420p" if (w, h) == (BAR_W, BAR_H) else f"scale={w}:{h}:flags=neighbor,format=yuv420p"
    return [_pu.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{BAR_W}x{BAR_H}", "-r", str(fps), "-i", "-",
            "-i", str(tmp_wav), "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", str(2 * fps), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            "-fflags", "+bitexact", "-flags", "+bitexact", "-map_metadata", "-1",
            "-shortest", "-movflags", "+faststart", str(dst)]


def render_barcode_angle(narration_wav: Path, dst: Path, *, sid: int, size: tuple[int, int], offset_s: float,
                         mic_gain_db: float = 0.0, mic_delay_ms: float = 0.0, click: bool = True,
                         fps: int = FPS, click_times: tuple[float, ...] | list[float] = (),
                         strip: list[tuple[float, float, tuple[int, int, int]]] | None = None,
                         noise_dbfs: float | None = None, seed: int = 0) -> Path:
    """One camera angle of a bar-coded fixture (EB1-A).

    Picture: frame n carries `frame_map_golden_lib.code_of(sid, n)` in the
    read cells (`BAR_*` above), a coloured strip (rows 88-96 of the base)
    whose colour follows `strip` = [(ref_t0, ref_t1, (Y, U, V)), …] (a pulse
    while a speaker talks, or a scene colour), and a one-frame flash at every
    `click_times` entry. Sound: `narration_wav` on the reference clock, passed
    through `room_mic` when a mic gain/delay/noise is given, then the clicks
    (undelayed, un-attenuated) at the same instants. Both are shifted by
    `offset_s` (`file_t = ref_t + offset_s`: a positive offset prepends lead,
    a negative one drops the head), so the flash and the click of one event
    sit at the same file time — the raw angle measures `av_offsets_ms ≈ 0`
    and the click tracks of two angles differ by exactly their offsets.
    Every event time must sit on the `fps` grid (the caller's job); the
    picture is generated at 320×180 and scaled by nearest neighbour."""
    clicks = [float(t) for t in click_times]
    pcm, sr = _angle_audio(narration_wav, offset_s=offset_s, mic_gain_db=mic_gain_db,
                           mic_delay_ms=mic_delay_ms, noise_dbfs=noise_dbfs, seed=seed,
                           clicks=clicks, click=click)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_wav = dst.with_suffix(".angle_audio.wav")
    _write_mono_wav(tmp_wav, pcm, sr)
    n_frames = int(math.ceil(len(pcm) / sr * fps - 1e-9))
    flash_frames = {int(round((t + offset_s) * fps)) for t in clicks}
    strip_file = [(t0 + offset_s, t1 + offset_s, c) for t0, t1, c in (strip or [])]
    base = np.full((BAR_H, BAR_W), 16, np.uint8)
    proc = subprocess.Popen(_angle_cmd(tmp_wav, dst, size, fps), stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                            **_pu.SUBPROCESS_FLAGS)
    try:
        assert proc.stdin is not None
        for n in range(n_frames):
            code = (sid << BAR_FRAME_BITS) | (n & ((1 << BAR_FRAME_BITS) - 1))
            proc.stdin.write(_yuv_frame(base, code, _strip_at(strip_file, n / fps), n in flash_frames))
        proc.stdin.close()
    finally:
        _err = proc.stderr.read() if proc.stderr else b""
        rc = proc.wait()
        tmp_wav.unlink(missing_ok=True)
    if rc != 0:
        raise MediaBuildError(f"bar-code angle {dst.name}: ffmpeg exit {rc}: {_err[-600:].decode('utf-8', 'replace')}")
    return dst


def render_broll(dst: Path, *, seconds: float = BROLL_SECONDS, size: tuple[int, int] = (1280, 720)) -> Path:
    """20 s of moving picture with a quiet tone — no speech, so it is b-roll."""
    w, h = size
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([_pu.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={FPS}:d={seconds:.3f}",
          "-f", "lavfi", "-i", f"sine=f=220:d={seconds:.3f}",
          "-af", "volume=-28dB", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2",
          "-shortest", str(dst)], f"b-roll {dst.name}")
    return dst


def render_looped_video(narration_wav: Path, dst: Path, *, seconds: float,
                        size: tuple[int, int] = (640, 360)) -> Path:
    """`seconds` of 360p picture with the narration looped underneath — the
    200 s loop fixture and the 12-minute run-time-gate fixture."""
    w, h = size
    dst.parent.mkdir(parents=True, exist_ok=True)
    cuts = scene_cuts_for(seconds)
    _run([_pu.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={FPS}:d={seconds:.3f}",
          "-stream_loop", "-1", "-i", str(narration_wav),
          "-filter_complex", f"[0:v]{_scene_filter(seconds, cuts)}[v]",
          "-map", "[v]", "-map", "1:a", "-t", f"{seconds:.3f}",
          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "96k",
          str(dst)], f"looped video {dst.name}")
    return dst


# --------------------------------------------------------------------------
# beat beds
# --------------------------------------------------------------------------

def _kick(sr: int, rng: np.random.Generator) -> np.ndarray:
    """Pitch-sweep kick with a 3 ms noise click on the attack. The click is
    what librosa's onset envelope locks onto: without it (and with louder
    hats) `detect_beats` reported every beat ~0.33 s late — on the off-beat
    hats — for a bed whose kicks sat on an exact grid."""
    t = np.arange(int(0.28 * sr)) / sr
    freq = 45.0 + 110.0 * np.exp(-t / 0.045)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    body = np.sin(phase) * np.exp(-t / 0.09)
    click_n = int(0.003 * sr)
    click = np.zeros_like(body)
    click[:click_n] = rng.standard_normal(click_n) * np.linspace(1.0, 0.0, click_n) * 0.6
    return (body + click).astype(np.float32)


def _hat(sr: int, rng: np.random.Generator) -> np.ndarray:
    n = int(0.025 * sr)
    noise = rng.standard_normal(n).astype(np.float32)
    # crude high-pass: first difference
    noise = np.diff(noise, prepend=0.0)
    return noise * np.exp(-np.arange(n) / (0.006 * sr)) * 0.06


def synth_bed_pcm(*, bpm: float, seconds: float, sr: int = BED_SR,
                  grid_offset: float = BED_GRID_OFFSET, seed: int = 7) -> tuple[np.ndarray, tuple[float, ...]]:
    """Mono float32 bed: kick on every beat of an exact grid, hats on the
    off-beats, a pulsing bass and a quiet two-note pad. Returns (pcm, grid)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    out = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    kick, hat = _kick(sr, rng), _hat(sr, rng)
    grid: list[float] = []
    t_beat = grid_offset
    while t_beat < seconds:
        i = int(round(t_beat * sr))
        out[i:i + len(kick)] += kick[: max(0, n - i)]
        grid.append(round(t_beat, 6))
        j = int(round((t_beat + period / 2) * sr))
        if j < n:
            out[j:j + len(hat)] += hat[: max(0, n - j)]
        t_beat += period
    t = np.arange(n) / sr
    pulse = 0.5 + 0.5 * np.cos(2 * np.pi * ((t - grid_offset) / period))
    out += 0.18 * np.sin(2 * np.pi * 55.0 * t).astype(np.float32) * pulse.astype(np.float32)
    trem = 0.75 + 0.25 * np.sin(2 * np.pi * 0.25 * t)
    out += (0.05 * (np.sin(2 * np.pi * 220.0 * t) + np.sin(2 * np.pi * 330.0 * t)) * trem).astype(np.float32)
    return out, tuple(grid)


def _write_pcm(path: Path, pcm: np.ndarray, sr: int) -> None:
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    stereo = np.stack([pcm, pcm], axis=1)
    sf.write(str(path), np.clip(stereo, -1.0, 1.0), sr, subtype="PCM_16")


_EBUR_I = re.compile(r"\bI:\s*(-?[\d.]+)\s*LUFS")


def integrated_lufs(path: Path) -> float:
    """Integrated loudness via ffmpeg `ebur128` (the same meter the export
    path's `loudnorm` targets)."""
    proc = subprocess.run([_pu.FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
                           "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_pu.SUBPROCESS_FLAGS)
    hits = _EBUR_I.findall(proc.stderr)
    if not hits:
        raise MediaBuildError(f"ebur128 gave no integrated loudness for {path.name}: {proc.stderr[-300:]}")
    return float(hits[-1])


def synth_bed(dst: Path, *, bpm: float, seconds: float = BED_SECONDS, lufs: float = BED_LUFS,
              sr: int = BED_SR, grid_offset: float = BED_GRID_OFFSET) -> Bed:
    """Write a bed normalised to `lufs`, then store librosa's detected beats."""
    pcm, grid = synth_bed_pcm(bpm=bpm, seconds=seconds, sr=sr, grid_offset=grid_offset)
    _write_pcm(dst, pcm, sr)
    measured = integrated_lufs(dst)
    gain = 10 ** ((lufs - measured) / 20.0)
    _write_pcm(dst, pcm * gain, sr)
    final = integrated_lufs(dst)
    detected = tuple(round(float(b), 4) for b in detect_beats(dst))
    return Bed(path=str(dst), bpm=bpm, seconds=seconds, grid_offset=grid_offset,
               grid=grid, detected=detected, lufs=final)


def bed_sidecar(bed: Bed, mood: str) -> dict:
    """The `.json` beside a preset bed — the shape P's generator writes."""
    return {"bpm": bed.bpm, "mood": mood, "source": "procedural",
            "beat_grid_offset": bed.grid_offset, "seconds": bed.seconds, "lufs": bed.lufs}


def ensure_preset_beds(presets_dir: Path | None = None, *, use_script: bool = True,
                       seconds: float = BED_SECONDS) -> list[Path]:
    """Make sure `presets/music/<mood>_<bpm>bpm.wav` exist for the four moods.
    P's `scripts/gen_music_beds.py` is authoritative when present (and
    `use_script`); it is always pointed at THIS `presets_dir` with `--out`, so
    a test running against a temp dir never writes into the repo. Otherwise
    the same spec is synthesized here (`seconds` lets a test keep it short)
    for the files that are missing. Returns the paths that exist afterwards."""
    presets_dir = presets_dir or _config.PRESETS_DIR
    music_dir = presets_dir / "music"
    wanted = {name: music_dir / f"{name}.wav" for name in PRESET_BEDS}
    if all(p.exists() for p in wanted.values()):
        return list(wanted.values())
    script = _config.PROJECT_ROOT / "scripts" / "gen_music_beds.py"
    if use_script and script.exists():
        subprocess.run([sys.executable, str(script), "--out", str(music_dir)], check=True,
                       capture_output=True, cwd=str(_config.PROJECT_ROOT), **_pu.SUBPROCESS_FLAGS)
    for name, path in wanted.items():
        if path.exists():
            continue
        mood, bpm = PRESET_BEDS[name]
        bed = synth_bed(path, bpm=bpm, seconds=seconds)
        path.with_suffix(".json").write_text(json.dumps(bed_sidecar(bed, mood), indent=1),
                                             encoding="utf-8")
    return [p for p in wanted.values() if p.exists()]


# --------------------------------------------------------------------------
# the media set
# --------------------------------------------------------------------------

def media_key() -> str:
    material = json.dumps({"v": MEDIA_VERSION, "script": SCRIPT_EN, "gap": GAP_S, "pause": PAUSE_S,
                           "pad": PART_PAD_S, "fps": FPS, "cuts": SCENE_CUT_FRACTIONS,
                           "bed": [BENCH_BED_BPM, BED_SECONDS, BED_LUFS, BED_GRID_OFFSET]},
                          sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def build_media_set(*, root: Path | None = None, force: bool = False) -> MediaSet:
    """The English media set, built once per `media_key()` and read from the
    manifest afterwards. Slow-tier extras (`long_fixture`, `hindi`) are lazy."""
    base = (root or CACHE_ROOT) / media_key()
    manifest = base / "manifest.json"
    if manifest.exists() and not force:
        ms = MediaSet.from_json(manifest.read_text(encoding="utf-8"))
        if all(Path(p).exists() for p in (ms.video_16x9, ms.video_9x16, ms.broll,
                                          ms.loop_fixture, ms.bench_bed.path, ms.narration.wav)):
            return ms
    base.mkdir(parents=True, exist_ok=True)
    started = time.time()
    narration = synthesize_narration(base, lang="en")
    cuts = scene_cuts_for(narration.duration)
    wav = Path(narration.wav)
    v16 = render_scene_video(wav, base / "scene_16x9.mp4", size=(1920, 1080), cuts=cuts)
    v9 = render_scene_video(wav, base / "scene_9x16.mp4", size=(1080, 1920), cuts=cuts)
    broll = render_broll(base / "broll_20s.mp4")
    loop = render_looped_video(wav, base / "loop_200s.mp4", seconds=LOOP_FIXTURE_SECONDS)
    bed = synth_bed(base / "bench_bed_100bpm.wav", bpm=BENCH_BED_BPM)
    ms = MediaSet(root=str(base), narration=narration, video_16x9=str(v16), video_9x16=str(v9),
                  broll=str(broll), loop_fixture=str(loop), scene_cuts=cuts, bench_bed=bed)
    manifest.write_text(ms.to_json(), encoding="utf-8")
    (base / "build_seconds.txt").write_text(f"{time.time() - started:.1f}\n", encoding="utf-8")
    return ms


__all__ = ["MEDIA_VERSION", "CACHE_ROOT", "FPS", "SCENE_CUT_FRACTIONS", "BED_SR", "BED_SECONDS",
           "BED_LUFS", "BED_GRID_OFFSET", "BENCH_BED_BPM", "PRESET_BEDS", "LOOP_FIXTURE_SECONDS",
           "LONG_FIXTURE_SECONDS", "BROLL_SECONDS", "MediaBuildError", "HindiVoiceUnavailable",
           "Bed", "MediaSet", "scene_cuts_for", "render_scene_video", "render_broll",
           "BAR_W", "BAR_H", "BAR_FRAME_BITS", "CLICK_AMP", "click_pcm", "add_clicks", "room_mic",
           "render_barcode_angle",
           "render_looped_video", "synth_bed_pcm", "synth_bed", "integrated_lufs", "bed_sidecar",
           "ensure_preset_beds", "media_key", "build_media_set"]
