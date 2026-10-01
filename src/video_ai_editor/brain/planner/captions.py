"""Caption intelligence, this wave (spec §4.7 modes without highlight or
speaker colours): Dynamic (`ig_chunky`) for reels, Podcast (`default`) for
episodes, off when the control says so. One `captions` decision with
`reason.code: caption_mode`; the compiler turns it into the literal
`add_caption_track` + `set_caption_style` pair.

The decision also carries the CUES, laid from the graph's own words on the
reference clock (review UX-03, UX-09): the words the plan KEEPS, every
speaker's, each captioned once, a cue starting with its first word and
ending with its last (never past a seam, never over a word that is cut,
never a removed filler, never a backchannel — the "mm-hm" that gets no
camera gets no caption — and never a word under no speaker turn). They are cut at every seam, sentence end, change of
speaker and real pause, so a cue is what one shot says. At run time
(`brain/caption_lane.py`) each cue is mapped through the live v1 layout by
source — the same second of whichever angle shows it — so the guest is
captioned when the picture is on the guest's camera. Reference seconds are
the decision's clock, like every other layer of the graph.
"""
from __future__ import annotations

import re
from typing import Any

from .. import energy as E
from .. import reasons as R
from . import select
from .graph_view import Graph
from .types import Ctx, decision, r4

MODES: dict[str, dict] = {
    "viral": {"style": "word_emphasis", "position": "center", "look": {"size": 96, "upper": True, "stroke_w": 8, "shadow_on": True}},
    "dynamic": {"style": "ig_chunky", "position": "bottom", "look": {"size": 72, "upper": True, "stroke_w": 6}},
    "podcast": {"style": "default", "position": "bottom", "look": {"size": 56, "background": "#000000AA", "upper": False},
                "max_chars": 32},
    "minimal": {"style": "default", "position": "bottom", "look": {"size": 48, "stroke_w": 2}},
}

#: (characters a line holds, lines a cue may take, longest a cue stays up, s) per mode.
BUDGETS: dict[str, tuple[int, int, float]] = {
    "viral": (16, 2, 3.0), "dynamic": (16, 2, 3.0), "podcast": (32, 2, 6.0), "minimal": (42, 2, 6.0),
}
#: A pause this long inside kept speech ends a cue; a cue shows at least this long
#: (extended only inside its own shot and never over the next cue).
GAP_BREAK_S = 0.45
MIN_CUE_S = 0.4
SENTENCE_END = (".", "?", "!", "…", "।")
CLAUSE_END = (",", ";", ":", "—")


def mode_for(ctx: Ctx) -> str | None:
    control = str(ctx.controls.get("captions") or "auto")
    if control == "off":
        return None
    if control in MODES:
        return control
    if ctx.reel:
        mode = str(ctx.energy["caption_mode_reel"])
        return "dynamic" if mode == "viral" else mode     # per-word highlight (Viral) is EB2
    return "podcast"


# ---------------------------------------------------------------- what plays

def kept_spans(g: Graph, ctx: Ctx) -> list[tuple[float, float]]:
    """The spans of the reference clock the programme keeps, each one shot
    of the finished cut (a reel's `pieces`, split where the hook moves; an
    episode's whole length minus the removals)."""
    if ctx.reel and ctx.state.get("kept"):
        return sorted(select.pieces(ctx))
    out: list[tuple[float, float]] = []
    cur = 0.0
    for c0, c1 in sorted((c["t0"], c["t1"]) for c in ctx.state.get("cuts") or []):
        if c0 > cur:
            out.append((cur, c0))
        cur = max(cur, c1)
    if cur < g.ref_end:
        out.append((cur, g.ref_end))
    return out


#: A word this close to a turn's edge still belongs to it (turns are cut from the sound, words are timed by the ASR).
TURN_MARGIN_S = 0.15


def unread(g: Graph):
    """`word -> True` for a word nobody reads a caption for: what does not
    get the camera (camera rule 1 — a turn under `BACKCHANNEL_MAX_S`, or
    nothing but "mm-hm", "yeah", "right") does not get one either, and — in a
    CONVERSATION — a word under NO turn at all is sound the speaker analysis
    could not place. Both are the words ASR mishears most ("Right." → "body",
    "Mm-hm." → "An image, em."), and a mishearing on screen is worse than a
    short answer left uncaptioned. A word two people speak over keeps its
    caption (a real turn is under it). With no turns in the graph nothing is
    skipped, and ONE speaker's word between two of their turns is theirs: the
    rule exists for a podcast's mishearings, and on a talking head it dropped
    spoken, kept words ('write', 'this small work?' — review UX-09)."""
    turns = [(float(t["t0"]), float(t["t1"]),
              float(t["t1"]) - float(t["t0"]) < E.BACKCHANNEL_MAX_S or g.is_backchannel(t)) for t in g.turns]
    conversation = len({t.get("spk") for t in g.turns}) > 1

    def skip(w: dict) -> bool:
        if not turns:
            return False
        mid = 0.5 * (float(w["t0"]) + float(w["t1"]))
        under = [bc for t0, t1, bc in turns if t0 - TURN_MARGIN_S <= mid <= t1 + TURN_MARGIN_S]
        if not under:
            return conversation
        return all(under)
    return skip


#: A speaker's (or a sentence's) FIRST word this far before the word after it, in a short word, was timed early by the recogniser
#: (whisper: 'Salt' 0.5 s and 'Let's' 1.8 s before the speaker starts): it rides with the words it belongs to.
EARLY_GAP_S = 0.3
EARLY_WORD_MAX_S = 0.6
EARLY_WORD_KEEP_S = 0.4


def _retimed(g: Graph, skip: Any) -> list[dict]:
    """The graph's words as CAPTION times: a copy per word with `_skip` set for a word nobody reads, and a
    turn's first word timed before its speaker starts moved up against the same speaker's next word — clamped to
    the start of the turn that word is in, so a cue never opens before its speaker does and never stands alone."""
    words = [{**w, "_skip": bool(skip(w))} for w in g.words]
    by_speaker: dict[Any, list[int]] = {}
    for i, w in enumerate(words):
        by_speaker.setdefault(w.get("spk"), []).append(i)
    for idx in by_speaker.values():
        for k, i in enumerate(idx[:-1]):
            w, nxt = words[i], words[idx[k + 1]]
            text = str(w.get("text", "")).strip()
            before = words[idx[k - 1]] if k else None
            turn_first = before is None or float(w["t0"]) - float(before["t1"]) > 0.9
            # …or the first word of its sentence (a graph that labels one speaker for a conversation has no turn edge
            # to say so: 'Salt' 95.9 s, the sentence's first word, 'gets' 97.1 s)
            opens_sentence = before is not None and (str(before.get("text", "")).strip().endswith(SENTENCE_END)
                                                     or before.get("sent") != w.get("sent"))
            if not ((turn_first or opens_sentence) and not w.get("filler") and not text.endswith(SENTENCE_END)
                    and float(w["t1"]) - float(w["t0"]) < EARLY_WORD_MAX_S
                    and float(nxt["t0"]) - float(w["t1"]) > EARLY_GAP_S):
                continue
            dur = min(max(float(w["t1"]) - float(w["t0"]), 0.1), EARLY_WORD_KEEP_S)
            t1 = float(nxt["t0"])
            turn = g.turn_at(0.5 * (float(nxt["t0"]) + float(nxt["t1"])))
            t0 = max(t1 - dur, float(turn["t0"]) if turn is not None and turn.get("spk") == w.get("spk") else 0.0)
            w["t0"], w["t1"] = r4(min(t0, t1 - 0.05)), r4(t1)
    return sorted(words, key=lambda w: (float(w["t0"]), float(w["t1"])))


def _words_in(words: list[dict], a: float, b: float) -> list[dict]:
    """The words a viewer hears in `[a, b]`: their midpoint inside it, a
    filler ("um") never — it is not a word to read — and never an unread one."""
    return [w for w in words if not w.get("filler") and str(w.get("text", "")).strip() and not w["_skip"]
            and a - 1e-6 <= 0.5 * (float(w["t0"]) + float(w["t1"])) <= b + 1e-6]


def _wrap(text: str, max_chars: int, max_lines: int) -> str:
    """Balanced two-line wrap when the cue is longer than a line."""
    if len(text) <= max_chars or max_lines < 2:
        return text
    words = text.split(" ")
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))), default=0)
    return "\n".join((" ".join(words[:best]), " ".join(words[best:]))) if best else text


def _chars(ws: list[dict]) -> int:
    return len(" ".join(str(w["text"]).strip() for w in ws))


def _sentences(words: list[dict]) -> list[list[dict]]:
    """One run per sentence of ONE speaker: cut at a change of speaker, a
    sentence end and a real pause — the boundaries the speech itself offers."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (cur[-1].get("spk") != w.get("spk") or str(cur[-1]["text"]).strip().endswith(SENTENCE_END)
                    or float(w["t0"]) - float(cur[-1]["t1"]) >= GAP_BREAK_S):
            out.append(cur)
            cur = []
        cur.append(w)
    return out + ([cur] if cur else [])


def _fits(run: list[dict], chars: int, lines: int, max_dur: float) -> bool:
    """One cue holds `run` when its balanced wrap keeps every line within
    `chars` (and within `lines` lines) and it does not stay up too long."""
    text = " ".join(str(w["text"]).strip() for w in run)
    rows = _wrap(text, chars, lines).split("\n")
    return len(rows) <= lines and max(len(r) for r in rows) <= chars and float(run[-1]["t1"]) - float(run[0]["t0"]) <= max_dur


def _split(run: list[dict], chars: int, lines: int, max_dur: float) -> list[list[dict]]:
    """A run that does not fit one cue is halved where it reads best — near
    the middle, preferably after a comma or in the longest pause — and each
    half again until it fits (never a lone word left dangling by a budget)."""
    if len(run) < 2 or _fits(run, chars, lines, max_dur):
        return [run]
    total = _chars(run)

    def cost(i: int) -> float:
        gap = float(run[i]["t0"]) - float(run[i - 1]["t1"])
        comma = 0.25 if str(run[i - 1]["text"]).strip().endswith(CLAUSE_END) else 0.0
        return abs(_chars(run[:i]) - _chars(run[i:])) / max(1, total) - comma - min(gap, 0.5)
    at = min(range(1, len(run)), key=cost)
    return _split(run[:at], chars, lines, max_dur) + _split(run[at:], chars, lines, max_dur)


def _groups(words: list[dict], chars: int, lines: int, max_dur: float) -> list[list[dict]]:
    return [half for run in _sentences(words) for half in _split(run, chars, lines, max_dur)]


_LONE_I = re.compile(r"\bi\b(?=$|[\s,.;:!?'’])")


def _polish(g: Graph, group: list[dict], text: str) -> str:
    """A cue reads the same whoever speaks: a cue that opens a sentence starts with a capital and a cue that ends
    one carries its full stop (or question mark), where the recogniser wrote neither — the guest's lines came
    out in lower case with no stops beside the host's punctuated ones (review UX-03). A cue that stops in the
    middle of a sentence (a budget split, a cut) is left as it is."""
    sent = g.sentence(str(group[0].get("sent") or "")) or {}
    own = [w for w in g.words_of(str(sent.get("id") or "")) if not w.get("filler")]
    if own and own[0].get("id") == group[0].get("id") and text[:1].islower():
        text = text[0].upper() + text[1:]
    last = g.sentence(str(group[-1].get("sent") or "")) or {}
    tail = [w for w in g.words_of(str(last.get("id") or "")) if not w.get("filler")]
    if tail and tail[-1].get("id") == group[-1].get("id") and not text.rstrip().endswith(SENTENCE_END + CLAUSE_END):
        text = text.rstrip() + ("?" if last.get("is_question") else ".")
    return _LONE_I.sub("I", text)


def _cue(g: Graph, group: list[dict], a: float, b: float, chars: int, lines: int) -> dict | None:
    t0, t1 = max(a, float(group[0]["t0"])), min(b, float(group[-1]["t1"]))
    if t1 - t0 <= 1e-3:
        return None
    text = _polish(g, group, " ".join(w["text"].strip() for w in group))
    return {"t0": r4(t0), "t1": r4(t1), "text": _wrap(text, chars, lines), "spk": str(group[0].get("spk") or "")}


def _settle(cues: list[dict], a: float, b: float) -> list[dict]:
    """Timing polish inside one shot: a cue shorter than `MIN_CUE_S` stays up
    a little longer, but never past the shot, never over the next cue; two
    speakers talking over each other never stack (the earlier gives way)."""
    out = [dict(c) for c in cues]
    for i, c in enumerate(out):
        limit = out[i + 1]["t0"] if i + 1 < len(out) else b
        if c["t1"] - c["t0"] < MIN_CUE_S:
            c["t1"] = r4(min(c["t0"] + MIN_CUE_S, max(c["t1"], limit), b))
        c["t1"] = r4(min(c["t1"], max(limit, c["t0"] + 0.05)))
    return [c for c in out if c["t1"] - c["t0"] > 1e-3]


def build_cues(g: Graph, ctx: Ctx, mode: str) -> list[dict]:
    """The cues for the kept words: `{t0, t1, text, spk}` in reference seconds."""
    chars, lines, max_dur = BUDGETS[mode]
    cues: list[dict] = []
    words = _retimed(g, unread(g))
    for a, b in kept_spans(g, ctx):
        raw = [c for grp in _groups(_words_in(words, a, b), chars, lines, max_dur) if (c := _cue(g, grp, a, b, chars, lines))]
        cues += _settle(sorted(raw, key=lambda c: c["t0"]), a, b)
    return sorted(cues, key=lambda c: (c["t0"], c["t1"]))


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    mode = mode_for(ctx)
    if mode is None:
        ctx.state["captions"] = None
        return decisions
    if ctx.reel and ctx.energy["caption_mode_reel"] == "viral":
        ctx.defer("per-word caption highlight", "arrives next wave; Dynamic captions instead")
    spec = MODES[mode]
    ctx.state["captions"] = {"mode": mode, "style": spec["style"], "position": spec["position"]}
    cues = build_cues(g, ctx, mode)
    decisions.append(decision(
        "captions", params={"mode": mode, "style": spec["style"], "position": spec["position"], "look": dict(spec["look"]),
                            **({"max_chars": spec["max_chars"]} if "max_chars" in spec else {}),
                            "src": ctx.primary, "cues": cues},
        reason=R.reason("caption_mode", [], mode=mode.capitalize(), content_type=ctx.project_type.replace("_", " "),
                        energy=ctx.level)))
    return decisions


__all__ = ["run", "mode_for", "MODES", "BUDGETS", "kept_spans", "build_cues", "unread"]
