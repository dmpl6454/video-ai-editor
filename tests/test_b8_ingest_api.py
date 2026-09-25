"""Wave B lane B8 (ingest / API): imports that used to be dead ends or lossy.

Every test drives the real routes with real ffmpeg-made media and measures the
result from the persisted EDL, the media library and ffprobe.

* QA-091 a valid video with a 235-character name imports (it failed as "may
  not be a valid video" with an orphan copy left behind);
* QA-092 an audio-only .mp4 sent to the video ingress lands on the music lane
  (it was refused with advice that routed it straight back);
* QA-090 a still image becomes a 5 s clip that can be extended (it was one
  frame, 0.033 s, clamped there forever);
* QA-083 a second audio file goes AFTER the first, whole, and the lane's
  ducking choice survives (it stacked at 0 s, was cut to the video length and
  re-enabled ducking the user had turned off).
"""
from __future__ import annotations

import errno
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


@pytest.fixture()
def env(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return _main, TestClient(_main.app)


def _ff(dst: Path, *args: str) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("b8media")
    return {
        "video": _ff(d / "v.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "audio_mp4": _ff(d / "audio_only.mp4", "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
                         "-c:a", "aac"),
        "png": _ff(d / "still.png", "-f", "lavfi", "-i", "testsrc2=size=640x360", "-frames:v", "1"),
        "big_png": _ff(d / "big.png", "-f", "lavfi", "-i", "testsrc2=size=4032x3024", "-frames:v", "1"),
        "song_a": _ff(d / "a_song.wav", "-f", "lavfi", "-i", "sine=frequency=220:duration=6"),
        "song_b": _ff(d / "b_song.wav", "-f", "lavfi", "-i", "sine=frequency=330:duration=6"),
        "narration": _ff(d / "narration.wav", "-f", "lavfi", "-i", "sine=frequency=500:duration=30"),
    }


def _new(client) -> str:
    return client.post("/api/sessions").json()["id"]


def _post(client, sid, route, path: Path, name: str | None = None, mime="video/mp4", **data):
    with path.open("rb") as f:
        return client.post(f"/api/sessions/{sid}/{route}", files={"file": (name or path.name, f, mime)},
                           data={"transcribe": "false", **data})


def _edl(client, sid) -> dict:
    return client.get(f"/api/sessions/{sid}/edl").json()


def _track(edl: dict, tid: str) -> dict | None:
    return next((t for t in edl["tracks"] if t["id"] == tid), None)


def _media(client, sid) -> list[dict]:
    return client.get(f"/api/sessions/{sid}/media").json()["media"]


def _ffprobe(path: str) -> dict:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-count_packets", "-show_entries",
                          "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_read_packets",
                          "-of", "json", path], capture_output=True, text=True, check=True)
    return {s["codec_type"]: s for s in json.loads(out.stdout)["streams"]}


# ----------------------------------------------------------------- QA-091


def test_long_filename_imports_with_its_full_display_name(env, media):
    main, client = env
    sid = _new(client)
    name = "a" * 235 + ".mp4"
    r = _post(client, sid, "upload", media["video"], name)
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == name
    [item] = _media(client, sid)
    assert item["name"] == name
    # Every path component on disk stays inside NAME_MAX.
    uploads = main.session_dir(sid) / "uploads"
    assert all(len(p.name.encode()) <= 255 for p in uploads.rglob("*"))
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    assert Path(clip["src"]).exists()


def test_a_filesystem_refusal_is_named_and_leaves_no_orphan(env, media, monkeypatch):
    main, client = env
    sid = _new(client)

    def refuse(*a, **k):
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(main, "ingest_upload", refuse)
    r = _post(client, sid, "upload", media["video"])
    assert r.status_code == 422, r.text
    details = r.json()["error"]["details"]
    assert details["error"] == "name_too_long"
    assert "name is too long" in details["message"] and "H.264" not in details["message"]
    uploads = main.session_dir(sid) / "uploads"
    assert [p for p in uploads.iterdir()] == [], "the raw copy and its dir must be removed"


# ----------------------------------------------------------------- QA-092


@pytest.mark.parametrize("wait", [1, 0])
def test_audio_only_mp4_on_the_video_ingress_lands_on_the_music_lane(env, media, wait):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", media["video"]).status_code == 200
    r = _post(client, sid, f"upload?wait={wait}", media["audio_mp4"], "पहला गाना.mp4")
    if wait == 0:
        assert r.status_code == 202, r.text
        import time
        for _ in range(200):
            job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
            if job["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)
        assert job["status"] == "completed", job
        body = job["result"]
    else:
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["kind"] == "audio" and body["routed_to"] == "music"
    edl = _edl(client, sid)
    assert len(_track(edl, "v1")["clips"]) == 1, "nothing picture-less may reach v1"
    [music] = _track(edl, "music")["clips"]
    assert music["out"] == pytest.approx(4.0, abs=0.05)
    assert "/uploads/audio/" in music["src"].replace("\\", "/")
    names = {m["name"]: m for m in _media(client, sid)}
    assert names["पहला गाना.mp4"]["kind"] == "audio"


# ----------------------------------------------------------------- QA-090


def test_still_png_becomes_a_five_second_clip_that_can_be_extended(env, media):
    main, client = env
    sid = _new(client)
    r = _post(client, sid, "upload", media["png"], mime="image/png")
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "image"
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    assert clip["out"] - clip["in"] == pytest.approx(5.0)
    # The source is a real CFR video at the project rate, long enough to edit.
    fps = _edl(client, sid)["canvas"]["fps"]
    s = _ffprobe(clip["src"])
    n = int(s["video"]["nb_read_packets"])
    assert n >= 60 * fps
    num, den = (int(x) for x in s["video"]["avg_frame_rate"].split("/"))
    assert num / den == pytest.approx(fps, abs=1e-6)
    assert "audio" in s, "every normalised file carries audio for the mixer"
    # Extend to 12 s like any clip.
    d = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "trim_clip", "args": {"clip_id": clip["id"], "out": 12.0}})
    assert d.status_code == 200, d.text
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    assert clip["out"] == pytest.approx(12.0)
    [item] = _media(client, sid)
    assert item["still"] is True and item["duration"] == pytest.approx(5.0)


def test_still_follows_the_project_rate_and_the_short_side_clamp(env, media):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", media["video"]).status_code == 200
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "set_canvas", "args": {"fps": 25}})
    assert r.status_code == 200, r.text
    r = _post(client, sid, "upload", media["big_png"], mime="image/png")
    assert r.status_code == 200, r.text
    clips = _track(_edl(client, sid), "v1")["clips"]
    still = clips[-1]
    s = _ffprobe(still["src"])["video"]
    assert s["avg_frame_rate"] in ("25/1", "25")
    assert (s["width"], s["height"]) == (1440, 1080)
    assert still["start"] == pytest.approx(3.0)


def test_a_still_after_a_video_exports_for_its_full_length(env, media):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", media["video"]).status_code == 200
    assert _post(client, sid, "upload", media["png"], mime="image/png").status_code == 200
    ex = client.post(f"/api/sessions/{sid}/export", json={"height": 180})
    assert ex.status_code == 200, ex.text
    v = _ffprobe(ex.json()["path"])["video"]
    assert int(v["nb_read_packets"]) == 8 * 30, "3 s of video + a 5 s photo, every frame rendered"


@pytest.mark.skipif(shutil.which("sips") is None, reason="needs macOS sips to make a HEIC")
def test_tiled_heic_photo_imports_at_full_picture(env, media, tmp_path):
    main, client = env
    heic = tmp_path / "IMG_0001.HEIC"
    subprocess.run(["sips", "-s", "format", "heic", str(media["big_png"]), "--out", str(heic)],
                   check=True, capture_output=True)
    sid = _new(client)
    r = _post(client, sid, "upload", heic, mime="image/heic")
    assert r.status_code == 200, r.text
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    s = _ffprobe(clip["src"])["video"]
    # A 4032x3024 photo stored as 512x512 tiles: the whole picture, clamped.
    assert (s["width"], s["height"]) == (1440, 1080)
    assert clip["out"] - clip["in"] == pytest.approx(5.0)


# ----------------------------------------------------------------- QA-083


def test_second_audio_file_goes_after_the_first_and_keeps_the_duck_choice(env, media):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", media["video"]).status_code == 200
    a = _post(client, sid, "audio_upload", media["song_a"], mime="audio/wav")
    assert a.status_code == 200, a.text
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "set_duck", "args": {"track": "music", "enabled": False}})
    assert r.status_code == 200, r.text
    b = _post(client, sid, "audio_upload", media["song_b"], mime="audio/wav")
    assert b.status_code == 200, b.text
    music = _track(_edl(client, sid), "music")
    first, second = music["clips"]
    assert (first["start"], first["out"]) == (pytest.approx(0.0), pytest.approx(6.0, abs=0.02))
    assert second["start"] == pytest.approx(first["start"] + first["out"] - first["in"], abs=0.04)
    assert second["out"] == pytest.approx(6.0, abs=0.02), "never trimmed"
    assert music.get("duck") is None, "the user's 'duck off' must survive a second add"
    assert b.json()["past_video_s"] == pytest.approx(9.0, abs=0.1)


def test_long_narration_is_not_truncated_to_the_video(env, media):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", media["video"]).status_code == 200
    r = _post(client, sid, "audio_upload", media["narration"], mime="audio/wav")
    assert r.status_code == 200, r.text
    [clip] = _track(_edl(client, sid), "music")["clips"]
    assert clip["out"] == pytest.approx(30.0, abs=0.05)
    body = r.json()
    assert body["past_video_s"] == pytest.approx(27.0, abs=0.1)
    assert body["clip_id"] == clip["id"] and body["video_end"] == pytest.approx(3.0, abs=0.05)


def test_chat_add_music_without_start_goes_after_the_lane(env, media):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", media["video"]).status_code == 200
    for p in (media["song_a"], media["song_b"]):
        r = client.post(f"/api/sessions/{sid}/dispatch",
                        json={"tool": "add_music", "args": {"src": str(p), "out": 2.0}})
        assert r.status_code == 200, r.text
    first, second = _track(_edl(client, sid), "music")["clips"]
    assert second["start"] == pytest.approx(first["start"] + 2.0, abs=0.04)


# ----------------------------------------------------------------- QA-089


def _ssim(a: str, b: str) -> float:
    out = subprocess.run([_pu.FFMPEG, "-v", "info", "-i", a, "-i", b, "-frames:v", "10",
                          "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    import re
    return float(re.findall(r"All:([0-9.]+)", out)[-1])


@pytest.fixture(scope="module")
def uhd(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("uhd")
    # One-pixel detail (a cellular automaton) is exactly what a 1080p round
    # trip destroys: upscaled from the proxy it scores ~0.47 SSIM against the
    # original, where smooth test patterns (testsrc2) still score ~0.97 and
    # could not tell the two paths apart.
    return _ff(d / "uhd.mp4", "-f", "lavfi", "-i", "cellauto=s=3840x2160:rule=110:rate=30,format=yuv420p",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-t", "0.5",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-c:a", "aac")


def test_4k_source_exports_4k_from_the_original_not_the_proxy(env, uhd):
    main, client = env
    sid = _new(client)
    r = _post(client, sid, "upload", uhd)
    assert r.status_code == 200, r.text
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    proxy = _ffprobe(clip["src"])["video"]
    assert (proxy["width"], proxy["height"]) == (1920, 1080), "editing stays on the 1080p proxy"
    [item] = _media(client, sid)
    assert (item["width"], item["height"]) == (3840, 2160), "the bin shows the real size"
    ex = client.post(f"/api/sessions/{sid}/export", json={"height": 2160, "bitrate_kbps": 0, "crf": 12})
    assert ex.status_code == 200, ex.text
    out = ex.json()["path"]
    v = _ffprobe(out)["video"]
    assert (v["width"], v["height"]) == (3840, 2160)
    # Measured against the ORIGINAL (see the fixture for why this content).
    # Measured: 0.9995 from the master, 0.48 when the 1080p proxy is upscaled.
    assert _ssim(out, str(uhd)) > 0.95
    # The master is kept for the next export; a 1080p export does not need one.
    masters = list(Path(clip["src"]).parent.glob("*.master*.mp4"))
    assert len(masters) == 1
    # The timeline itself still plays the proxy.
    [clip_after] = _track(_edl(client, sid), "v1")["clips"]
    assert clip_after["src"] == clip["src"]


def test_1080p_export_of_a_4k_source_needs_no_master(env, uhd):
    main, client = env
    sid = _new(client)
    assert _post(client, sid, "upload", uhd).status_code == 200
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    ex = client.post(f"/api/sessions/{sid}/export", json={"height": 1080})
    assert ex.status_code == 200, ex.text
    assert not list(Path(clip["src"]).parent.glob("*.master*.mp4"))


# ----------------------------------------------------------------- QA-045


def test_loose_audio_keeps_its_real_name_in_the_library(env, media):
    main, client = env
    sid = _new(client)
    name = "गाना \U0001F3B5 (final).wav"
    r = _post(client, sid, "audio_upload", media["song_a"], name, mime="audio/wav", add_to_music="false")
    assert r.status_code == 200, r.text
    assert "ग" not in Path(r.json()["src"]).name, "the disk name stays ASCII"
    [item] = _media(client, sid)
    assert item["name"] == name


def test_a_derived_render_is_named_after_the_upload_it_came_from(env, media):
    main, client = env
    sid = _new(client)
    _post(client, sid, "upload", media["video"], "My “best” take.mp4")
    [clip] = _track(_edl(client, sid), "v1")["clips"]
    # What auto_reframe / stabilize / noise_reduce do: render into cache/,
    # record the origin, swap the clip's src.
    from video_ai_editor.agent.media_origin import record_origin
    derived = main.session_dir(sid) / "cache" / "reframe_0123abcd.mp4"
    derived.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(clip["src"], derived)
    record_origin(derived, clip["src"])
    r = client.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "set_property", "args": {"clip_id": clip["id"], "path": "src", "value": str(derived)}})
    assert r.status_code == 200, r.text
    names = sorted(it["name"] for it in _media(client, sid))
    assert names == ["My “best” take.mp4", "My “best” take.mp4 (reframed)"]
