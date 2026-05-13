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
from .dispatch import dispatch
from .tools import list_tools as _list_tools
from .system_prompt import SYSTEM_PROMPT


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
        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=history,
            )
        except Exception as e:
            yield {"type": "error", "message": f"Anthropic call failed: {e}"}
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
