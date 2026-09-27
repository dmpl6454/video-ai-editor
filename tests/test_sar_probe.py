"""render/sar.py's source probe (review RD3).

* It is niced under priority=low like every other render probe (the wave D3
  follow-up E4 closed for compositor.py and segments.py; sar.py was new): the
  priority itself is pinned end to end by test_preview_priority_probes.py,
  which now counts render/sar.py among its modules.
* A FAILED probe (a timeout under load, an OS error) is not cached: it was a
  `None` inside an lru_cache, so one 20 s timeout made an anamorphic source
  render squeezed for the rest of the process, silently.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from video_ai_editor.render import cancel as C
from video_ai_editor.render import sar


@pytest.fixture()
def pal(tmp_path) -> str:
    p = tmp_path / "pal.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=720x576:r=25:d=1",
                    "-vf", "setsar=16/15", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    sar._probe.cache_clear()
    yield str(p)
    sar._probe.cache_clear()


def test_a_probe_that_times_out_is_retried_on_the_next_render(pal, monkeypatch):
    real = C.run_prioritised
    calls = []

    def flaky(args, **kw):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 20)
        return real(args, **kw)

    monkeypatch.setattr(C, "run_prioritised", flaky)
    assert sar.source_anamorphic(pal) is None          # this render: unknown
    ana = sar.source_anamorphic(pal)                   # the next one probes again
    assert ana is not None and (ana.display_w, ana.display_h) == (768, 576)
    assert len(calls) == 2
    assert sar.source_anamorphic(pal) is ana and len(calls) == 2    # a real answer is cached


def test_the_probe_runs_at_the_active_priority(pal, monkeypatch):
    seen = []
    real = C.run_prioritised

    def spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(C, "run_prioritised", spy)
    with C.low_priority():
        assert sar.source_anamorphic(pal) is not None
    assert seen and "ffprobe" in Path(seen[0][0]).name
