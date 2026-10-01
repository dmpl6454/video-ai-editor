"""The closed reason-code vocabulary and its human templates (spec §5.2).

One template per code, used by the planner's decisions, the compiled steps'
`why`, the card, the Inspector, the run log and the reply, so they all say
the same thing. `reason(code, facts, **fields)` renders the template and
returns the frozen `{code, facts, text}` shape. `pause_kept:{why}` is the
one parametric code.

A reason is an EDITOR'S reason (review UX-05): it names the moment with a
timecode ("0:24", the position in the ORIGINAL recording, because a removed
umm no longer has a place in the edit), the person by name ("the guest"),
and the line by a quote; it never carries a graph key, a file name, a
fallback topic, or a bare number (a σ, an arousal, a 0.82 confidence).
The numbers stay in the decision's `params` and `score`, where the Inspector
can show them behind a "details" affordance.

Enrichment: most passes hand `reason()` the graph ids that justify a
decision (`facts`) and a few fields; the words a good reason needs — the
speaker's name, the moment's timecode, the quote — are read from the graph
the planner is running on (`bind()`, called by the first pass), so no pass
has to know them. With no graph bound (a bare call) the text is built from
the fields alone and leaves the phrase out rather than inventing it.

The codes this wave emits are the EB1 brief's list; `brain/schema.py` (lane
C) validates them on write. The rest of §5.2 is kept here so a later wave
adds a pass, not a vocabulary.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

TEMPLATES: dict[str, str] = {
    "silence": "Cut a {dur} silence{at_tc}{spared_note}",
    "pause_kept": "Kept {kept} of the pause {where}{at_tc} — {why}",
    "filler": "Removed {an_word}{at_tc}",
    "soft_filler": "Trimmed {an_word}{at_tc}, said softly",
    "filler_acoustic": "Removed an “uh” the transcript missed{at_tc}",
    "dialogue_lane": "{dialogue_from}; {fades}",
    "jump_cut_hide": "{hide_verb}{at_tc} so the jump does not show",
    "music_intro": "{Mood} music under the opening, out at the first sentence",
    "music_outro": "{Mood} music under the last 8 s",
    "music_sting": "A sting at the chapter “{topic}”",
    "question_kept": "Kept the question: {why}",
    "false_start": "Removed the false start “{text}”{at_tc}; the line restarts as “{kept}”",
    "repeat": "Removed the earlier take of a repeated line{at_tc}; the retake{tc_of_at} stays",
    "dead_air": "Cut {dur} of dead air in {whose} answer{at_tc}{spared_note}",
    "weak_question": "Dropped a question that adds nothing the answer does not say",
    "dead_conversation": "{topic}: {dur} with little content",
    "technical": "{why}{at_tc}",
    "duration_fit": "Trimmed to {kept} to fit the {asked} you asked for, keeping only whole sentences",
    "best_window": "Kept {kept} of whole sentences{from_tc} as the strongest stretch{about}{retake_note}",
    "hook_strongest_opening": "Opens on “{quote}”{said_at}: the strongest line that stands on its own{why_axes}",
    "cold_open": "The quotable line moved to the front; the original order resumes after it",
    "speaker_turn": "Cut to {who}, who starts speaking{at_tc}",
    "question_shown": "Showed a question even though it is short",
    "reset_wide": "Wide shot after {dur} on one angle, at a sentence boundary",
    "reaction": "Cut to {listener} reacting ({what})",
    "overlap_wide": "Both people speak, so the wide shot holds",
    "at_cut": "Cut to {who}{at_tc}, right on the jump cut, so one edit does both jobs",
    "energy_motion_fallback": "No one is speaking here, so the picture follows the sound and movement",
    "emphasis_peak": "Punched in on “{quote}”{line_from}: {delivery}",
    "question_punch_out": "Pulled back on the question",
    "hook_emphasis": "Punched in on the opening line, “{quote}”{line_from}, to grab attention",
    "subject_follow": "Follows the speaker's face",
    "subject_moved_median": "The speaker moves too fast to follow, so the frame stays on their average position",
    "caption_mode": "Added {mode} captions: {mode_blurb}",
    "lower_third_intro": "{name}'s name card as they first appear",
    "music_mood": "Added a quiet {mood} music bed that ducks under speech",
    "beat_grid": "Cut on the beat{at_tc}",
    "broll_reference": "“{phrase}” for {dur} on one angle; {candidate} matches",
    "control": "{control_text}",
    "review_fix": "Fixed after review: {issue}",
    "user_named": "You asked: “{clause}”",
    "protected_skip": "Left alone: you edited this after V{n}",
}

#: The why of a `pause_kept:{why}` code → (where the pause is, why it stays).
PAUSE_WHY: dict[str, tuple[str, str]] = {
    "laughter": ("after the laugh", "it lets the laugh land"),
    "hard_question": ("before the answer to a hard question", "the beat before a hard answer lands it"),
    "emotion": ("after an emotional line", "it lets the line land"),
    "conclusion": ("after the conclusion", "it lets the point land"),
}

#: Codes a decision of this wave may carry (the brief's list + the pause whys).
WAVE_CODES: frozenset[str] = frozenset({
    "silence", "filler", "filler_acoustic", "false_start", "dead_air", "best_window", "duration_fit",
    "hook_strongest_opening", "speaker_turn", "at_cut", "jump_cut_hide", "emphasis_peak", "hook_emphasis",
    "caption_mode", "music_mood", "dialogue_lane", "control",
}) | frozenset(f"pause_kept:{w}" for w in PAUSE_WHY)

_MODE_BLURB = {
    "dynamic": "short bold phrases that read fast",
    "viral": "one or two big words at a time",
    "podcast": "clean readable lines, one person at a time",
    "minimal": "small, quiet lines",
}
_PLATFORM_NAME = {
    "reels": "Instagram Reels", "tiktok": "TikTok", "shorts": "YouTube Shorts", "story": "Stories",
    "youtube_16x9": "YouTube", "youtube_4k": "YouTube 4K", "ig_feed_1x1": "the Instagram feed (square)",
    "ig_feed_4x5": "the Instagram feed (4:5)",
}
_AXIS_WORD = {
    "curiosity": "intriguing", "surprise": "surprising", "emotion": "emotional", "authority": "confident",
    "conflict": "contrarian", "value": "useful", "question": "a question", "strong_statement": "bold",
    "visual_impact": "striking",
}
_RATIO_WORD = {"9:16": "vertical 9:16", "16:9": "widescreen 16:9", "1:1": "square", "4:5": "4:5 portrait"}
_ROLE_WORD = {"host": "the host", "guest": "the guest"}


def base_code(code: str) -> str:
    return code.split(":", 1)[0]


def tc(t: float, fps: Any = None) -> str:
    """A position in the original recording the way an editor says it:
    `0:24`, `1:22`, `1:02:03` (whole seconds, rounded down like a player)."""
    s = int(max(0.0, float(t)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def dur(seconds: float) -> str:
    s = max(0.0, float(seconds))
    return f"{s:.1f} s" if s < 10 else f"{s:.0f} s"


# ---------------------------------------------------------------- the graph the planner is running on

_GRAPH: ContextVar[Any] = ContextVar("brain_reasons_graph", default=None)


def bind(graph: Any) -> None:
    """Name the graph the current plan runs on (the first pass calls this),
    so a reason can look up a speaker, a timecode or a quote by graph id."""
    _GRAPH.set(graph)


def _turn(g: Any, tid: str) -> dict | None:
    return next((t for t in getattr(g, "turns", []) or [] if str(t.get("id")) == tid), None)


def _speaker(g: Any, sid: str) -> dict | None:
    return next((s for s in getattr(g, "speakers", []) or [] if str(s.get("id")) == sid), None)


def _speaker_name(sp: dict | None, fallback: str = "") -> str:
    """As the camera pass names one: the given name, else the role, else the id."""
    if sp is None:
        return fallback
    role = sp.get("role_guess")
    return str(sp.get("name") or ("Host" if role == "host" else "Guest" if role == "guest" else sp.get("id") or fallback))


def _who(name: str) -> str:
    """"Guest" → "the guest"; a real name stays; "S2" → "speaker 2"."""
    n = str(name or "").strip()
    if not n:
        return "the speaker"
    if n.lower() in _ROLE_WORD:
        return _ROLE_WORD[n.lower()]
    m = re.fullmatch(r"S(\d+)", n)
    return f"speaker {m.group(1)}" if m else n


def _whose(name: str) -> str:
    who = _who(name)
    return who + ("'" if who.endswith("s") else "'s")


def _quote(text: str, limit: int = 64) -> str:
    words = str(text or "").replace("“", "").replace("”", "").split()
    out = ""
    for i, w in enumerate(words):
        if len(out) + len(w) + 1 > limit and i:
            return out.rstrip(",;:—-.") + "…"
        out = f"{out} {w}".strip()
    return out.rstrip(",;:.")


def _an(word: str) -> str:
    w = word.strip()
    return f"{'an' if w[:1].lower() in 'aeiou' else 'a'} “{w}”"


def _first(facts: list[str], pred: Any) -> Any:
    for f in facts:
        hit = pred(f)
        if hit is not None:
            return hit
    return None


def _from_graph(base: str, facts: list[str], f: dict[str, Any]) -> dict[str, Any]:
    """The fields the graph can supply that the pass did not."""
    g = _GRAPH.get()
    if g is None:
        return {}
    out: dict[str, Any] = {}
    sent = _first(facts, lambda x: g.sentence(x) if hasattr(g, "sentence") else None)
    turn = _first(facts, lambda x: _turn(g, x))
    spk = _first(facts, lambda x: _speaker(g, x))
    if turn is not None and "tc" not in f:
        out["tc"] = tc(float(turn["t0"]))
    if base in ("speaker_turn", "at_cut"):
        out["speaker"] = f.get("speaker") or _speaker_name(spk or (_speaker(g, str(turn.get("spk"))) if turn else None))
    if sent is not None:
        out["quote"] = _quote(g.spoken(sent) if hasattr(g, "spoken") else sent.get("text", ""))
        if base in ("hook_strongest_opening", "hook_emphasis", "emphasis_peak") and "tc" not in f:
            out["tc"] = tc(float(sent["t0"]))
    if base == "repeat" and "tc" not in f:
        dup = _first(facts, lambda x: g.sentence(x) if str(x).startswith("s_") and hasattr(g, "sentence") else None)
        if dup is not None:
            out["tc"] = tc(float(dup["t0"]))
    if base == "dialogue_lane":
        src = g.source(facts[0]) if facts and hasattr(g, "source") else None
        out["role"] = (src or {}).get("role", "")
    return out


# ---------------------------------------------------------------- rendering

class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _delivery(f: dict[str, Any]) -> str:
    try:
        louder, slower = float(f.get("rms_z") or 0.0) >= 1.0, float(f.get("stretch") or 0.0) >= 1.1
    except (TypeError, ValueError):
        louder = slower = False
    parts = [w for w, on in (("louder", louder), ("slower", slower)) if on]
    return "said " + " and ".join(parts) + " than the rest" if parts else "the speaker leans on it"


def _count(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _hide_verb(f: dict[str, Any]) -> str:
    """What the hide did, from what the decision holds: the scale it left and the scale it holds. With
    either missing (an older plan) it says the less exact thing."""
    try:
        was, now = float(f["from_scale"]), float(f["scale"])
    except (KeyError, TypeError, ValueError):
        return "Nudged the zoom at the cut"
    return f"Stepped the zoom from {was:.0%} to {now:.0%}"


def _control_text(f: dict[str, Any]) -> str:
    control, value = str(f.get("control") or ""), str(f.get("value") or "")
    key = value.lower().replace(" ", "_")
    if control == "Reframe":
        where = _PLATFORM_NAME.get(str(f.get("platform") or key), "")
        return f"Cropped to {_RATIO_WORD.get(value, value)}{' for ' + where if where else ''}, filling the frame with no black bars"
    if control == "Platform":
        name = _PLATFORM_NAME.get(key, value)
        return f"Exported with the {name} preset: the right size, bitrate and loudness for it"
    return f"{control}: {value}" if control and value else control or value or "A setting you chose"


def _derive(base: str, code: str, f: dict[str, Any]) -> dict[str, Any]:
    at = str(f.get("tc") or "")
    d: dict[str, Any] = {**f}
    d["at_tc"] = f" at {at}" if at else ""
    d["from_tc"] = f" from {at}" if at else ""
    d["said_at"] = f" (said at {at})" if at else ""
    d["line_from"] = f" (from {at})" if at else ""
    d["tc_of_at"] = f" at {f['tc_of']}" if f.get("tc_of") else ""
    d["an_word"] = _an(str(f.get("word") or "um").lower())
    speaker = str(f.get("speaker") or "")
    d["who"] = _who(speaker)
    d["whose"] = _whose(speaker)
    d["about"] = f" about {f['topic']}" if f.get("topic") else ""
    d["retake_note"] = f"; the retake stays, the earlier take of that line (first said at {f['retake_of']}) is dropped" if f.get("retake_of") else ""
    d["mode_blurb"] = _MODE_BLURB.get(str(f.get("mode") or "").lower(), "easy to read")
    d["mode"] = str(f.get("mode") or "").capitalize()
    d["delivery"] = _delivery(f)
    d["spared_note"] = "; left the speech the transcript missed in place" if _count(f.get("spared")) else ""
    d["hide_verb"] = _hide_verb(f)
    d["control_text"] = _control_text(f)
    axes = [_AXIS_WORD[a] for a in re.split(r"\s*\+\s*", str(f.get("axes") or "")) if a in _AXIS_WORD][:2]
    d["why_axes"] = " — " + " and ".join(axes) if axes else ""
    n = int(f.get("n") or 0)
    d["fades"] = f"{n} cut{'s' if n != 1 else ''} get{'' if n != 1 else 's'} a 5 ms fade so none clicks" if n else "every cut gets a 5 ms fade so none clicks"
    d["dialogue_from"] = ("Dialogue now plays from the recorder on its own audio lane, the camera microphones muted"
                          if f.get("role") == "reference_audio"
                          else "Dialogue moved onto its own audio lane, the picture's own sound muted")
    if base == "technical":
        d["why"] = str(f.get("why") or "A technical problem")
    if base == "pause_kept":
        why = code.split(":", 1)[1] if ":" in code else str(f.get("why") or "")
        d["where"], d["why"] = PAUSE_WHY.get(why, (why, "it lets the point land"))
    d["mood"] = str(f.get("mood") or "").lower()
    d["Mood"] = d["mood"].capitalize()
    return d


def render(code: str, facts: list[str] | tuple[str, ...] = (), **fields: Any) -> str:
    base = base_code(code)
    tmpl = TEMPLATES[base]
    f = {**_from_graph(base, [str(x) for x in facts], fields), **fields}
    text = tmpl.format_map(_Blank(_derive(base, code, f)))
    return re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()


def reason(code: str, facts: list[str] | tuple[str, ...], **fields: Any) -> dict[str, Any]:
    """The frozen `{code, facts, text}`; `facts` are ids of real graph nodes."""
    if base_code(code) not in TEMPLATES:
        raise KeyError(f"unknown reason code {code!r}")
    return {"code": code, "facts": list(dict.fromkeys(str(f) for f in facts)), "text": render(code, facts, **fields)[:200]}


__all__ = ["TEMPLATES", "PAUSE_WHY", "WAVE_CODES", "base_code", "bind", "tc", "dur", "render", "reason"]
