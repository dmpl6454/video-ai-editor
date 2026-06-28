"""Caption cue formatting — turn a raw word stream into broadcast-quality cues.

Whisper gives words with timestamps. Good captions are NOT one-cue-per-segment
(too long) nor one-word-per-cue (flickery). They follow the conventions pro
captioners use:

  - ≤ ~42 chars per line, ≤ 2 lines per cue
  - reading speed ≤ ~17 chars/sec (CPS) so viewers can actually read it
  - 1.0s ≤ duration ≤ 6.0s
  - break on sentence punctuation and on long pauses
  - balanced 2-line wrap (don't leave one word dangling)

Works for Devanagari (Hindi) and Latin (English) alike — it counts characters,
splits on spaces, and treats the Devanagari danda (।/॥) as sentence-final.
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


def _wrap_two_lines(text: str, max_chars: int) -> str:
    """Wrap into at most 2 balanced lines; single line if it already fits."""
    if len(text) <= max_chars:
        return text
    words = text.split(" ")
    if len(words) < 2:
        return text
    # Find the split point closest to the middle of the character length.
    target = len(text) / 2
    best_i, best_d = 1, 1e9
    run = 0
    for i in range(1, len(words)):
        run += len(words[i - 1]) + 1
        d = abs(run - target)
        if d < best_d:
            best_d, best_i = d, i
    line1 = " ".join(words[:best_i])
    line2 = " ".join(words[best_i:])
    return f"{line1}\n{line2}"


def build_cues(
    words: list[dict],
    *,
    max_chars: int = 42,
    max_lines: int = 2,
    min_dur: float = 1.0,
    max_dur: float = 6.0,
    max_cps: float = 17.0,
    gap_break: float = 0.6,
) -> list[Cue]:
    """Greedily pack `words` ({start,end,word}) into readable cues.

    Breaks a cue when the next word would overflow the char budget, push the
    duration past max_dur, or when the current word ends a sentence or a long
    pause follows. Enforces min_dur by stretching the end into the gap.
    """
    char_budget = max_chars * max_lines
    cues: list[Cue] = []
    cur: list[dict] = []

    def _text(ws: list[dict]) -> str:
        return " ".join((w.get("word") or "").strip() for w in ws if (w.get("word") or "").strip())

    def _flush(next_word: dict | None) -> None:
        if not cur:
            return
        text = _text(cur)
        if not text:
            cur.clear()
            return
        start = float(cur[0]["start"])
        end = float(cur[-1]["end"])
        # Stretch a too-short cue toward the next word (or by min_dur).
        if end - start < min_dur:
            limit = float(next_word["start"]) - 0.05 if next_word else start + min_dur
            end = min(max(end, start + min_dur), max(end, limit))
        cues.append(Cue(start=start, end=end, text=_wrap_two_lines(text, max_chars)))
        cur.clear()

    for i, w in enumerate(words):
        word = (w.get("word") or "").strip()
        if not word:
            continue
        nxt = words[i + 1] if i + 1 < len(words) else None
        tentative = _text(cur + [w])
        dur = float(w["end"]) - float(cur[0]["start"]) if cur else 0.0
        cps = len(tentative) / max(0.1, dur) if dur > 0 else 0.0

        too_long = len(tentative) > char_budget
        too_slow = dur > max_dur
        too_fast = cps > max_cps and len(cur) >= 3  # only break for CPS once it's a real cue

        if cur and (too_long or too_slow or too_fast):
            _flush(w)

        cur.append(w)

        ends_sentence = word.endswith(_SENT_END)
        big_gap = nxt is not None and (float(nxt["start"]) - float(w["end"])) >= gap_break
        if ends_sentence or big_gap:
            _flush(nxt)

    _flush(None)
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
