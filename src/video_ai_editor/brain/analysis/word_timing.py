"""Word-timing repairs against the audio layer's voiced runs.

whisper.cpp's word spans are good to a few hundred milliseconds and wrong in
recognisable ways (all MEASURED on the fixtures): a token squeezed onto the
next sentence's onset past a pause ("them." as a 20 ms token after a 0.86 s
gap); a token spanning an unvoiced gap; the leading words of a sentence
spread across the preceding pause; word ends at a plosive closure. Every
repair here moves a word toward the SOUND — the VAD runs of the audio layer —
and never reorders words; no repair leaves `t1 < t0`.

Passes, in order (`repair_word_times`):
  squeezed → retime leading → gap snap → collapse unvoiced → pull back orphan
  tails → trim to voiced → align to run edges → tile the runs.
`sent_of` maps a word index to its sentence group (taken before any repair).
"""
from __future__ import annotations

from .fillers import HESITATION_SPELLINGS, LEXICAL_FILLERS, NON_LEXICAL, norm_token

ORPHAN_GAP_S = 0.5
ORPHAN_MAX_S = 0.6
TRIM_MAX_S = 0.6
EXTEND_GAP_S = 0.25
EDGE_ALIGN_S = 0.35
EXTEND_MAX_S = 0.7
TERMINAL = (".", "?", "!", "…", "।")


WORD_GAP_SNAP_S = 0.2
SQUEEZED_MAX_S = 0.06
LEADING_VOICED_SHARE = 0.5
TILE_MAX_S = 1.0
_HESITATIONS = LEXICAL_FILLERS | HESITATION_SPELLINGS | NON_LEXICAL
SQUEEZED_ABUT_S = 0.03


def snap_words_to_vad(words: list[dict], runs: list[tuple[float, float]], sent_of: dict[int, int]) -> None:
    """A word never spans a pause: whisper.cpp's token span can run across a
    gap to the next segment (measured: "them." placed inside a 0.86 s retake
    gap and ending at the next take's onset), so a word whose span contains an
    unvoiced gap ≥ WORD_GAP_SNAP_S is cut to the run that continues its
    predecessor (a sentence's first word takes the later run — an onset).
    `sent_of` maps a word index to its sentence group (whisper's own
    segmentation, taken before any repair)."""
    _relocate_squeezed_words(words, runs)
    _retime_leading_words(words, runs, sent_of)
    prev_end = None
    for w in words:
        gaps = [(x1, y0) for (x0, x1), (y0, y1) in zip(runs, runs[1:])
                if x1 > w["t0"] and y0 < w["t1"] and y0 - x1 >= WORD_GAP_SNAP_S]
        if gaps:
            g0, g1 = gaps[0][0], gaps[-1][1]
            follows = prev_end is not None and prev_end <= g0 + 0.05 and w["t0"] - prev_end < 0.5
            if follows:
                w["t1"] = round(g0, 3)
                w["t0"] = round(min(w["t0"], max(prev_end, g0 - 0.3)), 3)
            else:
                w["t0"] = round(g1, 3)
        prev_end = w["t1"]
    _collapse_unvoiced_words(words, runs)
    for w in words:
        if w["t1"] < w["t0"]:
            w["t1"] = w["t0"]


def _overlap_runs(w: dict, runs: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(w["t1"], b) - max(w["t0"], a)) for a, b in runs)


def _retime(ws: list[dict], start: float, end: float) -> None:
    """Spread `ws` over [start, end] in proportion to their own durations."""
    durs = [max(0.01, w["t1"] - w["t0"]) for w in ws]
    total = sum(durs)
    span = max(0.0, end - start)
    t = start
    for w, d in zip(ws, durs):
        w["t0"] = round(t, 3)
        t = start + span * (sum(durs[: ws.index(w) + 1]) / total) if span > 0 else start
        w["t1"] = round(max(w["t0"], t), 3)


def _retime_leading_words(words: list[dict], runs: list[tuple[float, float]], sent_of: dict[int, int]) -> None:
    """The leading words of a sentence that whisper placed IN SILENCE before the
    sentence's first voiced onset (the first VAD run starting after the
    previous sentence's last word) are re-timed, together with the word that
    does reach that onset, into the onset run."""
    first_idx = sorted({min(i for i, g in sent_of.items() if g == gi) for gi in set(sent_of.values())})
    for start_i in first_idx:
        gi = sent_of[start_i]
        prev_end = words[start_i - 1]["t1"] if start_i > 0 else -1.0
        first = words[start_i]
        if _overlap_runs(first, runs) >= LEADING_VOICED_SHARE * max(0.01, first["t1"] - first["t0"]):
            continue        # whisper put the sentence's first word ON sound: nothing to move
        onset = next((a for a, b in runs if a >= prev_end - 0.05 and a <= first["t1"] + 30.0), None)
        if onset is None or first["t1"] > onset:
            continue
        j = start_i
        while j < len(words) and sent_of.get(j) == gi and words[j]["t1"] <= onset:
            j += 1
        if j >= len(words) or sent_of.get(j) != gi:
            continue
        _retime(words[start_i: j + 1], onset, max(onset, words[j]["t1"]))


def _collapse_unvoiced_words(words: list[dict], runs: list[tuple[float, float]]) -> None:
    """A word with no voiced sample under it (whatever remains after the
    other repairs) collapses onto the nearest run edge, 10 ms long."""
    edges = sorted({e for r in runs for e in r})
    for w in words:
        if _overlap_runs(w, runs) > 0:
            continue
        mid = (w["t0"] + w["t1"]) / 2
        edge = min(edges, key=lambda e: abs(e - mid))
        w["t0"], w["t1"] = round(edge, 3), round(edge + 0.01, 3)


def _is_squeezed(prev_end: float, a: dict, b: dict) -> bool:
    """`a` sits on `b`'s onset: it overlaps `b` by half of itself, or it is a
    sliver (≤ SQUEEZED_MAX_S) that ABUTS `b` after a gap from its predecessor
    (measured on a camera microphone: "second," 76.81–76.84 before "so" 76.85,
    0.96 s after "the")."""
    dur = max(0.01, a["t1"] - a["t0"])
    if min(a["t1"], b["t1"]) - max(a["t0"], b["t0"]) >= 0.5 * dur:
        return True
    return dur <= SQUEEZED_MAX_S and 0.0 <= b["t0"] - a["t1"] <= SQUEEZED_ABUT_S and a["t0"] - prev_end >= WORD_GAP_SNAP_S


def _relocate_squeezed_words(words: list[dict], runs: list[tuple[float, float]]) -> None:
    """A word squeezed onto its successor's onset (measured: "them." and
    "buy?" as 20 ms tokens at the NEXT sentence's first word, past a pause)
    is moved back to the end of the run its predecessor speaks in."""
    for a, b, i in zip(words, words[1:], range(len(words))):
        if i == 0 or not _is_squeezed(words[i - 1]["t1"], a, b):
            continue
        prev_end = words[i - 1]["t1"]
        run = next(((x0, x1) for x0, x1 in runs if x0 - 0.05 <= prev_end <= x1 + 0.05), None)
        if run is None or run[1] > b["t0"]:
            continue
        a["t1"] = round(max(run[1], prev_end), 3)
        a["t0"] = round(max(prev_end, a["t1"] - 0.3), 3)


def _pull_back_orphan_tails(words: list[dict], runs: list[tuple[float, float]], sent_of: dict[int, int]) -> None:
    """A sentence-final token that whisper placed at the NEXT sentence's onset
    (measured: "work?" at 54.36 after "small" — itself in silence — with the
    next sentence starting at 54.55) belongs to the run its predecessor speaks
    in: it moves to that run's end."""
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        last_of_group = nxt is None or sent_of.get(i + 1) != sent_of.get(i)
        if i == 0 or not last_of_group or not w["text"].rstrip().endswith(TERMINAL):
            continue
        prev = words[i - 1]
        if sent_of.get(i - 1) != sent_of.get(i) or w["t0"] - prev["t1"] < ORPHAN_GAP_S:
            continue
        if w["t1"] - w["t0"] > ORPHAN_MAX_S or (nxt is not None and nxt["t0"] - w["t1"] > 0.05):
            continue
        before = [b for _a, b in runs if b <= prev["t1"] + 0.05]
        edge = max(before) if before else prev["t1"]
        w["t0"] = round(max(prev["t1"], edge - 0.01), 3)
        w["t1"] = round(w["t0"] + 0.01, 3)


def _trim_to_voiced(words: list[dict], runs: list[tuple[float, float]]) -> None:
    """A word's silent margins (whisper's ends run past the sound) are cut to
    the voiced support under it; interior dips are left alone."""
    for w in words:
        ov = [(max(a, w["t0"]), min(b, w["t1"])) for a, b in runs if min(b, w["t1"]) - max(a, w["t0"]) > 0.005]
        if not ov:
            continue
        if ov[0][0] - w["t0"] <= TRIM_MAX_S:
            w["t0"] = round(ov[0][0], 3)
        if w["t1"] - ov[-1][1] <= TRIM_MAX_S:
            w["t1"] = round(ov[-1][1], 3)


def _align_words_to_runs(words: list[dict], runs: list[tuple[float, float]]) -> None:
    """Word edges follow the sound. For each voiced run: its first word starts
    at the run's start and its last word ends at the run's end when whisper
    was within EDGE_ALIGN_S of it (whisper's onsets run late and its ends stop
    at a plosive closure); a run NO word overlaps, adjacent (≤ EXTEND_GAP_S)
    to a word, is that word's sound whisper cut short (the "book" of
    "notebook"): the word grows over it, before or after. Words are in time
    order, so a two-pointer sweep suffices."""
    n, j = len(words), 0
    for a, b in runs:
        while j < n and words[j]["t1"] <= a:
            j += 1
        k = j
        while k < n and words[k]["t0"] < b:
            k += 1
        if k > j:                                   # words overlap the run: [j, k)
            first, last = words[j], words[k - 1]
            prev_t1 = words[j - 1]["t1"] if j > 0 else float("-inf")
            nxt_t0 = words[k]["t0"] if k < n else float("inf")
            if 0.005 < first["t0"] - a <= EDGE_ALIGN_S and prev_t1 <= a + 0.005:
                first["t0"] = round(max(a, prev_t1), 3)
            if 0.005 < b - last["t1"] <= EDGE_ALIGN_S and nxt_t0 >= b - 0.005:
                last["t1"] = round(min(b, nxt_t0), 3)
            continue
        before = words[j - 1] if j > 0 else None    # unclaimed run between words j-1 and j
        after = words[j] if j < n else None
        if before is not None and 0.0 <= a - before["t1"] <= EXTEND_GAP_S and b - before["t1"] <= EXTEND_MAX_S:
            before["t1"] = round(b if after is None else min(b, after["t0"]), 3)
        elif after is not None and 0.0 <= after["t0"] - b <= EXTEND_GAP_S and after["t0"] - a <= EXTEND_MAX_S:
            after["t0"] = round(a if before is None else max(a, before["t1"]), 3)


def _is_hesitation(w: dict) -> bool:
    return norm_token(w["text"]) in _HESITATIONS


def _tile_run(inside: list[dict], a: float, b: float, prev_t1: float, next_t0: float) -> None:
    """The words of ONE run cover it: inner gaps close (the earlier word
    grows), the first word starts at the run's start and the last ends at its
    end — each by at most TILE_MAX_S, and never at a hesitation's edge."""
    for w, nxt in zip(inside, inside[1:]):
        gap = nxt["t0"] - w["t1"]
        if 0.005 < gap <= TILE_MAX_S and w["t1"] >= a and nxt["t0"] <= b and not (_is_hesitation(w) or _is_hesitation(nxt)):
            w["t1"] = nxt["t0"]
    first, last = inside[0], inside[-1]
    if 0.005 < first["t0"] - a <= TILE_MAX_S and prev_t1 <= a + 0.005 and not _is_hesitation(first):
        first["t0"] = round(a, 3)
    if 0.005 < b - last["t1"] <= TILE_MAX_S and next_t0 >= b - 0.005 and not _is_hesitation(last):
        last["t1"] = round(b, 3)


def _tile_runs(words: list[dict], runs: list[tuple[float, float]]) -> None:
    """Inside a voiced run a gap between words is not a pause (whisper's
    words are short of the sound — measured on a camera microphone: 26.9 s of
    135.4 s voiced under no word). Words are in time order."""
    n, j = len(words), 0
    for a, b in runs:
        while j < n and words[j]["t1"] <= a:
            j += 1
        k = j
        while k < n and words[k]["t0"] < b:
            k += 1
        if k > j:
            _tile_run(words[j:k], a, b, words[j - 1]["t1"] if j > 0 else float("-inf"),
                      words[k]["t0"] if k < n else float("inf"))


def repair_word_times(words: list[dict], vad: list, sent_of: dict[int, int]) -> None:
    """Repair `words` (`{t0, t1, text, …}`, in order) in place against `vad`."""
    runs = [(float(a), float(b)) for a, b in vad]
    if not runs:
        return
    snap_words_to_vad(words, runs, sent_of)
    _pull_back_orphan_tails(words, runs, sent_of)
    _trim_to_voiced(words, runs)
    _align_words_to_runs(words, runs)
    _tile_runs(words, runs)
    for w in words:
        if w["t1"] < w["t0"]:
            w["t1"] = w["t0"]


__all__ = ["repair_word_times", "snap_words_to_vad"]
