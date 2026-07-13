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
        # Mock out the real AVFoundation TCC check — it depends on this
        # machine's actual mic-permission state (and can block on a real
        # permission prompt if notDetermined), which a hardware-independent
        # unit test must not depend on.
        monkeypatch.setattr(desktop, "_ensure_mic_authorized_mac", lambda: (True, "mocked"))
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
        monkeypatch.setattr(desktop, "_ensure_mic_authorized_mac", lambda: (True, "mocked"))
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
        monkeypatch.setattr(desktop, "_ensure_mic_authorized_mac", lambda: (True, "mocked"))
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
        # `unsupported: True` lets the frontend distinguish "this platform
        # doesn't have the native bridge, fall back to getUserMedia" (Task 2)
        # from a real mac-side error that should be shown to the user.
        assert r == {"ok": False, "unsupported": True,
                     "error": "native mic capture is only implemented on macOS"}
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_vo_start_blocked_when_mic_not_authorized(monkeypatch, tmp_path):
    """vo_start must check TCC authorization via _ensure_mic_authorized_mac
    BEFORE spawning ffmpeg, and return its detail message when denied —
    never silently proceed to a doomed subprocess capture."""
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
        monkeypatch.setattr(
            desktop, "_ensure_mic_authorized_mac",
            lambda: (False, "microphone access denied — enable it in System Settings"),
        )

        def fail_if_called(*a, **kw):
            raise AssertionError("ffmpeg must not be spawned when mic auth is denied")
        monkeypatch.setattr(desktop.subprocess, "Popen", fail_if_called)

        r = api.vo_start(sid)
        assert r == {"ok": False, "error": "microphone access denied — enable it in System Settings"}
        # No recording state should have been latched.
        assert api._rec_proc is None
        assert api._rec_path is None
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_vo_stop_includes_ffmpeg_stderr_tail_on_empty_wav(monkeypatch, tmp_path):
    """When the WAV comes back empty, vo_stop should surface ffmpeg's own
    stderr tail so a TCC denial, a bad device index, and a genuinely silent
    device are distinguishable — not a single generic message."""
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
        monkeypatch.setattr(desktop, "_ensure_mic_authorized_mac", lambda: (True, "mocked"))
        monkeypatch.setattr(desktop, "_avfoundation_default_audio_index", lambda: "0")

        class _EmptyWavProc(_FakeProc):
            def __init__(self, out_path: Path):
                super().__init__(out_path, write_bytes=False)
                out_path.touch()  # exists but zero bytes — the failure case

            def communicate(self, timeout=None):
                return (b"", b"[AVFoundation indev] Input/output error: mic access denied")

        monkeypatch.setattr(desktop.subprocess, "Popen",
                            lambda args, **kw: _EmptyWavProc(Path(args[-1])))

        api.vo_start(sid)
        r = api.vo_stop(sid)
        assert r["ok"] is False
        assert "recording produced no audio" in r["error"]
        assert "ffmpeg said:" in r["error"]
        assert "mic access denied" in r["error"]
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)


def test_ensure_mic_authorized_mac_already_authorized(monkeypatch):
    """status==3 (authorized) short-circuits to True without prompting."""
    from video_ai_editor import desktop

    class _FakeAVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type):
            assert media_type == "soun"
            return 3

    class _FakeAVFoundation:
        AVCaptureDevice = _FakeAVCaptureDevice

    import sys
    monkeypatch.setitem(sys.modules, "AVFoundation", _FakeAVFoundation)
    ok, detail = desktop._ensure_mic_authorized_mac()
    assert ok is True
    assert detail == "already authorized"


def test_ensure_mic_authorized_mac_denied(monkeypatch):
    """status==2 (denied) returns False with an actionable message, without
    calling requestAccessForMediaType_ (there is nothing to prompt for)."""
    from video_ai_editor import desktop

    class _FakeAVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type):
            return 2

        @staticmethod
        def requestAccessForMediaType_completionHandler_(media_type, cb):
            raise AssertionError("must not request access when already denied")

    class _FakeAVFoundation:
        AVCaptureDevice = _FakeAVCaptureDevice

    import sys
    monkeypatch.setitem(sys.modules, "AVFoundation", _FakeAVFoundation)
    ok, detail = desktop._ensure_mic_authorized_mac()
    assert ok is False
    assert "denied" in detail


def test_ensure_mic_authorized_mac_not_determined_requests_and_grants(monkeypatch):
    """status==0 (notDetermined) triggers a real request; the completion
    handler firing synchronously (simulating the callback) should resolve to
    granted=True."""
    from video_ai_editor import desktop

    class _FakeAVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type):
            return 0

        @staticmethod
        def requestAccessForMediaType_completionHandler_(media_type, cb):
            cb(True)  # simulate the OS granting immediately

    class _FakeAVFoundation:
        AVCaptureDevice = _FakeAVCaptureDevice

    import sys
    monkeypatch.setitem(sys.modules, "AVFoundation", _FakeAVFoundation)
    ok, detail = desktop._ensure_mic_authorized_mac()
    assert ok is True
    assert detail == "granted"


def test_ensure_mic_authorized_mac_not_determined_dismissed(monkeypatch):
    """notDetermined + the user dismissing/denying the prompt (granted=False)
    must not be treated as authorized."""
    from video_ai_editor import desktop

    class _FakeAVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type):
            return 0

        @staticmethod
        def requestAccessForMediaType_completionHandler_(media_type, cb):
            cb(False)

    class _FakeAVFoundation:
        AVCaptureDevice = _FakeAVCaptureDevice

    import sys
    monkeypatch.setitem(sys.modules, "AVFoundation", _FakeAVFoundation)
    ok, detail = desktop._ensure_mic_authorized_mac()
    assert ok is False
    assert "dismissed" in detail or "denied" in detail


def test_ensure_mic_authorized_mac_degrades_when_avfoundation_missing(monkeypatch):
    """If pyobjc's AVFoundation bridge can't be imported at all, the
    pre-check must degrade to (True, ...) rather than block recording —
    falling through to the pre-existing subprocess-level prompt/denial."""
    from video_ai_editor import desktop
    import builtins
    import sys

    monkeypatch.delitem(sys.modules, "AVFoundation", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "AVFoundation":
            raise ImportError("no AVFoundation on this system")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, detail = desktop._ensure_mic_authorized_mac()
    assert ok is True
    assert "AVFoundation unavailable" in detail


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


def test_avfoundation_default_audio_index_prefers_microphone_over_first_listed(monkeypatch):
    """When an aggregate/virtual device (e.g. an audio-loopback tool) sorts
    BEFORE the real hardware microphone in ffmpeg's device dump, the probe
    must still pick the device whose name contains "microphone" rather than
    silently defaulting to whatever happened to list first."""
    from video_ai_editor import desktop

    fake_stderr = (
        "[AVFoundation indev @ 0x0] AVFoundation video devices:\n"
        "[AVFoundation indev @ 0x0] [0] FaceTime HD Camera\n"
        "[AVFoundation indev @ 0x0] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x0] [0] BlackHole 2ch\n"
        "[AVFoundation indev @ 0x0] [1] MacBook Air Microphone\n"
    )

    class _R:
        returncode = 1
        stderr = fake_stderr
        stdout = ""

    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **kw: _R())
    assert desktop._avfoundation_default_audio_index() == "1"


def test_avfoundation_default_audio_index_falls_back_to_first_when_no_microphone_named(monkeypatch):
    """If no device name contains "microphone", fall back to the first audio
    device listed (the pre-existing, documented default) rather than "0"."""
    from video_ai_editor import desktop

    fake_stderr = (
        "[AVFoundation indev @ 0x0] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x0] [2] USB Audio Device\n"
        "[AVFoundation indev @ 0x0] [3] Aggregate Device\n"
    )

    class _R:
        returncode = 1
        stderr = fake_stderr
        stdout = ""

    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **kw: _R())
    assert desktop._avfoundation_default_audio_index() == "2"
