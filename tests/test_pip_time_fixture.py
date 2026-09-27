"""The client's retimed-PIP map (frontend lib/pipTime.ts) is pinned by a
fixture of the export's frames; this keeps that fixture equal to the Python
model it was generated from (tests/gen_pip_time_fixture.py), so the model
and the vitest cannot drift apart silently."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gen_pip_time_fixture as gen  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def test_the_pip_time_fixture_is_the_models(tmp_path):
    want = gen.build(tmp_path)
    got = json.loads(gen.FIXTURE.read_text())
    assert got == want, "stale fixture: run .venv/bin/python tests/gen_pip_time_fixture.py"
    # every speed shape, every rate, both sources
    assert len(got["cases"]) == len(gen.RATES) * 2 * len(gen._kinds())
