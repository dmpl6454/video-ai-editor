"""Final QA: the MCP route and the cloud chat's tool calls go through the SAME
guards `/dispatch` uses, and never run a tool on the event loop.

Two defects, one cause (both surfaces called `dispatch()` directly):

  * during a Prompt-bar run, `/dispatch` answers 409 prompt_running, but an
    MCP `tools/call add_text` returned isError:false ("Added label text") and
    a chat tool_use emitted tool_result ok + an op — then Cancel restored the
    pre-run timeline and the agent's confirmed edit was silently gone (or,
    un-cancelled, it was committed in the MIDDLE of the prompt's batch);
  * a heavy tool (transcribe, auto_caption, reframe) called over MCP or chat
    ran synchronously inside an `async def`, so /api/health took 6-7 s and the
    whole UI froze for the length of the tool.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.agent import loop as L
from video_ai_editor.api import locks


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    wd = tmp_path / "wd"
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd, raising=False)
    wd.mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return _main, TestClient(_main.app)


def _texts(main, sid: str) -> list[str]:
    store = main._store(sid)
    return [c.text for t in store.edl.tracks for c in t.clips if hasattr(c, "text")]


def _mcp_add_text(c: TestClient, sid: str, text: str) -> dict:
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "add_text", "arguments": {"session_id": sid, "text": text,
                                          "start": 0.0, "end": 1.0}}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


# ------------------------------------------------------------------------ MCP

def test_mcp_edit_is_refused_while_a_prompt_run_holds_the_project(app):
    main, c = app
    sid = c.post("/api/sessions", json={"name": "mcp"}).json()["id"]
    locks.register_prompt_run(sid, "run-1")
    try:
        res = _mcp_add_text(c, sid, "MCP B")
        assert res["isError"] is True
        assert "Prompt-bar run" in res["content"][0]["text"]
        assert _texts(main, sid) == []
    finally:
        locks.clear_prompt_run(sid, "run-1")
    res = _mcp_add_text(c, sid, "MCP C")
    assert res["isError"] is False
    assert _texts(main, sid) == ["MCP C"]


def test_mcp_tool_waits_for_the_session_lock_without_freezing_the_server(app):
    main, c = app
    sid = c.post("/api/sessions", json={"name": "mcp"}).json()["id"]
    lock = locks.session_lock(sid)
    lock.acquire()
    done: dict = {}

    def call():
        done["res"] = _mcp_add_text(c, sid, "after lock")
        done["t"] = time.monotonic()

    th = threading.Thread(target=call)
    t0 = time.monotonic()
    th.start()
    time.sleep(0.3)
    # The tool is parked on the session lock: nothing written yet …
    assert "res" not in done and _texts(main, sid) == []
    # … and the server still answers (the tool is off the event loop).
    h0 = time.monotonic()
    assert c.get("/api/health").status_code == 200
    assert time.monotonic() - h0 < 0.5
    time.sleep(0.5)
    lock.release()
    th.join(5)
    assert done["res"]["isError"] is False and done["t"] - t0 >= 0.7
    assert _texts(main, sid) == ["after lock"]


# ----------------------------------------------------------------------- chat

class _Usage:
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    input_tokens = 1


class _ToolUse:
    type = "tool_use"
    id = "toolu_01"
    name = "add_text"
    input = {"text": "FROM CHAT", "start": 0.0, "end": 1.0}


class _Text:
    type = "text"
    text = "Added the label."


class _ToolResp:
    content = [_ToolUse()]
    stop_reason = "tool_use"
    usage = _Usage()


class _EndResp:
    content = [_Text()]
    stop_reason = "end_turn"
    usage = _Usage()


def _fake_client(monkeypatch):
    calls: list[int] = []

    class _Messages:
        def create(self, **kw):
            calls.append(1)
            return _ToolResp() if len(calls) == 1 else _EndResp()

    class _Client:
        def __init__(self, **_):
            self.messages = _Messages()

    monkeypatch.setattr(L, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(L, "Anthropic", _Client)


def test_chat_tool_call_is_refused_while_a_prompt_run_holds_the_project(app, monkeypatch):
    main, c = app
    sid = c.post("/api/sessions", json={"name": "chat"}).json()["id"]
    store = main._store(sid)
    _fake_client(monkeypatch)
    locks.register_prompt_run(sid, "run-2")
    try:
        events = asyncio.run(_collect(L.chat_turn(store, "label it", [], max_turns=2)))
    finally:
        locks.clear_prompt_run(sid, "run-2")
    res = next(e for e in events if e["type"] == "tool_result")
    assert res.get("is_error") is True and res["result"].get("code") == "prompt_running"
    assert not any(e["type"] == "op" for e in events)
    assert _texts(main, sid) == []


def test_chat_tool_call_takes_the_session_lock_off_the_event_loop(app, monkeypatch):
    main, c = app
    sid = c.post("/api/sessions", json={"name": "chat"}).json()["id"]
    store = main._store(sid)
    _fake_client(monkeypatch)
    lock = locks.session_lock(sid)
    lock.acquire()
    threading.Timer(0.6, lock.release).start()

    async def go():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.05)

        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        events = await _collect(L.chat_turn(store, "label it", [], max_turns=2))
        waited = time.monotonic() - t0
        stop = True
        await t
        return events, ticks, waited

    events, ticks, waited = asyncio.run(go())
    assert waited >= 0.5            # it waited for the lock …
    assert ticks >= 6               # … while the loop kept running
    assert any(e["type"] == "op" for e in events)
    assert _texts(main, sid) == ["FROM CHAT"]


async def _collect(agen) -> list[dict]:
    return [e async for e in agen]
