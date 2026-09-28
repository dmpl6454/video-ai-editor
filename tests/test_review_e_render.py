"""Wave E review fixes (the RE fixer) that need a real render: every test
decodes what ffmpeg wrote.

  * renders read a PRIVATE snapshot of the EDL (an edit during a render can
    neither fail it nor be cached under the wrong hash);
  * (more below as the render fixes land).
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu


def _ff(dst: Path, *args: str) -> Path:
    if not dst.exists():
        subprocess.run([_pu.FFMPEG, "-nostdin", "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


def nframes(path) -> int:
    out = subprocess.run([_pu.FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip().rstrip(","))


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("re_render")
    return _ff(d / "clip.mp4", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=8",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-shortest",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    from video_ai_editor import config as _config, main as _main, storage as _storage
    wd = tmp_path / "wd"
    wd.mkdir()
    for mod in (_config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd)
    before = _config._FORCED_RESTRICT
    _config.enable_path_restriction(False)
    _main._STORES.clear()
    yield TestClient(_main.app), _main
    _config.enable_path_restriction(before)


def _timeline(client: TestClient, clip: Path) -> str:
    sid = client.post("/api/sessions", json={"name": "race"}).json()["id"]
    def d(tool, **args):
        r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
        assert r.status_code == 200, r.text
    d("set_canvas", w=160, h=90)
    for i in range(3):
        d("add_clip", track="v1", src=str(clip), **{"in": 2.0 * i, "out": 2.0 * i + 2.0}, start=2.0 * i)
    return sid


def _edit_during_render(monkeypatch, main_mod, sid: str):
    """Land a ripple_delete on the LIVE store while the render is between its
    ffmpeg run and its frame-count check — the window the re-tester hit."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.render import compositor
    real = compositor._check_picture
    fired = threading.Event()

    def check(edl, path, fps):
        if not fired.is_set():
            fired.set()
            store = main_mod._STORES[sid]
            victim = store.edl.get_track("v1").clips[1].id
            dispatch(store, "ripple_delete", {"clip_id": victim})
        return real(edl, path, fps)
    monkeypatch.setattr(compositor, "_check_picture", check)
    return fired


def test_an_edit_during_an_export_neither_fails_it_nor_changes_what_it_rendered(api, clip, monkeypatch):
    """review RE: the export read `store.edl` by reference; ripple_delete
    mutated it mid-render and the (fatal) frame-count check planned against
    the edited timeline — 'render has 660 video frames, the timeline plans
    480'. It renders the timeline as it was when Export was pressed."""
    client, main_mod = api
    sid = _timeline(client, clip)
    fired = _edit_during_render(monkeypatch, main_mod, sid)
    before = main_mod._STORES[sid].edl.hash()
    r = client.post(f"/api/sessions/{sid}/export", json={"height": 90})
    assert fired.is_set()
    assert r.status_code == 200, r.text
    assert nframes(r.json()["path"]) == 180                  # 6 s at 30 fps: the timeline at Export
    assert main_mod._STORES[sid].edl.hash() != before       # the edit itself landed


def test_an_edit_during_a_preview_is_not_cached_under_the_old_hash(api, clip, monkeypatch):
    """review RE: the file cached under the PRE-edit hash held the EDITED
    timeline's frames, and Undo then served it ('cached: true, 480
    frames')."""
    client, main_mod = api
    sid = _timeline(client, clip)
    fired = _edit_during_render(monkeypatch, main_mod, sid)
    h0 = main_mod._preview_edl(main_mod._STORES[sid]).render_hash()
    r = client.post(f"/api/sessions/{sid}/preview")
    assert fired.is_set()
    assert r.status_code == 200, r.text
    cached = main_mod._STORES[sid].dir / "previews" / f"{h0}.mp4"
    assert cached.is_file() and nframes(cached) == 180


def test_a_frame_count_refusal_says_what_happened():
    """review RE: the export was told 'Couldn't render a preview for this
    clip — it may have corrupt frames or an unusual codec'."""
    from video_ai_editor.main import _render_failure_message
    msg = ("RuntimeError: The render came out 660 frames long where the timeline plans 480: it would not "
           "match the editor frame for frame, so it was not kept.")
    got = _render_failure_message(msg[-400:], msg, kind="export")
    assert "export came out 660 frames" in got and "corrupt" not in got and "preview" not in got
    assert "preview came out" in _render_failure_message(msg, msg)
    assert "export" in _render_failure_message("random ffmpeg noise", kind="export")


# ================================================================ sub-frame reversed clips

import sys  # noqa: E402
from fractions import Fraction  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import frame_map_golden_lib as G  # noqa: E402

from video_ai_editor.edl.schema import Canvas, Clip, empty_edl  # noqa: E402
from video_ai_editor.render import compositor, render_preview  # noqa: E402
from video_ai_editor.render.frame_map import planned_frames  # noqa: E402


@pytest.fixture(scope="module")
def bars(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("re_bars")
    out = {}
    for key, sid, rate in (("b30", 1, Fraction(30)), ("b24", 2, Fraction(24)), ("b25", 3, Fraction(25)),
                           ("b2997", 4, Fraction(30000, 1001))):
        out[key] = G.make_bar_source(d / f"{key}.mp4", G.SourceSpec(key=key, sid=sid, rate=rate, seconds=10.0))
    return out


def _codes(path: Path) -> list[int]:
    return [c & ((1 << G.FRAME_BITS) - 1) for c in G.measure(path)["top"]]


def _one_frame_reversed(src: Path) -> "EDL":  # noqa: F821
    e = empty_edl(Canvas(w=G.W, h=G.H, fps=29.97))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    a = Clip(src=str(src), id="a", in_=0.0, out=0.3, start=0.0)
    b = Clip(src=str(src), id="b", in_=1.0, out=1.0 + 1 / 30, start=0.3, speed=2.0, reverse=True)
    c = Clip(src=str(src), id="c", in_=2.0, out=2.5, start=0.3 + b.effective_duration)
    v1.clips = [a, b, c]
    e.recompute_duration()
    return e


def test_a_one_frame_reversed_2x_clip_keeps_its_picture_on_every_path(bars, tmp_path):
    """review RE: a reversed, 2x clip one source frame long (0.0167 s, under
    half a 29.97 frame) got ZERO frames from the chain: single pass slid the
    next clip a frame early, the chunked preview had a picture-less chunk and
    — the frame-count check being fatal — failed outright ('24 frames where
    the timeline plans 25')."""
    e = _one_frame_reversed(bars["b30"])
    want = planned_frames(e)
    assert want == 25
    single = compositor._render(e, tmp_path / "single.mp4", height=G.H, fps=29.97, preview=False,
                                cache_dir=tmp_path / "c1", chunked=False)
    chunked = compositor._render(e, tmp_path / "chunked.mp4", height=G.H, fps=29.97, preview=False,
                                 cache_dir=tmp_path / "c2", chunked=True)
    sess = tmp_path / "sess"
    sess.mkdir()
    preview = render_preview(e, sess).path
    for p in (single, chunked, preview):
        codes = _codes(p)
        assert len(codes) == want, (p.name, len(codes))
        # frame 9 is the reversed clip: source frame 30; frame 10 is clip c's first (60)
        assert codes[8:11] == [8, 30, 60], (p.name, codes[6:13])


# ================================================================ mixed-rate splits

@pytest.mark.parametrize("src,fps,in_,at", [
    ("b30", 25, 0.52, 1.48), ("b30", 25, 0.2, 0.9), ("b24", 30, 0.0, 1.2), ("b24", 30, 0.2, 0.9),
    ("b25", 29.97, 0.52, 1.48), ("b25", 29.97, 0.2, 0.9), ("b30", 24, 0.0, 1.2), ("b30", 24, 0.52, 1.48),
    ("b2997", 30, 0.2, 0.9),
])
def test_a_split_of_a_mixed_rate_clip_is_frame_exact(bars, tmp_path, src, fps, in_, at):
    """review RE (INSTANT_PREVIEW_SPEC 'Split: the frames are identical'): a
    source at another rate than the project's rebased at its first DECODED
    frame, so a split's right piece rounded against a different offset —
    30→25 in 0.52 split at 1.48: 22 frames one source frame off; 24→30 in
    0.2: 37. The chain now runs on the file clock anchored at `in`."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl import EDLStore
    e = empty_edl(Canvas(w=G.W, h=G.H, fps=fps))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips = [Clip(src=str(bars[src]), id="c", in_=in_, out=in_ + 3.0, start=0.0)]
    e.recompute_duration()
    whole = _codes(compositor._render(e, tmp_path / "whole.mp4", height=G.H, fps=fps, preview=False,
                                      cache_dir=tmp_path / "c", chunked=False))
    st = EDLStore(tmp_path / "s")
    st.edl = e.model_copy(deep=True)
    st.commit("init", {}, "init")
    dispatch(st, "split_at", {"track": "v1", "time": at})
    assert len(st.edl.get_track("v1").clips) == 2
    split = _codes(compositor._render(st.edl, tmp_path / "split.mp4", height=G.H, fps=fps, preview=False,
                                      cache_dir=tmp_path / "c", chunked=False))
    assert len(split) == len(whole) == planned_frames(e)
    diff = [(k, a, b) for k, (a, b) in enumerate(zip(whole, split)) if a != b]
    assert diff == [], diff[:6]


def test_a_split_quantises_the_timeline_offset_not_the_source_time(bars, tmp_path):
    """review RE: with `in` off the project grid the cut was snapped on the
    ABSOLUTE source clock — 25 fps, in 0.5, split at 1.2 gave pieces
    (in 0.5, out 1.68) and (in 1.7…) at 1.2: a gap frame, 100 → 101."""
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl import EDLStore
    e = empty_edl(Canvas(w=G.W, h=G.H, fps=25))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips = [Clip(src=str(bars["b25"]), id="c", in_=0.5, out=4.5, start=0.0)]
    e.recompute_duration()
    st = EDLStore(tmp_path / "s")
    st.edl = e
    st.commit("init", {}, "init")
    dispatch(st, "split_at", {"track": "v1", "time": 1.2})
    left, right = st.edl.get_track("v1").clips
    assert abs(left.out - 1.7) < 1e-9 and abs(right.in_ - 1.7) < 1e-9 and right.start == 1.2
    assert abs(left.effective_duration - 1.2) < 1e-9
    assert planned_frames(st.edl) == 100


# ================================================================ voice effects across a seam

import numpy as np  # noqa: E402

from video_ai_editor.edl.schema import AudioProps  # noqa: E402


@pytest.fixture(scope="module")
def tone(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("re_tone")
    return _ff(d / "tone.mov", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=30:d=4",
               "-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*220*t)+0.2*sin(2*PI*1250*t)':s=48000:d=4",
               "-c:v", "libx264", "-c:a", "pcm_s16le", "-shortest")


def _sound(clips) -> np.ndarray:
    e = empty_edl(Canvas(w=64, h=36, fps=30))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips = list(clips)
    e.recompute_duration()
    inputs, fc, label = compositor._audio_only_graph(e, fps=30, first_input=0, apply_loudnorm=False)
    p = subprocess.run([_pu.FFMPEG, "-v", "error", *inputs, "-filter_complex", fc, "-map", label,
                        "-f", "f32le", "-ac", "2", "-ar", "48000", "-"], capture_output=True, check=True)
    return np.frombuffer(p.stdout, "<f4").reshape(-1, 2)[:, 0].astype(np.float64)


@pytest.mark.parametrize("effect", ["chipmunk", "deep", "monster", "vibrato", "underwater"])
def test_a_voice_effect_leaves_no_silence_at_a_split_seam(tone, effect):
    """review RE: the pitch presets and Vibrato / Underwater started EVERY
    clip with 5-19 ms of digital silence (the atempo lag and the empty delay
    line), so a split seam had a hole mid-word (-224 dB for 10-15 ms). The
    effect is now primed with the sound before `in`: no 1 ms window after the
    seam is more than 6 dB below the unsplit clip."""
    def mk(cid, start, i, o):
        return Clip(src=str(tone), id=cid, start=start, in_=i, out=o, audio=AudioProps(voice_effect=effect))
    whole = _sound([mk("a", 0.0, 0.3, 2.3)])
    c1 = mk("a", 0.0, 0.3, 1.3)
    split = _sound([c1, mk("b", c1.effective_duration, 1.3, 2.3)])
    seam = int(round(c1.effective_duration * 48000))
    rms = lambda x: float(np.sqrt(np.mean(x ** 2)) + 1e-12)  # noqa: E731
    # Windows of one period of the 220 Hz tone (240 samples, 5 ms): a 1 ms
    # window measures WSOLA's PHASE against the unsplit clip's (Deep dipped to
    # -10.8 dB with sound present), not the silence this is about.
    worst = min(20 * np.log10(rms(split[i:i + 240]) / rms(whole[i:i + 240])) for i in range(seam, seam + 2400, 48))
    assert worst > -6.0, (effect, round(worst, 1))
    # and no run of digital silence after the seam (it was 239-913 samples)
    run = next((k for k in range(2400) if abs(split[seam + k]) > 1e-7), 2400)
    assert run <= 2, (effect, run)


# ================================================================ Out animations at a seam

from video_ai_editor.edl.schema import Sticker, Transform, Transition  # noqa: E402
from video_ai_editor.render import clock  # noqa: E402


@pytest.mark.parametrize("target", ["pip", "sticker"])
def test_an_out_animation_plays_before_a_seam_ends_the_element(tmp_path, target):
    """review RE: an overlay ending at a 1 s crossfade seam is on screen
    1.0-2.0 s (render), but pip.py planned its Out over the LAYOUT length
    (2 s): the Slide Left Out never started — it vanished mid-pose (cx 249 on
    every frame, then gone). The sticker already used the render window; one
    rule now: the element slides out over the last 0.6 s it is visible."""
    import clip_anim_lib as CL
    import test_clip_anim_render as T
    W, H, fps = 640, 360, 30
    box = CL.make_box_source(tmp_path / "box.mp4", w=W, h=H, fps=fps, seconds=8)
    blk = T._black(tmp_path / "black.mp4", fps, 8)
    e = empty_edl(Canvas(w=W, h=H, fps=fps))
    e.canvas.loudness_lufs = None
    v1 = e.get_track("v1")
    v1.clips = [Clip(src=str(blk), id="A", start=0.0, in_=0.0, out=3.0),
                Clip(src=str(blk), id="B", start=3.0, in_=0.0, out=3.0)]
    v1.transitions = [Transition(at=3.0, type="fade", duration=1.0)]
    if target == "pip":
        o = Clip(src=str(box), start=1.0, id="p", in_=0.0, out=2.0, transform=Transform(x=250, y=190))
        e.get_track("v2").clips.append(o)
    else:
        o = Sticker(src=str(T._sticker_png(tmp_path / "st.png")), start=1.0, end=3.0, id="s",
                    transform=Transform(x=380, y=170))
        next(t for t in e.tracks if t.type == "sticker").clips.append(o)
    o.anim_in, o.anim_dur, o.anim_out, o.anim_out_dur = "slide_right", 0.6, "slide_left", 0.6
    e.recompute_duration()
    assert clock.render_window(clock.seam_table(e), 1.0, 3.0) == pytest.approx((1.0, 2.0))
    ys = CL.decode_y(compositor._render(e, tmp_path / f"{target}.mp4", height=H, fps=fps, preview=False,
                                        cache_dir=tmp_path / "cache", chunked=False), W, H)
    cx = {f: (CL.measure(ys[f]).cx if CL.measure(ys[f]) else None) for f in range(40, 62)}
    rest = cx[44]                                     # render 1.47 s: after the In, before the Out
    assert rest is not None
    # the Out window is render [1.4, 2.0): the element travels LEFT through it
    late = [cx[f] for f in (52, 55, 58) if cx[f] is not None]
    assert late and min(late) < rest - 60, (target, rest, late)
    assert cx[52] is not None and cx[55] is not None and cx[55] < cx[52] - 20, (target, cx[52], cx[55])


# ================================================================ picture colours on an untagged base

def _yuv_at(path: Path, w: int, h: int, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "yuv444p", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw[: w * h * 3], np.uint8).reshape(3, h, w)
    return tuple(int(a[p][y, x]) for p in range(3))


@pytest.fixture()
def lossless(monkeypatch):
    monkeypatch.setattr(compositor, "_video_encoder_args",
                        lambda **kw: ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p"])


def _black_base(path: Path, tagged: bool) -> Path:
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"] \
        if tagged else []
    return _ff(path, "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=2", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", *tags)


@pytest.mark.parametrize("tagged", [False, True])
def test_a_picture_overlay_is_converted_with_bt709(tmp_path, lossless, tagged):
    """review RE (spec R12): on an UNTAGGED base a PNG overlay clip and a PNG
    sticker were converted with swscale's BT.601 default — #FF2828 at luma
    106 where BT.709 gives 90, so switching a PNG overlay from Normal to
    Lighten (or Screen) over black changed its colour by 16 levels. Normal ≡
    Lighten-over-black ≡ Screen-over-black now, tagged or not."""
    from PIL import Image
    from video_ai_editor.edl.schema import Sticker, Transform
    png = tmp_path / "red.png"
    Image.new("RGB", (120, 80), (255, 40, 40)).save(png)
    base = _black_base(tmp_path / f"base{int(tagged)}.mp4", tagged)
    got = {}
    for mode in ("normal", "lighten", "screen"):
        e = empty_edl(Canvas(w=320, h=180, fps=30))
        e.canvas.loudness_lufs = None
        e.get_track("v1").clips = [Clip(src=str(base), id="b", in_=0, out=1, start=0)]
        p = Clip(src=str(png), id="p", in_=0, out=1, start=0, transform=Transform(x=160, y=90))
        p.blend = mode
        e.get_track("v2").clips = [p]
        e.recompute_duration()
        out = compositor._render(e, tmp_path / f"{mode}{int(tagged)}.mp4", height=180, fps=30, preview=False,
                                 cache_dir=tmp_path / "c", chunked=False)
        got[mode] = _yuv_at(out, 320, 180, 160, 90)
    y709 = round(16 + 219 * (0.2126 * 1 + 0.7152 * 40 / 255 + 0.0722 * 40 / 255))
    assert abs(got["normal"][0] - y709) <= 2, got
    for mode in ("lighten", "screen"):
        assert max(abs(a - b) for a, b in zip(got["normal"], got[mode])) <= 2, got
    # a sticker PNG of the same colour, the same BT.709 luma
    e = empty_edl(Canvas(w=320, h=180, fps=30))
    e.canvas.loudness_lufs = None
    e.get_track("v1").clips = [Clip(src=str(base), id="b", in_=0, out=1, start=0)]
    next(t for t in e.tracks if t.type == "sticker").clips.append(
        Sticker(src=str(png), start=0, end=1, id="s", transform=Transform(x=160, y=90, scale=2.0)))
    e.recompute_duration()
    out = compositor._render(e, tmp_path / f"st{int(tagged)}.mp4", height=180, fps=30, preview=False,
                             cache_dir=tmp_path / "c2", chunked=False)
    assert max(abs(a - b) for a, b in zip(_yuv_at(out, 320, 180, 160, 90), got["normal"])) <= 2


# ================================================================ a static Canvas background

def _rgb_frame(path: Path, w: int, h: int, t: float) -> np.ndarray:
    raw = subprocess.run([_pu.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw[: w * h * 3], np.uint8).reshape(h, w, 3)


@pytest.fixture(scope="module")
def landscape(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("re_canvas")
    return _ff(d / "land.mp4", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p")


@pytest.mark.parametrize("kind", ["blur", "color", "image"])
@pytest.mark.parametrize("pose", ["scale07", "rot15", "zoom_in_t0", "slide_out", "opacity05"])
def test_the_canvas_background_stays_full_frame_behind_a_moving_clip(tmp_path, landscape, kind, pose):
    """review RE: the Canvas background was baked into the fitted frame
    BEFORE the transform and the animation, so a scale < 1, a rotation, a
    Zoom In or a Slide Out moved or shrank the background too and black bars
    came back (scale 0.7 on a 9:16 blur: 51 % black, rotation 15°: 12 %). It
    is a still, canvas-sized layer now: no frame of these shows black."""
    from PIL import Image
    from video_ai_editor.edl.schema import CanvasBackground, Transform
    W, H = 360, 640
    e = empty_edl(Canvas(w=W, h=H, fps=30))
    e.canvas.loudness_lufs = None
    c = Clip(src=str(landscape), id="c", in_=0, out=3, start=0)
    if kind == "blur":
        c.canvas_bg = CanvasBackground(type="blur", blur=3)
    elif kind == "color":
        c.canvas_bg = CanvasBackground(type="color", color="#E53935")
    else:
        pic = tmp_path / "pic.png"
        Image.new("RGB", (64, 48), (40, 170, 90)).save(pic)
        c.canvas_bg = CanvasBackground(type="image", image=str(pic))
    t = 1.5
    if pose == "scale07":
        c.transform = Transform(scale=0.7)
    elif pose == "rot15":
        c.transform = Transform(rotation=15)
    elif pose == "zoom_in_t0":
        c.anim_in, c.anim_dur, t = "zoom_in", 0.8, 0.0
    elif pose == "slide_out":
        c.anim_out, c.anim_out_dur, t = "slide_down", 0.8, 2.6
    else:
        c.transform = Transform(opacity=0.5)
    e.get_track("v1").clips = [c]
    e.recompute_duration()
    out = compositor._render(e, tmp_path / "out.mp4", height=H, fps=30, preview=False,
                             cache_dir=tmp_path / "cache", chunked=False)
    a = _rgb_frame(out, W, H, t)
    black = float((a.max(axis=2) < 16).mean())
    assert black < 0.002, (kind, pose, round(black, 4))


def test_a_chunk_missing_frames_is_not_cached_and_the_render_falls_back(bars, tmp_path, monkeypatch):
    """review RE: `render/chunks.py` now checks each built chunk holds
    exactly its clip's frames; a short one is refused (never cached) and the
    render takes the single pass, which meets the plan."""
    from video_ai_editor.render import chunks
    e = _one_frame_reversed(bars["b30"])
    orig = chunks.render_clip_to_chunk
    seen: list[str] = []

    def short_chunks(c, **k):               # the CHUNK chain drops a frame (the single pass does not)
        real = k["build_video_chain"]

        def chain(cl, **kw):
            text = real(cl, **kw)
            n = compositor.clip_frames(cl, kw["fps"])
            return text.replace(f"trim=end_frame={n},", f"trim=end_frame={n - 1},") if n > 1 else text
        try:
            return orig(c, **{**k, "build_video_chain": chain})
        except RuntimeError as ex:
            seen.append(str(ex))
            raise
    monkeypatch.setattr(chunks, "render_clip_to_chunk", short_chunks)
    out = compositor._render(e, tmp_path / "c.mp4", height=G.H, fps=29.97, preview=True,
                             cache_dir=tmp_path / "cache", chunked=True)
    assert any("video frames, the clip plans" in x for x in seen), seen
    # only the one-frame clip's chunk (untouched by the mutant) was cached
    cached = list((tmp_path / "cache" / "chunks").glob("chunk_*.mp4"))
    assert len(cached) <= 1 and all(nframes(p) == 1 for p in cached), [nframes(p) for p in cached]
    codes = _codes(out)
    assert len(codes) == planned_frames(e) and codes[8:11] == [8, 30, 60]
