"""ONE seam rule: the xfades the compositor emits ARE the seam table.

`EDL.v1_seam_table()` (body: `edl/schema.seam_table_for`) decides which v1
seams cross-fade and by how much — `edl.duration`, `render/clock.py` (where
every non-v1 lane is placed), the desktop's `seamTable` and the benchmark all
read it. The compositor kept a second copy of the rule that disagreed with it
three ways, and every disagreement was the picture running ahead of the
overlay lanes by the difference, with the video stream ending before the
audio:

  * it xfaded with the RAW record duration while the table clamps to the
    shorter neighbour (A=2 s, B=0.3 s, fade 0.5 → video 1.8 s, audio 2.0 s;
    fade 5.0 between 2 s clips → video 2.0 s, audio 4.0 s, clip C never seen);
  * the LAST record within 0.05 s of a cut won there, the FIRST here
    (fade 0.2 @ 2.0 + fade 0.8 @ 2.03 → a 3.2 s file for a 3.8 s transport);
  * a stored 0.0 / 0.05 s resolved to the transition's default there and was
    charged raw (or not at all) here (Transition(duration=0.05) → a 3.5 s
    file, transport 3.95, every overlay after the seam 0.45 s late).

And the trailing black/silent filler was measured in mixed clocks: a LAYOUT
cursor subtracted from `edl.duration` (RENDER time), so a music bed or
sticker outliving v1 on a timeline with transitions was cut by the total
overlap (A=2, B=2, fade 0.5, bed to layout 5.0 → a 4.0 s file for a 4.5 s
transport, the sticker at layout 4.5–5.0 never on screen).

These tests render and MEASURE: ffprobe stream durations (video == audio ==
`edl.duration`), the frames a white band on the post-seam clip occupies
against `clock.render_window` of its layout window, the frames a sticker
authored on that same window occupies, and the 50 ms blocks a 1 kHz tone on
the vo lane is heard in. Measurement helpers are shared with
tests/test_transition_overlay_sync.py (pytest puts tests/ on sys.path).

WHY the audio tolerance is TWO frames where the video's is one: the AAC
encoder pads the stream to whole 1024-sample frames (21.3 ms at 48 kHz) and
ffprobe reports that padded length; measured 4.50 s of video against 4.53 s
of audio on a file whose mix was cut at exactly 4.50. That is container
rounding, not a clock error, and it is the same with or without transitions.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl.schema import (EDL, Canvas, Clip, Sticker, Track, Transform,
                                        Transition, seam_table_for)
from video_ai_editor.edl.snapshot import EDLStore
from video_ai_editor.render import clock, render_export, render_preview
from video_ai_editor.render import compositor as C
from video_ai_editor.render import transitions as T

from test_transition_overlay_sync import (  # noqa: E402  (pure helpers only)
    AUDIO_TOL, FPS, FRAME, FRAME_TOL, H, TONE_HZ, W, _band_levels, _close,
    _file_duration, _frames, _is_magenta, _lavfi, _seen_window, _tone_window,
)

#: The white band sits at these SOURCE seconds of a marked clip — past any
#: crossfade ≤ 0.5 s, so it is seen unblended when the seam is a normal one.
BAND = (0.5, 1.0)
AAC_TOL = 2 * FRAME + 0.005


# ------------------------------------------------------------------ fixtures

def _band_filter() -> str:
    return (f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
            f"enable='between(t,{BAND[0]},{BAND[1]})'")


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict[str, Path]:
    """Solid colour clips: `red2`/`red03` (2 s / 0.3 s), `blue2`/`blue03`, and
    `lime2b` / `blue2b` — 2 s clips carrying the white BAND. All silent. Plus a
    2 s 1 kHz WAV for the vo/music lanes and a canvas-sized magenta PNG."""
    d = tmp_path_factory.mktemp("seam_fx")
    silent = "anullsrc=r=48000:cl=stereo"
    for name, colour, secs, band in (("red2", "red", 2, False), ("red03", "red", 0.3, False),
                                     ("blue2", "blue", 2, False), ("blue03", "blue", 0.3, False),
                                     ("lime2b", "lime", 2, True), ("blue2b", "blue", 2, True)):
        _lavfi(d / f"{name}.mp4", video=f"color=c={colour}", audio=silent, seconds=secs,
               marker=_band_filter() if band else "")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc=0.3*sin(2*PI*{TONE_HZ}*t):s=48000:d=2",
                    "-c:a", "pcm_s16le", str(d / "tone.wav")], check=True, capture_output=True)
    Image.new("RGBA", (W, H), (255, 0, 255, 255)).save(d / "magenta.png")
    return {p.stem: p for p in d.iterdir()}


def _edl(fx: dict[str, Path], clips: list[tuple[str, float]],
         transitions: list[Transition]) -> EDL:
    """v1 with the named fixture clips back to back (`(name, seconds)`)."""
    v1 = Track(id="v1", type="video", z=0, transitions=transitions)
    cursor = 0.0
    for i, (name, secs) in enumerate(clips):
        v1.clips.append(Clip(id=f"c{i}", src=str(fx[name]), in_=0, out=secs, start=cursor))
        cursor += secs
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=[
        v1,
        Track(id="music", type="music", z=0),
        Track(id="vo", type="vo", z=0),
        Track(id="stickers", type="sticker", z=12),
    ])
    edl.recompute_duration()
    return edl


def _sticker(edl: EDL, fx, window: tuple[float, float]) -> EDL:
    s, e = window
    edl.get_track("stickers").clips.append(Sticker(
        id="st", src=str(fx["magenta"]), start=s, end=e,
        transform=Transform(x=W / 2, y=H / 2, scale=1 / 0.22)))   # covers the canvas
    edl.recompute_duration()
    return edl


def _tone(edl: EDL, fx, lane: str, window: tuple[float, float]) -> EDL:
    s, e = window
    edl.get_track(lane).clips.append(Clip(id=f"{lane}_tone", src=str(fx["tone"]),
                                          in_=0, out=e - s, start=s))
    edl.recompute_duration()
    return edl


# -------------------------------------------------------------- measurement

def _stream_durations(path: Path) -> tuple[float, float]:
    """(video stream seconds, audio stream seconds) from the container."""
    out = []
    for sel in ("v:0", "a:0"):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries",
                            "stream=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        out.append(float(r.stdout.strip()))
    return out[0], out[1]


def _is_white_band(rgb) -> bool:
    # White blended at ≥ ~20 % over red/blue/lime lifts every channel; no
    # unblended fixture frame has all three above 40 (red: g,b≈0; blue: r,g≈0;
    # lime: r,b≈0; the red→blue fade never has green).
    r, g, b = rgb
    return r >= 40 and g >= 40 and b >= 40


def _mean_rgb_at(path: Path, t: float) -> tuple[int, int, int]:
    frames = _frames(path)
    return min(frames, key=lambda fr: abs(fr[0] - t))[1]


def _export(edl: EDL, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    return render_export(edl, where, height=H).path


def _preview(edl: EDL, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    return render_preview(edl, where, height=H).path


def _assert_file_is_edl_duration(path: Path, edl: EDL) -> None:
    v, a = _stream_durations(path)
    assert abs(v - edl.duration) <= FRAME_TOL, (v, edl.duration)
    assert abs(a - edl.duration) <= AAC_TOL, (a, edl.duration)
    assert abs(_file_duration(path) - edl.duration) <= AAC_TOL


# --------------------------------------------- the cost is the clamped cost

def test_a_short_right_clip_clamps_the_xfade_to_its_length(tmp_path, fx):
    """Finding (a): A=2.0 s, B=0.3 s, fade 0.5 @ 2.0. The table says the seam
    costs 0.3, so edl.duration is 2.0 and B's whole [2.0, 2.3) lands at
    render [1.7, 2.0) — the compositor used to run xfade with 0.5 (video
    1.8 s, audio 2.0 s, B blended in at 1.5–1.8, the sticker cut short)."""
    edl = _edl(fx, [("red2", 2.0), ("blue03", 0.3)], [Transition(at=2.0, type="fade", duration=0.5)])
    assert edl.v1_seam_table() == [(2.0, pytest.approx(0.3))]
    assert edl.duration == pytest.approx(2.0)
    win = clock.render_window(edl, 2.0, 2.3)
    assert win == pytest.approx((1.7, 2.0))

    ref = _export(_tone(edl, fx, "vo", (2.0, 2.3)), tmp_path / "ref")
    _assert_file_is_edl_duration(ref, edl)
    # B is not on screen before its render window and IS blended in inside it.
    assert _mean_rgb_at(ref, 1.60)[2] <= 30
    assert _mean_rgb_at(ref, 1.95)[2] >= 100
    assert _close(_tone_window(_band_levels(ref, TONE_HZ)), win, AUDIO_TOL)

    stk = _export(_sticker(edl, fx, (2.0, 2.3)), tmp_path / "stk")
    _assert_file_is_edl_duration(stk, edl)
    assert _close(_seen_window(stk, _is_magenta), win, FRAME_TOL)


def test_a_short_left_clip_then_a_full_seam(tmp_path, fx):
    """Finding (b): A=0.3, B=2, C=2, fades 0.5 @ 0.3 and 0.5 @ 2.3. Table
    [(0.3, 0.3), (2.3, 0.5)], edl.duration 3.5. C's band at layout [2.8, 3.3)
    must be at render [2.0, 2.5) — the old renderer put it 0.2 s earlier
    because its first xfade consumed 0.5 of a 0.3 s clip."""
    edl = _edl(fx, [("red03", 0.3), ("blue2", 2.0), ("lime2b", 2.0)],
               [Transition(at=0.3, type="fade", duration=0.5),
                Transition(at=2.3, type="fade", duration=0.5)])
    assert edl.v1_seam_table() == [(pytest.approx(0.3), pytest.approx(0.3)), (pytest.approx(2.3), 0.5)]
    assert edl.duration == pytest.approx(3.5)
    band_layout = (2.3 + BAND[0], 2.3 + BAND[1])
    win = clock.render_window(edl, *band_layout)
    assert win == pytest.approx((2.0, 2.5))

    ref = _export(_tone(edl, fx, "vo", band_layout), tmp_path / "ref")
    _assert_file_is_edl_duration(ref, edl)
    assert _close(_seen_window(ref, _is_white_band), win, FRAME_TOL)
    assert _close(_tone_window(_band_levels(ref, TONE_HZ)), win, AUDIO_TOL)

    stk = _export(_sticker(edl, fx, band_layout), tmp_path / "stk")
    assert _close(_seen_window(stk, _is_magenta), win, FRAME_TOL)


def test_a_transition_longer_than_both_clips_is_clamped_to_them(tmp_path, fx):
    """Finding (d): three 2 s clips, Transition(duration=5.0) at 4.0 (the
    schema accepts it; only dispatch caps). Table cost 2.0, edl.duration
    4.0. The old renderer handed xfade 5.0: video stream 2.0 s, audio 4.0 s,
    clip C and the sticker never appeared."""
    edl = _edl(fx, [("red2", 2.0), ("blue2", 2.0), ("lime2b", 2.0)],
               [Transition(at=4.0, type="fade", duration=5.0)])
    assert edl.v1_seam_table() == [(4.0, 2.0)]
    assert edl.duration == pytest.approx(4.0)
    band_layout = (4.0 + BAND[0], 4.0 + BAND[1])
    win = clock.render_window(edl, *band_layout)
    assert win == pytest.approx((2.5, 3.0))

    ref = _export(edl, tmp_path / "ref")
    _assert_file_is_edl_duration(ref, edl)
    # Blended at progress 0.25–0.5 over B, but present exactly there.
    assert _close(_seen_window(ref, _is_white_band), win, FRAME_TOL)

    stk = _export(_sticker(edl, fx, band_layout), tmp_path / "stk")
    _assert_file_is_edl_duration(stk, edl)
    assert _close(_seen_window(stk, _is_magenta), win, FRAME_TOL)


# ------------------------------------------------ the record is the FIRST one

def test_the_first_record_at_a_stacked_cut_is_the_one_rendered(tmp_path, fx):
    """Finding 4: fade 0.2 @ 2.0 and fade 0.8 @ 2.03 (a legacy stack). The
    table charges the FIRST (0.2); the compositor's own loop had no break and
    xfaded the LAST (0.8): a 3.2 s file for a 3.8 s transport, overlays 0.6 s
    late against the picture."""
    edl = _edl(fx, [("red2", 2.0), ("blue2b", 2.0)],
               [Transition(at=2.0, type="fade", duration=0.2),
                Transition(at=2.03, type="fade", duration=0.8)])
    assert edl.v1_seam_table() == [(2.0, pytest.approx(0.2))]
    assert edl.duration == pytest.approx(3.8)
    # The filtergraph is built from the table (and the render below proves it).
    fc, *_ = C._build_filter_complex(_v1(edl), W, H, transitions=edl.get_track("v1").transitions,
                                     total_duration=4.0)
    assert "xfade=transition=fade:duration=0.2:" in fc, fc
    band_layout = (2.0 + BAND[0], 2.0 + BAND[1])
    win = clock.render_window(edl, *band_layout)
    assert win == pytest.approx((2.3, 2.8))

    out = _export(_tone(edl, fx, "vo", band_layout), tmp_path / "ref")
    _assert_file_is_edl_duration(out, edl)
    assert _close(_seen_window(out, _is_white_band), win, FRAME_TOL)
    assert _close(_tone_window(_band_levels(out, TONE_HZ)), win, AUDIO_TOL)


def _v1(edl: EDL) -> list[Clip]:
    return [c for c in edl.get_track("v1").clips if isinstance(c, Clip)]


def test_seam_table_for_is_the_body_of_v1_seam_table(fx):
    edl = _edl(fx, [("red2", 2.0), ("blue03", 0.3), ("lime2b", 2.0)],
               [Transition(at=2.0, type="fade", duration=0.5),
                Transition(at=2.3, type="fade", duration=0.2)])
    v1 = edl.get_track("v1")
    assert seam_table_for(_v1(edl), v1.transitions) == edl.v1_seam_table()
    assert edl.v1_seam_table() == [(2.0, pytest.approx(0.3)), (pytest.approx(2.3), pytest.approx(0.2))]


# ------------------------------- a stored number is a number that renders

def test_the_schema_normalises_a_duration_below_the_renderers_floor():
    """Finding 3/5: 0.0 (legacy) and 0.05 (was accepted by dispatch) both
    resolve to the transition's OWN default — `render.transitions.
    effective_duration`, the number the compositor always used — on
    construction, on JSON load and on assignment, so no reader can charge a
    number the renderer will not use."""
    assert Transition(at=2.0, type="fade", duration=0.0).duration == T.default_duration("fade")
    assert Transition(at=2.0, type="fade", duration=0.05).duration == T.default_duration("fade")
    assert Transition(at=2.0, type="zoomin", duration=0.0).duration == pytest.approx(0.3)
    assert Transition(at=2.0, type="whip", duration=0.02).duration == pytest.approx(0.25)
    tr = Transition(at=2.0, type="fade", duration=0.35)
    assert tr.duration == 0.35                                  # a real number is kept
    tr.duration = 0.02                                          # validate_assignment
    assert tr.duration == T.default_duration("fade")
    loaded = Transition.model_validate_json('{"at": 2.0, "type": "fade", "duration": 0}')
    assert loaded.duration == T.default_duration("fade")
    assert Transition(at=2.0, type="fade", duration=T.MIN_DURATION_S).duration == T.MIN_DURATION_S


def test_a_legacy_zero_duration_record_costs_what_it_renders(tmp_path, fx, monkeypatch):
    """Finding 5: Transition(duration=0) between two 2 s clips. Before: the
    export xfaded 0.5 (default) while the table returned [] — edl.duration
    4.0 for a 3.5 s file, every lane 0.5 s late, and the remux fast path
    plain-concatted v1 audio under a cross-faded picture. Now the record IS
    0.5, on the export and on the remux path alike."""
    edl = _edl(fx, [("red2", 2.0), ("blue2b", 2.0)], [Transition(at=2.0, type="fade", duration=0.0)])
    assert edl.get_track("v1").transitions[0].duration == 0.5
    assert edl.v1_seam_table() == [(2.0, 0.5)]
    assert edl.duration == pytest.approx(3.5)
    band_layout = (2.0 + BAND[0], 2.0 + BAND[1])
    win = clock.render_window(edl, *band_layout)
    assert win == pytest.approx((2.0, 2.5))
    edl = _tone(edl, fx, "vo", band_layout)

    out = _export(edl, tmp_path / "exp")
    _assert_file_is_edl_duration(out, edl)
    assert _close(_seen_window(out, _is_white_band), win, FRAME_TOL)
    assert _close(_tone_window(_band_levels(out, TONE_HZ)), win, AUDIO_TOL)

    # Preview, then an audio-only edit → the remux fast path.
    first = _preview(edl, tmp_path / "prev")
    _assert_file_is_edl_duration(first, edl)
    calls: list[Path] = []
    real = C._remux_with_new_audio

    def spy(edl_, video_only, dst, **kw):
        calls.append(video_only)
        return real(edl_, video_only, dst, **kw)
    monkeypatch.setattr(C, "_remux_with_new_audio", spy)
    edl.get_track("vo").clips[0].audio.gain_db = 1.0
    second = _preview(edl, tmp_path / "prev")
    assert calls, "the audio-only change did not take the remux fast path"
    _assert_file_is_edl_duration(second, edl)
    assert _close(_tone_window(_band_levels(second, TONE_HZ)), win, AUDIO_TOL)


def test_dispatch_stores_the_duration_the_renderer_will_use(tmp_path, fx):
    """Finding 1/3 (dispatch): `add_transition` floors at MIN_DURATION_S (→
    the look's default), caps at MAX_DURATION_S and clamps to the shorter
    neighbour, so the stored number is the rendered one; then the export of
    the 0.05 case is exactly edl.duration with B's band on the clock."""
    store = EDLStore(tmp_path)
    v1 = store.edl.get_track("v1")
    v1.clips.extend([Clip(id="a", src=str(fx["red2"]), in_=0, out=2, start=0),
                     Clip(id="b", src=str(fx["blue2b"]), in_=0, out=2, start=2),
                     Clip(id="c", src=str(fx["blue03"]), in_=0, out=0.3, start=4)])
    store.edl.canvas = Canvas(w=W, h=H, fps=FPS, loudness_lufs=None)
    store.edl.recompute_duration()

    r = dispatch(store, "add_transition", {"at": 2.0, "type": "fade", "duration": 0.05})
    assert r["transition_duration"] == T.default_duration("fade") == 0.5
    assert store.edl.duration == pytest.approx(3.8)                       # 4.3 layout − 0.5
    r = dispatch(store, "add_transition", {"at": 2.0, "type": "fade", "duration": 5.0})
    assert r["transition_duration"] == T.MAX_DURATION_S                  # capped, 2 s neighbours
    r = dispatch(store, "add_transition", {"at": 4.0, "type": "fade", "duration": 1.0})
    assert r["transition_duration"] == pytest.approx(0.3)                # the 0.3 s neighbour
    r = dispatch(store, "add_transition", {"at": 9.0, "type": "fade", "duration": 5.0})
    assert r["transition_duration"] == T.MAX_DURATION_S                  # no seam: cap only
    dispatch(store, "remove_transition", {"all": True})

    dispatch(store, "add_transition", {"at": 2.0, "type": "fade", "duration": 0.05})
    edl = store.edl
    assert edl.v1_seam_table() == [(2.0, 0.5)]
    band_layout = (2.0 + BAND[0], 2.0 + BAND[1])
    win = clock.render_window(edl, *band_layout)
    out = _export(edl, tmp_path / "exp")
    _assert_file_is_edl_duration(out, edl)
    assert _close(_seen_window(out, _is_white_band), win, FRAME_TOL)


# --------------------------------------- the tail is measured in layout time

@pytest.mark.parametrize("path", ["export", "preview"])
def test_a_lane_past_v1s_end_renders_to_edl_duration(tmp_path, fx, path, monkeypatch):
    """Finding 2: A=2, B=2, fade 0.5, a bed at layout [0, 5) and a sticker at
    layout [4.5, 5.0) → edl.duration 4.5, clock: bed [0, 4.5), sticker
    [4.0, 4.5). The file used to be 4.0 s (tail = edl.duration − layout
    cursor), with the bed's last 0.45 s cut and the sticker never shown."""
    edl = _edl(fx, [("red2", 2.0), ("blue2", 2.0)], [Transition(at=2.0, type="fade", duration=0.5)])
    edl.get_track("music").clips.append(Clip(id="bed", src=str(fx["tone"]), in_=0, out=2, start=0))
    edl.get_track("music").clips.append(Clip(id="bed2", src=str(fx["tone"]), in_=0, out=2, start=2))
    edl.get_track("music").clips.append(Clip(id="bed3", src=str(fx["tone"]), in_=0, out=1, start=4))
    edl = _sticker(edl, fx, (4.5, 5.0))
    assert edl.duration == pytest.approx(4.5)
    assert clock.render_window(edl, 0.0, 5.0) == pytest.approx((0.0, 4.5))
    stk_win = clock.render_window(edl, 4.5, 5.0)
    assert stk_win == pytest.approx((4.0, 4.5))

    render = _export if path == "export" else _preview
    out = render(edl, tmp_path / path)
    _assert_file_is_edl_duration(out, edl)
    heard = _tone_window(_band_levels(out, TONE_HZ))
    assert heard is not None and abs(heard[1] - 4.5) <= AUDIO_TOL, heard
    if path == "export":            # the preview leaves stickers to the browser
        assert _close(_seen_window(out, _is_magenta), stk_win, FRAME_TOL)
    else:
        # The remux fast path pads its v1 audio to the same layout end.
        calls: list[Path] = []
        real = C._remux_with_new_audio

        def spy(edl_, video_only, dst, **kw):
            calls.append(video_only)
            return real(edl_, video_only, dst, **kw)
        monkeypatch.setattr(C, "_remux_with_new_audio", spy)
        edl.get_track("music").clips[0].audio.gain_db = 1.0
        second = _preview(edl, tmp_path / path)
        assert calls
        _assert_file_is_edl_duration(second, edl)
        heard = _tone_window(_band_levels(second, TONE_HZ))
        assert heard is not None and abs(heard[1] - 4.5) <= AUDIO_TOL, heard
