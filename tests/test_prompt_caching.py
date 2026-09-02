"""Prompt caching in the chat loop (agent/loop.py).

Every chat turn re-sends ~96 tool schemas + the static system prompt + the
whole conversation, and a user turn is typically 2-4 create() calls (one per
tool round). Two cache breakpoints turn most of that into cache reads: one on
the SYSTEM_PROMPT block (prefix order is tools → system → messages, so it
covers the tool list too) and one on the last persisted block of the trailing
user message, so each tool round re-reads the previous round from cache.

The per-call live timeline context rides AFTER that second marker, as an extra
text block inside the trailing user message — never in `system`. A second
system block would sit ahead of every message, so any change to it (a
mutating tool round, a moved playhead) would have invalidated the whole
conversation segment on exactly the rounds the breakpoint exists for.

These pin the wire shape with a fake client — no network — plus the
invariants that matter most: the marked prefix is byte-identical between two
rounds whose live context differs, persisted `history` never carries a marker
or the live block, and a backend that rejects `cache_control` degrades to the
uncached request once and then stays uncached for the process.
"""
from __future__ import annotations
import asyncio
import copy
from pathlib import Path

import anthropic
import httpx

from video_ai_editor.agent import loop as L
from video_ai_editor.agent.system_prompt import SYSTEM_PROMPT
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl

EPHEMERAL = {"type": "ephemeral"}
LIVE_HEAD = "\n\n# Live timeline state"


def _store(tmp_path: Path) -> EDLStore:
    (tmp_path / "edl.json").write_text(
        empty_edl(Canvas(w=320, h=180, fps=30)).model_dump_json())
    return EDLStore(tmp_path)


class _Usage:
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    input_tokens = 1


class _Text:
    type = "text"
    text = "ok"


class _ToolUse:
    type = "tool_use"
    id = "toolu_01"
    name = "get_timeline"
    input = {"summary": True}


class _Resp:
    content = [_Text()]
    stop_reason = "end_turn"
    usage = _Usage()


class _ToolResp:
    content = [_ToolUse()]
    stop_reason = "tool_use"
    usage = _Usage()


def _bad_request(msg: str) -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(
        msg, response=httpx.Response(400, request=req), body=None)


def _raising(e: Exception):
    def step():
        raise e
    return step


def _fake_client(script):
    """`script`: one callable per create() call; the last one repeats.
    Per-test instances — no class-level shared state between tests."""
    calls: list[dict] = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            step = script[min(len(calls) - 1, len(script) - 1)]
            return step()

    class _Client:
        def __init__(self, **_):
            self.messages = _Messages()

    return _Client, calls


def _run(store, monkeypatch, history, script=(lambda: _Resp(),), *, cache_env=None):
    Client, calls = _fake_client(list(script))
    monkeypatch.setattr(L, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(L, "Anthropic", Client)
    monkeypatch.setattr(L, "_CACHE_DISABLED", False)     # restored on teardown
    if cache_env is None:
        monkeypatch.delenv("VAI_PROMPT_CACHE", raising=False)
    else:
        monkeypatch.setenv("VAI_PROMPT_CACHE", cache_env)

    async def go():
        return [e async for e in L.chat_turn(store, "hi", history, max_turns=2)]

    return asyncio.run(go()), calls


def _markers_in_messages(messages: list[dict]) -> int:
    return sum(1 for m in messages if isinstance(m["content"], list)
               for b in m["content"] if "cache_control" in b)


def _markers_in_tools(tools: list[dict]) -> int:
    return sum(1 for t in tools if "cache_control" in t)


def _live_blocks(messages: list[dict]) -> list[dict]:
    return [b for m in messages if isinstance(m["content"], list)
            for b in m["content"]
            if b.get("type") == "text" and str(b.get("text", "")).startswith(LIVE_HEAD)]


def test_system_prompt_is_the_only_system_block(tmp_path, monkeypatch):
    _, calls = _run(_store(tmp_path), monkeypatch, [])
    assert calls[0]["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": EPHEMERAL}]


def test_live_context_trails_the_marker_in_the_last_user_message(tmp_path, monkeypatch):
    _, calls = _run(_store(tmp_path), monkeypatch, [])
    blocks = calls[0]["messages"][-1]["content"]
    assert blocks[0] == {"type": "text", "text": "hi", "cache_control": EPHEMERAL}
    assert blocks[1]["type"] == "text" and blocks[1]["text"].startswith(LIVE_HEAD)
    assert "cache_control" not in blocks[1]
    assert len(blocks) == 2
    # Nowhere else — and in particular not in `system`.
    assert len(_live_blocks(calls[0]["messages"])) == 1
    assert not any(b["text"].startswith(LIVE_HEAD) for b in calls[0]["system"])


def test_tools_carry_no_cache_control(tmp_path, monkeypatch):
    """A tools breakpoint buys nothing (the system breakpoint already covers
    the prefix), and `_anthropic_tools()` is shared with the MCP server."""
    _, calls = _run(_store(tmp_path), monkeypatch, [])
    assert _markers_in_tools(calls[0]["tools"]) == 0
    assert _markers_in_tools(L._anthropic_tools()) == 0
    assert [t["name"] for t in calls[0]["tools"]] == [t["name"] for t in L._anthropic_tools()]


def test_last_user_block_is_the_message_breakpoint(tmp_path, monkeypatch):
    history: list[dict] = []
    _, calls = _run(_store(tmp_path), monkeypatch, history)
    msgs = calls[0]["messages"]
    assert msgs[-1]["content"][0] == {"type": "text", "text": "hi", "cache_control": EPHEMERAL}
    assert _markers_in_messages(msgs) == 1
    # Marker and live block live on a per-request copy: persisted history is
    # the plain string the caller appended.
    assert history[0]["content"] == "hi"


def test_tool_round_moves_the_breakpoint_to_the_tool_result(tmp_path, monkeypatch):
    history: list[dict] = []
    events, calls = _run(_store(tmp_path), monkeypatch, history,
                         script=(lambda: _ToolResp(), lambda: _Resp()))
    assert len(calls) == 2
    msgs = calls[1]["messages"]
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    assert msgs[-1]["content"][0]["cache_control"] == EPHEMERAL
    assert msgs[-1]["content"][-1]["text"].startswith(LIVE_HEAD)   # live block trails
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]  # earlier turn: no marker, no live block
    assert _markers_in_messages(msgs) == 1
    assert len(_live_blocks(msgs)) == 1
    assert not any("cache_control" in b for m in history
                   if isinstance(m["content"], list) for b in m["content"])
    assert not _live_blocks(history)
    assert events[-1] == {"type": "done"}


def test_marked_prefix_is_stable_when_live_context_changes(tmp_path, monkeypatch):
    """The whole point of trailing the marker: a tool round that changes the
    timeline (or a moved playhead) must not change ANY byte before the block
    that carried round 1's marker, or round 2's message breakpoint is a fresh
    cache write instead of a read."""
    counter = iter(range(1, 100))
    monkeypatch.setattr(L, "_live_context_block",
                        lambda store, ui_state=None: f"{LIVE_HEAD} v{next(counter)}")
    _, calls = _run(_store(tmp_path), monkeypatch, [],
                    script=(lambda: _ToolResp(), lambda: _Resp()))
    assert len(calls) == 2
    assert calls[0]["system"] == calls[1]["system"]
    assert calls[0]["tools"] == calls[1]["tools"]

    def prefix_through_marker(messages: list[dict]) -> list[dict]:
        """Everything up to and including the marked block, marker stripped."""
        out: list[dict] = []
        for m in messages:
            blocks: list[dict] = []
            for b in m["content"]:
                marked = "cache_control" in b
                blocks.append({k: v for k, v in b.items() if k != "cache_control"})
                if marked:
                    return [*out, {**m, "content": blocks}]
            out.append({**m, "content": blocks})
        return out

    p1 = prefix_through_marker(calls[0]["messages"])
    m2 = calls[1]["messages"]
    # Round 2 begins with round 1's marked prefix, block for block …
    assert m2[:len(p1) - 1] == p1[:-1]
    assert m2[len(p1) - 1]["content"][:len(p1[-1]["content"])] == p1[-1]["content"]
    # … while the live block genuinely differed between the two calls.
    live1 = calls[0]["messages"][-1]["content"][-1]["text"]
    live2 = calls[1]["messages"][-1]["content"][-1]["text"]
    assert live1 != live2 and live1.startswith(LIVE_HEAD) and live2.startswith(LIVE_HEAD)


def test_kill_switch_disables_all_markers(tmp_path, monkeypatch):
    _, calls = _run(_store(tmp_path), monkeypatch, [], cache_env="0")
    kw = calls[0]
    assert kw["system"] == [{"type": "text", "text": SYSTEM_PROMPT}]
    assert _markers_in_tools(kw["tools"]) == 0
    assert _markers_in_messages(kw["messages"]) == 0
    assert len(_live_blocks(kw["messages"])) == 1          # the context still travels


def test_empty_live_context_adds_no_block():
    """`_live_context_block` returns "" on failure and the API rejects an
    empty text block, so the live block is omitted rather than sent empty."""
    assert L._system_blocks(True) == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": EPHEMERAL}]
    assert L._system_blocks(False) == [{"type": "text", "text": SYSTEM_PROMPT}]
    wire = L._request_messages([{"role": "user", "content": "hi"}], cached=True, live_context="")
    assert wire[-1]["content"] == [{"type": "text", "text": "hi", "cache_control": EPHEMERAL}]


def test_cache_control_rejection_retries_uncached_once(tmp_path, monkeypatch):
    events, calls = _run(
        _store(tmp_path), monkeypatch, [],
        script=(_raising(_bad_request("Unexpected field cache_control")), lambda: _Resp()))
    assert len(calls) == 2
    assert all("cache_control" not in b for b in calls[1]["system"])
    assert _markers_in_messages(calls[1]["messages"]) == 0
    assert events[-1] == {"type": "done"}
    assert not any(e["type"] == "error" for e in events)
    assert L._CACHE_DISABLED is True                   # monkeypatch resets it


def test_other_400s_do_not_retry_and_roll_back_history(tmp_path, monkeypatch):
    history: list[dict] = []
    events, calls = _run(
        _store(tmp_path), monkeypatch, history,
        script=(_raising(_bad_request("Your credit balance is too low")),))
    assert len(calls) == 1
    assert any(e["type"] == "error" for e in events)
    assert history == []                               # the appended user turn is rolled back
    assert L._CACHE_DISABLED is False


def test_request_messages_strips_stale_markers():
    history = [
        {"role": "user", "content": [{"type": "text", "text": "a", "cache_control": EPHEMERAL}]},
        {"role": "assistant", "content": [{"type": "text", "text": "b", "cache_control": EPHEMERAL}]},
        {"role": "user", "content": [{"type": "text", "text": "c", "cache_control": EPHEMERAL}]},
    ]
    snapshot = copy.deepcopy(history)
    wire = L._request_messages(history, cached=True, live_context="LIVE")
    assert _markers_in_messages(wire) == 1
    assert wire[-1]["content"] == [
        {"type": "text", "text": "c", "cache_control": EPHEMERAL},
        {"type": "text", "text": "LIVE"},
    ]
    assert "cache_control" not in wire[0]["content"][0]
    assert history == snapshot                         # input list unchanged
    assert _markers_in_messages(L._request_messages(history, cached=False)) == 0
    # A trailing assistant turn gets neither a marker nor the live block.
    tail = L._request_messages([{"role": "assistant", "content": "x"}], cached=True, live_context="LIVE")
    assert tail == [{"role": "assistant", "content": [{"type": "text", "text": "x"}]}]
