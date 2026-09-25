"""SEC-REBIND-127-PREFIX: the Host allowlist must accept IP LITERALS only.

`host_header_allowed` used to accept any Host that merely STARTS with "127.",
so `Host: 127.attacker.example:8765` (or `127.0.0.1.nip.io`) passed the
DNS-rebinding defence. After a rebind the attacker page is same-origin, so
`Sec-Fetch-Site` does not fire either, and in the default loopback posture
(path restriction off) the page could:
  POST /api/sessions -> dispatch add_sticker{src:<any file>} ->
  GET /api/sessions/{sid}/sticker/{id} -> the file's bytes.

Two defences, both tested through real requests:
  1. the Host check parses the name as an IP address (ipaddress.ip_address)
     instead of prefix-matching it;
  2. the sticker route only serves files that ARE images (magic bytes), so a
     sticker src pointing at a text file / key / database is a 404 even when
     the request is otherwise legitimate.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.api.auth import host_header_allowed


@pytest.mark.parametrize("host", [
    "127.attacker.example:8765",
    "127.0.0.1.nip.io",
    "127.0.0.1.nip.io:8853",
    "127.x",
    "999.1.1.1",                 # four digit groups, not an address
    "10.0.0.5.evil.example",
])
def test_names_that_look_like_loopback_are_refused(host):
    assert not host_header_allowed(host)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8765", "127.1.2.3:9000", "localhost:5173",
    "[::1]:8765", "::1", "10.120.2.82:8765", "192.168.1.9", "testserver",
    "0.0.0.0:8765",
])
def test_real_ip_literals_and_loopback_names_still_pass(host):
    assert host_header_allowed(host)


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return TestClient(_main.app)


def test_rebinding_host_gets_421_on_session_create(client):
    r = client.post("/api/sessions", headers={"Host": "127.attacker.example:8765"})
    assert r.status_code == 421, r.text
    r = client.post("/mcp", headers={"Host": "127.0.0.1.nip.io",
                                     "Content-Type": "application/json"},
                    content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert r.status_code == 421, r.text


_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4e90000000049454e44ae426082")


def test_sticker_route_serves_images_but_not_arbitrary_files(client, tmp_path):
    secret = tmp_path / "canary.txt"
    secret.write_text("TOP-SECRET-canary-9f2c\n")
    art = tmp_path / "art.png"
    art.write_bytes(_PNG_1PX)
    sid = client.post("/api/sessions").json()["id"]

    def add(src):
        r = client.post(f"/api/sessions/{sid}/dispatch",
                        json={"tool": "add_sticker", "args": {"src": str(src)}})
        assert r.status_code == 200, r.text
        return r.json()["result"]["sticker_id"] if "result" in r.json() \
            else r.json()["sticker_id"]

    ok = client.get(f"/api/sessions/{sid}/sticker/{add(art)}")
    assert ok.status_code == 200 and ok.content == _PNG_1PX

    leak = client.get(f"/api/sessions/{sid}/sticker/{add(secret)}")
    assert leak.status_code == 404, leak.status_code
    assert b"TOP-SECRET" not in leak.content


# --- MCP-ANY-CONTENT-TYPE ----------------------------------------------------

@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x", None])
def test_mcp_refuses_cors_simple_content_types(client, ctype):
    """A cross-origin page can POST text/plain (or a form) with no preflight.
    /mcp used to parse such a body as JSON-RPC and run the tool; it must be a
    415 before any tool runs, while application/json keeps working."""
    headers = {"Origin": "https://evil.example"}
    if ctype:
        headers["Content-Type"] = ctype
    body = (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
            b'{"name":"add_text","arguments":{"text":"pwned","start":0,"end":1}}}')
    r = client.post("/mcp", headers=headers, content=body)
    assert r.status_code == 415, (r.status_code, r.text)


@pytest.mark.parametrize("ctype", ["application/json", "application/json; charset=utf-8"])
def test_mcp_still_accepts_json(client, ctype):
    r = client.post("/mcp", headers={"Content-Type": ctype},
                    content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert r.status_code == 200, r.text
    assert r.json()["result"]["tools"]
