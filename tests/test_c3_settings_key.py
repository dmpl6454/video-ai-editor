"""Settings › Claude: the Anthropic API key in the macOS Keychain (QA-063-SETTINGS).

Before this there was no way to give the packaged app a key: the only path was
`ANTHROPIC_API_KEY` in a `.env` inside a read-only bundle or a hidden folder,
and the brain popover told people to "Add ANTHROPIC_API_KEY to .env and
restart". These tests drive the REAL `security` tool against a throwaway
Keychain service (uuid-named, deleted afterwards — never the app's real
"Video AI Editor" item) and the real FastAPI app, and prove:

  * the round trip (save → read → mask → remove) through the Keychain;
  * the key never rides on a command line (argv is world-readable via `ps`);
  * a saved key reaches the Claude rung WITHOUT a restart;
  * the key never appears in any response body, log line or file under
    WORKDIR / the app-data dir — including on errors and validation failures;
  * the routes are loopback-only, JSON-only, and behind the SEC-REBIND Host
    check like every other route.
"""
from __future__ import annotations

import io
import logging
import secrets
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import config, keychain

pytestmark = pytest.mark.skipif(not keychain.available(),
                                reason="needs the macOS Keychain (`security`)")

ROUTE = "/api/settings/anthropic-key"


def _fake_key() -> str:
    # Key-shaped and unique per test, so a hit anywhere is unambiguous.
    return "sk-ant-api03-" + secrets.token_urlsafe(64)


@pytest.fixture
def test_service(monkeypatch):
    """A Keychain service nobody else uses, removed whatever the test did."""
    svc = f"Video AI Editor TEST c3-settings {uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(keychain, "SERVICE", svc)
    keychain._CACHE.clear()
    yield svc
    subprocess.run([keychain.SECURITY_BIN, "delete-generic-password", "-s", svc,
                    "-a", keychain.ACCOUNT], capture_output=True)
    keychain._CACHE.clear()
    assert subprocess.run([keychain.SECURITY_BIN, "find-generic-password", "-s", svc],
                          capture_output=True).returncode == 44, "test Keychain item leaked"


@pytest.fixture
def no_env_key(monkeypatch):
    """No key from the environment: the Keychain is the source."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("VAI_PROMPT_CLOUD", "1")


@pytest.fixture
def app_env(tmp_path, monkeypatch, isolated_user_data_dir):
    from video_ai_editor import storage, storage_project, main as _main
    from video_ai_editor.api.hardening import RATE, get_logger
    for mod in (config, storage, storage_project, _main):
        monkeypatch.setattr(mod, "WORKDIR", tmp_path)
    RATE.windows.clear()
    _main._STORES.clear()
    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.DEBUG)
    logger = get_logger()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    yield {"client": TestClient(_main.app), "workdir": tmp_path, "logs": log_buf,
           "data_dir": isolated_user_data_dir}
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    hits = []
    if not root.exists():
        return hits
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if needle in p.read_bytes():
                    hits.append(p)
            except OSError:
                pass
    return hits


# --------------------------------------------------------------- keychain.py

def test_keychain_round_trip_under_a_test_only_service(test_service):
    key = _fake_key()
    assert keychain.read() is None
    keychain.write(key)
    assert keychain.read() == key
    assert keychain.anthropic_key() == key
    assert keychain.mask(key) == f"sk-ant-…{key[-4:]}"
    assert keychain.delete() is True
    assert keychain.read() is None
    assert keychain.anthropic_key() == ""          # the memo was dropped
    assert keychain.delete() is False               # nothing left to remove


def test_the_key_never_rides_on_a_command_line(test_service, monkeypatch):
    """`security add-generic-password -w <key>` would publish the key in the
    argv every local process can read with `ps`; it must go over stdin."""
    seen: list[tuple[list[str], str | None]] = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        seen.append((list(args), kw.get("input")))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(keychain.subprocess, "run", spy)
    key = _fake_key()
    keychain.write(key)
    keychain.delete()
    assert seen, "no security call was made"
    assert all(key not in " ".join(argv) for argv, _ in seen)
    assert any(stdin and key in stdin for _, stdin in seen)


def test_a_sandboxed_home_still_reaches_the_login_keychain(test_service, monkeypatch, tmp_path):
    """The login Keychain belongs to the account, not to $HOME. With $HOME
    pointed at a sandbox (a test server, an isolating launcher) `security`
    found no keychain and blocked on a "create a keychain?" prompt until it
    was killed — measured: `HOME=<tmp> security -i add-generic-password …`
    never returned (exit 143 when killed)."""
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    key = _fake_key()
    t0 = time.monotonic()
    keychain.write(key)
    assert keychain.read() == key
    assert keychain.delete() is True
    assert time.monotonic() - t0 < 5


@pytest.mark.parametrize("bad", ["", "sk-live-123", 'sk-ant-abc" -X "', "sk-ant-" + "a" * 30 + "\nrm",
                                 "sk-ant-short"])
def test_a_malformed_key_is_refused_before_the_keychain_is_touched(test_service, bad):
    with pytest.raises(keychain.KeychainError):
        keychain.write(bad)
    assert keychain.read() is None


def test_an_explicitly_empty_env_key_keeps_the_keychain_out(test_service, monkeypatch):
    """ANTHROPIC_API_KEY="" (the test gate, the benchmark harness) is "no
    Claude": a key saved on this Mac must not leak into those runs."""
    keychain.write(_fake_key())
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert config.anthropic_api_key() == ""
    assert config.anthropic_key_source() == "disabled"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert config.anthropic_key_source() == "keychain"


# ------------------------------------------------------------------- routes

def test_save_reaches_claude_without_a_restart_and_the_key_never_leaves(
        test_service, no_env_key, app_env, monkeypatch):
    from video_ai_editor.agent.prompt.brains.cloud_plan import CloudBrain
    from video_ai_editor.api import settings_routes
    c: TestClient = app_env["client"]
    key = _fake_key()
    bodies: list[str] = []

    def call(method: str, url: str, **kw):
        r = c.request(method, url, **kw)
        bodies.append(r.text)
        bodies.extend(f"{k}: {v}" for k, v in r.headers.items())
        return r

    brain = CloudBrain()                          # built BEFORE the key exists,
    assert not brain.availability()["available"]  # like the router's cached one

    r = call("GET", ROUTE)
    assert r.status_code == 200 and r.json()["configured"] is False
    assert r.json()["source"] == "none" and r.json()["can_edit"] is True
    assert r.headers["cache-control"] == "no-store"

    r = call("POST", ROUTE, json={"key": key})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st == {"configured": True, "source": "keychain", "masked": f"sk-ant-…{key[-4:]}",
                  "keychain_available": True, "can_edit": True}
    assert keychain.read() == key

    # Hot reload: the same brain object, the chat resolver and the features
    # flag all see it now.
    assert brain.availability()["available"] is True
    assert config.anthropic_api_key() == key
    assert call("GET", "/api/features").json()["anthropic_key_set"] is True
    rows = {b["id"]: b for b in call("GET", "/api/prompt/brains?refresh=1").json()["brains"]}
    assert rows["claude"]["available"] is True

    # Test: success, and a rejection whose SDK message quotes the key.
    monkeypatch.setattr(settings_routes, "PROBE", lambda k: None)
    r = call("POST", ROUTE + "/test", json={})
    assert r.json() == {"ok": True, "message": "The key works. Claude is ready."}

    class AuthenticationError(Exception):
        status_code = 401

    def rejecting(k: str) -> None:
        raise AuthenticationError(f"invalid x-api-key {k}")
    monkeypatch.setattr(settings_routes, "PROBE", rejecting)
    r = call("POST", ROUTE + "/test", json={})
    assert r.json()["ok"] is False and "rejected" in r.json()["message"]

    # Error paths that could echo the body: wrong type, bad shape, too long.
    assert call("POST", ROUTE, json={"key": key + '"'}).status_code == 400
    assert call("POST", ROUTE, json={"key": [key]}).status_code == 400
    assert call("POST", ROUTE, content=('{"key": "%s' % key).encode(),
                headers={"content-type": "application/json"}).status_code == 400
    assert call("POST", ROUTE, json={"key": key * 60}).status_code == 413

    r = call("DELETE", ROUTE)
    assert r.status_code == 200 and r.json()["removed"] is True
    assert r.json()["configured"] is False and r.json()["masked"] is None
    assert keychain.read() is None
    assert brain.availability()["available"] is False

    # THE property: not in any response, header, log line, or file the app wrote.
    for text in bodies:
        assert key not in text and key[12:40] not in text
    logs = app_env["logs"].getvalue()
    assert key not in logs and key[12:40] not in logs
    assert logs, "the request log handler captured nothing — the log check would be vacuous"
    for root in (app_env["workdir"], app_env["data_dir"]):
        assert _files_containing(root, key.encode()) == []
        assert _files_containing(root, key[12:40].encode()) == []


def test_routes_are_loopback_only_json_only_and_behind_the_host_check(
        test_service, no_env_key, app_env):
    from video_ai_editor.main import app
    key = _fake_key()
    lan = TestClient(app, client=("192.168.1.20", 50000))
    assert lan.get(ROUTE).status_code == 403
    assert lan.post(ROUTE, json={"key": key}).status_code == 403
    assert lan.delete(ROUTE).status_code == 403
    assert lan.post(ROUTE + "/test", json={}).status_code == 403
    c: TestClient = app_env["client"]
    # A form or text/plain body is what a page can send without a preflight.
    assert c.post(ROUTE, content=f'{{"key": "{key}"}}'.encode(),
                  headers={"content-type": "text/plain"}).status_code == 415
    assert c.post(ROUTE, data={"key": key}).status_code == 415
    # SEC-REBIND: a DNS name that merely starts with 127. is still a name.
    assert c.get(ROUTE, headers={"host": "127.0.0.1.nip.io:8765"}).status_code == 421
    assert c.post(ROUTE, json={"key": key}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert keychain.read() is None


def test_an_env_key_is_reported_not_overwritten(test_service, monkeypatch, app_env):
    env_key = _fake_key()
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", env_key)
    c: TestClient = app_env["client"]
    st = c.get(ROUTE).json()
    assert st["source"] == "env" and st["can_edit"] is False
    assert st["masked"] == f"sk-ant-…{env_key[-4:]}" and env_key not in c.get(ROUTE).text
    assert c.post(ROUTE, json={"key": _fake_key()}).status_code == 409
    assert c.delete(ROUTE).status_code == 409
    assert keychain.read() is None


def test_a_page_on_another_localhost_port_cannot_touch_the_key(test_service, no_env_key, app_env):
    """REVIEW-C3-KEY-CORS-5173: a page served at http://localhost:5173 (Vite's
    default port — any other dev project the owner runs) could SET, READ,
    TEST and DELETE the key: CORS allowed that origin with credentials and the
    route guard only checked the loopback peer and the content type. The
    browser labels such a request `Sec-Fetch-Site: same-site` (same host,
    different port) and sends an Origin that is not the Host it talks to."""
    c: TestClient = app_env["client"]
    mine = _fake_key()
    keychain.write(mine)                           # the owner's key is saved
    hostile = {"origin": "http://localhost:5173", "sec-fetch-site": "same-site",
               "host": "127.0.0.1:8765"}
    assert c.get(ROUTE, headers=hostile).status_code == 403
    assert c.post(ROUTE, json={"key": _fake_key()}, headers=hostile).status_code == 403
    assert c.post(ROUTE + "/test", json={}, headers=hostile).status_code == 403
    assert c.delete(ROUTE, headers=hostile).status_code == 403
    # An Origin that differs from the Host is refused even with no fetch
    # metadata (older WebViews, scripted clients that forge nothing else).
    assert c.delete(ROUTE, headers={"origin": "http://localhost:5173",
                                    "host": "127.0.0.1:8765"}).status_code == 403
    assert keychain.read() == mine, "a cross-origin request changed the Keychain"
    # What the app itself sends still works: the packaged window (same
    # origin), and the Vite dev proxy — it rewrites Host to the backend's
    # (changeOrigin) but forwards the browser's `same-origin`.
    assert c.get(ROUTE, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert c.get(ROUTE, headers={"sec-fetch-site": "none"}).status_code == 200
    proxied = {"origin": "http://localhost:5173", "host": "127.0.0.1:8765",
               "sec-fetch-site": "same-origin"}
    assert c.post(ROUTE + "/test", json={}, headers=proxied).status_code == 200
    assert c.delete(ROUTE, headers={"origin": "http://127.0.0.1:8765",
                                    "host": "127.0.0.1:8765"}).status_code == 200
    assert keychain.read() is None
