"""The CapCut-style transition surface (render/transitions.py, dispatch
add_transition / list_transitions, the compositor): every distinct look has
exactly one family, a display name and a per-transition default duration;
`add_transition` applies that default when the caller names none and the
EDL / compositor read the same number; `list_transitions` advertises it all.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.agent.tools import input_schema_for
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track, Transition
from video_ai_editor.render import render_preview
from video_ai_editor.render import transitions as T


# ---------------------------------------------------------------- catalog

def test_every_distinct_look_is_in_exactly_one_family():
    looks = set(T.NATIVE) | set(T.CUSTOM_EXPRS)
    placed = [n for names in T.FAMILIES.values() for n in names]
    assert set(placed) == looks and len(placed) == len(looks)
    assert set(T.FAMILIES) == set(T.FAMILY_NAMES) == {"Basic", "Wipe", "Slide", "Zoom", "Blur", "Shape",
                                                        "Glitch/Stylised", "Light"}
    for alias, target in T.ALIASES.items():
        assert T.family_of(alias) == T.family_of(target)
    assert T.family_of("nope") is None


def test_entries_carry_display_family_default_and_description():
    entries = T.entries()
    assert len(entries) == len(set(T.NATIVE) | set(T.CUSTOM_EXPRS)) == T.catalog()["looks"]
    names = [e["name"] for e in entries]
    assert len(set(names)) == len(names)
    for e in entries:
        assert e["display"] and e["display"][0].isupper()
        assert e["family"] in T.FAMILY_NAMES and e["kind"] in ("native", "custom", "post")
        assert 0.1 <= e["default_duration"] <= 2.0
        assert all(T.ALIASES[a] == e["name"] for a in e["aliases"])
    by = {e["name"]: e for e in entries}
    assert by["whip"]["kind"] == "post" and by["glitch"]["kind"] == "custom" and by["fade"]["kind"] == "native"
    assert by["fadeblack"]["display"] == "Fade to Black" and by["slideleft"]["display"] == "Slide Left"
    assert by["whipup"]["display"] == "Whip Pan Up" and "pixelate" in by["pixelize"]["aliases"]
    assert by["zoomin"]["family"] == "Zoom" and by["glitch"]["family"] == "Glitch/Stylised"


def test_default_durations_follow_the_family_with_named_overrides():
    assert T.default_duration("fade") == 0.5 and T.default_duration("crossfade") == 0.5
    assert T.default_duration("fadeblack") == 0.6 and T.default_duration("blackout") == 0.6
    assert T.default_duration("whip") == 0.25 == T.default_duration("whippan")
    assert T.default_duration("slideleft") == 0.35 and T.default_duration("push") == 0.35
    assert T.default_duration("zoomin") == 0.3 == T.default_duration("zoom")
    assert T.default_duration("glitch") == 0.3 and T.default_duration("spiral") == 0.6 == T.default_duration("spin")
    assert T.default_duration("not-a-transition") == T.FALLBACK_DURATION_S
    defaults = T.catalog()["defaults"]
    assert set(defaults) == set(T.all_names())


def test_effective_duration_treats_zero_or_none_as_the_default():
    assert T.effective_duration("whip", 0.0) == 0.25
    assert T.effective_duration("whip", None) == 0.25
    assert T.effective_duration("whip", -1) == 0.25
    assert T.effective_duration("whip", 0.05) == 0.25          # below the renderer's minimum
    assert T.effective_duration("fade", 0.7) == 0.7
    assert T.effective_duration("fade", "0.7") == 0.7


# ---------------------------------------------------------------- add_transition / list_transitions

def _two_clip_store(tmp_path: Path) -> EDLStore:
    edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
        Track(id="v1", type="video", clips=[
            Clip(src="/x/a.mp4", in_=0, out=2, start=0, id="c1"),
            Clip(src="/x/b.mp4", in_=0, out=2, start=2, id="c2"),
        ])])
    edl.recompute_duration()
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp_path)


@pytest.mark.parametrize("ttype, expected", [("whip", 0.25), ("fadeblack", 0.6), ("fade", 0.5), ("zoom", 0.3)])
def test_add_transition_uses_the_transitions_own_default(tmp_path, ttype, expected):
    store = _two_clip_store(tmp_path)
    r = dispatch(store, "add_transition", {"at": 2.0, "type": ttype})
    tr = store.edl.get_track("v1").transitions[0]
    assert tr.duration == expected and r["transition_duration"] == expected and r["type"] == ttype
    assert store.edl.duration == pytest.approx(4.0 - expected)
    assert r["shortened_by"] == pytest.approx(expected) and f"({expected:.2f}s)" in r["summary"]


def test_add_transition_honours_an_explicit_duration_and_treats_zero_as_default(tmp_path):
    store = _two_clip_store(tmp_path)
    dispatch(store, "add_transition", {"at": 2.0, "type": "whip", "duration": 0.9})
    assert store.edl.get_track("v1").transitions[0].duration == 0.9
    dispatch(store, "add_transition", {"at": 2.0, "type": "whip", "duration": 0})
    assert store.edl.get_track("v1").transitions[0].duration == 0.25       # replaced, not stacked
    assert len(store.edl.get_track("v1").transitions) == 1


def test_add_transition_accepts_every_catalog_name_and_the_schema_advertises_them(tmp_path):
    names = T.all_names()
    assert input_schema_for("add_transition")["properties"]["type"]["enum"] == names
    assert "default" not in input_schema_for("add_transition")["properties"]["duration"]
    store = _two_clip_store(tmp_path)
    for name in names:
        r = dispatch(store, "add_transition", {"at": 2.0, "type": name})
        assert r["transition_duration"] == T.default_duration(name)
    with pytest.raises(ValueError, match="unknown transition"):
        dispatch(store, "add_transition", {"at": 2.0, "type": "kapow"})


def test_list_transitions_advertises_the_product_surface(tmp_path):
    store = _two_clip_store(tmp_path)
    r = dispatch(store, "list_transitions", {})
    assert r["transitions"] == T.all_names() and r["count"] == len(T.all_names())
    assert r["looks"] == len(r["entries"]) and r["family_order"] == list(T.FAMILY_NAMES)
    assert set(r["families"]) == set(T.FAMILY_NAMES)
    assert set(r["defaults"]) == set(r["transitions"]) and r["defaults"]["whip"] == 0.25
    assert {"name", "display", "family", "category", "default_duration", "description", "aliases", "kind"} <= set(r["entries"][0])
    assert all(e["name"] in r["transitions"] for e in r["entries"])
    assert r["catalog"]["categories"] == T.CATEGORIES                        # the renderer's grouping is untouched


# The frontend's catalog tests run against a RECORDED copy of this handler's
# output (frontend/src/lib/__fixtures__/list_transitions.json) rather than a
# live backend. That copy drifted once without anything noticing: the
# descriptions in render/transitions.py were rewritten while every name,
# family and default stayed the same, so the vitest assertions (which check
# shape, not copy) kept passing over stale text. This pins the recording to
# the live handler field-for-field — names, families, defaults AND
# descriptions — so any change to the surface fails here until the fixture
# is regenerated (the command is in transitionCatalog.test.ts's header).
_FRONTEND_FIXTURE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "__fixtures__" / "list_transitions.json"


def _fixture_and_live(tmp_path: Path) -> tuple[dict, dict]:
    recorded = json.loads(_FRONTEND_FIXTURE.read_text())
    live = dispatch(_two_clip_store(tmp_path), "list_transitions", {})
    return recorded, json.loads(json.dumps(live))       # round-trip: compare what JSON can carry, as the fixture does


def test_frontend_fixture_matches_the_live_list_transitions_output(tmp_path):
    recorded, live = _fixture_and_live(tmp_path)
    # Field-by-field first so a drift names the field, then the whole payload
    # so nothing added to the handler later can slip past unrecorded.
    assert recorded["transitions"] == live["transitions"]
    assert recorded["families"] == live["families"] and recorded["family_order"] == live["family_order"]
    assert recorded["defaults"] == live["defaults"]
    assert recorded["catalog"]["descriptions"] == live["catalog"]["descriptions"]
    assert {e["name"]: e["description"] for e in recorded["entries"]} == \
        {e["name"]: e["description"] for e in live["entries"]}
    assert recorded == live


def test_frontend_fixture_descriptions_are_the_renderers_own(tmp_path):
    # The field that drifted: every recorded description is the renderer's
    # current copy, and the per-entry copy agrees with the catalog map.
    recorded, _ = _fixture_and_live(tmp_path)
    for entry in recorded["entries"]:
        assert entry["description"] == T.DESCRIPTIONS[entry["name"]]
        assert recorded["catalog"]["descriptions"][entry["name"]] == entry["description"]


# ---------------------------------------------------------------- compositor

def _mk_video(path: Path, *, color: str, duration: float = 2.0) -> None:
    keyed = path.with_suffix(".keyed.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={duration}:r=30",
                    "-pix_fmt", "yuv420p", str(keyed)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(keyed), "-f", "lavfi", "-i", f"sine=f=440:duration={duration}",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)], check=True, capture_output=True)


def _duration(path: Path) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
                          capture_output=True, text=True, check=True)
    return float(json.loads(proc.stdout)["format"]["duration"])


def test_compositor_renders_a_zero_duration_record_with_the_transitions_default(tmp_path: Path):
    """A legacy `duration: 0` record is not a 0 s xfade (ffmpeg would refuse)
    — the rendered length reflects the whip's 0.25 s default overlap, and a
    stored explicit 0.6 s renders 0.6 s shorter."""
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _mk_video(a, color="red")
    _mk_video(b, color="blue")
    for stored, expected_overlap in ((0.0, 0.25), (0.6, 0.6)):
        d = tmp_path / f"r{stored}"
        d.mkdir()
        edl = EDL(canvas=Canvas(w=320, h=180, fps=30), tracks=[
            Track(id="v1", type="video", clips=[
                Clip(src=str(a), in_=0, out=2, start=0, id="c1"),
                Clip(src=str(b), in_=0, out=2, start=2, id="c2"),
            ], transitions=[Transition(at=2.0, type="whip", duration=stored)])])
        (d / "edl.json").write_text(edl.model_dump_json())
        out = render_preview(EDLStore(d).edl, d, height=180).path
        assert _duration(out) == pytest.approx(4.0 - expected_overlap, abs=0.12)
