"""Wave E review fixes (the RE fixer): dispatch, verifier and prompt-layer
regressions the export/engine/editor re-testers found, each pinned by a test
that failed on the tree they reviewed.

Render-level fixes have their own decoded-render tests
(tests/test_review_e_render.py); the Prompt-bar phrasings are in the sweep
(tests/test_prompt_capcut_sweep.py, "review RE" block).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.agent.prompt import schema as Sc
from video_ai_editor.agent.prompt import verify as V
from video_ai_editor.agent.prompt.facts import TimelineFacts
from video_ai_editor.agent.prompt.validate import validate_plan
from video_ai_editor.edl.schema import Canvas, Clip, Sticker, Track, empty_edl
from video_ai_editor.edl.snapshot import EDLStore


def _media(tmp: Path, name: str, *, audio: bool = True, dur: float = 4.0) -> Path:
    p = tmp / f"{name}.mp4"
    if p.exists():
        return p
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=64x36:r=30:d={dur}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={dur}", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)]
    subprocess.run(args, check=True, capture_output=True)
    return p


@pytest.fixture
def store(tmp_path) -> EDLStore:
    st = EDLStore(tmp_path / "sess")
    src = _media(tmp_path, "a")
    e = empty_edl(Canvas(w=1920, h=1080, fps=30))
    v1 = e.get_track("v1")
    v1.clips = [Clip(src=str(src), in_=0, out=2, start=0, id="c1"),
                Clip(src=str(src), in_=2, out=2 + 1 / 30, start=2, id="fz", freeze=1.0),
                Clip(src=str(src), in_=2, out=4, start=3, id="c2")]
    v2 = e.get_track("v2")
    v2.clips = [Clip(src=str(src), in_=0, out=1, start=0.5, id="p1"),
                Clip(src=str(src), in_=1, out=2, start=2.5, id="p2")]
    st.edl = e
    st.commit("init", {}, "init")
    return st


# ---------------------------------------------------------------- dispatch

def test_voice_effect_on_a_lane_skips_its_freeze_frame(store):
    """review RE: set_voice_effect(track='v1') refused the WHOLE edit when the
    lane held a freeze — and the Prompt bar's default target is track=v1."""
    dispatch(store, "set_voice_effect", {"track": "v1", "effect": "robot"})
    got = {c.id: c.audio.voice_effect for c in store.edl.get_track("v1").clips}
    assert got == {"c1": "robot", "fz": None, "c2": "robot"}


def test_a_named_freeze_frame_is_still_refused(store):
    with pytest.raises(ValueError, match="freeze frame"):
        dispatch(store, "set_voice_effect", {"clip_id": "fz", "effect": "robot"})


def test_voice_effect_refuses_a_clip_whose_file_has_no_sound(store, tmp_path):
    """review RE: a picture-only overlay took 'Robot' and 'Preview' played
    silence."""
    silent = _media(tmp_path, "silent", audio=False)
    store.edl.get_track("v2").clips.append(Clip(src=str(silent), in_=0, out=1, start=5, id="mute"))
    store.commit("add", {}, "add")
    with pytest.raises(ValueError, match="no sound"):
        dispatch(store, "set_voice_effect", {"clip_id": "mute", "effect": "robot"})
    dispatch(store, "set_voice_effect", {"track": "v2", "effect": "echo"})     # the lane covers the sounding ones
    got = {c.id: c.audio.voice_effect for c in store.edl.get_track("v2").clips}
    assert got == {"p1": "echo", "p2": "echo", "mute": None}


# ---------------------------------------------------------------- verifier

def _facts(store) -> TimelineFacts:
    return TimelineFacts.minimal(v1_clip_ids=["c1", "fz", "c2"], clip_ids=["c1", "fz", "c2", "p1", "p2"],
                                 track_ids=[t.id for t in store.edl.tracks])


def _verify(store, tool: str, args: dict) -> list:
    p = validate_plan(Sc.Plan.new(intent="t", brain="claude", steps=[Sc.Step(tool=tool, args=args, why="t")]),
                      _facts(store))
    ctx = SimpleNamespace(edl=store.edl)
    return [V.CHECKS[pc.check](ctx, pc) for pc in V._postconditions(p)]


@pytest.mark.parametrize("tool,args", [
    ("set_blend_mode", {"clip_ids": ["p1", "p2"], "mode": "screen"}),
    ("set_voice_effect", {"clip_ids": ["p1", "p2"], "effect": "robot"}),
    ("set_voice_effect", {"track": "v1", "effect": "robot"}),
    ("set_animation", {"clip_ids": ["p1", "p2"], "in": "zoom_in"}),
    ("set_animation", {"track": "v2", "in": "zoom_in"}),
    ("set_canvas_background", {"all": True, "type": "blur", "blur": 3}),
])
def test_a_model_plan_with_every_advertised_target_form_verifies(store, tool, args):
    """review RE: the default postconditions bound only `clip_id`, so a
    correct model plan using `clip_ids` or `track` always failed (or, for an
    animation, was judged on every v1 clip)."""
    dispatch(store, tool, dict(args))
    res = _verify(store, tool, args)
    assert res and all(r.passed for r in res), res


def test_the_multi_target_check_still_fails_a_partial_edit(store):
    dispatch(store, "set_blend_mode", {"clip_id": "p1", "mode": "screen"})        # only one of the two
    res = _verify(store, "set_blend_mode", {"clip_ids": ["p1", "p2"], "mode": "screen"})
    assert res and not res[0].passed, res
    dispatch(store, "set_animation", {"clip_id": "p1", "in": "zoom_in"})
    res = _verify(store, "set_animation", {"track": "v2", "in": "zoom_in"})
    assert res and not res[0].passed, res


def test_an_animation_on_a_sticker_lane_verifies(tmp_path):
    st = EDLStore(tmp_path / "s2")
    e = empty_edl(Canvas(w=1080, h=1920, fps=30))
    e.tracks.append(Track(id="stk", type="sticker", z=9,
                          clips=[Sticker(src="star.png", start=0, end=2, id="s1")]))
    st.edl = e
    st.commit("init", {}, "init")
    dispatch(st, "set_animation", {"track": "stk", "combo": "shake"})
    res = [V.CHECKS[pc.check](SimpleNamespace(edl=st.edl), pc)
           for pc in Sc.bind_postconditions("set_animation", {"track": "stk", "combo": "shake"})]
    assert res and all(r.passed for r in res), res


# ---------------------------------------------------------------- one mirror model

def test_flip_clip_folds_the_legacy_effects_panel_flip(store):
    """review RE: Effects > Flip H added an `hflip` effect the Inspector's
    Flip button could not see; pressing it set flip_h and the render showed
    the clip UNflipped. The toggle now starts from what the picture shows and
    folds the effect into Transform.flip_h."""
    dispatch(store, "add_effect", {"clip_id": "c1", "type": "hflip", "params": {}})
    dispatch(store, "flip_clip", {"clip_id": "c1", "axis": "horizontal"})          # a toggle: it WAS mirrored
    c = store.edl.get_clip("c1")[1]
    assert (c.transform.flip_h, [e.type for e in c.effects]) == (False, [])
    dispatch(store, "add_effect", {"clip_id": "c1", "type": "hflip", "params": {}})
    dispatch(store, "flip_clip", {"clip_id": "c1", "axis": "horizontal", "value": True})
    c = store.edl.get_clip("c1")[1]
    assert (c.transform.flip_h, [e.type for e in c.effects]) == (True, [])


def test_the_prompt_facts_read_the_mirror_the_picture_shows(store):
    from video_ai_editor.agent.prompt.facts import _edit_facts
    dispatch(store, "add_effect", {"clip_id": "c2", "type": "vflip", "params": {}})
    clips, _t, _tr = _edit_facts(store, store.edl)
    got = {c.id: (c.flip_h, c.flip_v) for c in clips if c.track == "v1"}
    assert got["c2"] == (False, True) and got["c1"] == (False, False)


# ---------------------------------------------------------------- one preset table

def test_the_canvas_and_blend_tool_schemas_read_the_one_table():
    """review RE: tools.py / validate.py restated the canvas_blend and
    clip_animations tables by hand; a new mode or level would have been
    refused by the schema while the handler accepted it."""
    from video_ai_editor.agent import tools
    from video_ai_editor.agent.prompt import validate as PV
    from video_ai_editor.edl import canvas_blend as CB
    from video_ai_editor.edl.clip_animations import ANIM_DUR_RANGE
    by = {t["name"]: t for t in tools.ALL_TOOLS}
    assert by["set_blend_mode"]["input_schema"]["properties"]["mode"]["enum"] == list(CB.BLEND_IDS)
    for b in CB.BLENDS:
        assert b.id in by["set_blend_mode"]["description"]
    assert by["set_canvas_background"]["input_schema"]["properties"]["type"]["enum"] == [*CB.CANVAS_KINDS, "none"]
    assert tools._ARG_BOUNDS[("set_canvas_background", "blur")] == (1.0, float(len(CB.CANVAS_BLUR_LEVELS)))
    for side in ("in_duration", "out_duration"):
        assert tools._ARG_BOUNDS[("set_animation", side)] == tuple(map(float, ANIM_DUR_RANGE))
        assert PV.ARG_BOUNDS[("set_animation", side)] == tuple(ANIM_DUR_RANGE)


# ---------------------------------------------------------------- canvas pictures

def _png(path: Path, size=(64, 48), color=(200, 120, 40)) -> Path:
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture
def canvas_client(store, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import main
    from video_ai_editor.api import canvas_blend_routes as R
    monkeypatch.setattr(R, "_RESOLVE_STORE", lambda sid: store)
    return TestClient(main.app, base_url="http://127.0.0.1")


@pytest.mark.skipif(subprocess.run(["which", "sips"], capture_output=True).returncode != 0, reason="needs sips")
def test_a_heic_canvas_picture_is_decoded_not_refused(store, canvas_client, tmp_path):
    """review RE: .heic was advertised (picker, presets, prompt) but Pillow
    here cannot read it, so every iPhone photo was answered 400."""
    heic = tmp_path / "IMG_0001.HEIC"
    subprocess.run(["sips", "-s", "format", "heic", str(_png(tmp_path / "p.png")), "--out", str(heic)],
                   check=True, capture_output=True)
    r = canvas_client.post("/api/sessions/s/canvas-bg/upload",
                           files={"file": ("IMG_0001.HEIC", heic.read_bytes(), "image/heic")})
    assert r.status_code == 200, r.text
    got = Path(r.json()["src"])
    assert got.suffix == ".png" and got.is_file()
    assert [p.suffix.lower() for p in got.parent.iterdir()] == [".png"]          # one picture, not two
    from PIL import Image
    assert max(abs(a - b) for a, b in zip(Image.open(got).convert("RGB").getpixel((10, 10)), (200, 120, 40))) <= 3
    # and a Media-panel HEIC original, through dispatch
    orig = tmp_path / "IMG_0002.heic"
    orig.write_bytes(heic.read_bytes())
    dispatch(store, "set_canvas_background", {"clip_id": "c1", "type": "image", "image": str(orig)})
    img = Path(store.edl.get_clip("c1")[1].canvas_bg.image)
    assert img.suffix == ".png" and img.is_file()


def test_a_picture_over_the_pixel_cap_is_refused(store, canvas_client, tmp_path):
    """review RE: a flat 10000×6000 PNG (a few KB) passed the upload and was
    decoded in full on every render."""
    from video_ai_editor.render import canvas_bg as CBG
    big = _png(tmp_path / "big.png", size=(10000, 6000))
    assert big.stat().st_size < 1_000_000
    r = canvas_client.post("/api/sessions/s/canvas-bg/upload", files={"file": ("big.png", big.read_bytes(), "image/png")})
    assert r.status_code == 400 and "MP" in r.json()["error"]["message"], r.text
    with pytest.raises(ValueError, match="MP"):
        dispatch(store, "set_canvas_background", {"clip_id": "c1", "type": "image", "image": str(big)})
    assert CBG.image_file(big, 64, 36, cache_dir=tmp_path / "c") is None       # a hostile .vae lands here
    ok = _png(tmp_path / "ok.png", size=(4000, 3000))
    assert CBG.image_file(ok, 64, 36, cache_dir=tmp_path / "c") is not None


# ---------------------------------------------------------------- forward compatibility

def test_a_newer_builds_wave_e_values_load_as_their_defaults(monkeypatch):
    """review RE: an unknown animation name loaded, but an unknown blend
    mode, canvas type or voice id made the WHOLE EDL unloadable — a project
    saved by a newer build could not be opened. One policy now: log and fall
    back (blend → normal, canvas → none, voice → none)."""
    from video_ai_editor.edl.schema import EDL
    e = empty_edl(Canvas(w=1920, h=1080, fps=30))
    e.get_track("v1").clips = [Clip(src="a.mp4", in_=0, out=2, start=0, id="c1")]
    e.get_track("v2").clips = [Clip(src="a.mp4", in_=0, out=2, start=0, id="p1")]
    raw = e.model_dump(mode="json", by_alias=True)
    raw["tracks"][0]["clips"][0]["canvas_bg"] = {"type": "gradient", "color": "#FF0000"}
    raw["tracks"][0]["clips"][0]["audio"]["voice_effect"] = "alien"
    raw["tracks"][0]["clips"][0]["anim_in"] = "future_preset"
    v2 = next(t for t in raw["tracks"] if t["id"] == "v2")
    v2["clips"][0]["blend"] = "hue"
    from video_ai_editor.edl import schema as SCH
    logged: list[str] = []
    monkeypatch.setattr(SCH, "_log_unknown", lambda what, v, valid: logged.append(str(v)))
    got = EDL.model_validate(raw)
    c1, p1 = got.get_clip("c1")[1], got.get_clip("p1")[1]
    assert (c1.canvas_bg, c1.audio.voice_effect, c1.anim_in, p1.blend) == (None, None, None, "normal")
    assert {"gradient", "alien", "hue"} <= set(logged), logged
    # a known alias still resolves, and the typed tools still refuse
    raw["tracks"][0]["clips"][0]["canvas_bg"] = {"type": "blur", "blur": 3}
    v2["clips"][0]["blend"] = "Linear Dodge"
    got = EDL.model_validate(raw)
    assert got.get_clip("p1")[1].blend == "add" and got.get_clip("c1")[1].canvas_bg.blur == 3
