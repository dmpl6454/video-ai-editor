"""Hinglish romanisation — the two schwa rules are the whole feature.

Every expected spelling here is how the word is ordinarily typed in Hinglish,
not a scholarly transliteration: `apni`, not ITRANS's `apanI`.
"""
import pytest

from video_ai_editor.ai.romanize import romanize, romanize_segments


@pytest.mark.parametrize("deva,latin", [
    # --- word-final schwa deletion -----------------------------------------
    ("बाद", "baad"),
    ("यार", "yaar"),
    ("दिल", "dil"),
    ("घर", "ghar"),
    # A one-syllable word keeps its inherent vowel — nothing to delete.
    ("न", "na"),
    ("क", "ka"),
    # --- medial schwa deletion ---------------------------------------------
    ("अपनी", "apni"),
    ("करता", "karta"),
    ("समझ", "samajh"),
    # Word-initial consonants are exempt, which is what keeps these intact.
    ("गया", "gaya"),
    ("जगा", "jaga"),
    # --- matras, medial vs word-final --------------------------------------
    ("ठीक", "theek"),        # medial ी -> ee
    ("मुझे", "mujhe"),
    ("मेरे", "mere"),
    ("होता", "hota"),
    ("तो", "to"),
    ("के", "ke"),
    ("क्या", "kya"),         # virama cluster + final ा
    # --- clusters and nasals -----------------------------------------------
    ("स्किन", "skin"),
    ("दिल्ली", "dilli"),
    ("संसे", "sanse"),
    ("संभव", "sambhav"),     # nasal before a labial is written m
    ("हिंदी", "hindi"),
    # --- nukta consonants ---------------------------------------------------
    ("ज़्यादा", "zyaada"),
    ("बड़ी", "badi"),
    # --- a trailing nasal keeps the vowel word-FINAL ------------------------
    # Reading ी as medial here produced `naheen`; all four of these appear in
    # real captions from the test footage.
    ("नहीं", "nahin"),
    ("मैं", "main"),
    ("यहाँ", "yahan"),
    # हूँ is spelled both `hoon` and `hun` in the wild; the word-final rule
    # gives `hun`, and consistency with nahin/yahan is worth more than picking
    # the marginally more common spelling for this one word.
    ("हूँ", "hun"),
    # --- an independent vowel closing a word shortens too -------------------
    ("हुआ", "hua"),
    ("गई", "gai"),
    # A word that is a vowel plus one consonant still drops its final schwa —
    # counting consonants (rather than asking whether a vowel came before)
    # made this `aba`.
    ("अब", "ab"),
])
def test_common_words(deva, latin):
    assert romanize(deva) == latin


def test_code_switched_line_keeps_its_latin_words():
    """The real shape of a Hinglish caption: Whisper writes English words in
    Latin already, and only the Devanagari runs need converting."""
    assert romanize("अपनी last meeting के बाद") == "apni last meeting ke baad"


def test_a_fully_devanagari_line_including_transliterated_english():
    """Whisper sometimes writes the English words in Devanagari instead
    (measured: लास्ट मीटिंग). Romanising recovers readable Hinglish either way."""
    assert romanize("अपनी लास्ट मीटिंग के बाद") == "apni laast meeting ke baad"


def test_latin_digits_and_punctuation_pass_through():
    assert romanize("STD हो गया था") == "STD ho gaya tha"
    assert romanize("मैं 2 बार") == "main 2 baar"
    assert romanize("ठीक है।") == "theek hai."


def test_non_devanagari_input_is_returned_unchanged():
    for s in ("", "hello world", "已经 中文", "🔥 SALE", "123"):
        assert romanize(s) == s


def test_danda_becomes_a_full_stop():
    assert romanize("चलो।") == "chalo."


def test_segments_keep_their_timing_and_convert_word_level_text():
    segs = [{
        "start": 0.0, "end": 2.0, "text": "अपनी last meeting",
        "words": [
            {"word": "अपनी", "start": 0.0, "end": 0.5},
            {"word": "last", "start": 0.5, "end": 1.0},
            {"word": "meeting", "start": 1.0, "end": 2.0},
        ],
    }]
    out = romanize_segments(segs)
    assert out[0]["text"] == "apni last meeting"
    assert [w["word"] for w in out[0]["words"]] == ["apni", "last", "meeting"]
    # Timing must survive untouched — cue building depends on it.
    assert out[0]["start"] == 0.0 and out[0]["end"] == 2.0
    assert out[0]["words"][0]["end"] == 0.5
    # The input is not mutated.
    assert segs[0]["text"] == "अपनी last meeting"


def test_a_segment_without_word_timing_is_handled():
    out = romanize_segments([{"start": 0.0, "end": 1.0, "text": "बाद"}])
    assert out[0]["text"] == "baad"
    assert "words" not in out[0]


def test_output_carries_no_devanagari_left_over():
    """A caption that still contains Devanagari would defeat the point of the
    Hinglish target, so assert the whole codepoint block is gone."""
    line = "अपनी last meeting के बाद तो मुझे STD हो गया था यार"
    assert not any("ऀ" <= c <= "ॿ" for c in romanize(line))
