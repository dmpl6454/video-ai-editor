"""MCP server — expose the editor's tools to external agents over HTTP.

Inspired by palmier-pro, which runs an MCP server so Claude Code / Cursor /
Codex can drive its timeline. We already have 48 schema'd dispatch tools, so
this is a thin JSON-RPC 2.0 adapter (MCP "streamable HTTP" transport in plain
JSON-response mode) mounted at POST /mcp.

Session model: palmier-pro is single-project. We're multi-session, so the MCP
server drives ONE "active" session — created lazily on first use, reused
across calls, surfaced via the `mcp_session` tool. Any tool call may override
with a `session_id` argument to target a specific project.

Wire-up (main.py):

    claude mcp add --transport http video-ai-editor http://127.0.0.1:8000/mcp

Methods handled: initialize, ping, tools/list, tools/call, plus no-op replies
for resources/list, prompts/list, and notifications/*.
"""
from __future__ import annotations
import json
from typing import Any, Callable

from .tools import list_tools
from .dispatch import dispatch as _dispatch, DISPATCH

# MCP protocol version we advertise. We echo the client's version when it sends
# a known one, else fall back to this.
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "video-ai-editor", "version": "1.0.0"}

# Dispatch tools that don't have a tools.py schema but are safe + useful over
# MCP. Minimal hand-written schemas so they show up in tools/list. (search_media
# and most others live in tools.py / ALL_TOOLS; these are the gaps.)
_EXTRA_SCHEMAS: list[dict] = [
    {
        "name": "list_transitions",
        "description": "List the full transition catalog (categories, aliases, ~45 names).",
        "category": "inspection", "input_schema": {"type": "object", "properties": {}},
    },
]


def _mcp_tools() -> list[dict]:
    """All exposed tools in MCP format: {name, description, inputSchema}.

    Only tools that actually exist in the dispatch registry are listed, so a
    tools/call can never advertise something it can't run.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for t in list(list_tools()) + _EXTRA_SCHEMAS:
        name = t["name"]
        if name in seen or name not in DISPATCH:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "description": t.get("description", ""),
            "inputSchema": t.get("input_schema") or {"type": "object", "properties": {}},
        })
    return out


# ---- JSON-RPC plumbing -------------------------------------------------------

def _ok(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str, data: Any = None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}


def handle_message(
    msg: dict,
    *,
    resolve_store: Callable[[str | None], tuple[Any, str]],
) -> dict | None:
    """Handle one JSON-RPC message. Returns a response dict, or None for
    notifications (which must not produce a response).

    `resolve_store(session_id)` → (EDLStore, resolved_session_id). When
    session_id is None it returns the active MCP session (creating it lazily).
    """
    method = msg.get("method")
    id_ = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion")
        return _ok(id_, {
            "protocolVersion": client_ver or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "ping":
        return _ok(id_, {})

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications get no response

    if method == "tools/list":
        return _ok(id_, {"tools": _mcp_tools()})

    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return _ok(id_, {key: []})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = dict(params.get("arguments") or {})
        if name not in DISPATCH:
            return _err(id_, -32601, f"unknown tool: {name}")
        # Pull session_id out of args (it's an MCP-level concern, not a tool arg).
        session_id = args.pop("session_id", None)
        try:
            store, resolved = resolve_store(session_id)
            result = _dispatch(store, name, args)
            text = json.dumps(result, default=str, ensure_ascii=False)
            return _ok(id_, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
                # Surface which session was edited so the agent can track it.
                "_meta": {"session_id": resolved},
            })
        except Exception as e:  # tool errors → MCP tool error, not protocol error
            return _ok(id_, {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            })

    # Unknown method
    if is_notification:
        return None
    return _err(id_, -32601, f"method not found: {method}")


def handle_request(body: Any, *, resolve_store) -> Any:
    """Top-level entry: handle a single message or a JSON-RPC batch.

    Returns the response object/array, or None if everything was a
    notification (caller should then return HTTP 202 with no body).
    """
    if isinstance(body, list):
        responses = [r for r in (handle_message(m, resolve_store=resolve_store)
                                 for m in body) if r is not None]
        return responses or None
    return handle_message(body, resolve_store=resolve_store)
