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
