"""The shipped posture: the iPhone companion is temporarily switched off.

This release ships the desktop editor as a normal standalone editor. The phone /
local-network pairing feature is not deleted — the code, `tests/test_pair_auth.py`
and the whole `mobile/` app stay in the tree — it is gated behind ONE reversible
flag, `api/pairing.py::PHONE_PAIRING_ENABLED` (env override
`VAE_PHONE_PAIRING`). A future release flips it back.

So this file has two jobs, and the second is as important as the first:

  1. Prove the OFF state through production shapes — real HTTP status codes and
     the app's real error envelope, the real `/api/version` payload, the real
     bytes of settings.json on disk, the real return value of the function that
     chooses desktop.py's bind address. Never by reading a comment or a flag
     back to itself.
  2. Prove the flag is a DOOR, not a one-way street: the last section runs the
     same calls with `VAE_PHONE_PAIRING=1` and asserts today's behaviour comes
     back. A gate nobody can verify re-opening is a deletion with extra steps.

`tests/test_pair_auth.py` owns the ON state in depth; it arms the flag for its
own module.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lan_fixtures import reset_pairing_state

from video_ai_editor.main import app

#: Every route the pair router publishes, as (method, path, json body). The list
#: is the point: the guard is one router-level dependency, so a route added later
#: is covered automatically — but only if this list is kept honest, which
#: `test_no_pair_route_escapes_the_list` enforces against the live route table.
PAIR_ROUTES = [
    ("GET", "/api/pair/info", None),
    ("POST", "/api/pair/lan", {"enabled": True}),
    ("POST", "/api/pair/new", None),
    ("POST", "/api/pair/claim", {"code": "0" * 32, "device_name": "iPhone"}),
    ("GET", "/api/pair/whoami", None),
    ("GET", "/api/pair/devices", None),
    ("POST", "/api/pair/revoke", {"device_id": "abc"}),
    ("POST", "/api/pair/media_token", None),
]

#: A settings.json shaped like the one an upgrading user already has on disk:
#: LAN off, one remembered iPhone. It must survive this build untouched.
EXISTING_SETTINGS = {
    "version": 1,
    "lan_enabled": False,
    "devices": [{
        "id": "d00dfeed",
        "name": "Owner's iPhone",
        "token_sha256": "a" * 64,
        "created_at": 1_700_000_000.0,
        "last_seen": 1_700_000_600.0,
    }],
}


@pytest.fixture
def off(monkeypatch, tmp_path):
    """The shipped posture, with settings.json redirected to a temp dir.

    Both env vars are DELETED rather than set to "0": the shipped `.app` inherits
    launchd's environment, which has neither, and that is the state under test.
    Redirecting `settings_path` matters for the same reason `lan_fixtures` does
    it — without it these tests would read and could rewrite the developer's real
    `~/Library/Application Support/Video AI Editor/settings.json`.
    """
    from video_ai_editor.api import pairing

    monkeypatch.delenv("VAE_PHONE_PAIRING", raising=False)
    monkeypatch.delenv("VAE_LAN", raising=False)
    home = tmp_path / "appdata"
    home.mkdir()
    monkeypatch.setattr(pairing, "settings_path", lambda: home / "settings.json")
    reset_pairing_state()
    yield home
    reset_pairing_state()


@pytest.fixture
def on(off, monkeypatch):
    """The same world with the feature turned back on — the reversibility half."""
    monkeypatch.setenv("VAE_PHONE_PAIRING", "1")
    yield off
    reset_pairing_state()


def write_settings(home, data: dict) -> bytes:
    """Put a settings.json on disk and return its exact bytes."""
    path = home / "settings.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return path.read_bytes()


# --- 1. the routes -----------------------------------------------------------

@pytest.mark.parametrize("method,path,body", PAIR_ROUTES,
                         ids=[f"{m}{p}" for m, p, _ in PAIR_ROUTES])
def test_every_pair_route_answers_404_in_the_standard_envelope(off, method, path, body):
    """404, not 403: in this build the companion does not exist, and 403 would
    advertise a feature that is merely withheld."""
    r = TestClient(app).request(method, path, json=body)
    assert r.status_code == 404, f"{method} {path}"
    error = r.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert "not available in this build" in error["message"].lower()
    # The envelope's own contract (api/hardening.py): a correlatable request id,
    # in the body and on the header. This is what proves the 404 came from this
    # app's handler and not from Starlette's unrouted-path fallback.
    assert error["request_id"]
    assert r.headers["X-Request-ID"] == error["request_id"]


def test_an_invalid_body_still_gets_the_404_and_not_a_422(off):
    """The guard must win over request validation.

    Otherwise the shipped build leaks the shape of a feature it does not have: a
    422 listing `enabled` as a required field says "this endpoint is real, you
    called it wrong".
    """
    r = TestClient(app).post("/api/pair/lan", json={"nonsense": 1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_no_pair_route_escapes_the_list(off):
    """Keeps PAIR_ROUTES honest against the live route table, and proves the
    router is still MOUNTED — the 404 above is a routed, logged, enveloped
    response, not an accident of an unregistered path."""
    live = {route.path for route in app.routes
            if getattr(route, "path", "").startswith("/api/pair/")}
    assert live == {path for _, path, _ in PAIR_ROUTES}


# --- 2. /api/version, the frontend's only channel ----------------------------

def test_version_reports_phone_pairing_false(off):
    """The ONE signal the UI uses to decide the phone affordance exists."""
    body = TestClient(app).get("/api/version").json()
    assert body["phone_pairing"] is False
    # The pre-existing keys keep their meaning and their types.
    assert isinstance(body["version"], str) and body["version"]
    assert isinstance(body["build"], str)


# --- 2b. nothing the build publishes or says advertises the companion --------

#: The request models only the pair routes use. Dropping the paths from the
#: schema while leaving these behind would still publish the feature's shape.
PAIR_ONLY_MODELS = ("LanRequest", "ClaimRequest", "RevokeRequest")


def test_openapi_does_not_publish_the_pair_routes_or_their_models(off):
    """The published schema is a contract, and it must not offer eight endpoints
    that every real verb answers 404 on — that reads as "pairing is present and
    merely broken", the exact signal the 404 is chosen to avoid."""
    schema = TestClient(app).get("/openapi.json").json()

    assert [p for p in schema["paths"] if p.startswith("/api/pair/")] == []
    models = schema.get("components", {}).get("schemas", {})
    assert [m for m in PAIR_ONLY_MODELS if m in models] == []
    # The filter is surgical, not a blunt instrument: the rest of the API is
    # still documented, and a model shared with routes that remain survives.
    assert "/api/version" in schema["paths"]
    assert "HTTPValidationError" in models
    # /docs and /redoc render whatever this schema says, so they follow it.
    assert TestClient(app).get("/docs").status_code == 200


def test_the_misdirected_host_error_does_not_point_at_a_panel_that_is_not_there(off):
    """The Host allowlist fires in EVERY posture, including this one, so its
    message is reachable in a build with no Phone panel. It must not send the
    user looking for a UI element this build does not render."""
    r = TestClient(app).get("/api/version", headers={"Host": "my-mac.local"})

    assert r.status_code == 421
    error = r.json()["error"]
    assert error["code"] == "MISDIRECTED_REQUEST"      # unchanged contract
    assert "phone" not in error["message"].lower()
    # It still has to be actionable: say the address that does work.
    assert "127.0.0.1" in error["message"]


# --- 3. the toggle cannot be written, and the user's file is not touched ------

def test_set_lan_enabled_refuses_and_leaves_settings_json_byte_identical(off):
    from video_ai_editor.api import pairing

    before = write_settings(off, EXISTING_SETTINGS)
    path = off / "settings.json"
    stat_before = path.stat()

    with pytest.raises(ValueError):
        pairing.set_lan_enabled(True)

    assert path.read_bytes() == before, "the user's settings.json was rewritten"
    assert path.stat().st_mtime == stat_before.st_mtime
    # The remembered device is still remembered, ready for the day the flag flips.
    assert json.loads(before)["devices"][0]["id"] == "d00dfeed"


def test_set_lan_enabled_does_not_create_a_settings_file_at_all(off):
    """A fresh install must stay fresh: refusing before the write means a build
    with no phone feature never writes a file the user did not ask for."""
    from video_ai_editor.api import pairing

    with pytest.raises(ValueError):
        pairing.set_lan_enabled(True)
    # Turning it OFF is refused too — neither direction is meaningful here, and
    # both would rewrite the one record of which phones are paired.
    with pytest.raises(ValueError):
        pairing.set_lan_enabled(False)
    assert not (off / "settings.json").exists()


def test_lan_enabled_is_false_even_when_settings_json_says_true(off):
    """An upgrading user who had the toggle on gets the shipped posture, and
    their file keeps saying true so re-enabling restores their real setting."""
    from video_ai_editor.api import pairing

    before = write_settings(off, {**EXISTING_SETTINGS, "lan_enabled": True})
    assert pairing.lan_enabled() is False
    assert pairing.load_settings()["lan_enabled"] is True, "reading must not rewrite"
    assert (off / "settings.json").read_bytes() == before


def test_the_dev_env_override_cannot_reopen_the_feature(off, monkeypatch):
    """`VAE_LAN=1` is the dev/test LAN override. It is not a second door: the
    ship gate is the single source of truth, so it outranks it."""
    from video_ai_editor.api import pairing

    monkeypatch.setenv("VAE_LAN", "1")
    assert pairing.lan_enabled() is False
    assert TestClient(app).get("/api/pair/info").status_code == 404


def test_auth_required_stays_honest_about_a_public_socket(off):
    """Off means "no phone companion", NOT "no authentication".

    The flag must not be able to strip auth from a socket a stranger can reach —
    an operator running `uvicorn --host 0.0.0.0` by hand still gets it.
    """
    from video_ai_editor.api import pairing

    assert pairing.auth_required() is False
    pairing.mark_bound_public(True)
    assert pairing.auth_required() is True


# --- 4. the shipped build cannot put itself on the network -------------------

def test_bind_host_is_loopback_even_for_a_public_vae_host(off):
    from video_ai_editor import desktop

    assert desktop._resolve_bind_host("0.0.0.0") == ("127.0.0.1", False)
    assert desktop._resolve_bind_host("10.0.0.5") == ("127.0.0.1", False)
    assert desktop._resolve_bind_host("127.0.0.1") == ("127.0.0.1", False)


def test_bind_host_is_loopback_even_when_settings_json_enables_lan(off):
    from video_ai_editor import desktop

    write_settings(off, {**EXISTING_SETTINGS, "lan_enabled": True})
    assert desktop._resolve_bind_host("127.0.0.1") == ("127.0.0.1", False)
    assert desktop._resolve_bind_host("0.0.0.0") == ("127.0.0.1", False)


# --- 5. the flag is a door: everything above comes back ----------------------

def test_pair_routes_are_reachable_again_when_the_flag_is_on(on):
    """Not 404 any more. `/whoami` is the one pair route a loopback caller needs
    no token for, so a 200 here is the feature actually working end to end."""
    assert TestClient(app).get("/api/pair/whoami").status_code == 200
    # And a desktop-only route is refused for the RIGHT reason: a LAN peer is not
    # the person at the Mac. 403, not 404 — the feature exists again.
    peer = TestClient(app, client=("192.168.1.50", 51234))
    assert peer.get("/api/pair/devices", headers={"X-VAE-Client": "1"}).status_code == 403


def test_version_reports_phone_pairing_true_when_the_flag_is_on(on):
    assert TestClient(app).get("/api/version").json()["phone_pairing"] is True


def test_openapi_documents_the_pair_routes_again_when_the_flag_is_on(on):
    """The schema is filtered by posture, not stripped: a build that serves the
    companion documents it, models included."""
    schema = TestClient(app).get("/openapi.json").json()

    assert {path for _, path, _ in PAIR_ROUTES} <= set(schema["paths"])
    models = schema.get("components", {}).get("schemas", {})
    assert all(m in models for m in PAIR_ONLY_MODELS)


def test_the_misdirected_host_error_names_the_phone_panel_again_when_on(on):
    """With the companion present, the Phone panel IS where a user reads the LAN
    IP, so the advice that was a dead end in the shipped build is correct here."""
    r = TestClient(app).get("/api/version", headers={"Host": "my-mac.local"})

    assert r.status_code == 421
    assert r.json()["error"]["code"] == "MISDIRECTED_REQUEST"
    assert "phone panel" in r.json()["error"]["message"].lower()


def test_the_toggle_writes_and_reads_again_when_the_flag_is_on(on):
    from video_ai_editor.api import pairing

    write_settings(on, EXISTING_SETTINGS)
    result = pairing.set_lan_enabled(True)
    assert result["lan_enabled"] is True
    assert pairing.lan_enabled() is True
    assert json.loads((on / "settings.json").read_text())["lan_enabled"] is True
    # The device the OFF build refused to touch is still there afterwards.
    assert [d["id"] for d in pairing.list_devices()] == ["d00dfeed"]


def test_bind_host_honours_the_operator_again_when_the_flag_is_on(on):
    from video_ai_editor import desktop

    assert desktop._resolve_bind_host("10.0.0.5") == ("10.0.0.5", True)
    assert desktop._resolve_bind_host("127.0.0.1") == ("127.0.0.1", False)
    write_settings(on, {**EXISTING_SETTINGS, "lan_enabled": True})
    assert desktop._resolve_bind_host("127.0.0.1") == ("0.0.0.0", True)
