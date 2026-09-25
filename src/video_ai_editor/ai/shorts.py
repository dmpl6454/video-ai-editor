"""Short-form highlight extraction: distinct, complete, strong moments.

QA-068. "make 3 shorts under 30 seconds" returned three back-to-back windows
(11.1-23.83, 23.83-38.3, 38.3-51.03) that started and ended mid-sentence, kept
the ums and the 2 s pauses, and were hooked with fragments ("A JACKET POCKET
AND THE GRIP"). The old picker scored fixed SHOTS by loudness and word count,
padded them out to `min_dur` symmetrically — straight through whatever
sentence was being spoken — and only forbade overlap, so touching windows were
fine by it.

With a transcript, a short is now a run of whole SENTENCES:

* candidates start at a sentence start and end at a sentence end (with a
  breath of padding that never reaches into the neighbouring sentence), and
  last between `min_dur` and `max_dur`;
* each is scored for what makes a clip work on its own — speech density,
  energy, a strong opening line (a question or a short declarative that does
  not begin with "and / so / but / um"), a finished last sentence — and
  penalised for dead air and filler words;
* picks may not overlap AND may not touch: at least one sentence separates any
  two shorts, so they are different moments rather than one clip in three
  pieces (the constraint is relaxed only when the source cannot hold N
  separated shorts, and then only to "not overlapping");
* each short carries a `hook`: a complete sentence from inside it (its
  question if it has one), never a slice cut at a word count.

Without a transcript the shot-based heuristic remains, with the same
no-touching rule. The caller tightens each short (fillers, pauses) and applies
the hook — see `agent/prompt/executor._finish_children`.
"""
from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..ingest.probe import probe
from ..ingest.scenes import detect_shots, Shot
from .. import platformutil as _pu

#: Words that carry nothing on their own (removed by `tighten`, penalised here).
FILLERS = frozenset({"um", "uh", "umm", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm"})
#: A sentence opening with one of these depends on the one before it.
_WEAK_START = frozenset({"and", "but", "so", "or", "because", "which", "that", "then",
                         "also", "like", "well", "yeah", "anyway", "plus"} | FILLERS)
#: A gap this long between words is a sentence boundary even without punctuation.
_PAUSE_BREAK_S = 0.8
#: A gap this long inside a short is dead air.
_DEAD_AIR_S = 0.6
_PAD_IN_S = 0.12
_PAD_OUT_S = 0.3
_WORD = re.compile(r"[\w'’]+", re.UNICODE)


@dataclass
class _Sentence:
    start: float
    end: float
    text: str
    words: list[tuple[float, float, str]] = field(default_factory=list)

    @property
    def tokens(self) -> list[str]:
        return [t.lower() for t in _WORD.findall(self.text)]

    @property
    def is_question(self) -> bool:
        return self.text.rstrip().endswith("?")

    @property
    def is_complete(self) -> bool:
        return self.text.rstrip().endswith((".", "?", "!", "…", "।"))

    @property
    def weak_start(self) -> bool:
        toks = self.tokens
        return bool(toks) and toks[0] in _WEAK_START


def _sentences(transcript: dict | None) -> list[_Sentence]:
    """Transcript → sentences, from word timing when there is some (split on
    terminal punctuation and on real pauses), else one per segment."""
    out: list[_Sentence] = []
    for seg in (transcript or {}).get("segments", []) or []:
        words = [(float(w["start"]), float(w["end"]), str(w.get("word", w.get("text", ""))).strip())
                 for w in (seg.get("words") or []) if "start" in w and "end" in w]
        if not words:
            text = str(seg.get("text", "")).strip()
            if text:
                out.append(_Sentence(float(seg["start"]), float(seg["end"]), text))
            continue
        # Punctuated text (whisper's usual output) splits on its punctuation
        # only: a speaker's mid-sentence pause is not a sentence end, and
        # splitting there made "one." a "sentence". Unpunctuated text falls
        # back to real pauses.
        by_pause = not any(w[2].rstrip().endswith((".", "?", "!", "…", "।")) for w in words)
        cur: list[tuple[float, float, str]] = []
        for w in words:
            if by_pause and cur and w[0] - cur[-1][1] >= _PAUSE_BREAK_S:
                out.append(_Sentence(cur[0][0], cur[-1][1], " ".join(x[2] for x in cur), cur))
                cur = []
            cur.append(w)
            if w[2].rstrip().endswith((".", "?", "!", "…", "।")):
                out.append(_Sentence(cur[0][0], cur[-1][1], " ".join(x[2] for x in cur), cur))
                cur = []
        if cur:
            out.append(_Sentence(cur[0][0], cur[-1][1], " ".join(x[2] for x in cur), cur))
    out.sort(key=lambda s: s.start)
    return [s for s in out if s.end > s.start and s.tokens]


def _audio_levels(src: Path, *, n_buckets: int = 400) -> tuple[list[float], float]:
    """Peak level per equal-length bucket across the clip, and the duration."""
    info = probe(src)
    duration = info.duration
    if duration <= 0:
        return [], 0.0
    sr = 8000  # cheap mono extraction
    proc = subprocess.run(
        [_pu.FFMPEG, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        capture_output=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    if proc.returncode != 0:
        return [], duration
    raw = proc.stdout
    n = len(raw) // 2
    if n == 0:
        return [], duration
    import struct
    samples = struct.unpack(f"<{n}h", raw)
    bucket = max(1, n // n_buckets)
    levels = [max(abs(v) for v in samples[i:i + bucket]) / 32768.0
              for i in range(0, n, bucket) if samples[i:i + bucket]]
    return levels, duration


def _energy(levels: list[float], duration: float, a: float, b: float) -> float:
    if not levels or duration <= 0 or b <= a:
        return 0.5
    i = int(a / duration * len(levels))
    j = max(i + 1, int(b / duration * len(levels)))
    chunk = levels[i:j]
    peak = max(levels) or 1.0
    return (sum(chunk) / len(chunk)) / peak if chunk else 0.5


#: Connectives trimmed off the FRONT of an opening line. Not "that"/"which":
#: in "that is the review" they are the subject.
_LEAD_WEAK = FILLERS | {"so", "and", "but", "or", "well", "okay", "ok", "anyway", "plus",
                        "yeah", "like", "also", "then", "now", "right"}
_QUESTION_START = frozenset({"who", "what", "why", "how", "when", "where", "which",
                             "is", "are", "do", "does", "did", "can", "could", "would", "should"})


def _hook_text(s: _Sentence) -> str:
    """The sentence as an opening line: fillers dropped, leading connectives
    ("um, so who is this for?" → "Who is this for?") trimmed, first letter up."""
    toks = [t for t in s.text.split() if t.lower().strip(".,!?…;:") not in FILLERS]
    while len(toks) > 3 and toks[0].lower().strip(".,!?…;:") in _LEAD_WEAK:
        toks = toks[1:]
    text = " ".join(toks).strip().lstrip(",;: ")
    return text[:1].upper() + text[1:] if text else ""


def _hook_line(sents: list[_Sentence]) -> str:
    """A complete sentence from the short to open it with: its first
    question, else its first self-standing statement, else its shortest
    complete sentence — never a slice cut at a word count."""
    cands = [(s, _hook_text(s)) for s in sents]
    fits = [(s, t) for s, t in cands if 3 <= len(_WORD.findall(t)) <= 12]
    for s, t in fits:
        if s.is_question:
            return t
    for s, t in fits:
        if s.is_complete and not s.weak_start:
            return t
    for s, t in fits:
        if s.is_complete:
            return t
    # Nothing short enough: the opening CLAUSE of the first sentence, when it
    # stands on its own ("Um, so who is this for, travel vloggers, …" →
    # "Who is this for?"), else the shortest whole sentence.
    if cands:
        clause = re.split(r"[,;:—]", cands[0][1], maxsplit=1)[0].strip()
        n = len(_WORD.findall(clause))
        if 3 <= n <= 8:
            first = _WORD.findall(clause)[0].lower()
            return clause + ("?" if first in _QUESTION_START and not clause.endswith("?") else "")
    complete = sorted(((s, t) for s, t in cands if s.is_complete and len(_WORD.findall(t)) >= 3),
                      key=lambda st: len(st[1]))
    if complete:
        return complete[0][1]
    return cands[0][1] if cands else ""


def _score_window(sents: list[_Sentence], levels: list[float], duration: float,
                  target: float) -> tuple[float, str, dict]:
    start, end = sents[0].start, sents[-1].end
    span = max(1e-6, end - start)
    tokens = [t for s in sents for t in s.tokens]
    fillers = [t for t in tokens if t in FILLERS]
    # Dead air: gaps between consecutive sentences / words that exceed a breath.
    gaps: list[float] = []
    for a, b in zip(sents, sents[1:]):
        gaps.append(b.start - a.end)
    for s in sents:
        for w1, w2 in zip(s.words, s.words[1:]):
            gaps.append(w2[0] - w1[1])
    dead = sum(g for g in gaps if g >= _DEAD_AIR_S)
    density = min(1.0, (len(tokens) / span) / 3.0)
    energy = _energy(levels, duration, start, end)
    first = sents[0]
    hook = 1.0 if first.is_question else (0.8 if len(first.tokens) <= 14 else 0.55)
    if first.weak_start:
        hook = 0.15
    ending = 1.0 if sents[-1].is_complete else 0.4
    length_fit = 1.0 - min(1.0, abs(span - target) / max(target, 1.0))
    dead_pen = min(1.0, dead / (0.12 * span))
    filler_pen = min(1.0, 8.0 * len(fillers) / max(1, len(tokens)))
    score = (0.28 * density + 0.20 * energy + 0.24 * hook + 0.10 * ending
             + 0.18 * length_fit - 0.30 * dead_pen - 0.25 * filler_pen)
    why = []
    if first.is_question:
        why.append("opens on a question")
    elif not first.weak_start:
        why.append("strong opening line")
    if density > 0.6:
        why.append("dense speech")
    if energy > 0.6:
        why.append("high energy")
    if not fillers and dead < 0.3:
        why.append("clean delivery")
    return score, ", ".join(why) or "balanced", {"fillers": len(fillers), "dead_air": round(dead, 2)}


def _sentence_shorts(sents: list[_Sentence], levels: list[float], duration: float, *,
                     target_count: int, min_dur: float, max_dur: float) -> list[dict]:
    target = (min_dur + max_dur) / 2.0
    cands: list[tuple[float, int, int, str, dict]] = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            span = sents[j].end - sents[i].start
            if span > max_dur:
                break
            if span < min_dur:
                continue
            score, why, stats = _score_window(sents[i:j + 1], levels, duration, target)
            cands.append((score, i, j, why, stats))
    if not cands:
        return []
    cands.sort(key=lambda c: (-c[0], c[1]))

    def clear_of(c, chosen, sep: int) -> bool:
        # `sep` whole sentences must lie between c and every chosen short.
        return all(c[2] < a - sep or c[1] > b + sep for _, a, b, _, _ in chosen)

    chosen: list[tuple[float, int, int, str, dict]] = []
    for sep in (1, 0):          # separated first; merely non-overlapping as a last resort
        for c in cands:
            if len(chosen) >= target_count:
                break
            if c not in chosen and clear_of(c, chosen, sep):
                chosen.append(c)
    out: list[dict] = []
    for score, i, j, why, stats in chosen:
        prev_end = sents[i - 1].end if i > 0 else 0.0
        next_start = sents[j + 1].start if j + 1 < len(sents) else duration or sents[j].end + _PAD_OUT_S
        start = min(sents[i].start, max(prev_end, sents[i].start - _PAD_IN_S, 0.0))
        end = min(next_start, sents[j].end + _PAD_OUT_S)
        if duration > 0:
            end = min(end, duration)
        out.append({"start": round(start, 3), "end": round(end, 3), "score": round(float(score), 3),
                    "why": why, "hook": _hook_line(sents[i:j + 1]),
                    "sentences": j - i + 1, **stats})
    out.sort(key=lambda r: r["start"])
    return out


def _shot_shorts(src: Path, levels: list[float], duration: float, transcript: dict | None, *,
                 target_count: int, min_dur: float, max_dur: float) -> list[dict]:
    """No usable transcript: score shots by energy / length / position, and
    still never return two shorts that touch."""
    shots = detect_shots(src, threshold=0.3) or [Shot(index=0, start=0.0, end=duration)]
    median_shot = statistics.median([s.end - s.start for s in shots if s.end > s.start] or [1.0]) or 1.0
    scored: list[tuple[float, str, Shot]] = []
    for s in shots:
        sd = s.end - s.start
        if sd < 0.5:
            continue
        energy = _energy(levels, duration, s.start, s.end)
        length_fit = 1.0 - abs(sd - median_shot) / max(median_shot, sd)
        rel_pos = (s.start + sd / 2) / duration
        pos_bias = 1.0 - abs(rel_pos - 0.5) * 0.6
        score = 0.6 * energy + 0.25 * length_fit + 0.15 * pos_bias
        scored.append((score, "loud" if energy > 0.5 else "balanced", s))
    scored.sort(key=lambda x: x[0], reverse=True)
    gap = min(2.0, 0.05 * duration)
    picked: list[dict] = []
    for score, why, s in scored:
        start, end = s.start, s.end
        if end - start < min_dur:
            pad = (min_dur - (end - start)) / 2
            start, end = max(0.0, start - pad), min(duration, end + pad)
        if end - start > max_dur:
            mid = (start + end) / 2
            start, end = mid - max_dur / 2, mid + max_dur / 2
        if any(not (end + gap <= a or start >= b + gap) for a, b in
               ((p["start"], p["end"]) for p in picked)):
            continue
        picked.append({"start": float(start), "end": float(end),
                       "score": round(float(score), 3), "why": why, "hook": ""})
        if len(picked) >= target_count:
            break
    picked.sort(key=lambda r: r["start"])
    return picked


def make_shorts(src: Path, transcript: dict | None, cache_dir: Path,
                *, target_count: int = 3, max_dur: float = 60.0,
                min_dur: float = 12.0) -> list[dict]:
    """Pick `target_count` highlight ranges (SOURCE seconds) from a long source.

    Returns `{start, end, score, why, hook, ...}` per short, in source order.
    The caller owns turning them into EDLs / separate sessions.
    """
    levels, duration = _audio_levels(src)
    if duration <= 0:
        duration = probe(src).duration
    if duration < min_dur:
        return [{"start": 0.0, "end": duration, "score": 1.0,
                 "why": "source shorter than min_dur — single highlight", "hook": ""}]
    sents = _sentences(transcript)
    if len(sents) >= 2:
        shorts = _sentence_shorts(sents, levels, duration, target_count=target_count,
                                  min_dur=min_dur, max_dur=max_dur)
        if shorts:
            return shorts
    return _shot_shorts(src, levels, duration, transcript, target_count=target_count,
                        min_dur=min_dur, max_dur=max_dur)
