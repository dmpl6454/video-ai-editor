"""Letter case in text overlays — the ALL-CAPS override, and the caps-only font.

Reported as "Text layer only shows capital alphabets and doesn't support the
small alphabets". That is TWO independent causes, and fixing either alone leaves
the report true:

  1. The caps rule was a hardcoded `role in ("super", "hook")` at the draw call
     in render/text_overlay.py, mirrored by an `upper: true` role flag in
     TextLayer.tsx, with NOTHING in the schema able to override it. So a
     lowercase hook or super was unreachable — in the preview and the export
     alike, which is why it read as the text layer simply not supporting them.
  2. The `hook` role's font is Bebas Neue, an all-caps display face whose
     lowercase slots contain CAPITAL letterforms. Turning the rule off changes
     nothing there, because there is no lowercase in the file to draw.

(1) is fixed by TextStyle.upper + resolve_upper_override. (2) cannot be fixed in
code at all, so the panel labels the font and warns when "As typed" is chosen in
it. These tests pin both, so the next person seeing all-caps Bebas output does
not go hunting through the caps logic for a bug that is not there.
"""
import string

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from video_ai_editor.edl.schema import TextClip, TextStyle
from video_ai_editor.render.text_overlay import (
    FONTS_DIR, ROLE_STYLES, render_text_png, resolve_upper_override,
)


# --------------------------------------------------------------- the override

def test_the_role_default_is_unchanged_when_style_is_untouched():
    """super/hook capitalise as a house style; nothing else does. An existing
    project must render exactly as it did before the field existed."""
    for role in ("super", "hook"):
        assert resolve_upper_override(TextClip(text="a", start=0, end=1, role=role), role)
    for role in ("caption", "label", "lower_third", "watermark", "default"):
        assert not resolve_upper_override(TextClip(text="a", start=0, end=1), role)


def test_an_explicit_choice_overrides_the_role_in_both_directions():
    lower_hook = TextClip(text="a", start=0, end=1, role="hook", style=TextStyle(upper=False))
    assert resolve_upper_override(lower_hook, "hook") is False
    upper_caption = TextClip(text="a", start=0, end=1, style=TextStyle(upper=True))
    assert resolve_upper_override(upper_caption, "caption") is True


def test_none_is_distinguishable_from_false():
    """The whole reason `upper` is nullable. A `False` default would have
    retroactively un-capitalised every hook and super in every saved project."""
    assert TextStyle().upper is None
    assert resolve_upper_override(TextClip(text="a", start=0, end=1, role="hook"), "hook") is True
    assert resolve_upper_override(
        TextClip(text="a", start=0, end=1, role="hook", style=TextStyle(upper=False)), "hook") is False


def test_the_rule_lives_in_the_role_table_not_a_hardcoded_list():
    """It has to be the same shape of declaration as the client's role table, or
    the two lists drift — which is how the preview and the export come to
    disagree about capitalisation."""
    assert ROLE_STYLES["super"].get("upper") is True
    assert ROLE_STYLES["hook"].get("upper") is True
    assert not ROLE_STYLES["caption"].get("upper", False)


def test_the_override_actually_changes_pixels_in_a_font_that_has_lowercase():
    """Anton (the `super` role) has real lowercase, so the flag is observable."""
    caps = np.array(render_text_png("Hello World", "super", 1080, 1920, upper=True))
    typed = np.array(render_text_png("Hello World", "super", 1080, 1920, upper=False))
    assert not np.array_equal(caps, typed)


def test_the_png_cache_key_tracks_the_resolved_caps_flag(tmp_path):
    """The key hashes the RAW text, so without the flag in it, toggling ALL CAPS
    off would keep serving the capitalised PNG forever — the dead-control bug the
    key's own comments describe for size and opacity.

    Asserted through the actual cache rather than by grepping the key expression:
    the flag reaches the hash via `geo_key`, so a source check would have to know
    which sub-key it rides in, and would pass while the pixels were wrong.
    """
    from video_ai_editor.edl.schema import EDL, Canvas, Track
    from video_ai_editor.render.text_overlay import cache_text_pngs

    def png_for(upper):
        style = TextStyle() if upper is None else TextStyle(upper=upper)
        edl = EDL(canvas=Canvas(w=1080, h=1920, fps=30), tracks=[
            Track(id="tx_super", type="text", clips=[
                TextClip(id="t1", text="Hello World", start=0, end=2,
                         role="super", style=style)])])
        return cache_text_pngs(edl, tmp_path)[0][2]

    caps, typed = png_for(True), png_for(False)
    assert caps != typed, "ALL CAPS and As-typed must not share one cached PNG"
    assert caps.exists() and typed.exists()
    assert np.array_equal(np.array(Image.open(caps)), np.array(Image.open(png_for(None)))), (
        "a super with `upper` unset resolves to True, so it must SHARE the caps "
        "PNG rather than render a second identical one")


# ------------------------------------------------------- the caps-only font

def _raster(font_file: str, s: str, size: int = 170) -> np.ndarray:
    f = ImageFont.truetype(str(FONTS_DIR / font_file), size)
    im = Image.new("L", (900, 400), 0)
    ImageDraw.Draw(im).text((10, 10), s, font=f, fill=255)
    return np.array(im)


def test_bebas_neue_has_no_lowercase_letterforms():
    """Cause (2), measured rather than assumed.

    Bebas Neue maps all 26 lowercase codepoints to differently-NAMED glyphs, so
    a cmap check says it supports lowercase — it does not. The glyphs are capital
    letterforms, and the advances are identical to the caps (a real 'a' has a
    different advance from 'A'). Rasterising is the only check that sees this.
    """
    for ch in "abg":
        assert np.array_equal(_raster("BebasNeue-Regular.ttf", ch),
                              _raster("BebasNeue-Regular.ttf", ch.upper())), ch
    assert np.array_equal(_raster("BebasNeue-Regular.ttf", "hello"),
                          _raster("BebasNeue-Regular.ttf", "HELLO"))


def test_the_other_bundled_faces_do_have_lowercase():
    """So the caps-only list stays exactly one font long, and "As typed" is
    genuinely useful everywhere else."""
    for f in ("Anton-Regular.ttf", "Inter-Bold.ttf", "Inter-Black.ttf",
              "Montserrat-Bold.ttf"):
        assert not np.array_equal(_raster(f, "hello"), _raster(f, "HELLO")), f


def test_turning_caps_off_is_a_no_op_in_the_hook_role():
    """The honest consequence of (2), pinned so it reads as expected behaviour
    rather than a regression: the `hook` role renders in Bebas Neue, so
    `upper=False` changes nothing. The panel says so; the renderer cannot."""
    assert ROLE_STYLES["hook"]["font"] == "BebasNeue-Regular.ttf"
    caps = np.array(render_text_png("Hello World", "hook", 1080, 1920, upper=True))
    typed = np.array(render_text_png("Hello World", "hook", 1080, 1920, upper=False))
    assert np.array_equal(caps, typed)


def test_lowercase_is_reachable_on_a_hook_by_changing_the_font():
    """The actual route out for a user who wants a lowercase hook: the caps
    override AND a font that has lowercase. Both halves, together."""
    typed = np.array(render_text_png(
        "Hello World", "hook", 1080, 1920, upper=False,
        font_file=FONTS_DIR / "Anton-Regular.ttf"))
    caps = np.array(render_text_png(
        "Hello World", "hook", 1080, 1920, upper=True,
        font_file=FONTS_DIR / "Anton-Regular.ttf"))
    assert not np.array_equal(typed, caps)


def test_every_ascii_letter_survives_as_typed():
    """A blunt guard that nothing else in the path uppercases behind our back."""
    img = render_text_png(string.ascii_lowercase[:8], "caption", 1080, 1920, upper=False)
    upper_img = render_text_png(string.ascii_lowercase[:8].upper(), "caption", 1080, 1920, upper=False)
    assert not np.array_equal(np.array(img), np.array(upper_img))


# ------------------------------------ what a NEWLY CREATED clip does by default

def _store(tmp_path):
    from video_ai_editor.edl.snapshot import EDLStore
    return EDLStore(tmp_path)


def test_new_text_respects_the_case_you_typed(tmp_path):
    """The second half of the report: "again, i cant write the text in the small
    letters".

    Making lowercase merely POSSIBLE (via style.upper) was not enough — the role
    default still overrode the typed text, so a user typing "hello" into a super
    got "HELLO" until they found a dropdown. A default you have to go and find is
    not support, so the creation handlers now record an explicit False.

    Asserted as `is False`, not falsy: `None` is also falsy and is exactly the
    value that means "use the role default", i.e. the bug.
    """
    from video_ai_editor.agent.dispatch import dispatch

    for tool, args in (
        ("add_super_text", {"text": "hello", "start": 0, "end": 2, "role": "super"}),
        ("add_super_text", {"text": "hello", "start": 4, "end": 6, "role": "hook"}),
        ("add_text", {"text": "hello", "start": 8, "end": 9}),
    ):
        s = _store(tmp_path / f"{tool}{args['start']}")
        res = dispatch(s, tool, args)
        _, c = s.edl.get_clip(res.get("clip_id") or res["id"])
        assert c.style.upper is False, f"{tool} {args.get('role')}: {c.style.upper!r}"
        assert resolve_upper_override(c, c.role or "default") is False


def test_an_explicit_upper_true_still_gives_the_house_look(tmp_path):
    from video_ai_editor.agent.dispatch import dispatch

    s = _store(tmp_path / "caps")
    res = dispatch(s, "add_super_text",
                   {"text": "hello", "start": 0, "end": 2, "role": "hook", "upper": True})
    _, c = s.edl.get_clip(res["clip_id"])
    assert c.style.upper is True
    assert resolve_upper_override(c, "hook") is True


def test_the_generated_hook_keeps_its_capitals(tmp_path):
    """add_hook_overlay and apply_hook_stack are HOUSE STYLE — the all-caps look
    is the point, and it must survive add_super_text's switch to respecting the
    typed case. They pass upper=True explicitly rather than leaning on the role."""
    from video_ai_editor.agent.dispatch import dispatch

    s = _store(tmp_path / "hookoverlay")
    dispatch(s, "add_hook_overlay", {"text": "wait for it", "duration": 3.0})
    hooks = [c for t in s.edl.tracks for c in t.clips
             if isinstance(c, TextClip) and c.role == "hook"]
    assert hooks and hooks[0].style.upper is True, [h.style.upper for h in hooks]


def test_existing_clips_are_untouched(tmp_path):
    """Forward-only. A clip already saved carries `upper=None` and must keep
    rendering with its capitals — a silent load-time change to somebody's
    finished project is worse than the leftover."""
    s = _store(tmp_path / "legacy")
    legacy = TextClip(id="old", text="hello", start=0, end=2, role="super")
    assert legacy.style.upper is None
    s.edl.get_track("tx_super").clips.append(legacy)
    s.commit("legacy", {}, "legacy")
    _, c = s.edl.get_clip("old")
    assert c.style.upper is None
    assert resolve_upper_override(c, "super") is True


def test_named_presets_keep_their_own_case(tmp_path):
    """A preset is house style, so it declares its case rather than inheriting a
    default that just moved. Only the two hook-role presets were affected —
    add_text's new default would otherwise have quietly changed their look."""
    from video_ai_editor.agent.dispatch import dispatch

    for name, fields, want in (
        ("big_question", {"text": "is that a Sauber"}, True),
        ("countdown_3_2_1", {}, True),
        # These roles never capitalised, so they must stay as typed.
        ("hashtag_chunky", {"hashtag": "fyp"}, False),
        ("end_card_handle", {"handle": "@me"}, False),
        ("watermark_handle", {"handle": "@me"}, False),
        ("callout_arrow", {"text": "look here"}, False),
    ):
        s = _store(tmp_path / name)
        res = dispatch(s, "apply_text_template",
                       {"name": name, "fields": fields, "start": 0, "end": 2})
        _, c = s.edl.get_clip(res["clip_id"] if "clip_id" in res else res["id"])
        assert c.style.upper is want, f"{name}: {c.style.upper!r} != {want}"
