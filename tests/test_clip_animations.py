"""Clip animations (wave E, F1): the ONE preset table, the model fields, the
`set_animation` op, splits/cuts/trims, .vae save/open, the GET route, the
agent tool and the browser's generated copy of the table.

The rendered pixels are pinned by tests/test_clip_anim_render.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import clip_animations as A
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Keyframe, Sticker, TextClip, Track

import gen_clip_anim_table as GEN


# ------------------------------------------------------------------ table

def test_generated_browser_table_matches_the_python_table():
    assert json.loads(GEN.TABLE.read_text()) == json.loads(json.dumps(A.table_json())), \
        "run .venv/bin/python tests/gen_clip_anim_table.py"
    assert json.loads(GEN.CASES.read_text()) == json.loads(json.dumps(
        {"channels": list(GEN.CHANNELS), "cases": GEN.cases()}, separators=(",", ":"))), \
        "run .venv/bin/python tests/gen_clip_anim_table.py"


def test_the_capcut_sets_are_all_there():
    assert [p.label for p in A.IN_PRESETS] == [
        "Fade In", "Zoom In", "Zoom Out", "Slide Left", "Slide Right", "Slide Up", "Slide Down",
        "Rotate", "Spin", "Blur In", "Bounce"]
    assert [p.label for p in A.OUT_PRESETS] == [
        "Fade Out", "Zoom In", "Zoom Out", "Slide Left", "Slide Right", "Slide Up", "Slide Down",
        "Rotate", "Spin", "Blur Out", "Bounce"]
    assert [p.label for p in A.COMBO_PRESETS] == ["Rock", "Swing", "Pendulum", "Shake", "Zoom In-Out"]


@pytest.mark.parametrize("kind,raw,want", [
    ("in", "Zoom In", "zoom_in"), ("in", "zoom-in", "zoom_in"), ("in", "ZOOMIN", "zoom_in"),
    ("in", "fade", "fade_in"), ("out", "fade", "fade_out"), ("out", "Blur", "blur_out"),
    ("combo", "Zoom In-Out", "zoom_in_out"), ("combo", "rock", "rock"),
    ("in", "wobble", None), ("combo", "fade_in", None), ("in", 3, None)])
def test_preset_spellings(kind, raw, want):
    assert A.preset_id(kind, raw) == want


def test_durations_mirror_text_and_never_overlap():
    assert A.duration_of(None, 10) == A.ANIM_DUR_DEFAULT
    assert A.duration_of(9, 100) == A.ANIM_DUR_RANGE[1]
    assert A.duration_of(0.01, 100) == A.ANIM_DUR_RANGE[0]
    assert A.duration_of(3, 1) == pytest.approx(0.4)
    pl = A.plan("zoom_in", "slide_left", None, 3, 3, 1.0)
    assert pl.d_in + pl.d_out <= 1.0 + 1e-12


def test_in_ends_and_out_starts_at_the_rest_pose():
    for p_in, p_out in zip(A.IN_PRESETS, A.OUT_PRESETS):
        pl = A.plan(p_in.id, p_out.id, None, 0.5, 0.5, 3.0)
        for t in (0.5, 1.0, 1.5, 2.5):
            assert pl.value("scale", t) == pytest.approx(1.0)
            for ch in ("x", "y", "rotation"):
                assert pl.value(ch, t) == pytest.approx(0.0, abs=1e-12)
            assert A.fade_gain(pl, t) == pytest.approx(1.0)
            assert A.blur_weight(pl, t) == 0.0


def test_expressions_evaluate_to_the_plan_values():
    """Each channel's ffmpeg expression, evaluated like ffmpeg would (its
    functions mapped onto Python), gives `AnimPlan.value` — the text the
    renderers bake is the model the tests and the browser use."""
    import math
    import re
    env = {"if": lambda c, a, b: a if c else b, "lt": lambda a, b: 1.0 if a < b else 0.0,
           "gte": lambda a, b: 1.0 if a >= b else 0.0, "pow": math.pow, "sin": math.sin,
           "clip": lambda x, lo, hi: min(hi, max(lo, x)), "PI": math.pi}
    for spec in [("bounce", "spin", None), ("slide_up", "zoom_out", None), (None, None, "pendulum"),
                 (None, None, "shake"), ("rotate", "rotate", None)]:
        pl = A.plan(*spec, None, 0.8, 2.4)
        for ch in ("scale", "x", "y", "rotation"):
            ex = pl.expr(ch, "t")
            if ex is None:
                assert not pl.animates(ch)
                continue
            src = re.sub(r"\bif\(", "if_(", ex.replace("\\,", ","))
            for k in range(0, 73, 3):
                t = k / 30
                got = eval(src, {"if_": lambda c, a, b: a if c else b, **env, "t": t})  # noqa: S307
                assert got == pytest.approx(pl.value(ch, t), abs=2e-4), (spec, ch, t)


# ------------------------------------------------------------------ model

def test_model_fields_default_to_absent_and_round_trip():
    c = Clip(src="/x.mp4", out=2.0)
    assert "anim" not in c.model_dump_json(by_alias=True)
    c.anim_in, c.anim_out, c.anim_dur, c.anim_out_dur = "Zoom In", "fade", 1.0, 9.0
    assert (c.anim_in, c.anim_out, c.anim_dur, c.anim_out_dur) == ("zoom_in", "fade_out", 1.0, A.ANIM_DUR_RANGE[1])
    back = Clip.model_validate_json(c.model_dump_json(by_alias=True))
    assert (back.anim_in, back.anim_out, back.anim_dur, back.anim_out_dur) == ("zoom_in", "fade_out", 1.0, 3.0)
    s = Sticker(src="/a.png", start=0, end=2, anim_combo="shake")
    assert Sticker.model_validate_json(s.model_dump_json()).anim_combo == "shake"


def test_an_unknown_stored_name_loads_as_none():
    c = Clip.model_validate({"src": "/x.mp4", "out": 2, "anim_in": "from_a_newer_build"})
    assert c.anim_in is None


def test_a_combo_excludes_in_and_out_in_the_model():
    c = Clip.model_validate({"src": "/x.mp4", "out": 2, "anim_in": "zoom_in", "anim_combo": "rock"})
    assert (c.anim_in, c.anim_combo) == (None, "rock")


def test_hash_of_an_unanimated_edl_is_unchanged_by_the_fields():
    """The fields are omitted while unset: an existing EDL keeps its hash."""
    e = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(src="/x.mp4", out=2, id="c1")])])
    d = json.loads(e.model_dump_json(by_alias=True))
    assert not any(k.startswith("anim") for k in d["tracks"][0]["clips"][0])


# ------------------------------------------------------------------ dispatch

def _store(tmp_path: Path) -> EDLStore:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(id="c1", src="/fake/a.mp4", in_=0.0, out=4.0, start=0.0),
            Clip(id="c2", src="/fake/b.mp4", in_=0.0, out=3.0, start=4.0)]),
        Track(id="v2", type="video", z=1, clips=[Clip(id="p1", src="/fake/p.mp4", in_=0.0, out=2.0, start=1.0)]),
        Track(id="music", type="music", clips=[Clip(id="m1", src="/fake/m.mp3", in_=0.0, out=4.0)]),
        Track(id="tx", type="text", clips=[TextClip(id="t1", text="hi", start=0.0, end=1.0)]),
        Track(id="stickers", type="sticker", clips=[Sticker(id="s1", src="/fake/s.png", start=0.0, end=2.0)]),
    ])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json(by_alias=True))
    return EDLStore(tmp_path)


def _clip(store, cid):
    return store.edl.get_clip(cid)[1]


def test_set_animation_sets_in_out_and_durations_in_one_undo_step(tmp_path):
    store = _store(tmp_path)
    n_ops = len(store.ops.ops)
    r = dispatch(store, "set_animation", {"clip_id": "c1", "in": "Zoom In", "out": "slide-left",
                                           "in_duration": 0.8, "out_duration": 1.2})
    c = _clip(store, "c1")
    assert (c.anim_in, c.anim_out, c.anim_dur, c.anim_out_dur) == ("zoom_in", "slide_left", 0.8, 1.2)
    assert "Zoom In" in r["summary"] and "Slide Left" in r["summary"]
    assert len(store.ops.ops) == n_ops + 1 and store.ops.ops[-1].tool == "set_animation"
    dispatch(store, "undo", {})
    c = _clip(store, "c1")
    assert (c.anim_in, c.anim_out) == (None, None)


def test_combo_and_in_out_replace_each_other(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "p1", "in": "fade_in", "out": "bounce"})
    dispatch(store, "set_animation", {"clip_id": "p1", "combo": "rock"})
    c = _clip(store, "p1")
    assert (c.anim_in, c.anim_out, c.anim_combo) == (None, None, "rock")
    dispatch(store, "set_animation", {"clip_id": "p1", "out": "fade_out"})
    c = _clip(store, "p1")
    assert (c.anim_in, c.anim_out, c.anim_combo) == (None, "fade_out", None)
    dispatch(store, "set_animation", {"clip_id": "p1", "out": "none"})
    assert _clip(store, "p1").anim_out is None
    with pytest.raises(ValueError, match="replaces In and Out"):
        dispatch(store, "set_animation", {"clip_id": "p1", "in": "zoom_in", "combo": "rock"})


def test_set_animation_on_stickers_and_many_clips(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "s1", "in": "bounce"})
    assert _clip(store, "s1").anim_in == "bounce"
    dispatch(store, "set_animation", {"track": "v1", "combo": "shake"})
    assert [_clip(store, c).anim_combo for c in ("c1", "c2")] == ["shake", "shake"]
    dispatch(store, "set_animation", {"clip_ids": ["c1", "p1"], "in": "blur in"})
    assert [_clip(store, c).anim_in for c in ("c1", "p1")] == ["blur_in", "blur_in"]


@pytest.mark.parametrize("args,msg", [
    ({"clip_id": "c1", "in": "wobble"}, "unknown in animation"),
    ({"clip_id": "c1", "combo": "fade_in"}, "unknown combo animation"),
    ({"clip_id": "c1"}, "needs `in`, `out` or `combo`"),
    ({"in": "fade_in"}, "needs a clip_id"),
    ({"clip_id": "m1", "in": "fade_in"}, "audio lane"),
    ({"clip_id": "t1", "in": "fade_in"}, "is text"),
    ({"clip_id": "nope", "in": "fade_in"}, "not found"),
    ({"clip_id": "c1", "in": "fade_in", "in_duration": 9}, "between 0.1 and 3"),
    ({"clip_id": "c1", "in": "fade_in", "in_duration": float("nan")}, "number"),
])
def test_set_animation_refuses_clearly(tmp_path, args, msg):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match=msg):
        dispatch(store, "set_animation", args)


@pytest.mark.parametrize("alias", [
    {"anim_in": "zoom_in"}, {"anim_out": "fade_out"}, {"anim_combo": "rock"},
    {"anim_dur": 1.0}, {"duration": 1.0}, {"anim_out_dur": 1.0},
])
def test_set_animation_reads_only_its_advertised_names(tmp_path, alias):
    """One contract (gate X3): the tool schema, the Prompt validator and the
    plan schema all say `in`/`out`/`combo` and `in_duration`/`out_duration`.
    The EDL field names used to be read as silent aliases — a call the
    schema rejects must not quietly work through dispatch either."""
    store = _store(tmp_path)
    n_ops = len(store.ops.ops)
    with pytest.raises(ValueError, match="needs `in`, `out` or `combo`"):
        dispatch(store, "set_animation", {"clip_id": "c1", **alias})
    c = _clip(store, "c1")
    assert (c.anim_in, c.anim_out, c.anim_combo) == (None, None, None)
    assert len(store.ops.ops) == n_ops


def test_set_property_rejects_an_unknown_animation_name(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="unknown in animation"):
        dispatch(store, "set_property", {"clip_id": "c1", "path": "anim_in", "value": "wobble"})
    dispatch(store, "set_property", {"clip_id": "c1", "path": "anim_in", "value": "Spin"})
    assert _clip(store, "c1").anim_in == "spin"


def test_split_keeps_in_on_the_left_and_out_on_the_right(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "c1", "in": "zoom_in", "out": "fade_out",
                                       "in_duration": 0.7, "out_duration": 0.9})
    r = dispatch(store, "split_at", {"track": "v1", "time": 2.0})
    left, right = _clip(store, "c1"), _clip(store, r["halves"]["c1"])
    assert (left.anim_in, left.anim_dur, left.anim_out, left.anim_out_dur) == ("zoom_in", 0.7, None, None)
    assert (right.anim_in, right.anim_dur, right.anim_out, right.anim_out_dur) == (None, None, "fade_out", 0.9)


def test_split_keeps_a_combo_on_both_pieces(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "c2", "combo": "rock"})
    r = dispatch(store, "split_at", {"track": "v1", "time": 5.0})
    assert _clip(store, "c2").anim_combo == "rock" and _clip(store, r["halves"]["c2"]).anim_combo == "rock"


def test_cut_inside_a_clip_partitions_the_animation(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "c1", "in": "slide_up", "out": "spin"})
    dispatch(store, "cut_range", {"track": "v1", "start": 1.0, "end": 2.0})
    v1 = store.edl.get_track("v1").clips
    assert (v1[0].anim_in, v1[0].anim_out) == ("slide_up", None)
    assert (v1[1].anim_in, v1[1].anim_out) == (None, "spin")


def test_trims_keep_the_animation_on_the_clip(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "c1", "in": "zoom_out", "out": "slide_down"})
    dispatch(store, "trim_clip", {"clip_id": "c1", "in": 1.0})
    dispatch(store, "trim_clip", {"clip_id": "c1", "out": 3.0})
    c = _clip(store, "c1")
    assert (c.anim_in, c.anim_out) == ("zoom_out", "slide_down")


def test_duplicate_copies_the_animation(tmp_path):
    store = _store(tmp_path)
    dispatch(store, "set_animation", {"clip_id": "s1", "combo": "swing"})
    dispatch(store, "duplicate_clip", {"clip_id": "s1"})
    st = store.edl.get_track("stickers").clips
    assert [s.anim_combo for s in st] == ["swing", "swing"]


def test_vae_save_and_open_keep_the_animation(tmp_path, monkeypatch):
    from video_ai_editor import storage as _storage, storage_project as _sp
    from video_ai_editor.storage_project import load_project, save_project
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setattr(_sp, "session_dir", lambda sid: tmp_path / "wd" / sid)
    sd = tmp_path / "wd" / "s1"
    sd.mkdir(parents=True)
    src = sd / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=30",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    png = sd / "st.png"
    from PIL import Image
    Image.new("RGBA", (20, 10), (255, 0, 0, 255)).save(png)
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(src=str(src), in_=0, out=2, id="c1", anim_in="spin",
                                                 anim_out="blur_out", anim_dur=0.4, anim_out_dur=0.6)]),
        Track(id="stickers", type="sticker", clips=[Sticker(src=str(png), start=0, end=2, id="s1",
                                                            anim_combo="zoom_in_out")])])
    edl.recompute_duration()
    (sd / "edl.json").write_text(edl.model_dump_json(by_alias=True))
    EDLStore(sd)
    save_project("s1", tmp_path / "p.vae")
    new = EDLStore(tmp_path / "wd" / load_project(tmp_path / "p.vae"))
    c = new.edl.get_clip("c1")[1]
    assert (c.anim_in, c.anim_out, c.anim_dur, c.anim_out_dur) == ("spin", "blur_out", 0.4, 0.6)
    assert new.edl.get_clip("s1")[1].anim_combo == "zoom_in_out"


# ------------------------------------------------------------------ surfaces

def test_get_route_serves_the_table():
    from fastapi.testclient import TestClient
    from video_ai_editor.main import app
    r = TestClient(app).get("/api/animations/presets")
    assert r.status_code == 200
    assert r.json() == json.loads(json.dumps(A.table_json()))


def test_agent_tool_advertises_every_preset():
    from video_ai_editor.agent.tools import ALL_TOOLS
    from video_ai_editor.agent.dispatch import DISPATCH
    t = next(t for t in ALL_TOOLS if t["name"] == "set_animation")
    props = t["input_schema"]["properties"]
    for kind in ("in", "out", "combo"):
        assert props[kind]["enum"] == [*A.PRESET_IDS[kind], "none"]
    assert props["in_duration"]["minimum"] == A.ANIM_DUR_RANGE[0]
    assert props["in_duration"]["maximum"] == A.ANIM_DUR_RANGE[1]
    assert "set_animation" in DISPATCH


# ------------------------------------------------------------------ caches

def test_render_caches_key_on_the_animation():
    """A chunk / the video-only mp4 bake the animation, so their keys carry
    it (only when set: every existing key is unchanged); a long animated clip
    never previews from source segments (its frames depend on its own clock)."""
    from video_ai_editor.render.chunks import fingerprint_clip
    from video_ai_editor.render.compositor import _video_only_fingerprint
    from video_ai_editor.render.segments import segment_bounds
    c = Clip(src="/nonexistent.mp4", out=30.0, id="c1")
    kw = dict(canvas_w=320, canvas_h=180, fps=30, encoder_args=["x"])
    plain = fingerprint_clip(c, **kw)
    e = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[Track(id="v1", type="video", clips=[c])])
    vo_plain = _video_only_fingerprint(e)
    c.anim_in = "zoom_in"
    assert fingerprint_clip(c, **kw) != plain
    assert _video_only_fingerprint(e) != vo_plain
    keyed = fingerprint_clip(c, **kw)
    c.anim_dur = 1.0
    assert fingerprint_clip(c, **kw) != keyed
    assert segment_bounds(c, 30, 6.0) is None
    c.anim_in, c.anim_dur = None, None
    assert fingerprint_clip(c, **kw) == plain and _video_only_fingerprint(e) == vo_plain
