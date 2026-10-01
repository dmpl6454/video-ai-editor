"""Preview, then apply (0.8.0): the key-free Prompt bar shows what a plan
WOULD change and changes nothing until the person presses Apply.

OWNER DECISION. A plan from an on-device brain (Recipes, Apple Intelligence,
the local model) is run as a DRY RUN first; the Claude cloud brain and the
chat assistant with a key are unchanged. The setting "Ask before applying
Prompt bar edits" (`prompt_setting`, default ON) turns this off, and then the
old immediate behaviour returns — the K3 safety net runs either way.

THE DRY RUN (`scratch_store`, executor `run_plan(dry_run=True)`):
  * runs on a COPY of the session: a throwaway EDLStore in
    `<workdir>/.prompt_preview/<run>/<sid>/` holding the live tree and the
    small per-session files handlers read (meta.json, transcript.json,
    speakers.json). Every file a handler writes under `store.dir` — caches,
    TTS lines, a canvas choice in meta.json, snapshots — lands there and is
    deleted with it;
  * the files a step may rewrite OUTSIDE `store.dir` (the upload's
    `ingest.json`, which sits beside the media) are covered by the executor's
    side-effect snapshot, restored after every dry run;
  * never a render (`DRY_RUN_SKIP`: render_preview, audit_aesthetic, and no
    verify pass), never a new project (`make_shorts` runs with
    `save_as_sessions=False` and reports its picks), no op, no history entry,
    no undo step;
  * the blocking checks and the prompt contract judge it exactly as they
    judge a real run: a failure becomes the same question it is today;
  * what it DERIVES — a whisper transcript, a denoised, reframed or matted
    render — goes to the content-addressed artefact cache beside the scratch
    copies (`.prompt_preview/artefacts/`, artefacts.py), so Apply does the
    heavy work once: it reads an entry whose key (tool, input file identity,
    parameters, model version) still matches and derives again otherwise.

THE CARD. The change list comes from the EDL diff (`changes.summarize`),
never from the plan text. The run pauses as a `confirm` pending state
(pending.py) holding the plan, the base EDL hash (the live tree it was
previewed against), the dry run's fingerprint (`changes.canonical`) and the
lines. The `clarify` frame carries it as `preview` so an older client (the
chat pane, the phone's text) still gets a yes/no question with the list in
the reply text.

APPLY re-runs the SAME plan on the live store in one `EDLStore.batch()` (one
op, one undo step) — only if the live tree still hashes to the base; if not,
the prompt is planned again and a fresh card shown (a stale preview is never
applied). Inside the batch the result must fingerprint the same as the dry
run, or produce the very same lines; if it does not, the batch rolls back
and the card is re-drawn from what it would really do. Change / Cancel drops
the pending state and nothing is committed.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from ...edl import EDLStore
from ...edl.schema import EDL
from . import changes as C
#: The dry-run artefact cache under PREVIEW_DIR (final QA r4, artefacts.py):
#: what a preview derived — a transcript, a denoised or reframed render —
#: carried to Apply so heavy work runs once. Kept out of `_sweep`.
from .artefacts import ARTEFACT_DIR
from .schema import NeedsInput, NeedsInputOption, Plan

#: Scratch copies live beside the sessions, never inside one.
PREVIEW_DIR = ".prompt_preview"
#: A scratch dir older than this is a crash leftover and is swept.
STALE_SCRATCH_S = 3600.0
#: Per-session files a handler may READ from `store.dir` (copied; the media
#: itself is read in place through its absolute path).
_COPIED_FILES = ("meta.json", "transcript.json", "speakers.json")
#: Tools that do not change the timeline and would render: skipped in a dry
#: run (they run when the plan is applied).
DRY_RUN_SKIP: frozenset[str] = frozenset({"render_preview", "audit_aesthetic"})

#: The confirm question's key; its answer is yes (apply) or no (drop).
APPLY_KEY = "apply"
APPLY_QUESTION = "Apply these changes?"
NOTHING_CHANGED_YET = "Nothing has changed yet."
#: `text_delta.outcome` of a preview that would change nothing (no card).
NOTHING_TO_APPLY = "nothing_to_apply"


def apply_question() -> NeedsInput:
    return NeedsInput(key=APPLY_KEY, question=APPLY_QUESTION, kind="confirm", required=True,
                      options=[NeedsInputOption(value="yes", label="Apply", synonyms=["apply", "apply it", "do it"]),
                               NeedsInputOption(value="no", label="Change",
                                                synonyms=["change", "change it", "cancel", "dont apply",
                                                          "don't apply", "discard"])])


def wants_preview(plan: Plan, confirm: bool | None) -> bool:
    """Whether this plan is previewed before it runs: an on-device brain's
    plan with steps, while "Ask before applying" is on. `confirm=None` reads
    the setting."""
    if plan.brain == "claude" or not plan.steps or plan.is_read_only:
        return False
    if confirm is None:
        from ...prompt_setting import confirm_before_apply
        confirm = confirm_before_apply()[0]
    return bool(confirm)


def is_apply_yes(value: Any) -> bool | None:
    """True = apply, False = drop, None = not an answer."""
    if value is True:
        return True
    if value is False:
        return False
    v = str(value or "").strip().lower()
    if v in ("yes", "y", "apply", "true", "1", "ok", "go"):
        return True
    if v in ("no", "n", "change", "cancel", "false", "0", "discard"):
        return False
    return None


# --------------------------------------------------------------------------
# the scratch copy
# --------------------------------------------------------------------------

def scratch_root(live: EDLStore) -> Path:
    return Path(live.dir).parent / PREVIEW_DIR


def scratch_store(live: EDLStore, run_id: str) -> EDLStore:
    """A throwaway store holding a deep copy of the live tree. Named by the
    session id (handlers read `store.dir.name` as the session), in its own
    run directory so two previews never share one."""
    _sweep(scratch_root(live))
    root = scratch_root(live) / run_id / Path(live.dir).name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    (root / "edl.json").write_text(live.edl.to_json(), encoding="utf-8")
    for name in _COPIED_FILES:
        src = Path(live.dir) / name
        if src.is_file():
            shutil.copyfile(src, root / name)
    store = EDLStore(root)
    store.edl = live.edl.model_copy(deep=True)
    return store


def discard_scratch(store: EDLStore | None) -> None:
    if store is None:
        return
    run_dir = Path(store.dir).parent
    shutil.rmtree(run_dir if run_dir.parent.name == PREVIEW_DIR else store.dir, ignore_errors=True)


def _sweep(root: Path) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - STALE_SCRATCH_S
    for d in root.iterdir():
        if d.name == ARTEFACT_DIR:
            continue            # the dry-run artefacts: their own LRU (artefacts.py), not an age
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def path_map(scratch: EDLStore, live: EDLStore) -> dict[str, str]:
    """Scratch paths → the session's, both as written and resolved (macOS
    /var ↔ /private/var)."""
    out = {str(scratch.dir): str(live.dir)}
    try:
        out[str(Path(scratch.dir).resolve())] = str(Path(live.dir).resolve())
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# the lines
# --------------------------------------------------------------------------

def shorts_lines(results: list[dict[str, Any]], fps: Any) -> list[str]:
    """What a `make_shorts` step would create — new projects, outside this
    timeline — from its own result (the ranges it picked)."""
    from .live import smpte
    out: list[str] = []
    for r in results:
        shorts = [s for s in (r.get("shorts") or []) if isinstance(s, dict)]
        if not shorts:
            continue
        spans = []
        for s in shorts[:4]:
            a, b = s.get("start"), s.get("end")
            if a is None or b is None:
                a, b = s.get("source_start", 0.0), s.get("source_end", 0.0)
            spans.append(f"{smpte(float(a), fps)}-{smpte(float(b), fps)}")
        more = f" and {len(shorts) - 4} more" if len(shorts) > 4 else ""
        out.append(f"Create {len(shorts)} new short project{'s' if len(shorts) != 1 else ''} "
                   f"({', '.join(spans)}{more}); this timeline is not changed by them")
    return out


def lines_for(before: EDL, after: EDL, results: list[dict[str, Any]], session_dir: Path) -> list[str]:
    return shorts_lines(results, before.canvas.fps) + [
        c.text for c in C.summarize(before, after, session_dir=session_dir)]


def brain_lines(plan: Plan, live: EDLStore, log: Any, before: EDL, after: EDL,
                lines: list[str]) -> tuple[list[str], dict[str, Any] | None]:
    """Editor Brain (EB1, behind `brain.enabled`): for a brain run, the
    lines the card SHOWS — the cuts grouped with their reason tally, every
    line with a why — and the card's `brain` payload; `(lines, None)` for
    an ordinary prompt or with the flag off, so the 0.8.0 card is
    byte-for-byte what it was. The footprint the resolver wrote during the
    dry run is read from the run's scratch copy (still present: the
    executor discards it after this returns)."""
    from . import brain_card
    if brain_card.decisions_id_of(plan) is None:
        return lines, None
    from ...brain_setting import is_enabled
    if not is_enabled():
        return lines, None
    run_id = getattr(getattr(log, "record", None), "run_id", None)
    scratch = (scratch_root(live) / str(run_id) / Path(live.dir).name) if run_id else None
    changes = C.summarize(before, after, session_dir=Path(live.dir))
    display, payload = brain_card.card_payload(plan, live, scratch, before, after, changes)
    if payload is None:
        return lines, None
    head = lines[: len(lines) - len(changes)]       # the shorts lines, if any, stay in front
    return head + [c.text for c in display], payload


def summary_line(plan: Plan, total: int, length_s: float | None = None) -> str:
    """"Edit: 12 changes"; a brain run's card also says how long the result
    is ("· 44.7 s when applied") — the length was never on the card (UX-12)."""
    title = (plan.title or plan.intent or "This edit").strip()
    line = f"{title}: {total} change{'s' if total != 1 else ''}"
    return f"{line} · {length_s:.1f} s when applied" if length_s else line


def reply_text(label: str, summary: str, shown: list[str], more: int, note: str | None) -> str:
    """The card as text (the chat pane, the phone, a typed "yes"): EVERY
    line, since text has no disclosure to open — "and N more changes" hid a
    trimmed music bed from anyone applying from the chat. `more` is kept for
    callers that pass only the shown lines."""
    from .service import via
    bullets = "\n".join(f"- {line}" for line in shown)
    tail = f"\n- {C.more_line(more)}" if more else ""
    lead = f"{note} " if note else ""
    return (f"{via(label)}{lead}Preview — {summary}. {NOTHING_CHANGED_YET}\n{bullets}{tail}\n"
            f"{APPLY_QUESTION} Reply **yes** to apply or **no** to change it.")


# --------------------------------------------------------------------------
# the pause and the check Apply runs
# --------------------------------------------------------------------------

def pause_for_confirm(live: EDLStore, *, plan: Plan, prompt: str, facts: Any, before: EDL, after: EDL,
                      results: list[dict[str, Any]], base_hash: str, fingerprint: str, log: Any,
                      ui_state: dict | None, contract_hint: dict[str, Any] | None,
                      consented: frozenset[str], note: str | None = None,
                      strip_note: str | None = None) -> str:
    """Save the confirm pending state and publish the card. Returns the
    reply text. A plan that would change nothing gets an honest line and no
    card (there is nothing to apply)."""
    from . import pending
    from .brains.base import BRAIN_LABELS
    from .service import CLARIFY_TTL_S, via

    label = BRAIN_LABELS.get(plan.brain, plan.brain)
    lines = lines_for(before, after, results, Path(live.dir))
    if not lines:
        text = via(label) + ((note + " ") if note else "") + (
            f"{plan.title or 'That'} would not change anything on this timeline, so there is nothing to apply.")
        log.set_reply(text)
        # final sweep 3 r2: its own outcome, so the bar does not show the dry
        # run's steps as "✓ 1 step done" and announce "Done"
        log.emit({"type": "text_delta", "text": text, "outcome": NOTHING_TO_APPLY})
        log.set_status("done")
        return text
    shown, brain = brain_lines(plan, live, log, before, after, lines)
    cl = C.change_list([C.Change(group="", text=t) for t in shown])
    summary = summary_line(plan, cl.total, ((brain or {}).get("summary") or {}).get("result_s"))
    q = apply_question()
    paused = plan.with_(needs_input=[q])
    # `hidden`: the lines past the cap, so the card's "and N more changes"
    # opens to show them — every change is seen before Apply (final sweep 3).
    public = {"summary": summary, "lines": cl.lines, "more": cl.more, "total": cl.total,
              "hidden": cl.all_lines[len(cl.lines):], "note": note, "nothing_changed": NOTHING_CHANGED_YET}
    if brain is not None:
        public["brain"] = brain
    # `all_lines` stays the RAW diff (what `apply_check` compares against);
    # a brain run's grouped lines and whys are presentation (brain_card).
    record = pending.save_pending(
        Path(live.dir), plan=paused, prompt=prompt, facts=facts, ui_state=ui_state,
        preview={**public, "all_lines": lines, "base_hash": base_hash, "fingerprint": fingerprint,
                 "plan": plan.model_dump(), "contract_hint": contract_hint, "strip_note": strip_note,
                 "consented": sorted(consented)})
    text = reply_text(label, summary, cl.all_lines, 0, note)
    log.set_reply(text)
    log.emit({"type": "text_delta", "text": text})
    log.emit({"type": "clarify", "token": record["token"], "plan_id": paused.id,
              "questions": [q.model_dump()], "expires_in_s": CLARIFY_TTL_S, "preview": public})
    log.set_status("clarify")
    return text


def apply_check(preview: dict[str, Any]):
    """The executor's `expect` hook for an Apply run: None when the live
    result is what the card said (same fingerprint, or — for a step whose
    output is not bit-stable — the very same lines), else why not."""
    expected_fp = str(preview.get("fingerprint") or "")
    expected_lines = list(preview.get("all_lines") or [])

    def check(store: EDLStore, result: Any) -> str | None:
        if C.canonical(store.edl, result.edl_before) == expected_fp:
            return None
        results = [r for s in result.steps for r in s.results if s.tool == "make_shorts"]
        got = lines_for(result.edl_before, store.edl, results, Path(store.dir))
        if got == expected_lines:
            return None
        return "the edit came out differently from the preview"

    return check


def public_view(record: dict[str, Any]) -> dict[str, Any] | None:
    p = record.get("preview")
    if not isinstance(p, dict):
        return None
    view = {k: p.get(k) for k in ("summary", "lines", "more", "total", "note", "nothing_changed")}
    lines = list(p.get("lines") or [])
    view["hidden"] = list(p.get("hidden") or list(p.get("all_lines") or [])[len(lines):])
    if isinstance(p.get("brain"), dict):
        # a brain run's payload, only while the flag is on (it can be turned
        # off with a card open; the card then reads as an ordinary one)
        from ...brain_setting import is_enabled
        if is_enabled():
            view["brain"] = p["brain"]
    return view


def preview_plan(record: dict[str, Any]) -> Plan:
    """The plan the card previewed, to run on Apply — without the card's own
    note. service.prompt_turn leads the plan's reply with it ("The timeline
    changed since the preview, so nothing was applied. Here is a fresh
    one.", "Dropped the earlier preview …"), and the Apply's report is
    built from that reply: a successful Apply said "nothing was applied"."""
    plan = Plan.model_validate(record["preview"]["plan"])
    info = record["preview"]
    note = str((info.get("strip_note") if "strip_note" in info else info.get("note")) or "").strip()
    reply = (plan.reply or "").strip()
    if note and reply.startswith(note):
        plan = plan.with_(reply=reply[len(note):].strip() or None)
    return plan


__all__ = ["PREVIEW_DIR", "ARTEFACT_DIR", "DRY_RUN_SKIP", "APPLY_KEY", "APPLY_QUESTION", "NOTHING_CHANGED_YET",
           "apply_question", "wants_preview", "is_apply_yes", "scratch_store", "discard_scratch",
           "path_map", "shorts_lines", "lines_for", "brain_lines", "summary_line", "reply_text",
           "pause_for_confirm",
           "apply_check", "public_view", "preview_plan"]
