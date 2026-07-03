import sys
from video_ai_editor import platformutil as pu


def test_exe_name_appends_exe_on_windows(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    assert pu.exe_name("ffmpeg") == "ffmpeg.exe"
    assert pu.exe_name("whisper-cli") == "whisper-cli.exe"


def test_exe_name_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    assert pu.exe_name("ffmpeg") == "ffmpeg"


def test_exe_name_does_not_double_suffix(monkeypatch):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    assert pu.exe_name("ffmpeg.exe") == "ffmpeg.exe"


def test_find_binary_prefers_path(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    fake = tmp_path / "mytool"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(pu.shutil, "which", lambda n: str(fake) if n == "mytool" else None)
    assert pu.find_binary("mytool", []) == str(fake)


def test_find_binary_falls_back_to_extra_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(pu.shutil, "which", lambda n: None)
    d = tmp_path / "bin"
    d.mkdir()
    (d / "mytool").write_text("x")
    assert pu.find_binary("mytool", [d]) == str(d / "mytool")


def test_find_binary_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(pu.shutil, "which", lambda n: None)
    assert pu.find_binary("nope", [tmp_path]) is None
