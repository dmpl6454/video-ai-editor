"""Anthropic tool-use loop.

Yields events to be SSE-streamed to the client:
  - {"type":"text_delta","text":"…"}              streamed assistant text
  - {"type":"tool_use","name":"…","args":{...}}    tool call about to run
  - {"type":"tool_result","name":"…","result":...} dispatched tool result
  - {"type":"op","op":{...}}                       ops_log entry that resulted
  - {"type":"done"}                                end of turn
  - {"type":"error","message":"…"}
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import AsyncIterator
from anthropic import Anthropic, BadRequestError
from ..config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from ..edl import EDLStore
from .dispatch import dispatch, get_timeline as _get_timeline
from .tools import list_tools as _list_tools
from .system_prompt import SYSTEM_PROMPT


def _clip_line(i: int, c: dict) -> str:
    """One enumerated clip entry: ordinal, id, human name, timeline span."""
    name = c.get("src_name") or c.get("text") or ""
    if isinstance(name, str) and len(name) > 32:
        name = name[:29] + "…"
    start = float(c.get("start", 0.0))
    dur = float(c.get("duration", 0.0) or 0.0)
    label = f' "{name}"' if name else ""
    return f"[{i}] {c.get('id', '?')}{label} {start:.1f}–{start + dur:.1f}s"


def _ui_state_block(ui_state: dict | None, tracks: list[dict]) -> str:
    """What the user is pointing at in the editor UI right now.

    This is what lets Claude bind "this clip" (selection) and "here"
    (playhead) to concrete clip ids instead of guessing.
    """
    if not ui_state:
        return ""
    lines: list[str] = []
    sel = ui_state.get("selection")
    multi = [s for s in (ui_state.get("multi_selection") or []) if s != sel]
    if sel:
        lines.append(f"Selected clip (what the user means by 'this'): {sel}"
                     + (f" (+ also selected: {', '.join(multi)})" if multi else ""))
    playhead = ui_state.get("playhead")
    if playhead is not None:
        ph = float(playhead)
        under = None
        for t in tracks:
            if t.get("type") != "video":
                continue
            for c in t.get("clips", []):
                start = float(c.get("start", 0.0))
                dur = float(c.get("duration", 0.0) or 0.0)
                if start <= ph < start + dur:
                    under = c.get("id")
                    break
            if under:
                break
        lines.append(f"Playhead (what the user means by 'here'): {ph:.2f}s"
                     + (f" — inside clip {under}" if under else ""))
    if not lines:
        return ""
    return "\n\n# Editor UI state (what the user is pointing at)\n" + "\n".join(lines)


def _live_context_block(store: EDLStore, ui_state: dict | None = None) -> str:
    """A fresh, ground-truth snapshot of what's actually on the timeline right
    now, sent with every API call as the last block of the trailing user
    message (never persisted into `history`, so it can never itself go stale;
    never in `system`, so its churn never invalidates the prompt cache).

    Without this, Claude answers "what's in this video" purely from whatever
    it said earlier in the conversation — including about footage from a
    prior upload that's no longer on the timeline. The system prompt already
    *asks* Claude to call get_timeline first, but that's advisory: a model
    that skips the call (or a long conversation where the advice scrolled out
    of attention) falls back to memory. Making the current state structurally
    present in every turn's system prompt closes that gap regardless of
    whether Claude chooses to call the tool.
    """
    try:
        snap = _get_timeline(store, {"summary": True})
    except Exception:
        return ""
    tracks = snap.get("tracks", [])
    lines = []
    for t in tracks:
        clips = t.get("clips") or []
        if not clips:
            continue
        # Enumerate with ordinals so "the second clip" resolves to an id.
        shown = clips[:12]
        entries = " · ".join(_clip_line(i + 1, c) for i, c in enumerate(shown))
        more = f" · +{len(clips) - len(shown)} more" if len(clips) > len(shown) else ""
        lines.append(f"- {t['type']} ({t['label']}): {len(clips)} clip(s): "
                     f"{entries}{more}")
    if not lines:
        return ("\n\n# Live timeline state (ground truth — the timeline is EMPTY)\n"
                "There is nothing on the timeline right now. If the user refers to "
                "a video, an upload just happened; call get_timeline(summary=true) "
                "before describing any footage."
                + _ui_state_block(ui_state, tracks))
    body = "\n".join(lines)
    return (
        "\n\n# Live timeline state (ground truth, recomputed this turn)\n"
        f"Duration: {snap.get('duration', 0):.1f}s\n{body}\n"
        "This reflects the ACTUAL current timeline — not anything described "
        "earlier in this conversation. Ordinals like 'the second clip' refer "
        "to the [n] numbering above. If the user asks what a video shows or "
        "contains, verify against get_transcript()/find_moments() rather than "
        "recalling a prior answer; footage from an earlier upload may no "
        "longer be on the timeline at all."
        + _ui_state_block(ui_state, tracks)
    )


#: The ONE place this module tells a user how to supply a key, so the chat
#: pane's two key failures (never had one / has a bad one) cannot drift apart.
#: It names the toolbar affordance and not a dotfile: `apikey.py` makes
#: `POST /api/settings/api-key` a complete substitute for hand-creating
#: `~/Library/Application Support/Video AI Editor/.env` — a Finder-hidden file
#: in a Finder-hidden folder — and it rebinds the running process, so neither
#: "edit .env" nor "restart" was ever advice a packaged-app user could act on.
#: True from source too, where the same button is present and does the same
#: thing, which is why this needs no per-build branch.
_KEY_HOWTO = ("Click the 🔑 key button in the toolbar and paste a key "
              "from console.anthropic.com — it takes effect right away, no "
              "restart needed.")

#: Shown when the process has no key at all (the chat pane normally catches
#: this first and offers the same button inline).
_NO_KEY_MESSAGE = "Chat needs an Anthropic API key. " + _KEY_HOWTO


def _friendly_anthropic_error(e: Exception) -> str:
    """Map a raw Anthropic SDK exception to a user-facing message.

    The editor surfaces this string directly in the chat pane, so it must read
    like product copy — never a stack trace or a raw `Error code: 400 {...}`.
    The most common operational failure is an exhausted credit balance (a 400
    whose body says "credit balance is too low"); auth and rate-limit errors get
    their own copy. Anything unrecognised falls back to a generic-but-honest
    "temporarily unavailable" line.

    Anything about the KEY points at the in-app route, never at a dotfile. The
    auth line used to say "check ANTHROPIC_API_KEY in your .env and restart",
    which is wrong twice for the audience that hits it most: a packaged-app user
    has no `.env` they know of (it lives in a Finder-hidden folder under
    Application Support, which is exactly the problem `apikey.py` exists to
    solve), and no restart is needed either — `POST /api/settings/api-key`
    rebinds the live process. One sentence has to stay true from source too, so
    it names the toolbar affordance rather than a build-specific path.
    """
    status = getattr(e, "status_code", None)
    text = str(e).lower()

    if "credit balance is too low" in text or "plans & billing" in text:
        return ("AI features are temporarily unavailable — the Anthropic API "
                "credit balance is exhausted. Add credits at "
                "console.anthropic.com (Plans & Billing) and try again.")
    if status == 401 or "authentication" in text or "invalid x-api-key" in text:
        return ("AI features are unavailable — the Anthropic API key is missing "
                "or invalid. " + _KEY_HOWTO)
    if status == 429 or "rate limit" in text:
        return ("AI is busy right now (rate limited). Wait a few seconds and "
                "try again.")
    if status == 529 or "overloaded" in text:
        return "Claude is temporarily overloaded. Please try again in a moment."
    return ("AI features are temporarily unavailable. Please try again shortly. "
            f"(details: {e})")


def _strip_internal_keys(schema: dict) -> dict:
    """Drop this repo's own `x-…` schema annotations before the wire.

    `tools.py` carries a few keys the dispatch-boundary validator reads (today:
    `x-validated-by-handler`). They are not JSON Schema and have no meaning to
    the model, so they are projected out here alongside the `category` field —
    the same reason.
    """
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    return {
        **schema,
        "properties": {
            k: ({pk: pv for pk, pv in v.items() if not pk.startswith("x-")}
                if isinstance(v, dict) else v)
            for k, v in props.items()
        },
    }


# Tool list cached — same Anthropic-format spec lives in tools.py.
def _anthropic_tools(categories: list[str] | None = None) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"],
         "input_schema": _strip_internal_keys(t["input_schema"])}
        for t in _list_tools(categories)
    ]


# Prompt caching. Every chat turn re-sends 96 tool schemas + the house-style
# system prompt + the whole conversation, and a user turn is typically 2-4
# create() calls (one per tool round). Two cache breakpoints: the static
# SYSTEM_PROMPT block (prefix order is tools → system → messages, so this one
# caches the tool list too) and the last persisted block of the trailing user
# message, so each tool round re-reads the previous round from cache.
#
# The per-call live-context block goes LAST — an extra text block appended
# after the marked block inside the wire copy of that trailing user message,
# never into `system`. Position matters: the cache is a prefix cache, and a
# second system block would sit BEFORE every message, so any change to it
# (a mutating tool round, a moved playhead) would have invalidated the whole
# conversation segment and turned the message breakpoint into a fresh write
# on exactly the rounds it was meant to serve. Trailing the breakpoint, its
# churn costs only its own (uncached) tokens.
#
# Both markers are applied to a per-request copy: persisted history never
# carries `cache_control` (max 4 per request) nor the live block (it must not
# go stale — see _live_context_block). VAI_PROMPT_CACHE=0 is the operator kill
# switch; _CACHE_DISABLED is the runtime one, set only when the API itself
# rejects `cache_control`.
CACHE_EPHEMERAL = {"type": "ephemeral"}
_CACHE_DISABLED = False
_log = logging.getLogger(__name__)


def _prompt_cache_enabled() -> bool:
    if _CACHE_DISABLED:
        return False
    return os.environ.get("VAI_PROMPT_CACHE", "1").strip().lower() not in ("0", "false", "no")


def _system_blocks(cached: bool) -> list[dict]:
    """SYSTEM_PROMPT as the ONLY system block, cacheable. The live context is
    deliberately not here — see the prompt-caching note above."""
    head: dict = {"type": "text", "text": SYSTEM_PROMPT}
    if cached:
        head["cache_control"] = CACHE_EPHEMERAL
    return [head]


def _request_messages(history: list[dict], cached: bool, live_context: str = "") -> list[dict]:
    """A copy of `history` for the wire: every stale cache_control stripped,
    (when cached) one marker on the last persisted block of the last user
    message, and the live-context block appended AFTER that marker as a
    separate uncached text block. Plain-string content is promoted to a text
    block everywhere (not only where the marker lands) so the wire form of a
    message is the same whether it is the trailing one or history — the
    prefix the cache is keyed on must be literally identical from one round
    to the next, and tests/test_prompt_caching.py checks it that way. The
    live block is omitted when empty (the API rejects an empty text block and
    `_live_context_block` returns "" on failure). `history` is never mutated.

    chat_turn always ends `history` on a user message (the initial text, or a
    tool_result); should a caller ever hand over one ending on an assistant
    turn, neither marker nor live block has a legal home and both are left
    out rather than sent somewhere they would break the alternation."""
    out: list[dict] = []
    for m in history:
        content = m["content"]
        if isinstance(content, list):
            content = [{k: v for k, v in b.items() if k != "cache_control"} for b in content]
        else:
            content = [{"type": "text", "text": content}]
        out.append({**m, "content": content})
    if not out or out[-1]["role"] != "user":
        return out
    last = out[-1]
    blocks = last["content"]
    if cached:
        blocks = [*blocks[:-1], {**blocks[-1], "cache_control": CACHE_EPHEMERAL}]
    if live_context:
        blocks = [*blocks, {"type": "text", "text": live_context}]
    out[-1] = {**last, "content": blocks}
    return out


def _is_cache_control_rejection(e: Exception) -> bool:
    return isinstance(e, BadRequestError) and "cache_control" in str(e).lower()


async def _create_with_cache_fallback(create):
    """`create(cached)` off the event loop, retried once uncached if — and only
    if — the API rejected `cache_control` itself (an old proxy, a model without
    caching). That must degrade to the plain request, not kill chat, and must
    not be re-attempted on every later round: the rejection flips the
    process-wide switch. Any other exception takes the caller's normal
    rollback path unchanged."""
    global _CACHE_DISABLED
    try:
        return await asyncio.to_thread(create, _prompt_cache_enabled())
    except Exception as e:
        if not _is_cache_control_rejection(e):
            raise
        _CACHE_DISABLED = True
        _log.warning("prompt caching disabled for this process: %s", e)
        return await asyncio.to_thread(create, False)


TOOL_RESULT_LIMIT = 8000


def _tool_result_json(result) -> str:
    """A tool result as JSON, bounded — and still VALID JSON when bounded.

    This used to be `json.dumps(result)[:8000]`, which slices mid-structure: on
    a 40-clip timeline `get_timeline` is ~11.4k chars, so what reached the model
    was a document that simply stopped — an unclosed string, half a key. Asked
    to reason over that, it says it is confused, or invents the missing half.

    Truncating into a labelled envelope keeps the payload parseable and tells
    the model both that it is partial and what to do about it. Cheaper than
    guessing which fields matter, and it cannot corrupt a small result.
    """
    s = json.dumps(result, default=str)
    if len(s) <= TOOL_RESULT_LIMIT:
        return s

    def envelope(preview_chars: int) -> str:
        return json.dumps({
            "_truncated": True,
            "original_chars": len(s),
            "note": ("This result was too large to include in full. It is CUT OFF — "
                     "do not assume anything about what is missing. Narrow the "
                     "query (one track, one clip) or work from the ids you have."),
            "preview": s[:preview_chars],
        })

    # Re-encoding escapes quotes and backslashes, so the envelope is longer than
    # the preview it holds — by however much the payload happens to contain.
    # Shrink until it genuinely fits rather than assuming a fixed allowance.
    room = TOOL_RESULT_LIMIT - 400
    out = envelope(room)
    while len(out) > TOOL_RESULT_LIMIT and room > 200:
        room -= max(64, len(out) - TOOL_RESULT_LIMIT)
        out = envelope(room)
    return out


async def chat_turn(
    store: EDLStore,
    user_message: str,
    history: list[dict],
    *,
    max_turns: int = 8,
    ui_state: dict | None = None,
) -> AsyncIterator[dict]:
    """Run a single chat turn — possibly multiple tool-use rounds — to completion.

    `history` is mutated to append the new user/assistant messages so the caller
    can persist it.
    """
    if not ANTHROPIC_API_KEY:
        # Same route as the auth failure above, and for the same reason — the
        # old copy named a path (`~/video-ai-editor/.env`) that is not where any
        # build reads one from.
        yield {"type": "error", "message": _NO_KEY_MESSAGE}
        yield {"type": "done"}
        return

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    history.append({"role": "user", "content": user_message})

    tools = _anthropic_tools()

    def _create(cached: bool):
        # Live context is recomputed on every call (not once per turn) so a
        # tool call that mutates the EDL mid-turn (e.g. a destructive batch
        # op) is reflected before the next round — see _live_context_block's
        # docstring. It rides as the LAST block of the trailing user message,
        # behind the cache marker, so that churn never invalidates the cached
        # tools + SYSTEM_PROMPT + conversation prefix (prompt-caching note).
        live = _live_context_block(store, ui_state)
        return client.messages.create(
            model=CLAUDE_MODEL, max_tokens=4096,
            system=_system_blocks(cached), tools=tools,
            messages=_request_messages(history, cached, live))

    # Run the tool-use loop
    for turn in range(max_turns):
        try:
            resp = await _create_with_cache_fallback(_create)
        except Exception as e:
            # Roll back the trailing user message we appended before this call.
            # If we leave it, the persisted history ends on a user turn; the next
            # chat appends a second user message and the API rejects the whole
            # conversation ("roles must alternate") — so one credit failure would
            # wedge every subsequent message even after credits are restored.
            if turn == 0 and history and history[-1].get("role") == "user":
                history.pop()
            yield {"type": "error", "message": _friendly_anthropic_error(e)}
            yield {"type": "done"}
            return

        # Debug-level only: the one place to confirm the cache is actually
        # hitting (cache_read > 0 from the second create() of a turn on).
        usage = getattr(resp, "usage", None)
        if usage is not None:
            _log.debug("anthropic usage: cache_read=%s cache_write=%s input=%s",
                       getattr(usage, "cache_read_input_tokens", 0),
                       getattr(usage, "cache_creation_input_tokens", 0),
                       getattr(usage, "input_tokens", 0))

        assistant_blocks = []
        any_tool = False
        for block in resp.content:
            if block.type == "text":
                yield {"type": "text_delta", "text": block.text}
                assistant_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                any_tool = True
                tool_name = block.name
                tool_args = dict(block.input)
                yield {"type": "tool_use", "name": tool_name, "args": tool_args, "id": block.id}
                assistant_blocks.append({
                    "type": "tool_use", "id": block.id, "name": tool_name, "input": tool_args,
                })
                # Dispatch
                try:
                    result = dispatch(store, tool_name, tool_args)
                    op = store.ops.last()
                    yield {"type": "tool_result", "name": tool_name, "result": result, "id": block.id}
                    if op:
                        yield {"type": "op", "op": op.model_dump()}
                    history.append({"role": "assistant", "content": assistant_blocks})
                    history.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _tool_result_json(result),
                        }],
                    })
                    assistant_blocks = []  # reset; next turn starts fresh
                except Exception as e:
                    err = {"error": str(e)}
                    yield {"type": "tool_result", "name": tool_name, "result": err, "id": block.id, "is_error": True}
                    history.append({"role": "assistant", "content": assistant_blocks})
                    history.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(err),
                            "is_error": True,
                        }],
                    })
                    assistant_blocks = []

        if assistant_blocks:
            history.append({"role": "assistant", "content": assistant_blocks})

        if not any_tool:
            break
    else:
        # Ran out of tool-use rounds with the model still working.
        #
        # Two failures, both silent. The chat produced NO closing text — the
        # user saw a run of tool calls and then nothing, which reads as the
        # assistant giving up or losing the thread. And `history` was left
        # ending on a `user` tool_result, so the next message appended a second
        # consecutive user turn and the API rejected the whole conversation
        # ("roles must alternate") — the same wedge the credit-failure path
        # above already guards against, just reached from the other direction.
        #
        # So: say what happened, and close the history on an assistant turn.
        msg = (f"I stopped after {max_turns} tool steps — the task needed more "
               f"than one turn's worth. Nothing is broken and every step so far "
               f"has been applied; tell me to continue and I'll pick up from here.")
        yield {"type": "text_delta", "text": msg}
        history.append({"role": "assistant", "content": [{"type": "text", "text": msg}]})

    yield {"type": "done"}
