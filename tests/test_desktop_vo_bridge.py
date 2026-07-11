"""Tests for desktop.py's native VO-capture js_api bridge (_Api.vo_start/vo_stop).

These exercise the guard rails and control flow with subprocess.Popen and the
network POST mocked out (no real mic/ffmpeg/HTTP needed, so this runs in CI
on machines with no audio hardware). The one thing NOT covered here — real
avfoundation mic capture actually producing audio — was verified manually
against a live macOS backend during implementation (see task notes); it
can't be exercised in an automated, hardware-independent test.
"""
from __future__ import annotations
from pathlib import Path


class _FakeProc:
    """Stand-in for subprocess.Popen that never really launches ffmpeg."""
    def __init__(self, out_path: Path, write_bytes: bool = True):
        self._out_path = out_path
        self._returncode: int | None = None
        if write_bytes:
            # Simulate ffmpeg having created a non-empty file by the time
            # vo_stop's poll()/communicate() would run.
            out_path.write_bytes(b"RIFF....WAVEfmt ")

    def poll(self):
        return self._returncode

    def send_signal(self, sig):
        self._returncode = 255  # ffmpeg's normal "stopped by signal" code

    def communicate(self, timeout=None):
        return (b"", b"")

    def kill(self):
        self._returncode = -9


def test_vo_start_rejects_invalid_session_id(monkeypatch):
    from video_ai_editor import desktop
    api = desktop._Api("127.0.0.1", 8765)
    r = api.vo_start("../../etc/passwd")
    assert r == {"ok": False, "error": "invalid session id"}


def test_vo_start_rejects_concurrent_recording(monkeypatch, tmp_path):
    from video_ai_editor import desktop
    from video_ai_editor import storage

    monkeypatch.setenv("WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config
    importlib.reload(config)
    importlib.reload(storage)
    try:
        sid = storage.new_session_id()
        storage.session_dir(sid)

        api = desktop._Api("127.0.0.1", 8765)
        monkeypatch.setattr(desktop, "_avfoundation_default_audio_index", lambda: "0")
        monkeypatch.setattr(
            desktop.subprocess, "Popen",
            lambda args, **kw: _FakeProc(Path(args[-1]))
        )
        r1 = api.vo_start(sid)
        assert r1 == {"ok": True}
        r2 = api.vo_start(sid)
        assert r2 == {"ok": False, "error": "a recording is already in progress"}
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_vo_stop_without_start_returns_error():
    from video_ai_editor import desktop
    api = desktop._Api("127.0.0.1", 8765)
    r = api.vo_stop("s_whatever")
    assert r == {"ok": False, "error": "no recording in progress"}


def test_vo_start_stop_round_trip_uploads_and_cleans_up(monkeypatch, tmp_path):
    """Full vo_start -> vo_stop happy path with ffmpeg + the HTTP POST both
    faked out: asserts the bridge (a) spawns a process, (b) SIGINTs it on
    stop, (c) posts the resulting file to /vo_record, (d) returns the
    upload's response merged with ok:True, and (e) deletes the raw WAV
    afterwards (it's a transfer artifact, not the session's stored copy)."""
    from video_ai_editor import desktop
    from video_ai_editor import storage
    import signal as _signal

    monkeypatch.setenv("WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config
    importlib.reload(config)
    importlib.reload(storage)
    try:
        sid = storage.new_session_id()
        storage.session_dir(sid)

        api = desktop._Api("127.0.0.1", 8765)
        monkeypatch.setattr(desktop, "_avfoundation_default_audio_index", lambda: "0")

        captured_signals = []
        fake_proc_holder: dict[str, _FakeProc] = {}

        def fake_popen(args, **kw):
            out_path = Path(args[-1])
            proc = _FakeProc(out_path)
            orig_send = proc.send_signal
            def send_signal(sig):
                captured_signals.append(sig)
                orig_send(sig)
            proc.send_signal = send_signal
            fake_proc_holder["proc"] = proc
            return proc

        monkeypatch.setattr(desktop.subprocess, "Popen", fake_popen)

        captured_upload = {}
        def fake_post(url, field_name, file_path, extra_fields, timeout=30.0):
            captured_upload["url"] = url
            captured_upload["field_name"] = field_name
            captured_upload["file_path"] = file_path
            captured_upload["extra_fields"] = extra_fields
            assert file_path.exists()  # must still exist at upload time
            return {"clip_id": "c_fake123", "duration": 1.5}

        monkeypatch.setattr(desktop, "_post_multipart_file", fake_post)

        r1 = api.vo_start(sid)
        assert r1 == {"ok": True}
        assert fake_proc_holder["proc"] is not None

        recorded_path = api._rec_path
        assert recorded_path is not None and recorded_path.exists()

        r2 = api.vo_stop(sid, start=2.5, gain_db=-3.0)
        assert r2 == {"ok": True, "clip_id": "c_fake123", "duration": 1.5}
        assert captured_signals == [_signal.SIGINT]
        assert captured_upload["url"] == f"http://127.0.0.1:8765/api/sessions/{sid}/vo_record"
        assert captured_upload["field_name"] == "file"
        assert captured_upload["extra_fields"] == {"start": "2.5", "gain_db": "-3.0"}
        # Cleanup: the raw capture WAV is deleted after a successful upload.
        assert not recorded_path.exists()
        # Internal state reset so a subsequent vo_start is allowed again.
        assert api._rec_proc is None
        assert api._rec_path is None
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_vo_stop_reports_upload_failure_but_still_cleans_up(monkeypatch, tmp_path):
    from video_ai_editor import desktop
    from video_ai_editor import storage
    import urllib.error

    monkeypatch.setenv("WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config
    importlib.reload(config)
    importlib.reload(storage)
    try:
        sid = storage.new_session_id()
        storage.session_dir(sid)

        api = desktop._Api("127.0.0.1", 8765)
        monkeypatch.setattr(desktop, "_avfoundation_default_audio_index", lambda: "0")
        monkeypatch.setattr(desktop.subprocess, "Popen",
                            lambda args, **kw: _FakeProc(Path(args[-1])))

        def fake_post_fail(*a, **kw):
            raise urllib.error.HTTPError(
                a[0], 422, "Unprocessable", {}, None
            )
        monkeypatch.setattr(desktop, "_post_multipart_file", fake_post_fail)
        # HTTPError.read() needs a file-like `fp`; patch it directly since we
        # passed None above (real urlopen would supply a real fp).
        monkeypatch.setattr(
            urllib.error.HTTPError, "read",
            lambda self: b'{"error": "transcode failed"}', raising=False,
        )

        api.vo_start(sid)
        recorded_path = api._rec_path
        r = api.vo_stop(sid)
        assert r["ok"] is False
        assert "vo_record upload failed" in r["error"]
        # Still cleaned up even on failure — no orphaned temp WAV.
        assert not recorded_path.exists()
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_vo_start_no_op_on_non_mac(monkeypatch, tmp_path):
    from video_ai_editor import desktop
    from video_ai_editor import platformutil as _pu
    from video_ai_editor import storage

    monkeypatch.setenv("WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config
    importlib.reload(config)
    importlib.reload(storage)
    try:
        sid = storage.new_session_id()
        storage.session_dir(sid)
        monkeypatch.setattr(_pu, "IS_MAC", False)
        api = desktop._Api("127.0.0.1", 8765)
        r = api.vo_start(sid)
        assert r == {"ok": False, "error": "native mic capture is only implemented on macOS"}
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_avfoundation_default_audio_index_parses_device_list(monkeypatch):
    from video_ai_editor import desktop

    fake_stderr = (
        "[AVFoundation indev @ 0x0] AVFoundation video devices:\n"
        "[AVFoundation indev @ 0x0] [0] FaceTime HD Camera\n"
        "[AVFoundation indev @ 0x0] [1] Capture screen 0\n"
        "[AVFoundation indev @ 0x0] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x0] [0] MacBook Air Microphone\n"
        "[AVFoundation indev @ 0x0] [1] LoomAudioDevice\n"
    )

    class _R:
        returncode = 1
        stderr = fake_stderr
        stdout = ""

    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **kw: _R())
    assert desktop._avfoundation_default_audio_index() == "0"


def test_avfoundation_default_audio_index_falls_back_on_error(monkeypatch):
    from video_ai_editor import desktop

    def raise_err(*a, **kw):
        raise FileNotFoundError("no ffmpeg")
    monkeypatch.setattr(desktop.subprocess, "run", raise_err)
    assert desktop._avfoundation_default_audio_index() == "0"
