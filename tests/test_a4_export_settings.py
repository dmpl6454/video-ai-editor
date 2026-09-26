"""Wave-A lane A4: export settings are real, measured on the encoded file.

QA-025  a named export resolution is the SHORT side for the canvas orientation
        ("1080p" on 9:16 used to export 608x1080).
QA-027  a platform preset's bitrate target reaches the encoder (it was written
        to canvas.bitrate_kbps and read by nothing in render/), and the preset's
        loudness target lands in the file.

Every assertion is an ffprobe / ebur128 measurement of a real ffmpeg export.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip
from video_ai_editor.render import render_export
from video_ai_editor.render.compositor import export_dimensions

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _mk(path: Path, w: int, h: int, dur: float, *, noisy: bool = False,
        tone_db: float = -30.0) -> Path:
    """A synthetic clip. `noisy` makes every frame expensive to code, so a
    quality-mode encode lands FAR above any platform target — the case where a
    missing bitrate cap is visible."""
    video = f"testsrc2=size={w}x{h}:rate=30:duration={dur}"
    if noisy:
        video += ",noise=alls=35:allf=t+u"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", video,
         "-f", "lavfi", "-i", f"sine=f=440:duration={dur}:sample_rate=48000",
         "-af", f"volume={tone_db}dB",
         "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(path)],
        check=True, capture_output=True)
    return path


def _probe_video(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,bit_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return {"w": int(s["width"]), "h": int(s["height"]), "kbps": int(s["bit_rate"]) / 1000}


def _integrated_lufs(path: Path) -> float:
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])


def _session(tmp_path: Path, src: Path, dur: float, w: int, h: int) -> EDLStore:
    s = EDLStore(tmp_path / "sess")
    s.edl.canvas = Canvas(w=w, h=h, fps=30)
    s.edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=dur, start=0.0))
    s.commit("seed", {}, "seed")
    return s


# ---------------------------------------------------------------- QA-025

@pytest.mark.parametrize("cw,ch,short,expect", [
    (1080, 1920, 1080, (1080, 1920)),
    (1080, 1920, 720, (720, 1280)),
    (1080, 1920, 2160, (2160, 3840)),
    (1920, 1080, 1080, (1920, 1080)),
    (1920, 1080, 720, (1280, 720)),
    (1080, 1080, 720, (720, 720)),
    (1080, 1350, 1080, (1080, 1350)),
    (1080, 1920, None, (1080, 1920)),
])
def test_named_resolution_is_the_short_side(cw, ch, short, expect):
    assert export_dimensions(cw, ch, short) == expect


@pytest.mark.parametrize("cw,ch,short,expect", [
    (1080, 1920, 1080, (1080, 1920)),     # QA-025: was 608x1080
    (1080, 1920, 720, (720, 1280)),       # was 404x720
    (1920, 1080, 720, (1280, 720)),       # landscape unchanged
    (1080, 1350, 1080, (1080, 1350)),     # 4:5 (was 864x1080)
])
def test_export_file_measures_the_named_resolution(tmp_path, cw, ch, short, expect):
    src = _mk(tmp_path / "src.mp4", cw, ch, 1.0)
    s = _session(tmp_path, src, 1.0, cw, ch)
    res = render_export(s.edl, s.dir, height=short)
    got = _probe_video(res.path)
    assert (got["w"], got["h"]) == expect


def test_export_over_http_honours_short_side_and_rejects_absurd_sizes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import video_ai_editor.main as m
    src = _mk(tmp_path / "src.mp4", 1080, 1920, 1.0)
    s = _session(tmp_path, src, 1.0, 1080, 1920)
    monkeypatch.setattr(m, "_store", lambda sid: s)
    c = TestClient(m.app)
    r = c.post("/api/sessions/s_export0001/export", json={"height": 720})
    assert r.status_code == 200, r.text
    got = _probe_video(Path(r.json()["path"]))
    assert (got["w"], got["h"]) == (720, 1280)
    assert c.post("/api/sessions/s_export0001/export",
                  json={"height": 1_000_000_000}).status_code == 422


# ---------------------------------------------------------------- QA-027

@pytest.mark.parametrize("preset,cw,ch,target", [
    ("story", 1080, 1920, 6000),
    ("youtube_16x9", 1920, 1080, 12000),
])
def test_preset_bitrate_target_is_what_the_file_measures(tmp_path, preset, cw, ch, target):
    dur = 4.0
    src = _mk(tmp_path / "noisy.mp4", cw, ch, dur, noisy=True)
    s = _session(tmp_path, src, dur, cw, ch)
    dispatch(s, "apply_export_preset", {"name": preset})
    assert s.edl.canvas.bitrate_kbps == target
    got = _probe_video(render_export(s.edl, s.dir).path)
    # Pre-fix this content exported in quality mode at several times the target.
    assert got["kbps"] == pytest.approx(target, rel=0.15), got


def test_explicit_quality_mode_bypasses_the_preset_target(tmp_path):
    """bitrate_kbps=0 is the Quality selector's explicit "encode by crf" — the
    preset must not silently override a choice the user made in the dialog."""
    dur = 3.0
    src = _mk(tmp_path / "noisy.mp4", 1080, 1920, dur, noisy=True)
    s = _session(tmp_path, src, dur, 1080, 1920)
    dispatch(s, "apply_export_preset", {"name": "story"})
    got = _probe_video(render_export(s.edl, s.dir, bitrate_kbps=0, crf=18).path)
    assert got["kbps"] > 6000 * 1.5, got


def test_shorts_preset_loudness_target_lands_in_the_file(tmp_path):
    dur = 6.0
    src = _mk(tmp_path / "quiet.mp4", 1080, 1920, dur, tone_db=-32.0)
    s = _session(tmp_path, src, dur, 1080, 1920)
    dispatch(s, "apply_export_preset", {"name": "shorts"})
    lufs = _integrated_lufs(render_export(s.edl, s.dir).path)
    assert lufs == pytest.approx(-14.0, abs=1.5)


# ---------------------------------------------------------------- QA-041 (export)

@pytest.mark.parametrize("wait", [1, 0])
@pytest.mark.parametrize("fps", [1_000_000, 0, -3, 241])
def test_export_frame_rate_is_bounded_before_any_render_starts(tmp_path, monkeypatch, wait, fps):
    """`fps` was the one unbounded number on POST /export: fps=1e6 built an
    `fps=1000000,trim=end_frame=10000000` graph per 10 s segment — an encode
    with no practical end, the QA-041 failure through the export door. It is
    now a 422 at the request boundary, for the sync and the job path alike,
    and nothing is rendered."""
    from fastapi.testclient import TestClient
    import video_ai_editor.main as m
    s = EDLStore(tmp_path / "sess")
    s.edl.get_track("v1").clips.append(Clip(src="/x/a.mp4", in_=0.0, out=2.0, start=0.0))
    s.commit("seed", {}, "seed")
    monkeypatch.setattr(m, "_store", lambda sid: s)
    calls: list = []
    monkeypatch.setattr(m, "render_export", lambda *a, **k: calls.append(k))
    r = TestClient(m.app).post(f"/api/sessions/s_exportfps1/export?wait={wait}", json={"fps": fps})
    assert r.status_code == 422, r.text
    assert "fps" in r.text
    assert calls == []


def test_export_accepts_real_delivery_rates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import video_ai_editor.main as m
    from types import SimpleNamespace
    s = EDLStore(tmp_path / "sess")
    # A clip to export: an empty timeline is refused up front (QA-123).
    s.edl.get_track("v1").clips.append(Clip(src=str(tmp_path / "a.mp4"), in_=0.0, out=2.0, start=0.0))
    (tmp_path / "a.mp4").write_bytes(b"\0")
    s.commit("seed", {}, "seed")
    monkeypatch.setattr(m, "_store", lambda sid: s)
    seen: list = []

    def fake(edl, sdir, **k):
        seen.append(k["fps"])
        p = sdir / "exports" / "x.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return SimpleNamespace(path=p, edl_hash="h")
    monkeypatch.setattr(m, "render_export", fake)
    c = TestClient(m.app)
    for fps in (23.976, 29.97, 60, None):
        body = {} if fps is None else {"fps": fps}
        assert c.post("/api/sessions/s_exportfps2/export", json=body).status_code == 200
    assert seen == [23.976, 29.97, 60, None]


_BENCH_16X9 = Path("/Users/sudhanshu/Library/Caches/Video AI Editor/bench/8515fa4411c9/scene_16x9.mp4")


@pytest.mark.parametrize("kind", ["testsrc2", "bench_scene"])
def test_easy_footage_is_not_starved_below_the_preset_target(tmp_path, kind):
    """The other half of QA-027: a platform target is an AVERAGE the file
    should measure, not only a ceiling. VideoToolbox treats any -maxrate as a
    data-rate LIMIT and undershoots compressible footage — the owner's
    16:9 bench scene exported at 7948 kb/s on the 12000 kb/s YouTube preset
    (-34%), testsrc2 at about -15%. The encode is now uncapped first and only
    re-encoded with the peak cap when it overshoots (the noisy-content case
    above), so both directions land on the target."""
    if kind == "bench_scene":
        if not _BENCH_16X9.exists():
            pytest.skip("owner's bench footage not on this machine")
        src, i0, dur = _BENCH_16X9, 30.0, 12.0
    else:
        src, i0, dur = _mk(tmp_path / "clean.mp4", 1920, 1080, 6.0), 0.0, 6.0
    s = EDLStore(tmp_path / "sess")
    s.edl.canvas = Canvas(w=1920, h=1080, fps=30)
    s.edl.get_track("v1").clips.append(Clip(src=str(src), in_=i0, out=i0 + dur, start=0.0))
    s.commit("seed", {}, "seed")
    dispatch(s, "apply_export_preset", {"name": "youtube_16x9"})
    got = _probe_video(render_export(s.edl, s.dir).path)
    assert got["kbps"] == pytest.approx(12000, rel=0.10), got


def test_capped_second_pass_keeps_progress_monotonic(tmp_path):
    """The job path (on_progress + cancel_event) through an overshooting
    first pass: the capped re-encode lands on target and the progress bar
    never runs backwards between the two passes."""
    import threading
    dur = 4.0
    src = _mk(tmp_path / "noisy.mp4", 1080, 1920, dur, noisy=True)
    s = _session(tmp_path, src, dur, 1080, 1920)
    dispatch(s, "apply_export_preset", {"name": "story"})
    seen: list[float] = []
    res = render_export(s.edl, s.dir, on_progress=seen.append, cancel_event=threading.Event())
    assert _probe_video(res.path)["kbps"] == pytest.approx(6000, rel=0.15)
    assert seen and all(b >= a for a, b in zip(seen, seen[1:])), seen
