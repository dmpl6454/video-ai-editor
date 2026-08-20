"""Caption cue formatting — turn a raw word stream into broadcast-quality cues.

Whisper gives words with timestamps. Good captions are NOT one-cue-per-segment
(too long) nor one-word-per-cue (flickery). They follow the conventions pro
captioners use:

  - ≤ ~42 chars per line, ≤ 2 lines per cue
  - reading speed ≤ ~17 chars/sec (CPS) so viewers can actually read it
  - 1.0s ≤ duration ≤ ~5.0s
  - break on sentence punctuation and on real pauses in the speech
  - balanced 2-line wrap (don't leave one word dangling)

Works for Devanagari (Hindi) and Latin (English) alike — it counts characters,
splits on spaces, and treats the Devanagari danda (।/॥) as sentence-final.

WHY THIS IS PHRASE-FIRST RATHER THAN GREEDY
-------------------------------------------
The earlier version walked the words and cut wherever a budget ran out. Measured
on 25s of real Hinglish speech, that put **8 of 11** cue boundaries in the middle
of a phrase — mid-breath cuts like `kya bakchoodi kar` / `rahe ho main` — which is
exactly the "captions don't match what's being said" complaint. It also produced
five cues under a second (shortest 0.40s, a flicker) and, worst of all, a
**35 CPS** cue while nominally enforcing a 17 CPS limit.

That last number exposed the design error: the old code *split* a cue when its
reading speed was too high. Splitting cannot reduce chars-per-second — the text
and its duration shrink together, so each half is just as dense and now flickers
too. Reading speed is fixed by holding a cue on screen LONGER, never by cutting
it up. So:

  1. The word stream is first cut into PHRASES at sentence punctuation and at
     real pauses. These are the only boundaries the speech itself offers.
  2. Phrases are packed into cues. A phrase too big for one cue is split at its
     own largest internal pause, not at the character budget's edge.
  3. Timing is then polished: a cue that is too short or too dense is EXTENDED
     into the silence that follows it, bounded by the next cue's start so a
     caption never sits over the next line's speech.

Cue start/end always come from the first and last word's own timestamps, so what
is on screen begins when the words begin.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Cue:
    start: float
    end: float
    text: str  # may contain a single "\n" for a 2-line cue

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


_SENT_END = ("।", "॥", ".", "!", "?", "…")
# A comma is a weaker break: usable when a phrase must be split anyway, but not
# a reason to end a cue on its own.
_CLAUSE_END = (",", ";", ":", "—")


def _w_text(w: dict) -> str:
    return (w.get("word") or "").strip()


def _join(ws: list[dict]) -> str:
    return " ".join(t for t in (_w_text(w) for w in ws) if t)


def _gap_after(words: list[dict], i: int) -> float:
    """Silence between word i and word i+1; 0.0 at the end of the stream."""
    if i + 1 >= len(words):
        return 0.0
    return max(0.0, float(words[i + 1]["start"]) - float(words[i]["end"]))


def _split_into_phrases(words: list[dict], gap_break: float) -> list[list[dict]]:
    """Cut the stream where the SPEAKER paused or finished a sentence."""
    phrases: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        if not _w_text(w):
            continue
        cur.append(w)
        if _w_text(w).endswith(_SENT_END) or _gap_after(words, i) >= gap_break:
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)
    return phrases


def _best_split_index(ws: list[dict], max_chars_total: int, max_dur: float) -> int:
    """Where to cut a phrase that will not fit in one cue.

    Prefers the largest internal pause, then a clause boundary, then the word
    boundary nearest the middle — in every case a real boundary in the speech
    rather than wherever the character budget happened to run out. The returned
    index is the count of words in the FIRST part (always ≥1 and < len(ws)).

    A cut must leave a first part that fits BOTH budgets. Checking only
    characters (as this first did) let a chunk through that fit in 84 characters
    but ran 5.86s against a 5.0s cap — and since the packer never revisits an
    emitted chunk, that over-long cue survived to the screen. Real speech makes
    this the common case, not a corner: the clip this was measured on runs
    12.78s with a largest internal pause of 0.08s, so the character budget is
    reached long before any pause is.
    """
    n = len(ws)
    if n <= 1:
        return 1
    # Only consider cuts whose first part fits; otherwise the second cue would
    # inherit the overflow and we would recurse forever.
    t0 = float(ws[0]["start"])
    feasible = []
    run = 0
    for i in range(n - 1):
        run += len(_w_text(ws[i])) + (1 if i else 0)
        if run <= max_chars_total and float(ws[i]["end"]) - t0 <= max_dur:
            feasible.append(i + 1)
    if not feasible:
        return 1                      # one word longer/slower than the budget
    mid = n / 2
    best, best_key = feasible[0], None
    for i in feasible:
        gap = _gap_after(ws, i - 1)
        clause = 1 if _w_text(ws[i - 1]).endswith(_CLAUSE_END) else 0
        # Sort by: biggest pause, then clause punctuation, then closest to the
        # middle (so two-line cues stay balanced).
        key = (round(gap, 2), clause, -abs(i - mid))
        if best_key is None or key > best_key:
            best, best_key = i, key
    return best


def _wrap_two_lines(text: str, max_chars: int) -> str:
    """Wrap plain text into at most 2 balanced lines; single line if it fits.

    Length-balanced only — used when there is no word timing to consult (an
    imported subtitle file, or a cue whose pauses give no better break).
    `_wrap_words_two_lines` is the timing-aware version.
    """
    if len(text) <= max_chars:
        return text
    words = text.split(" ")
    if len(words) < 2:
        return text
    target = len(text) / 2
    best_i, best_d = 1, 1e9
    run = 0
    for i in range(1, len(words)):
        run += len(words[i - 1]) + 1
        d = abs(run - target)
        if d < best_d:
            best_d, best_i = d, i
    return f"{' '.join(words[:best_i])}\n{' '.join(words[best_i:])}"


def _wrap_words_two_lines(ws: list[dict], max_chars: int) -> str:
    """Wrap into at most 2 lines, breaking at a pause when there is one.

    The line break is a reading cue, so putting it where the speaker paused
    keeps the two halves meaningful — a purely length-balanced split cut
    phrases like `STD / hi hua tha` apart.
    """
    text = _join(ws)
    if len(text) <= max_chars or len(ws) < 2:
        return text
    n = len(ws)
    mid = n / 2
    best, best_key = 1, None
    run = 0
    for i in range(1, n):
        run += len(_w_text(ws[i - 1])) + (1 if i > 1 else 0)
        line1 = _join(ws[:i])
        line2 = _join(ws[i:])
        if len(line1) > max_chars or len(line2) > max_chars:
            continue
        gap = _gap_after(ws, i - 1)
        key = (round(gap, 2), -abs(i - mid))
        if best_key is None or key > best_key:
            best, best_key = i, key
    if best_key is None:
        # No pause-based split keeps both lines inside the budget — fall back to
        # pure length balancing rather than dropping words.
        return _wrap_two_lines(text, max_chars)
    return f"{_join(ws[:best])}\n{_join(ws[best:])}"


def build_cues(
    words: list[dict],
    *,
    max_chars: int = 42,
    max_lines: int = 2,
    min_dur: float = 1.0,
    max_dur: float = 5.0,
    max_cps: float = 17.0,
    gap_break: float = 0.35,
    lead_out: float = 0.08,
) -> list[Cue]:
    """Pack word-timed `words` ({start,end,word}) into readable, speech-aligned cues.

    `gap_break` is the pause that ends a phrase. 0.35s is a real breath in
    conversational speech; the old 0.6s treated ordinary pauses as continuous
    speech, which is why boundaries ended up mid-phrase instead.

    `lead_out` is how far a cue may be held BEFORE the next word starts when it
    needs more time on screen, so an extended cue never covers the next line.
    """
    words = [w for w in words if _w_text(w)]
    if not words:
        return []

    char_budget = max_chars * max_lines

    # --- 1. the speech's own boundaries ------------------------------------
    phrases = _split_into_phrases(words, gap_break)

    # --- 2. pack phrases into cues, splitting a long phrase at its own pause -
    groups: list[list[dict]] = []
    for phrase in phrases:
        # Split THIS phrase into as many parts as its budgets require. Kept
        # per-phrase so the tail-merge below can only ever join pieces of one
        # continuous breath: merging across a boundary undid the split the
        # speech itself provided, and turned "Done." / "Next one" into a single
        # cue reading `Done. Next one`.
        parts: list[list[dict]] = []
        pending = phrase
        while True:
            text = _join(pending)
            dur = float(pending[-1]["end"]) - float(pending[0]["start"])
            if len(text) <= char_budget and dur <= max_dur:
                break
            cut = _best_split_index(pending, char_budget, max_dur)
            parts.append(pending[:cut])
            pending = pending[cut:]
            if not pending:
                break
        if pending:
            # A 1-2 word tail left over from splitting reads as a flicker, so
            # fold it back into the previous part of the SAME phrase when both
            # budgets still allow it.
            if (parts and len(pending) <= 2
                    and len(_join(parts[-1])) + len(_join(pending)) + 1 <= char_budget
                    and float(pending[-1]["end"]) - float(parts[-1][0]["start"]) <= max_dur):
                parts[-1] = parts[-1] + pending
            else:
                parts.append(pending)
        groups.extend(parts)

    # --- 3. timing: exact word boundaries, then extend where it helps -------
    cues: list[Cue] = []
    for gi, ws in enumerate(groups):
        start = float(ws[0]["start"])
        end = float(ws[-1]["end"])
        text = _wrap_words_two_lines(ws, max_chars)
        n_chars = len(text.replace("\n", " "))

        # How far the cue may run: the next cue's first word, less a lead-out.
        if gi + 1 < len(groups):
            ceiling = float(groups[gi + 1][0]["start"]) - lead_out
        else:
            ceiling = end + max_dur
        ceiling = max(ceiling, end)          # never pull the end back

        need = end
        if end - start < min_dur:
            need = max(need, start + min_dur)
        if max_cps > 0:
            # Holding it longer is the ONLY thing that reduces reading speed.
            need = max(need, start + n_chars / max_cps)
        end = min(max(end, need), max(end, min(ceiling, start + max_dur)))
        cues.append(Cue(start=start, end=end, text=text))

    return cues


def cues_from_segments(segments: list[dict], **kw) -> list[Cue]:
    """Build cues from whisper segments. Uses word-level timing when present;
    falls back to evenly-splitting a segment's text across its duration when a
    segment has no words (e.g. an imported .srt)."""
    words: list[dict] = []
    for seg in segments:
        ws = seg.get("words") or []
        if ws:
            words.extend(ws)
            continue
        # No word timing: synthesize evenly-spaced pseudo-words from the text.
        text = (seg.get("text") or "").strip()
        toks = text.split(" ")
        if not toks:
            continue
        s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
        step = (e - s) / max(1, len(toks))
        for j, tok in enumerate(toks):
            words.append({"word": tok, "start": s + j * step, "end": s + (j + 1) * step})
    return build_cues(words, **kw)
