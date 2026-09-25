"""QA-066: the heavy per-frame AI tools report progress, stop on Cancel, and
keep every frame.

They used to run as blocking chains with no progress and no cancel (/api/tools
honestly said `cancellable=false, reports_progress=false`, and the card said
"this tool can't be interrupted" for 273 s), and their encoders passed a
rounded frame rate plus `-shortest`, so a source whose audio ends early lost
its last frames.

Real binaries where this machine has them (Real-ESRGAN, RIFE, a vidstab
ffmpeg, noisereduce) — skipped cleanly where it does not; rembg and LaMa run
their real frame loop and encoder with the MODEL call stubbed, since neither
model is cached here and a test must never download one.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, empty_edl

HEAVY = ("upscale", "stabilize", "smooth_slow_motion", "noise_reduce",
         "object_erase", "remove_background")


def _ff(dst: Path, *args: str) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


def _clip(d: Path, name: str, *, size="96x54", seconds=1.0, audio_seconds=0.5,
          rate="30000/1001") -> Path:
    """Picture longer than its audio — the shape that lost frames."""
    return _ff(d / name, "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_seconds}",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")


def _frames(path: str | Path) -> tuple[int, str]:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-count_packets", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_packets,avg_frame_rate", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["nb_read_packets"]), s["avg_frame_rate"]


def _store(tmp: Path, src: Path) -> EDLStore:
    d = tmp / "sess"
    d.mkdir()
    edl = empty_edl(Canvas(w=320, h=180, fps=29.97))
    n, rate = _frames(src)
    edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=round(n * 1001 / 30000, 6),
                                          start=0.0, id="c1"))
    edl.recompute_duration()
    (d / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(d)


class _Progress:
    def __init__(self):
        self.values: list[float] = []

    def __call__(self, p: float) -> None:
        self.values.append(float(p))

    def assert_sane(self, min_steps: int = 3) -> None:
        assert self.values, "no progress was reported"
        assert all(0.0 <= v <= 1.0 for v in self.values), self.values
        assert self.values == sorted(self.values), "progress went backwards"
        assert len({round(v, 3) for v in self.values if 0 < v < 1}) >= min_steps, self.values
        assert self.values[-1] == pytest.approx(1.0, abs=0.02)


def _cancel_when(progress_over: float, prog: _Progress, ev: threading.Event) -> dict:
    """Set `ev` once progress passes `progress_over`; the returned dict gets
    the monotonic time it was set, so a test can bound how fast Cancel bites."""
    at: dict = {}

    def watch():
        for _ in range(3000):
            if prog.values and prog.values[-1] > progress_over:
                at["t"] = time.monotonic()
                ev.set()
                return
            time.sleep(0.02)
    threading.Thread(target=watch, daemon=True).start()
    return at


# ---------------------------------------------------------------- contract


def test_tools_route_advertises_cancel_and_progress_for_the_heavy_tools():
    from fastapi.testclient import TestClient
    from video_ai_editor import main
    tools = {t["name"]: t for t in TestClient(main.app).get("/api/tools").json()["tools"]}
    for name in HEAVY:
        assert tools[name]["cancellable"] is True, name
        assert tools[name]["reports_progress"] is True, name


# ---------------------------------------------------------------- Real-ESRGAN


def _need(mod: str, attr: str = "available"):
    import importlib
    m = importlib.import_module(f"video_ai_editor.ai.{mod}")
    if not getattr(m, attr)():
        pytest.skip(f"{mod} not installed on this machine")
    return m


def test_upscale_reports_progress_and_keeps_every_frame(tmp_path):
    _need("upscale")
    # Enough frames that the binary is sampled mid-run (frames are counted
    # as it writes them); audio ends at half the picture.
    src = _clip(tmp_path, "v.mp4", size="160x90", seconds=3.0, audio_seconds=1.5)
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "upscale", {"clip_id": "c1", "factor": 2}, set_progress=prog)
    prog.assert_sane()
    out = store.edl.get_clip("c1")[1].src
    assert out != str(src)
    assert _frames(out) == _frames(src), "the upscale must keep every frame at the exact rate"


def test_upscale_cancel_stops_the_binary_and_leaves_nothing(tmp_path):
    _need("upscale")
    src = _clip(tmp_path, "long.mp4", size="160x90", seconds=6.0, audio_seconds=6.0)
    store = _store(tmp_path, src)
    prog, ev = _Progress(), threading.Event()
    at = _cancel_when(0.15, prog, ev)
    from video_ai_editor.ai.jobio import ToolCancelled
    with pytest.raises(ToolCancelled):
        dispatch(store, "upscale", {"clip_id": "c1", "factor": 2},
                 set_progress=prog, cancel_event=ev)
    # The binary is killed, not waited out (a full run here takes ~15 s).
    assert time.monotonic() - at["t"] < 3.0
    assert store.edl.get_clip("c1")[1].src == str(src), "a cancelled tool must not swap the clip"
    cache = store.dir / "cache" / "upscale"
    assert not list(cache.glob("esrgan_work_*")) and not list(cache.glob("upscaled_*"))


def test_upscale_job_can_be_cancelled_over_http(monkeypatch, tmp_path):
    _need("upscale")
    import importlib
    from fastapi.testclient import TestClient
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    client = TestClient(_main.app)
    sid = client.post("/api/sessions").json()["id"]
    src = _clip(tmp_path, "long.mp4", size="160x90", seconds=6.0, audio_seconds=6.0)
    with src.open("rb") as f:
        assert client.post(f"/api/sessions/{sid}/upload", files={"file": ("long.mp4", f, "video/mp4")},
                           data={"transcribe": "false"}).status_code == 200
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    cid = next(c["id"] for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"])
    before = next(c["src"] for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"])
    job = client.post(f"/api/sessions/{sid}/dispatch?wait=0",
                      json={"tool": "upscale", "args": {"clip_id": cid, "factor": 2}}).json()
    seen = 0.0
    for _ in range(600):
        j = client.get(f"/api/jobs/{job['job_id']}").json()
        seen = max(seen, j.get("progress") or 0.0)
        if seen > 0.1 or j["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert 0.0 < seen < 1.0, "the job must report progress while it runs"
    assert client.post(f"/api/jobs/{job['job_id']}/cancel").status_code in (200, 202)
    for _ in range(200):
        j = client.get(f"/api/jobs/{job['job_id']}").json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert j["status"] == "cancelled", j
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert next(c["src"] for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"]) == before


# ---------------------------------------------------------------- RIFE


def test_slow_motion_reports_progress_and_cancels(tmp_path):
    _need("rife")
    src = _clip(tmp_path, "v.mp4", size="160x90", seconds=2.0, audio_seconds=2.0)
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "smooth_slow_motion", {"clip_id": "c1", "factor": 2}, set_progress=prog)
    prog.assert_sane()
    n_src, _ = _frames(src)
    n_out, rate = _frames(store.edl.get_clip("c1")[1].src)
    assert n_out == 2 * n_src and rate == "30000/1001"

    src2 = _clip(tmp_path, "long.mp4", size="160x90", seconds=6.0, audio_seconds=6.0)
    (tmp_path / "b").mkdir()
    store2 = _store(tmp_path / "b", src2)
    prog2, ev = _Progress(), threading.Event()
    _cancel_when(0.15, prog2, ev)
    from video_ai_editor.ai.jobio import ToolCancelled
    with pytest.raises(ToolCancelled):
        dispatch(store2, "smooth_slow_motion", {"clip_id": "c1", "factor": 2},
                 set_progress=prog2, cancel_event=ev)
    assert store2.edl.get_clip("c1")[1].src == str(src2)
    assert not list((store2.dir / "cache" / "rife").glob("rife_work_*"))


# ---------------------------------------------------------------- stabilise


def test_stabilize_reports_progress_and_cancels(tmp_path):
    _need("stabilize")
    # Long enough that ffmpeg's own -progress is sampled inside each pass.
    src = _clip(tmp_path, "v.mp4", size="1280x720", seconds=6.0, audio_seconds=6.0)
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "stabilize", {"clip_id": "c1"}, set_progress=prog)
    prog.assert_sane()
    assert _frames(store.edl.get_clip("c1")[1].src)[0] == _frames(src)[0]

    ev = threading.Event()
    ev.set()                               # cancelled before it starts
    src2 = _clip(tmp_path, "w.mp4", seconds=2.0, audio_seconds=2.0)
    (tmp_path / "b").mkdir()
    store2 = _store(tmp_path / "b", src2)
    from video_ai_editor.ai.jobio import ToolCancelled
    with pytest.raises(ToolCancelled):
        dispatch(store2, "stabilize", {"clip_id": "c1"}, set_progress=_Progress(), cancel_event=ev)
    assert store2.edl.get_clip("c1")[1].src == str(src2)
    assert not list((store2.dir / "cache" / "stabilize").glob("stable_*.mp4"))


# ---------------------------------------------------------------- noise reduce


def test_noise_reduce_reports_progress_and_keeps_every_frame(tmp_path):
    _need("denoise")
    src = _clip(tmp_path, "v.mp4", seconds=2.0, audio_seconds=1.2)
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "noise_reduce", {"clip_id": "c1"}, set_progress=prog)
    prog.assert_sane(min_steps=2)          # extract → per channel → mux
    assert _frames(store.edl.get_clip("c1")[1].src) == _frames(src), \
        "audio ending early must not cut the picture"


# ---------------------------------------------------------------- stubbed models


def _stub_rembg(monkeypatch, delay: float = 0.0):
    from PIL import Image
    calls = {"n": 0}
    fake = types.ModuleType("rembg")

    def remove(img, session=None):
        calls["n"] += 1
        time.sleep(delay)
        out = img.convert("RGBA")
        out.putalpha(255)
        return out
    fake.new_session = lambda model: object()
    fake.remove = remove
    monkeypatch.setitem(sys.modules, "rembg", fake)
    return calls


def test_remove_background_loop_reports_progress_keeps_frames_and_cancels(tmp_path, monkeypatch):
    calls = _stub_rembg(monkeypatch)
    src = _clip(tmp_path, "v.mp4", seconds=1.0, audio_seconds=0.4)
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "remove_background", {"clip_id": "c1"}, set_progress=prog)
    prog.assert_sane()
    n_src, rate = _frames(src)
    assert calls["n"] == n_src
    assert _frames(store.edl.get_clip("c1")[1].src) == (n_src, rate)

    calls = _stub_rembg(monkeypatch, delay=0.05)
    src2 = _clip(tmp_path, "w.mp4", seconds=2.0, audio_seconds=2.0)
    (tmp_path / "b").mkdir()
    store2 = _store(tmp_path / "b", src2)
    prog2, ev = _Progress(), threading.Event()
    _cancel_when(0.3, prog2, ev)
    from video_ai_editor.ai.jobio import ToolCancelled
    with pytest.raises(ToolCancelled):
        dispatch(store2, "remove_background", {"clip_id": "c1"}, set_progress=prog2, cancel_event=ev)
    assert calls["n"] < _frames(src2)[0], "the frame loop must stop at the cancel"
    assert store2.edl.get_clip("c1")[1].src == str(src2)
    assert not list((store2.dir / "cache" / "bgremove").glob("rembg_work_*"))


def _stub_lama(monkeypatch):
    calls = {"n": 0}

    class SimpleLama:
        def __call__(self, img, mask):
            calls["n"] += 1
            return img
    pkg = types.ModuleType("simple_lama_inpainting")
    pkg.SimpleLama = SimpleLama
    utils = types.ModuleType("simple_lama_inpainting.utils")
    utils.download_model = lambda url: "unused"
    monkeypatch.setitem(sys.modules, "simple_lama_inpainting", pkg)
    monkeypatch.setitem(sys.modules, "simple_lama_inpainting.utils", utils)
    monkeypatch.setenv("LAMA_MODEL", "stub.pt")
    import torch

    class _Model:
        def eval(self):
            return self

        def to(self, device):
            return self
    monkeypatch.setattr(torch.jit, "load", lambda path, map_location=None: _Model())
    return calls


def test_object_erase_loop_reports_progress_keeps_frames_and_cancels(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    calls = _stub_lama(monkeypatch)
    src = _clip(tmp_path, "v.mp4", seconds=1.0, audio_seconds=0.4)
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "object_erase", {"clip_id": "c1", "bbox": [0.1, 0.1, 0.2, 0.2]},
             set_progress=prog)
    prog.assert_sane()
    n_src, rate = _frames(src)
    assert calls["n"] == n_src
    assert _frames(store.edl.get_clip("c1")[1].src) == (n_src, rate)

    ev = threading.Event()
    ev.set()
    (tmp_path / "b").mkdir()
    store2 = _store(tmp_path / "b", _clip(tmp_path, "w.mp4"))
    from video_ai_editor.ai.jobio import ToolCancelled
    with pytest.raises(ToolCancelled):
        dispatch(store2, "object_erase", {"clip_id": "c1", "bbox": [0.1, 0.1, 0.2, 0.2]},
                 set_progress=_Progress(), cancel_event=ev)
    assert not list((store2.dir / "cache" / "lama").glob("lama_work_*"))
