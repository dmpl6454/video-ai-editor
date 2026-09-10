"""Pure helpers the recipes reason with: where beat splits may go, and what
a hook line looks like when no brain wrote one (spec §2.4 `beat_sync`,
`hook`). Both are table-tested in tests/test_prompt_recipes.py.
"""
from __future__ import annotations

from .recipes import FILLERS_STRICT


MAX_BEAT_SPLITS = 12
MIN_SHOT_S = 0.8
WORD_EDGE_TOLERANCE_S = 0.12
PULSE_SCALE = 1.05
PULSE_RISE_S = 0.4


def beat_split_times(beats: list[float], extent: float, word_spans: list[tuple[float, float]], *,
                     subdivision: int = 4, max_splits: int = MAX_BEAT_SPLITS,
                     min_shot: float = MIN_SHOT_S) -> list[float]:
    """Split instants on the beat grid: every `subdivision`-th beat, thinned so
    at most `max_splits` land evenly across `extent`, dropping any that sits
    inside a word or within WORD_EDGE_TOLERANCE_S of one (a split 100 ms
    after a word starts still cuts the word), or that would leave a shot
    shorter than `min_shot`. Pure; unit-tested."""
    if extent <= min_shot * 2 or not beats:
        return []
    candidates = [b for i, b in enumerate(sorted(beats)) if i % max(1, subdivision) == 0
                  and min_shot <= b <= extent - min_shot]
    if not candidates:
        return []

    def _inside_word(t: float) -> bool:
        for s, e in word_spans:
            if s - WORD_EDGE_TOLERANCE_S < t < e + WORD_EDGE_TOLERANCE_S:
                return True
            if s - WORD_EDGE_TOLERANCE_S > t:
                break
        return False

    candidates = [c for c in candidates if not _inside_word(c)]
    if not candidates:
        return []
    if len(candidates) > max_splits:
        stride = len(candidates) / max_splits
        candidates = [candidates[int(i * stride)] for i in range(max_splits)]
    out: list[float] = []
    last = 0.0
    for c in candidates:
        if c - last >= min_shot and extent - c >= min_shot:
            out.append(round(c, 3))
            last = c
    return out


_CANNED_HOOK = "WATCH THIS BEFORE YOU SCROLL"


#: A hook must not END on one of these — "TODAY I AM TAKING A" is a fragment,
#: not a line. Trimmed from the tail (and a longer clause preferred) so the
#: heuristic picks a whole thought the way a content brain would.
_DANGLING = frozenset({"a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "but", "with",
                       "is", "are", "was", "were", "be", "am", "my", "your", "our", "its", "this", "that",
                       "so", "if", "as", "by", "from", "into", "about", "than", "then"})


def _trim_dangling(words: list[str]) -> list[str]:
    out = list(words)
    while out and out[-1].strip(",.!?;:'\"").lower() in _DANGLING:
        out.pop()
    return out


def heuristic_hook(transcript_head: str, *, max_words: int = 7) -> str | None:
    """The strongest whole clause of the transcript head, ≤ `max_words`,
    openers and fillers stripped, upper-cased. None when there is nothing to
    work with — never a canned line here; the caller decides whether canned
    is acceptable and says so.

    Delegates to `brains.content.heuristic_hook_candidates` (claim-scored
    sentences, openers like "hey everyone" removed) so the no-model path and
    the content-brain fallback pick the SAME line; the old first-seven-words
    rule shipped "HEY EVERYONE TODAY I AM TAKING A" on the benchmark clip —
    cut at word 7, ending on an article. The tail is then trimmed so the
    line never ends on an article or preposition."""
    head = (transcript_head or "").replace("\n", " ").strip()
    if not head:
        return None
    from .brains.content import heuristic_hook_candidates      # lazy: content imports recipes
    candidates = heuristic_hook_candidates(head, n=3, max_words=max_words)
    for cand in candidates:
        words = _trim_dangling(cand.split())
        text = " ".join(words).strip(" .,!?")
        if len(text) >= 3 and len(words) >= 2:
            return text.upper()[:60]
    # Fallback: the first clause, fillers stripped (a head of two words still
    # yields a line rather than None).
    words = [w for w in head.split() if w.strip(",.!?;:").lower() not in FILLERS_STRICT]
    clause: list[str] = []
    for w in words:
        clause.append(w.strip(",;:"))
        if w.endswith((".", "!", "?")) or len(clause) >= max_words:
            break
    clause = _trim_dangling(clause) or clause
    text = " ".join(clause).strip(" .,!?")
    return text.upper()[:60] if len(text) >= 3 else None


__all__ = ["MAX_BEAT_SPLITS", "MIN_SHOT_S", "WORD_EDGE_TOLERANCE_S", "PULSE_SCALE", "PULSE_RISE_S",
           "beat_split_times", "_CANNED_HOOK", "heuristic_hook"]
