"""Ingest fidelity and project safety (0.7.3 wave A, lane A2).

Every test here drives real code on real media synthesised with ffmpeg lavfi:
the FastAPI routes through TestClient, `normalize()`/`ingest_upload()` on
actual files, and renders/exports measured with ffprobe and decoded pixels.

  QA-001  colliding upload names overwrote each other's media on disk
  QA-007  normalisation froze the event loop for the whole import
  QA-008  portrait sources were shrunk to 1080 px TALL (608x1080)
  QA-009  every source was forced to integer 30 fps; Canvas.fps was an int
  QA-042  HDR PQ sources were neither tone-mapped nor retagged
"""
from __future__ import annotations

import importlib
import io
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


def _colour_clip(path: Path, colour: str, seconds: float, *, size: str = "320x240",
                 rate: str = "30", tone: int = 440) -> Path:
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={size}:r={rate}:d={seconds}",
          "-f", "lavfi", "-i", f"sine=f={tone}:d={seconds}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])
    return path


def _counter_clip(path: Path, rate: str, frames: int, size: str = "320x240") -> Path:
    """A clip whose every frame is visibly different (testsrc2 draws a moving
    pattern and a frame counter), so duplicated frames are detectable."""
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r={rate}",
          "-f", "lavfi", "-i", "sine=f=440",
          "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])
    return path


def _stream(path: Path, entries: str) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                          "-count_frames", "-show_entries", f"stream={entries}",
                          "-of", "json", str(path)],
                         check=True, capture_output=True, text=True)
    return json.loads(out.stdout)["streams"][0]


def _duration(path: Path) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def _pixel(path: Path, t: float) -> tuple[int, int, int]:
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t}", "-i", str(path),
                          "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return raw[0], raw[1], raw[2]


def _frame_hashes(path: Path) -> list[str]:
    out = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0",
                          "-f", "framemd5", "-"], check=True, capture_output=True, text=True)
    return [ln.rsplit(",", 1)[-1].strip() for ln in out.stdout.splitlines()
            if ln and not ln.startswith("#")]


@pytest.fixture()
def api(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    client = TestClient(_main.app)
    client.main = _main          # type: ignore[attr-defined]
    return client


def _session(client: TestClient) -> str:
    return client.post("/api/sessions").json()["id"]


def _upload(client: TestClient, sid: str, path: Path, name: str, **params) -> dict:
    with path.open("rb") as fh:
        r = client.post(f"/api/sessions/{sid}/upload", params=params,
                        files={"file": (name, fh, "video/mp4")},
                        data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code in (200, 202), r.text
    return r.json()


def _v1(client: TestClient, sid: str) -> list[dict]:
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    return next(t for t in edl["tracks"] if t["id"] == "v1")["clips"]


# --------------------------------------------------------------------------
# QA-001 — uploads never collide
# --------------------------------------------------------------------------

def test_two_uploads_whose_names_sanitise_alike_keep_their_own_media(api, tmp_path):
    red = _colour_clip(tmp_path / "r.mp4", "red", 2)
    blue = _colour_clip(tmp_path / "b.mp4", "blue", 3)
    sid = _session(api)
    a = _upload(api, sid, red, "clip (1).mp4")
    b = _upload(api, sid, blue, "clip [1].mp4")

    assert a["normalized"] != b["normalized"]
    assert a["src"] != b["src"]
    clips = _v1(api, sid)
    assert len(clips) == 2 and clips[0]["src"] != clips[1]["src"]
    # The FIRST clip still plays the red footage at its own length.
    first = Path(clips[0]["src"])
    assert abs(_duration(first) - 2.0) < 0.2
    r, g, bl = _pixel(first, 1.0)
    assert r > 200 and bl < 60, (r, g, bl)
    r, g, bl = _pixel(Path(clips[1]["src"]), 1.0)
    assert bl > 200 and r < 60, (r, g, bl)
    assert a["display_name"] == "clip (1).mp4"
    assert b["display_name"] == "clip [1].mp4"


def test_browser_escaped_quotes_are_decoded_before_sanitising(api, tmp_path):
    """Browsers send `"` in a multipart filename as %22; undecoded, every
    quoted name became `22_22.mp4`."""
    red = _colour_clip(tmp_path / "r.mp4", "red", 1)
    blue = _colour_clip(tmp_path / "b.mp4", "blue", 1)
    sid = _session(api)
    a = _upload(api, sid, red, "%22पहला वीडियो%22.mp4")
    b = _upload(api, sid, blue, "%22दूसरा वीडियो%22.mp4")
    assert a["display_name"] == '"पहला वीडियो".mp4'
    assert b["display_name"] == '"दूसरा वीडियो".mp4'
    assert "22" not in Path(a["normalized"]).name
    assert Path(a["normalized"]).parent != Path(b["normalized"]).parent
    ingest = json.loads((Path(a["normalized"]).parent / "ingest.json").read_text(encoding="utf-8"))
    assert ingest["display_name"] == '"पहला वीडियो".mp4'


def test_same_name_music_uploads_do_not_replace_each_other(api, tmp_path):
    sid = _session(api)
    srcs = []
    for tone in (3000, 440):
        wav = tmp_path / f"t{tone}.wav"
        _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=f={tone}:d=2", str(wav)])
        with wav.open("rb") as fh:
            r = api.post(f"/api/sessions/{sid}/audio_upload",
                         files={"file": ("song.wav", fh, "audio/wav")},
                         data={"add_to_music": "true"})
        assert r.status_code == 200, r.text
        srcs.append(r.json()["src"])
    assert srcs[0] != srcs[1]
    assert Path(srcs[0]).read_bytes() == (tmp_path / "t3000.wav").read_bytes()
    assert Path(srcs[1]).read_bytes() == (tmp_path / "t440.wav").read_bytes()


def test_two_voiceover_takes_in_the_same_second_are_two_files(api, tmp_path, monkeypatch):
    sid = _session(api)
    wav = tmp_path / "take.wav"
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=f=500:d=1", str(wav)])
    monkeypatch.setattr(api.main.time, "time", lambda: 1_790_344_487.0)
    srcs = []
    for start in (0.0, 2.0):
        with wav.open("rb") as fh:
            r = api.post(f"/api/sessions/{sid}/vo_record",
                         files={"file": ("vo.webm", fh, "audio/webm")},
                         data={"start": str(start)})
        assert r.status_code == 200, r.text
        srcs.append(r.json()["src"])
    assert srcs[0] != srcs[1]
    assert Path(srcs[0]).exists() and Path(srcs[1]).exists()


def test_same_name_stickers_and_subtitles_get_their_own_files(api, tmp_path):
    sid = _session(api)
    got = []
    for body in (b"\x89PNG\r\n\x1a\nAAAA", b"\x89PNG\r\n\x1a\nBBBB"):
        r = api.post(f"/api/sessions/{sid}/sticker_upload",
                     files={"file": ("smile.png", io.BytesIO(body), "image/png")})
        assert r.status_code == 200, r.text
        got.append((r.json()["src"], body))
    for body in (b"1\n00:00:00,000 --> 00:00:01,000\nA\n", b"1\n00:00:00,000 --> 00:00:01,000\nB\n"):
        r = api.post(f"/api/sessions/{sid}/subtitle_upload",
                     files={"file": ("subs.srt", io.BytesIO(body), "text/plain")})
        assert r.status_code == 200, r.text
        got.append((r.json()["path"], body))
    assert len({p for p, _ in got}) == 4
    for p, body in got:
        assert Path(p).read_bytes() == body


def test_chunk_cache_is_keyed_on_file_identity_not_path(tmp_path):
    """A file replaced in place must not be served from a stale chunk: render a
    red clip through the chunk path, overwrite the file with blue at the SAME
    path, change nothing in the clip, and render again."""
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import EDL, Canvas, Clip, Marker, Track
    from video_ai_editor.render import render_preview
    from video_ai_editor.render.chunks import fingerprint_clip

    media = tmp_path / "clip.mp4"
    _colour_clip(media, "red", 2)
    clip = Clip(src=str(media), in_=0, out=2, start=0, id="c1")
    fp_red = fingerprint_clip(clip, canvas_w=320, canvas_h=240, fps=30, encoder_args=[])

    def _edl(marker: str) -> EDL:
        e = EDL(canvas=Canvas(w=320, h=240, fps=30),
                tracks=[Track(id="v1", type="video", clips=[clip.model_copy(deep=True)])],
                markers=[Marker(time=0.5, label=marker)])
        e.recompute_duration()
        return e

    sess = tmp_path / "s"
    sess.mkdir()
    (sess / "edl.json").write_text(_edl("a").to_json())
    first = render_preview(EDLStore(sess).edl, sess, height=240)
    assert _pixel(first.path, 1.0)[0] > 200

    time.sleep(0.02)
    _colour_clip(media, "blue", 2)
    assert fingerprint_clip(clip, canvas_w=320, canvas_h=240, fps=30, encoder_args=[]) != fp_red
    # A different marker changes the EDL hash (so the whole-preview cache
    # misses) but not the clip — only the chunk key can tell the file changed.
    second = render_preview(_edl("b"), sess, height=240)
    r, g, b = _pixel(second.path, 1.0)
    assert b > 200 and r < 60, (r, g, b)


# --------------------------------------------------------------------------
# QA-007 — the upload does not freeze the backend
# --------------------------------------------------------------------------

@pytest.fixture()
def live_server(tmp_path):
    """A REAL uvicorn process. TestClient cannot prove an event-loop stall:
    it gives every request its own portal and therefore its own loop, so a
    route that blocks its loop never delays a concurrent request there."""
    import os
    import socket
    import sys
    import urllib.request
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, WORKDIR=str(tmp_path / "wd"), ANTHROPIC_API_KEY="",
               HUGGINGFACE_TOKEN="", PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "video_ai_editor.main:app",
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("uvicorn did not start")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _upload_while_polling_version(live_server, src: Path) -> tuple[dict, list[float]]:
    """Upload `src` synchronously while polling /api/version every 0.2 s."""
    import httpx
    sid = httpx.post(live_server + "/api/sessions").json()["id"]
    done = threading.Event()
    result: dict = {}

    def _do_upload():
        t0 = time.monotonic()
        with src.open("rb") as fh:
            r = httpx.post(f"{live_server}/api/sessions/{sid}/upload",
                           files={"file": (src.name, fh, "video/mp4")},
                           data={"add_to_timeline": "true", "transcribe": "false"},
                           timeout=300)
        result["status"] = r.status_code
        result["seconds"] = time.monotonic() - t0
        done.set()

    th = threading.Thread(target=_do_upload)
    th.start()
    latencies = []
    time.sleep(0.5)
    while not done.is_set():
        t0 = time.monotonic()
        assert httpx.get(live_server + "/api/version", timeout=300).status_code == 200
        latencies.append(time.monotonic() - t0)
        time.sleep(0.2)
    th.join()
    return result, latencies


def test_version_stays_fast_while_a_long_import_normalises(live_server, tmp_path):
    # The import must take long enough (> 2 s) for the latency samples to mean
    # something, on an idle machine too: a 40 s 1080p fixture normalised in
    # 1.9 s with nothing else running. Double the fixture until it is long
    # enough rather than assuming how fast this machine is.
    result, latencies = {}, []
    for dur in (40, 80, 160, 320):
        src = tmp_path / f"long_{dur}.mp4"
        _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={dur}",
              "-f", "lavfi", "-i", f"sine=f=440:d={dur}", "-c:v", "libx264", "-preset",
              "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)])
        result, latencies = _upload_while_polling_version(live_server, src)
        src.unlink(missing_ok=True)
        assert result["status"] == 200
        if result["seconds"] > 2.0:
            break
    assert result["seconds"] > 2.0, "fixture too small to prove anything"
    assert len(latencies) >= 5, (latencies, result)
    assert max(latencies) < 1.0, (max(latencies), result["seconds"])


def test_wait0_upload_is_a_job_with_progress(api, tmp_path):
    src = _counter_clip(tmp_path / "c.mp4", "25", 100)
    sid = _session(api)
    body = _upload(api, sid, src, "c.mp4", wait=0)
    job_id = body["job_id"]
    seen = []
    for _ in range(300):
        job = api.get(f"/api/jobs/{job_id}").json()
        seen.append(job["progress"])
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job
    assert job["result"]["normalized"].endswith(".normalized.mp4")
    assert max(seen) > 0.0
    assert len(_v1(api, sid)) == 1


# --------------------------------------------------------------------------
# QA-008 — the clamp tests the SHORT side
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size,expect", [
    ("1080x1920", (1080, 1920)),      # portrait 1080p: untouched
    ("720x1280", (720, 1280)),        # sub-1080 portrait: untouched
    ("3840x2160", (1920, 1080)),      # 4K landscape: short side -> 1080
    ("2160x3840", (1080, 1920)),      # 4K portrait: short side -> 1080
])
def test_ingest_clamps_on_the_short_side(tmp_path, size, expect):
    from video_ai_editor.ingest.pipeline import ingest_upload
    src = tmp_path / "in.mp4"
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d=0.5",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)])
    res = ingest_upload(src, tmp_path / "out", transcribe_audio=False)
    v = res.probe.video
    assert (v.width, v.height) == expect


def test_rotated_phone_clip_is_judged_by_its_displayed_shape(tmp_path):
    """Stored 1920x1080 with a 90° rotation tag = displayed 1080x1920."""
    from video_ai_editor.ingest.pipeline import ingest_upload
    coded = tmp_path / "coded.mp4"
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=0.5",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(coded)])
    src = tmp_path / "rot.mp4"
    _run([FFMPEG, "-y", "-display_rotation", "90", "-i", str(coded), "-c", "copy", str(src)])
    res = ingest_upload(src, tmp_path / "out", transcribe_audio=False)
    v = res.probe.video
    assert (v.width, v.height) == (1080, 1920)


# --------------------------------------------------------------------------
# QA-009 — the source's own frame rate is the project timebase
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rate,frames,expect", [
    ("25", 50, "25/1"),
    ("24000/1001", 48, "24000/1001"),
    ("60000/1001", 120, "60000/1001"),
    ("24", 48, "24/1"),
])
def test_normalize_keeps_the_source_rate_and_every_frame(tmp_path, rate, frames, expect):
    from video_ai_editor.ingest.normalize import normalize
    src = _counter_clip(tmp_path / "in.mp4", rate, frames)
    dst = tmp_path / "out.mp4"
    normalize(src, dst)
    s = _stream(dst, "r_frame_rate,nb_read_frames")
    assert s["r_frame_rate"] == expect
    assert int(s["nb_read_frames"]) == frames
    hashes = _frame_hashes(dst)
    assert len(set(hashes)) == len(hashes), "normalisation duplicated frames"


def test_vfr_source_is_conformed_to_its_standard_rate(tmp_path):
    """A 25 fps camera clip with dropped frames (VFR, 21.6 fps average) is a
    25 fps clip, not a 30 fps one."""
    from video_ai_editor.ingest.normalize import normalize
    base = _counter_clip(tmp_path / "base.mp4", "25", 75)
    vfr = tmp_path / "vfr.mp4"
    # Drop every 7th frame and keep the timestamps: irregular spacing.
    _run([FFMPEG, "-y", "-i", str(base), "-vf", "select='not(eq(mod(n\\,7)\\,0))'",
          "-fps_mode", "vfr", "-an", "-c:v", "libx264", "-preset", "ultrafast", str(vfr)])
    assert _stream(vfr, "avg_frame_rate")["avg_frame_rate"] != "25/1", "fixture must be VFR"
    dst = tmp_path / "out.mp4"
    normalize(vfr, dst)
    s = _stream(dst, "r_frame_rate,nb_read_frames")
    assert s["r_frame_rate"] == "25/1"
    assert abs(int(s["nb_read_frames"]) - 75) <= 1


def test_first_upload_sets_the_project_fps_and_export_keeps_it(api, tmp_path):
    src = _counter_clip(tmp_path / "pal.mp4", "25", 50)
    sid = _session(api)
    _upload(api, sid, src, "pal.mp4")
    edl = api.get(f"/api/sessions/{sid}/edl").json()
    assert edl["canvas"]["fps"] == 25
    r = api.post(f"/api/sessions/{sid}/export")
    assert r.status_code == 200, r.text
    out = Path(r.json()["path"])
    s = _stream(out, "r_frame_rate,nb_read_frames")
    assert s["r_frame_rate"] == "25/1"
    assert int(s["nb_read_frames"]) == 50
    hashes = _frame_hashes(out)
    assert len(set(hashes)) == len(hashes), "export repeated frames (pulldown judder)"


def test_a_user_chosen_fps_is_not_overridden_by_the_first_upload(api, tmp_path):
    src = _counter_clip(tmp_path / "pal.mp4", "25", 25)
    sid = _session(api)
    r = api.post(f"/api/sessions/{sid}/dispatch", json={"tool": "set_canvas", "args": {"fps": 29.97}})
    assert r.status_code == 200, r.text
    _upload(api, sid, src, "pal.mp4")
    fps = api.get(f"/api/sessions/{sid}/edl").json()["canvas"]["fps"]
    assert abs(fps - 30000 / 1001) < 1e-9


def test_export_at_ntsc_rate_uses_the_exact_rational(api, tmp_path):
    src = _counter_clip(tmp_path / "ntsc.mp4", "30000/1001", 60)
    sid = _session(api)
    _upload(api, sid, src, "ntsc.mp4")
    assert abs(api.get(f"/api/sessions/{sid}/edl").json()["canvas"]["fps"] - 30000 / 1001) < 1e-9
    r = api.post(f"/api/sessions/{sid}/export")
    assert r.status_code == 200, r.text
    s = _stream(Path(r.json()["path"]), "r_frame_rate,nb_read_frames")
    assert s["r_frame_rate"] == "30000/1001"
    assert int(s["nb_read_frames"]) == 60


def test_canvas_fps_is_a_float_and_old_int_edls_hash_unchanged():
    from video_ai_editor.edl.schema import EDL, Canvas
    old = EDL.model_validate_json('{"canvas":{"w":1080,"h":1920,"fps":30},"tracks":[]}')
    assert old.canvas.fps == 30 and '"fps":30,' in old.to_json()
    assert Canvas(fps=29.97).fps == pytest.approx(30000 / 1001)
    assert Canvas(fps="30000/1001").fps == pytest.approx(30000 / 1001)
    assert Canvas(fps=24).fps == 24          # an exact 24 must not snap to 23.976
    assert Canvas(fps=500).fps == 240
    with pytest.raises(Exception):
        Canvas(fps=float("nan"))


def test_source_rate_rules():
    from fractions import Fraction
    from video_ai_editor.edl.timebase import source_rate, rate_of
    assert source_rate("25/1", "25/1") == 25
    assert source_rate("30000/1001", "30000/1001") == Fraction(30000, 1001)
    # VFR phone clip: nominal 30, average a hair under -> 30, not 29.97
    assert source_rate("2998/100", "30/1") == 30
    # constant high-frame-rate slow motion keeps its frames
    assert source_rate("120/1", "120/1") == 120
    # irregular average, no usable nominal -> nearest standard
    assert source_rate("257/10", "90000/1") == 25
    assert source_rate(None, None) == 30
    assert source_rate("0/0", None) == 30
    assert rate_of(24) == 24 and rate_of(24.0) == 24


# --------------------------------------------------------------------------
# QA-042 — HDR PQ/HLG is tone-mapped to BT.709 and tagged as such
# --------------------------------------------------------------------------

def _pq_code(nits: float) -> float:
    m1, m2 = 2610 / 16384, 2523 / 4096 * 128
    c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
    y = (nits / 10000) ** m1
    return ((c1 + c2 * y) / (1 + c3 * y)) ** m2


def _hdr_grey_clip(path: Path, code: float, transfer: str) -> Path:
    """A 10-bit BT.2020 clip whose every pixel is the grey `code` (0..1 of the
    encoded signal), tagged with `transfer`. Written as raw yuv420p10le
    (limited range, so Y = 64 + 876*code, chroma neutral)."""
    import numpy as np
    w, h, n = 64, 64, 10
    y = int(round(64 + 876 * code))
    frame = np.concatenate([np.full(w * h, y, np.uint16),
                            np.full(w * h // 2, 512, np.uint16)]).tobytes()
    raw = path.with_suffix(".yuv")
    raw.write_bytes(frame * n)
    _run([FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{w}x{h}",
          "-r", "25", "-i", str(raw), "-c:v", "libx265", "-x265-params",
          f"log-level=error:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc",
          "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", str(path)])
    return path


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_hdr_source_is_tonemapped_and_tagged_bt709(tmp_path, transfer):
    from video_ai_editor.ingest.pipeline import ingest_upload
    # PQ: HDR reference white (203 nits). HLG: its 75 % reference-white signal.
    code = _pq_code(203) if transfer == "smpte2084" else 0.75
    src = _hdr_grey_clip(tmp_path / "hdr.mp4", code, transfer)
    tags = _stream(src, "color_transfer,color_primaries")
    assert tags["color_transfer"] == transfer, "fixture must be tagged HDR"

    res = ingest_upload(src, tmp_path / "out", transcribe_audio=False)
    out = Path(res.normalized)
    s = _stream(out, "color_transfer,color_primaries,color_space,pix_fmt")
    assert (s["color_transfer"], s["color_primaries"], s["color_space"]) == ("bt709", "bt709", "bt709")
    assert s["pix_fmt"] == "yuv420p"
    assert res.color == "tonemapped" and res.notices == []
    # A pass-through leaves reference white at the PQ code value (~58 %,
    # ~150/255); tone-mapped it must sit near SDR white.
    r, g, b = _pixel(out, 0.1)
    passthrough = 255 * code
    assert r > passthrough + 40 and r > 215, (r, g, b, passthrough)
    assert max(r, g, b) - min(r, g, b) <= 6, "neutral grey must stay neutral"


def test_tone_curve_is_monotonic_and_keeps_shadows():
    import numpy as np
    from video_ai_editor.ingest.hdr import hdr_to_sdr_rgb
    nits = np.array([0.0, 1, 10, 50, 100, 203, 500, 1000, 4000])
    codes = np.array([_pq_code(n) if n else 0.0 for n in nits])
    out = hdr_to_sdr_rgb(np.stack([codes] * 3, axis=-1), "smpte2084")[:, 0]
    assert np.all(np.diff(out) >= -1e-9)
    assert out[0] == 0.0 and out[-2] >= 0.99
    assert 0.9 < out[5] < 0.95       # 203 nits -> SDR white region
