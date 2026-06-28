"""MCP server: JSON-RPC protocol + tool routing over the /mcp endpoint.

Locks in the palmier-pro-style "external agent drives the editor" feature:
external tools (Claude Code / Cursor / Codex) POST JSON-RPC to /mcp.
"""
from __future__ import annotations
import importlib
from collections import OrderedDict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    _main._MCP_ACTIVE_SESSION.clear()
    return TestClient(_main.app)


def _rpc(client, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body).json()


def test_initialize_echoes_protocol(client):
    r = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["serverInfo"]["name"] == "video-ai-editor"
    assert "tools" in r["result"]["capabilities"]


def test_tools_list_has_schemas_and_only_real_tools(client):
    from video_ai_editor.agent.dispatch import DISPATCH
    r = _rpc(client, "tools/list")
    tools = r["result"]["tools"]
    assert len(tools) >= 40
    for t in tools:
        assert {"name", "description", "inputSchema"} <= set(t)
        assert t["name"] in DISPATCH  # never advertise an unrunnable tool


def test_tools_call_autocreates_and_persists_session(client):
    r1 = _rpc(client, "tools/call",
              {"name": "set_aspect_ratio", "arguments": {"ratio": "16:9"}}, id_=1)
    assert r1["result"]["isError"] is False
    sid1 = r1["result"]["_meta"]["session_id"]

    r2 = _rpc(client, "tools/call", {"name": "get_timeline", "arguments": {}}, id_=2)
    sid2 = r2["result"]["_meta"]["session_id"]
    assert sid1 == sid2, "MCP active session must persist across calls"

    import json
    tl = json.loads(r2["result"]["content"][0]["text"])
    assert tl["canvas"]["w"] == 1920 and tl["canvas"]["h"] == 1080  # mutation stuck


def test_tools_call_unknown_tool_is_protocol_error(client):
    r = _rpc(client, "tools/call", {"name": "does_not_exist", "arguments": {}})
    assert r["error"]["code"] == -32601


def test_tool_runtime_error_is_isError_not_crash(client):
    # get_clip on a missing id raises ValueError inside the tool → surfaced as
    # an MCP tool error (isError true), NOT a JSON-RPC protocol error.
    r = _rpc(client, "tools/call",
             {"name": "get_clip", "arguments": {"clip_id": "nope"}})
    assert "error" not in r
    assert r["result"]["isError"] is True
    assert "content" in r["result"]


def test_notification_returns_202_no_body(client):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202
    assert resp.content in (b"", b"null")


def test_session_id_arg_targets_specific_session(client):
    # Create a real session via the REST API, then drive it by id over MCP.
    sid = client.post("/api/sessions", json={"name": "target"}).json()["id"]
    r = _rpc(client, "tools/call",
             {"name": "set_aspect_ratio",
              "arguments": {"ratio": "1:1", "session_id": sid}})
    assert r["result"]["_meta"]["session_id"] == sid
    # Confirm it edited THAT session, not the MCP default.
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    assert edl["canvas"]["w"] == edl["canvas"]["h"]  # 1:1


def test_batch_request(client):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    resp = client.post("/mcp", json=batch).json()
    assert isinstance(resp, list) and len(resp) == 2
    assert resp[0]["result"] == {}
    assert "tools" in resp[1]["result"]


def test_get_probe(client):
    r = client.get("/mcp").json()
    assert r["server"]["name"] == "video-ai-editor"
    assert r["transport"] == "http"
