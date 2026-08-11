"""Whisper backend auto-selection + whisper-cli invocation flags.

Locks in the two fine-tune fixes:
  1. backend="auto" (the new default) routes to whisper.cpp when the binary
     AND the ggml model exist, else falls back to faster-whisper.
  2. whisper-cli is ALWAYS invoked with an explicit `-l` — its built-in
     default is `en` (not auto-detect), which force-decoded Hindi uploads
     as English garbage until we passed `-l auto`.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from video_ai_editor.ingest import transcribe as T


def test_auto_routes_to_whisper_cpp_when_available(monkeypatch, tmp_path: Path):
    sentinel = T.Transcript(language="hi", duration=1.0, segments=[])
    calls = {}

    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    fake_model = tmp_path / "ggml-tiny.bin"; fake_model.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_model)
    def fake_cpp(audio_path, language, model_size):
        calls["used"] = "whisper_cpp"; return sentinel
    monkeypatch.setattr(T, "_transcribe_via_whisper_cpp", fake_cpp)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)

    out = T.transcribe(tmp_path / "in.wav", model_size="tiny")
    assert out is sentinel
    assert calls["used"] == "whisper_cpp"


def test_auto_falls_back_when_model_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    monkeypatch.setattr(T, "_whisper_cpp_model_path",
                        lambda name: tmp_path / "missing.bin")
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)

    # faster-whisper path will be taken; stub the model loader so no real
    # model loads (we only care about routing).
    class FakeInfo:
        language = "en"; duration = 0.0
    class FakeModel:
        def transcribe(self, *a, **kw): return iter(()), FakeInfo()
    monkeypatch.setattr(T, "_get_model", lambda size=None: FakeModel())

    out = T.transcribe(tmp_path / "in.wav", model_size="tiny")
    assert out.language == "en"  # came through the faster-whisper branch


def test_env_override_still_wins(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WHISPER_BACKEND", "faster_whisper")
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    fake_model = tmp_path / "ggml-tiny.bin"; fake_model.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_model)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("cpp used despite env"))
    monkeypatch.setattr(T, "_transcribe_via_whisper_cpp", boom)
    class FakeInfo:
        language = "en"; duration = 0.0
    class FakeModel:
        def transcribe(self, *a, **kw): return iter(()), FakeInfo()
    monkeypatch.setattr(T, "_get_model", lambda size=None: FakeModel())

    out = T.transcribe(tmp_path / "in.wav")
    assert out.language == "en"


def test_whisper_cli_gets_explicit_language_flag(monkeypatch, tmp_path: Path):
    """The cmd must contain `-l auto` when no language is given, and `-l hi`
    when one is. Captured by stubbing subprocess.run inside the module."""
    fake_model = tmp_path / "ggml-tiny.bin"; fake_model.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_model)
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append([str(c) for c in cmd])
        class P:
            returncode = 0; stderr = ""; stdout = ""
        # First call is ffmpeg wav extract; second is whisper-cli. For the
        # whisper-cli call, drop a minimal JSON next to the -of prefix.
        if "whisper-cli" in str(cmd[0]) or str(cmd[0]).endswith("whisper-cli"):
            of = cmd[cmd.index("-of") + 1]
            Path(f"{of}.json").write_text('{"transcription": [], "result": {"language": "hi"}}')
        return P()

    monkeypatch.setattr(T.subprocess, "run", fake_run)

    T._transcribe_via_whisper_cpp(tmp_path / "in.mp4", language=None, model_size="tiny")
    cli = captured[-1]
    assert "-l" in cli and cli[cli.index("-l") + 1] == "auto"

    T._transcribe_via_whisper_cpp(tmp_path / "in.mp4", language="hi", model_size="tiny")
    cli = captured[-1]
    assert cli[cli.index("-l") + 1] == "hi"


# ---------------------------------------------------------------------------
# Packaged-build failure modes.
#
# Both of these reproduce ONLY in a PyInstaller bundle, never in dev — which is
# exactly why they shipped. The Windows app imported faster_whisper fine and
# then died inside `model.transcribe(..., vad_filter=True)` on a missing
# `faster_whisper/assets/silero_vad_v6.onnx` (PyInstaller collects modules, not
# a package's data files). onnxruntime's NoSuchFile is neither ValueError nor
# RuntimeError, so main.py's dispatch mapping passed it straight through as a
# bare HTTP 500 — the user saw "internal server error" and no captions, with
# the identical click working in the browser.
# ---------------------------------------------------------------------------

def test_missing_vad_asset_degrades_instead_of_raising(monkeypatch, tmp_path: Path):
    """VAD only trims silence — a missing asset must cost quality, not the
    whole feature (and must never surface as an unmapped 500)."""
    attempts: list[bool] = []

    class _Info:
        language = "en"; duration = 1.0

    class _FakeModel:
        def transcribe(self, path, *, language=None, word_timestamps=True, vad_filter=True):
            attempts.append(vad_filter)
            if vad_filter:
                # The real shape: onnxruntime raises its own exception type,
                # deliberately NOT a ValueError/RuntimeError subclass.
                raise Exception(
                    "[ONNXRuntimeError] : 3 : NO_SUCHFILE : Load model from "
                    r"...\_internal\faster_whisper\assets\silero_vad_v6.onnx failed"
                )
            return iter([]), _Info()

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _FakeModel())
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: False)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)

    out = T.transcribe(tmp_path / "in.mp4", model_size="tiny")
    assert attempts == [True, False], "should retry exactly once, without VAD"
    assert out.language == "en"


def test_missing_faster_whisper_raises_runtime_error(monkeypatch):
    """A bare ImportError escapes main.py's dispatch mapping as a 500; a
    RuntimeError becomes a 422 carrying an actionable message. The macOS .app
    excludes faster-whisper by design, so it reaches this line for real."""
    import sys
    monkeypatch.setattr(T, "_models", {})
    # Setting a sys.modules entry to None makes `import` raise ImportError.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(RuntimeError) as ei:
        T._get_model("tiny")
    msg = str(ei.value).lower()
    assert "faster-whisper" in msg and "not installed" in msg


def test_spec_bundles_faster_whisper_data_files():
    """The Windows package is built from this .spec (build_win.ps1). Losing
    this line silently reintroduces the 500 above, and no dev path would
    notice — so assert it rather than trusting review."""
    spec = Path(__file__).resolve().parents[1] / "Video AI Editor.spec"
    text = spec.read_text(encoding="utf-8")
    assert "collect_data_files('faster_whisper')" in text


def test_missing_faster_whisper_is_a_422_over_http_not_a_500(tmp_path, monkeypatch):
    """The macOS .app EXCLUDES faster-whisper by design (build_app.sh, to stay
    ~150MB), so every Mac user of the packaged app reaches that import. Unit
    tests prove the handler raises; only HTTP proves the client sees 422 with
    a readable sentence rather than a bare 500 "internal server error" — which
    is exactly what the Windows build did before this fix.
    """
    import subprocess
    import sys
    from fastapi.testclient import TestClient
    from video_ai_editor import main as m
    from video_ai_editor.storage import new_session_id, session_dir
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import Clip

    src = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=160x90:rate=30:duration=1",
         "-f", "lavfi", "-i", "sine=f=440:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)], check=True, capture_output=True)

    sid = new_session_id()
    sd = session_dir(sid)
    for sub in ("uploads", "previews", "exports", "cache", "snapshots"):
        (sd / sub).mkdir(parents=True, exist_ok=True)
    store = EDLStore(sd)
    store.edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=1.0, start=0.0))
    store.commit("test", {}, "seed")

    monkeypatch.setattr(T, "_models", {})
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: False)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    client = TestClient(m.app)
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "auto_caption", "args": {}})
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:400]}"
    assert "faster-whisper" in r.text
