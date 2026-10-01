"""The reply of an Editor Brain run, written as prose (EB1-F, FX-E wave, UX-13).

`summary.compose_reply` joined the planner's raw notes with spaces. For a
brain plan those notes carry, in one string cut at 400 characters, the hook
quote (already truncated at 60 characters), the title, the resolver's notes
and the deferred list ("not done this time — …" once per item) — and the
reply then printed "Not done this time" a second time from the EDP. This
module keeps the notes worth saying, drops the ones the reply says better
from the EDP (the opening, the deferred list), and writes every part as a
sentence:

    V1 Reel — talking head → 45 s reel: 8 cuts, 2 punch-ins, captions: done.
    13 steps applied, 13 of 13 checks held. Opens on “I now finish 40% more
    of what I plan, the best stretch of my working life.” The video is now
    45.0 s. Not done this time: face-follow reframe pans (next wave; centred
    crop). Undo with ⌘Z.

A run the safety net rolled back is ONE honest sentence and a next step (`rollback_text`), never a question
with options the request has nothing to do with.

A check that only ADVISES (the aesthetic audit: "score is reported, not
gated") is a note beside the verdict, never a failed step in it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: Checks that report and never gate: a failure is a "Note", not a ✗ and not a miss in the headline.
ADVISORY_CHECKS = frozenset({"audit_ok"})
#: The longest hook quote a reply prints; a longer one is cut at a word with an ellipsis.
QUOTE_MAX = 90
_OPENS_ON = re.compile(r"opens on “.*?”;?\s*")
_IDS = re.compile(r"\s*\((?:[a-z]{1,3}_[0-9a-f_]{4,}(?:, )?)+\)")
_TERMINAL = (".", "!", "?", "…")


def cut_quote(quote: str, limit: int = QUOTE_MAX) -> str:
    """The quote whole, or cut at the last word that fits + an ellipsis."""
    q = " ".join(str(quote).split())
    if len(q) <= limit:
        return q
    head = q[:limit + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head[1:] else q[:limit]
    return cut.rstrip(" ,;:-–—") + "…"


def sentence(text: str) -> str:
    """A part as a sentence: a capital first letter and a full stop."""
    t = text.strip()
    if not t:
        return t
    t = t[0].upper() + t[1:] if t[0].islower() else t
    return t if t.endswith(_TERMINAL) else t + "."


def scrub_ids(text: str) -> str:
    """No decision id ("k_0003"), clip id or EDP id in words a person reads."""
    return _IDS.sub("", text)


def result_seconds(session_dir: Path | None) -> float | None:
    """The length of the tree the run left (the live `edl.json`)."""
    if session_dir is None:
        return None
    try:
        body = json.loads((Path(session_dir) / "edl.json").read_text(encoding="utf-8"))
        return float(body["duration"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def planner_notes(reply: str | None, deferred: list[Any]) -> tuple[str | None, list[str]]:
    """(the compiled title, the other notes) from the planner's `reply`
    string, minus what the reply says from the EDP — the opening quote and
    the "not done this time" items — and minus a last note the planner's
    400-character cap cut short."""
    text = reply or ""
    truncated = len(text) >= 400
    text = _OPENS_ON.sub("", text, count=1)
    for d in deferred:
        if isinstance(d, dict) and d.get("asked"):
            text = text.replace(f"not done this time — {d['asked']}: {d.get('why', '')}", "")
    parts = [p.strip() for p in text.split(";") if p.strip() and not p.strip().lower().startswith("not done this time")
             and not p.strip().lower().startswith("the result runs")]      # the reply states the length once, measured
    if truncated and parts:
        parts = parts[:-1]
    title = next((p for p in parts if " → " in p), None)
    return title, [p for p in parts if p is not title]


def without_advisory(verify: dict[str, Any] | None) -> dict[str, Any] | None:
    """The verify payload with the advisory checks taken out and the counts
    recomputed over what GATES (the payload's own counts include them)."""
    if not verify:
        return verify
    checks = list(verify.get("checks") or [])
    if not checks:
        return verify
    kept = [c for c in checks if c.get("check") not in ADVISORY_CHECKS]
    gating = [c for c in kept if c.get("headline", True) and c.get("pass") is not None]
    return {**verify, "checks": kept, "passed": sum(1 for c in gating if c.get("pass")), "total": len(gating)}


def _audit_note(c: dict[str, Any], fmt: Any) -> str:
    m = c.get("measured")
    if isinstance(m, dict) and "hook_score" in m:
        said = f"the aesthetic audit scored {m.get('score', '?')} out of 100 and rated the opening {m['hook_score']} of 3"
        errors = [str(e).replace("_", " ") for e in m.get("errors") or []]
        if errors:
            said += f"; it flagged {', '.join(errors)}"
        return f"Note: {said}. It advises only, so the edit is kept"
    return (f"Note: {c['human']} — measured {fmt(m)}, expected {fmt(c.get('expected'))}. "
            f"It advises only, so the edit is kept")


def advisory_notes(verify: dict[str, Any] | None, fmt: Any) -> list[str]:
    return [_audit_note(c, fmt) for c in (verify or {}).get("checks", [])
            if c.get("check") in ADVISORY_CHECKS and c.get("pass") is False]


def opening_sentence(quote: str | None) -> str | None:
    return f"Opens on “{cut_quote(quote)}”." if quote else None


#: Bookkeeping steps of a brain plan whose "nothing to do" is not news to the person ("the opening moment is
#: already isolated — nothing to split", "reframed 0 clips"): left out of the reply's list of steps that did nothing.
QUIET_WHEN_IDLE = frozenset({"split_at", "reorder_clips", "auto_reframe", "set_clip_fit"})


def worth_saying(steps: list[Any]) -> list[Any]:
    """The steps whose reply line is worth printing: an idle bookkeeping step is not."""
    return [s for s in steps if not (getattr(s, "effect", None) == "none" and getattr(s, "tool", None) in QUIET_WHEN_IDLE)]


#: What a blocking check that failed means, said plainly (never the check's name, a source second or a seam).
_ROLLBACK_WHY = {
    "no_cut_mid_word": "one of its cuts would have landed inside a spoken word",
    "dialogue_in_sync": "the dialogue would have drifted out of step with the picture",
    "one_moment_once": "the second camera would have played the same stretch again",
    "removal_within_plan": "it would have removed more of the picture than the plan says",
    "duration_shrank": "the video did not get shorter",
}


def rollback_text(reasons: list[dict[str, Any]], *, applied: bool, reel: bool, asked_s: float | None = None) -> str:
    """The reply when the safety net rolled a brain plan back: what did not hold in plain words, that nothing
    changed, and a concrete next step. `applied` is false for the dry run before the card is offered (nothing
    was ever applied) and true after the person said yes."""
    why = list(dict.fromkeys(_ROLLBACK_WHY.get(str(r.get("clause")), "") or
                             (str(r["message"]).rstrip(" .") if r.get("kind") == "unasked" and r.get("message")
                              else "one of its safety checks did not hold") for r in reasons))[:2]
    reason = " and ".join(why) or "one of its safety checks did not hold"
    if reel:
        length = f"{asked_s:g}-second " if asked_s else ""
        lead = (f"I could not cut a clean {length}reel from this recording: {reason}." if not applied
                else f"The reel was rolled back: {reason}.")
        nxt = "Try another length, for example “make a 30-second reel”, or say which part you want."
    else:
        lead = (f"I could not make that edit cleanly: {reason}." if not applied
                else f"The edit was rolled back: {reason}.")
        nxt = "Try a smaller request, for example “remove the silences”, or edit that part by hand."
    return f"{lead} Nothing was changed. {nxt}"


def length_sentence(seconds: float | None) -> str | None:
    return f"The video is now {seconds:.1f} s." if seconds else None


def compose(verify: dict[str, Any] | None, title: str, ran: int, failed: list[str], unmeasured: list[str],
            steps: list[str], plan: Any, bits: dict[str, Any], fmt: Any) -> list[str]:
    """The parts of a brain run's reply, each one sentence. `verify` is the
    payload the run produced; `failed` are the ✗ lines of its GATING checks."""
    plan_title, notes = planner_notes(getattr(plan, "reply", None), bits.get("deferred_items") or [])
    gate = without_advisory(verify)
    name = f"{bits['version']} — {plan_title}" if bits.get("version") and plan_title else \
        (bits.get("version") and f"{bits['version']} — {title}") or plan_title or title
    n = f"{ran} step{'' if ran == 1 else 's'} applied"
    if gate and gate.get("total"):
        verdict = "Done with issues" if failed else "Done"
        outcome = f"{verdict} — {n}, {gate['passed']} of {gate['total']} check{'' if gate['total'] == 1 else 's'} held."
    else:
        outcome = f"Done — {n}." if not failed else f"Done with issues — {n}."
    parts = [name, outcome, *failed, *unmeasured, *advisory_notes(verify, fmt), *steps, *notes]
    parts.extend(x for x in (opening_sentence(bits.get("hook_quote")), length_sentence(bits.get("length_s")),
                             bits.get("deferred")) if x)
    return [sentence(scrub_ids(p)) for p in parts]
