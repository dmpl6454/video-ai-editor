"""Speed must change a clip's TIMELINE footprint (CapCut semantics).

The EDL treated `duration` (source seconds) as timeline time everywhere, so
a 2x clip rendered 5s of video while the timeline drew 10s, `edl.duration`
said 10s, and the transport total never changed — the tester's exact report.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Track, Clip, Canvas, Sticker, TextClip, Transition
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.render import render_preview


def _mk_video(path: Path, *, duration: float = 5.0, color: str = "blue"):
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)


@pytest.fixture
def store(tmp_path: Path) -> EDLStore:
    a = tmp_path / "a.mp4"; b = tmp_path / "b.mp4"
    _mk_video(a); _mk_video(b, color="red")
    s = EDLStore(tmp_path)
    s.edl.get_track("v1").clips = [
        Clip(src=str(a), in_=0, out=5, start=0, id="c1"),
        Clip(src=str(b), in_=0, out=5, start=5, id="c2"),
    ]
    s.edl.recompute_duration()
    return s


def test_effective_duration_property():
    c = Clip(src="x.mp4", in_=0, out=6, start=0, id="c")
    assert c.effective_duration == 6.0
    c.speed = 2.0
    assert c.effective_duration == 3.0
    c.speed = 0.5
    assert c.effective_duration == 12.0
    # Curve dicts (schema-only today) fall back to source duration.
    c.speed = {"curve": [[0, 1.0], [3, 2.0]]}
    assert c.effective_duration == 6.0


def test_set_speed_retimes_timeline(store: EDLStore):
    """2x on clip 1: its footprint halves, clip 2 ripples left, total drops."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    edl = store.edl
    c1, c2 = edl.get_track("v1").clips
    assert c1.effective_duration == pytest.approx(2.5)
    assert c2.start == pytest.approx(2.5), "clip 2 must ripple to the new end"
    assert edl.duration == pytest.approx(7.5)


def test_set_speed_slowdown_pushes_right(store: EDLStore):
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 0.5})
    edl = store.edl
    c2 = edl.get_track("v1").clips[1]
    assert c2.start == pytest.approx(10.0)
    assert edl.duration == pytest.approx(15.0)


def test_set_speed_ripples_overlays(store: EDLStore):
    """A caption sitting over clip 2 must move with it when clip 1 speeds up."""
    dispatch(store, "add_text", {"text": "over c2", "start": 6.0, "end": 8.0,
                                 "role": "super", "allow_stack": True})
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    tx = None
    for t in store.edl.tracks:
        for c in t.clips:
            if getattr(c, "text", None) == "over c2":
                tx = c
    assert tx is not None
    assert tx.start == pytest.approx(3.5), "overlay must shift left by 2.5s"
    assert tx.end == pytest.approx(5.5)


def test_rendered_duration_matches_edl_duration(store: EDLStore):
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    out = render_preview(store.edl, store.session_dir
                         if hasattr(store, "session_dir") else store.edl_path.parent,
                         height=180).path
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    rendered = float(probe.stdout.strip())
    assert rendered == pytest.approx(store.edl.duration, abs=0.35), (
        f"rendered {rendered}s vs edl.duration {store.edl.duration}s")


def test_move_clip_rejects_sped_clip_onto_audio_lane(store: EDLStore):
    """The second ingress for the audio-lane geometry lie: moving an
    already-sped v1 clip onto music must be rejected (no atempo there)."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    with pytest.raises(ValueError, match="reset speed"):
        dispatch(store, "move_clip", {"clip_id": "c1", "new_start": 0.0,
                                      "new_track": "music"})
    with pytest.raises(ValueError, match="reset speed"):
        dispatch(store, "move_clip", {"clip_id": "c1", "new_start": 0.0,
                                      "new_track": "v2"})
    # Normal-speed clips still move freely.
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 1.0})
    dispatch(store, "move_clip", {"clip_id": "c1", "new_start": 0.0,
                                  "new_track": "v2"})
    assert any(c.id == "c1" for c in store.edl.get_track("v2").clips)


def test_cut_range_on_sped_clip_uses_effective_time(store: EDLStore):
    """Cutting timeline [1.0, 1.5) out of a 2x clip removes 1.0s of SOURCE
    (0.5 timeline-seconds * 2x), leaving a 2.0s-footprint clip pair."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    # c1 now occupies [0, 2.5); cut [1.0, 1.5) from inside it.
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 1.5})
    clips = store.edl.get_track("v1").clips
    left, right = clips[0], clips[1]
    assert left.out == pytest.approx(2.0)      # 1.0 timeline * 2x source
    assert right.in_ == pytest.approx(3.0)     # 1.5 timeline * 2x source
    assert right.effective_duration == pytest.approx(1.0)
    # Timeline repacked: 1.0 + 1.0 + 5.0 (clip c2) = 7.0 total
    assert store.edl.duration == pytest.approx(7.0)


def test_cut_range_ids_stay_unique(store: EDLStore):
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 1.5})
    dispatch(store, "cut_range", {"track": "v1", "start": 0.5, "end": 0.7})
    ids = [c.id for c in store.edl.get_track("v1").clips]
    assert len(set(ids)) == len(ids), f"duplicate ids after cuts: {ids}"


def test_split_at_on_sped_clip_uses_effective_time(store: EDLStore):
    """Splitting a 2x clip at timeline t=1.0 must cut 2.0s into the SOURCE."""
    dispatch(store, "set_speed", {"clip_id": "c1", "factor": 2.0})
    dispatch(store, "split_at", {"track": "v1", "time": 1.0})
    clips = store.edl.get_track("v1").clips
    left = clips[0]
    assert left.out == pytest.approx(2.0), (
        "left half must contain 2.0 source-seconds (1.0 timeline * 2x)")
    assert clips[1].start == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Consumers of the effective_duration contract (tester round-2 regressions).
# Each test below encodes a failure reproduced live on the pre-fix branch —
# the repro scripts printed the buggy values quoted in the docstrings, so
# these were red before the corresponding dispatch/compositor fix.
# ---------------------------------------------------------------------------

def _fast_store(tmp_path: Path) -> EDLStore:
    """In-memory-only store (no real media; these tests never render)."""
    return EDLStore(tmp_path)


def test_set_speed_rejected_on_v2_pip(tmp_path: Path):
    """set_speed on a non-v1 video track must ValueError, not commit.

    render/pip.py applies no setpts, so speed on a v2 clip is fiction — and
    the pre-fix code additionally ran _ripple_close_gap on the v2 track,
    repacking deliberately-gapped PIP placements from t=0 (8.0/20.0 became
    0.0/2.0 in the live repro)."""
    s = _fast_store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="c_pip1", src="/x/a.mp4", in_=0, out=4, start=8.0))
    v2.clips.append(Clip(id="c_pip2", src="/x/b.mp4", in_=0, out=4, start=20.0))
    s.commit("seed", {}, "seed")
    with pytest.raises(ValueError, match="main video track"):
        dispatch(s, "set_speed", {"clip_id": "c_pip1", "factor": 2.0})
    assert v2.clips[0].start == pytest.approx(8.0), "PIP placement must survive"
    assert v2.clips[1].start == pytest.approx(20.0)
    assert v2.clips[0].speed is None, "rejected call must not commit the field"


def test_set_speed_rejected_on_audio_lanes(tmp_path: Path):
    """set_speed on audio/music/vo clips must ValueError: audio_mix applies
    no atempo, so a committed speed field never changes playback but DOES
    shrink effective_duration/edl.duration (live repro: a 10s music clip
    'became' 5s on the timeline while still playing all 10s)."""
    s = _fast_store(tmp_path)
    music = s.edl.get_track("music")
    music.clips.append(Clip(id="c_music", src="/song.mp3", in_=0, out=10, start=0.0))
    s.commit("seed", {}, "seed")
    with pytest.raises(ValueError, match="audio lane"):
        dispatch(s, "set_speed", {"clip_id": "c_music", "factor": 2.0})
    c = s.edl.get_clip("c_music")[1]
    assert c.speed is None
    assert s.edl.duration == pytest.approx(10.0)


def test_ripple_delete_sped_clip_shifts_overlays_by_effective_duration(tmp_path: Path):
    """Deleting a 2x clip (source 10s, footprint 5s) must shift later
    overlays by 5s, not 10s. Pre-fix, removed_end used source duration: a
    sticker at 7.0 collapsed to 0.0 instead of landing at 2.0."""
    s = _fast_store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    v1.clips.append(Clip(id="c_B", src="/x/b.mp4", in_=0, out=5, start=5.0))
    st = s.edl.get_track("stickers")
    st.clips.append(Sticker(id="s_1", src="/x/s.png", start=7.0, end=8.0))
    s.commit("seed", {}, "seed")
    dispatch(s, "ripple_delete", {"clip_id": "c_A"})
    assert v1.clips[0].start == pytest.approx(0.0)
    assert st.clips[0].start == pytest.approx(2.0)
    assert st.clips[0].end == pytest.approx(3.0)


def test_trim_clip_back_trim_sped_ripples_overlays_in_timeline_coords(tmp_path: Path):
    """Back-trim out 10->8 on a 2x clip removes timeline [4,5) (1s), so a
    sticker at 6.0 slides to 5.0. Pre-fix the removal interval was source-
    based [8,10): the sticker (at 6.0 < 8.0) never moved -> 1s desync."""
    s = _fast_store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    v1.clips.append(Clip(id="c_B", src="/x/b.mp4", in_=0, out=5, start=5.0))
    st = s.edl.get_track("stickers")
    st.clips.append(Sticker(id="s_1", src="/x/s.png", start=6.0, end=7.0))
    s.commit("seed", {}, "seed")
    dispatch(s, "trim_clip", {"clip_id": "c_A", "out": 8.0})
    assert v1.clips[1].start == pytest.approx(4.0)
    assert st.clips[0].start == pytest.approx(5.0)
    assert st.clips[0].end == pytest.approx(6.0)


def test_trim_clip_front_trim_sped_ripples_overlays_in_timeline_coords(tmp_path: Path):
    """Front-trim in 0->2 on a 2x clip removes timeline [0,1) (2 source-sec
    / 2x), so a sticker at 6.0 slides to 5.0. Pre-fix the interval was
    [0,2) timeline -> the sticker over-shifted to 4.0."""
    s = _fast_store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    v1.clips.append(Clip(id="c_B", src="/x/b.mp4", in_=0, out=5, start=5.0))
    st = s.edl.get_track("stickers")
    st.clips.append(Sticker(id="s_1", src="/x/s.png", start=6.0, end=7.0))
    s.commit("seed", {}, "seed")
    dispatch(s, "trim_clip", {"clip_id": "c_A", "in": 2.0})
    assert v1.clips[1].start == pytest.approx(4.0)
    assert st.clips[0].start == pytest.approx(5.0)
    assert st.clips[0].end == pytest.approx(6.0)


def test_trim_positions_differ_front_vs_back(tmp_path: Path):
    """CLAUDE.md's warning holds under speed: for a clip at start=5,
    source 10s @ 2x (footprint [5,10)) trimmed to source 6s, the removed
    TIMELINE interval is [5,7) from the front but [8,10) from the back —
    verified via an overlay placed between the two candidate windows."""
    for trim_args, expected in ((({"in": 4.0}), 5.5), (({"out": 6.0}), 7.5)):
        s = _fast_store(tmp_path / f"t_{'in' if 'in' in trim_args else 'out'}")
        v1 = s.edl.get_track("v1")
        v1.clips.append(Clip(id="c_L", src="/x/l.mp4", in_=0, out=5, start=0.0))
        v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=5.0, speed=2.0))
        st = s.edl.get_track("stickers")
        # Overlay at 7.5 — inside the back-trim window [8,10)? No: before it.
        # After a front-trim (removes [5,7)) it shifts left by 2 -> 5.5.
        # After a back-trim (removes [8,10)) it is before the window -> stays 7.5.
        st.clips.append(Sticker(id="s_1", src="/x/s.png", start=7.5, end=7.8))
        s.commit("seed", {}, "seed")
        dispatch(s, "trim_clip", {"clip_id": "c_A", **trim_args})
        assert st.clips[0].start == pytest.approx(expected), (
            f"trim {trim_args}: overlay expected at {expected}, got {st.clips[0].start}")


def test_remove_silences_converts_source_offsets_on_sped_clip(tmp_path: Path):
    """A silence at SOURCE [2,4) inside a 2x clip plays at TIMELINE [1,2);
    remove_silences must emit cut_range(1,2) so the surviving source is
    [0,2)+[4,10). Pre-fix it emitted cut_range(2,4), cutting source [4,8) —
    the wrong content, twice over. Silencedetect is stubbed by monkeypatching
    subprocess.run so no real audio is needed."""
    s = _fast_store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    s.commit("seed", {}, "seed")

    class _FakeProc:
        returncode = 0
        stdout = ""
        # keep_pad=0.1 default: local [2.1, 3.9] after padding; min_dur must
        # pass, so report a full 2s window.
        stderr = "silence_start: 2.0\nsilence_end: 4.0\n"

    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if argv and str(argv[0]).endswith(("ffmpeg", "ffmpeg.exe")) and "silencedetect" in " ".join(map(str, argv)):
            return _FakeProc()
        return real_run(argv, *a, **kw)

    import unittest.mock as mock
    # remove_silences does `import re, subprocess` inside the function body,
    # so patching the stdlib module attribute covers it.
    with mock.patch("subprocess.run", fake_run):
        dispatch(s, "remove_silences", {"track": "v1", "keep_pad": 0.0, "min_dur": 0.5})

    srcs = sorted((c.in_, c.out) for c in v1.clips if isinstance(c, Clip))
    assert srcs == [(0.0, 2.0), (4.0, 10.0)], (
        f"surviving source ranges {srcs}: the silence at source [2,4) must be "
        "what was removed (timeline [1,2) on a 2x clip)")


def test_first_free_gap_and_move_clip_use_effective_duration(tmp_path: Path):
    """A 2x clip (source 10s) occupies timeline [0,5): moving another clip
    to start=6 must succeed without snapping. Pre-fix _first_free_gap
    treated it as occupying [0,10) and pushed the drop to 10.0."""
    s = _fast_store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="c_fast", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    v2.clips.append(Clip(id="c_other", src="/x/b.mp4", in_=0, out=3, start=30.0))
    s.commit("seed", {}, "seed")
    dispatch(s, "move_clip", {"clip_id": "c_other", "new_start": 6.0})
    c = next(c for c in v2.clips if c.id == "c_other")
    assert c.start == pytest.approx(6.0), "timeline [6,9) is genuinely free"


def test_move_clip_passes_moved_clips_effective_width(tmp_path: Path):
    """The MOVED clip's width is its effective_duration: a 2x clip (source
    6s, footprint 3s) fits a 3.5s gap; passing source duration (6s) would
    false-positive a collision and snap past it."""
    s = _fast_store(tmp_path)
    v2 = s.edl.get_track("v2")
    v2.clips.append(Clip(id="c_a", src="/x/a.mp4", in_=0, out=2, start=0.0))       # [0,2)
    v2.clips.append(Clip(id="c_b", src="/x/b.mp4", in_=0, out=4, start=5.5))       # [5.5,9.5)
    v2.clips.append(Clip(id="c_fast", src="/x/f.mp4", in_=0, out=6, start=20.0, speed=2.0))  # fp 3s
    s.commit("seed", {}, "seed")
    dispatch(s, "move_clip", {"clip_id": "c_fast", "new_start": 2.0})
    c = next(c for c in v2.clips if c.id == "c_fast")
    assert c.start == pytest.approx(2.0), (
        "3s-footprint clip fits the [2,5.5) gap; source-width math snapped it away")


def test_duplicate_clip_places_copy_after_effective_footprint(tmp_path: Path):
    """duplicate of a 2x clip (footprint 2.5s at [0,2.5)) starts at 2.5,
    not at source-duration 5.0."""
    s = _fast_store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=5, start=0.0, speed=2.0))
    s.commit("seed", {}, "seed")
    res = dispatch(s, "duplicate_clip", {"clip_id": "c_A"})
    dup = s.edl.get_clip(res["new_clip_id"])[1]
    assert dup.start == pytest.approx(2.5)
    assert s.edl.duration == pytest.approx(5.0)


def test_get_timeline_reports_effective_and_source_duration(tmp_path: Path):
    """Per-clip `duration` is TIMELINE seconds (what start+duration
    arithmetic needs — Claude's coordinates, loop.py's _clip_line span);
    `source_duration` preserves the raw out-in."""
    s = _fast_store(tmp_path)
    v1 = s.edl.get_track("v1")
    v1.clips.append(Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0))
    v1.clips.append(Clip(id="c_B", src="/x/b.mp4", in_=0, out=5, start=5.0))
    s.commit("seed", {}, "seed")
    snap = dispatch(s, "get_timeline", {"summary": True})
    t = next(t for t in snap["tracks"] if t["id"] == "v1")
    a, b = t["clips"]
    assert a["duration"] == pytest.approx(5.0), "2x clip occupies 5 timeline-s"
    assert a["source_duration"] == pytest.approx(10.0)
    assert b["duration"] == pytest.approx(5.0)
    assert b["source_duration"] == pytest.approx(5.0)
    assert snap["duration"] == pytest.approx(10.0)


def test_compositor_transition_matches_after_sped_clip(tmp_path: Path):
    """A transition at the visible boundary (timeline 5.0) after a 2x clip
    must produce an xfade in the real compositor graph. Pre-fix the matcher
    summed SOURCE durations (running=10.0), never matched tr.at=5.0, and
    silently rendered a hard cut."""
    from video_ai_editor.render.compositor import _build_filter_complex

    clips = [
        Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0),
        Clip(id="c_B", src="/x/b.mp4", in_=0, out=5, start=5.0),
    ]
    transitions = [Transition(at=5.0, type="fade", duration=0.5)]
    fc, _inputs, labels, _extra = _build_filter_complex(
        clips, 320, 180, transitions=transitions)
    assert "xfade" in fc, "transition after a sped clip must not silently drop"
    # Offset math must be self-consistent with effective lengths: the left
    # stream is 5.0s post-setpts, so the xfade starts at 5.0 - 0.5 = 4.5.
    assert "offset=4.500" in fc, fc


def test_compositor_xfade_offsets_accumulate_effective_durations(tmp_path: Path):
    """Three clips, two transitions: cur_dur after the first xfade must be
    effective-based (5.0 + 5.0 - 0.5 = 9.5), so the second xfade's offset is
    9.5 - 0.5 = 9.0 — source-based accumulation would put it at 14.0."""
    from video_ai_editor.render.compositor import _build_filter_complex

    clips = [
        Clip(id="c_A", src="/x/a.mp4", in_=0, out=10, start=0.0, speed=2.0),  # eff 5
        Clip(id="c_B", src="/x/b.mp4", in_=0, out=5, start=5.0),              # eff 5
        Clip(id="c_C", src="/x/c.mp4", in_=0, out=5, start=10.0),             # eff 5
    ]
    transitions = [
        Transition(at=5.0, type="fade", duration=0.5),
        Transition(at=10.0, type="fade", duration=0.5),
    ]
    fc, *_ = _build_filter_complex(clips, 320, 180, transitions=transitions)
    assert fc.count("xfade") == 2, "both transitions must match"
    assert "offset=4.500" in fc
    assert "offset=9.000" in fc, fc


def test_render_behavior_version_bumped_for_text_transform_and_z_order():
    """v4: text transform.x/y + style.size/stroke now render and sticker
    z-order sorts — unchanged EDL bytes must produce a different cache key."""
    from video_ai_editor.edl.schema import RENDER_BEHAVIOR_VERSION
    assert RENDER_BEHAVIOR_VERSION >= 4
