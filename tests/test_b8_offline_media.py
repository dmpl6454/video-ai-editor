"""QA-095 / QA-096: missing media is flagged, named, previewable and relinkable,
and Save says when a project is not self-contained.

Real routes, real ffmpeg media; the preview claim is measured by decoding
frames of the rendered preview (a slate where the missing clip is, real
footage elsewhere), not by reading a flag.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


def _ff(dst: Path, *args: str) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


def _clip(d: Path, name: str, colour: str, seconds: float = 2.0) -> Path:
    return _ff(d / name, "-f", "lavfi", "-i", f"color=c={colour}:size=320x180:rate=30:duration={seconds}",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("offline_media")
    return {"green": _clip(d, "first.mp4", "0x00c000"), "blue": _clip(d, "second.mp4", "0x0000d0"),
            "yellow": _clip(d, "replacement.mp4", "0xe0e000"),
            "short": _clip(d, "short.mp4", "0xe0e000", 0.5)}


def _client(monkeypatch, workdir: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", workdir)
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", workdir)
    _main._STORES.clear()
    return _main, TestClient(_main.app)


@pytest.fixture()
def env(monkeypatch, tmp_path: Path):
    return _client(monkeypatch, tmp_path / "wd")


def _upload(client, sid, path: Path, name: str | None = None):
    with path.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/upload", files={"file": (name or path.name, f, "video/mp4")},
                        data={"transcribe": "false"})
    assert r.status_code == 200, r.text
    return r.json()


def _v1(client, sid) -> list[dict]:
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    return next(t for t in edl["tracks"] if t["id"] == "v1")["clips"]


def _two_clip_project(client, media) -> tuple[str, list[dict]]:
    sid = client.post("/api/sessions").json()["id"]
    _upload(client, sid, media["green"])
    _upload(client, sid, media["blue"])
    return sid, _v1(client, sid)


def _mean_rgb(video: Path, t: float) -> tuple[float, float, float]:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                          "-vf", "scale=32:18", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    n = len(raw) // 3
    return tuple(sum(raw[i::3]) / n for i in range(3))  # type: ignore[return-value]


def _hide(src: str) -> Path:
    p = Path(src)
    moved = p.with_name(p.name + ".moved")
    p.rename(moved)
    return moved


# ----------------------------------------------------------------- QA-095


def test_missing_media_is_listed_offline_with_its_real_name(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    _hide(clips[1]["src"])
    items = client.get(f"/api/sessions/{sid}/media").json()["media"]
    by_name = {it["name"]: it for it in items}
    assert by_name["second.mp4"]["missing"] is True
    assert by_name["second.mp4"]["clip_ids"] == [clips[1]["id"]]
    assert by_name["first.mp4"]["missing"] is False


def test_preview_slates_the_missing_clip_and_never_serves_the_stale_render(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    before = client.post(f"/api/sessions/{sid}/preview")
    assert before.status_code == 200, before.text
    _hide(clips[1]["src"])
    after = client.post(f"/api/sessions/{sid}/preview")
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["edl_hash"] != before.json()["edl_hash"], "the stale render was served"
    assert body["cached"] is False
    video = Path(body["path"])
    r, g, b = _mean_rgb(video, 0.5)          # first clip: still real green footage
    assert g > 100 and r < 60 and b < 60, (r, g, b)
    r, g, b = _mean_rgb(video, 3.0)          # second clip: the dark-red offline slate
    assert r > g and r > b and max(r, g, b) < 110, (r, g, b)
    # GET (what the <video> element loads) agrees with the offline state.
    got = client.get(f"/api/sessions/{sid}/preview.mp4")
    assert got.status_code == 200
    assert got.content == video.read_bytes()


def test_export_with_missing_media_names_the_file(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    _hide(clips[1]["src"])
    r = client.post(f"/api/sessions/{sid}/export")
    assert r.status_code == 422, r.text
    d = r.json()["error"]["details"]
    assert d["error"] == "media_missing"
    assert "second.mp4" in d["message"] and "Relink" in d["message"]
    assert [m["clip_ids"] for m in d["missing"]] == [[clips[1]["id"]]]


def test_relink_repoints_every_clip_in_one_undoable_step(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    _hide(clips[1]["src"])
    [item] = [it for it in client.get(f"/api/sessions/{sid}/media").json()["media"] if it["missing"]]
    with media["yellow"].open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/media/{item['id']}/relink",
                        files={"file": ("replacement.mp4", f, "video/mp4")})
    assert r.status_code == 200, r.text
    assert r.json()["relinked"] == [clips[1]["id"]]
    now = _v1(client, sid)
    assert Path(now[1]["src"]).is_file() and now[1]["src"] != clips[1]["src"]
    assert not any(it["missing"] for it in client.get(f"/api/sessions/{sid}/media").json()["media"])
    prev = client.post(f"/api/sessions/{sid}/preview")
    assert prev.status_code == 200, prev.text
    r2, g2, b2 = _mean_rgb(Path(prev.json()["path"]), 3.0)
    assert r2 > 150 and g2 > 150 and b2 < 80, (r2, g2, b2)   # the yellow replacement plays
    # One undo restores the old link.
    u = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "undo", "args": {}})
    assert u.status_code == 200, u.text
    assert _v1(client, sid)[1]["src"] == clips[1]["src"]


def test_relink_refuses_a_replacement_too_short_for_the_clip(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    _hide(clips[1]["src"])
    [item] = [it for it in client.get(f"/api/sessions/{sid}/media").json()["media"] if it["missing"]]
    with media["short"].open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/media/{item['id']}/relink",
                        files={"file": ("short.mp4", f, "video/mp4")})
    assert r.status_code == 422, r.text
    assert "long" in r.json()["error"]["details"]["message"]
    assert _v1(client, sid)[1]["src"] == clips[1]["src"]


# ----------------------------------------------------------------- QA-096


def test_save_with_missing_media_warns_and_names_it(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    _hide(clips[1]["src"])
    r = client.post(f"/api/sessions/{sid}/save_project")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["name"] for m in body["missing"]] == ["second.mp4"]
    assert "second.mp4" in body["warning"] and "not in the project" in body["warning"]
    with zipfile.ZipFile(body["path"]) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert [m["name"] for m in manifest["missing"]] == ["second.mp4"]


def test_reopened_project_flags_what_was_not_bundled(env, media):
    main, client = env
    sid, clips = _two_clip_project(client, media)
    _hide(clips[1]["src"])
    vae = Path(client.post(f"/api/sessions/{sid}/save_project").json()["path"])
    with vae.open("rb") as f:
        opened = client.post("/api/load_project", files={"file": (vae.name, f, "application/zip")})
    assert opened.status_code == 200, opened.text
    new_sid = opened.json()["id"]
    now = _v1(client, new_sid)
    assert new_sid in now[0]["src"], "bundled media must point into the new session"
    items = {it["name"]: it for it in client.get(f"/api/sessions/{new_sid}/media").json()["media"]}
    assert items["second.mp4"]["missing"] is True
    assert client.post(f"/api/sessions/{new_sid}/export").status_code == 422


def test_non_ascii_workdir_round_trip_is_self_contained(monkeypatch, tmp_path, media):
    main, client = _client(monkeypatch, tmp_path / "wd_josé")
    sid, clips = _two_clip_project(client, media)
    vae = Path(client.post(f"/api/sessions/{sid}/save_project").json()["path"])
    with vae.open("rb") as f:
        new_sid = client.post("/api/load_project", files={"file": (vae.name, f, "application/zip")}).json()["id"]
    assert all(new_sid in c["src"] for c in _v1(client, new_sid))
