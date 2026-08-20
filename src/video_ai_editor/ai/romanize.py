"""Devanagari → Latin romanisation for Hinglish captions.

Why this is hand-written rather than a dependency
-------------------------------------------------
The captions pipeline offers three targets: `hi` (Devanagari), `en` (Whisper's
own translation) and `hinglish` — Hindi *spoken* content written in the Latin
script, the way people actually type it:

    अपनी last meeting के बाद   →   apni last meeting ke baad

Transliteration libraries target scholarly schemes (ITRANS, IAST, ISO-15919),
which encode vowel length with capitals or diacritics and keep every inherent
vowel: ITRANS renders that line `apanI last meeting ke bAda`. That is not what
a Hinglish caption looks like, so post-processing a scheme output would be more
code than doing it directly — and one deterministic implementation guarantees
the packaged app and a source checkout produce byte-identical captions, which a
"use the library if importable, else fall back" arrangement cannot.

What makes it read naturally
----------------------------
Two rules do almost all the work, and both are about the *inherent* vowel that
every Devanagari consonant carries unless a matra or virama says otherwise:

1. **Word-final schwa deletion.** बाद is `baad`, not `baada`. Applied only when
   the word has two or more consonants, so a genuine one-syllable word keeps
   its vowel (न → `na`).
2. **Medial schwa deletion.** अपनी is `apni`, not `apani`. A consonant's
   inherent vowel drops when it is *not* word-initial, the syllable before it
   carries a vowel, and the consonant after it has an explicit vowel of its
   own. The word-initial exclusion is what keeps गया as `gaya` rather than
   `gya`, and the "next consonant has its own vowel" condition is what keeps
   करता as `karta`.

Long vowels also shorten at the end of a word in ordinary Hinglish spelling —
अपनी is `apni` but ठीक is `theek`, गया is `gaya` but बाद is `baad` — so the
matra table has separate word-final and medial spellings.

Latin, digits and punctuation pass through untouched, which is what makes a
code-switched line work: only the Devanagari runs are converted, so English
words Whisper already wrote in Latin ("last meeting") survive as they are.

Known rough edges, deliberately accepted: an independent vowel directly after a
consonant (गई → `gaee`, usually typed `gayi`), and ू medially always becoming
`oo` (बकचूदी → `bakchoodi` for `bakchodi`). Both are spelling variants of a
correctly-identified word, not comprehension failures.
"""
from __future__ import annotations

# --- consonants -------------------------------------------------------------
# Each maps to its bare consonant sound; the inherent vowel is added by the
# walker, never baked in here.
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # Nukta forms — Urdu-origin sounds that are everywhere in spoken Hindi.
    # ड़/ढ़ are retroflex flaps, phonetically closer to `r` (बड़ा as `bara`),
    # but Hinglish overwhelmingly spells them with d — `bada`, `badi`,
    # `thoda` — so the common spelling wins over the closer phonetics.
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "dh",
    "फ़": "f", "य़": "y", "ऴ": "l",
}

# --- independent vowels -----------------------------------------------------
# (medial spelling, word-final spelling) — the same shortening the matras get,
# because an independent vowel can also close a word: हुआ is `hua`, not `huaa`,
# and गई is `gai`, not `gaee`.
_VOWELS = {
    "अ": ("a", "a"), "आ": ("aa", "a"), "इ": ("i", "i"), "ई": ("ee", "i"),
    "उ": ("u", "u"), "ऊ": ("oo", "u"), "ऋ": ("ri", "ri"),
    "ए": ("e", "e"), "ऐ": ("ai", "ai"), "ओ": ("o", "o"), "औ": ("au", "au"),
    "ऑ": ("o", "o"), "ऍ": ("e", "e"),
}

# --- matras: (medial spelling, word-final spelling) -------------------------
# Long vowels shorten in ordinary Hinglish spelling at the end of a word:
# ठीक -> theek but अपनी -> apni; बाद -> baad but गया -> gaya.
_MATRAS = {
    "ा": ("aa", "a"),
    "ि": ("i", "i"),
    "ी": ("ee", "i"),
    "ु": ("u", "u"),
    "ू": ("oo", "u"),
    "ृ": ("ri", "ri"),
    "े": ("e", "e"),
    "ै": ("ai", "ai"),
    "ो": ("o", "o"),
    "ौ": ("au", "au"),
    "ॉ": ("o", "o"),
    "ॅ": ("e", "e"),
}

_VIRAMA = "्"       # ् — suppresses the inherent vowel
_ANUSVARA = "ं"     # ं — nasal
_CHANDRABINDU = "ँ"  # ँ — nasal
_VISARGA = "ः"      # ः
_NUKTA = "़"        # ़ — combining, handled by the two-char lookahead
_AVAGRAHA = "ऽ"     # ऽ

_DIGITS = {"०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
           "५": "5", "६": "6", "७": "7", "८": "8", "९": "9"}

_PUNCT = {"।": ".", "॥": ".", "॰": "."}

# A nasal before a labial is written `m` (संभव -> sambhav), else `n`.
_LABIALS = {"p", "b", "m", "ph", "bh"}


def _is_devanagari(ch: str) -> bool:
    return "ऀ" <= ch <= "ॿ"


def _at_word_end(text: str, j: int) -> bool:
    """Is position `j` the end of a Devanagari word, ignoring nasal marks?

    A trailing anusvara or chandrabindu is part of the same syllable, not a new
    one, so a vowel before it is still word-final: नहीं is `nahin`, and reading
    ी as medial there produced `naheen`.
    """
    while j < len(text) and text[j] in (_ANUSVARA, _CHANDRABINDU, _VISARGA, _NUKTA):
        j += 1
    if j >= len(text):
        return True
    nxt = text[j]
    return not _is_devanagari(nxt) or nxt in _PUNCT


class _Word:
    """Accumulates one word so the schwa rules can see its shape."""

    __slots__ = ("parts", "consonants", "had_vowel", "pending")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.consonants = 0
        # True once the word has produced any vowel sound — the "syllable
        # before it carries a vowel" half of the medial-schwa rule.
        self.had_vowel = False
        # A consonant emitted but not yet followed by a vowel/virama decision.
        self.pending: str | None = None


def _flush(word: _Word, *, at_word_end: bool) -> None:
    """Resolve a consonant still holding its undecided inherent vowel."""
    if word.pending is None:
        return
    cons = word.pending
    word.pending = None
    word.parts.append(cons)
    # Word-final schwa: dropped once the word already carries a vowel, kept
    # otherwise so a lone syllable survives (न -> `na`, क -> `ka`). Counting
    # consonants instead looks the same and is not: अब has one consonant but a
    # preceding independent vowel, and came out `aba` rather than `ab`.
    if at_word_end:
        if not word.had_vowel:
            word.parts.append("a")
            word.had_vowel = True
        return
    word.parts.append("a")
    word.had_vowel = True


def _resolve_medial(word: _Word, next_has_own_vowel: bool) -> None:
    """Decide the inherent vowel of a pending consonant mid-word.

    Dropped only when all three hold: the consonant is not word-initial, a
    vowel has already been produced in this word, and the following consonant
    carries its own explicit vowel. That is what separates अपनी (`apni`) from
    गया (`gaya`) — in गया the pending consonant IS word-initial.
    """
    if word.pending is None:
        return
    cons = word.pending
    word.pending = None
    word.parts.append(cons)
    # `had_vowel` alone is the correct guard for "is there a syllable before
    # this one". An additional word-initial test on the consonant COUNT looks
    # equivalent and is not: a word opening with an independent vowel (अपनी)
    # has produced a vowel while its first consonant is still consonant #1, so
    # counting made अपनी come out `apani`. गया stays `gaya` because at that
    # point the word has emitted no vowel at all.
    if not word.had_vowel or not next_has_own_vowel:
        word.parts.append("a")
        word.had_vowel = True


def _next_consonant_has_own_vowel(text: str, i: int) -> bool:
    """Looking at text[i:], does the next consonant carry an explicit vowel?

    Explicit means a matra or a virama (a virama-joined cluster belongs to the
    following syllable), not the inherent vowel.
    """
    j = i
    while j < len(text):
        ch = text[j]
        if ch in _CONSONANTS or (j + 1 < len(text) and text[j + 1] == _NUKTA):
            # Found the consonant — inspect what follows it.
            k = j + 1
            if k < len(text) and text[k] == _NUKTA:
                k += 1
            if k < len(text) and (text[k] in _MATRAS or text[k] == _VIRAMA):
                return True
            return False
        if ch in _MATRAS or ch in _VOWELS:
            return True
        if not _is_devanagari(ch):
            return False
        j += 1
    return False


def romanize(text: str) -> str:
    """Romanise the Devanagari runs of `text`, leaving everything else alone.

    Latin words, digits and punctuation pass through, so a code-switched
    caption line converts cleanly:

        >>> romanize("अपनी last meeting के बाद")
        'apni last meeting ke baad'
    """
    if not text:
        return text

    out: list[str] = []
    word = _Word()
    i = 0
    n = len(text)

    def close_word() -> None:
        nonlocal word
        _flush(word, at_word_end=True)
        out.append("".join(word.parts))
        word = _Word()

    while i < n:
        ch = text[i]

        # Consonant, possibly with a nukta immediately after it.
        two = text[i:i + 2]
        cons = _CONSONANTS.get(two) if len(two) == 2 and two[1] == _NUKTA else None
        if cons is None:
            cons = _CONSONANTS.get(ch)
            step = 1
        else:
            step = 2

        if cons is not None:
            # The previous consonant's inherent vowel can only be decided once
            # we know whether THIS consonant has a vowel of its own.
            _resolve_medial(word, _next_consonant_has_own_vowel(text, i))
            word.consonants += 1
            word.pending = cons
            i += step
            continue

        if ch in _MATRAS:
            # An explicit vowel replaces the pending inherent one.
            if word.pending is not None:
                word.parts.append(word.pending)
                word.pending = None
            medial, final = _MATRAS[ch]
            word.parts.append(final if _at_word_end(text, i + 1) else medial)
            word.had_vowel = True
            i += 1
            continue

        if ch == _VIRAMA:
            # Cluster: the pending consonant gets no vowel at all.
            if word.pending is not None:
                word.parts.append(word.pending)
                word.pending = None
            i += 1
            continue

        if ch in (_ANUSVARA, _CHANDRABINDU):
            if word.pending is not None:
                word.parts.append(word.pending)
                word.parts.append("a")
                word.pending = None
                word.had_vowel = True
            nxt_cons = None
            j = i + 1
            if j < n:
                nxt_cons = _CONSONANTS.get(text[j:j + 2]) or _CONSONANTS.get(text[j])
            word.parts.append("m" if nxt_cons in _LABIALS else "n")
            i += 1
            continue

        if ch == _VISARGA:
            if word.pending is not None:
                word.parts.append(word.pending)
                word.parts.append("a")
                word.pending = None
            word.parts.append("h")
            i += 1
            continue

        if ch in _VOWELS:
            # An independent vowel starts a new syllable; the pending
            # consonant keeps its inherent vowel.
            if word.pending is not None:
                word.parts.append(word.pending)
                word.parts.append("a")
                word.pending = None
            medial, final = _VOWELS[ch]
            word.parts.append(final if _at_word_end(text, i + 1) else medial)
            word.had_vowel = True
            i += 1
            continue

        if ch in _DIGITS:
            close_word()
            out.append(_DIGITS[ch])
            i += 1
            continue

        if ch in _PUNCT:
            close_word()
            out.append(_PUNCT[ch])
            i += 1
            continue

        if ch in (_NUKTA, _AVAGRAHA) or ch == "‍" or ch == "‌":
            i += 1                      # stray combining mark — drop it
            continue

        # Anything else (Latin, space, punctuation, emoji) ends the current
        # Devanagari word and passes through verbatim.
        close_word()
        out.append(ch)
        i += 1

    close_word()
    return "".join(out)


def romanize_segments(segments: list[dict]) -> list[dict]:
    """Romanise `text` (and word-level `word`s) of whisper segments in place-ish.

    Returns new dicts; timing is untouched, so cue building and the caption
    track see exactly the same structure they would for a Hindi transcript.
    """
    out: list[dict] = []
    for seg in segments:
        new = dict(seg)
        new["text"] = romanize(seg.get("text") or "")
        words = seg.get("words")
        if words:
            new["words"] = [
                {**w, "word": romanize(w.get("word") or "")} for w in words
            ]
        out.append(new)
    return out
