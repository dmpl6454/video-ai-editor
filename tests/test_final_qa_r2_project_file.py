"""Final QA round 2: the .vae project file.

* A custom LUT (.cube) was not bundled, so a project opened elsewhere failed
  every preview and export ("a colour LUT applied to a clip is missing"),
  and Save did not warn.
* A crafted .vae could hide an absolute path to any file on the Mac in its
  undo history; the next Save silently copied that file into the archive.
* A .vae whose meta.json is not an object (``[1,2,3]``/``null``) broke the
  project list and New project for good (500 on every call).
* A full disk mid-save left a truncated .vae (destroying the previous good
  save of the same name), and routes answered "internal server error".
"""
from __future__ import annotations

import errno
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Effect, Track

CUBE = "LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1))


@pytest.fixture
def wd(tmp_path: Path, monkeypatch) -> Path:
    from video_ai_editor import storage as _storage
    root = tmp_path / "wd"
    root.mkdir()
    monkeypatch.setattr(_storage, "WORKDIR", root)
    return root


def _media(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=160x90:d=1:r=30", "-pix_fmt", "yuv420p", str(p)],
                   check=True, capture_output=True)
    return p


def _session(wd: Path, sid: str, *, lut: bool = False) -> Path:
    sd = wd / sid
    src = _media(sd / "uploads" / "clip.mp4")
    c = Clip(id="c1", src=str(src), in_=0, out=1, start=0)
    if lut:
        cube = sd / "uploads" / "luts" / "r2 look.cube"
        cube.parent.mkdir(parents=True)
        cube.write_text(CUBE)
        c.effects.append(Effect(type="lut", params={"src": str(cube), "intensity": 1.0}))
    edl = EDL(canvas=Canvas(w=160, h=90, fps=30), tracks=[Track(id="v1", type="video", clips=[c])])
    edl.recompute_duration()
    (sd / "edl.json").write_text(edl.model_dump_json())
    EDLStore(sd)
    return sd


def _members(vae: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(vae) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ------------------------------------------------------------------ LUT

def test_a_custom_lut_travels_in_the_project_file(wd, tmp_path):
    from video_ai_editor.storage_project import load_project, save_project
    sd = _session(wd, "s_lutsource1", lut=True)
    report: dict = {}
    vae = save_project("s_lutsource1", tmp_path / "p.vae", report=report)
    assert report["missing"] == []
    assert any(n.startswith("media/") and n.endswith(".cube") for n in _members(vae))
    shutil.rmtree(sd)                                    # "another Mac"
    new = wd / load_project(vae)
    lut = EDLStore(new).edl.get_track("v1").clips[0].effects[0].params["src"]
    assert Path(lut).is_file() and Path(lut).resolve().is_relative_to(new.resolve()), lut


def test_save_warns_when_a_lut_is_missing(wd, tmp_path):
    from video_ai_editor.storage_project import save_project
    sd = _session(wd, "s_lutmissing", lut=True)
    (sd / "uploads" / "luts" / "r2 look.cube").unlink()
    report: dict = {}
    save_project("s_lutmissing", tmp_path / "p.vae", report=report)
    assert [r["name"] for r in report["missing"]] == ["r2 look.cube"], report


# --------------------------------------------------- crafted undo history

def _craft(vae: Path, dst: Path, victim: Path) -> None:
    with zipfile.ZipFile(vae) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.startswith("snapshots/"):
                edl = json.loads(data)
                hidden = dict(edl["tracks"][0]["clips"][0], id="c_hidden", start=100.0,
                              src=str(victim))
                edl["tracks"][0]["clips"].append(hidden)
                data = json.dumps(edl).encode()
            zout.writestr(info, data)


def test_a_path_hidden_in_the_undo_history_is_never_bundled(wd, tmp_path):
    from video_ai_editor.edl import EDLStore as Store
    from video_ai_editor.storage_project import load_project, save_project
    sd = _session(wd, "s_crafted01")
    store = Store(sd)
    store.edl.get_track("v1").clips[0].out = 0.5
    store.commit("trim_clip", {}, "trim")           # at least one snapshot
    victim = tmp_path / "victim_private" / "id_rsa"
    victim.parent.mkdir()
    victim.write_bytes(b"PRIVATE-KEY-MATERIAL-r2test\n")
    vae = save_project("s_crafted01", tmp_path / "good.vae")
    crafted = tmp_path / "crafted.vae"
    _craft(vae, crafted, victim)
    new_sid = load_project(crafted)
    # nothing in the reopened session names the outside file any more
    for p in (wd / new_sid).rglob("*.json"):
        assert str(victim) not in p.read_text(encoding="utf-8"), p
    resaved = save_project(new_sid, tmp_path / "resaved.vae")
    assert all(b"PRIVATE-KEY-MATERIAL" not in data for data in _members(resaved).values())


def test_save_does_not_bundle_an_outside_file_named_only_by_history(wd, tmp_path):
    """Defence in depth: even a session already holding such a snapshot."""
    from video_ai_editor.storage_project import save_project
    sd = _session(wd, "s_histonly1")
    victim = tmp_path / "secret.txt"
    victim.write_bytes(b"SECRET-r2\n")
    edl = json.loads((sd / "edl.json").read_text())
    edl["tracks"][0]["clips"][0]["src"] = str(victim)
    (sd / "snapshots" / "00009_deadbeef.json").write_text(json.dumps(edl))
    vae = save_project("s_histonly1", tmp_path / "p.vae")
    assert all(b"SECRET-r2" not in d for d in _members(vae).values())


# ------------------------------------------------------- meta.json shape

@pytest.mark.parametrize("raw", ["[1,2,3]", "null", '"x"', "7"])
def test_a_non_object_meta_json_does_not_break_the_project_list(wd, raw):
    from video_ai_editor import storage
    _session(wd, "s_badmeta01")
    (wd / "s_badmeta01" / "meta.json").write_text(raw)
    rows = storage.list_sessions()
    assert [r["id"] for r in rows] == ["s_badmeta01"]
    assert storage.read_meta("s_badmeta01") == {}
    storage.name_reopened_copy("s_badmeta01", "today")
    assert isinstance(storage.read_meta("s_badmeta01").get("name"), str)


def test_opening_a_vae_with_a_list_meta_json_imports_a_dict(wd, tmp_path):
    from video_ai_editor.storage_project import load_project, save_project
    _session(wd, "s_metasrc01")
    (wd / "s_metasrc01" / "meta.json").write_text('{"name": "ok"}')
    vae = save_project("s_metasrc01", tmp_path / "p.vae")
    bad = tmp_path / "bad.vae"
    with zipfile.ZipFile(vae) as zin, zipfile.ZipFile(bad, "w") as zout:
        for info in zin.infolist():
            data = b"[1,2,3]" if info.filename == "meta.json" else zin.read(info.filename)
            zout.writestr(info, data)
    sid = load_project(bad)
    meta = wd / sid / "meta.json"
    assert not meta.exists() or isinstance(json.loads(meta.read_text()), dict)


# ------------------------------------------------------------ disk full

def test_a_failed_save_keeps_the_previous_good_file_and_leaves_no_partial(wd, tmp_path, monkeypatch):
    from video_ai_editor import storage_project as sp
    _session(wd, "s_diskfull1")
    dst = tmp_path / "exports" / "Project.vae"
    good = sp.save_project("s_diskfull1", dst)
    before = good.read_bytes()
    real = zipfile.ZipFile.write

    def full(self, filename, *a, **k):
        if str(filename).endswith(".mp4"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, filename, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "write", full)
    with pytest.raises(OSError):
        sp.save_project("s_diskfull1", dst)
    assert dst.read_bytes() == before
    assert sorted(p.name for p in dst.parent.iterdir()) == ["Project.vae"]


def test_a_full_disk_on_any_route_is_a_507_with_a_sentence(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from video_ai_editor.api import hardening

    app = FastAPI()
    hardening.install(app)

    @app.post("/boom")
    def boom():
        raise OSError(errno.ENOSPC, "No space left on device")

    r = TestClient(app, raise_server_exceptions=False).post("/boom")
    assert r.status_code == 507, r.text
    body = r.json()["error"]
    assert "disk is full" in body["message"]
    assert body["details"]["error"] == "disk_full"
