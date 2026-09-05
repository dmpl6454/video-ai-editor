"""LAN mode as a persisted, restartable posture — not an environment variable.

The first design read `VAE_LAN` from the environment. A double-clicked `.app`
inherits launchd's environment, which has no such variable and no user-facing
way to acquire one, so the toggle would have been unreachable in the only build
that ships while `/api/pair/new` refused to mint without it. These tests pin
the file-backed version, the bind-address split, and the fact that arming the
posture also arms the filesystem allowlist — in BOTH directions, because a
guard that cannot be turned off breaks tools that worked yesterday.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lan_fixtures import CLIENT_HEADERS, lan_home, lan_peer, pair_a_device  # noqa: F401

from video_ai_editor.main import app


# --- 1. the toggle round-trips through a file --------------------------------

def test_pair_lan_toggle_round_trips(lan_home):
    from video_ai_editor.api import pairing
    assert pairing.lan_enabled() is False

    c = TestClient(app)
    on = c.post("/api/pair/lan", json={"enabled": True}).json()
    assert on["lan_enabled"] is True
    # The bind address is chosen once, in desktop.py::main, before uvicorn
    # starts. Turning the toggle on cannot move a listening socket, so the
    # panel has to say "restart" rather than show a QR for 127.0.0.1.
    assert on["restart_required"] is True
    assert pairing.lan_enabled() is True

    # Persisted, not just in memory: a fresh read of the file agrees.
    saved = pairing.load_settings()
    assert saved["lan_enabled"] is True
    assert (lan_home / "settings.json").exists()

    off = c.post("/api/pair/lan", json={"enabled": False}).json()
    assert off["lan_enabled"] is False
    assert pairing.lan_enabled() is False


def test_lan_toggle_is_desktop_only(lan_home):
    assert lan_peer().post("/api/pair/lan",
                           json={"enabled": True}).status_code == 403


def test_a_corrupt_settings_file_reads_as_defaults(lan_home):
    """A broken settings file must fail closed on the feature, not take the
    editor down — the user would have no way to repair it from inside the app."""
    from video_ai_editor.api import pairing
    (lan_home / "settings.json").write_text("{not json", encoding="utf-8")
    pairing._cache = None
    assert pairing.lan_enabled() is False
    assert pairing.list_devices() == []


def test_turning_lan_off_cannot_disarm_auth_on_a_public_socket(lan_home):
    """The rung the toggle cannot lower.

    Flipping the switch off does not un-bind a socket that is already
    listening on 0.0.0.0, so it must not be able to remove the authentication
    in front of one either.
    """
    from video_ai_editor.api import pairing
    pairing.mark_bound_public(True)
    pairing.set_lan_enabled(False)
    assert pairing.lan_enabled() is False
    assert pairing.auth_required() is True
    assert lan_peer().get("/api/health",
                          headers=CLIENT_HEADERS).status_code == 401


# --- 2. path restriction follows the posture, both ways ----------------------

def test_lan_mode_forces_path_restriction_on(lan_home):
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    assert config.restrict_paths_active() is False
    pairing.set_lan_enabled(True)
    assert config.restrict_paths_active() is True
    with pytest.raises(ValueError, match="outside the allowed roots"):
        config.assert_path_allowed("/etc/hosts")


def test_turning_lan_off_releases_path_restriction(lan_home):
    """The other direction matters just as much. A guard that stays armed after
    the feature is switched off turns "works on my Mac" into 400s on tools the
    user was using yesterday, with no visible cause."""
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    pairing.set_lan_enabled(True)
    pairing.set_lan_enabled(False)
    assert config.restrict_paths_active() is False
    assert config.assert_path_allowed("/etc/hosts") == Path("/etc/hosts").resolve()


def test_workdir_is_resolved_per_call_not_snapshotted(lan_home, tmp_path, monkeypatch):
    """The fixtures monkeypatch `config.WORKDIR` AFTER the module is imported.

    A guard that captured WORKDIR at import time would reject every legitimate
    session path in the suite the moment restriction was armed — and the
    failure would look like a path bug, not a snapshot bug.
    """
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    pairing.set_lan_enabled(True)
    inside = tmp_path / "s_abc" / "uploads" / "clip.mp4"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert config.assert_path_allowed(inside) == inside.resolve()


def test_the_write_allowlist_is_narrower_than_the_read_one(lan_home):
    """Reading the wrong file leaks data; writing the wrong file — a shell rc,
    a LaunchAgent plist — executes code at the next login."""
    from video_ai_editor import config
    read_roots = {str(r) for r in config.allowed_read_roots()}
    write_roots = {str(r) for r in config.allowed_write_roots()}
    assert write_roots < read_roots
    pictures = Path.home() / "Pictures"
    if pictures.is_dir():
        assert str(pictures.resolve()) in read_roots
        assert str(pictures.resolve()) not in write_roots


def test_home_media_folders_still_work_under_restriction(lan_home, monkeypatch):
    """Forcing the allowlist on must not break tools that worked yesterday.

    ~/Movies and ~/Downloads are where footage actually lives; if importing
    from them 400s, LAN mode is unusable and the user's only clue is a generic
    error on a path they can see in Finder.
    """
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    pairing.set_lan_enabled(True)
    movies = Path.home() / "Movies"
    if not movies.is_dir():
        pytest.skip("no ~/Movies on this machine")
    assert config.assert_path_allowed(movies / "a.mp4")
    assert config.assert_write_path_allowed(movies / "captions.srt")


def test_allowed_roots_escape_hatch_is_honoured_at_runtime(lan_home, tmp_path,
                                                           monkeypatch):
    """VAI_ALLOWED_ROOTS has to be read live, not at import: LAN mode arms the
    allowlist long after config.py was imported, so an import-time snapshot of
    the variable would silently ignore the user's own escape hatch."""
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    vault = tmp_path / "footage"
    vault.mkdir()
    monkeypatch.setenv("VAI_ALLOWED_ROOTS", str(vault))
    pairing.set_lan_enabled(True)
    assert config.assert_path_allowed(vault / "a.mp4") == (vault / "a.mp4").resolve()


# --- 3. the bind-address split ------------------------------------------------

def test_bind_host_splits_from_the_window_url(lan_home, monkeypatch):
    """desktop.py must bind 0.0.0.0 while the webview and the JS bridge keep
    dialling loopback — _Api posts to /vo_record, and that request must never
    leave the machine."""
    from video_ai_editor import desktop
    from video_ai_editor.api import pairing

    assert desktop._resolve_bind_host("127.0.0.1") == ("127.0.0.1", False)
    pairing.set_lan_enabled(True)
    assert desktop._resolve_bind_host("127.0.0.1") == ("0.0.0.0", True)


def test_an_explicit_non_loopback_vae_host_arms_auth(lan_home):
    """Someone who sets VAE_HOST=0.0.0.0 by hand gets the socket they asked for
    AND the authentication that socket needs — the toggle is not the only way
    to end up public."""
    from video_ai_editor import desktop
    assert desktop._resolve_bind_host("0.0.0.0") == ("0.0.0.0", True)
    assert desktop._resolve_bind_host("10.0.0.5") == ("10.0.0.5", True)


# --- 4. host discovery --------------------------------------------------------

def test_host_candidates_are_private_and_never_public(lan_home):
    """Verified against this machine: `getaddrinfo(gethostname())` returns no
    IPv4 here at all, which is why discovery is a connected-UDP socket plus
    `ifconfig`, not a hostname lookup."""
    from video_ai_editor.api import pairing
    hosts = pairing.host_candidates()
    assert all(pairing.is_private_ipv4(h) for h in hosts), hosts
    assert not any(h.startswith("127.") for h in hosts)


@pytest.mark.parametrize("addr,private", [
    ("10.120.2.82", True), ("192.168.1.9", True), ("172.16.0.1", True),
    ("172.31.255.254", True), ("169.254.1.1", True), ("100.87.139.4", True),
    ("172.32.0.1", False), ("8.8.8.8", False), ("100.63.0.1", False),
    ("not.an.ip.x", False),
])
def test_is_private_ipv4(addr, private):
    from video_ai_editor.api import pairing
    assert pairing.is_private_ipv4(addr) is private


def test_pair_payload_grammar_is_stable(lan_home):
    """S's scanner parses this exact string. `vaepair:` has NO registered
    handler on iOS by design — the app installs no deep-link handler, so a
    hostile page cannot fire a pairing payload at it and re-point the phone."""
    from video_ai_editor.api import pairing
    payload = pairing.pair_payload("192.168.1.9", 8765, "a" * 32)
    assert payload == f"vaepair:v=1&h=192.168.1.9&p=8765&c={'a' * 32}"


# --- 5. the third rung is a mechanism, not a docstring ------------------------

@pytest.mark.parametrize("addr,public", [
    ("192.168.1.20", True), ("10.0.0.5", True), ("100.87.139.4", True),
    ("8.8.8.8", True), ("fd00::1", True),
    ("127.0.0.1", False), ("127.5.5.5", False), ("::1", False),
    ("0.0.0.0", False), ("::", False), ("localhost", False),
    # Starlette's synthetic peer. A NAME proves nothing about which interface
    # anything arrived on, so it must never arm the posture — otherwise the
    # whole suite would run in LAN mode.
    ("testserver", False), ("", False), (None, False), (1234, False),
])
def test_is_public_local_addr(addr, public):
    from video_ai_editor.api import pairing
    assert pairing._is_public_local_addr(addr) is public


def test_a_request_on_a_public_interface_arms_the_posture(lan_home):
    """The rung the docstring promised and nothing implemented.

    Before 0.6.0 `_bound_public` was set from exactly one place —
    `desktop.py::main` — so `uvicorn video_ai_editor.main:app --host 0.0.0.0`
    with `VAE_LAN` unset came up on every interface with NO authentication and
    the path allowlist disarmed, while three docstrings and the CHANGELOG said
    a public bind self-arms "for any reason".
    """
    from video_ai_editor import config
    from video_ai_editor.api import pairing
    assert pairing.auth_required() is False
    assert config.restrict_paths_active() is False

    pairing.note_local_address("192.168.1.20")

    assert pairing.bound_public() is True
    assert pairing.auth_required() is True
    # And the allowlist came with it — the two are one posture, not two toggles.
    assert config.restrict_paths_active() is True


def test_a_loopback_request_never_arms_the_posture(lan_home):
    """The default desktop must stay byte-for-byte 0.5.0."""
    from video_ai_editor.api import pairing
    for addr in ("127.0.0.1", "::1", "testserver", None):
        pairing.note_local_address(addr)
    assert pairing.auth_required() is False


# --- 6. concurrent settings writes -------------------------------------------

def test_a_revoke_is_not_undone_by_a_racing_touch(lan_home):
    """The user revokes a lost iPhone while that phone is still polling.

    `revoke_device` runs on a threadpool worker; `touch_device` runs on the
    event loop for a request that authenticated a moment earlier and has
    already loaded the OLD device list. Without a lock across the whole
    read-modify-write, the touch writes the revoked device back with a fresh
    `last_seen`: the Phone panel shows the phone gone and the phone keeps
    working. `save_settings` being atomic per write does not help — that
    guarantees no torn file, not last-writer-correctness.
    """
    import threading
    from video_ai_editor.api import pairing

    token = pair_a_device("Lost iPhone")
    device = pairing.device_for_token(token)
    assert device is not None
    did = device["id"]

    # Force the touch past its own once-a-minute throttle, so it really does
    # want to write.
    data = pairing.load_settings()
    pairing.save_settings({**data, "devices": [
        {**d, "last_seen": 0.0} for d in data["devices"]]})

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def touch():
        try:
            barrier.wait(timeout=5)
            for _ in range(40):
                pairing.touch_device(did)
        except BaseException as e:      # noqa: BLE001 - reported, never swallowed
            errors.append(e)

    def revoke():
        try:
            barrier.wait(timeout=5)
            for _ in range(40):
                pairing.revoke_device(did)
        except BaseException as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=touch), threading.Thread(target=revoke)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert pairing.device_by_id(did) is None, "a racing touch resurrected a revoked device"
    assert pairing.device_for_token(token) is None


def test_settings_json_is_not_world_readable(lan_home):
    """It is the only record of which phones are paired — every device id,
    name and last_seen. On a shared Mac the process umask made it 0644."""
    import stat
    from video_ai_editor.api import pairing

    pair_a_device()
    p = pairing.settings_path()
    assert p.exists()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode & 0o077 == 0, oct(mode)
    # And no predictable temp file was left behind for anyone to read either.
    assert list(p.parent.glob("*.tmp")) == []
