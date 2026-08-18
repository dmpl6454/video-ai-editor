"""`Transform.opacity` on a v1 clip must actually darken the picture.

It was a DEAD CONTROL: compositor.py's per-clip chain had no opacity handling at
all, so the value committed to the EDL, survived a reload, and changed nothing in
the preview or the export. It was honoured for PIPs (pip.py), text and stickers,
which is exactly why it looked implemented.

It also made the live preview lie. Preview.tsx applies a CSS opacity RELATIVE to
what the visible render has baked in and reads that baked value from the EDL — so
the EDL said 0.3 while the frame was at full brightness, and dragging back up
showed nothing because CSS opacity cannot exceed 1.

These tests measure REAL RENDERED PIXELS rather than asserting on the filter
string, because the interesting failure is not "is a filter present" but "does
the picture get darker, and does it stay the same colour while it does". A YUV
colorchannelmixer would scale the chroma offsets too and tint the shot green as
it dimmed — a string assertion passes straight through that.
"""
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from video_ai_editor.edl.schema import Canvas, Clip, EDL, Track

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _has_filter(name: str) -> bool:
    """Is this filter compiled into the ffmpeg we will actually shell out to?

    `geq` is GPL-only. Every build this project targets has it (Homebrew's
    formula is --enable-gpl, Gyan's full build likewise, and text_overlay.py has
    relied on it for a long time), but the macOS CI job installs plain `ffmpeg`
    rather than ffmpeg-full — so this degrades to a skip rather than a red build
    on a toolchain we did not anticipate. Same posture as `stabilize` skipping
    where libvidstab is absent.
    """
    if not FFMPEG:
        return False
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-filters"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30).stdout
    except Exception:
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


@pytest.fixture
def src(tmp_path):
    """A flat mid-grey clip: any brightness change is unambiguous.

    WITH a silent audio track — the compositor always builds an `[0:a]` branch,
    so a video-only fixture fails the whole graph with "Stream specifier ':a'
    matches no streams" and every assertion here becomes unreachable.
    """
    p = tmp_path / "grey.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=0x808080:s=320x180:r=15:d=1",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1",
                    "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(p)], check=True)
    return p


def _edl(src, opacity, rotation=0.0):
    e = EDL(canvas=Canvas(w=320, h=180, fps=15))
    e.tracks = [Track(id="v1", type="video", z=0, clips=[
        Clip(id="c1", src=str(src), start=0.0, **{"in": 0.0}, out=1.0,
             transform={"opacity": opacity, "rotation": rotation})])]
    return e


def _render_mean(edl, tmp_path, tag):
    from video_ai_editor.render.compositor import render_preview
    # render_preview returns a RenderResult, not a Path.
    out = render_preview(edl, tmp_path / f"prev_{tag}").path
    png = tmp_path / f"f_{tag}.png"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "0.3", "-i", str(out),
                    "-frames:v", "1", str(png)], check=True)
    a = np.array(Image.open(png).convert("RGB")).astype(float)
    # Centre patch only: a rotated frame has black corners by design, and this
    # measures the PICTURE, not the letterbox.
    h, w, _ = a.shape
    c = a[h // 3:2 * h // 3, w // 3:2 * w // 3, :]
    return c.reshape(-1, 3).mean(axis=0)


def test_opacity_1_is_unchanged(src, tmp_path):
    """The default must emit no filter and no colour shift at all."""
    full = _render_mean(_edl(src, 1.0), tmp_path, "full")
    assert full.mean() > 100, f"fixture is not mid-grey: {full}"


def test_lower_opacity_darkens_the_picture(src, tmp_path):
    full = _render_mean(_edl(src, 1.0), tmp_path, "o1")
    half = _render_mean(_edl(src, 0.5), tmp_path, "o5")
    # The whole bug: this used to be equal.
    assert half.mean() < full.mean() * 0.75, (
        f"opacity 0.5 did not darken: full={full.mean():.1f} half={half.mean():.1f}")


def test_opacity_is_monotonic(src, tmp_path):
    """Each step down must be darker than the last — a control that only reacts
    at one threshold reads as broken just as surely as one that never reacts."""
    means = [_render_mean(_edl(src, o), tmp_path, f"m{int(o*100)}").mean()
             for o in (1.0, 0.75, 0.5, 0.25)]
    for a, b in zip(means, means[1:]):
        assert b < a - 8, f"not monotonic: {[round(m, 1) for m in means]}"


def test_dimming_does_not_TINT_the_picture(src, tmp_path):
    """A grey stays grey.

    This is why the multiply happens in gbrp. colorchannelmixer on YUV input
    scales the chroma OFFSETS as well as luma, which drags 128/128 toward 0/0 —
    i.e. hard green — so the shot would tint as it dimmed. The filter string
    looks identical either way; only pixels catch it.
    """
    m = _render_mean(_edl(src, 0.4), tmp_path, "tint")
    spread = float(m.max() - m.min())
    assert spread < 12, f"dimming tinted the picture: RGB={m.round(1)} spread={spread:.1f}"


def test_opacity_composes_with_rotation(src, tmp_path):
    """The reported combination. Rotation cuts corners; opacity dims what is
    left. Both must apply, and the centre must still hold a picture (the report
    was that it went black)."""
    plain = _render_mean(_edl(src, 1.0, rotation=25.0), tmp_path, "r25")
    dim = _render_mean(_edl(src, 0.5, rotation=25.0), tmp_path, "r25o5")
    assert dim.mean() < plain.mean() * 0.75, "opacity ignored once rotated"
    assert dim.mean() > 15, f"rotated+dimmed went black: {dim.round(1)}"


@pytest.mark.skipif(not _has_filter("geq"), reason="ffmpeg built without geq (GPL)")
def test_keyframed_opacity_animates_instead_of_being_dropped(src, tmp_path):
    """A keyed opacity must not silently fall back to fully opaque.

    `add_keyframe` puts opacity in its props list, so this is reachable from one
    click in the panel — and a static-only implementation would leave it as the
    same dead control, just in a corner nobody checks.
    """
    e = _edl(src, 1.0)
    e.tracks[0].clips[0].transform.opacity = {"keyframes": [[0.0, 1.0], [1.0, 0.0]]}
    from video_ai_editor.render.compositor import render_preview
    out = render_preview(e, tmp_path / "kf").path

    def mean_at(ts, tag):
        png = tmp_path / f"kf_{tag}.png"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", str(ts), "-i", str(out),
                        "-frames:v", "1", str(png)], check=True)
        return float(np.array(Image.open(png).convert("RGB")).astype(float).mean())

    early, late = mean_at(0.1, "early"), mean_at(0.9, "late")
    assert late < early * 0.6, f"keyframed opacity did not ramp: {early:.1f} -> {late:.1f}"


def test_a_dimmed_clip_concats_with_an_UNDIMMED_one(src, tmp_path):
    """A timeline mixing opacities must still render.

    The opacity multiply happens in `gbrp`, so a dimmed clip's chain ended in a
    different PIXEL FORMAT from an untouched one — and `concat` requires all its
    inputs to match. A single-clip test cannot see this: every assertion above
    passes while any real two-clip timeline with one faded clip dies with
    "Input link parameters do not match". Same failure class the `setsar=1` at
    the end of the chain already exists to prevent for SAR.
    """
    from video_ai_editor.edl.schema import Canvas, Clip, EDL, Track
    from video_ai_editor.render.compositor import render_preview
    e = EDL(canvas=Canvas(w=320, h=180, fps=15))
    e.tracks = [Track(id="v1", type="video", z=0, clips=[
        Clip(id="a", src=str(src), start=0.0, **{"in": 0.0}, out=1.0,
             transform={"opacity": 1.0}),
        Clip(id="b", src=str(src), start=1.0, **{"in": 0.0}, out=1.0,
             transform={"opacity": 0.4}),
    ])]
    out = render_preview(e, tmp_path / "mixed").path
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.skipif(not _has_filter("geq"), reason="ffmpeg built without geq (GPL)")
def test_a_keyframed_clip_concats_with_a_plain_one(src, tmp_path):
    """Same trap on the animated branch, which also emits gbrp."""
    from video_ai_editor.edl.schema import Canvas, Clip, EDL, Track
    from video_ai_editor.render.compositor import render_preview
    e = EDL(canvas=Canvas(w=320, h=180, fps=15))
    kf = Clip(id="b", src=str(src), start=1.0, **{"in": 0.0}, out=1.0)
    kf.transform.opacity = {"keyframes": [[0.0, 1.0], [1.0, 0.2]]}
    e.tracks = [Track(id="v1", type="video", z=0, clips=[
        Clip(id="a", src=str(src), start=0.0, **{"in": 0.0}, out=1.0), kf])]
    out = render_preview(e, tmp_path / "mixedkf").path
    assert out.exists() and out.stat().st_size > 0
