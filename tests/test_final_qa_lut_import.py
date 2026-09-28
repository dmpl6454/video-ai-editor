"""Final QA (frontend-engine): the Effects panel's own .cube import and
"Apply to all clips".

The panel could only apply the six bundled looks to ONE clip; a user's own LUT
and every-clip application were reachable only by typing in the Prompt bar
(which stacked duplicates on a second run). The panel now uploads the .cube
into the session (`/lut_upload`, never a typed path) and applies it with
`apply_lut` — with no `clip_id` for every main-track clip — passing
`replace: true`, so a second application swaps the look instead of stacking a
second LUT under the first.
"""
from __future__ import annotations

import importlib
import io
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_WARM = Path(__file__).resolve().parents[1] / "presets" / "luts" / "warm.cube"


@pytest.fixture()
def api(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


def _clip(path: Path, color: str) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=25:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


def _dispatch(api, sid, tool, **args):
    r = api.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def _two_clip_project(api, tmp_path) -> str:
    sid = api.post("/api/sessions").json()["id"]
    for name, color in (("a.mp4", "red"), ("b.mp4", "blue")):
        with _clip(tmp_path / name, color).open("rb") as fh:
            r = api.post(f"/api/sessions/{sid}/upload", files={"file": (name, fh, "video/mp4")},
                         data={"add_to_timeline": "true", "transcribe": "false"})
        assert r.status_code == 200, r.text
    return sid


def _v1_luts(api, sid) -> list[list[str]]:
    v1 = next(t for t in api.get(f"/api/sessions/{sid}/edl").json()["tracks"] if t["id"] == "v1")
    # an upload's disk name carries a uniqueness suffix: compare the stem's head
    return [[Path(e["params"]["src"]).name.split("_")[0] for e in c.get("effects", []) if e["type"] == "lut"]
            for c in v1["clips"]]


def test_uploaded_cube_applies_to_every_clip_and_replaces_instead_of_stacking(api, tmp_path):
    sid = _two_clip_project(api, tmp_path)
    up = api.post(f"/api/sessions/{sid}/lut_upload",
                  files={"file": ("warm_teal.cube", io.BytesIO(_WARM.read_bytes()), "text/plain")})
    assert up.status_code == 200, up.text
    path = up.json()["path"]
    assert Path(path).exists() and f"/{sid}/" in path.replace("\\", "/")

    _dispatch(api, sid, "apply_lut", src=path, intensity=0.8, replace=True)
    assert _v1_luts(api, sid) == [["warm"], ["warm"]]
    assert Path(path).name.startswith("warm_teal")

    # a second look on every clip swaps it — one LUT per clip, not two
    _dispatch(api, sid, "apply_lut", src="warm.cube", replace=True)
    luts = api.get(f"/api/sessions/{sid}/edl").json()
    srcs = [e["params"]["src"] for t in luts["tracks"] if t["id"] == "v1" for c in t["clips"] for e in c["effects"]]
    assert len(srcs) == 2 and all(Path(x).name == "warm.cube" for x in srcs), srcs


def test_without_replace_apply_lut_keeps_its_old_behaviour(api, tmp_path):
    sid = _two_clip_project(api, tmp_path)
    _dispatch(api, sid, "apply_lut", src="warm.cube")
    # A DIFFERENT look stacks without replace (the SAME one again updates its
    # intensity instead — tests/test_apply_lut_same_src.py).
    _dispatch(api, sid, "apply_lut", src="cool.cube")
    assert all(len(x) == 2 for x in _v1_luts(api, sid))


def test_lut_upload_takes_only_cube_files(api, tmp_path):
    sid = api.post("/api/sessions").json()["id"]
    r = api.post(f"/api/sessions/{sid}/lut_upload",
                 files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 422, r.text
    assert "cube" in r.text.lower()
