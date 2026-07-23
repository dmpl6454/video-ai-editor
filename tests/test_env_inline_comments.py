"""Inline comments in .env values must not poison config.

.env.example itself shipped `WHISPER_DEVICE=auto  # auto | cpu | cuda | mps`,
and _apply_env_file kept the comment as part of the value — every tester who
followed `cp .env.example .env` then hit whisper's
"unsupported device auto  # auto | cpu | cuda | mps" on the Captions button.
"""
from __future__ import annotations
import os
from pathlib import Path

import pytest

from video_ai_editor import config as cfg


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("VAI_TEST_KEY", raising=False)
    monkeypatch.delenv("VAI_TEST_QUOTED", raising=False)
    yield
    os.environ.pop("VAI_TEST_KEY", None)
    os.environ.pop("VAI_TEST_QUOTED", None)


def test_unquoted_inline_comment_is_stripped(tmp_path: Path, clean_env):
    env = tmp_path / ".env"
    env.write_text("VAI_TEST_KEY=auto  # auto | cpu | cuda | mps\n")
    cfg._apply_env_file(env)
    assert os.environ["VAI_TEST_KEY"] == "auto"


def test_quoted_value_keeps_hash(tmp_path: Path, clean_env):
    env = tmp_path / ".env"
    env.write_text('VAI_TEST_QUOTED="pass#word # not a comment"\n')
    cfg._apply_env_file(env)
    assert os.environ["VAI_TEST_QUOTED"] == "pass#word # not a comment"


def test_env_example_has_no_inline_comments():
    """Our own example file must obey the parser's rules."""
    repo = Path(__file__).resolve().parents[1]
    for line in (repo / ".env.example").read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        _, v = s.split("=", 1)
        assert not (v.strip() and not v.lstrip().startswith(('"', "'"))
                    and " #" in v), f".env.example inline comment: {line!r}"


def test_whisper_device_sanitized(monkeypatch):
    """Even a poisoned existing .env must not crash transcription: the device
    string is validated and unknown values fall back to cpu."""
    from video_ai_editor.ingest import transcribe
    monkeypatch.setattr(transcribe, "WHISPER_DEVICE",
                        "auto  # auto | cpu | cuda | mps")
    assert transcribe._resolve_device() == "cpu"
    monkeypatch.setattr(transcribe, "WHISPER_DEVICE", "cuda")
    assert transcribe._resolve_device() == "cuda"
    monkeypatch.setattr(transcribe, "WHISPER_DEVICE", "mps")
    # faster-whisper/ctranslate2 has no mps backend — must degrade to cpu
    assert transcribe._resolve_device() == "cpu"
