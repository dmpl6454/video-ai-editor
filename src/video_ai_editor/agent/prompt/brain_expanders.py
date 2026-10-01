"""The `edit` recipe's expander — the Editor Brain's entry from the Prompt
bar (EDITOR_BRAIN_SPEC.md §2.2; EB1 lane E).

    x_edit(intent, facts, ctx) -> Expansion

  * `brain.enabled` OFF (the default; `VAI_BRAIN_ENABLED=0` is the kill
    switch): the grammar never reads a sentence as `edit` (agent/prompt/
    edit_grammar.py is consulted only with the flag on), so this expander
    is reached only by a model draft that names the recipe — it then hands
    the clause to the recipes the sentence meant before the brain existed,
    without a word about the hidden feature (review SC-04).
  * ON, "sync the dialogue": `sync_dialogue_lane` over the lane the brain
    laid (review SC-12 / EX-06) — the only way to fix a stale lane by hand.
  * ON, the timeline already carries a brain edit (`facts.brain_edit`): ONE
    honest sentence, no plan — never a plan that cuts footage that is
    already cut (review EX-02 / UX-04; the resolver refuses again as a
    last line, lane FX-B).
  * ON, no Content Graph for the session (`facts.brain_graph_id` is None):
    the analysis gate — one question, "Read the footage first / Stop"; no
    step runs. Lane F's route starts the analysis on "read".
  * ON with a graph: a length longer than the footage is refused in one
    sentence; otherwise `brain.planner.plan()` → the EDP is written to
    `<session>/brain/decisions/<did>.json` through lane C's store
    (validated, immutable, content-addressed) BEFORE anything is dry-run,
    then `brain.compile.compile_edp()` turns it into the steps of this
    expansion. Both the dry run and Apply read that same file.

The expander is the only place in the recipe layer that reads the session
directory: the brief sanctions it ("`_x_edit` → plan() → write the EDP →
compile()"), and it reads only what the graph and the store own.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

from . import edit_grammar as _EG
from . import slots as S
from .facts import TimelineFacts
from .facts_brain import brain_on
from .recipes import RECIPE_SLOTS, Context, Expansion, Intent, ask, normalize_slots, step

ANALYSIS_GATE_KEY = "gate_analysis"
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[- ]?\s*(s|sec|secs|seconds?|min|mins|minutes?)\b", re.I)
_PUNCHY_RE = re.compile(r"\b(?:punchier|punchy|snappier|snappy|crisper|more (?:dynamic|energetic|engaging))\b", re.I)
#: `energy` for a "make it punchier" ask (the planner's scale is 1-10, 5 the default).
PUNCHY_ENERGY = 7
_TIGHTEN_RE = re.compile(r"\btighten|\bsnappier\b|\bthe fat\b|\bthe fluff\b", re.I)
_CT_RE = re.compile(r"\b(podcast|interview|talking[- ]head|reel)\b", re.I)
_PLATFORM_OF_WORD = {"reel": "reels", "reels": "reels", "tiktok": "tiktok", "short": "shorts", "shorts": "shorts"}


def brain_enabled() -> bool:
    """`brain_setting.is_enabled()`; anything unreadable is off."""
    return brain_on()


def _session_dir(f: TimelineFacts) -> Path | None:
    from ... import config
    sid = str(f.session_id or "")
    if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
        return None
    return Path(config.WORKDIR) / sid


def controls_from_intent(it: Intent, f: TimelineFacts, ctx: Context) -> dict[str, Any]:
    """The brain controls a Prompt-bar sentence carries: the asked length,
    the platform/ratio, a content-type word, `no captions` / `no music`."""
    clause = it.clause or ""
    read = S.extract(clause)
    dur = it.get("duration_s") or it.get("_duration_s") or read.duration_s
    if dur is None:
        m = _DUR_RE.search(clause)
        dur = float(m.group(1)) * (60.0 if m.group(2).lower().startswith("m") else 1.0) if m else None
    platform = it.get("platform") or read.platform
    if platform is None:
        m = re.search(r"\b(reels?|tiktok|shorts?)\b", clause, re.I)
        platform = _PLATFORM_OF_WORD.get(m.group(1).lower()) if m else None
    ratio = it.get("ratio") or it.get("_ratio") or read.ratio or (S.PLATFORM_RATIO.get(platform) if platform else None)
    ct = it.get("content_type")
    if ct is None:
        m = _CT_RE.search(clause)
        word = m.group(1).lower().replace(" ", "_").replace("-", "_") if m else None
        ct = {"podcast": "podcast", "interview": "interview", "talking_head": "talking_head"}.get(word or "")
    controls: dict[str, Any] = {
        "content_type": ct or "auto",
        "energy": int(it.get("energy") or (PUNCHY_ENERGY if _PUNCHY_RE.search(clause) else 5)),
        "captions": "off" if "captions" in ctx.exclusions else str(it.get("captions") or "auto"),
        "music": "off" if ("music" in ctx.exclusions or f.has_music) else (it.get("music") or None),
        "duration_s": float(dur) if dur else None,
        "platform": platform, "ratio": ratio, "count": 1,
    }
    if _EG.is_cleanup(clause):
        # "clean this up": the cuts, the fillers and the pauses — no captions, no music, no punch-ins, no new lane
        controls.update(captions="off", music="off", scope="cleanup")
    return {k: v for k, v in controls.items() if v is not None}


def _fallback(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    """The brain is off (a model draft named the recipe): the recipes this
    sentence meant before it existed — silently, the feature is hidden."""
    from .expanders import expand_auto_edit
    clause = it.clause or ""
    if _TIGHTEN_RE.search(clause):
        return Expansion(prerequisites=(Intent("tighten", {}, it.score, it.clause),))
    c = controls_from_intent(it, f, ctx)
    slots = {"platform": c.get("platform"), "_ratio": c.get("ratio"), "_duration_s": c.get("duration_s"),
             "_hook_text": None, "_lufs": None, "_template": None, "_caption_style": None, "_caption_position": None,
             "language": None, "mood": None, "look": None}
    auto = Intent("auto_edit", normalize_slots("auto_edit", {k: v for k, v in slots.items() if not k.startswith("_")})
                  | {k: v for k, v in slots.items() if k.startswith("_")}, it.score, it.clause)
    return Expansion(prerequisites=tuple(expand_auto_edit(auto, f, ctx.exclusions)))


def _eta_words(seconds: float) -> str:
    """"≈ 5 s" / "≈ 2 min": the read's estimate from the media's length alone (brain_seams.estimate_read_s)."""
    s = max(1.0, float(seconds))
    return f"≈ {int(round(s))} s" if s < 90 else f"≈ {max(2, int(round(s / 60.0)))} min"


def _gate(f: TimelineFacts) -> Expansion:
    """The analysis gate: ONE question, "Read the footage first / Stop". While the upload's transcript is
    still being made the footage IS being read, so it says that instead of promising a read of its own
    (review UX-07: the gate said "≈ 1 min" over footage that reads in seconds)."""
    if f.transcript_pending:
        q = ask(ANALYSIS_GATE_KEY, "The footage is still being read — its speech is still being transcribed. "
                                   "Wait for it, then edit?",
                kind="choice", options=[("read", "Wait for it, then edit", "The edit is planned when the words are ready"),
                                        ("abort", "Stop", "Nothing changes")])
        return Expansion(questions=(q,), notes=("the Editor Brain waits for the transcript before it plans",))
    from .brain_seams import estimate_read_s
    q = ask(ANALYSIS_GATE_KEY, f"The footage has not been read yet. Read it first ({_eta_words(estimate_read_s(f.duration))}), "
                               "then edit?",
            kind="choice", options=[("read", "Read the footage first", "Speech, speakers, sound and moments — on this Mac"),
                                    ("abort", "Stop", "Nothing changes")])
    return Expansion(questions=(q,), notes=("the Editor Brain needs the footage read before it plans",))


def _speechless(f: TimelineFacts, sdir: Path, gid: str) -> Expansion | None:
    """A graph whose speech layer is empty (pinned when no transcript came, review UX-01): a plan on it would be
    a "reel" of the whole clip with no cut, no hook and no captions. While the transcript is on its way that is
    the gate's "still being read"; otherwise one honest sentence — and nothing is planned."""
    try:
        from ...brain import store as _store
        from ...brain.graph import blockers
        found = blockers(_store.read_json(_store.graph_path(sdir, gid)))
    except Exception:  # noqa: BLE001 — an unreadable header is the loader's to refuse, below
        return None
    if not found:
        return None
    if found[0]["code"] == "silent":                     # a transcript that says next to nothing: no wait will change it
        return Expansion(notes=(f"{found[0]['message']} Nothing was changed.",))
    if f.transcript_pending:
        return _gate(f)
    return Expansion(notes=(f"I cannot edit this footage yet. {found[0]['message']} Nothing was changed; ask again once "
                            "its transcript is ready.",))


def _write_edp(sdir: Path, edp: dict[str, Any]) -> list[str]:
    """Through lane C's store: validated, canonical, immutable. A second run
    over the same footage produces the same decisions and therefore the
    same id; only `created` differs, which the content hash excludes."""
    from ...brain import schema as _S
    from ...brain import store as _store
    model = _S.EDP.model_validate(edp)
    try:
        _store.write_edp(sdir, model)
    except FileExistsError:
        return [f"decisions {edp['id']} already on file; reused"]
    return []


def _load_graph(sdir: Path, gid: str):
    from ...brain.planner.graph_view import load_graph
    return load_graph(sdir, gid)


def _clock(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    return f"{total // 60}:{total % 60:02d}"


#: One honest sentence each: never a plan over a timeline the brain already edited.
ALREADY_EDITED = ("This timeline was already edited by the brain ({label}). Undo that edit first (⌘Z) or restore the "
                  "original from the versions list, then ask again.")
EDITED_AFTER = ("This timeline was edited after the brain's {label}, and the brain does not keep hand edits yet. Undo back "
                "to {label} (⌘Z) or restore the original from the versions list, then ask again.")
ALREADY_EDITED_NO_VERSION = ("This timeline already carries an edit by the brain (its dialogue lane and camera pieces "
                             "are in place). Undo that edit first (⌘Z) or restore the original from the versions list, "
                             "then ask again.")


def already_edited(f: TimelineFacts) -> Expansion | None:
    """A refusal when `f.brain_edit` says the timeline holds an earlier brain edit."""
    e = getattr(f, "brain_edit", None)
    if e is None:
        return None
    label = e.label or "the brain's edit"
    text = (ALREADY_EDITED.format(label=label) if e.state == "brain_edited" and e.label
            else EDITED_AFTER.format(label=label) if e.state == "edited_after" and e.label
            else ALREADY_EDITED_NO_VERSION)
    return Expansion(notes=(text,))


def _resync(f: TimelineFacts) -> Expansion:
    """"sync the dialogue": `sync_dialogue_lane` over the lane as the brain laid it."""
    lane = f.dialogue_lane
    if lane is None:
        return Expansion(notes=("There is no dialogue lane on this timeline to re-sync; the brain lays one when it edits.",))
    if lane.in_sync is True:
        return Expansion(notes=("The dialogue lane already follows the picture; nothing to re-sync.",))
    if not lane.offsets:
        return Expansion(notes=("I cannot tell how this dialogue lane was laid (no brain edit made it), so I will not "
                                "re-sync it.",))
    st = step("sync_dialogue_lane", 2, "put the dialogue lane back in step with the picture",
              src=lane.src, lane="a1", offsets=dict(lane.offsets), seam_fade_s=0.005, mute_camera_mics=True)
    return Expansion(steps=(st,), notes=("re-syncing the dialogue lane to the picture",))


def _too_long(controls: dict[str, Any], graph: Any) -> Expansion | None:
    """A length longer than the footage is refused in one sentence (review UX-12)."""
    asked, have = controls.get("duration_s"), float(getattr(graph, "ref_end", 0.0) or 0.0)
    if not asked or not have or asked <= have + 1.0:
        return None
    return Expansion(notes=(f"The footage is {_clock(have)} long, shorter than the {_clock(asked)} you asked for. "
                            f"Ask for {_clock(have)} or less, for example “make a {max(1, int(have))}-second reel”.",))


def edit_for_auto_edit(it: Intent, f: TimelineFacts, exclusions: frozenset[str]) -> list[Intent] | None:
    """`expand_auto_edit`'s hand-over: with a Content Graph for the session (the brain is on and the footage was
    read) the whole edit is the brain's `edit` recipe; without one None, and the Auto edit's checklist runs
    exactly as before."""
    if not getattr(f, "brain_graph_id", None) or "edit" in exclusions:
        return None
    carried = {k: v for k, v in it.slots.items() if k in RECIPE_SLOTS["edit"] and v is not None}
    carried.update({k: v for k, v in it.slots.items() if k in ("_ratio", "_duration_s") and v is not None})
    return [Intent("edit", carried, it.score, it.clause)]


def x_edit(it: Intent, f: TimelineFacts, ctx: Context) -> Expansion:
    if not brain_enabled():
        return _fallback(it, f, ctx)
    if _EG.sync_only(it.clause or ""):
        return _resync(f)
    if (refused := already_edited(f)) is not None:
        return refused
    gid = getattr(f, "brain_graph_id", None)
    sdir = _session_dir(f)
    if not gid or sdir is None:
        return _gate(f)
    from ...brain.compile import compile_edp
    from ...brain.planner import plan
    try:
        graph = _load_graph(sdir, gid)
    except (OSError, ValueError, FileNotFoundError) as e:
        return Expansion(notes=(f"the footage's analysis could not be read ({e}); read it again",),
                         questions=_gate(f).questions)
    if (refused := _speechless(f, sdir, gid)) is not None:
        return refused
    controls = controls_from_intent(it, f, ctx)
    if (refused := _too_long(controls, graph)) is not None:
        return refused
    created = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edp = plan(graph, controls, created=created)
    notes = _write_edp(sdir, edp)
    compiled = compile_edp(edp, f, graph)
    summary = edp["summary"]
    lead = (f"opens on “{_quote_cut(str(summary['hook']['quote']))}”; " if summary.get("hook") else "")
    notes.append(f"{lead}{compiled.title}")
    if summary.get("duration_s"):
        notes.append(f"the result runs {_clock(float(summary['duration_s']))}")
    return Expansion(steps=tuple(compiled.steps), postconditions=tuple(compiled.postconditions),
                     notes=tuple(notes + compiled.notes), content_brain=edp.get("content_brain"))


def _quote_cut(quote: str, limit: int = 60) -> str:
    """The hook's line for the plan note: whole if it fits, else cut at a word with an ellipsis (never mid-word)."""
    quote = " ".join(quote.split())
    if len(quote) <= limit:
        return quote
    return quote[:limit].rsplit(" ", 1)[0].rstrip(",;:-— ") + "…"


__all__ = ["x_edit", "edit_for_auto_edit", "controls_from_intent", "brain_enabled", "already_edited", "ANALYSIS_GATE_KEY"]
