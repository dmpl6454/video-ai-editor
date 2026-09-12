"""The four auth layers, the lockout, and the /api/pair/* routes.

Read alongside `src/video_ai_editor/api/auth.py`, whose docstring explains why
there are four layers rather than one. Every test here answers a question of
the form "what happens on a coffee-shop network?", because that is the only
place LAN mode is ever used.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lan_fixtures import CLIENT_HEADERS, lan_home, lan_on, lan_peer, pair_a_device  # noqa: F401

from video_ai_editor.main import app


@pytest.fixture(autouse=True)
def phone_feature_on(monkeypatch):
    """Arm the phone feature for every test in this module.

    This release ships with the iPhone companion temporarily gated off
    (`api/pairing.py::PHONE_PAIRING_ENABLED`, reversible by one line). Nothing
    below was weakened or deleted, because these tests are the feature's
    specification: they must keep proving that pairing, the four auth layers and
    the lockout all still work, so that flipping the flag back on is a decision
    and not a gamble.

    Declared here rather than inherited, so this file states the posture it
    tests. `tests/test_phone_feature_off.py` owns the other half — what the
    shipped build does with the flag closed.
    """
    monkeypatch.setenv("VAE_PHONE_PAIRING", "1")


# --- 1. the guard: nothing changes until LAN mode is on ----------------------

def test_default_posture_requires_no_auth(lan_home):
    """THE regression guard for the other 1338 tests.

    LAN off, loopback socket: every route answers exactly as it did in 0.5.0.
    If this ever fails, the shipped desktop app has silently grown an auth wall
    and nobody's `settings.json` can open it.
    """
    from video_ai_editor.api import pairing
    assert pairing.auth_required() is False

    c = TestClient(app)
    for path in ("/api/health", "/api/version", "/api/tools", "/api/sessions"):
        assert c.get(path).status_code == 200, path
    # And from a genuine off-machine peer, too — auth is gated on the posture,
    # not on who is calling.
    peer = lan_peer()
    assert peer.get("/api/health").status_code == 200


def test_default_posture_leaves_path_restriction_off(lan_home):
    """`git diff` promise: with no settings.json the allowlist is inert."""
    from pathlib import Path
    from video_ai_editor import config
    assert config.restrict_paths_active() is False
    # Resolved, but not rejected. (On macOS /etc is a symlink to /private/etc,
    # which is exactly the symlink-following the guard relies on when it IS on.)
    assert config.assert_path_allowed("/etc/hosts") == Path("/etc/hosts").resolve()


# --- 2. bearer tokens --------------------------------------------------------

def test_lan_peer_without_a_token_is_rejected(lan_on):
    r = lan_peer().get("/api/sessions", headers=CLIENT_HEADERS)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_lan_peer_with_a_paired_token_is_accepted(lan_on):
    token = pair_a_device()
    r = lan_peer().get("/api/sessions",
                       headers={**CLIENT_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_loopback_still_bypasses_auth_in_lan_mode(lan_on):
    """The desktop's own React frontend holds no token and never will — it is
    served by this process to a webview on the same machine."""
    assert TestClient(app).get("/api/sessions").status_code == 200


def test_revoking_a_device_kills_its_token(lan_on):
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    auth_headers = {**CLIENT_HEADERS, "Authorization": f"Bearer {token}"}
    assert lan_peer().get("/api/sessions", headers=auth_headers).status_code == 200
    assert pairing.revoke_device(device_id) is True
    assert lan_peer().get("/api/sessions", headers=auth_headers).status_code == 401


# --- 3. the client header a hostile page cannot send -------------------------

def test_missing_client_header_is_refused_even_with_a_token(lan_on):
    """`X-VAE-Client` is not a CORS-simple header, so a browser must preflight
    to send it and this app's CORS policy only allows localhost:5173. A page
    that cannot preflight cannot reach an API route, token or no token."""
    token = pair_a_device()
    r = lan_peer().get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "client header" in r.json()["error"]["message"].lower()


# --- 4. Host validation (DNS rebinding) --------------------------------------

@pytest.mark.parametrize("host", ["evil.com", "attacker.example:8765",
                                  "my-mac.local:8765", ""])
def test_rebinding_hosts_are_misdirected(lan_home, host):
    """A page on evil.com can re-resolve its own name to the Mac's LAN IP, but
    it cannot change the `Host` header the browser sends. We only answer to a
    loopback name or a bare IP literal — enforced on /api/pair/* even with LAN
    mode off, because minting and reading credentials is what such an attack
    would be for."""
    r = lan_peer().get("/api/pair/whoami", headers={"Host": host})
    assert r.status_code == 421
    assert r.json()["error"]["code"] == "MISDIRECTED_REQUEST"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765",
                                  "10.120.2.82:8765", "192.168.1.9", "testserver"])
def test_direct_addresses_are_accepted(lan_home, host):
    r = lan_peer().get("/api/pair/whoami", headers={"Host": host})
    assert r.status_code == 200


def test_host_check_applies_to_every_route_in_the_DEFAULT_posture(lan_home):
    """The rebinding defence runs with LAN mode OFF, which is the only posture
    that ships.

    This assertion is the inverse of the one it replaces. That test pinned the
    old `if armed or is_pair_route:` gate, i.e. it asserted that the cheapest
    control in this file was switched off for 100% of users — while the
    CHANGELOG claimed "a page on evil.com that re-resolves its own name to your
    Mac's LAN address now gets a 421 instead of a session". It did not: on a
    default build `POST /api/sessions` with `Host: evil.com` returned a session
    id and the follow-up `dispatch` reached the handler, with the path
    allowlist disarmed and `add_clip.src` able to read any file on the Mac.

    Nothing the desktop does is affected: the PyWebView shell and the Vite dev
    proxy both send a loopback Host, and `/livez` + `/readyz` stay in
    `_OPEN_PATHS` so a monitor with its own Host header is untouched.
    """
    peer = lan_peer()
    for path in ("/api/health", "/api/version", "/api/sessions"):
        assert peer.get(path, headers={"Host": "whatever.internal"}).status_code == 421, path
    for path in ("/livez", "/readyz"):
        assert peer.get(path, headers={"Host": "whatever.internal"}).status_code == 200, path


def test_the_default_posture_still_serves_a_loopback_host(lan_home):
    """The other half of the promise above: 0.5.0 behaviour is preserved for
    every Host the app itself ever produces."""
    peer = lan_peer()
    for host in ("127.0.0.1:8765", "localhost:5173", "192.168.1.9:8765", "testserver"):
        assert peer.get("/api/health", headers={"Host": host}).status_code == 200, host


def test_host_check_applies_to_every_route_in_lan_mode(lan_on):
    assert lan_peer().get("/api/health",
                          headers={"Host": "evil.com"}).status_code == 421


def test_cross_site_fetches_are_refused(lan_home):
    """Browsers label a request initiated from another origin. Native clients
    send no such header at all, so this costs them nothing."""
    r = lan_peer().get("/api/pair/whoami",
                       headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


# --- 5. middleware ordering --------------------------------------------------

def test_cors_preflight_survives_lan_mode(lan_on):
    """Auth must sit INSIDE CORS. Installed the other way round, an OPTIONS
    preflight would hit auth, 401, and every browser request would fail citing
    CORS while the real cause was authentication."""
    r = TestClient(app).options(
        "/api/sessions",
        headers={"Origin": "http://localhost:5173",
                 "Access-Control-Request-Method": "GET",
                 "Access-Control-Request-Headers": "x-vae-client"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_unauthenticated_flood_is_401_not_429(lan_on):
    """Auth must sit OUTSIDE the rate limiter. The other order absorbs an
    unauthenticated flood into a per-path rate bucket and answers "too many
    requests", which tells the user's phone the wrong thing and lets an
    attacker's traffic share a bucket with the real client's."""
    from video_ai_editor.api.hardening import RATE
    RATE.windows.clear()
    peer = lan_peer()
    codes = {peer.get("/api/sessions", headers=CLIENT_HEADERS).status_code
             for _ in range(10)}
    assert codes == {401}
    assert not any("/api/sessions" in k for k in RATE.windows)


# --- 6. the auth lockout -----------------------------------------------------

def test_repeated_failures_lock_the_peer_out(lan_on):
    from video_ai_editor.api import auth
    peer = lan_peer()
    for _ in range(auth.LOCKOUT_THRESHOLD):
        peer.get("/api/sessions", headers=CLIENT_HEADERS)
    r = peer.get("/api/sessions", headers=CLIENT_HEADERS)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_LOCKOUT"


def test_media_failures_never_count_toward_the_lockout(lan_on):
    """One filmstrip paint is ~24 thumbnail requests fired at once. If a stale
    media token counted, a phone whose token had merely expired would lock
    itself out in under a second and then show a green connection banner over a
    dead app."""
    from video_ai_editor.api import auth
    peer = lan_peer()
    path = "/api/sessions/s_abc1234567/files/previews/nope.mp4"
    for _ in range(auth.LOCKOUT_THRESHOLD * 2):
        assert peer.get(path).status_code == 401
    assert auth._locked_out("192.168.1.50") is False
    # …and a real request from the same peer still gets the honest answer.
    assert peer.get("/api/sessions",
                    headers=CLIENT_HEADERS).json()["error"]["code"] == "UNAUTHORIZED"


def test_a_successful_auth_clears_the_counter(lan_on):
    from video_ai_editor.api import auth
    token = pair_a_device()
    peer = lan_peer()
    for _ in range(auth.LOCKOUT_THRESHOLD - 1):
        peer.get("/api/sessions", headers=CLIENT_HEADERS)
    peer.get("/api/sessions",
             headers={**CLIENT_HEADERS, "Authorization": f"Bearer {token}"})
    assert auth._locked_out("192.168.1.50") is False


# --- 7. media tokens ---------------------------------------------------------

def _media_path(tmp_path, monkeypatch) -> str:
    """A real, reachable media URL whose route answers 404 once auth passes —
    so a 401 can only have come from the middleware."""
    from video_ai_editor import config, storage
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    return "/api/sessions/s_abc1234567/files/previews/nope.mp4"


def test_media_token_works_on_media_paths_without_the_client_header(
        lan_on, tmp_path, monkeypatch):
    """AVURLAsset and the native image loader drop custom headers across
    redirects and byte-range refetches, and there is no simulator here to find
    out which of them do — so media authenticates with a short-lived `?k=`
    instead, and is exempt from the header rule."""
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    k = pairing.mint_media_token(device_id)["token"]
    path = _media_path(tmp_path, monkeypatch)
    assert lan_peer().get(f"{path}?k={k}").status_code == 404


def test_media_token_is_refused_on_a_non_media_path(lan_on):
    """It is a media credential, not a session one: it must not open /edl,
    /dispatch or the chat history."""
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    k = pairing.mint_media_token(device_id)["token"]
    r = lan_peer().get(f"/api/sessions?k={k}", headers=CLIENT_HEADERS)
    assert r.status_code == 401


def test_media_token_dies_with_its_device(lan_on, tmp_path, monkeypatch):
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    k = pairing.mint_media_token(device_id)["token"]
    path = _media_path(tmp_path, monkeypatch)
    assert lan_peer().get(f"{path}?k={k}").status_code == 404
    pairing.revoke_device(device_id)
    assert lan_peer().get(f"{path}?k={k}").status_code == 401


def test_expired_media_token_is_refused(lan_on, monkeypatch):
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    monkeypatch.setattr(pairing, "MEDIA_TOKEN_TTL_S", -1.0)
    k = pairing.mint_media_token(device_id)["token"]
    assert pairing.verify_media_token(k) is None


def test_forged_media_token_is_refused(lan_on):
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    forged = f"{device_id}.{int(time.time()) + 600}.{'0' * 32}"
    assert pairing.verify_media_token(forged) is None


# --- 8. the pair routes ------------------------------------------------------

def test_pair_new_is_desktop_only(lan_on):
    """A paired phone must not be able to pair a second phone, and a LAN peer
    must not be able to pair itself."""
    token = pair_a_device()
    r = lan_peer().post("/api/pair/new",
                        headers={**CLIENT_HEADERS,
                                 "Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_pair_new_refuses_while_lan_is_off(lan_home):
    """Minting a code for a socket no phone can reach would make the user
    blame the phone."""
    r = TestClient(app).post("/api/pair/new")
    assert r.status_code == 409
    assert r.json()["error"]["details"]["error"] == "lan_disabled"


def test_pair_new_then_claim_then_use(lan_on, monkeypatch):
    from video_ai_editor.api import pairing
    monkeypatch.setattr(pairing, "host_candidates", lambda: ["192.168.1.9"])
    pairing.set_server_port(8765)

    minted = TestClient(app).post("/api/pair/new").json()
    assert minted["payload"] == (
        f"vaepair:v=1&h=192.168.1.9&p=8765&c={minted['code']}")

    claimed = lan_peer().post("/api/pair/claim", headers=CLIENT_HEADERS,
                              json={"code": minted["code"],
                                    "device_name": "Owner iPhone"}).json()
    assert claimed["name"] == "Owner iPhone"
    assert claimed["server"]["job_workers"] >= 1

    who = lan_peer().get("/api/pair/whoami",
                         headers={**CLIENT_HEADERS,
                                  "Authorization": f"Bearer {claimed['token']}"}).json()
    assert who["device"]["id"] == claimed["id"]
    assert who["server"]["max_upload_bytes"] > 0


def test_a_pair_code_can_only_be_claimed_once(lan_on):
    from video_ai_editor.api import pairing
    code = pairing.new_pair_code()
    peer = lan_peer()
    body = {"code": code, "device_name": "iPhone"}
    assert peer.post("/api/pair/claim", headers=CLIENT_HEADERS,
                     json=body).status_code == 200
    second = peer.post("/api/pair/claim", headers=CLIENT_HEADERS, json=body)
    assert second.status_code == 401
    assert second.json()["error"]["details"]["error"] == "bad_code"


def test_an_expired_pair_code_is_refused(lan_on, monkeypatch):
    """Ten minutes, because the code is a secret displayed on a screen and a
    photograph of it should not be a standing invitation."""
    from video_ai_editor.api import pairing
    monkeypatch.setattr(pairing, "PAIR_CODE_TTL_S", -1.0)
    code = pairing.new_pair_code()
    assert pairing.claim_pair_code(code, "iPhone") is None


def test_pair_info_lists_devices_and_the_settings_file(lan_on, monkeypatch):
    from video_ai_editor.api import pairing
    monkeypatch.setattr(pairing, "host_candidates", lambda: ["192.168.1.9"])
    pair_a_device("Owner iPhone")
    info = TestClient(app).get("/api/pair/info").json()
    assert [d["name"] for d in info["devices"]] == ["Owner iPhone"]
    assert info["hosts"] == ["192.168.1.9"]
    assert info["settings_path"].endswith("settings.json")
    assert info["auth_required"] is True


def test_revoke_route_is_desktop_only_and_removes_the_device(lan_on):
    from video_ai_editor.api import pairing
    token = pair_a_device()
    device_id = pairing.device_for_token(token)["id"]
    denied = lan_peer().post("/api/pair/revoke",
                             headers={**CLIENT_HEADERS,
                                      "Authorization": f"Bearer {token}"},
                             json={"device_id": device_id})
    assert denied.status_code == 403
    ok = TestClient(app).post("/api/pair/revoke", json={"device_id": device_id})
    assert ok.status_code == 200
    assert ok.json()["devices"] == []


def test_media_token_route_needs_a_device(lan_on):
    """Loopback is trusted by address and needs no media token; asking for one
    from there is a client bug worth naming rather than a silent empty token."""
    assert TestClient(app).post("/api/pair/media_token").status_code == 400
    token = pair_a_device()
    r = lan_peer().post("/api/pair/media_token",
                        headers={**CLIENT_HEADERS,
                                 "Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["ttl_s"] == 60.0


# --- 9. the routes that are neither /api/pair nor a browser page -------------

def test_mcp_is_behind_the_same_wall(lan_on):
    """`/mcp` is a second front door onto the same dispatcher — it must not be
    reachable from the LAN just because its path does not start with /api."""
    peer = lan_peer()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert peer.post("/mcp", headers=CLIENT_HEADERS, json=body).status_code == 401
    token = pair_a_device()
    ok = peer.post("/mcp", json=body,
                   headers={**CLIENT_HEADERS, "Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_liveness_probes_answer_without_auth(lan_on):
    """The only way to tell "the Mac is asleep" from "the Mac said no" from
    outside — the phone's Connect screen probes these before it blames itself."""
    peer = lan_peer()
    assert peer.get("/livez").status_code == 200
    assert peer.get("/readyz").status_code in (200, 503)


def test_the_chat_stream_still_streams_through_the_auth_middleware(lan_on,
                                                                   monkeypatch,
                                                                   tmp_path):
    """A second BaseHTTPMiddleware in front of an SSE route is exactly the kind
    of change that buffers a stream into one blob, and the failure looks like
    "the phone's chat never types" rather than like a middleware bug.

    Framing is checked here too: plain `data: {json}\\n\\n` with NO `event:`
    line — `sse-starlette` is deliberately not used on this route, and the
    companion's parser is written to what this actually emits.
    """
    from video_ai_editor import config, main, storage

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)

    async def fake_turn(store, message, history, ui_state=None):
        yield {"type": "text_delta", "text": "hel"}
        yield {"type": "text_delta", "text": "lo"}
        yield {"type": "done"}

    monkeypatch.setattr(main, "chat_turn", fake_turn)

    c = TestClient(app)
    sid = c.post("/api/sessions", json={"name": "chat"}).json()["id"]
    with c.stream("POST", f"/api/sessions/{sid}/chat",
                  json={"message": "hi"}) as r:
        assert r.status_code == 200
        raw = "".join(chunk for chunk in r.iter_text())
    assert "event:" not in raw
    frames = [f for f in raw.split("\n\n") if f.strip()]
    assert [f.split("data: ", 1)[1] for f in frames] == [
        '{"type": "text_delta", "text": "hel"}',
        '{"type": "text_delta", "text": "lo"}',
        '{"type": "done"}']


def test_pair_code_carries_the_port_the_request_arrived_on(monkeypatch):
    """A bare `uvicorn --port 8000` run never calls set_server_port(), and the
    payload used to fall through to the packaged default of 8765 — a QR
    pointing at a port with nothing behind it. The request is ground truth."""
    from video_ai_editor.api import pairing

    monkeypatch.setattr(pairing, "_server_port", 0, raising=False)
    monkeypatch.delenv("VAE_PORT", raising=False)

    class _Req:
        def __init__(self, port):
            self.scope = {"server": ("192.168.0.23", port)}
            self.url = type("U", (), {"port": port})()

    assert pairing.observed_port(_Req(8000)) == 8000
    assert pairing.server_port(pairing.observed_port(_Req(8000))) == 8000
    # An explicit recorded port always wins over the observation.
    monkeypatch.setattr(pairing, "_server_port", 8765, raising=False)
    assert pairing.server_port(pairing.observed_port(_Req(8000))) == 8765
