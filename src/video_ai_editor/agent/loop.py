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
import os
from typing import AsyncIterator
from anthropic import Anthropic
from ..config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from ..edl import EDLStore
from .dispatch import dispatch, get_timeline as _get_timeline
from .tools import list_tools as _list_tools
from .system_prompt import SYSTEM_PROMPT


def _live_context_block(store: EDLStore) -> str:
    """A fresh, ground-truth snapshot of what's actually on the timeline right
    now, appended to the system prompt on every API call (never persisted into
    `history`, so it can never itself go stale).

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
    lines = [f"- {t['type']} ({t['label']}): {len(t['clips'])} clip(s)"
             for t in tracks if t.get("clips")]
    if not lines:
        return ("\n\n# Live timeline state (ground truth — the timeline is EMPTY)\n"
                "There is nothing on the timeline right now. If the user refers to "
                "a video, an upload just happened; call get_timeline(summary=true) "
                "before describing any footage.")
    body = "\n".join(lines)
    return (
        "\n\n# Live timeline state (ground truth, recomputed this turn)\n"
        f"Duration: {snap.get('duration', 0):.1f}s\n{body}\n"
        "This reflects the ACTUAL current timeline — not anything described "
        "earlier in this conversation. If the user asks what a video shows or "
        "contains, verify against get_transcript()/find_moments() rather than "
        "recalling a prior answer; footage from an earlier upload may no "
        "longer be on the timeline at all."
    )


def _friendly_anthropic_error(e: Exception) -> str:
    """Map a raw Anthropic SDK exception to a user-facing message.

    The editor surfaces this string directly in the chat pane, so it must read
    like product copy — never a stack trace or a raw `Error code: 400 {...}`.
    The most common operational failure is an exhausted credit balance (a 400
    whose body says "credit balance is too low"); auth and rate-limit errors get
    their own copy. Anything unrecognised falls back to a generic-but-honest
    "temporarily unavailable" line.
    """
    status = getattr(e, "status_code", None)
    text = str(e).lower()

    if "credit balance is too low" in text or "plans & billing" in text:
        return ("AI features are temporarily unavailable — the Anthropic API "
                "credit balance is exhausted. Add credits at "
                "console.anthropic.com (Plans & Billing) and try again.")
    if status == 401 or "authentication" in text or "invalid x-api-key" in text:
        return ("AI features are unavailable — the Anthropic API key is missing "
                "or invalid. Check ANTHROPIC_API_KEY in your .env and restart.")
    if status == 429 or "rate limit" in text:
        return ("AI is busy right now (rate limited). Wait a few seconds and "
                "try again.")
    if status == 529 or "overloaded" in text:
        return "Claude is temporarily overloaded. Please try again in a moment."
    return ("AI features are temporarily unavailable. Please try again shortly. "
            f"(details: {e})")


# Tool list cached — same Anthropic-format spec lives in tools.py.
def _anthropic_tools(categories: list[str] | None = None) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in _list_tools(categories)
    ]


async def chat_turn(
    store: EDLStore,
    user_message: str,
    history: list[dict],
    *,
    max_turns: int = 8,
) -> AsyncIterator[dict]:
    """Run a single chat turn — possibly multiple tool-use rounds — to completion.

    `history` is mutated to append the new user/assistant messages so the caller
    can persist it.
    """
    if not ANTHROPIC_API_KEY:
        yield {"type": "error", "message": "ANTHROPIC_API_KEY is not set. Add it to ~/video-ai-editor/.env and restart."}
        yield {"type": "done"}
        return

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    history.append({"role": "user", "content": user_message})

    tools = _anthropic_tools()

    # Run the tool-use loop
    for turn in range(max_turns):
        # Recomputed every iteration (not just once) so a tool call that
        # mutates the EDL mid-turn (e.g. a destructive batch op) is reflected
        # before the next round — see _live_context_block's docstring.
        system_with_context = SYSTEM_PROMPT + _live_context_block(store)
        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=system_with_context,
                tools=tools,
                messages=history,
            )
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
                            "content": json.dumps(result, default=str)[:8000],
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

    yield {"type": "done"}
