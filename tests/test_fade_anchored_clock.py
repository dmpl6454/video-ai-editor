"""Video fades on a clip cut at in > 0 (wave E gate, X2).

Review RE put every constant-speed / 1x v1 chain on the file clock anchored at
`in`, seeking >= 0.5 s early: the pre-roll frames before `in` reach the fades
with NEGATIVE pts. vf_fade keeps its start and length as uint64, so the first
negative frame compared as "after the start" and "past the end" at once: the
fade-in never happened and a fade-out held the whole clip black (the geometry
parity gate: 13.86 dB). The fades now run on the same clock lifted by whole
seconds (`_fade_clock_shift`) — the factor of every frame is the one the
unshifted clock gives, the retimed source frame's time T = (pts − in) / speed.
"""
from __future__ import annotations

import re
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import geometry_golden_lib as lib  # noqa: E402

from video_ai_editor.render import compositor  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _clip(in_: float, out: float, **kw):
    case = lib.Case("x", [lib.ClipSpec("land", in_=in_, out=out, **kw)], [0], gain_only=True)
    return lib.build_edl(case).get_track("v1").clips[0]


def test_the_fade_starts_move_by_whole_seconds_in_decimal():
    f = ",fade=t=in:st=0:d=0.400,fade=t=out:st=0.600:d=0.400"
    got = compositor._shift_fade_starts(f, 3)
    assert got == ",fade=t=in:st=3:d=0.400,fade=t=out:st=3.600:d=0.400"
    # the printed digits survive: vf_fade parses the same µs + K·10^6
    assert Decimal("3.600") - 3 == Decimal("0.600")


@pytest.mark.parametrize("in_, speed, want", [
    (0.0, None, 0),        # no seek, no pre-roll: the chain is unchanged
    (0.4, None, 2),        # no -ss (curve_seek 0), but 0.4 s decoded before `in`
    (1.0, None, 2),        # 0.6 s pre-roll at 1x
    (1.0, 0.1, 7),         # 0.6 s at 0.1x is 6 s of output before `in`
])
def test_the_shift_covers_the_pre_roll(in_, speed, want):
    c = _clip(in_, in_ + 1.0, speed=speed)
    assert compositor._fade_clock_shift(c, 30, None, compositor.v1_const_speed(c, 30)) == want


def test_a_clip_without_pre_roll_keeps_its_filter_text():
    c = _clip(0.0, 1.0, fade_in=0.4, fade_out=0.4)
    chain = compositor._build_clip_video_chain(c, input_label="[0:v]", label_out="[v]",
                                               canvas_w=640, canvas_h=360, fps=30)
    assert "fade=t=in:st=0:d=0.400,fade=t=out:st=0.600:d=0.400,fps=30" in chain
    assert not re.search(r"setpts=PTS[+-]\d+/TB", chain)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    return lib.ensure_sources(tmp_path_factory.mktemp("fade-src"))


def _gains(sources, tmp: Path, **spec) -> list[float]:
    paths, _infos = sources
    case = lib.Case("f", [lib.ClipSpec("land", **spec)], list(range(30)), gain_only=True)
    return [f["gain"] for f in lib.render_case(case, paths, tmp)]


def test_a_clip_cut_at_in_fades_like_one_cut_at_zero(sources, tmp_path):
    """The flat-grey master: frames at in = 1 s look like frames at 0, so the
    two fades must measure the same (before: all 0 — black — at in = 1)."""
    at0 = _gains(sources, tmp_path / "a", in_=0.0, out=1.0, fade_in=0.4, fade_out=0.4)
    at1 = _gains(sources, tmp_path / "b", in_=1.0, out=2.0, fade_in=0.4, fade_out=0.4)
    assert at1 == pytest.approx(at0, abs=0.005)   # the golden tests' gain tolerance
    assert at1[0] == 0.0 and at1[15] == 1.0 and 0.0 < at1[29] < 0.2


def test_a_fade_out_at_half_speed_after_in_is_not_black(sources, tmp_path):
    g = _gains(sources, tmp_path / "c", in_=1.01, out=1.51, speed=0.5, fade_out=0.3)
    assert g[0] == 1.0 and g[20] == 1.0 and g[29] < 0.2
