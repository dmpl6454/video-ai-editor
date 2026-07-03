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
