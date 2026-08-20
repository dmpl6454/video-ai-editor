"""The Real-ESRGAN / RIFE installer (`cli/setup_ai_binaries.py`).

These are the two features `check_features` reports missing on a fresh
checkout — everything else is a pip package. Both are ncnn-vulkan binaries
with per-OS release zips of two DIFFERENT shapes, both verified against the
real upstream archives before writing this:

  * Real-ESRGAN: flat at the zip root (exe + models/ side by side).
  * RIFE: nested one level under `rife-ncnn-vulkan-<date>-<os>/`.

`_extract`'s job is to make both land the same way — binary and model dirs
directly inside the destination — without hardcoding which tool nests, since a
future release changing either layout should not silently install one level
too deep or too shallow.

Network access is never exercised here: `_download` is tested against a
`file://` URL (a real urllib code path, no HTTP), and `install_one` is tested
against a locally-built fixture zip. `install_one` calls
`importlib.reload(mod)` on the verify module — which needs a REAL file backing
it (a bare in-memory module has no loader to re-exec, and reload() raises
`ModuleNotFoundError: spec not found` on one, verified) — so the `fake_tool`
fixture writes a real throwaway .py file into `tmp_path` rather than faking a
module in `sys.modules`.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

from video_ai_editor.cli import setup_ai_binaries as S


def _make_zip(path: Path, entries: dict[str, bytes], *, executable: set[str] = frozenset()):
    """Build a zip with the given {name: content} entries. Names ending in '/'
    become directory entries. `executable` names get unix mode 0o755 stored in
    external_attr, mirroring how the real archives are built on POSIX."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            if name.endswith("/"):
                zf.writestr(zipfile.ZipInfo(name), b"")
                continue
            zi = zipfile.ZipInfo(name)
            if name in executable:
                zi.external_attr = (0o100755 & 0xFFFF) << 16
            zf.writestr(zi, data)


# --- _common_prefix ----------------------------------------------------------

def test_common_prefix_flat_layout():
    names = ["input.jpg", "models/", "models/x.bin", "realesrgan-ncnn-vulkan.exe"]
    assert S._common_prefix(names) == ""


def test_common_prefix_nested_layout():
    names = ["rife-ncnn-vulkan-20221029-windows/",
             "rife-ncnn-vulkan-20221029-windows/rife-ncnn-vulkan.exe",
             "rife-ncnn-vulkan-20221029-windows/rife-v4.6/",
             "rife-ncnn-vulkan-20221029-windows/rife-v4.6/flownet.bin"]
    assert S._common_prefix(names) == "rife-ncnn-vulkan-20221029-windows/"


def test_common_prefix_no_shared_root_is_empty():
    """Two distinct top-level entries — must NOT strip anything, or files would
    collide or vanish."""
    names = ["a/one.bin", "b/two.bin"]
    assert S._common_prefix(names) == ""


def test_common_prefix_single_top_level_file_is_not_treated_as_a_directory():
    names = ["readme.txt"]
    assert S._common_prefix(names) == ""


# --- _extract ----------------------------------------------------------------

def test_extract_flat_layout_lands_directly(tmp_path):
    zpath = tmp_path / "flat.zip"
    _make_zip(zpath, {
        "realesrgan-ncnn-vulkan.exe": b"BINARY",
        "models/": b"",
        "models/realesrgan-x4plus.bin": b"WEIGHTS",
        "README.md": b"hi",
    })
    dest = tmp_path / "dest_flat"
    S._extract(zpath, dest)
    assert (dest / "realesrgan-ncnn-vulkan.exe").read_bytes() == b"BINARY"
    assert (dest / "models" / "realesrgan-x4plus.bin").read_bytes() == b"WEIGHTS"


def test_extract_nested_layout_is_stripped(tmp_path):
    zpath = tmp_path / "nested.zip"
    top = "rife-ncnn-vulkan-20221029-windows/"
    _make_zip(zpath, {
        top: b"",
        top + "rife-ncnn-vulkan.exe": b"BINARY",
        top + "rife-v4.6/": b"",
        top + "rife-v4.6/flownet.bin": b"WEIGHTS",
    })
    dest = tmp_path / "dest_nested"
    S._extract(zpath, dest)
    # The top-level directory name must NOT appear in the destination at all.
    assert not (dest / top.rstrip("/")).exists()
    assert (dest / "rife-ncnn-vulkan.exe").read_bytes() == b"BINARY"
    assert (dest / "rife-v4.6" / "flownet.bin").read_bytes() == b"WEIGHTS"


@pytest.mark.skipif(os.name == "nt", reason="unix permission bits do not exist on Windows")
def test_extract_preserves_the_executable_bit_on_posix(tmp_path):
    zpath = tmp_path / "exe.zip"
    _make_zip(zpath, {"tool": b"#!/bin/sh\necho hi"}, executable={"tool"})
    dest = tmp_path / "dest_exe"
    S._extract(zpath, dest)
    mode = (dest / "tool").stat().st_mode
    assert mode & 0o100, f"executable bit not set: {oct(mode)}"


# --- _download -----------------------------------------------------------

def test_download_over_file_url_writes_the_bytes(tmp_path):
    """Exercises the REAL urllib code path (file:// is a real urllib scheme),
    not a mock of it."""
    src = tmp_path / "payload.bin"
    payload = b"x" * (1 << 20) + b"tail"
    src.write_bytes(payload)
    dst = tmp_path / "out.bin"
    S._download(src.as_uri(), dst, label="test")
    assert dst.read_bytes() == payload


def test_download_raises_a_plain_error_on_a_bad_url(tmp_path):
    dst = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="download failed"):
        S._download("file:///no/such/path/at/all.zip", dst, label="test")


# --- install_one -----------------------------------------------------------

@pytest.fixture
def fake_tool(monkeypatch, tmp_path):
    """A BinaryTool backed by a local fixture zip (via file://), a `_dest_dir`
    routed into tmp_path instead of the real per-OS user data dir, and a
    throwaway verify module that is a REAL FILE on disk.

    It has to be a real file, not an in-memory `ModuleType`: `install_one`
    calls `importlib.reload()` on the verify module (the real modules,
    `ai/upscale.py`/`ai/rife.py`, compute `ESRGAN_DIR`/`RIFE_DIR` at IMPORT
    TIME, so a fresh install needs a real reload to be seen at all) — and
    `reload()` requires a loader that can re-exec source, which a synthetic
    module with no backing file does not have (verified: it raises
    `ModuleNotFoundError: spec not found`, even with a hand-built ModuleSpec).
    A real file is also more faithful to the bug this guards against.

    `available()` reads an env var so a test can flip it without touching the
    file — env vars are live global state, so no second reload is needed to
    observe a change made after the fixture's own install.

    Returns (tool, dest_dir, zip_path, flag_env_var).
    """
    from video_ai_editor import platformutil as _pu
    exe_name = _pu.exe_name("toolbin")   # the real archives already bake in
    # .exe on Windows (verified against the actual Real-ESRGAN/RIFE zips), so
    # the fixture must too, or these tests would pass for the wrong reason.
    zpath = tmp_path / "fixture.zip"
    _make_zip(zpath, {
        exe_name: b"BINARY-CONTENT",
        "models/": b"",
        "models/weights.bin": b"W",
    })
    flag = f"VAI_TEST_FAKE_UNAVAILABLE_{tmp_path.name}"
    monkeypatch.delenv(flag, raising=False)
    verify_name = f"vai_test_verify_{tmp_path.name}"
    (tmp_path / f"{verify_name}.py").write_text(
        "import os\n"
        f"def available():\n    return not os.environ.get({flag!r})\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib
    importlib.invalidate_caches()

    tool = S.BinaryTool(
        key="faketool", exe="toolbin", dest_name="faketool",
        verify=verify_name, urls={S._OS_KEY: zpath.as_uri()},
    )
    monkeypatch.setitem(S.TOOLS, "faketool", tool)
    dest = tmp_path / "installed" / "faketool"
    real_dest_dir = S._dest_dir
    monkeypatch.setattr(S, "_dest_dir",
                        lambda t: dest if t.key == "faketool" else real_dest_dir(t))
    yield tool, dest, zpath, flag, exe_name
    sys.modules.pop(verify_name, None)


def test_install_one_downloads_extracts_and_verifies(fake_tool):
    tool, dest, zpath, flag, exe_name = fake_tool
    ok, msg = S.install_one("faketool")
    assert ok, msg
    assert (dest / exe_name).exists()
    assert (dest / "models" / "weights.bin").read_bytes() == b"W"
    assert "installed and verified" in msg


def test_install_one_skips_when_already_present(fake_tool):
    tool, dest, zpath, flag, exe_name = fake_tool
    dest.mkdir(parents=True)
    (dest / exe_name).write_bytes(b"already here")
    ok, msg = S.install_one("faketool")
    assert ok
    assert "already installed" in msg
    # Must not have touched the existing file (and therefore never fetched).
    assert (dest / exe_name).read_bytes() == b"already here"


def test_install_one_force_reinstalls_over_an_existing_binary(fake_tool):
    tool, dest, zpath, flag, exe_name = fake_tool
    dest.mkdir(parents=True)
    (dest / exe_name).write_bytes(b"stale")
    ok, msg = S.install_one("faketool", force=True)
    assert ok, msg
    assert (dest / exe_name).read_bytes() == b"BINARY-CONTENT"


def test_install_one_reports_verify_failure_without_raising(fake_tool, monkeypatch):
    tool, dest, zpath, flag, exe_name = fake_tool
    monkeypatch.setenv(flag, "1")
    ok, msg = S.install_one("faketool")
    assert not ok
    assert "still says no" in msg
    # The files ARE on disk even though verify failed — a corrupt module state
    # or an unexpected extra guard should not be hidden as "nothing happened".
    assert (dest / exe_name).exists()


def test_install_one_reports_missing_binary_after_extraction(fake_tool):
    """The archive extracted fine but never contained the named binary — a
    layout change upstream, not a network or zip failure."""
    tool, dest, zpath, flag, exe_name = fake_tool
    _make_zip(zpath, {"not_the_binary": b"nope"})
    ok, msg = S.install_one("faketool")
    assert not ok
    assert "was not found" in msg


def test_install_one_reports_a_bad_zip_cleanly(fake_tool):
    tool, dest, zpath, flag, exe_name = fake_tool
    zpath.write_bytes(b"this is not a zip file")
    ok, msg = S.install_one("faketool")
    assert not ok
    assert "not a valid zip" in msg


def test_install_one_reports_no_build_for_this_os(fake_tool, monkeypatch):
    tool, dest, zpath, flag, exe_name = fake_tool
    no_url_tool = S.BinaryTool(**{**tool.__dict__, "urls": {}})
    monkeypatch.setitem(S.TOOLS, "faketool", no_url_tool)
    ok, msg = S.install_one("faketool")
    assert not ok
    assert "no faketool build" in msg


def test_download_failure_leaves_no_files_behind(fake_tool, monkeypatch):
    """A network failure must not leave a half-extracted, misleadingly-present
    install for `available()` to trip over later."""
    tool, dest, zpath, flag, exe_name = fake_tool
    broken = S.BinaryTool(**{**tool.__dict__,
                            "urls": {S._OS_KEY: "file:///no/such/file.zip"}})
    monkeypatch.setitem(S.TOOLS, "faketool", broken)
    ok, msg = S.install_one("faketool")
    assert not ok
    assert "download failed" in msg
    assert not dest.exists()


# --- main() ------------------------------------------------------------

def test_main_exits_nonzero_when_a_target_fails(fake_tool, monkeypatch, capsys):
    tool, dest, zpath, flag, exe_name = fake_tool
    no_url_tool = S.BinaryTool(**{**tool.__dict__, "urls": {}})
    monkeypatch.setattr(S, "TOOLS", {"faketool": no_url_tool})
    rc = S.main(["--which", "faketool"])
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_exits_zero_when_everything_succeeds(fake_tool, monkeypatch, capsys):
    tool, dest, zpath, flag, exe_name = fake_tool
    monkeypatch.setattr(S, "TOOLS", {"faketool": tool})
    rc = S.main(["--which", "faketool"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_main_which_all_is_the_default_and_installs_every_registered_tool(
        fake_tool, monkeypatch, capsys):
    tool, dest, zpath, flag, exe_name = fake_tool
    monkeypatch.setattr(S, "TOOLS", {"faketool": tool})
    rc = S.main([])
    assert rc == 0
    assert (dest / exe_name).exists()


# --- the real tools are wired correctly (no network) ------------------------

def test_real_tools_point_at_the_same_dest_dir_the_ai_modules_check_first():
    """`_dest_dir` must agree with `ai/upscale.py::_esrgan_dir` and
    `ai/rife.py::_rife_dir`'s own "new, per-OS" candidate — the first path each
    module checks — or the installer would put files somewhere `available()`
    never looks.
    """
    from video_ai_editor import platformutil as _pu

    esrgan_dest = S._dest_dir(S.TOOLS["upscale"])
    rife_dest = S._dest_dir(S.TOOLS["interpolate"])
    assert esrgan_dest == _pu.user_data_dir("Video AI Editor") / "models" / "realesrgan"
    assert rife_dest == _pu.user_data_dir("Video AI Editor") / "models" / "rife"


def test_every_real_tool_has_a_url_for_windows_macos_and_ubuntu():
    for key, tool in S.TOOLS.items():
        for os_key in ("windows", "macos", "ubuntu"):
            assert os_key in tool.urls, f"{key} has no {os_key} build configured"
            assert tool.urls[os_key].startswith("https://"), tool.urls[os_key]


def test_the_features_fix_strings_name_the_real_module_path():
    """`check_features` hands this string to Claude, which is instructed to
    quote a `fix` VERBATIM rather than composing an install command by hand —
    so the module path inside it has to be real, or that instruction produces
    a command that 404s the moment someone runs it."""
    from video_ai_editor.ai.features import FEATURES

    by_key = {f.key: f for f in FEATURES}
    for key in ("upscale", "interpolate"):
        assert "cli.setup_ai_binaries" in by_key[key].fix
        assert f"--which {key}" in by_key[key].fix
