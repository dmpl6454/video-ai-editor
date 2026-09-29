"""The preview's alimiter port is pinned to THIS ffmpeg's alimiter (P2 limiter
tail, 0.8.0 final QA): `tests/goldens/alimiter_cases.json` must be what the
render binary's `alimiter` makes of the golden inputs today. An ffmpeg whose
alimiter changed fails here — re-port `frontend/src/lib/preview/audio/
alimiter.ts`, then regenerate with `tests/gen_alimiter_goldens.py`."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gen_alimiter_goldens as G  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def test_the_golden_is_this_ffmpegs_alimiter():
    old = json.loads(G.GOLDEN.read_text())
    new = G.build()
    assert [c["filter"] for c in old["cases"]] == [c["filter"] for c in new["cases"]]
    for o, n in zip(old["cases"], new["cases"]):
        assert o["out_f32le_b64"] == n["out_f32le_b64"], (
            f"{o['input']} / {o['filter']}: ffmpeg {new['ffmpeg']} limits differently from the "
            f"golden's ffmpeg {old['ffmpeg']} — re-port alimiter.ts")


def test_the_golden_limits_every_input_and_both_limiters_are_the_servers():
    from video_ai_editor.render import audio_mix
    doc = json.loads(G.GOLDEN.read_text())
    assert all(c["peak_in"] > c["limit"] for c in doc["cases"])
    # the two filters the golden pins are the ones the server's preview runs
    assert audio_mix.PREVIEW_LIMITER.startswith(G.LIMITERS[1]["filter"])
    assert "alimiter=limit=0.97:latency=1" in Path(audio_mix.__file__).read_text()
