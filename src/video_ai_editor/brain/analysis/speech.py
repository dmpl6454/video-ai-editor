"""The `speech` layer from the upload's own transcript (no new ASR pass this
wave): words with `prob` passed through, sentences by `ai/shorts._sentences`'
rule (terminal punctuation; pauses only when the text is unpunctuated) applied
over the whole word stream and split further at speaker changes, the §3.2
`features` per sentence, turns, false starts, repeats, weak questions, dead
air, acoustic fillers (`fillers.py`) and the words to check.

Times are the transcript's clock — reference seconds, since the transcript of
record is the reference source's — repaired toward the sound by
`word_timing.py`. Ids, stable for a given transcript: sentences `s_0001…`,
words `w_<sentence>_<index>` (`w_0007_03`), turns `u_0001…`, and `f_`, `r_`,
`q_`, `d_`, `af_` 1-based.

Shape: lane C's `schema.SpeechLayer` (`extra="forbid"`): words `{id, t0, t1,
text, prob, spk, sent, filler, check}`, sentences `{id, t0, t1, spk, text,
kind, is_question, answer_of, complete, weak_start, topic, features}`, turns,
acoustic_fillers, words_to_check, flags. `features` is exactly C's
`Features` (wpm, fillers, has_number, strong/weak_number, claim,
conclusion_marker, story_marker, contrast_words, anaphora_start, len_words);
the §3.4 delivery and importance inputs (`rms_z`, `pitch_range_st`, `stretch`,
…) are computed on demand by `delivery.py`. Speaker roles are computed here
(`speaker_roles`) but written to the GRAPH HEADER by graph.py.

A sentence's span is that of its spoken words: a leading or trailing filler
("Um, it is not an app") belongs to the sentence (`word.sent`) but not to its
`t0..t1`, so a window opened on the sentence starts at the word, and the
filler is the planner's own cut. `acoustic_fillers` lists only the islands the
transcript did NOT hear: an island a `filler` word covers is that word's cut.

Dead air, made concrete: with two or more speakers a gap ≥ DEAD_AIR_S between
two consecutive sentences of the SAME speaker (nobody else in between) or
inside a sentence is dead air; with one speaker the turns are paragraphs
split at such gaps, so a between-sentence pause is the audio layer's silence
and only an in-sentence gap is dead air.
"""
from __future__ import annotations

import re

import numpy as np

from . import ANALYSIS_VERSION
from .fillers import HESITATION_MAX_S, HESITATION_SPELLINGS, LEXICAL_FILLERS, NON_LEXICAL, acoustic_fillers, norm_token
from .word_timing import repair_word_times

DEAD_AIR_S = 1.2
FALSE_START_GAP = (0.25, 0.8)
FALSE_START_MAX_WORDS = 4
FALSE_START_WINDOW_S = 2.0
REPEAT_JACCARD = 0.8
REPEAT_WINDOW_S = 90.0
REPEAT_MIN_TOKENS = 6
REPEAT_LEN_RATIO = 1.3
ANSWER_WINDOW_S = 2.0
LOW_PROB = 0.6
RMS_STD_FLOOR_DB = 1.0
#: An acoustic island must cover this share of a NON_LEXICAL word to mark it a filler.
ISLAND_WORD_OVERLAP = 0.5
#: a verbatim repeat is a stumble only within one speaker's breath.
REPEAT_STUMBLE_GAP_S = 0.5

OPENERS = frozenset({"so", "okay", "ok", "well", "hey", "hello", "hi", "yeah", "right", "alright", "anyway",
                     "basically", "actually", "look", "listen", "oh", "now"})
WEAK_START = frozenset({"and", "but", "or", "because", "which", "that", "then", "also", "like", "plus"})
CONJ_START = frozenset({"and", "but", "or", "because", "so", "yet"})
ANAPHORA = frozenset({"it", "this", "that", "they", "he", "she", "these", "those", "there", "him", "her",
                      "them", "its", "which"})
WH = frozenset({"who", "what", "when", "where", "why", "how", "which"})
AUX = frozenset({"is", "are", "do", "does", "did", "can", "could", "would", "should", "will", "have", "has",
                 "was", "were"})
CONTRAST = frozenset({"never", "nobody", "secret", "mistake", "wrong", "best", "worst", "only", "stop"})
IMPERATIVE = frozenset({"buy", "stop", "do", "don't", "dont", "never", "watch", "try", "use", "get", "make",
                        "take", "think", "imagine", "remember", "look", "listen", "forget", "start", "keep",
                        "put", "go", "let", "tell"})
CONCLUSION = ("therefore", "which means", "that is why", "the point is", "in the end", "bottom line",
              "the truth is", "that means", "honestly", "this is where")
STORY = ("one day", "when i", "i remember", "years ago", "the first time", "back then", "there was", "a wedding")
STOP = frozenset({"the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "is", "are", "was", "were",
                  "it", "this", "that", "i", "you", "we", "they", "he", "she", "for", "with", "as", "be", "have",
                  "has", "do", "does", "not", "so", "if", "one", "all", "them", "me", "my", "your", "our", "its"})
COMMON_CAPS = frozenset({"i", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                         "january", "february", "march", "april", "may", "june", "july", "august", "september",
                         "october", "november", "december", "ok", "okay", "tv", "ai", "usb", "hd", "uk", "us"})
#: A claim: a superlative/comparative, a modal, a universal or a negation —
#: not a bare copula ("the picture was stunning" is an evaluation), and not
#: `only`/`most`, which are counted once, as contrast words.
#: "every morning" is a time adverbial, not a universal claim.
_TIME_EVERY = re.compile(r"\bevery (?:morning|day|night|evening|week|weekend|month|year|time|hour|minute|second)\b")
_CLAIM = re.compile(r"\b(will|would|can|cannot|can't|never|always|every|nobody|everyone|no one|nothing|than"
                    r"|should|must|more|less|fewer)\b")
_SUPERLATIVE = re.compile(r"\b(?:\w{3,}est|best|worst|most|first|last)\b")
_COMPARATIVE = re.compile(r"\b(?:than|more|less|fewer|faster|slower|better|worse|bigger|smaller|cheaper|higher"
                          r"|lower|longer|shorter|greater|larger|stronger|weaker|harder|easier|older|newer"
                          r"|quicker|lighter|heavier)\b")
#: What pairs a number into a strong one within 4 words (§3.4 number class).
_PAIRING = re.compile(r"\b(?:only|first)\b")
_DIGIT = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_CURRENCY = frozenset({"dollars", "dollar", "euros", "euro", "rupees", "pounds", "bucks", "cents"})


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------

_PRONOUN_ONE_BEFORE = frozenset({"the", "this", "that", "which", "each", "every", "any", "no", "another", "other",
                                 "last", "next", "only", "first", "second", "third", "fourth", "fifth", "sixth",
                                 "seventh", "eighth", "ninth", "tenth", "same", "new", "old", "right", "wrong",
                                 "best", "worst", "cheapest", "fastest", "safe", "good", "bad", "big", "small"})


def _is_pronoun_one(tokens: list[str], i: int) -> bool:
    """`one` after a determiner, ordinal or adjective ("the third one",
    "the cheapest one") is a pronoun, not a quantity."""
    return tokens[i] == "one" and i > 0 and (tokens[i - 1] in _PRONOUN_ONE_BEFORE or tokens[i - 1].endswith("est"))


def _number_mentions(tokens: list[str]) -> list[tuple[int, int, float, dict]]:
    """(first index, last index, value, flags) for every numeric mention."""
    out: list[tuple[int, int, float, dict]] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _is_pronoun_one(tokens, i):
            i += 1
            continue
        m = _DIGIT.fullmatch(t)
        if m:
            raw = t.replace(",", "")
            flags = {"currency": raw.startswith("$"), "percent": raw.endswith("%")}
            val = float(raw.strip("$%") or 0)
            flags["year"] = bool(re.fullmatch(r"(19|20)\d\d", raw))
            out.append((i, i, val, flags))
            i += 1
            continue
        if t in _UNITS or t in _TENS:
            j, val, cur, words = i, 0.0, 0.0, []
            while j < len(tokens) and (tokens[j] in _UNITS or tokens[j] in _TENS or tokens[j] in _SCALES):
                w = tokens[j]
                if w in _SCALES:
                    cur = (cur or 1) * _SCALES[w]
                    if _SCALES[w] >= 1000:
                        val, cur = val + cur, 0.0
                else:
                    cur += _UNITS.get(w, _TENS.get(w, 0))
                words.append(w)
                j += 1
            val += cur
            year = len(words) == 2 and words[0] in ("nineteen", "twenty", "eighteen") and words[1] in _UNITS | _TENS \
                and words[1] not in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
            out.append((i, j - 1, val, {"currency": False, "percent": False, "year": year}))
            i = j
            continue
        i += 1
    return out


def number_class(text: str) -> str | None:
    """`strong` for a quantity ≥ 10, a percentage, a currency amount or a
    number paired with a comparative/superlative within 4 words; `weak` for a
    year, a date or a small count without such a pairing; None otherwise."""
    tokens = [t for t in (norm_token(x) if not _DIGIT.fullmatch(x.strip(".,;:!?")) else x.strip(".,;:!?")
                          for x in text.split()) if t]
    mentions = _number_mentions(tokens)
    if not mentions:
        return None
    strong = False
    for a, b, val, flags in mentions:
        near = " ".join(tokens[max(0, a - 4): b + 5])
        paired = bool(_COMPARATIVE.search(near) or _SUPERLATIVE.search(near) or _PAIRING.search(near))
        currency = flags["currency"] or any(t in _CURRENCY for t in tokens[b + 1: b + 3])
        percent = flags["percent"] or "percent" in tokens[b + 1: b + 3]
        if flags["year"] and not (currency or percent):
            continue
        if val >= 10 or currency or percent or paired:
            strong = True
    return "strong" if strong else "weak"


# --------------------------------------------------------------------------
# words and sentences
# --------------------------------------------------------------------------

#: What a recogniser writes for a sound that is not speech: "(eerie music)", "[MUSIC]", "*applause*", "♪".
_SOUND_NOTE_OPEN = ("(", "[", "*", "♪", "♫")
_SOUND_NOTE_CLOSE = (")", "]", "*", "♪", "♫")


def _drop_sound_notes(tokens: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """The tokens of a segment without its bracketed sound annotations. An annotation opens on a token that
    starts with a bracket and closes on the next token that ends with one (it may be one token, "[MUSIC]")."""
    out: list[tuple[dict, str]] = []
    inside = False
    for w, text in tokens:
        rest = text.lstrip("([*♪♫")
        if not inside and text.startswith(_SOUND_NOTE_OPEN) and (not rest or rest[:1].isalpha()):    # "(1998)" is speech
            inside = not text.endswith(_SOUND_NOTE_CLOSE) or len(text) == 1
            continue
        if inside:
            inside = not text.endswith(_SOUND_NOTE_CLOSE)
            continue
        out.append((w, text))
    return out


def _words(transcript: dict) -> list[dict]:
    out: list[dict] = []
    for seg in transcript.get("segments") or []:
        toks = [(w, str(w.get("word", w.get("text", ""))).strip()) for w in seg.get("words") or []
                if "start" in w and "end" in w]
        for w, text in _drop_sound_notes([(w, t) for w, t in toks if t]):
            out.append({"t0": round(float(w["start"]), 3), "t1": round(float(w["end"]), 3), "text": text,
                        "prob": float(w.get("prob", 1.0))})
    out.sort(key=lambda w: (w["t0"], w["t1"]))
    for i, w in enumerate(out):
        w["id"] = f"w_x{i:05d}"
    return out


def _mark_fillers(words: list[dict], islands: list[dict]) -> None:
    """`filler`: a `_DEFAULT_FILLERS` token; whisper's other hesitation
    spellings when short (`Hum,` ≤ 0.7 s); a NON_LEXICAL interjection the
    acoustic detector's island covers (`Ah,`/`Huh,` written over an `uh`);
    the first of a verbatim repeat ("the the") — never across a sentence end
    ("…about money. Money was…")."""
    prev, prev_w = "", None
    for w in words:
        tok = norm_token(w["text"])
        dur = max(0.01, w["t1"] - w["t0"])
        covered = sum(max(0.0, min(w["t1"], f["t1"]) - max(w["t0"], f["t0"])) for f in islands) / dur
        if tok in LEXICAL_FILLERS or (tok in HESITATION_SPELLINGS and dur <= HESITATION_MAX_S):
            w["filler"] = True
        elif tok in NON_LEXICAL and covered >= ISLAND_WORD_OVERLAP:
            w["filler"] = True
        elif tok and tok == prev and tok not in STOP - {"the", "a", "i"} | {"that"} and prev_w is not None \
                and prev_w.get("spk") == w.get("spk") and w["t0"] - prev_w["t1"] < REPEAT_STUMBLE_GAP_S \
                and not prev_w["text"].rstrip().endswith(TERMINAL):
            prev_w["filler"] = True     # "the the": the first repeat is a stumble
        prev, prev_w = tok, w


TERMINAL = (".", "?", "!", "…", "।")
PAUSE_BREAK_S = 0.8     # ai/shorts._PAUSE_BREAK_S: the fallback for unpunctuated text


def _uncovered_islands(words: list[dict], islands: list[dict]) -> list[dict]:
    """The acoustic fillers the transcript did NOT hear: an island a filler
    word already covers (≥ 50 % of the island) is that word's cut, not a
    second one — the planner emits one decision per word and one per island."""
    keep: list[dict] = []
    for f in islands:
        dur = max(0.01, f["t1"] - f["t0"])
        heard = any(w.get("filler") and max(0.0, min(w["t1"], f["t1"]) - max(w["t0"], f["t0"])) / dur >= ISLAND_WORD_OVERLAP
                    for w in words)
        if not heard:
            keep.append(f)
    return [{**f, "id": f"af_{i + 1:04d}"} for i, f in enumerate(keep)]


def _sentence_groups(words: list[dict]) -> list[list[int]]:
    """Word-index groups per sentence by `ai/shorts._sentences`' rule — split
    on terminal punctuation; unpunctuated text falls back to real pauses —
    applied over the WHOLE word stream: whisper's segment boundaries are not
    sentence ends ("…another productivity" | "system." is one sentence)."""
    punctuated = any(w["text"].rstrip().endswith(TERMINAL) for w in words)
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        if not punctuated and cur and w["t0"] - words[cur[-1]]["t1"] >= PAUSE_BREAK_S:
            groups.append(cur)
            cur = []
        cur.append(i)
        if w["text"].rstrip().endswith(TERMINAL):
            groups.append(cur)
            cur = []
    return groups + ([cur] if cur else [])


def _word_speakers(words: list[dict], speakers_layer: dict | None) -> None:
    turns = (speakers_layer or {}).get("turns") or []
    if not turns:
        for w in words:
            w["spk"] = "S1"
        return
    for w in words:
        best = max(turns, key=lambda t: (min(w["t1"], t["t1"]) - max(w["t0"], t["t0"]),
                                         -min(abs(t["t0"] - w["t0"]), abs(t["t1"] - w["t1"]))))
        w["spk"] = best["spk"]


SPLIT_MIN_WORDS = 2


def _split_by_speaker(groups: list[list[int]], words: list[dict]) -> list[list[int]]:
    """A sentence never spans two speakers: split at a SUSTAINED change (both
    sides ≥ SPLIT_MIN_WORDS words); a lone word flipping speaker inside a
    sentence is diarization noise and takes the sentence's majority speaker."""
    out: list[list[int]] = []
    for g in groups:
        runs: list[list[int]] = [[g[0]]]
        for i in g[1:]:
            if words[i]["spk"] != words[runs[-1][-1]]["spk"]:
                runs.append([])
            runs[-1].append(i)
        merged: list[list[int]] = []
        for r in runs:
            if merged and (len(r) < SPLIT_MIN_WORDS or len(merged[-1]) < SPLIT_MIN_WORDS):
                merged[-1].extend(r)
            else:
                merged.append(list(r))
        for r in merged:
            majority = max({words[i]["spk"] for i in r}, key=lambda sp: sum(1 for i in r if words[i]["spk"] == sp))
            for i in r:
                words[i]["spk"] = majority
        out.extend(merged)
    return _merge_filler_only(out, words)


def _merge_filler_only(groups: list[list[int]], words: list[dict]) -> list[list[int]]:
    """A group of nothing but fillers/interjections is not a sentence: it
    joins its NEARER neighbour in time (an `um` 0.14 s after "deal." and
    1.6 s before "that is the test" belongs to the sentence it trails, so the
    pause after it stays a pause); ties go forward."""
    out: list[list[int]] = []
    pending: list[int] = []
    for g in groups:
        if all(norm_token(words[i]["text"]) in NON_LEXICAL or not norm_token(words[i]["text"]) for i in g):
            pending.extend(g)
            continue
        if pending and out:
            back = words[pending[0]]["t0"] - words[out[-1][-1]]["t1"]
            fwd = words[g[0]]["t0"] - words[pending[-1]]["t1"]
            if back < fwd:
                out[-1].extend(pending)
                pending = []
        out.append(pending + g)
        pending = []
    if pending:
        if out:
            out[-1].extend(pending)
        else:
            out.append(pending)
    return out


VOCATIVES = frozenset({"everyone", "everybody", "guys", "folks", "friends", "all", "team", "y'all"})


def _content(tokens: list[str]) -> list[str]:
    """Leading fillers/interjections/openers stripped, and the vocative
    that greets ("hey everyone" — `everyone` is not a universal claim)."""
    i = 0
    while i < len(tokens) and (tokens[i] in NON_LEXICAL or tokens[i] in OPENERS):
        i += 1
    if 0 < i < len(tokens) and tokens[i] in VOCATIVES and tokens[i - 1] in OPENERS:
        i += 1
    return tokens[i:]


def _is_question(text: str, content: list[str]) -> bool:
    """`?` decides; without one, a wh-word or auxiliary must be followed by an
    auxiliary/pronoun ("when did", "how many do") — "When the shutter opened"
    is a clause, not a question."""
    if text.rstrip().endswith("?"):
        return True
    if len(content) < 2:
        return False
    return (content[0] in WH and (content[1] in AUX or content[1] in ("many", "much", "long", "often")
                                  and len(content) > 2 and content[2] in AUX)) \
        or (content[0] in AUX and content[1] in ANAPHORA | {"you", "i", "we", "the", "a", "your"})


def content_tokens(text: str) -> list[str]:
    """Normalised tokens with leading fillers/interjections/openers stripped."""
    return _content([t for t in (norm_token(x) for x in text.split()) if t])


def is_imperative(text: str) -> bool:
    content = content_tokens(text)
    first = content[0] if content else ""
    return (first in IMPERATIVE or " ".join(content).startswith("do not")) and not _is_question(text, content)


def _base_features(text: str, tokens: list[str], content: list[str], dur: float, n_fill: int) -> dict:
    """§3.2 `features` — exactly C's `Features` fields (the model forbids
    more); `weak_start` lives on the sentence, `imperative` in its `kind`,
    `conjunction_start` is derived from the text by readers, and the §3.4
    delivery and importance inputs are computed by `delivery.py` on demand."""
    low = " ".join(content)
    ncls = number_class(text)
    n_words = max(0, len(tokens) - n_fill)
    first = content[0] if content else ""
    question = _is_question(text, content)
    claim = not question and bool(_CLAIM.search(_TIME_EVERY.sub("", low)) or _SUPERLATIVE.search(low) or _COMPARATIVE.search(low)
                                  or ncls == "strong")
    return {
        "wpm": int(round(n_words / dur * 60)) if dur > 0 else 0, "fillers": n_fill, "has_number": ncls is not None,
        "strong_number": ncls == "strong", "weak_number": ncls == "weak", "claim": claim,
        "conclusion_marker": any(low.startswith(c) or f" {c}" in low for c in CONCLUSION),
        "story_marker": any(c in low for c in STORY),
        "contrast_words": sum(1 for t in content if t in CONTRAST),
        "anaphora_start": first in ANAPHORA, "len_words": n_words,
    }


def _make_sentences(groups: list[list[int]], words: list[dict]) -> list[dict]:
    sents: list[dict] = []
    for n, g in enumerate(groups):
        ws = [words[i] for i in g]
        text = " ".join(w["text"] for w in ws)
        tokens = [t for t in (norm_token(w["text"]) for w in ws) if t]
        content = _content(tokens)
        spoken = [w for w in ws if not w.get("filler")] or ws
        t0, t1 = spoken[0]["t0"], spoken[-1]["t1"]
        dur = max(0.01, t1 - t0)
        feats = _base_features(text, tokens, content, dur, sum(1 for w in ws if w.get("filler")))
        question = _is_question(text, content)
        first = content[0] if content else ""
        sid = f"s_{n + 1:04d}"
        for w in ws:
            w["sent"] = sid
        sents.append({"id": sid, "t0": t0, "t1": max(t1, t0 + 0.01), "spk": spoken[0]["spk"],
                      "text": text,
                      "kind": "question" if question else ("imperative" if is_imperative(text) else "statement"),
                      "is_question": question, "answer_of": None,
                      "complete": text.rstrip().endswith((".", "?", "!", "…", "।")),
                      "weak_start": first in WEAK_START or first in CONJ_START, "topic": "t_0001",
                      "features": feats, "_tokens": content})
    return sents


def _assign_word_ids(sents: list[dict], words: list[dict]) -> None:
    """`w_<sentence>_<index>` (the sentence's own number and the word's
    position in it) for words of a sentence; `w_x<n>` for any orphan. Stable
    under an edit elsewhere in the transcript, unlike a running counter."""
    by_sent: dict[str, list[dict]] = {}
    for w in words:
        w.setdefault("sent", None)
        by_sent.setdefault(str(w["sent"]), []).append(w)
    for s in sents:
        for i, w in enumerate(by_sent.get(s["id"], [])):
            w["id"] = f"w_{s['id'][2:]}_{i:02d}"
        spoken = [w for w in by_sent.get(s["id"], []) if not w.get("filler")] or by_sent.get(s["id"], [])
        if spoken:      # the first SPOKEN word is capitalised because it opens the sentence, not because it is a name
            spoken[0]["sent_initial"] = True


def _answers(sents: list[dict]) -> None:
    for i, s in enumerate(sents):
        if not s["is_question"]:
            continue
        for nxt in sents[i + 1: i + 4]:
            if nxt["spk"] != s["spk"] and nxt["t0"] - s["t1"] <= ANSWER_WINDOW_S:
                nxt["answer_of"] = s["id"]
                break


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------

def _runs_for_false_starts(words: list[dict]) -> list[list[int]]:
    """Word runs split at cut-offs, 0.25–0.8 s gaps and sentence ends."""
    runs: list[list[int]] = [[]]
    for i, w in enumerate(words):
        runs[-1].append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["t0"] - w["t1"]) if nxt else 0.0
        cut = w["text"].rstrip(",").endswith(("—", "-"))
        if nxt is None or cut or FALSE_START_GAP[0] <= gap <= FALSE_START_GAP[1] or w["sent"] != nxt["sent"]:
            runs.append([])
    return [r for r in runs if r]


def _head(tokens: list[str]) -> list[str]:
    """The first two tokens that carry the phrase: a discourse opener or a
    conjunction in front ("So the second —", "so the second test …") is how a
    restart begins, and whisper drops it from one side as often as not."""
    body = list(tokens)
    while len(body) > 2 and body[0] in (OPENERS | CONJ_START):
        body.pop(0)
    return body[:2]


def _false_starts(words: list[dict]) -> list[dict]:
    out: list[dict] = []
    runs = _runs_for_false_starts(words)
    for r, nxt in zip(runs, runs[1:]):
        if len(r) > FALSE_START_MAX_WORDS or len(nxt) < 2:
            continue
        a, b = [words[i] for i in r], [words[i] for i in nxt]
        if b[0]["t0"] - a[-1]["t1"] > FALSE_START_WINDOW_S or a[0]["spk"] != b[0]["spk"]:
            continue
        la = _head([norm_token(w["text"]) for w in a])
        lb = _head([norm_token(w["text"]) for w in b])
        if len(la) < 2 or la != lb or " ".join(norm_token(w["text"]) for w in a) == \
                " ".join(norm_token(w["text"]) for w in b[: len(a)]) and len(a) == len(b):
            continue
        out.append({"id": f"f_{len(out) + 1:04d}", "t0": a[0]["t0"], "t1": a[-1]["t1"], "kept": b[0]["sent"],
                    "text": " ".join(w["text"] for w in a)})
    return out


def _repeats(sents: list[dict], false_start_sents: set[str]) -> list[dict]:
    out: list[dict] = []
    for j, later in enumerate(sents):
        lt = set(later["_tokens"])
        if len(later["_tokens"]) < REPEAT_MIN_TOKENS:
            continue
        for earlier in sents[:j]:
            if earlier["spk"] != later["spk"] or later["t0"] - earlier["t1"] > REPEAT_WINDOW_S:
                continue
            et = set(earlier["_tokens"])
            if len(earlier["_tokens"]) < REPEAT_MIN_TOKENS or not et:
                continue
            ratio = len(later["_tokens"]) / max(1, len(earlier["_tokens"]))
            jac = len(lt & et) / len(lt | et)
            if jac >= REPEAT_JACCARD and 1 / REPEAT_LEN_RATIO <= ratio <= REPEAT_LEN_RATIO:
                dup, of = (earlier, later) if earlier["id"] in false_start_sents else (later, earlier)
                out.append({"id": f"r_{len(out) + 1:04d}", "dup": dup["id"], "of": of["id"], "similarity": round(jac, 3)})
                break
    return out


def conjunction_start(sentence: dict) -> bool:
    content = sentence.get("_tokens") or content_tokens(sentence["text"])
    return bool(content) and content[0] in CONJ_START


def standalone_score(sentence: dict, question: dict | None) -> float:
    """§3.4 standalone: 1 − anaphora − 0.5·conjunction_start − 0.3·(an answer
    whose first 3 words lack the question's noun)."""
    f = sentence["features"]
    v = 1.0 - float(f["anaphora_start"]) - 0.5 * float(conjunction_start(sentence))
    if question is not None:
        q_tokens = question.get("_tokens") or content_tokens(question["text"])
        q_nouns = {t for t in q_tokens if t not in STOP and len(t) > 3}
        first3 = set(sentence.get("_tokens") or content_tokens(sentence["text"])[:3])
        if q_nouns and not (q_nouns & first3):
            v -= 0.3
    return round(max(0.0, min(1.0, v)), 3)


def _weak_questions(sents: list[dict]) -> list[dict]:
    by_id = {s["id"]: s for s in sents}
    out: list[dict] = []
    for ans in sents:
        q = by_id.get(ans["answer_of"] or "")
        if q is None:
            continue
        q_content = {t for t in q["_tokens"] if t not in STOP}
        restated = len(q_content & set(ans["_tokens"])) / len(q_content) if q_content else 0.0
        short_q = q["features"]["len_words"] < 4 and (ans["t1"] - ans["t0"]) >= 6.0
        if restated >= 0.6 or short_q:
            why = "answer restates the question" if restated >= 0.6 else "short question, long answer"
            out.append({"id": f"q_{len(out) + 1:04d}", "sent": q["id"], "spk": q["spk"], "answer": ans["id"],
                        "removable": standalone_score(ans, q) >= 0.7, "why": why})
    return out


def _dead_air(sents: list[dict], words: list[dict], multi: bool, unheard: list[dict] | None = None) -> list[dict]:
    """In-turn gaps ≥ DEAD_AIR_S with NO sound in them: a gap that holds
    `unheard` voice (somebody talks, the transcript did not hear it) is not."""
    out: list[dict] = []
    by_sent: dict[str, list[dict]] = {}
    for w in words:
        by_sent.setdefault(w["sent"], []).append(w)
    for s in sents:
        ws = by_sent.get(s["id"], [])
        for a, b in zip(ws, ws[1:]):
            if b["t0"] - a["t1"] >= DEAD_AIR_S and not a.get("filler"):     # a filler's trailing gap is the filler cut's
                out.append({"id": "", "t0": a["t1"], "t1": b["t0"]})
    if multi:
        for a, b in zip(sents, sents[1:]):
            if a["spk"] == b["spk"] and b["t0"] - a["t1"] >= DEAD_AIR_S:
                out.append({"id": "", "t0": a["t1"], "t1": b["t0"]})
    out = [d for d in out if not any(min(d["t1"], x["t1"]) - max(d["t0"], x["t0"]) > 0 for x in unheard or [])]
    out.sort(key=lambda d: d["t0"])
    for i, d in enumerate(out):
        d["id"] = f"d_{i + 1:04d}"
    return out


UNHEARD_MIN_S = 0.3


def unheard_voice(words: list[dict], islands: list[dict], vad: list) -> list[dict]:
    """`technical` flags: voiced stretches ≥ UNHEARD_MIN_S under no word and
    no acoustic filler — sound the transcript does not name (speech whisper
    dropped, a laugh, a cough). Never a silence."""
    spans = sorted((float(x["t0"]), float(x["t1"])) for x in (*words, *islands))
    out: list[dict] = []
    for a, b in ((float(r[0]), float(r[1])) for r in vad):
        t = a
        for s0, s1 in spans:
            if s1 <= t or s0 >= b:
                continue
            if s0 - t >= UNHEARD_MIN_S:
                out.append((t, s0))
            t = max(t, s1)
        if b - t >= UNHEARD_MIN_S:
            out.append((t, b))
    return [{"id": f"x_{i + 1:04d}", "t0": round(t0, 3), "t1": round(t1, 3), "why": "unheard_voice"}
            for i, (t0, t1) in enumerate(out)]


def _words_to_check(words: list[dict]) -> list[dict]:
    """`words_to_check` and, on each such word, `check: <why>` (spec §3.2)."""
    out: list[dict] = []
    for w in words:
        if w.get("filler"):
            continue                # a filler is cut, not proofread
        raw = w["text"].strip(".,;:!?\"'")
        tok = norm_token(raw)
        why = None
        if w["prob"] < LOW_PROB:
            why = "low_prob"
        elif re.search(r"\d", raw):
            why = "digit"
        elif raw[:1].isupper() and not w.get("sent_initial") and tok not in COMMON_CAPS and len(tok) > 1:
            why = "capitalised_unknown"
        if why:
            w["check"] = why
            out.append({"word": w["id"], "why": why})
    return out


# --------------------------------------------------------------------------
# turns and speakers
# --------------------------------------------------------------------------

def _turns(sents: list[dict], speakers_layer: dict | None) -> list[dict]:
    layer_turns = (speakers_layer or {}).get("turns") or []
    out: list[dict] = []
    if layer_turns:
        for t in layer_turns:
            inside = [s["id"] for s in sents if s["spk"] == t["spk"] and min(s["t1"], t["t1"]) - max(s["t0"], t["t0"]) > 0]
            out.append({"id": t["id"], "spk": t["spk"], "t0": t["t0"], "t1": t["t1"], "sents": inside})
        return out
    for s in sents:
        if out and s["t0"] - out[-1]["t1"] < DEAD_AIR_S:
            out[-1]["t1"] = s["t1"]
            out[-1]["sents"].append(s["id"])
        else:
            out.append({"id": f"u_{len(out) + 1:04d}", "spk": s["spk"], "t0": s["t0"], "t1": s["t1"], "sents": [s["id"]]})
    return out


def speaker_roles(layer: dict, speakers_layer: dict | None) -> list[dict]:
    """The graph header's `speakers[]` (C's `GraphSpeaker`): the speakers
    layer's rows with `share` and `questions` from this speech layer and
    `role_guess` by §3.4 (host = higher question share AND lower speech
    share; unknown within 10 %). Deterministic from the two layers."""
    return _speakers(layer["sentences"], speakers_layer)


def _speakers(sents: list[dict], speakers_layer: dict | None) -> list[dict]:
    base = [dict(s) for s in ((speakers_layer or {}).get("speakers") or [])]
    if not base:
        base = [{"id": "S1", "label": "SPEAKER_00", "share": 1.0, "questions": 0, "role_guess": "unknown"}]
    talk = {s["id"]: 0.0 for s in base}
    for s in sents:
        talk[s["spk"]] = talk.get(s["spk"], 0.0) + (s["t1"] - s["t0"])
    total = sum(talk.values()) or 1.0
    for sp in base:
        sp["questions"] = sum(1 for s in sents if s["spk"] == sp["id"] and s["is_question"])
        sp["share"] = round(talk.get(sp["id"], 0.0) / total, 3)
    if len(base) >= 2:
        _roles(base)
    return base


def _roles(speakers: list[dict]) -> None:
    """host = higher question share AND lower speech share; unknown within 10 %."""
    q_total = sum(s["questions"] for s in speakers) or 1
    ranked = sorted(speakers, key=lambda s: (-s["questions"] / q_total, s["share"]))
    a, b = ranked[0], ranked[1]
    if (a["questions"] - b["questions"]) / q_total > 0.1 and a["share"] < b["share"] + 0.1:
        a["role_guess"] = "host"
        for s in speakers:
            if s is not a:
                s["role_guess"] = "guest"


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def build_speech_layer(transcript: dict, *, audio_layer: dict, pcm: np.ndarray | None = None, sr: int = 16000,
                       speakers_layer: dict | None = None, params: dict | None = None) -> dict:
    """The speech layer of the transcript of record."""
    words = _words(transcript)
    _word_speakers(words, speakers_layer)
    groups = _sentence_groups(words) if words else []     # on whisper's times, before the repairs
    sent_of = {i: gi for gi, g in enumerate(groups) for i in g}
    vad = audio_layer.get("vad") or []
    repair_word_times(words, vad, sent_of)
    islands = acoustic_fillers(pcm, sr, vad=vad, words=[{"t0": w["t0"], "t1": w["t1"], "text": w["text"]}
                                                       for w in words]) if pcm is not None and len(pcm) else []
    technical = unheard_voice(words, islands, vad)
    _mark_fillers(words, islands)
    islands = _uncovered_islands(words, islands)
    groups = _split_by_speaker(groups, words) if words else []
    sents = _make_sentences(groups, words)
    _assign_word_ids(sents, words)
    _answers(sents)
    false_starts = _false_starts([w for w in words if w["sent"]])
    fs_sents = {w["sent"] for f in false_starts for w in words if w["sent"] and w["t0"] == f["t0"]}
    repeats = _repeats(sents, fs_sents)
    multi = len({s["spk"] for s in sents}) >= 2
    words_to_check = _words_to_check(words)
    return {
        "params": {"backend": "upload_transcript", "model": None, "language": transcript.get("language"),
                   "prompt": "none", "analysis_version": ANALYSIS_VERSION, **(params or {})},
        "words": [{k: v for k, v in w.items() if k != "sent_initial"} for w in words],
        "acoustic_fillers": islands,
        "words_to_check": words_to_check,
        "sentences": [{k: v for k, v in s.items() if k != "_tokens"} for s in sents],
        "turns": _turns(sents, speakers_layer),
        "flags": {"false_starts": false_starts, "repeats": repeats, "weak_questions": _weak_questions(sents),
                  "dead_air": _dead_air(sents, [w for w in words if w["sent"]], multi, technical),
                  "technical": technical},
    }


__all__ = ["DEAD_AIR_S", "number_class", "standalone_score", "conjunction_start", "content_tokens", "is_imperative",
           "build_speech_layer", "speaker_roles", "unheard_voice", "OPENERS", "WEAK_START", "CONJ_START", "CONTRAST", "STOP"]
