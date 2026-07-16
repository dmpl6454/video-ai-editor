"""render/compositor.py — PIP input-count off-by-one corrupts VO/music labels
(render_failed "Error binding filtergraph inputs/outputs: Invalid argument").

Regression coverage: each V2/PIP clip is added to the ffmpeg command line as
6 argv tokens (-ss v -to v -i path, see pip.py's build_pip_overlay_chain), but
_render counted them with `len(pip_inputs) // 4`. With N PIP clips that
over-counts by floor(1.5*N) - N, which is 0 for N<=1 but >0 once a timeline
has 2+ PIP clips. The drift only leaks into build_audio_mix's
first_input_index (the PIP video overlay and PIP audio-fold both stay
correct — the video chain uses the pre-inflation index, and the fold's own
subtraction cancels the same error). So a timeline with 2+ PIP clips AND a
voiceover/music clip gets a filter_complex that references a [N:a] pad one
past the last real -i input, which ffmpeg rejects at parse time.
"""
from __future__ import annotations
import re
import tempfile
from pathlib import Path

from video_ai_editor.edl.schema import EDL, Canvas, Track, Clip
from video_ai_editor.render import compositor as C


def _edl_with_two_pip_and_one_vo() -> EDL:
    v1 = Track(id="v1", type="video", clips=[
        Clip(src="/tmp/nonexistent/main.mp4", in_=0.0, out=10.0, start=0.0),
    ])
    v2 = Track(id="v2", type="video", clips=[
        Clip(src="/tmp/nonexistent/pip_a.mp4", in_=0.0, out=2.0, start=1.0),
        Clip(src="/tmp/nonexistent/pip_b.mp4", in_=0.0, out=2.0, start=4.0),
    ])
    vo = Track(id="vo", type="vo", clips=[
        Clip(src="/tmp/nonexistent/vo.m4a", in_=0.0, out=3.0, start=0.0),
    ])
    edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30), tracks=[v1, v2, vo])
    edl.recompute_duration()
    return edl


def _capture_filter_complex_and_inputs(edl: EDL) -> tuple[str, int]:
    """Run _render with subprocess.run stubbed out; return (filter_complex,
    number of real -i inputs actually on the argv)."""
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        # _render writes to a .part path then atomically replaces `dst` with
        # it — create an empty stand-in so that bookkeeping succeeds and we
        # reach the filter_complex we actually want to inspect.
        dst_idx = len(args) - 1
        Path(args[dst_idx]).touch()

        class _R:
            returncode = 0
            stderr = ""
        return _R()

    orig_run = C.subprocess.run
    C.subprocess.run = fake_run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            C._render(edl, Path(tmp) / "out.mp4", height=540, fps=30,
                      preview=True, cache_dir=None)
    finally:
        C.subprocess.run = orig_run

    args = captured["args"]
    fc_idx = args.index("-filter_complex")
    fc = args[fc_idx + 1]
    num_inputs = sum(1 for a in args if a == "-i")
    return fc, num_inputs


def test_two_pip_clips_plus_vo_produces_no_out_of_range_input_refs():
    edl = _edl_with_two_pip_and_one_vo()
    fc, num_inputs = _capture_filter_complex_and_inputs(edl)

    referenced = {int(n) for n in re.findall(r"\[(\d+):[va]\]", fc)}
    max_valid_index = num_inputs - 1

    out_of_range = {n for n in referenced if n > max_valid_index}
    assert not out_of_range, (
        f"filter_complex references input index/indices {out_of_range} but "
        f"only {num_inputs} -i inputs exist (valid indices 0..{max_valid_index}). "
        f"This is the off-by-one that made ffmpeg fail with "
        f"'Error binding filtergraph inputs/outputs: Invalid argument'.\n{fc}"
    )


def test_single_pip_clip_plus_vo_stays_correct():
    """Sanity check: N=1 PIP clip has zero drift even before the fix, so this
    must pass both before and after — it isolates that the bug is specific to
    N>=2 PIP clips, not PIP+VO in general."""
    v1 = Track(id="v1", type="video", clips=[
        Clip(src="/tmp/nonexistent/main.mp4", in_=0.0, out=10.0, start=0.0),
    ])
    v2 = Track(id="v2", type="video", clips=[
        Clip(src="/tmp/nonexistent/pip_a.mp4", in_=0.0, out=2.0, start=1.0),
    ])
    vo = Track(id="vo", type="vo", clips=[
        Clip(src="/tmp/nonexistent/vo.m4a", in_=0.0, out=3.0, start=0.0),
    ])
    edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30), tracks=[v1, v2, vo])
    edl.recompute_duration()

    fc, num_inputs = _capture_filter_complex_and_inputs(edl)
    referenced = {int(n) for n in re.findall(r"\[(\d+):[va]\]", fc)}
    max_valid_index = num_inputs - 1
    assert all(n <= max_valid_index for n in referenced)
