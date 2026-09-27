"""The key-free Prompt bar sweep (wave D3, lane E3): how editors actually talk.

Every phrase below runs through the REAL service on a REAL session — the
grammar, the planner, `validate_plan`, the executor, dispatch and the
verifier (`service.prompt_turn(brain="recipes")`, the path the Prompt bar
takes with no API key) — and the resulting EDL is checked for the RIGHT
edit, or the timeline is proved unchanged and the reply asks one question.
No phrase may commit a wrong edit.

The session: a 12 s talking clip (tone in 0-3 / 5-8 / 10-12 s, a word-level
transcript beside it) split into three 4 s clips on the main track (A = source
0-4 s, B = 4-8 s, C = 8-12 s), a 16:9 1920x1080 canvas, a 12 s music bed at
-14 dB; the UI has clip B selected and the playhead at 5.5 s.

Before this sweep: "speed up the second clip 2x" sped up EVERY clip, "zoom
in on the last clip" put crossfades on every seam, "mute the last 5 seconds"
muted the music, "export in 4k" upscaled every clip, and "delete the second
clip", "move the second clip to the end", "duplicate this clip", "make it
brighter", "rotate the first clip 90 degrees", "split here" and "add a
sticker" were "I did not catch that". A record of every phrase (plan, steps,
verification, reply, the EDL after) is written to `capcut_sweep.json` in the
test's temp dir, and to `$VAE_E3_SWEEP_OUT` when that is set.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prompt_fixtures as F  # noqa: E402
from prompt_fixtures import no_downloads  # noqa: E402,F401

from video_ai_editor import config, storage  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.agent.prompt import service  # noqa: E402
from video_ai_editor.edl.schema import Clip, TextClip  # noqa: E402
from video_ai_editor.edl.snapshot import EDLStore  # noqa: E402

UI = {"selection": "B", "playhead": 5.5}
NO_SEL = {"playhead": 5.5}


# --------------------------------------------------------------------------- the session

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("capcut_sweep")
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    mp = pytest.MonkeyPatch()
    mp.setattr(storage, "WORKDIR", root)
    mp.setattr(service, "_RESOLVE_STORE", None)
    src = F.speech_clip(root)
    F.write_ingest(src)
    bed = F.music_bed(root, dur=12.0)
    ingest = (src.parent / "ingest.json").read_bytes()
    record: list[dict] = []
    yield {"root": root, "src": src, "bed": bed, "ingest": ingest, "record": record}
    mp.undo()
    config.enable_path_restriction(before)
    body = json.dumps(record, indent=1, default=str)
    (root / "capcut_sweep.json").write_text(body, encoding="utf-8")
    out = os.environ.get("VAE_E3_SWEEP_OUT")
    if out:
        Path(out).write_text(body, encoding="utf-8")


def _session(media: dict, name: str) -> tuple[EDLStore, dict[str, str]]:
    (media["src"].parent / "ingest.json").write_bytes(media["ingest"])   # a caption/cut step may rewrite it
    st = EDLStore(media["root"] / name)
    dispatch(st, "add_clip", {"track": "v1", "src": str(media["src"]), "in": 0, "out": F.CLIP_DUR, "start": 0})
    dispatch(st, "set_canvas", {"w": 1920, "h": 1080})
    dispatch(st, "split_at", {"track": "v1", "time": 4.0})
    dispatch(st, "split_at", {"track": "v1", "time": 8.0})
    dispatch(st, "add_music", {"src": str(media["bed"]), "start": 0.0, "volume_db": -14.0, "duck": False})
    ids = [c.id for c in st.edl.get_track("v1").clips]
    return st, {"A": ids[0], "B": ids[1], "C": ids[2]}


# --------------------------------------------------------------------------- reading the result

def v1(e) -> list[Clip]:
    return sorted((c for c in e.get_track("v1").clips if isinstance(c, Clip)), key=lambda c: c.start)


def ins(e) -> list[float]:
    return [round(c.in_, 2) for c in v1(e)]


def by_in(e, src_in: float) -> Clip:
    return next(c for c in v1(e) if abs(c.in_ - src_in) < 1e-3 and c.freeze is None)


def dur(e) -> float:
    return round(e.video_extent(), 2)


def texts(e) -> list[TextClip]:
    return [c for t in e.tracks for c in t.clips if isinstance(c, TextClip)]


def music(e):
    return e.get_track("music")


def transitions(e) -> list:
    return list(e.get_track("v1").transitions)


def speed_name(c: Clip) -> str | None:
    return c.speed.get("name") if isinstance(c.speed, dict) else None


def kf(c: Clip, prop: str) -> list[tuple[float, float]]:
    v = getattr(c.transform, prop)
    return [(round(t, 2), round(x, 3)) for t, x in v.keyframes] if hasattr(v, "keyframes") else []


def fx(c: Clip, etype: str) -> list[dict]:
    return [e.params for e in c.effects if e.type == etype]


def untouched(c: Clip) -> bool:
    return (c.speed in (None, 1.0) and not c.reverse and not c.audio.mute and not c.effects
            and not kf(c, "scale") and c.video_fade_in == 0 and c.video_fade_out == 0)


# --------------------------------------------------------------------------- the phrases

Check = Callable[[Any, dict[str, str]], None]
ASKS = "asks"


@dataclass
class Case:
    phrase: str
    expect: Check | str
    ui: dict = field(default_factory=lambda: dict(UI))
    pre: Callable[[EDLStore, dict[str, str]], None] | None = None
    #: a substring the question must contain (ASKS cases)
    question: str | None = None


def _eq(a: Any, b: Any) -> None:
    assert a == b, f"{a!r} != {b!r}"


def _approx(a: float, b: float, tol: float = 0.06) -> None:
    assert abs(a - b) <= tol, f"{a} != {b} ± {tol}"


def _only(e, ids, cid: str, pred: Callable[[Clip], bool]) -> None:
    for c in v1(e):
        if c.id == cid:
            assert pred(c), f"{c.id} did not get the edit: {c.model_dump(exclude_defaults=True)}"
        else:
            assert untouched(c), f"{c.id} was edited too: {c.model_dump(exclude_defaults=True)}"


def _pre_speed_b(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_speed", {"clip_id": ids["B"], "factor": 2.0})


def _undone(e, ids) -> None:
    assert by_in(e, 4.0).speed in (None, 1.0), by_in(e, 4.0).speed
    _eq(dur(e), 12.0)


def _split_at(t: float) -> Check:
    def chk(e, ids):
        _eq(len(v1(e)), 4)
        assert any(abs(c.start - t) < 0.02 for c in v1(e)), [c.start for c in v1(e)]
        _eq(dur(e), 12.0)
    return chk


def _canvas(w: int, h: int, *, fill: bool = True) -> Check:
    def chk(e, ids):
        _eq((e.canvas.w, e.canvas.h), (w, h))
        if fill:
            assert all(c.fit == "cover" for c in v1(e)), [c.fit for c in v1(e)]
    return chk


def _preset(w: int, h: int, lufs: float, kbps: int) -> Check:
    def chk(e, ids):
        _eq((e.canvas.w, e.canvas.h, e.canvas.loudness_lufs, e.canvas.bitrate_kbps), (w, h, lufs, kbps))
        if w * 9 == h * 16:
            # Same aspect as the 16:9 project: the clips are UNTOUCHED ("export
            # in 4k" used to upscale every clip; review RD3 — the old check
            # compared the helper's own arguments, so it could not fail).
            assert all(c.fit == "contain" and c.transform.scale == 1.0 and c.transform.x == 0.0
                       and c.transform.y == 0.0 for c in v1(e)), [(c.fit, c.transform) for c in v1(e)]
        else:
            assert all(c.fit == "cover" for c in v1(e)), [c.fit for c in v1(e)]
    return chk


def _transition(n: int, ttype: str | None, at: list[float]) -> Check:
    def chk(e, ids):
        tr = transitions(e)
        _eq(len(tr), n)
        _eq(sorted(round(t.at, 2) for t in tr), at)
        if ttype:
            assert all(t.type == ttype for t in tr), [t.type for t in tr]
    return chk


CASES: list[Case] = [
    # ---- split -------------------------------------------------------------
    Case("split at 3 seconds", _split_at(3.0)),
    Case("split the clip at 00:05", _split_at(5.0)),
    Case("split here", _split_at(5.5)),
    Case("cut it at 6s", _split_at(6.0)),
    Case("split the second clip at 00:06", _split_at(6.0)),
    Case("split at 1:20", ASKS, question="not inside the video"),
    # ---- trim a range ------------------------------------------------------
    Case("trim the first 2 seconds", lambda e, ids: (_eq(dur(e), 10.0), _eq(ins(e)[0], 2.0))),
    Case("cut the last 3 seconds", lambda e, ids: (_eq(dur(e), 9.0), _eq(round(v1(e)[-1].out, 2), 9.0))),
    Case("delete from 00:04 to 00:06", lambda e, ids: (_eq(dur(e), 10.0), _eq(ins(e), [0.0, 6.0, 8.0]))),
    Case("remove 3s to 5s", lambda e, ids: (_eq(dur(e), 10.0), _eq(ins(e), [0.0, 5.0, 8.0]))),
    Case("trim 2 seconds off the end", lambda e, ids: (_eq(dur(e), 10.0), _eq(round(v1(e)[-1].out, 2), 10.0))),
    # ---- delete / move / duplicate a clip ----------------------------------
    Case("delete the second clip", lambda e, ids: (_eq(ins(e), [0.0, 8.0]), _eq(dur(e), 8.0))),
    Case("delete this clip", lambda e, ids: (_eq(ins(e), [0.0, 8.0]), _eq(dur(e), 8.0))),
    Case("delete this clip", ASKS, ui=NO_SEL, question="Which clip"),
    Case("ripple delete the last clip", lambda e, ids: (_eq(ins(e), [0.0, 4.0]), _eq(dur(e), 8.0))),
    Case("remove the first clip", lambda e, ids: (_eq(ins(e), [4.0, 8.0]), _eq(dur(e), 8.0))),
    Case("delete clip 3", lambda e, ids: (_eq(ins(e), [0.0, 4.0]), _eq(dur(e), 8.0))),
    Case("cut out the second clip", lambda e, ids: (_eq(ins(e), [0.0, 8.0]), _eq(dur(e), 8.0))),
    Case("delete the fifth clip", ASKS, question="only 3 clips"),
    Case("move the second clip to the end", lambda e, ids: (_eq(ins(e), [0.0, 8.0, 4.0]), _eq(dur(e), 12.0))),
    Case("move the last clip to the start", lambda e, ids: _eq(ins(e), [8.0, 0.0, 4.0])),
    Case("swap the first two clips", lambda e, ids: _eq(ins(e), [4.0, 0.0, 8.0])),
    Case("swap the second and third clip", lambda e, ids: _eq(ins(e), [0.0, 8.0, 4.0])),
    Case("put the first clip after the third one", lambda e, ids: _eq(ins(e), [4.0, 8.0, 0.0])),
    Case("reverse the order of the clips", lambda e, ids: _eq(ins(e), [8.0, 4.0, 0.0])),
    Case("duplicate the second clip", lambda e, ids: (_eq(ins(e), [0.0, 4.0, 4.0, 8.0]), _eq(dur(e), 16.0))),
    Case("duplicate this clip", lambda e, ids: (_eq(ins(e), [0.0, 4.0, 4.0, 8.0]), _eq(dur(e), 16.0))),
    Case("copy the first clip", lambda e, ids: (_eq(ins(e), [0.0, 0.0, 4.0, 8.0]), _eq(dur(e), 16.0))),
    # ---- speed / curves / reverse / freeze ---------------------------------
    Case("speed up the second clip 2x",
         lambda e, ids: (_only(e, ids, ids["B"], lambda c: c.speed == 2.0), _eq(dur(e), 10.0))),
    Case("make this clip 2x", lambda e, ids: (_only(e, ids, ids["B"], lambda c: c.speed == 2.0), _eq(dur(e), 10.0))),
    Case("slow motion on the last clip",
         lambda e, ids: (_only(e, ids, ids["C"], lambda c: c.speed == 0.5), _eq(dur(e), 16.0))),
    Case("speed up the clip at 00:09 to 3x", lambda e, ids: _only(e, ids, ids["C"], lambda c: c.speed == 3.0)),
    Case("slow it down to 0.5x", lambda e, ids: (_eq({c.speed for c in v1(e)}, {0.5}), _eq(dur(e), 24.0))),
    Case("play everything at 1.5x", lambda e, ids: (_eq({c.speed for c in v1(e)}, {1.5}), _eq(dur(e), 8.0))),
    Case("speed the whole video up to 2x", lambda e, ids: (_eq({c.speed for c in v1(e)}, {2.0}), _eq(dur(e), 6.0))),
    Case("hero speed ramp on the second clip", lambda e, ids: _only(e, ids, ids["B"], lambda c: speed_name(c) == "hero")),
    Case("add a montage curve to this clip", lambda e, ids: _only(e, ids, ids["B"], lambda c: speed_name(c) == "montage")),
    Case("bullet time on the last clip", lambda e, ids: _only(e, ids, ids["C"], lambda c: speed_name(c) == "bullet")),
    Case("speed ramp the first clip", ASKS, question="Which speed curve"),
    Case("slow down the last 3 seconds",
         lambda e, ids: (_eq(len(v1(e)), 4), _eq(v1(e)[-1].speed, 0.8), _approx(v1(e)[-1].start, 9.0),
                         _eq([c.speed for c in v1(e)[:3]], [None, None, None]), _approx(dur(e), 12.75))),
    Case("speed up the last 4 seconds 2x",
         lambda e, ids: (_only(e, ids, ids["C"], lambda c: c.speed == 2.0), _eq(dur(e), 10.0))),
    Case("slow down the first 2 seconds",
         lambda e, ids: (_eq(len(v1(e)), 4), _eq(v1(e)[0].speed, 0.8), _approx(v1(e)[0].out, 2.0),
                         _eq([c.speed for c in v1(e)[1:]], [None, None, None]), _approx(dur(e), 12.5))),
    Case("slow down the last 5 seconds", ASKS, question="just the last clip"),
    Case("reverse the second clip", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.reverse)),
    Case("play this clip backwards", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.reverse)),
    Case("freeze frame at 00:03",
         lambda e, ids: (_eq(dur(e), 15.0), _eq([round(c.start, 2) for c in v1(e) if c.freeze], [3.0]))),
    Case("freeze the frame at 2s for 2 seconds",
         lambda e, ids: (_eq(dur(e), 14.0), _eq([(round(c.start, 2), c.freeze) for c in v1(e) if c.freeze], [(2.0, 2.0)]))),
    Case("freeze here", lambda e, ids: _eq([round(c.start, 2) for c in v1(e) if c.freeze], [5.5])),
    Case("freeze the last frame", lambda e, ids: (_eq(len([c for c in v1(e) if c.freeze]), 1),
                                                  _approx([c.start for c in v1(e) if c.freeze][0], 12.0 - 1 / 30, 0.04))),
    # ---- fades / transitions -----------------------------------------------
    Case("fade in at the start",
         lambda e, ids: _only(e, ids, ids["A"], lambda c: c.video_fade_in == 1.0 and c.audio.fade_in == 1.0)),
    Case("fade out the last clip over 2 seconds",
         lambda e, ids: _only(e, ids, ids["C"], lambda c: c.video_fade_out == 2.0 and c.audio.fade_out == 2.0)),
    Case("fade in the second clip", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.video_fade_in == 1.0)),
    Case("fade to black at the end", lambda e, ids: _only(e, ids, ids["C"], lambda c: c.video_fade_out == 1.0)),
    Case("add a cross dissolve between the clips", _transition(2, "dissolve", [4.0, 8.0])),
    Case("add a transition between the first and second clip", _transition(1, None, [4.0])),
    Case("add a dissolve after the second clip", _transition(1, "dissolve", [8.0])),
    Case("whip pan at the last cut", _transition(1, "whip", [8.0])),
    Case("add a zoom transition at 00:04", _transition(1, "zoomin", [4.0])),
    # ---- zoom / Ken Burns / keyframes / rotate -----------------------------
    Case("zoom in on the second clip",
         lambda e, ids: _only(e, ids, ids["B"], lambda c: kf(c, "scale") == [(0.0, 1.0), (4.0, 1.15)])),
    Case("add a ken burns effect to every clip",
         lambda e, ids: _eq([kf(c, "scale") for c in v1(e)], [[(0.0, 1.0), (4.0, 1.15)]] * 3)),
    Case("slowly zoom out on the first clip",
         lambda e, ids: _only(e, ids, ids["A"], lambda c: kf(c, "scale") == [(0.0, 1.15), (4.0, 1.0)])),
    Case("punch in on this clip", lambda e, ids: _eq([c.transform.scale for c in v1(e)], [1.0, 1.2, 1.0])),
    Case("zoom this clip to 150%", lambda e, ids: _eq([c.transform.scale for c in v1(e)], [1.0, 1.5, 1.0])),
    Case("zoom in on the product", ASKS, ui=NO_SEL, question="Which clip"),
    Case("rotate the first clip 90 degrees", lambda e, ids: _eq([c.transform.rotation for c in v1(e)], [90.0, 0.0, 0.0])),
    Case("rotate this clip upside down", lambda e, ids: _eq([c.transform.rotation for c in v1(e)], [0.0, 180.0, 0.0])),
    # ---- crop / aspect -----------------------------------------------------
    Case("make it 9:16", _canvas(1080, 1920)),
    Case("change the aspect ratio to square", _canvas(1080, 1080)),
    Case("crop to 4:5 for instagram feed", _canvas(1080, 1350)),
    Case("make it vertical for tiktok", _canvas(1080, 1920)),
    # ---- sound -------------------------------------------------------------
    Case("turn the music down", lambda e, ids: _eq({c.audio.gain_db for c in music(e).clips}, {-20.0})),
    Case("turn the music down to 10%", lambda e, ids: _eq({c.audio.gain_db for c in music(e).clips}, {-20.0})),
    Case("turn the music down to 20%", ASKS, question="already at -14 dB"),
    Case("set the music volume to 30%", lambda e, ids: _eq({c.audio.gain_db for c in music(e).clips}, {-10.5})),
    Case("mute the music", lambda e, ids: _eq(music(e).muted, True)),
    Case("mute the second clip", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.audio.mute)),
    Case("mute this clip", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.audio.mute)),
    Case("mute the original audio", lambda e, ids: _eq([c.audio.mute for c in v1(e)], [True] * 3)),
    Case("mute the last 3 seconds",
         lambda e, ids: (_eq([c.audio.mute for c in v1(e)], [False, False, False, True]), _approx(v1(e)[-1].start, 9.0),
                         _eq(music(e).muted, False))),
    Case("mute the last 5 seconds", ASKS, question="just the last clip"),
    Case("duck the music under my voice", lambda e, ids: _eq(music(e).duck is not None, True)),
    Case("fade the music out at the end", lambda e, ids: _eq(music(e).clips[-1].audio.fade_out, 2.0)),
    Case("make my voice louder", lambda e, ids: _eq({c.audio.gain_db for c in v1(e)}, {6.0})),
    # ---- captions / text / titles ------------------------------------------
    Case("add captions", lambda e, ids: _eq(bool([t for t in texts(e) if t.role == "caption"]), True)),
    Case("add subtitles at the top", lambda e, ids: _eq(bool([t for t in texts(e) if t.role == "caption"]), True)),
    Case("add a title that says 'Day One'",
         lambda e, ids: _eq([(t.text, t.start, t.end) for t in texts(e)], [("Day One", 0.0, 3.0)])),
    Case("put text 'SALE' at 00:03", lambda e, ids: _eq([(t.text, t.start, t.end) for t in texts(e)], [("SALE", 3.0, 6.0)])),
    Case("add a lower third for Priya Sharma",
         lambda e, ids: _eq([t.role for t in texts(e) if "Priya Sharma" in t.text], ["lower_third"])),
    Case("add text", ASKS, question="What should the title say"),
    Case("add a hook", lambda e, ids: _eq(bool([t for t in texts(e) if t.start < 0.5]), True)),
    # ---- filters / LUT / colour --------------------------------------------
    Case("add a warm filter", lambda e, ids: _eq([bool(fx(c, "lut")) for c in v1(e)], [True] * 3)),
    Case("make it black and white",
         lambda e, ids: _eq([str(fx(c, "lut")[0].get("src", "")).endswith("mono.cube") for c in v1(e)], [True] * 3)),
    Case("apply a cinematic look to the second clip",
         lambda e, ids: _eq([bool(fx(c, "lut")) for c in v1(e)], [False, True, False])),
    Case("make it brighter", lambda e, ids: _eq([fx(c, "color") for c in v1(e)], [[{"brightness": 0.1}]] * 3)),
    Case("make the second clip darker",
         lambda e, ids: _eq([fx(c, "color") for c in v1(e)], [[], [{"brightness": -0.1}], []])),
    Case("boost the contrast", lambda e, ids: _eq([fx(c, "color") for c in v1(e)], [[{"contrast": 1.2}]] * 3)),
    Case("more saturation", lambda e, ids: _eq([fx(c, "color") for c in v1(e)], [[{"saturation": 1.3}]] * 3)),
    # ---- stickers ----------------------------------------------------------
    Case("add a sticker", ASKS, question="Stickers panel"),
    Case("put a heart emoji on the first clip", ASKS, question="Stickers panel"),
    # ---- export presets ----------------------------------------------------
    Case("export for tiktok", _preset(1080, 1920, -16.0, 8000)),
    Case("set up the export for youtube", _preset(1920, 1080, -14.0, 12000)),
    Case("export in 4k", _preset(3840, 2160, -14.0, 35000)),
    Case("export for instagram reels", _preset(1080, 1920, -16.0, 8000)),
    # ---- second pass: phrasings that committed a WRONG edit or were not read
    Case("remove background noise",       # denoised copies replace the source; the loudness target is set
         lambda e, ids: (_eq(e.canvas.loudness_lufs, -16.0),
                         _eq([Path(c.src).name == "talk.normalized.mp4" for c in v1(e)], [False] * 3))),
    Case("lower the volume of the second clip",
         lambda e, ids: (_eq([c.audio.gain_db for c in v1(e)], [0.0, -6.0, 0.0]), _eq(e.canvas.loudness_lufs, -16.0))),
    Case("make the last clip louder", lambda e, ids: _eq([c.audio.gain_db for c in v1(e)], [0.0, 0.0, 6.0])),
    Case("delete everything after 10 seconds", lambda e, ids: _eq(dur(e), 10.0)),
    Case("remove the captions", ASKS, question="say 'undo'"),
    Case("remove the filter", ASKS, question="say 'undo'"),
    Case("turn off the transitions", ASKS, question="say 'undo'"),
    Case("make the second clip slower", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.speed == 0.8)),
    Case("speed up this part", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.speed == 1.25)),
    Case("reverse it", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.reverse)),
    Case("mute it", lambda e, ids: (_only(e, ids, ids["B"], lambda c: c.audio.mute), _eq(music(e).muted, False))),
    Case("zoom in", lambda e, ids: _only(e, ids, ids["B"], lambda c: kf(c, "scale") == [(0.0, 1.0), (4.0, 1.15)])),
    Case("delete it", lambda e, ids: _eq(ins(e), [0.0, 8.0])),
    Case("move it to the end", lambda e, ids: _eq(ins(e), [0.0, 8.0, 4.0])),
    Case("put the second clip first", lambda e, ids: _eq(ins(e), [4.0, 0.0, 8.0])),
    Case("add a title at the end that says 'Subscribe'",
         lambda e, ids: _eq([(t.text, t.start, t.end) for t in texts(e)], [("Subscribe", 9.0, 12.0)])),
    Case("add a flash transition between clips 2 and 3", _transition(1, "fadewhite", [8.0])),
    Case("add a fade between the second and third clip", _transition(1, "fade", [8.0])),
    Case("freeze the first frame", lambda e, ids: _eq([round(c.start, 2) for c in v1(e) if c.freeze], [0.0])),
    Case("rotate it", ASKS, question="how much"),
    Case("change the speed to 1.5x on the last clip", lambda e, ids: _only(e, ids, ids["C"], lambda c: c.speed == 1.5)),
    Case("make the colors pop",
         lambda e, ids: _eq([str(fx(c, "lut")[0].get("src", "")).endswith("punch.cube") for c in v1(e)], [True] * 3)),
    Case("brighten the second clip by 20%",
         lambda e, ids: _eq([fx(c, "color") for c in v1(e)], [[], [{"brightness": 0.2}], []])),
    # ---- third pass: the live Apple Intelligence ladder's misses, now read by the grammar
    Case("shove the last shot to the front", lambda e, ids: _eq(ins(e), [8.0, 0.0, 4.0])),
    Case("the intro drags, speed it up", lambda e, ids: _only(e, ids, ids["A"], lambda c: c.speed == 1.25)),
    Case("make everything pop more",
         lambda e, ids: _eq([str(fx(c, "lut")[0].get("src", "")).endswith("punch.cube") for c in v1(e)], [True] * 3)),
    Case("take the sound out of clip 3", lambda e, ids: _only(e, ids, ids["C"], lambda c: c.audio.mute)),
    Case("can the middle part go in reverse", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.reverse)),
    Case("hold on the final frame for a bit",
         lambda e, ids: _approx([c.start for c in v1(e) if c.freeze][0], 12.0 - 1 / 30, 0.04)),
    Case("can you make clip two play double speed", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.speed == 2.0)),
    Case("give the second clip a slow push in",
         lambda e, ids: _only(e, ids, ids["B"], lambda c: kf(c, "scale") == [(0.0, 1.0), (4.0, 1.15)])),
    Case("get rid of that middle bit", lambda e, ids: _eq(ins(e), [0.0, 8.0])),
    # ---- fourth pass (review RD3): committed wrong edits and needless questions
    # a percentage with a direction is RELATIVE: "in 20%" is 120 %, not 20 %
    Case("punch in 20% on the third clip", lambda e, ids: _eq([c.transform.scale for c in v1(e)], [1.0, 1.0, 1.2])),
    Case("zoom in 20% on the second clip", lambda e, ids: _eq([c.transform.scale for c in v1(e)], [1.0, 1.2, 1.0])),
    Case("zoom out 20% on the first clip", lambda e, ids: _eq([c.transform.scale for c in v1(e)], [0.8, 1.0, 1.0])),
    Case("zoom the second clip in by 30%", lambda e, ids: _eq([c.transform.scale for c in v1(e)], [1.0, 1.3, 1.0])),
    # render / export FOR a platform is the export preset, never the Auto edit
    Case("render it for youtube shorts",
         lambda e, ids: (_eq(ins(e), [0.0, 4.0, 8.0]), _eq(texts(e), []), _eq((e.canvas.w, e.canvas.h), (1080, 1920)))),
    Case("render this for reels",
         lambda e, ids: (_eq(ins(e), [0.0, 4.0, 8.0]), _eq(texts(e), []), _eq((e.canvas.w, e.canvas.h), (1080, 1920)))),
    # explicit requests the grammar used to answer with a question
    Case("slow the middle clip down to 75%", lambda e, ids: _only(e, ids, ids["B"], lambda c: c.speed == 0.75)),
    Case("write 'The End' over the last 2 seconds",
         lambda e, ids: _eq([(t.text, t.start, t.end) for t in texts(e)], [("The End", 10.0, 12.0)])),
    Case("lose the first second", lambda e, ids: _eq(dur(e), 11.0)),
    Case("chop it at 00:06", _split_at(6.0)),
    Case("turn clip two upside down", lambda e, ids: _eq([c.transform.rotation for c in v1(e)], [0.0, 180.0, 0.0])),
    Case("flip the second clip horizontally", ASKS, question="not available"),
    # ---- undo --------------------------------------------------------------
    Case("undo", _undone, pre=_pre_speed_b),
    Case("undo that", _undone, pre=_pre_speed_b),
    Case("go back", _undone, pre=_pre_speed_b),
]


# --------------------------------------------------------------------------- the run

def _turn(st: EDLStore, phrase: str, ui: dict) -> list[dict]:
    events = F.collect(service.prompt_turn(st, phrase, [], brain="recipes", ui_state=ui))
    assert events and events[-1]["type"] == "done", events[-3:]
    return events


def _record(case: Case, events: list[dict], e) -> dict:
    plan = next((x["plan"] for x in events if x["type"] == "plan"), None)
    ver = next((x for x in events if x["type"] == "verify"), None)
    return {
        "phrase": case.phrase, "ui": case.ui, "expect": case.expect if isinstance(case.expect, str) else "edit",
        "intent": plan and plan["intent"], "confidence": plan and plan["confidence"],
        "steps": [(s["tool"], s["args"]) for s in (plan or {}).get("steps", [])],
        "questions": [q["question"] for q in (plan or {}).get("needs_input", [])],
        "reply": "".join(x.get("text", "") for x in events if x["type"] == "text_delta"),
        "verify": ver and {"passed": ver.get("passed"), "total": ver.get("total")},
        "edl": {"duration": dur(e), "canvas": [e.canvas.w, e.canvas.h],
                "v1": [c.model_dump(exclude_defaults=True, exclude={"src"}) for c in v1(e)],
                "text": [(t.text, t.start, t.end, t.role) for t in texts(e)],
                "transitions": [t.model_dump() for t in transitions(e)]},
    }


def _question_text(events: list[dict]) -> str:
    bits = [q["question"] for x in events if x["type"] == "clarify" for q in x.get("questions", [])]
    bits += [x.get("text", "") for x in events if x["type"] == "text_delta"]
    return " ".join(bits)


@pytest.mark.usefixtures("no_downloads")
@pytest.mark.parametrize("case", CASES, ids=[f"{i:03d}-{c.phrase}" for i, c in enumerate(CASES)])
def test_capcut_phrase_does_the_right_edit_or_asks(media, case, request):
    st, ids = _session(media, f"s_{request.node.callspec.id[:3]}")
    ui = {k: (ids[v] if k == "selection" else v) for k, v in case.ui.items()}
    if case.pre:
        case.pre(st, ids)
    before = st.edl.hash()
    events = _turn(st, case.phrase, ui)
    e = st.edl
    media["record"].append(_record(case, events, e))
    errors = [x for x in events if x["type"] == "error"]
    assert not errors, errors
    if case.expect == ASKS:
        assert e.hash() == before, "the timeline changed on a phrase that must ask"
        q = _question_text(events)
        assert "?" in q and (case.question or "") in q, q
        return
    assert e.hash() != before, f"nothing changed: {_question_text(events)}"
    ver = next((x for x in events if x["type"] == "verify"), None)
    if ver is not None:                                   # undo / redo have no verify pass
        assert ver["passed"] == ver["total"], ver
    case.expect(e, ids)


def test_the_sweep_is_at_least_eighty_phrases():
    assert len({c.phrase for c in CASES}) >= 80


def test_the_same_aspect_preset_check_catches_an_upscaled_clip():
    """The `_preset` check itself can fail (review RD3: it compared its own
    arguments and passed whatever the plan did to the clips)."""
    from video_ai_editor.edl.schema import Canvas, empty_edl
    e = empty_edl(Canvas(w=3840, h=2160, fps=30))
    e.canvas.loudness_lufs, e.canvas.bitrate_kbps = -14.0, 35000
    e.get_track("v1").clips = [Clip(src="a.mp4", in_=0.0, out=2.0, start=0.0, id="a")]
    _preset(3840, 2160, -14.0, 35000)(e, {})
    e.get_track("v1").clips[0].transform.scale = 2.0          # "export in 4k" upscaled it
    with pytest.raises(AssertionError):
        _preset(3840, 2160, -14.0, 35000)(e, {})


def test_the_zoom_check_fails_a_zoom_in_that_shrinks_the_picture():
    """review RD3: "punch in 20%" committed scale 0.2 and passed on
    `tool_ok` alone; `clip_zoomed` measures the direction on the EDL."""
    from types import SimpleNamespace

    from video_ai_editor.agent.prompt import verify as V
    from video_ai_editor.agent.prompt.schema import Postcondition
    from video_ai_editor.edl.schema import Canvas, Keyframe, empty_edl
    e = empty_edl(Canvas(w=1920, h=1080, fps=30))
    c = Clip(src="a.mp4", in_=0.0, out=4.0, start=0.0, id="c_a")
    e.get_track("v1").clips = [c]
    ctx = SimpleNamespace(edl=e)

    def run(direction):
        return V.c_clip_zoomed(ctx, Postcondition(check="clip_zoomed", human="zoom", args={"clip_id": "c_a",
                                                                                          "direction": direction}))
    c.transform.scale = 0.2
    assert run("in").passed is False and run("out").passed is True
    c.transform.scale = 1.2
    assert run("in").passed is True and run("out").passed is False
    c.transform.scale = Keyframe(keyframes=[(0.0, 1.15), (4.0, 1.0)], interp="ease-in-out")
    assert run("out").passed is True and run("in").passed is False


def test_an_unknown_request_is_offered_the_nearest_intents_by_keyword():
    """review RD3: every unread request was offered Captions / Tighten /
    Auto edit, whatever it said."""
    from video_ai_editor.agent.prompt.planner import _guesses
    assert [i for i, _ in _guesses("make the colours feel warmer somehow")][0] == "color_look"
    assert "speed" in [i for i, _ in _guesses("the pace should be gentler in clip two")]
    assert "auto_edit" not in [i for i, _ in _guesses("blorp")]
