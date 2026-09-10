"""The honest summary at the end of a prompt run (spec §4.5).

`compose_reply` is the ONLY text most users read, and on the phone it is the
only thing they see besides the ops — so it leads with what did not hold
(measured vs expected, and a next step), then names the steps that ran with
no effect ("remove_fillers: nothing to cut"), the sessions a shorts run
created, and how to undo. It always starts with `via <Brain> — ` (and
`· text by <content brain>` when a different brain wrote the hook) because
`mobile/lib/sse.ts` drops `brain` events and the prefix is the phone's only
way to know which brain answered (§4.1).
"""
from __future__ import annotations

from typing import Any

from .brains.base import BRAIN_LABELS
from .schema import Plan
from .service import via

#: What to try when a check fails. Keyed by check name; the default asks for
#: the render because "verify the export" is the honest general answer.
_NEXT_STEP: dict[str, str] = {
    "captions_cover": "run 'accurate captions' to re-transcribe, or check the transcript",
    "captions_within_extent": "re-run captions after the cuts",
    "captions_language": "say 'translate captions to <lang>'",
    "fillers_remaining_leq": "quote the words to remove, e.g. remove \"like\"",
    "duration_shrank": "there may have been nothing to cut — check the transcript",
    "silence_total_leq": "lower the silence threshold or raise the minimum pause",
    "no_letterbox": "say 'fill the frame' to crop every clip to the canvas",
    "overlays_inside_safe_zone": "move the text up — the platform UI covers that area",
    "music_covers": "add the music again with loop on, or pick a longer bed",
    "music_ducked": "say 'duck the music more'",
    "hook_text_starts_leq": "say 'add a hook' to place one at the start",
    "loudness_within": "export and measure — the loudness target applies at export",
    "audit_ok": "run the audit for the exact issues",
    "shorts_created": "try a different count or length",
    "shorts_finished": "open each short and finish it by hand",
}


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def _prefix(plan: Plan) -> str:
    label = BRAIN_LABELS.get(plan.brain, plan.brain)
    if plan.content_brain and plan.content_brain != plan.brain:
        label = f"{label} · text by {BRAIN_LABELS.get(plan.content_brain, plan.content_brain)}"
    return via(label)


def _failed_lines(verify_result: dict[str, Any] | None) -> list[str]:
    if not verify_result:
        return []
    lines: list[str] = []
    for c in verify_result.get("checks", []):
        if c.get("pass") is not False:
            continue
        unit = f" {c['unit']}" if c.get("unit") else ""
        line = f"✗ {c['human']}: measured {_fmt(c.get('measured'))}{unit}, expected {_fmt(c.get('expected'))}"
        if c.get("detail"):
            line += f" ({c['detail']})"
        nxt = _NEXT_STEP.get(str(c.get("check")))
        if nxt:
            line += f" — next: {nxt}"
        lines.append(line)
    return lines


def _unmeasured_lines(verify_result: dict[str, Any] | None) -> list[str]:
    if not verify_result:
        return []
    return [f"? {c['human']}: not measured" + (f" ({c['detail']})" if c.get("detail") else "")
            for c in verify_result.get("checks", []) if c.get("pass") is None]


def compose_reply(plan: Plan, exec_result: Any, verify_result: dict[str, Any] | None) -> str:
    """The end-of-run text. `exec_result` is an `executor.ExecResult`;
    `verify_result` the `verify` event payload (or None when nothing ran)."""
    head = _prefix(plan)
    if exec_result.error:
        return head + exec_result.error

    steps = list(exec_result.steps)
    ran = [s for s in steps if s.status == "ok" and s.effect != "none"]
    none = [s for s in steps if s.status == "ok" and s.effect == "none"]
    skipped = [s for s in steps if s.status == "skipped"]
    title = plan.title or plan.intent

    parts: list[str] = []
    if not steps:
        parts.append(plan.reply or f"{title}: nothing to change.")
        return head + " ".join(parts)

    if verify_result:
        passed, total = verify_result.get("passed", 0), verify_result.get("total", 0)
        verdict = "done" if passed == total and total > 0 else "done with issues" if total else "done"
        parts.append(f"{title}: {verdict} — {len(ran)} step(s) applied, {passed}/{total} checks held.")
    else:
        parts.append(f"{title}: {len(ran)} step(s) applied.")

    parts.extend(_failed_lines(verify_result))
    parts.extend(_unmeasured_lines(verify_result))
    for s in none:
        summary = ""
        if s.results and isinstance(s.results[0], dict):
            summary = str(s.results[0].get("summary") or "")
        parts.append(f"· {s.tool}: {summary or 'nothing to do'}")
    for s in skipped:
        parts.append(f"· {s.tool}: skipped ({s.error})")
    if plan.reply:
        parts.append(plan.reply)
    if exec_result.new_sessions:
        parts.append("Created sessions: " + ", ".join(exec_result.new_sessions) + ".")
        finished = [c for c in exec_result.child_runs if c.get("status") == "ok"]
        if exec_result.child_runs:
            parts.append(f"Finished {len(finished)}/{len(exec_result.child_runs)} of them "
                         f"(reframe, captions, hook).")
    if exec_result.committed:
        parts.append("Undo with ⌘Z" + (" (created sessions are kept)." if exec_result.new_sessions else "."))
    return head + " ".join(parts)


__all__ = ["compose_reply"]
