"""The sticker picker's catalogue — every swatch must be clickable.

`frontend/src/lib/emojiCatalog.ts` is generated (scripts/gen_emoji_catalog.py)
from Unicode's RGI set intersected with the artwork sources' real inventories.
That generator guarantees artwork EXISTS; these tests guard the other half —
that the frontend can actually ask for it, and that the picker still contains
what it is supposed to.

A failure here is a swatch you can see and click that renders nothing. That is
worse than an omission: an absent emoji reads as "not supported", a broken one
reads as "the app is broken".
"""
from __future__ import annotations
import json
import re
from pathlib import Path

import pytest

from video_ai_editor.main import _EMOJI_SEQ_RE

CATALOG = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib" / "emojiCatalog.ts"

pytestmark = pytest.mark.skipif(not CATALOG.exists(),
                                reason="generated emoji catalogue not present")


def _entries() -> list[dict]:
    txt = CATALOG.read_text(encoding="utf-8")
    return [json.loads(m) for m in re.findall(r"^    (\{.*\}),$", txt, re.M)]


def _all_chars(entries: list[dict]) -> list[str]:
    out: list[str] = []
    for e in entries:
        out.append(e["c"])
        out.extend(e.get("t", []))
    return out


def _seq(ch: str) -> str:
    """What the frontend's codepointSeq() builds for the artwork URL."""
    return "-".join(f"{ord(c):x}" for c in ch)


def test_catalogue_is_not_the_old_curated_list():
    """The picker was 113 hand-picked emoji and the reported bug was simply
    that a wanted one (😁) was not among them. A regenerated catalogue that
    collapsed back to a few hundred entries would silently restore that."""
    entries = _entries()
    assert len(entries) > 1500, f"only {len(entries)} base emoji — catalogue looks truncated"
    chars = {e["c"] for e in entries}
    # The one from the report, plus a spread that would catch a parser that
    # silently dropped a whole category.
    for ch, why in [("\U0001F601", "beaming face — the emoji the report was about"),
                    ("\U0001F600", "grinning face"),
                    ("\U0001F1EE\U0001F1F3", "flag (comes from the Fluent fallback)"),
                    ("❤️", "heart (VS16 spelling)"),
                    ("\U0001F9D1", "person (has skin tones)")]:
        assert ch in chars, f"missing {why}"


def test_every_swatch_survives_the_artwork_route_regex():
    """`GET /api/emoji/{seq}.png` validates `seq` before turning it back into
    characters. Anything the picker can display but that route rejects is a
    swatch that 400s on sight.

    This is not hypothetical: the bound was 8 codepoints, and an RGI kiss
    sequence carrying a skin tone on BOTH people is 10
    (`1f469-1f3fb-200d-2764-fe0f-200d-1f48b-200d-1f468-1f3ff`), so 15 real
    entries were unloadable while `fetch_emoji_png` resolved them fine.
    """
    bad = [ch for ch in _all_chars(_entries()) if not _EMOJI_SEQ_RE.match(_seq(ch))]
    assert not bad, (
        f"{len(bad)} catalogue entries are rejected by _EMOJI_SEQ_RE, "
        f"e.g. {_seq(bad[0])!r} ({len(bad[0])} codepoints)")


def test_skin_tone_variants_are_complete_when_present():
    """The tone selector indexes `t` positionally, light→dark. A short array
    would be an out-of-bounds read that silently inserts the WRONG emoji, so
    the generator only attaches `t` when all five exist."""
    for e in _entries():
        if "t" in e:
            assert len(e["t"]) == 5, f"{e['c']} has {len(e['t'])} tone variants"
            # Each variant must actually be that base plus a tone, or the
            # selector would swap the emoji for an unrelated one.
            tones = {0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF}
            for v in e["t"]:
                assert "".join(c for c in v if ord(c) not in tones) == e["c"], \
                    f"{v!r} is not a tone variant of {e['c']!r}"


def test_no_duplicate_swatches():
    """React keys the grid by the emoji itself, so a duplicate is both a
    rendering warning and a wasted row in a list people scan."""
    entries = _entries()
    chars = [e["c"] for e in entries]
    dupes = {c for c in chars if chars.count(c) > 1}
    assert not dupes, f"duplicate base emoji: {sorted(dupes)[:10]}"


def test_every_entry_has_search_text():
    """Search is the primary way into a 1900-swatch grid; an entry with no
    name and no keywords is reachable only by scrolling to it by eye."""
    for e in _entries():
        assert e.get("n"), f"{e['c']} has no name"
        assert e.get("k") is not None, f"{e['c']} has no keywords field"


def test_search_finds_the_reported_emoji():
    """The exact query the report implies. 😁 is 'beaming face with smiling
    eyes' — nobody types that, so the subgroup keywords ('face smiling') have
    to be searched too or the emoji stays as unfindable as it was."""
    entries = _entries()
    def search(q: str) -> list[str]:
        terms = q.lower().split()
        return [e["c"] for e in entries
                if all(t in f"{e['n']} {e['k']}".lower() for t in terms)]

    assert "\U0001F601" in search("beaming")
    assert "\U0001F601" in search("smiling eyes")
    assert "\U0001F600" in search("grinning")
    # A term that only the subgroup supplies.
    assert "\U0001F34E" in search("fruit"), "subgroup keywords are not being searched"
