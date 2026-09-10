"""Every non-v1 lane follows the RENDER clock — proven on real renders.

THE DEFECT these lock out: v1 transitions `xfade` two adjacent clips, so clip
B plays EARLIER than its layout `start` by the overlap accumulated at and
before that seam, while text, captions, stickers, the PiP picture, the PiP
audio, music and voiceover were all positioned in raw LAYOUT time. On a
71.86 s timeline with twelve Zoom-Ins every overlay lane drifted up to 2.4 s
late against the picture and the speech. `render/clock.py` is the fix; these
tests are what "fixed" means, measured from the pixels and the samples of
the file the export and the preview produce — never from filter strings.

THE FIXTURE: red 2 s + blue 2 s back to back, ONE 0.5 s fade at the 2.0 s
seam → a 3.5 s render. A probe authored at LAYOUT 3.0–3.5 (on clip B) must
land at RENDER 2.5–3.0 with the transition and at 3.0–3.5 without; a probe
on clip A's tail (layout 1.6–1.9) must not move; a probe covering exactly
A's consumed tail (layout 1.5–2.0) is dropped (the desktop draws it dropped,
`timelineLayout.renderWindow(...).dropped`). Each probe is first proven
visible/audible in the NO-transition render, so a probe that never renders
cannot pass vacuously.

THE PICTURE IS THE REFERENCE, not only the clock's arithmetic: clip B's own
source turns navy for its 1.0–1.5 s (= layout 3.0–3.5, the probe window) and
clip A's turns yellow for 1.6–1.9 (= the pre-seam window), so each overlay
probe is compared frame for frame against where v1 itself put that instant.
Audio probes are compared against v1's own 440 Hz "speech" burst, which sits
under the same layout window in clip B.

WHY ±1 FRAME: `overlay=enable='between(t,a,b)'` evaluates `t` as pts ×
timebase in floating point, so a frame sitting EXACTLY on `a` or `b` lands
on either side depending on the path's timebase (1/15360 through the chunk
concat, µs through the xfade graph) — measured: the same sticker opens on
its first frame in one path and on the next in the other, with or without a
transition. That is a boundary-rounding property of the gate, not a clock
error, and the picture markers show it identically.

WHY THE PICTURE PROBES GO THROUGH `render_export`: `render_preview` never
bakes text, captions, stickers or the PiP PICTURE — the browser draws them
live (TextLayer / StickerLayer / pipDraw) — which is why an earlier probe
"showed nothing in render_preview even without a transition". Both entry
points share `_render` and the same lane builders; the audio lanes are
additionally checked through `render_preview`, including its audio-only
remux fast path, which re-assembles v1's audio by itself.

WHY THE PIP IS EXPLICITLY CENTRED: `Transform()` defaults to x=0, y=0 and
pip.py takes those as the element's CENTRE in canvas pixels, so a default
PiP sits three-quarters off-canvas — the other reason the earlier probe was
invisible. `scale=1/0.35` with `fit="cover"` makes it fill the frame.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.edl.schema import (EDL, Canvas, CaptionsConfig, Clip, MusicDuck, Sticker,
                                        TextClip, TextStyle, Track, Transform, Transition)
from video_ai_editor.render import clock, render_export, render_preview
from video_ai_editor.render import compositor as C

W, H, FPS = 320, 180, 30
SEAM, FADE = 2.0, 0.5
PROBE = (3.0, 3.5)          # layout window on clip B (= clip B's source 1.0–1.5)
PRE = (1.6, 1.9)            # layout window on clip A, before the seam
TAIL = (SEAM - FADE, SEAM)  # clip A's consumed tail: exactly what the fade eats
FRAME = 1.0 / FPS
#: One frame plus a hair — see "WHY ±1 FRAME" above.
FRAME_TOL = FRAME + 0.005
#: Audio is measured in 50 ms blocks; a block plus a hair.
HOP_S = 0.05
AUDIO_TOL = HOP_S + 0.01
TONE_HZ = 1000
SPEECH_HZ = 440
#: A tone block counts as ON above this; the probes sit near −16 dBFS in-band.
TONE_ON_DB = -30.0


# ------------------------------------------------------------------ fixtures

def _lavfi(dst: Path, *, video: str, audio: str, seconds: float, marker: str = "") -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"{video}:s={W}x{H}:d={seconds}:r={FPS}",
            "-f", "lavfi", "-i", audio]
    if marker:
        args += ["-vf", marker]
    subprocess.run([*args, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)],
                   check=True, capture_output=True)


def _marker(color: str, lo: float, hi: float) -> str:
    return f"drawbox=x=0:y=0:w=iw:h=ih:color={color}:t=fill:enable='between(t,{lo},{hi})'"


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict[str, Path]:
    """Clip A: red, silent, YELLOW for its 1.6–1.9 s. Clip B: blue, NAVY for its
    1.0–1.5 s with a 440 Hz 'speech' burst there (layout 3.0–3.5, the probe
    window), silent otherwise. A PiP source that is lime with a 1 kHz tone
    throughout; a 1 kHz WAV for the music/vo lanes; a canvas-sized magenta
    PNG for the sticker lane."""
    d = tmp_path_factory.mktemp("fx")
    _lavfi(d / "red.mp4", video="color=c=red", audio="anullsrc=r=48000:cl=stereo:d=2", seconds=2,
           marker=_marker("yellow", *PRE))
    _lavfi(d / "blue.mp4", video="color=c=blue", seconds=2,
           audio=f"aevalsrc=0.5*sin(2*PI*{SPEECH_HZ}*t)*between(t\\,1.0\\,1.5):s=48000:d=2",
           marker=_marker("navy", PROBE[0] - SEAM, PROBE[1] - SEAM))
    _lavfi(d / "green.mp4", video="color=c=lime", seconds=2,
           audio=f"aevalsrc=0.3*sin(2*PI*{TONE_HZ}*t):s=48000:d=2")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc=0.3*sin(2*PI*{TONE_HZ}*t):s=48000:d=2",
                    "-c:a", "pcm_s16le", str(d / "tone.wav")], check=True, capture_output=True)
    Image.new("RGBA", (W, H), (255, 0, 255, 255)).save(d / "magenta.png")
    return {p.stem: p for p in d.iterdir()}


def _base(fx: dict[str, Path], *, fade: bool) -> EDL:
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        Track(id="v1", type="video", z=0, clips=[
            Clip(id="a", src=str(fx["red"]), in_=0, out=2, start=0),
            Clip(id="b", src=str(fx["blue"]), in_=0, out=2, start=SEAM),
        ], transitions=[Transition(at=SEAM, type="fade", duration=FADE)] if fade else []),
        Track(id="v2", type="video", z=1),
        Track(id="music", type="music", z=0),
        Track(id="vo", type="vo", z=0),
        Track(id="tx_hook", type="text", z=10),
        Track(id="stickers", type="sticker", z=12),
        Track(id="captions", type="captions", z=13, config=CaptionsConfig()),
    ])
    edl.recompute_duration()
    return edl


_GREEN_TEXT = TextStyle(color="#00FF00", stroke_w=0, shadow=None, size=96)


def _plant(edl: EDL, kind: str, fx: dict[str, Path], window: tuple[float, float]) -> EDL:
    """Author ONE probe at layout `window` on the lane `kind` names."""
    s, e = window
    if kind == "sticker":
        edl.get_track("stickers").clips.append(Sticker(
            id="st", src=str(fx["magenta"]), start=s, end=e,
            # 22 % of the long edge × scale = the whole canvas.
            transform=Transform(x=W / 2, y=H / 2, scale=1 / 0.22)))
    elif kind in ("text", "anim_text"):
        edl.get_track("tx_hook").clips.append(TextClip(
            id="t", text="WWWW", start=s, end=e, role="hook", style=_GREEN_TEXT,
            transform=Transform(x=W / 2, y=H / 2),
            anim_in="pop" if kind == "anim_text" else None))
    elif kind == "caption":
        edl.get_track("captions").clips.append(TextClip(
            id="c", text="WWWW", start=s, end=e, role="caption",
            style=TextStyle(color="#00FF00", stroke_w=0, shadow=None)))
    elif kind in ("pip_picture", "pip_audio"):
        edl.get_track("v2").clips.append(Clip(
            id="p", src=str(fx["green"]), in_=0, out=e - s, start=s, fit="cover",
            transform=Transform(x=W / 2, y=H / 2, scale=1 / 0.35)))
    elif kind in ("music", "vo"):
        edl.get_track(kind).clips.append(Clip(id=kind, src=str(fx["tone"]), in_=0, out=e - s, start=s))
    else:
        raise ValueError(kind)
    edl.recompute_duration()
    return edl


# -------------------------------------------------------------- measurement

_PTS = re.compile(r"pts_time:\s*([\d.]+)")
_RMS = re.compile(r"RMS_level=(-?[\d.]+|-inf)")


def _frames(path: Path) -> list[tuple[float, tuple[int, int, int]]]:
    """(pts, mean RGB) for EVERY frame of the file, one ffmpeg call: no seek
    arithmetic (an accurate `-ss` can start one frame off depending on the
    GOP), `scale=1:1:flags=area` (a true box average) as rgb24 rawvideo, and
    `showinfo` for the pts each triple belongs to."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                           "-vf", "scale=1:1:flags=area,format=rgb24,showinfo",
                           "-f", "rawvideo", "-"], capture_output=True)
    assert proc.returncode == 0, proc.stderr[-400:]
    pts = [float(x) for x in _PTS.findall(proc.stderr.decode("utf-8", "replace"))]
    d = proc.stdout
    rgb = [(d[3 * i], d[3 * i + 1], d[3 * i + 2]) for i in range(len(d) // 3)]
    assert len(pts) == len(rgb), (len(pts), len(rgb))
    return list(zip(pts, rgb))


def _seen_window(path: Path, is_on) -> tuple[float, float] | None:
    """`[first on-frame, one past the last on-frame)` over the whole file."""
    on = [t for t, rgb in _frames(path) if is_on(rgb)]
    return (on[0], on[-1] + FRAME) if on else None


def _is_magenta(rgb) -> bool:
    r, g, b = rgb
    return r >= 170 and b >= 170 and g <= 110


def _is_green_text(rgb) -> bool:
    # Pure-green glyphs (no stroke, no shadow) over the blue/navy clip: any
    # green in the frame mean is text; the clip itself averages g=0.
    r, g, b = rgb
    return g >= 20 and r <= 40


def _is_lime_frame(rgb) -> bool:
    r, g, b = rgb
    return g >= 170 and r <= 80 and b <= 80


def _is_navy_marker(rgb) -> bool:
    # Clip B's marker: blue dimmed to navy. The red→blue dissolve never has
    # r ≤ 30 while b is this low.
    r, g, b = rgb
    return r <= 30 and g <= 30 and 90 <= b <= 170


def _is_yellow_marker(rgb) -> bool:
    # Clip A's marker, seen fading INTO blue inside the crossfade window:
    # green stays ≥ 40 through progress 0.8 (layout 1.9), and nothing else
    # in the fixture puts green and red in the same frame.
    r, g, b = rgb
    return g >= 40 and r >= 40


def _band_levels(path: Path, freq_hz: float) -> list[tuple[float, float]]:
    """(block start, RMS dBFS) per `HOP_S` block of the audio inside a 40 Hz
    band around `freq_hz`, over the whole file (no `-ss`, so times stay
    absolute)."""
    n = int(round(48000 * HOP_S))
    af = (f"aresample=48000,bandpass=f={freq_hz:g}:width_type=h:w=40,"
          f"asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
          "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
                           "-af", af, "-f", "null", "-"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-400:]
    out: list[tuple[float, float]] = []
    t: float | None = None
    for line in proc.stdout.splitlines():
        m = _PTS.search(line)
        if m:
            t = float(m.group(1))
            continue
        m = _RMS.search(line)
        if m and t is not None:
            out.append((t, float(m.group(1))))
            t = None
    return out


def _tone_window(levels: list[tuple[float, float]]) -> tuple[float, float] | None:
    on = [t for t, lvl in levels if lvl > TONE_ON_DB]
    return (min(on), max(on) + HOP_S) if on else None


def _mean_level(levels: list[tuple[float, float]], lo: float, hi: float) -> float:
    rows = [lvl for t, lvl in levels if lo <= t < hi]
    assert rows, f"no audio blocks in [{lo}, {hi})"
    return sum(rows) / len(rows)


def _file_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def _close(got: tuple[float, float] | None, exp: tuple[float, float], tol: float) -> bool:
    return got is not None and abs(got[0] - exp[0]) <= tol and abs(got[1] - exp[1]) <= tol


def _expected(edl: EDL, window: tuple[float, float]) -> tuple[float, float]:
    """Where the clock says `window` lands — the same numbers the desktop
    draws (`timelineLayout.renderWindow`)."""
    win = clock.render_window(edl, *window)
    assert win is not None
    return win


def _export(edl: EDL, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    return render_export(edl, where, height=H).path


def _preview(edl: EDL, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    return render_preview(edl, where, height=H).path


@pytest.fixture(scope="module")
def picture(fx, tmp_path_factory) -> dict[bool, dict[str, tuple[float, float]]]:
    """Where v1 ITSELF shows the two marked instants, per fade setting, from a
    probe-less export: `b` = clip B's navy window (layout 3.0–3.5), `a` =
    clip A's yellow window (layout 1.6–1.9). The v1 assembly is identical
    with or without overlays on top, so these are the frames any probe
    authored at those windows must share."""
    out: dict[bool, dict[str, tuple[float, float]]] = {}
    for fade in (False, True):
        path = _export(_base(fx, fade=fade), tmp_path_factory.mktemp(f"picture_{int(fade)}"))
        b = _seen_window(path, _is_navy_marker)
        a = _seen_window(path, _is_yellow_marker)
        assert b is not None and a is not None, (fade, b, a)
        out[fade] = {"b": b, "a": a}
    # Sanity on the reference itself: the picture is where the clock says.
    assert _close(out[False]["b"], PROBE, FRAME_TOL) and _close(out[True]["b"], (2.5, 3.0), FRAME_TOL), out
    assert _close(out[False]["a"], PRE, FRAME_TOL) and _close(out[True]["a"], PRE, FRAME_TOL), out
    return out


# --------------------------------------------------------------- the picture

_PICTURE = {"sticker": _is_magenta, "text": _is_green_text, "anim_text": _is_green_text,
            "caption": _is_green_text, "pip_picture": _is_lime_frame}


@pytest.mark.parametrize("kind", sorted(_PICTURE))
def test_a_picture_probe_on_clip_b_lands_with_the_picture(tmp_path, fx, picture, kind):
    is_on = _PICTURE[kind]
    # Proven visible at layout time first: no transitions, render time IS
    # layout time, so the probe must sit at 3.0–3.5 — with the picture.
    plain = _plant(_base(fx, fade=False), kind, fx, PROBE)
    seen = _seen_window(_export(plain, tmp_path / "plain"), is_on)
    assert _close(seen, PROBE, FRAME_TOL), f"{kind} not visible at layout time: {seen}"
    assert _close(seen, picture[False]["b"], FRAME_TOL), (seen, picture[False]["b"])

    faded = _plant(_base(fx, fade=True), kind, fx, PROBE)
    out = _export(faded, tmp_path / "faded")
    assert abs(_file_duration(out) - 3.5) < 0.1
    assert _expected(faded, PROBE) == pytest.approx((2.5, 3.0))
    seen = _seen_window(out, is_on)
    assert _close(seen, (2.5, 3.0), FRAME_TOL), (
        f"{kind}: authored at layout {PROBE}, expected at render (2.5, 3.0) after the "
        f"{FADE}s fade at {SEAM}s, seen at {seen}")
    assert _close(seen, picture[True]["b"], FRAME_TOL), (
        f"{kind} at {seen} is not on the frames v1 shows layout {PROBE} on: {picture[True]['b']}")


# ----------------------------------------------------------------- the audio

@pytest.mark.parametrize("path", ["export", "preview"])
@pytest.mark.parametrize("kind", ["vo", "music", "pip_audio"])
def test_an_audio_probe_on_clip_b_lands_with_the_speech(tmp_path, fx, kind, path):
    render = _export if path == "export" else _preview
    plain = _plant(_base(fx, fade=False), kind, fx, PROBE)
    heard = _tone_window(_band_levels(render(plain, tmp_path / "plain"), TONE_HZ))
    assert _close(heard, PROBE, AUDIO_TOL), f"{kind} not audible at layout time: {heard}"

    faded = _plant(_base(fx, fade=True), kind, fx, PROBE)
    out = render(faded, tmp_path / "faded")
    heard = _tone_window(_band_levels(out, TONE_HZ))
    assert _close(heard, (2.5, 3.0), AUDIO_TOL), (
        f"{kind} via {path}: authored at layout {PROBE}, expected at render (2.5, 3.0), heard at {heard}")
    # ...and v1's own speech burst sits in the same window, which is the
    # whole point: the lane and the picture's sound agree.
    speech = _tone_window(_band_levels(out, SPEECH_HZ))
    assert _close(speech, (2.5, 3.0), AUDIO_TOL), speech
    assert _close(heard, speech, AUDIO_TOL), (heard, speech)


def test_the_audio_only_remux_fast_path_keeps_v1_and_the_lanes_on_the_clock(tmp_path, fx, monkeypatch):
    """A music/vo-only edit on a timeline WITH transitions takes the remux
    fast path (cached video-only mp4 + a freshly built audio mix). That path
    assembled v1's audio with a plain concat — no acrossfade, no gap filler —
    so after the first seam the speech ran late against a picture that did
    not, and the bed sat on the old layout clock on top of it."""
    edl = _plant(_base(fx, fade=True), "vo", fx, PROBE)
    first = _preview(edl, tmp_path)                       # full render, seeds the video-only cache
    assert _close(_tone_window(_band_levels(first, TONE_HZ)), (2.5, 3.0), AUDIO_TOL)

    calls: list[Path] = []
    real = C._remux_with_new_audio

    def spy(edl_, video_only, dst, **kw):
        calls.append(video_only)
        return real(edl_, video_only, dst, **kw)
    monkeypatch.setattr(C, "_remux_with_new_audio", spy)

    edl.get_track("vo").clips[0].audio.gain_db = 1.0     # audio-only change: same video fingerprint
    second = _preview(edl, tmp_path)
    assert calls, "the audio-only change did not take the remux fast path"
    assert second != first
    assert abs(_file_duration(second) - 3.5) < 0.1
    assert _close(_tone_window(_band_levels(second, TONE_HZ)), (2.5, 3.0), AUDIO_TOL)
    assert _close(_tone_window(_band_levels(second, SPEECH_HZ)), (2.5, 3.0), AUDIO_TOL), (
        "v1's speech drifted on the remux path: its audio must be acrossfaded like the picture was")


# ------------------------------------------------------- before / inside a seam

def test_a_probe_on_clip_a_before_the_seam_does_not_move(tmp_path, fx, picture):
    edl = _plant(_plant(_base(fx, fade=True), "sticker", fx, PRE), "vo", fx, PRE)
    out = _export(edl, tmp_path)
    assert _expected(edl, PRE) == pytest.approx(PRE)
    seen = _seen_window(out, _is_magenta)
    assert _close(seen, PRE, FRAME_TOL), seen
    assert _close(seen, picture[True]["a"], FRAME_TOL), (seen, picture[True]["a"])
    assert _close(_tone_window(_band_levels(out, TONE_HZ)), PRE, AUDIO_TOL)


def test_a_probe_covering_exactly_the_consumed_tail_is_dropped(tmp_path, fx):
    """Layout `[1.5, 2.0)` is clip A's last 0.5 s — what the fade eats. It maps
    to a zero-length window and is DROPPED, the same answer the desktop gives
    (`renderWindow(...).dropped`), rather than an inverted or clamped gate.
    Proven non-vacuous: without the fade the same sticker is on screen."""
    plain = _plant(_base(fx, fade=False), "sticker", fx, TAIL)
    assert _close(_seen_window(_export(plain, tmp_path / "plain"), _is_magenta), TAIL, FRAME_TOL)

    faded = _plant(_plant(_base(fx, fade=True), "sticker", fx, TAIL), "vo", fx, TAIL)
    assert clock.render_window(faded, *TAIL) is None
    out = _export(faded, tmp_path / "faded")            # must still render cleanly
    assert _seen_window(out, _is_magenta) is None
    assert _tone_window(_band_levels(out, TONE_HZ)) is None


def test_a_lane_clip_straddling_the_seam_ends_where_the_picture_does(tmp_path, fx):
    """A music clip at layout 1.5–2.5 covers A's tail AND B's head; on screen
    those are the same 0.5 s, so the bed is heard for 1.5–2.0 and stops there
    — it does not run on past the frame authored under its end."""
    edl = _plant(_base(fx, fade=True), "music", fx, (1.5, 2.5))
    assert _expected(edl, (1.5, 2.5)) == pytest.approx((1.5, 2.0))
    heard = _tone_window(_band_levels(_export(edl, tmp_path), TONE_HZ))
    assert _close(heard, (1.5, 2.0), AUDIO_TOL), heard


# ------------------------------------------------------------------ ducking

def test_music_ducks_under_the_shifted_speech(tmp_path, fx):
    """The bed is side-chained against v1's mix — already on the render clock
    — so once the bed itself is placed on that clock the dip lands where the
    word is heard. Reference level from 1.0–1.4 (no speech either way)."""
    def bed(edl: EDL) -> EDL:
        m = edl.get_track("music")
        m.duck = MusicDuck()
        m.clips += [Clip(id="m1", src=str(fx["tone"]), in_=0, out=2, start=0),
                    Clip(id="m2", src=str(fx["tone"]), in_=0, out=2, start=2)]
        edl.recompute_duration()
        return edl

    plain = _band_levels(_export(bed(_base(fx, fade=False)), tmp_path / "plain"), TONE_HZ)
    ref = _mean_level(plain, 1.0, 1.4)
    assert _mean_level(plain, 2.6, 2.95) > ref - 1.5, "no speech at layout 2.6–2.95 without a transition"
    assert _mean_level(plain, 3.1, 3.4) < ref - 8.0, "the bed must dip under the burst at layout 3.0–3.5"

    faded_out = _export(bed(_base(fx, fade=True)), tmp_path / "faded")
    assert _close(_tone_window(_band_levels(faded_out, SPEECH_HZ)), (2.5, 3.0), AUDIO_TOL)
    faded = _band_levels(faded_out, TONE_HZ)
    assert abs(_mean_level(faded, 1.0, 1.4) - ref) < 1.5
    assert _mean_level(faded, 2.6, 2.95) < ref - 8.0, (
        "the duck did not move with the speech: bed level at render 2.6–2.95 "
        f"{_mean_level(faded, 2.6, 2.95):.1f} dB vs reference {ref:.1f} dB")
