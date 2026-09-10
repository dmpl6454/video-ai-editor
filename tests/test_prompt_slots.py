"""Slot extraction — a table (spec §2.2, ≥ 140 cases).

Every row is `(prompt, slot, expected)`. The rows that matter most are the
ORDERING traps the spec calls out: `1080x1920` is a ratio, never a 1080×
speed-up; `4x upscale` is an upscale factor, never 4× speed; `make 2
shorts` is a count of clips, never the YouTube Shorts platform; `youtube
shorts` is the platform, never a count. Numbers spelled out, Hinglish
verbs, mm:ss ranges and the seam vocabulary for transitions are here too.
"""
from __future__ import annotations

import importlib

import pytest

from video_ai_editor.agent.prompt import slots as S

T = S.TimeRange

CASES: list[tuple[str, str, object]] = [
    # --- duration -------------------------------------------------------
    ("make it under 30 seconds", "duration_s", 30.0),
    ("make it under 30 seconds", "duration_qualifier", "max"),
    ("keep it to 45s", "duration_s", 45.0),
    ("2 minutes long", "duration_s", 120.0),
    ("about 1.5 min", "duration_s", 90.0),
    ("about 1.5 min", "duration_qualifier", "target"),
    ("around 15 seconds", "duration_qualifier", "target"),
    ("exactly 10 seconds", "duration_qualifier", "exact"),
    ("at most 20 sec", "duration_s", 20.0),
    ("at most 20 sec", "duration_qualifier", "max"),
    ("no more than 40 seconds", "duration_qualifier", "max"),
    ("half a minute", "duration_s", 30.0),
    ("a minute long", "duration_s", 60.0),
    ("shorts of thirty seconds", "duration_s", 30.0),
    ("cut it to 0:45", "duration_s", 45.0),
    ("add captions", "duration_s", None),
    ("a hook for 3 seconds", "duration_s", 3.0),
    ("a title at 5 seconds for 3 seconds", "duration_s", 3.0),   # "at N s" is a position
    # --- count ----------------------------------------------------------
    ("make 3 shorts", "count", 3),
    ("make three shorts", "count", 3),
    ("make 3 shorts", "platform", None),
    ("make 2 shorts", "ratio", None),
    ("make 2 shorts for youtube", "count", 2),
    ("make 2 shorts for youtube", "platform", "shorts"),
    ("give me 5 clips", "count", 5),
    ("a few highlights", "count", 3),
    ("a couple of reels", "count", 2),
    ("cut it into 4 parts", "count", 4),
    ("youtube shorts", "count", None),
    ("youtube shorts", "platform", "shorts"),
    ("make a youtube short", "count", None),
    ("ten highlights", "count", 10),
    # --- platform -------------------------------------------------------
    ("for reels", "platform", "reels"),
    ("post it on instagram", "platform", "reels"),
    ("for ig", "platform", "reels"),
    ("for tiktok", "platform", "tiktok"),
    ("for tik tok", "platform", "tiktok"),
    ("for youtube", "platform", "youtube_16x9"),
    ("for yt", "platform", "youtube_16x9"),
    ("in 4k for youtube", "platform", "youtube_4k"),
    ("instagram story", "platform", "story"),
    ("for stories", "platform", "story"),
    ("square for the feed", "platform", "ig_feed_1x1"),
    ("for linkedin", "platform", "ig_feed_4x5"),
    ("4:5 portrait feed", "platform", "ig_feed_4x5"),
    ("add captions", "platform", None),
    # --- ratio ----------------------------------------------------------
    ("9:16", "ratio", "9:16"),
    ("1080x1920", "ratio", "9:16"),
    ("1080x1920", "speed", None),
    ("make it vertical", "ratio", "9:16"),
    ("portrait mode", "ratio", "9:16"),
    ("make it landscape", "ratio", "16:9"),
    ("16:9", "ratio", "16:9"),
    ("1920x1080", "ratio", "16:9"),
    ("1920x1080", "speed", None),
    ("widescreen", "ratio", "16:9"),
    ("make it square", "ratio", "1:1"),
    ("1:1", "ratio", "1:1"),
    ("4:5", "ratio", "4:5"),
    ("portrait feed", "ratio", "4:5"),
    ("make it vertical for reels", "ratio", "9:16"),
    ("crop it for tiktok", "ratio", "9:16"),
    ("resize for youtube", "ratio", "16:9"),
    ("add captions", "ratio", None),
    # --- upscale --------------------------------------------------------
    ("4x upscale", "upscale_factor", 4),
    ("4x upscale", "speed", None),
    ("upscale 2x", "upscale_factor", 2),
    ("upscale to 4x", "upscale_factor", 4),
    ("2x resolution", "upscale_factor", 2),
    ("2x resolution", "speed", None),
    ("speed it up 2x", "upscale_factor", None),
    # --- speed ----------------------------------------------------------
    ("speed it up 1.5x", "speed", 1.5),
    ("1.5x", "speed", 1.5),
    ("2x faster", "speed", 2.0),
    ("double speed", "speed", 2.0),
    ("twice as fast", "speed", 2.0),
    ("half speed", "speed", 0.5),
    ("slow motion", "speed", 0.5),
    ("slow-mo", "speed", 0.5),
    ("quarter speed", "speed", 0.25),
    ("speed it up", "speed", 1.25),
    ("make it faster", "speed", 1.25),
    ("slow it down", "speed", 0.8),
    ("slower", "speed", 0.8),
    ("150% speed", "speed", 1.5),
    ("make 2 shorts", "speed", None),
    ("0.5x", "speed", 0.5),
    # --- language -------------------------------------------------------
    ("hindi captions", "language", "hi"),
    ("captions in hindi", "language", "hi"),
    ("in hinglish", "language", "hinglish"),
    ("roman hindi captions", "language", "hinglish"),
    ("romanised hindi", "language", "hinglish"),
    ("spanish subtitles", "language", "es"),
    ("translate to es", "language", "es"),
    ("in english", "language", "en"),
    ("captions in hi", "language", "hi"),
    ("कैप्शन जोड़ो", "language", "hi"),
    ("add captions", "language", None),
    # --- captions -------------------------------------------------------
    ("chunky captions", "caption_style", "ig_chunky"),
    ("bold captions", "caption_style", "ig_chunky"),
    ("instagram-style captions", "caption_style", "ig_chunky"),
    ("karaoke captions", "caption_style", "word_emphasis"),
    ("word by word captions", "caption_style", "word_emphasis"),
    ("highlighted words", "caption_style", "word_emphasis"),
    ("plain captions", "caption_style", "default"),
    ("simple subtitles", "caption_style", "default"),
    ("add captions", "caption_style", None),
    ("captions at the top", "caption_position", "top"),
    ("captions in the center", "caption_position", "center"),
    ("subtitles at the bottom", "caption_position", "bottom"),
    ("add captions", "caption_position", None),
    ("at the top", "caption_position", None),           # no caption/text noun
    ("accurate captions", "model_upgrade", True),
    ("better subtitles", "model_upgrade", True),
    ("make the captions more accurate", "model_upgrade", True),
    ("add captions", "model_upgrade", False),
    # --- mood -----------------------------------------------------------
    ("chill music", "mood", "chill"),
    ("calm song", "mood", "chill"),
    ("upbeat track", "mood", "upbeat"),
    ("energetic bgm", "mood", "upbeat"),
    ("cinematic bed", "mood", "cinematic"),
    ("epic soundtrack", "mood", "cinematic"),
    ("lo-fi background music", "mood", "lofi"),
    ("chill vibes", "mood", None),                      # no music noun
    # --- look -----------------------------------------------------------
    ("give it a cinematic look", "look", "teal_orange.cube"),
    ("teal and orange grade", "look", "teal_orange.cube"),
    ("warm grade", "look", "warm.cube"),
    ("a golden vibe", "look", "warm.cube"),
    ("cool tone", "look", "cool.cube"),
    ("make it punchy", "look", "punch.cube"),
    ("vivid colours", "look", "punch.cube"),
    ("vintage filter", "look", "faded.cube"),
    ("faded film look", "look", "faded.cube"),
    ("make it black and white", "look", "mono.cube"),
    ("b&w look", "look", "mono.cube"),
    ("add captions", "look", None),
    # --- transitions ----------------------------------------------------
    ("smooth transitions", "transition_look", "smooth"),
    ("punchy transitions", "transition_look", "punchy"),
    ("cinematic transitions", "transition_look", "cinematic"),
    ("clean cuts between the clips", "transition_look", "clean"),
    ("fade to black between clips", "transition_look", "cinematic"),
    ("fade to black between clips", "transition_type", "fadeblack"),
    ("smooth zoom between every clip", "transition_type", "zoomin"),
    ("smooth zoom between every clip", "transition_at", "all"),
    ("add a glitch transition at the hook", "transition_type", "glitch"),
    ("add a glitch transition at the hook", "transition_at", "first"),
    ("whip pan between the cuts", "transition_type", "whip"),
    ("zoom transition at 12 seconds", "transition_at", 12.0),
    ("add a wipe left at 0:12", "transition_type", "wipeleft"),
    ("add a wipe left at 0:12", "transition_at", 12.0),
    ("dissolve at the end", "transition_at", "last"),
    ("iris at the first cut", "transition_type", "circleopen"),
    ("spin transitions", "transition_type", "spiral"),
    ("pixelate between every clip", "transition_type", "pixelize"),
    ("Fade to Black transition after the first clip", "transition_type", "fadeblack"),
    ("Fade to Black transition after the first clip", "transition_at", "first"),
    ("add smooth transitions", "transition_type", None),
    ("add smooth transitions", "transition_at", None),
    ("zoom in on the product", "transition_type", None),
    ("add captions", "transition_look", None),
    # --- identifiers ----------------------------------------------------
    ("use #ff0000", "color_hex", "#FF0000"),
    ("brand colour #abc", "color_hex", "#ABC"),
    ("watermark @priya.codes", "handle", "@priya.codes"),
    ("apply my brand kit @quicksolutions.in", "handle", "@quicksolutions.in"),
    ("with #techtips and #coding", "hashtags", ("#techtips", "#coding")),
    ("add captions", "hashtags", ()),
    ("a lower third for Priya Sharma @priya.codes", "name", "Priya Sharma"),
    ("introduce Priya Sharma", "name", "Priya Sharma"),
    ("a title called 'Big Launch'", "name", "Big Launch"),
    ("a title called 'Big Launch'", "quoted_text", ("Big Launch",)),
    ("a lower third for tiktok", "name", None),
    ("add a lower third for Priya Sharma @priya.codes at the start", "name", "Priya Sharma"),
    ("add a lower third for Priya Sharma @priya.codes at the start", "handle", "@priya.codes"),
    ("add a lower third for Priya Sharma @priya.codes at the start", "at_start", True),
    ("add a lower third of Ravi Kumar", "name", "Ravi Kumar"),
    ("add a lower third named Ravi Kumar", "name", "Ravi Kumar"),
    ('saying "Thanks for watching"', "quoted_text", ("Thanks for watching",)),
    # --- clip refs ------------------------------------------------------
    ("speed up this clip", "clip_ref", "$selected"),
    ("the selected clip", "clip_ref", "$selected"),
    ("the first clip", "clip_ref", "$v1_first"),
    ("the last clip", "clip_ref", "$v1_last"),
    ("at the playhead", "clip_ref", "$playhead"),
    ("all clips", "clip_ref", "$v1_all"),
    ("the whole video", "clip_ref", "$v1_all"),
    ("stabilize c_abc123", "clip_ref", "c_abc123"),
    ("add captions", "clip_ref", None),
    # --- ranges ---------------------------------------------------------
    ("cut the first 5 seconds", "range", T("first", end=5.0)),
    ("cut the first five seconds", "range", T("first", end=5.0)),
    ("remove the last 10 seconds", "range", T("last", end=10.0)),
    ("cut from 0:05 to 0:12", "range", T("abs", start=5.0, end=12.0)),
    ("cut from 3 to 8 seconds", "range", T("abs", start=3.0, end=8.0)),
    ("between 10s and 20s", "range", T("abs", start=10.0, end=20.0)),
    ("from 1:00 to 1:30", "range", T("abs", start=60.0, end=90.0)),
    ("cut the first 2 minutes", "range", T("first", end=120.0)),
    ("add captions", "range", None),
    # --- fillers --------------------------------------------------------
    ("cut out the ums", "filler_words", ("um",)),
    ("remove the ums and uhs", "filler_words", ("um", "uh")),
    ("remove the umms and hmm", "filler_words", ("umm", "hmm")),
    ("strip the ums, uhs and likes", "filler_words", ("um", "uh")),
    ("remove fillers", "filler_words", ()),
    ("remove 'like' and 'you know'", "filler_words", ("you know", "like")),
    ("cut the hesitations", "filler_words", ()),
    ("add captions", "filler_words", ()),
    # --- lufs -----------------------------------------------------------
    ("normalize to -14 LUFS", "lufs", -14.0),
    ("normalise to 16 lufs", "lufs", -16.0),
    ("-23 LUFS", "lufs", -23.0),
    ("add captions", "lufs", None),
    # --- voice ----------------------------------------------------------
    ("female voice", "voice", "en_US-amy-medium"),
    ("male voice", "voice", "en_US-ryan-medium"),
    ("a british narrator", "voice", "en_GB-alan-medium"),
    ("indian female voice", "voice", "hi_IN-priyamvada-medium"),
    ("hindi male narration", "voice", "hi_IN-pratham-medium"),
    ("narrate in a deep voice", "voice", "en_US-ryan-medium"),
    ("a voice saying hi", "voice", None),
    ("female", "voice", None),                          # no voice noun
    # --- flags ----------------------------------------------------------
    ("at the start", "at_start", True),
    ("at the end", "at_end", True),
    ("smooth slow motion", "smooth", True),
    ("another track", "replace_existing", True),
    ("replace the music", "replace_existing", True),
    ("add captions", "at_start", False),
    ("add captions", "smooth", False),
]


@pytest.mark.parametrize("prompt,slot,expected", CASES, ids=[f"{s}:{p}" for p, s, _ in CASES])
def test_slot_table(prompt, slot, expected):
    got = getattr(S.extract(prompt), slot)
    if isinstance(expected, tuple) and slot in ("filler_words", "hashtags", "quoted_text"):
        assert set(got) == set(expected), (got, expected)
    else:
        assert got == expected, (got, expected)


def test_table_is_big_enough():
    assert len(CASES) >= 140


# --- normalisation ------------------------------------------------------------

@pytest.mark.parametrize("raw,norm", [
    ("captions laga do", "captions add"),
    ("ums hata do", "ums remove"),
    ("chhota kar do", "shorten"),
    ("tez karo", "speed up"),
    ("bina music", "without music"),
    ("Captions & music", "captions and music"),
    ("video w/ captions", "video with captions"),
    ("Hello   World!!", "hello world"),
    ("Make it pop...", "make it pop"),
    ("dheere karo aur captions lagao", "slow down and captions add"),
])
def test_normalize(raw, norm):
    assert S.normalize(raw) == norm


def test_hinglish_table_is_small_and_longest_first():
    assert 30 <= len(S.HINGLISH_VERBS) <= 60
    assert S.normalize("chhota kar do") == "shorten"          # not "chhota do"


# --- helpers used by the clarification parser --------------------------------

@pytest.mark.parametrize("text,expected", [
    ("half a minute", (30.0, None)), ("90s", (90.0, None)), ("2:30", (150.0, None)),
    ("under 20 seconds", (20.0, "max")), ("about a minute", (60.0, None)), ("nothing", None),
])
def test_parse_duration(text, expected):
    assert S.parse_duration(text) == expected


@pytest.mark.parametrize("text,expected", [("three", 3.0), ("2.5", 2.5), ("12", 12.0), ("abc", None), ("", None)])
def test_parse_number(text, expected):
    assert S.parse_number(text) == expected


def test_time_range_resolve():
    assert T("first", end=5.0).resolve(30.0) == (0.0, 5.0)
    assert T("last", end=10.0).resolve(30.0) == (20.0, 30.0)
    assert T("abs", start=5.0, end=40.0).resolve(30.0) == (5.0, 30.0)
    assert T("first", end=50.0).resolve(30.0) == (0.0, 30.0)


def test_default_lufs_is_the_export_preset_table():
    presets = importlib.import_module("video_ai_editor.agent.dispatch")._EXPORT_PRESETS
    assert S.PLATFORM_LUFS == {k: float(v["lufs"]) for k, v in presets.items()}
    assert S.default_lufs("shorts", None) == -14.0
    assert S.default_lufs("reels", -20.0) == -16.0
    assert S.default_lufs(None, -20.0) == -20.0
    assert S.default_lufs(None, None) == -16.0
    assert set(S.PLATFORM_RATIO) == set(presets)


def test_merge_prefers_override_and_concatenates_tuples():
    base = S.extract("for reels with #aa")
    over = S.extract("make it vertical with #bb")
    merged = S.merge(base, over)
    assert merged.platform == "reels" and merged.ratio == "9:16" and merged.hashtags == ("#aa", "#bb")
    assert S.merge(base, S.Slots()) is base


def test_as_dict_drops_empty_values():
    d = S.extract("for reels").as_dict()
    assert d["platform"] == "reels" and "speed" not in d and "hashtags" not in d


def test_transition_type_of_understands_labels_aliases_and_synonyms():
    assert S.transition_type_of("Fade to Black") == "fadeblack"
    assert S.transition_type_of("crossdissolve") == "dissolve"
    assert S.transition_type_of("whip pan") == "whip"
    assert S.transition_type_of("Push Left") == "slideleft"
    assert S.transition_type_of("smooth") is None                 # a look, never a type
    assert S.transition_type_of("nothing here") is None
    assert S.transition_at_of("at 1:05") == 65.0 and S.transition_at_of("between every clip") == "all"
