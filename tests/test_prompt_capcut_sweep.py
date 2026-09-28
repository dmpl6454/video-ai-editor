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
            and not kf(c, "scale") and c.video_fade_in == 0 and c.video_fade_out == 0
            and not getattr(c.transform, "flip_h", False) and not getattr(c.transform, "flip_v", False))


def flips(e) -> list[tuple[bool, bool]]:
    return [(bool(getattr(c.transform, "flip_h", False)), bool(getattr(c.transform, "flip_v", False))) for c in v1(e)]


def durs(e) -> list[float]:
    return [round(c.effective_duration, 3) for c in v1(e)]


def gains(e) -> list[float]:
    return [round(c.audio.gain_db, 2) for c in v1(e)]


def looks(e) -> list[list[str]]:
    return [[Path(str(x.params.get("src", ""))).name if x.type == "lut" else x.type for x in c.effects] for c in v1(e)]


# --------------------------------------------------------------------------- the phrases

Check = Callable[[Any, dict[str, str]], None]
ASKS = "asks"
#: nothing to do: the timeline is unchanged and the reply says why (no question)
NOOP = "noop"


@dataclass
class Case:
    phrase: str
    expect: Check | str
    ui: dict = field(default_factory=lambda: dict(UI))
    pre: Callable[[EDLStore, dict[str, str]], None] | None = None
    #: a substring the question must contain (ASKS cases) or the reply (NOOP)
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


# ---- wave E (F4b) setups: what an edit BY NAME finds on the timeline
def _pre_captions(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "add_caption_track", {})


def _pre_title(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "add_text", {"text": "Day One", "start": 0.0, "end": 3.0})


def _pre_texts(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "add_text", {"text": "Day One", "start": 0.0, "end": 3.0})
    dispatch(st, "add_text", {"text": "SALE", "start": 5.0, "end": 7.0})


def _pre_lower_third(st: EDLStore, ids: dict[str, str]) -> None:
    _pre_texts(st, ids)
    dispatch(st, "add_lower_third", {"name": "Priya Sharma", "start": 8.0, "end": 11.0})


def _pre_luts(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "apply_lut", {"clip_id": ids["A"], "src": "warm.cube"})
    dispatch(st, "apply_lut", {"clip_id": ids["B"], "src": "mono.cube"})


def _pre_grade(st: EDLStore, ids: dict[str, str]) -> None:
    _pre_luts(st, ids)
    dispatch(st, "color_grade", {"clip_id": ids["C"], "brightness": 0.1})


def _pre_transitions(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "add_transition", {"at": 4.0, "type": "dissolve"})
    dispatch(st, "add_transition", {"at": 8.0, "type": "fade"})


def _pre_gain_b(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_volume", {"target": ids["B"], "db": -3.0})


def _pre_flip_b(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "flip_clip", {"clip_id": ids["B"], "axis": "horizontal", "value": True})


def _pre_hero_c(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_speed", {"clip_id": ids["C"], "preset": "hero"})


def _hero_footprint(src_seconds: float) -> float:
    from video_ai_editor.edl.speed_curve import mean_speed
    from video_ai_editor.edl.speed_presets import preset_speed
    return src_seconds / mean_speed([tuple(p) for p in preset_speed("hero")["curve"]])


# ---- wave E (F2): canvas backgrounds and overlay blend modes
def canvases(e) -> list:
    out = []
    for c in v1(e):
        bg = c.canvas_bg
        out.append(None if bg is None else (bg.type, bg.color if bg.type == "color" else bg.blur
                                             if bg.type == "blur" else Path(bg.image or "").name))
    return out


def blends(e) -> list[str]:
    return [c.blend for c in e.get_track("v2").clips if isinstance(c, Clip)]


def _picture(st: EDLStore, name: str) -> Path:
    from PIL import Image
    d = st.dir / "uploads" / "images"
    d.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (200, 120, 40)).save(d / name)
    return d / name


def _pre_picture(st: EDLStore, ids: dict[str, str]) -> None:
    _picture(st, "sunset.png")


def _pre_two_pictures(st: EDLStore, ids: dict[str, str]) -> None:
    _picture(st, "sunset.png")
    _picture(st, "beach.jpg")


def _portrait(pre=None):
    """A 9:16 project (the 16:9 clips letterboxed), then `pre`."""
    def run(st: EDLStore, ids: dict[str, str]) -> None:
        dispatch(st, "set_canvas", {"w": 1080, "h": 1920})
        if pre is not None:
            pre(st, ids)
    return run


def _pre_blur_all(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_canvas_background", {"all": True, "type": "blur", "blur": 3})


def _pre_overlay(st: EDLStore, ids: dict[str, str]) -> None:
    src = st.edl.get_track("v1").clips[0].src
    dispatch(st, "add_clip", {"track": "v2", "src": src, "in": 0, "out": 3, "start": 1.0})


def _pre_two_overlays(st: EDLStore, ids: dict[str, str]) -> None:
    _pre_overlay(st, ids)
    src = st.edl.get_track("v1").clips[0].src
    dispatch(st, "add_clip", {"track": "v2", "src": src, "in": 0, "out": 2, "start": 6.0})


def _pre_overlay_screen(st: EDLStore, ids: dict[str, str]) -> None:
    _pre_overlay(st, ids)
    dispatch(st, "set_blend_mode", {"clip_id": st.edl.get_track("v2").clips[0].id, "mode": "screen"})


def _roles(e) -> list[str | None]:
    return sorted((t.role or "text") for t in texts(e))


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


# ---- wave E (F3): voice effects ------------------------------------------

def voices(e) -> list[str | None]:
    return [c.audio.voice_effect for c in v1(e)]


def vo_clips(e) -> list[Clip]:
    return [c for c in e.get_track("vo").clips if isinstance(c, Clip)]


def _pre_vo(st: EDLStore, ids: dict[str, str]) -> None:
    src = v1(st.edl)[0].src
    dispatch(st, "add_clip", {"track": "vo", "src": src, "in": 0, "out": 3.0, "start": 1.0})


def _pre_robot(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_voice_effect", {"track": "v1", "effect": "robot"})


def _pre_echo_b_robot_c(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_voice_effect", {"clip_id": ids["B"], "effect": "echo"})
    dispatch(st, "set_voice_effect", {"clip_id": ids["C"], "effect": "robot"})


def _voice_only(e, ids, want: list[str | None], music_fx: str | None = None) -> None:
    _eq(voices(e), want)
    _eq({c.audio.voice_effect for c in music(e).clips}, {music_fx})


# ---- wave E (F1): clip animations ---------------------------------------

def anims(e) -> list[tuple[str | None, str | None, str | None]]:
    return [(c.anim_in, c.anim_out, c.anim_combo) for c in v1(e)]


def stickers(e) -> list:
    from video_ai_editor.edl.schema import Sticker
    return sorted((c for t in e.tracks for c in t.clips if isinstance(c, Sticker)), key=lambda c: c.start)


def _pre_sticker(st: EDLStore, ids: dict[str, str], n: int = 1) -> None:
    """`n` stickers of a local PNG (add_sticker with `src`: no emoji fetch)."""
    from PIL import Image
    png = st.dir / "uploads" / "stickers" / "star.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (64, 64), (255, 200, 0, 255)).save(png)
    for i in range(n):
        dispatch(st, "add_sticker", {"src": str(png), "start": 1.0 + 3 * i, "end": 3.0 + 3 * i})


def _pre_two_stickers(st: EDLStore, ids: dict[str, str]) -> None:
    _pre_sticker(st, ids, 2)


def _pre_overlay(st: EDLStore, ids: dict[str, str]) -> None:
    src = st.edl.get_track("v1").clips[0].src
    dispatch(st, "add_clip", {"track": "v2", "src": src, "in": 0, "out": 2.0, "start": 2.0})


def _pre_anim_b(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "set_animation", {"clip_id": ids["B"], "in": "zoom_in", "out": "fade_out"})


def _anims_only(want: list[tuple[str | None, str | None, str | None]], sticker=None) -> Check:
    def chk(e, ids):
        _eq(anims(e), want)
        if sticker is not None:
            _eq([(s.anim_in, s.anim_out, s.anim_combo) for s in stickers(e)], sticker)
        else:
            assert all(not (s.anim_in or s.anim_out or s.anim_combo) for s in stickers(e))
    return chk


_NA = (None, None, None)


# ---- review RE (wave E fixer): the phrasings the re-test caught ------------

def _pre_music_split(st: EDLStore, ids: dict[str, str]) -> None:
    """Two music clips at DIFFERENT levels (-12 dB and -3 dB)."""
    dispatch(st, "split_at", {"track": "music", "time": 6.0})
    a, b = [c.id for c in music(st.edl).clips]
    dispatch(st, "set_volume", {"target": a, "db": -12.0})
    dispatch(st, "set_volume", {"target": b, "db": -3.0})


def _pre_vo_on_music(st: EDLStore, ids: dict[str, str]) -> None:
    """A voice recording dropped on the MUSIC lane (the Timeline routes an
    audio drop there), next to the bed."""
    vo = F.music_bed(st.dir, name="voiceover.wav", dur=3.0, freq=300)
    dispatch(st, "add_clip", {"track": "music", "src": str(vo), "in": 0, "out": 3.0, "start": 12.5})


def _pre_vo_on_music_robot(st: EDLStore, ids: dict[str, str]) -> None:
    _pre_vo_on_music(st, ids)
    dispatch(st, "set_voice_effect", {"clip_id": _music_named(st.edl, "voiceover").id, "effect": "robot"})


def _music_named(e, stem: str):
    return next(c for c in music(e).clips if Path(str(c.src)).stem == stem)


def _pre_effects(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "add_effect", {"clip_id": ids["A"], "type": "vintage", "params": {}})
    dispatch(st, "add_effect", {"clip_id": ids["B"], "type": "vignette", "params": {}})


def _pre_freeze_v1(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "freeze_frame", {"clip_id": ids["A"], "at": 2.0})


def _pre_subscribe(st: EDLStore, ids: dict[str, str]) -> None:
    dispatch(st, "add_text", {"text": "Subscribe", "start": 9.0, "end": 11.0})


def _vo_music(e, want: str | None) -> None:
    _eq(_music_named(e, "voiceover").audio.voice_effect, want)
    _eq(_music_named(e, "bed").audio.voice_effect, None)
    _eq(voices(e), [None] * 3)


def _overlay_fx(e) -> list:
    return [c.audio.voice_effect for c in e.get_track("v2").clips if isinstance(c, Clip)]


def _fx_types(e) -> list[list[str]]:
    return [[x.type for x in c.effects] for c in v1(e)]


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
    # wave E (F4b): these three are real removals now (they used to ask, and
    # before that "remove the captions" LAID captions)
    Case("remove the captions", lambda e, ids: _eq([t for t in texts(e) if t.role == "caption"], []), pre=_pre_captions),
    Case("remove the filter", lambda e, ids: _eq(looks(e), [[], [], []]), pre=_pre_luts),
    Case("turn off the transitions", lambda e, ids: _eq(transitions(e), []), pre=_pre_transitions),
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
    Case("flip the second clip horizontally",           # wave E (F4b): it used to say "not available"
         lambda e, ids: _eq(flips(e), [(False, False), (True, False), (False, False)])),
    # ---- fifth pass (wave E, F4b): removals by name ------------------------
    Case("get rid of the subtitles", lambda e, ids: _eq([t for t in texts(e) if t.role == "caption"], []),
         pre=_pre_captions),
    Case("turn off the captions", lambda e, ids: _eq([t for t in texts(e) if t.role == "caption"], []),
         pre=_pre_captions),
    Case("remove the subtitles", NOOP, question="no captions"),
    Case("take off the filter", lambda e, ids: _eq(looks(e), [[], [], []]), pre=_pre_luts),
    Case("remove the filter from the second clip", lambda e, ids: _eq(looks(e), [["warm.cube"], [], []]), pre=_pre_luts),
    Case("remove the black and white filter", lambda e, ids: _eq(looks(e), [["warm.cube"], [], []]), pre=_pre_luts),
    Case("take the warm filter off", lambda e, ids: _eq(looks(e), [[], ["mono.cube"], []]), pre=_pre_luts),
    Case("strip the LUT off the first clip", lambda e, ids: _eq(looks(e), [[], ["mono.cube"], []]), pre=_pre_luts),
    Case("remove the colour grade", lambda e, ids: _eq(looks(e), [[], [], []]), pre=_pre_grade),
    Case("take off the filter", NOOP, question="no filter"),
    Case("remove the transition between clip 2 and 3",
         lambda e, ids: _eq([(t.at, t.type) for t in transitions(e)], [(4.0, "dissolve")]), pre=_pre_transitions),
    Case("delete the transition between the first and second clip",
         lambda e, ids: _eq([(t.at, t.type) for t in transitions(e)], [(8.0, "fade")]), pre=_pre_transitions),
    Case("remove the last transition",
         lambda e, ids: _eq([(t.at, t.type) for t in transitions(e)], [(4.0, "dissolve")]), pre=_pre_transitions),
    Case("get rid of the crossfade after the first clip",
         lambda e, ids: _eq([(t.at, t.type) for t in transitions(e)], [(8.0, "fade")]), pre=_pre_transitions),
    Case("remove all the transitions", lambda e, ids: _eq(transitions(e), []), pre=_pre_transitions),
    Case("remove the transition", ASKS, pre=_pre_transitions, question="Which transition"),
    Case("remove the transitions", NOOP, question="no transitions"),
    Case("delete the title", lambda e, ids: _eq(texts(e), []), pre=_pre_title),
    Case("delete the text 'SALE'", lambda e, ids: _eq([t.text for t in texts(e)], ["Day One"]), pre=_pre_texts),
    Case("delete the title", ASKS, pre=_pre_texts, question="Which text"),
    Case("remove all the text", lambda e, ids: _eq(texts(e), []), pre=_pre_texts),
    Case("remove the lower third", lambda e, ids: _eq(_roles(e), ["text", "text"]), pre=_pre_lower_third),
    Case("delete the title", NOOP, question="no text"),
    # ---- a clip's length ---------------------------------------------------
    Case("trim the second clip to 2 seconds",
         lambda e, ids: (_eq(durs(e), [4.0, 2.0, 4.0]), _eq(ins(e), [0.0, 4.0, 8.0]), _eq(dur(e), 10.0))),
    Case("make the first clip 3 seconds long", lambda e, ids: (_eq(durs(e), [3.0, 4.0, 4.0]), _eq(dur(e), 11.0))),
    Case("shorten clip 3 to 1.5 seconds", lambda e, ids: _eq(durs(e), [4.0, 4.0, 1.5])),
    Case("set the duration of the first clip to 2 seconds", lambda e, ids: _eq(durs(e), [2.0, 4.0, 4.0])),
    Case("clip 2 should be 3 seconds long", lambda e, ids: _eq(durs(e), [4.0, 3.0, 4.0])),
    Case("trim 1 second off the end of the second clip",
         lambda e, ids: (_eq(durs(e), [4.0, 3.0, 4.0]), _eq(round(by_in(e, 4.0).out, 2), 7.0))),
    Case("cut 1 second from the start of the last clip",
         lambda e, ids: (_eq(durs(e), [4.0, 4.0, 3.0]), _eq(ins(e), [0.0, 4.0, 9.0]))),
    Case("make the first clip 2 seconds longer", lambda e, ids: (_eq(durs(e), [6.0, 4.0, 4.0]), _eq(dur(e), 14.0))),
    Case("extend the second clip to 5 seconds", lambda e, ids: _eq(durs(e), [4.0, 5.0, 4.0])),
    Case("trim this clip to 2.5 seconds", lambda e, ids: _eq(durs(e), [4.0, 2.5, 4.0])),
    Case("trim the second clip to 1 second",                 # at 2x: 1 s of timeline is 2 s of source
         lambda e, ids: (_eq(durs(e), [4.0, 1.0, 4.0]), _eq(round(by_in(e, 4.0).out, 2), 6.0)), pre=_pre_speed_b),
    Case("trim the last clip to 2 seconds",                  # a Hero curve: through its integral
         lambda e, ids: _approx(durs(e)[2], 2.0, 1 / 30 + 1e-3),
         pre=_pre_hero_c),
    Case("cut 1 second off the start of the last clip",       # a Hero HEAD trim keeps the curve's tail
         lambda e, ids: (_approx(durs(e)[2], _hero_footprint(4.0) - 1.0, 1 / 30 + 1e-3),
                         _approx(v1(e)[2].out, 12.0, 1e-3)),
         pre=_pre_hero_c),
    Case("make the last clip 6 seconds long", ASKS, question="at most"),
    Case("trim the second clip to 4 seconds", ASKS, question="already"),
    Case("trim the clip to 2 seconds", ASKS, ui=NO_SEL, question="Which clip"),
    # ---- relative levels, from the CURRENT gain ----------------------------
    Case("lower the volume of the second clip", lambda e, ids: _eq(gains(e), [0.0, -9.0, 0.0]), pre=_pre_gain_b),
    Case("make the second clip louder", lambda e, ids: _eq(gains(e), [0.0, 3.0, 0.0]), pre=_pre_gain_b),
    Case("turn the second clip up by 3 dB", lambda e, ids: _eq(gains(e), [0.0, 0.0, 0.0]), pre=_pre_gain_b),
    Case("make the music quieter by 6 dB", lambda e, ids: _eq({c.audio.gain_db for c in music(e).clips}, {-20.0})),
    Case("turn the music up by 4 dB", lambda e, ids: _eq({c.audio.gain_db for c in music(e).clips}, {-10.0})),
    Case("make my voice louder", lambda e, ids: _eq(gains(e), [6.0, 3.0, 6.0]), pre=_pre_gain_b),
    Case("lower the volume of the last clip by 50%", lambda e, ids: _eq(gains(e), [0.0, 0.0, -6.0])),
    # ---- flip / mirror -----------------------------------------------------
    Case("flip the second clip", lambda e, ids: _eq(flips(e), [(False, False), (True, False), (False, False)])),
    Case("mirror this clip", lambda e, ids: _eq(flips(e), [(False, False), (True, False), (False, False)])),
    Case("flip the first clip vertically", lambda e, ids: _eq(flips(e), [(False, True), (False, False), (False, False)])),
    Case("mirror the last clip", lambda e, ids: _eq(flips(e), [(False, False), (False, False), (True, False)])),
    Case("turn it upside down",
         lambda e, ids: (_eq([c.transform.rotation for c in v1(e)], [0.0, 180.0, 0.0]),
                         _eq(flips(e), [(False, False)] * 3))),
    Case("flip it back", lambda e, ids: _eq(flips(e), [(False, False)] * 3), pre=_pre_flip_b),
    Case("unflip the second clip", lambda e, ids: _eq(flips(e), [(False, False)] * 3), pre=_pre_flip_b),
    Case("flip the second clip", lambda e, ids: _eq(flips(e), [(False, False)] * 3), pre=_pre_flip_b),   # a toggle
    Case("flip it", ASKS, ui=NO_SEL, question="Which clip"),
    # ---- voice effects (wave E, F3) -----------------------------------------
    # "my voice" with no voice-over is the talking clip: every main-track clip
    Case("make my voice sound like a robot", lambda e, ids: _voice_only(e, ids, ["robot"] * 3)),
    Case("make my voice deeper", lambda e, ids: _voice_only(e, ids, ["deep"] * 3)),
    Case("give me a chipmunk voice", lambda e, ids: _voice_only(e, ids, ["chipmunk"] * 3)),
    Case("add echo to the voiceover",
         lambda e, ids: (_eq([c.audio.voice_effect for c in vo_clips(e)], ["echo"]), _voice_only(e, ids, [None] * 3)),
         pre=_pre_vo),
    Case("make my voice sound like a robot",          # with a voice-over, "my voice" IS the voice-over
         lambda e, ids: (_eq([c.audio.voice_effect for c in vo_clips(e)], ["robot"]), _voice_only(e, ids, [None] * 3)),
         pre=_pre_vo),
    Case("add echo to the voiceover", ASKS, question="no voice-over"),
    Case("chipmunk voice on the second clip", lambda e, ids: _voice_only(e, ids, [None, "chipmunk", None])),
    Case("make this clip sound like a telephone", lambda e, ids: _voice_only(e, ids, [None, "telephone", None])),
    Case("add reverb", lambda e, ids: _voice_only(e, ids, [None, "reverb", None])),   # the selected clip
    Case("put a megaphone effect on the last clip", lambda e, ids: _voice_only(e, ids, [None, None, "megaphone"])),
    Case("a little reverb on the music",
         lambda e, ids: (_voice_only(e, ids, [None] * 3, "reverb"),
                         _eq({c.audio.voice_intensity for c in music(e).clips}, {0.5}))),
    Case("make the first clip sound like an old radio", lambda e, ids: _voice_only(e, ids, ["radio", None, None])),
    Case("make it sound like it's underwater", lambda e, ids: _voice_only(e, ids, [None, "underwater", None])),
    Case("change my voice", ASKS, question="Which voice effect"),
    Case("add a voice effect", ASKS, question="Which voice effect"),
    Case("remove the voice effect", lambda e, ids: _voice_only(e, ids, [None] * 3), pre=_pre_robot),
    Case("turn off the echo", lambda e, ids: _voice_only(e, ids, [None, None, "robot"]), pre=_pre_echo_b_robot_c),
    Case("voice back to normal", lambda e, ids: _voice_only(e, ids, [None] * 3), pre=_pre_robot),
    Case("remove the voice effect", ASKS, question="nothing to take off"),
    # ---- canvas background and blend modes (wave E, F2) --------------------
    # a canvas with no clip named is CapCut's "Apply to all"; the project is
    # made 9:16 first so the 16:9 clips are letterboxed (review RE: on a
    # frame the clips fill, a canvas is invisible and the phrase asks)
    Case("blur the background", lambda e, ids: _eq(canvases(e), [("blur", 2)] * 3), pre=_portrait()),
    Case("make the background black", lambda e, ids: _eq(canvases(e), [("color", "#000000")] * 3), pre=_portrait()),
    Case("fill the black bars with white", lambda e, ids: _eq(canvases(e), [("color", "#FFFFFF")] * 3), pre=_portrait()),
    Case("make the background red", lambda e, ids: _eq(canvases(e), [("color", "#E53935")] * 3), pre=_portrait()),
    Case("set the canvas colour to #1e88e5", lambda e, ids: _eq(canvases(e), [("color", "#1E88E5")] * 3), pre=_portrait()),
    Case("heavy blur on the background", lambda e, ids: _eq(canvases(e), [("blur", 4)] * 3), pre=_portrait()),
    Case("a slight blur behind the video",
         lambda e, ids: _eq(canvases(e), [("blur", 1)] * 3), pre=_portrait()),
    Case("blur the background of the second clip", lambda e, ids: _eq(canvases(e), [None, ("blur", 2), None]), pre=_portrait()),
    Case("blurred background on this clip", lambda e, ids: _eq(canvases(e), [None, ("blur", 2), None]), pre=_portrait()),
    Case("use this image as the background", lambda e, ids: _eq(canvases(e), [("image", "sunset.png")] * 3),
         pre=_portrait(_pre_picture)),
    Case("use sunset.png as the background", lambda e, ids: _eq(canvases(e), [("image", "sunset.png")] * 3),
         pre=_portrait(_pre_two_pictures)),
    Case("use this image as the background", ASKS, pre=_portrait(_pre_two_pictures), question="Which picture"),
    Case("use my photo as the background", NOOP, question="no picture", pre=_portrait()),
    Case("remove the background blur", lambda e, ids: _eq(canvases(e), [None] * 3), pre=_pre_blur_all),
    Case("change the background", ASKS, question="Blur, a colour or a picture"),
    Case("set the overlay to screen", lambda e, ids: _eq(blends(e), ["screen"]), pre=_pre_overlay),
    Case("multiply blend the top clip", lambda e, ids: _eq(blends(e), ["multiply"]), pre=_pre_overlay),
    Case("blend the pip with overlay", lambda e, ids: _eq(blends(e), ["overlay"]), pre=_pre_overlay),
    Case("change the blend mode to add", lambda e, ids: _eq(blends(e), ["add"]), pre=_pre_overlay),
    Case("use linear dodge on the overlay", lambda e, ids: _eq(blends(e), ["add"]), pre=_pre_overlay),
    Case("make the top clip soft light", lambda e, ids: _eq(blends(e), ["soft_light"]), pre=_pre_overlay),
    Case("set the overlay clip to color burn", lambda e, ids: _eq(blends(e), ["color_burn"]), pre=_pre_overlay),
    Case("screen blend the overlay", lambda e, ids: _eq(blends(e), ["screen"]), pre=_pre_overlay),
    Case("set the overlay back to normal", lambda e, ids: _eq(blends(e), ["normal"]), pre=_pre_overlay_screen),
    Case("blend mode", ASKS, pre=_pre_overlay, question="Which blend mode"),
    Case("set the overlay to screen", ASKS, pre=_pre_two_overlays, question="Which overlay"),
    Case("set the overlay to screen", NOOP, question="no overlay clip"),
    Case("make the overlay darker",                        # a brightness request, never a Darken blend
         lambda e, ids: _eq(blends(e), []) if not e.get_track("v2").clips else _eq(blends(e), ["normal"]),
         pre=_pre_overlay),
    # ---- wave E (F1): clip animations -------------------------------------
    Case("add a zoom in animation to the first clip", _anims_only([("zoom_in", None, None), _NA, _NA])),
    Case("make the sticker bounce in", _anims_only([_NA] * 3, sticker=[("bounce", None, None)]), pre=_pre_sticker),
    Case("slide the last clip out", ASKS, question="Which way"),
    Case("slide the last clip out to the left", _anims_only([_NA, _NA, (None, "slide_left", None)])),
    Case("slide the first clip in from the right", _anims_only([("slide_left", None, None), _NA, _NA])),
    Case("make the second clip shake", _anims_only([_NA, (None, None, "shake"), _NA])),
    Case("add a rock animation to this clip", _anims_only([_NA, (None, None, "rock"), _NA])),
    Case("make the sticker spin out", _anims_only([_NA] * 3, sticker=[(None, "spin", None)]), pre=_pre_sticker),
    Case("fade the sticker out", _anims_only([_NA] * 3, sticker=[(None, "fade_out", None)]), pre=_pre_sticker),
    Case("make the second sticker swing", _anims_only([_NA] * 3, sticker=[_NA, (None, None, "swing")]),
         pre=_pre_two_stickers),
    Case("make the sticker bounce in", ASKS, question="Which sticker", ui=NO_SEL, pre=_pre_two_stickers),
    Case("make the sticker bounce in", ASKS, question="no sticker"),
    Case("give the overlay a spin",
         lambda e, ids: (_eq(anims(e), [_NA] * 3),
                         _eq([(c.anim_in, c.anim_out) for c in e.get_track("v2").clips], [("spin", None)])),
         pre=_pre_overlay),
    Case("add a blur in animation to the last clip over 1 second",
         lambda e, ids: (_eq(anims(e), [_NA, _NA, ("blur_in", None, None)]), _eq(v1(e)[2].anim_dur, 1.0))),
    Case("animate the first clip", ASKS, question="Which animation"),
    Case("add an animation", ASKS, question="Which animation"),
    Case("make it pendulum", _anims_only([_NA, (None, None, "pendulum"), _NA])),
    Case("add a zoom in and out animation to every clip", _anims_only([(None, None, "zoom_in_out")] * 3)),
    Case("remove the animation from the second clip", _anims_only([_NA] * 3), pre=_pre_anim_b),
    Case("remove the animation from the first clip", ASKS, question="nothing to take off", pre=_pre_anim_b),
    Case("make the title bounce in", ASKS, question="Text panel"),
    # ---- review RE (wave E fixer): wrong edits and dead ends the re-test hit --
    Case("set the overlay blend mode to multiply", lambda e, ids: _eq(blends(e), ["multiply"]), pre=_pre_overlay),
    Case("make the overlay blend mode screen", lambda e, ids: _eq(blends(e), ["screen"]), pre=_pre_overlay),
    Case("set the overlay's blend to overlay", lambda e, ids: _eq(blends(e), ["overlay"]), pre=_pre_overlay),
    Case("set blend to normal on the overlay", lambda e, ids: _eq(blends(e), ["normal"]), pre=_pre_overlay_screen),
    Case("shake the second clip the whole time",           # CapCut's Shake combo, never Stabilise
         lambda e, ids: (_anims_only([_NA, (None, None, "shake"), _NA])(e, ids),
                         _eq(by_in(e, 4.0).src.endswith("talk.normalized.mp4"), True))),
    Case("add a shake combo to the first clip", _anims_only([(None, None, "shake"), _NA, _NA])),
    Case("raise the first clip's volume by 3 db",
         lambda e, ids: (_eq(gains(e), [3.0, 0.0, 0.0]), _eq(e.canvas.loudness_lufs, -16.0))),
    Case("lower clip 2 by 4 dB", lambda e, ids: _eq(gains(e), [0.0, -4.0, 0.0])),
    Case("lower the voiceover by 6 dB",
         lambda e, ids: (_eq([c.audio.gain_db for c in vo_clips(e)], [-6.0]), _eq(gains(e), [0.0] * 3)),
         pre=_pre_vo),
    Case("lower the voiceover by 6 dB", ASKS, question="no voice-over"),
    Case("make the voice on clip 1 deeper at half strength",
         lambda e, ids: (_voice_only(e, ids, ["deep", None, None]), _eq(v1(e)[0].audio.voice_intensity, 0.5))),
    Case("make the first clip zoom in at the start",       # the In animation, not a whole-clip Ken Burns
         lambda e, ids: (_anims_only([("zoom_in", None, None), _NA, _NA])(e, ids), _eq(kf(v1(e)[0], "scale"), []))),
    Case("make the music quieter",                         # each music clip -6 dB from its OWN level
         lambda e, ids: _eq([c.audio.gain_db for c in music(e).clips], [-18.0, -9.0]), pre=_pre_music_split),
    Case("delete the vintage filter", lambda e, ids: _eq(_fx_types(e), [[], ["vignette"], []]), pre=_pre_effects),
    Case("take off the vignette", lambda e, ids: _eq(_fx_types(e), [["vintage"], [], []]), pre=_pre_effects),
    Case("take off the vignette", NOOP, question="no Vignette"),
    Case("make my voiceover sound like a robot", lambda e, ids: _vo_music(e, "robot"), pre=_pre_vo_on_music),
    Case("add echo to the narration", lambda e, ids: _vo_music(e, "echo"), pre=_pre_vo_on_music),
    Case("remove the voice effect from the voiceover", lambda e, ids: _vo_music(e, None),
         pre=_pre_vo_on_music_robot),
    Case("put a red background behind the video", lambda e, ids: _eq(canvases(e), [("color", "#E53935")] * 3),
         pre=_portrait()),
    Case("blue canvas behind the first clip", lambda e, ids: _eq(canvases(e), [("color", "#1E88E5"), None, None]),
         pre=_portrait()),
    Case("blur the background", ASKS, question="no bars"),     # 16:9 clips on 16:9: nothing letterboxed
    Case("mirror the overlay",
         lambda e, ids: (_eq(flips(e), [(False, False)] * 3),
                         _eq([c.transform.flip_h for c in e.get_track("v2").clips], [True])), pre=_pre_overlay),
    Case("delete the text that says subscribe", lambda e, ids: _eq(texts(e), []), pre=_pre_subscribe),
    Case("add echo to the overlays",                       # plural: the overlays, never every main clip
         lambda e, ids: (_eq(_overlay_fx(e), ["echo", "echo"]), _eq(voices(e), [None] * 3)),
         ui=NO_SEL, pre=_pre_two_overlays),
    Case("put reverb on the pips",
         lambda e, ids: (_eq(_overlay_fx(e), ["reverb", "reverb"]), _eq(voices(e), [None] * 3)),
         pre=_pre_two_overlays),
    Case("make my voice sound like a robot",               # a freeze on v1 no longer refuses the lane
         lambda e, ids: (_eq({c.audio.voice_effect for c in v1(e) if c.freeze is None}, {"robot"}),
                         _eq([c.audio.voice_effect for c in v1(e) if c.freeze is not None], [None])),
         pre=_pre_freeze_v1),
    Case("add an animated title saying hello", lambda e, ids: _eq([t.text for t in texts(e)], ["hello"])),
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
    if case.expect == NOOP:
        assert e.hash() == before, "the timeline changed on a phrase with nothing to do"
        assert (case.question or "") in _question_text(events), _question_text(events)
        return
    assert e.hash() != before, f"nothing changed: {_question_text(events)}"
    ver = next((x for x in events if x["type"] == "verify"), None)
    if ver is not None:                                   # undo / redo have no verify pass
        assert ver["passed"] == ver["total"], ver
    case.expect(e, ids)


def test_the_sweep_is_at_least_eighty_phrases():
    assert len({c.phrase for c in CASES}) >= 80


def test_wave_e_added_forty_new_phrasings():
    """Wave E (F4b): removals by name, clip lengths, relative levels and
    flips — at least 40 phrasings the sweep did not have before."""
    first = CASES.index(next(c for c in CASES if c.phrase == "get rid of the subtitles"))
    wave_e = {c.phrase for c in CASES[first:] if c.phrase not in ("undo", "undo that", "go back")}
    before = {c.phrase for c in CASES[:first]}
    assert len(wave_e - before) >= 40, len(wave_e - before)


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
