"""Final QA (0.8.0 LUT import): a 1D or malformed .cube was accepted by
`/lut_upload` (it only checked the extension) and by `apply_lut`, then EVERY
preview and export failed with "a clip may have corrupt frames or an unusual
codec" — the clip was fine; the LUT was the problem (ffmpeg: "3D LUT is empty",
"Unexpected EOF", or just "Error initializing filters").

Now the .cube is parsed the way ffmpeg's lut3d parses it (`render/lut_cube.py`)
at import and again in `apply_lut`, and a render that fails on a LUT already in
a project (saved before this guard, or edited on disk) names the look instead
of blaming the clip.
"""
from __future__ import annotations

import importlib
import io
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.render.lut_cube import InvalidLut, validate_cube

_LUTS = Path(__file__).resolve().parents[1] / "presets" / "luts"


def _rows(n: int) -> str:
    return "\n".join(f"{i / (n - 1):.4f} {j / (n - 1):.4f} {k / (n - 1):.4f}"
                     for k in range(n) for j in range(n) for i in range(n))


GOOD = "LUT_3D_SIZE 2\n" + _rows(2) + "\n"
ONE_D = "LUT_1D_SIZE 2\n0 0 0\n1 1 1\n"
TRUNCATED = "LUT_3D_SIZE 4\n0 0 0\n1 1 1\n"
TOO_BIG = "LUT_3D_SIZE 300\n"

# name -> (body, ffmpeg renders it). Every body ffmpeg REJECTS must be rejected
# at import; every body we ACCEPT must render. (We are stricter than ffmpeg on
# purpose for a few it tolerates but renders wrongly, e.g. a 1D shaper in front
# of the 3D table, whose rows ffmpeg reads as 3D rows.)
CASES = {
    "good": (GOOD, True),
    "title_comment_blank": ('# made by hand\nTITLE "x"\nLUT_3D_SIZE 2\n\n# mid\n' + _rows(2) + "\n", True),
    "domain_lines": ("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n" + _rows(2) + "\n", True),
    "input_range_before_size": ("LUT_3D_INPUT_RANGE 0.0 1.0\nLUT_3D_SIZE 2\n" + _rows(2) + "\n", True),
    "crlf": (GOOD.replace("\n", "\r\n"), True),
    "inline_comment": ("LUT_3D_SIZE 2\n" + "\n".join(r + " # c" for r in _rows(2).split("\n")) + "\n", True),
    "exponent": ("LUT_3D_SIZE 2\n1e-1 0 0\n" + "\n".join(_rows(2).split("\n")[1:]) + "\n", True),
    "one_d": (ONE_D, False),
    "truncated": (TRUNCATED, False),
    "too_big": (TOO_BIG, False),
    "size_one": ("LUT_3D_SIZE 1\n0 0 0\n", False),
    "empty": ("", False),
    "input_range_after_size": ("LUT_3D_SIZE 2\nLUT_3D_INPUT_RANGE 0.0 1.0\n" + _rows(2) + "\n", False),
    "bom": ("﻿" + GOOD, False),
    "indented_header": ("  " + GOOD, False),
    "two_numbers": ("LUT_3D_SIZE 2\n0 0\n" + "\n".join(_rows(2).split("\n")[1:]) + "\n", False),
    "text_row": ("LUT_3D_SIZE 2\nfoo bar baz\n" + "\n".join(_rows(2).split("\n")[1:]) + "\n", False),
    "bad_domain": ("LUT_3D_SIZE 2\nDOMAIN_FOO 0 0 0\n" + _rows(2) + "\n", False),
}


def _ffmpeg_renders(path: Path) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:d=0.04",
                        "-vf", f"lut3d=file={path.name}", "-f", "null", "-"],
                       cwd=path.parent, capture_output=True, text=True)
    return r.returncode == 0


@pytest.mark.parametrize("name", sorted(CASES))
def test_validator_agrees_with_ffmpeg(tmp_path: Path, name: str):
    body, renders = CASES[name]
    p = tmp_path / f"{name}.cube"
    p.write_bytes(body.encode("utf-8"))
    assert _ffmpeg_renders(p) is renders, "fixture no longer describes this ffmpeg"
    if renders:
        validate_cube(p)
    else:
        with pytest.raises(InvalidLut):
            validate_cube(p)


def test_a_1d_shaper_in_front_of_the_3d_table_is_rejected(tmp_path: Path):
    # ffmpeg "renders" it by reading the shaper rows as the first 3D rows.
    p = tmp_path / "shaper.cube"
    p.write_text("LUT_1D_SIZE 2\nLUT_3D_SIZE 2\n0 0 0\n1 1 1\n" + _rows(2) + "\n")
    with pytest.raises(InvalidLut, match="1D"):
        validate_cube(p)


@pytest.mark.parametrize("lut", sorted(p.name for p in _LUTS.glob("*.cube")))
def test_every_bundled_look_is_valid(lut: str):
    validate_cube(_LUTS / lut)


def test_messages_are_plain_and_name_the_file(tmp_path: Path):
    p = tmp_path / "x.cube"
    p.write_text(ONE_D)
    with pytest.raises(InvalidLut) as e:
        validate_cube(p, display_name="film_1d.cube")
    assert "film_1d.cube" in str(e.value) and "1D" in str(e.value) and "3D" in str(e.value)
    p.write_text(TRUNCATED)
    with pytest.raises(InvalidLut) as e:
        validate_cube(p, display_name="t.cube")
    assert "64" in str(e.value) and "2" in str(e.value)
    p.write_text(TOO_BIG)
    with pytest.raises(InvalidLut, match="256"):
        validate_cube(p)


# ---------------------------------------------------------------- the routes

@pytest.fixture()
def api(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


def _one_clip_project(api, tmp_path: Path) -> tuple[str, str]:
    sid = api.post("/api/sessions").json()["id"]
    clip = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x120:r=25:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True)
    with clip.open("rb") as fh:
        r = api.post(f"/api/sessions/{sid}/upload", files={"file": ("a.mp4", fh, "video/mp4")},
                     data={"add_to_timeline": "true", "transcribe": "false"})
    assert r.status_code == 200, r.text
    v1 = next(t for t in api.get(f"/api/sessions/{sid}/edl").json()["tracks"] if t["id"] == "v1")
    return sid, v1["clips"][0]["id"]


def _detail(r) -> dict:
    """The app's 422 envelope (hardening.py): {"error": {..., "details": <HTTPException detail>}}."""
    return r.json()["error"]["details"]


def _upload(api, sid, name, body: str):
    return api.post(f"/api/sessions/{sid}/lut_upload",
                    files={"file": (name, io.BytesIO(body.encode()), "text/plain")})


@pytest.mark.parametrize("body,needle", [(ONE_D, "1D"), (TRUNCATED, "64"), (TOO_BIG, "256")])
def test_lut_upload_rejects_a_cube_ffmpeg_cannot_render(api, tmp_path, body, needle):
    sid = api.post("/api/sessions").json()["id"]
    r = _upload(api, sid, "film.cube", body)
    assert r.status_code == 422, r.text
    detail = _detail(r)
    assert detail["error"] == "invalid_lut"
    assert "film.cube" in detail["message"] and needle in detail["message"]
    # nothing half-imported is left in the session
    from video_ai_editor import main as _main
    assert not list((_main.session_dir(sid) / "uploads" / "luts").glob("*.cube"))


def test_lut_upload_still_takes_a_good_cube(api, tmp_path):
    sid = api.post("/api/sessions").json()["id"]
    r = _upload(api, sid, "mine.cube", GOOD)
    assert r.status_code == 200, r.text
    assert Path(r.json()["path"]).exists()


def test_apply_lut_rejects_a_bad_cube_path(api, tmp_path):
    sid, cid = _one_clip_project(api, tmp_path)
    from video_ai_editor import main as _main
    bad = _main.session_dir(sid) / "uploads" / "film_1d.cube"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(ONE_D)
    r = api.post(f"/api/sessions/{sid}/dispatch",
                 json={"tool": "apply_lut", "args": {"clip_id": cid, "src": str(bad)}})
    assert r.status_code >= 400, r.text
    assert "1D" in r.text
    v1 = next(t for t in api.get(f"/api/sessions/{sid}/edl").json()["tracks"] if t["id"] == "v1")
    assert not [e for e in v1["clips"][0].get("effects", []) if e["type"] == "lut"]


def _project_with_broken_lut(api, tmp_path) -> str:
    """A project whose LUT went bad AFTER it was applied (a project saved
    before the import guard, or a .cube edited on disk)."""
    sid, cid = _one_clip_project(api, tmp_path)
    up = _upload(api, sid, "film_look.cube", GOOD)
    assert up.status_code == 200, up.text
    path = Path(up.json()["path"])
    r = api.post(f"/api/sessions/{sid}/dispatch",
                 json={"tool": "apply_lut", "args": {"clip_id": cid, "src": str(path)}})
    assert r.status_code == 200, r.text
    path.write_text(ONE_D)
    return sid


def test_preview_failing_on_a_broken_lut_names_the_look_not_the_clip(api, tmp_path):
    sid = _project_with_broken_lut(api, tmp_path)
    r = api.post(f"/api/sessions/{sid}/preview")
    assert r.status_code == 422, r.text
    msg = _detail(r)["message"]
    assert "film_look" in msg and "look" in msg.lower(), msg
    assert "corrupt" not in msg and "codec" not in msg, msg


def test_export_failing_on_a_broken_lut_names_the_look_not_the_clip(api, tmp_path):
    sid = _project_with_broken_lut(api, tmp_path)
    r = api.post(f"/api/sessions/{sid}/export", json={"height": 360})
    assert r.status_code == 422, r.text
    msg = _detail(r)["message"]
    assert "film_look" in msg and "export" in msg.lower(), msg
    assert "corrupt" not in msg and "codec" not in msg, msg


def test_failure_text_from_lut3d_alone_does_not_blame_the_clip():
    from video_ai_editor.main import _render_failure_message
    full = ("ffmpeg render failed (rc=183):\n[Parsed_lut3d_3 @ 0x7b3c] 3D LUT is empty\n"
            "[AVFilterGraph @ 0x7b3d] Error initializing filters\n"
            "Error opening output files: Invalid data found when processing input\n")
    for kind in ("preview", "export"):
        msg = _render_failure_message(full[-400:], full, kind=kind)
        assert "LUT" in msg or "look" in msg, msg
        assert "corrupt" not in msg and "codec" not in msg, msg
