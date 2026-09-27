"""The v1 keyframe matrix on decoded renders (Wave D3, lane E1a; see
keyframe_matrix_lib): every keyframed property × every clock (1x, 0.5x,
2x, a speed curve, reverse, reverse 2x, a freeze) × clips at 0, 2 and 30 s ×
every interpolation, against the value the UI samples at `playhead -
clip.start`, mapped through ffmpeg's integer rules. The export used to key
at the SOURCE frame's time with a crop-zoom whose +x moved the picture LEFT
(a 0.5x clip held every second value, a keyed zoom grew from the top-left,
a scale < 1 did nothing); every row here failed before.

Also: the chunked render and the server preview, a pan at another output
size, and the other keyframable field in the schema (`AudioProps.gain_env`).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import keyframe_matrix_lib as lib
from video_ai_editor.edl.keyframes import sample
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Keyframe, empty_edl
from video_ai_editor.render import compositor

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

#: px: centroid noise on a lossless-ish render, after ffmpeg's rounding is modelled
POS_TOL = 0.35
#: RGB gain: geq truncates, and a yuv420p round trip moves flat grey ≤ 2 levels
GAIN_TOL = 0.02
RENDERS = {r.name: r for r in lib.renders()}


@pytest.fixture(scope="module")
def work(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("kf-matrix")


@pytest.fixture(scope="module")
def src(work) -> str:
    return str(lib.make_source(work / "src" / "mx.mp4"))


@pytest.fixture(scope="module")
def measured(src, work) -> dict[str, tuple[EDL, list[dict]]]:
    return {name: lib.render_and_measure(r, src, work) for name, r in RENDERS.items()}


def _check(r: lib.Render, edl: EDL, rows: list[dict], *, pos_tol: float, gain_tol: float) -> list[str]:
    clips = {c.id: c for c in edl.get_track("v1").clips}
    names = ("white",) if r.group == "pan" else ("red", "green")
    bad = []
    for row in rows:
        c = clips[row["clip"]]
        if any(row[n] is None for n in names):
            bad.append(f"k={row['k']} {row['clip']}: a marker is off the picture")
            continue
        err = lib.marker_error(c, row["k"], row, names)
        if err > pos_tol:
            bad.append(f"k={row['k']} {row['clip']}: {err:.2f} px from the UI's value")
        if r.group == "pan":
            want = lib.ui_values(c, row["k"])["opacity"]
            if abs(row["gain"] - want) > gain_tol:
                bad.append(f"k={row['k']} {row['clip']}: gain {row['gain']} vs opacity {want:.4f}")
    return bad


def test_matrix_covers_every_clock_start_property_and_interpolation():
    clocks = {c for r in RENDERS.values() for _cid, _st, c, _tx in r.clips}
    assert clocks == set(lib.CLOCKS)
    assert {st for r in RENDERS.values() for _cid, st, _c, _tx in r.clips} >= set(lib.STARTS)
    props = {p for r in RENDERS.values() for *_x, tx in r.clips for p, v in tx.items() if isinstance(v, dict)}
    assert props == {"x", "y", "scale", "rotation", "opacity"}
    interps = {v["interp"] for r in RENDERS.values() for *_x, tx in r.clips for v in tx.values() if isinstance(v, dict)}
    assert interps == set(lib.INTERPS)


@pytest.mark.parametrize("name", list(RENDERS))
def test_export_animates_at_the_ui_clock(name, measured):
    edl, rows = measured[name]
    frames = sum(b - a for a, b in lib.clip_frames(edl).values())
    assert len(rows) == frames
    bad = _check(RENDERS[name], edl, rows, pos_tol=POS_TOL, gain_tol=GAIN_TOL)
    assert not bad, f"{name}: {len(bad)} of {len(rows)} frames disagree, e.g. {bad[:4]}"


def test_step_keys_switch_on_their_own_frame(measured):
    """A step key authored at a playhead (a frame time such as 23/30 s) shows
    its value ON that frame: the export used to print it %.4f (0.7667) and
    switch a frame late, and the UI sampler held the previous value there."""
    edl, rows = measured["pan_interp"]
    c = next(c for c in edl.get_track("v1").clips if c.transform.x.interp == "step")
    t_key = c.transform.x.keyframes[1][0]
    k_key = round((c.start + t_key) * lib.FPS)
    by_k = {row["k"]: row for row in rows}
    before, at = by_k[k_key - 1]["white"][0], by_k[k_key]["white"][0]
    assert at - before == pytest.approx(150, abs=2)        # -90 → 60 exactly on the key's frame
    assert sample(c.transform.x, t_key) == 60


def test_golden_is_current(measured):
    """tests/goldens/keyframe_matrix.json (the frontend's fixture) still
    describes the compositor."""
    gold = {r["name"]: r for r in lib.load()["renders"]}
    assert set(gold) == set(RENDERS)
    for name, (edl, rows) in measured.items():
        assert gold[name]["edl"] == lib.edl_json(edl), name
        for got, want in zip(rows, gold[name]["rows"], strict=True):
            assert (got["k"], got["clip"]) == (want["k"], want["clip"])
            for n in ("white", "red", "green"):
                if want[n] is None or got[n] is None:
                    assert want[n] is None and got[n] is None, (name, got["k"], n)
                else:
                    assert got[n] == pytest.approx(want[n], abs=0.3), (name, got["k"], n)
            assert got["gain"] == pytest.approx(want["gain"], abs=0.01), (name, got["k"])


@pytest.mark.parametrize("name", ["pan_0.5x", "zoom_curve", "pan_reverse_2x"])
def test_chunked_render_agrees(name, src, work):
    edl, rows = lib.render_and_measure(RENDERS[name], src, work, chunked=True)
    bad = _check(RENDERS[name], edl, rows, pos_tol=POS_TOL, gain_tol=GAIN_TOL)
    assert not bad, bad[:4]


@pytest.mark.parametrize("name", ["pan_freeze", "zoom_0.5x"])
def test_server_preview_agrees(name, src, work):
    """render_preview (ultrafast crf 30: a little noisier) — the bake spans
    the engine splices come from this path."""
    edl, rows = lib.render_and_measure(RENDERS[name], src, work, preview=True)
    bad = _check(RENDERS[name], edl, rows, pos_tol=0.6, gain_tol=0.03)
    assert not bad, bad[:4]


def test_pans_are_canvas_pixels_at_any_output_size(src, work):
    """A 640x360 project exported at 1280x720: every pan doubles with the
    frame (it stayed in output pixels, half as far: compositor
    `v1_pans_at_output`); the server preview of a large canvas is the same
    case the other way round."""
    r = RENDERS["pan_1x"]
    edl, (info,) = lib.render_and_measure(r, src, work, height=720)
    spans = lib.clip_frames(edl)
    ks = [k for a, b in spans.values() for k in range(a, b, 7)]
    rgb, ys = lib.decode(Path(info["path"]), ks, 1280, 720)
    clips = {c.id: c for c in edl.get_track("v1").clips}
    for i, k in enumerate(ks):
        cid = next(c for c, (a, b) in spans.items() if a <= k < b)
        m = lib.measure(rgb[i], ys[i])
        want = lib.expected_markers(clips[cid], lib.ui_values(clips[cid], k))["white"]
        assert m["white"] == pytest.approx([2 * want[0], 2 * want[1]], abs=2.2), k


# ----------------------------------------------- the other keyframable field

def _tone(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=0x808080:s=320x180:r=30:d=12",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=12",
                    "-af", "volume=-12dB", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "pcm_s16le",
                    "-shortest", str(path.with_suffix(".mov"))], check=True, capture_output=True)
    return path.with_suffix(".mov")


@pytest.mark.parametrize("start,speed", [(0.0, None), (2.0, 2.0), (30.0, 0.5)])
def test_gain_envelope_follows_the_timeline_clock(start, speed, work, tmp_path):
    """`AudioProps.gain_env` (dB offsets on clip-local TIMELINE seconds): the
    rendered level (5 ms RMS steps) against the envelope sampled at
    `playhead - clip.start`. A wrong clock is hundreds of ms off (source
    time at 0.5x/2x) or 30 s off (`t - start`); what remains is `volume`'s
    per-audio-FRAME evaluation (eval=frame: one gain per ≈21-43 ms packet
    after atempo), measured as the lag and bounded here."""
    tone = _tone(work / "tone")
    e = empty_edl(Canvas(w=320, h=180, fps=30))
    e.canvas.loudness_lufs = None
    c = Clip(src=str(tone), id="g", start=start, speed=speed)
    c.in_, c.out = 1.0, 1.0 + 1.5 * (speed or 1.0)
    c.audio.gain_env = Keyframe(keyframes=[(0.2, 0.0), (0.9, -18.0), (1.3, -6.0)], interp="ease-in-out")
    e.get_track("v1").clips.append(c)
    e.recompute_duration()
    with lib.software_encoder():
        out = compositor._render(e, tmp_path / "a.mp4", height=180, fps=30, preview=False,
                                 cache_dir=tmp_path / "cache", chunked=False)
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-map", "0:a", "-ac", "1",
                          "-f", "f32le", "-ar", "48000", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, np.float32).astype(np.float64)

    def level(t: float) -> float:
        seg = a[int((t - 0.0025) * 48000):int((t + 0.0025) * 48000)]
        return 20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) + 1e-12)

    ts = np.arange(0.02, 1.46, 0.005)
    got = np.array([level(start + t) for t in ts])
    got -= float(np.median(got[(ts > 0.05) & (ts < 0.18)]))     # the 0 dB hold before the first key
    fits = [(float(np.median(np.abs(got - np.array([sample(c.audio.gain_env, float(t - lag)) for t in ts])))), lag)
            for lag in np.arange(-0.06, 0.0601, 0.0025)]
    med, lag = min(fits)
    # ≤ one audio packet (1024 samples at 1x/2x, 2048 after atempo 0.5x):
    # the envelope rides the timeline clock; its per-packet staircase is a
    # separate, audio-side limit (see the lane notes)
    assert abs(lag) <= 0.045, (start, speed, lag, med)
    assert med < 0.6, (start, speed, lag, med)
