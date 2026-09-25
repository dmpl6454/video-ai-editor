"""QA-010: the Media panel is a per-project library, not a view of the timeline.

The bin used to be derived from the clips on the timeline, so deleting the last
clip that used a file removed the file from the bin (for good — also after a
reload), although its bytes were still under uploads/. These tests drive the
real routes with real ffmpeg-made media: import, delete every clip, and the
item must still be listed with its duration, resolution and a used-count of 0.
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


def _lavfi(dst: Path, *args: str) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    return {
        "video": _lavfi(d / "take one.mp4", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=1.5",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        "audio": _lavfi(d / "bed.wav", "-f", "lavfi", "-i", "sine=frequency=220:duration=2"),
    }


def _upload(client, sid, path: Path):
    with path.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/upload", files={"file": (path.name, f, "video/mp4")},
                        data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code == 200, r.text
    return r.json()


def _media(client, sid):
    r = client.get(f"/api/sessions/{sid}/media")
    assert r.status_code == 200, r.text
    return r.json()["media"]


def _dispatch(client, sid, tool, args):
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _v1_ids(client, sid):
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    return [c["id"] for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"]]


def test_deleting_the_last_clip_keeps_the_media_in_the_bin(client, media):
    sid = client.post("/api/sessions").json()["id"]
    _upload(client, sid, media["video"])
    [item] = _media(client, sid)
    assert item["uses"] == 1 and item["kind"] == "video"

    _dispatch(client, sid, "bulk_delete", {"clip_ids": _v1_ids(client, sid)})
    assert _v1_ids(client, sid) == []

    [after] = _media(client, sid)
    assert after["id"] == item["id"]
    assert after["uses"] == 0 and after["clip_ids"] == []
    # The facts the bin shows: the user's own name, duration and resolution.
    assert after["name"] in {"take one.mp4", "take_one.mp4"}
    assert after["duration"] == pytest.approx(1.5, abs=0.1)
    assert (after["width"], after["height"]) == (160, 90)
    # And the src it lists is a real, draggable file the timeline can use again.
    assert Path(after["src"]).is_file()
    _dispatch(client, sid, "add_clip", {"track": "v1", "src": after["src"], "in": 0.0, "out": 1.0, "start": 0.0})
    assert _media(client, sid)[0]["uses"] == 1


def test_used_count_tracks_every_clip_of_a_source(client, media):
    sid = client.post("/api/sessions").json()["id"]
    _upload(client, sid, media["video"])
    [cid] = _v1_ids(client, sid)
    _dispatch(client, sid, "split_at", {"track": "v1", "time": 0.5})
    [item] = _media(client, sid)
    assert item["uses"] == 2 and set(item["clip_ids"]) == set(_v1_ids(client, sid))
    assert cid in item["clip_ids"]


def test_audio_imported_without_the_timeline_is_listed(client, media):
    sid = client.post("/api/sessions").json()["id"]
    with media["audio"].open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/audio_upload", files={"file": ("bed.wav", f, "audio/wav")},
                        data={"add_to_music": "false"})
    assert r.status_code == 200, r.text
    [item] = _media(client, sid)
    assert item["kind"] == "audio" and item["uses"] == 0
    assert item["name"] == "bed.wav"
    assert item["duration"] == pytest.approx(2.0, abs=0.1)


def test_remove_from_bin_is_refused_while_clips_use_it_and_keeps_the_bytes(client, media):
    sid = client.post("/api/sessions").json()["id"]
    _upload(client, sid, media["video"])
    [item] = _media(client, sid)

    r = client.delete(f"/api/sessions/{sid}/media/{item['id']}")
    assert r.status_code == 409

    _dispatch(client, sid, "bulk_delete", {"clip_ids": _v1_ids(client, sid)})
    r = client.delete(f"/api/sessions/{sid}/media/{item['id']}")
    assert r.status_code == 200, r.text
    assert _media(client, sid) == []
    assert Path(item["src"]).is_file(), "a bin removal must never delete the media file"

    # Undo brings the clip back — and with it the item, since the timeline uses it.
    _dispatch(client, sid, "undo", {})
    [back] = _media(client, sid)
    assert back["id"] == item["id"] and back["uses"] == 1


def test_importing_the_same_file_again_after_removal_lists_it_again(client, media):
    sid = client.post("/api/sessions").json()["id"]
    _upload(client, sid, media["video"])
    [item] = _media(client, sid)
    _dispatch(client, sid, "bulk_delete", {"clip_ids": _v1_ids(client, sid)})
    assert client.delete(f"/api/sessions/{sid}/media/{item['id']}").status_code == 200
    _upload(client, sid, media["video"])
    items = _media(client, sid)
    assert len(items) == 1 and items[0]["uses"] == 1


def test_unknown_or_malformed_media_ids(client):
    sid = client.post("/api/sessions").json()["id"]
    assert client.delete(f"/api/sessions/{sid}/media/0123456789ab").status_code == 404
    assert client.delete(f"/api/sessions/{sid}/media/NOT_A_HEX_ID").status_code == 400


def test_an_import_no_clip_ever_used_survives_a_vae_save_and_open(client, media):
    # The video's clip is on the timeline (so there is something to save); the
    # audio was imported without ever touching the timeline, so no EDL — not
    # the live one, not an undo snapshot — references it. Only the library does.
    sid = client.post("/api/sessions").json()["id"]
    _upload(client, sid, media["video"])
    with media["audio"].open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/audio_upload", files={"file": ("bed.wav", f, "audio/wav")},
                        data={"add_to_music": "false"})
    assert r.status_code == 200, r.text

    saved = client.post(f"/api/sessions/{sid}/save_project")
    assert saved.status_code == 200, saved.text
    vae = Path(saved.json()["path"])
    with vae.open("rb") as f:
        opened = client.post("/api/load_project", files={"file": (vae.name, f, "application/zip")})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    new_sid = body["id"] if isinstance(body, dict) else body
    assert new_sid != sid

    items = {it["kind"]: it for it in _media(client, new_sid)}
    assert set(items) == {"video", "audio"}
    assert items["audio"]["uses"] == 0 and items["video"]["uses"] == 1
    assert items["audio"]["duration"] == pytest.approx(2.0, abs=0.1)
    assert Path(items["audio"]["src"]).is_file() and new_sid in items["audio"]["src"]
