"""Presets (spec §2.8): the generated music beds measure what their sidecars
promise (180 s, −18 LUFS, the stated BPM as librosa hears it), the text
styles place text inside the safe zone of every canvas, every transition
look and every catalog entry names a look the renderer has, the templates
name real recipes, and a malformed preset file fails loudly at load.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from video_ai_editor import config
from video_ai_editor.agent.prompt import presets as P
from video_ai_editor.agent.prompt import recipes as R
from video_ai_editor.agent.prompt import validate as V
from video_ai_editor.render import transitions as T


# --- music beds (generated once per session into a temp dir) -------------------

@pytest.fixture(scope="module")
def beds(tmp_path_factory) -> list[P.MusicBed]:
    out = tmp_path_factory.mktemp("beds")
    made = P.generate_music_beds(out)
    assert len(made) == len(P.BED_SPECS)
    return made


def test_beds_are_180s_stereo_48k_and_load_back_from_their_sidecars(beds):
    from video_ai_editor.ingest.probe import probe
    by_name = {b.name: b for b in beds}
    assert set(by_name) == {n for n, _, _ in P.BED_SPECS}
    for b in beds:
        info = probe(b.path)
        assert info.duration == pytest.approx(P.BED_DURATION_S, abs=0.1), b.name
        meta = json.loads(b.path.with_suffix(".json").read_text())
        assert meta["bpm"] == b.bpm and meta["source"] == "procedural" and meta["beat_grid_offset"] == 0.0
    reloaded = P.music_beds(beds[0].path.parent)
    assert {b.name for b in reloaded} == set(by_name) and all(b.duration_s == P.BED_DURATION_S for b in reloaded)
    again = P.generate_music_beds(beds[0].path.parent)          # idempotent: existing files are kept
    assert {b.name for b in again} == set(by_name)


def test_beds_sit_at_minus_18_lufs(beds):
    for b in beds:
        assert P._measure_lufs(b.path) == pytest.approx(P.BED_TARGET_LUFS, abs=1.0), b.name


def test_beds_bpm_as_librosa_hears_it_matches_the_sidecar(beds, monkeypatch, tmp_path):
    from video_ai_editor.ingest import beats as B
    monkeypatch.setattr(B._pu, "user_cache_dir", lambda name: tmp_path / "cache")
    for b in beds:
        detected = B.detect_beats(b.path)
        assert len(detected) > 50, b.name
        # Mean period over the whole run, not the median: librosa reports
        # beats on a ~23 ms frame grid, so a 0.5 s period alternates 21/22
        # frames and the median lands on 117 BPM for a 120 BPM bed.
        period = (detected[-1] - detected[0]) / (len(detected) - 1)
        assert 60.0 / period == pytest.approx(b.bpm, abs=2.0), (b.name, 60.0 / period)
        assert abs(statistics.median(y - x for x, y in zip(detected, detected[1:])) - period) < 0.03
        grid = b.beats(20.0)
        assert grid[0] == 0.0 and grid[1] == pytest.approx(60.0 / b.bpm)
        near = sum(1 for d in detected if d < 20.0 and min(abs(d - g) for g in grid) <= 0.06)
        assert near >= 0.8 * sum(1 for d in detected if d < 20.0), b.name


def test_music_beds_degrade_to_empty_and_bed_for_mood_prefers_chill(tmp_path, beds):
    assert P.music_beds(tmp_path / "nowhere") == []
    assert P.bed_for_mood("upbeat", tmp_path / "nowhere") is None
    d = beds[0].path.parent
    assert P.bed_for_mood("cinematic", d).bpm == 70 and P.bed_for_mood(None, d).mood == "chill"
    assert P.bed_for_mood("polka", d).mood == "chill"
    (d / "broken.json").write_text("{not json")
    (d / "broken.wav").write_bytes(b"RIFF")
    assert "broken" not in {b.name for b in P.music_beds(d)}


# --- safe zones ------------------------------------------------------------------

def test_safe_zones_match_the_spec_and_the_verifier_reads_them():
    z = P.SAFE_ZONES["9:16"]
    assert (z.y_min, z.y_max, z.x_max) == (0.10, 0.78, 0.85) and z.caption_y == 0.76 and z.lower_third_y == 0.74
    assert (P.SAFE_ZONES["1:1"].y_min, P.SAFE_ZONES["1:1"].y_max) == (0.08, 0.90)
    assert (P.SAFE_ZONES["4:5"].y_min, P.SAFE_ZONES["4:5"].y_max) == (0.08, 0.90)
    assert (P.SAFE_ZONES["16:9"].y_min, P.SAFE_ZONES["16:9"].y_max) == (0.05, 0.92) and P.SAFE_ZONES["16:9"].caption_y == 0.85
    assert P.safe_zone_for("other") is P.SAFE_ZONES["16:9"]
    assert z.contains(0.5, 0.76) and not z.contains(0.5, 0.84) and not z.contains(0.9, 0.5)
    assert P.SAFE_ZONE_EXEMPT_ROLES == {"watermark"}


@pytest.mark.parametrize("w,h,aspect", [(1080, 1920, "9:16"), (1920, 1080, "16:9"), (1080, 1080, "1:1"),
                                        (1080, 1350, "4:5"), (1088, 1360, "4:5"), (1280, 720, "16:9"),
                                        (700, 1000, "other"), (0, 100, "other")])
def test_aspect_of(w, h, aspect):
    assert P.aspect_of(w, h) == aspect


# --- text styles -----------------------------------------------------------------

def test_text_styles_place_text_inside_the_safe_zone_with_advertised_args_only():
    styles = P.text_styles()
    assert set(styles) == {"bold_pop", "clean_lower", "hook_shout", "label_tag", "caption_karaoke", "caption_ig"}
    add_text_props = set(V.plan_schema_for("add_text")["properties"])
    fonts = P.font_names()
    for aspect in ("9:16", "16:9", "1:1"):
        z = P.safe_zone_for(aspect)
        w, h = P.ASPECT_DIMS[aspect]
        for name, st in styles.items():
            assert st.font in fonts, name
            y = st.y_frac(aspect)
            assert z.y_min <= y <= z.y_max, (name, aspect, y)
            args = st.add_text_args(text="Hi", start=0, end=2, canvas_w=w, canvas_h=h, aspect=aspect)
            assert set(args) <= add_text_props, (name, set(args) - add_text_props)
            assert args["y"] == pytest.approx(h * y, abs=0.1) and args["x"] == w / 2
    assert styles["caption_ig"].y_frac("9:16") == 0.76 and styles["caption_ig"].y_frac("16:9") == 0.85


def test_fonts_and_luts_are_the_bundled_files():
    fonts = P.font_names()
    assert {"Inter-Black.ttf", "Inter-Black", "Anton-Regular.ttf", "BebasNeue-Regular.ttf", "Montserrat-Bold.ttf"} <= fonts
    assert P.lut_names() == {"cool.cube", "faded.cube", "mono.cube", "punch.cube", "teal_orange.cube", "warm.cube"}
    assert P.show_template_names() >= {"quicksolutions_techtip"}


# --- transitions -----------------------------------------------------------------

def test_transition_looks_name_real_types_and_skip_the_catalog_file():
    looks = P.transition_looks()
    assert set(looks) == {"smooth", "cinematic", "punchy", "clean"}
    valid = set(T.all_names())
    for look in looks.values():
        assert set(look.types) <= valid and 0.1 <= look.duration <= 2.0
    assert looks["punchy"].type_at(4) == "zoomin" and looks["smooth"].types == ("crossdissolve",)


def test_transition_catalog_covers_every_renderable_look_exactly_once():
    cat = P.transition_catalog()
    looks = set(T.NATIVE) | set(T.CUSTOM_EXPRS)
    assert set(cat) == looks and len(cat) == 72
    labels = [e.label for e in cat.values()]
    assert len(set(labels)) == len(labels)                       # no two looks share a display name
    for e in cat.values():
        assert e.category in P.TRANSITION_CATEGORY_IDS and 0.1 <= e.duration <= 2.0 and e.description.endswith(".")
        assert e.as_dict()["name"] == e.name
    by_cat = P.transitions_by_category()
    assert list(by_cat) == list(P.TRANSITION_CATEGORY_IDS) and all(by_cat.values())
    assert sum(len(v) for v in by_cat.values()) == 72
    assert [lbl for _, lbl in P.TRANSITION_CATEGORIES] == ["Basic", "Light", "Wipe", "Slide", "Zoom", "Blur", "Shape", "Glitch & Stylised"]


def test_transition_catalog_is_one_source_with_the_renderer():
    """ONE catalog (the finding: two catalogs disagreed on 45 of 72 rows —
    labels, families and default durations — so a tile click and the same
    tile's 'Every cut' applied different looks/durations). Every entry
    equals the renderer's own record, and every look has a description."""
    cat = P.transition_catalog()
    by = {e["name"]: e for e in T.entries()}
    assert set(cat) == set(by)
    for name, e in cat.items():
        r = by[name]
        assert e.label == r["display"] == T.display_name(name)
        assert e.duration == r["default_duration"] == T.default_duration(name)
        assert e.category == P._FAMILY_TO_CATEGORY[r["family"]] and r["family"] == T.family_of(name)
        assert e.description == r["description"] == T.DESCRIPTIONS[name] and e.description.endswith(".")
    assert cat["fadewhite"].label == "Fade to White" and cat["fadewhite"].duration == 0.6
    assert cat["zoomin"].duration == 0.3 and cat["burn"].duration == 0.7 and cat["whip"].category == "stylised"


@pytest.mark.parametrize("entry", T.entries(), ids=lambda e: e["name"])
def test_every_display_name_resolves_to_its_own_look_through_the_grammar(entry):
    """What the Transitions panel shows must round-trip through the prompt:
    'Barn Doors Open' → vertopen, never crossdissolve (19 of 72 did)."""
    from video_ai_editor.agent.prompt import slots as S
    phrase = f"add a {entry['display'].lower()} transition between every clip"
    assert S.transition_type_of(phrase) == entry["name"], phrase
    assert S.transition_type_of(f"add a {entry['name']} transition between every clip") == entry["name"]
    for alias in entry["aliases"]:
        assert T.canonical(alias) == entry["name"]


def test_transition_entry_resolves_aliases_and_defaults_durations():
    assert P.transition_entry("crossdissolve").name == "dissolve"
    assert P.transition_entry("zoom").label == "Zoom In" and P.transition_entry("whippan").duration == 0.25
    assert P.transition_entry("nope") is None
    assert P.transition_default_duration("fadeblack") == 0.6 and P.transition_default_duration("nope") == 0.5
    assert P.transition_default_duration("nope", fallback=0.3) == 0.3
    for alias in T.ALIASES:
        assert P.transition_entry(alias) is not None, alias
    assert set(R.RECIPE_BY_NAME["transitions"].slot_values["type"]) == set(P.transition_catalog())


# --- templates -------------------------------------------------------------------

def test_edit_templates_name_real_recipes_and_slots():
    tpls = P.edit_templates()
    assert set(tpls) == {"talking_head_reel", "youtube_explainer", "tiktok_hype", "story_teaser", "lecture_cleanup"}
    for t in tpls.values():
        assert t.keywords and t.description
        for name, slots in t.recipes:
            assert name in R.RECIPE_SLOTS and set(slots) <= set(R.RECIPE_SLOTS[name]), (t.name, name)
    assert tpls["lecture_cleanup"].exclusions == ("hook", "music") and tpls["story_teaser"].platform == "story"


# --- malformed preset files fail loudly -----------------------------------------

@pytest.fixture
def scratch_presets(tmp_path, monkeypatch):
    root = tmp_path / "presets"
    for sub in ("text_styles", "transitions", "templates", "luts"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setattr(config, "PRESETS_DIR", root)
    return root


def test_malformed_presets_raise(scratch_presets):
    root = scratch_presets
    (root / "text_styles" / "bad.json").write_text(json.dumps({"role": "banner", "font": "Inter-Black.ttf", "anchor": "caption"}))
    with pytest.raises(ValueError, match="role"):
        P.text_styles()
    (root / "text_styles" / "bad.json").write_text(json.dumps({"role": "super", "font": "Comic.ttf", "anchor": "caption"}))
    with pytest.raises(ValueError, match="bundled font"):
        P.text_styles()
    (root / "text_styles" / "bad.json").write_text(json.dumps({"role": "super", "font": "Inter-Black.ttf", "anchor": "sky"}))
    with pytest.raises(ValueError, match="anchor"):
        P.text_styles()
    (root / "text_styles" / "bad.json").unlink()
    (root / "transitions" / "odd.json").write_text(json.dumps({"types": ["matrix"], "duration": 0.4}))
    with pytest.raises(ValueError, match="unknown transition types"):
        P.transition_looks()
    (root / "transitions" / "odd.json").write_text(json.dumps({"types": ["fade"], "duration": 5}))
    with pytest.raises(ValueError, match="duration"):
        P.transition_looks()
    (root / "transitions" / "odd.json").unlink()
    # The catalog is the renderer's table now, not a file: a stale
    # catalog.json from 0.7.0-pre is ignored by the look loader and never
    # read by the catalog.
    (root / "transitions" / "catalog.json").write_text(json.dumps({"transitions": {"fade": {"label": "F", "category": "basic", "duration": 0.5}}}))
    assert "catalog" not in P.transition_looks()
    assert len(P.transition_catalog()) == 72
    (root / "templates" / "t.json").write_text(json.dumps({"recipes": [{"recipe": "frobnicate"}]}))
    with pytest.raises(ValueError, match="unknown recipe"):
        P.edit_templates()
    (root / "templates" / "t.json").write_text(json.dumps({"recipes": [{"recipe": "captions", "slots": {"colour": "red"}}]}))
    with pytest.raises(ValueError, match="unknown slots"):
        P.edit_templates()
    (root / "templates" / "t.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        P.edit_templates()
    assert P.lut_names() == frozenset() and P.text_styles() == {}
