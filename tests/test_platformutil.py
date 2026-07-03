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


def test_user_data_dir_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    monkeypatch.setattr(pu, "IS_MAC", False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    got = pu.user_data_dir("Video AI Editor")
    assert got == tmp_path / "AppData" / "Roaming" / "Video AI Editor"


def test_user_data_dir_mac_uses_app_support(monkeypatch, tmp_path):
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(pu, "IS_MAC", True)
    monkeypatch.setattr(pu.Path, "home", staticmethod(lambda: tmp_path))
    got = pu.user_data_dir("Video AI Editor")
    assert got == tmp_path / "Library" / "Application Support" / "Video AI Editor"


def test_write_then_read_utf8_roundtrips_devanagari(tmp_path):
    p = tmp_path / "t.txt"
    s = "नमस्ते 🙏 hello"
    pu.write_text_utf8(p, s)
    assert pu.read_text_utf8(p) == s
    # bytes on disk are UTF-8 regardless of platform locale
    assert p.read_bytes().decode("utf-8") == s


def test_replace_with_retry_succeeds(tmp_path):
    src = tmp_path / "a"; dst = tmp_path / "b"
    src.write_text("new"); dst.write_text("old")
    pu.replace_with_retry(src, dst)
    assert dst.read_text() == "new"
    assert not src.exists()


def test_unlink_with_retry_missing_ok(tmp_path):
    pu.unlink_with_retry(tmp_path / "does-not-exist")  # must not raise


def test_whisper_cpp_bin_uses_exe_name(monkeypatch):
    """_WHISPER_CPP_BIN resolution must add .exe on Windows and not hardcode a
    brew path as the win fallback."""
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    monkeypatch.setattr(pu.shutil, "which",
                        lambda n: "C:/tools/whisper-cli.exe" if n == "whisper-cli.exe" else None)
    import importlib, video_ai_editor.ingest.transcribe as t
    importlib.reload(t)
    assert t._WHISPER_CPP_BIN == "C:/tools/whisper-cli.exe"
    importlib.reload(t)


def test_ffmpeg_constants_exist():
    assert pu.FFMPEG in ("ffmpeg", "ffmpeg.exe")
    assert pu.FFPROBE in ("ffprobe", "ffprobe.exe")


def test_ffmpeg_filter_path_escapes_windows_drive_path():
    """A Windows path embedded in an ffmpeg filter option value must have its
    backslashes turned into forward slashes and its drive colon escaped as
    '\\\\:' — the only form that survives ffmpeg's two-pass filtergraph parser.
    Verified empirically against real ffmpeg."""
    got = pu.ffmpeg_filter_path(r"C:\Users\me\cache\stable.trf")
    assert got == "C\\\\:/Users/me/cache/stable.trf"


def test_ffmpeg_filter_path_leaves_posix_path_untouched_except_colon():
    """A POSIX path has no backslashes; a stray colon (rare) still gets escaped
    so the value never breaks the filtergraph parser."""
    assert pu.ffmpeg_filter_path("/tmp/cache/stable.trf") == "/tmp/cache/stable.trf"
    assert pu.ffmpeg_filter_path("/tmp/a:b.trf") == "/tmp/a\\\\:b.trf"
