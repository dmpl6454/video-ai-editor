"""QA-066 (wave C remainder): Demucs vocal / instrumental isolation reports
progress and stops on Cancel, with identical stems at the segment seams.

It ran as ONE `apply_model` call, so /api/tools honestly advertised
`vocal_isolate` / `instrumental_isolate` as `cancellable=false,
reports_progress=false` and the AI card said "this tool can't be
interrupted". Demucs already separates in overlapping 7.8 s segments with a
linear cross-fade, and takes the executor those segments run on as its
`pool` argument; ai/separate.py now passes a pool that checks the cancel event
and reports progress around every segment. The output is measured against
the old single call: identical everywhere, correlation 1.0 at every seam.

Real htdemucs on this Mac when its checkpoint is already cached — skipped
otherwise, and any download attempt fails the test.
"""
from __future__ import annotations

import random
import subprocess
import threading
import time
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, Clip, empty_edl

HTDEMUCS_CKPT = Path.home() / ".cache" / "torch" / "hub" / "checkpoints" / "955717e8-8726e21a.th"


@pytest.fixture
def demucs_ready(monkeypatch):
    from video_ai_editor.ai import separate
    if not separate.available():
        pytest.skip("demucs / torch / soundfile not installed")
    if not HTDEMUCS_CKPT.exists():
        pytest.skip("htdemucs checkpoint not cached (a test must never download it)")
    import torch.hub

    def _no_download(*a, **k):
        raise AssertionError("a model download was attempted")
    monkeypatch.setattr(torch.hub, "download_url_to_file", _no_download)
    return separate


def _mix(dst: Path, seconds: float = 20.0) -> Path:
    """A 'voice' (a gliding, amplitude-modulated tone) over a 'band' (a bass
    note, a chord and hats), stereo 44.1 kHz."""
    voice = "0.35*sin(2*PI*(300+80*sin(2*PI*0.7*t))*t)*(0.5+0.5*sin(2*PI*3*t))"
    band = "0.25*sin(2*PI*55*t)+0.12*sin(2*PI*220*t)+0.12*sin(2*PI*277*t)+0.05*(random(0)-0.5)*gt(mod(t\\,0.5)\\,0.45)"
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc='{voice}+{band}|{voice}+0.9*({band})':s=44100:d={seconds}",
                    "-c:a", "pcm_s16le", str(dst)], check=True)
    return dst


def _clip_video(tmp: Path, wav: Path) -> Path:
    out = tmp / "talk.mp4"
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:s=160x90:r=30:d=20", "-i", str(wav), "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
    return out


def _store(tmp: Path, src: Path) -> EDLStore:
    d = tmp / "sess"
    d.mkdir()
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=19.9, start=0.0, id="c1"))
    edl.recompute_duration()
    (d / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(d)


def _normalised(separate, wav: Path):
    import soundfile as sf
    import torch
    data, _sr = sf.read(str(wav), dtype="float32", always_2d=True)
    mix = torch.from_numpy(data.T).contiguous()
    ref = mix.mean(0)
    return ((mix - ref.mean()) / (ref.std() + 1e-8))[None]


def _corr(a, b) -> float:
    import torch
    a, b = a.flatten().double(), b.flatten().double()
    a, b = a - a.mean(), b - b.mean()
    return float((a * b).sum() / (a.norm() * b.norm() + 1e-12))


def test_segmented_run_matches_the_single_call_at_every_seam(tmp_path, demucs_ready):
    import torch
    from demucs.apply import apply_model
    separate = demucs_ready
    mix = _normalised(separate, _mix(tmp_path / "mix.wav"))
    model = separate._load_model()

    random.seed(1234)                                   # demucs' random time shift
    with torch.no_grad():
        single = apply_model(model, mix, device="cpu", progress=False)
    seen: list[float] = []
    random.seed(1234)
    chunked = separate._run_model(model, mix, on_progress=seen.append)

    sr = model.samplerate
    seg = int(sr * model.models[0].segment) if hasattr(model, "models") else int(sr * model.segment)
    stride = int(0.75 * seg)
    n = mix.shape[-1]
    assert len(seen) >= 3 and seen == sorted(seen) and seen[-1] == pytest.approx(1.0)
    # Seams: every segment start and end inside the file (± 0.25 s windows).
    half = int(0.25 * sr)
    seams = sorted({p for o in range(0, n, stride) for p in (o, o + seg) if half < p < n - half})
    assert len(seams) >= 4
    for k, name in enumerate(model.sources):
        a, b = single[0, k], chunked[0, k]
        assert _corr(a, b) > 0.99999, name
        for p in seams:
            assert _corr(a[..., p - half:p + half], b[..., p - half:p + half]) > 0.99999, (name, p / sr)
    assert float((single - chunked).abs().max()) < 1e-5


class _Progress:
    def __init__(self):
        self.values: list[float] = []

    def __call__(self, p: float) -> None:
        self.values.append(float(p))


def test_vocal_isolate_reports_progress_per_segment(tmp_path, demucs_ready):
    src = _clip_video(tmp_path, _mix(tmp_path / "mix.wav"))
    store = _store(tmp_path, src)
    prog = _Progress()
    dispatch(store, "vocal_isolate", {"clip_id": "c1"}, set_progress=prog)
    v = prog.values
    assert v and v == sorted(v) and v[-1] == pytest.approx(1.0)
    assert len({round(x, 3) for x in v if 0 < x < 1}) >= 3, v
    assert store.edl.get_clip("c1")[1].audio.mute is True
    assert [c for c in store.edl.get_track("vo").clips]


def test_cancel_lands_within_a_segment_and_writes_no_stems(tmp_path, demucs_ready):
    from video_ai_editor.ai.jobio import ToolCancelled
    src = _clip_video(tmp_path, _mix(tmp_path / "mix.wav"))
    store = _store(tmp_path, src)
    prog, ev = _Progress(), threading.Event()
    at: dict = {}

    def watch():
        for _ in range(6000):
            if prog.values and prog.values[-1] > 0.2:
                at["t"] = time.monotonic()
                ev.set()
                return
            time.sleep(0.01)
    threading.Thread(target=watch, daemon=True).start()
    with pytest.raises(ToolCancelled):
        dispatch(store, "instrumental_isolate", {"clip_id": "c1"}, set_progress=prog, cancel_event=ev)
    assert time.monotonic() - at["t"] < 15.0            # one segment, not the rest of the file
    assert prog.values[-1] < 0.95
    stems = store.dir / "cache" / "stems"
    assert not list(stems.rglob("vocals.wav")) and not list(stems.glob("instrumental_*"))
    assert store.edl.get_clip("c1")[1].audio.mute is False
    assert not store.edl.get_track("music").clips


def test_tools_route_advertises_cancel_and_progress_for_stem_separation():
    from fastapi.testclient import TestClient
    from video_ai_editor import main
    tools = {t["name"]: t for t in TestClient(main.app).get("/api/tools").json()["tools"]}
    for name in ("vocal_isolate", "instrumental_isolate"):
        assert tools[name]["cancellable"] is True, name
        assert tools[name]["reports_progress"] is True, name


def test_vocal_isolate_job_reports_progress_and_cancels_over_http(tmp_path, demucs_ready, monkeypatch):
    """The AI card's path: a 202 job that the user watches and then cancels."""
    from fastapi.testclient import TestClient
    from video_ai_editor import main as _main, storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    client = TestClient(_main.app)
    sid = client.post("/api/sessions").json()["id"]
    src = _clip_video(tmp_path, _mix(tmp_path / "mix.wav"))
    with src.open("rb") as f:
        assert client.post(f"/api/sessions/{sid}/upload", files={"file": ("talk.mp4", f, "video/mp4")},
                           data={"transcribe": "false"}).status_code == 200
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    cid = next(c["id"] for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"])
    job = client.post(f"/api/sessions/{sid}/dispatch?wait=0",
                      json={"tool": "vocal_isolate", "args": {"clip_id": cid}}).json()
    seen = 0.0
    for _ in range(1200):
        j = client.get(f"/api/jobs/{job['job_id']}").json()
        seen = max(seen, j.get("progress") or 0.0)
        if seen > 0.3 or j["status"] not in ("queued", "running"):
            break
        time.sleep(0.02)
    assert 0.0 < seen < 1.0, "the job must report progress while it runs"
    assert client.post(f"/api/jobs/{job['job_id']}/cancel").status_code in (200, 202)
    for _ in range(1000):
        j = client.get(f"/api/jobs/{job['job_id']}").json()
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.02)
    assert j["status"] == "cancelled", j
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert not [c for t in edl["tracks"] if t["id"] == "vo" for c in t["clips"]]
    _main._STORES.clear()
