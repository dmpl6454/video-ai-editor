"""Shared setup for the LAN-mode / pairing tests.

Not a conftest.py on purpose. Three test files need the same four things and
nothing else in the suite does, so an importable module keeps the blast radius
of a mistake here inside those three files.

The critical part is `lan_reset`: LAN mode arms process-global state (the
filesystem allowlist in `config`, the "we bound a public socket" flag, the
pending-code table, the auth lockout counters). tests/test_config_paths.py
documents what happens when that kind of state leaks — a later, unrelated test
fails with a path error for a reason it has nothing to do with. Every fixture
here restores the default posture whether the test passed or not.
"""
from __future__ import annotations

import pytest


def reset_pairing_state() -> None:
    """Return the process to the shipped desktop posture."""
    from video_ai_editor import config
    from video_ai_editor.api import auth, pairing

    pairing._bound_public = False
    pairing._server_port = 0
    pairing._cache = None
    pairing._pending.clear()
    auth.reset_lockout()
    config.enable_path_restriction(False)


@pytest.fixture
def lan_home(monkeypatch, tmp_path):
    """Point settings.json at a temp dir and start from a clean posture.

    Without this, every pairing test would write to the developer's real
    `~/Library/Application Support/Video AI Editor/settings.json` and could
    pair a phantom device with their actual Mac.
    """
    from video_ai_editor.api import pairing

    home = tmp_path / "appdata"
    home.mkdir()
    monkeypatch.setattr(pairing, "settings_path",
                        lambda: home / "settings.json")
    reset_pairing_state()
    yield home
    reset_pairing_state()


@pytest.fixture
def lan_on(lan_home, monkeypatch):
    """`lan_home`, with LAN mode armed via the env override.

    The env switch is used here rather than a written settings.json so that a
    test exercising the middleware does not also depend on the file writer
    working — `test_lan_posture.py` tests that half separately.
    """
    monkeypatch.setenv("VAE_LAN", "1")
    from video_ai_editor.api import pairing
    pairing.sync_path_restriction()
    yield lan_home
    reset_pairing_state()


def lan_peer(**kwargs):
    """A TestClient whose peer address is a real LAN IP.

    The default TestClient reports `request.client.host == "testclient"`, which
    `api.auth._is_loopback` deliberately treats as loopback under pytest — so a
    default client would sail past every check this suite exists to prove.
    """
    from fastapi.testclient import TestClient
    from video_ai_editor.main import app
    return TestClient(app, client=("192.168.1.50", 51234), **kwargs)


#: Headers a well-behaved companion app sends on every non-media request.
CLIENT_HEADERS = {"X-VAE-Client": "1"}


def pair_a_device(name: str = "Test iPhone") -> str:
    """Mint + claim a code the way the desktop and the phone would, and return
    the bearer token."""
    from video_ai_editor.api import pairing
    code = pairing.new_pair_code()
    device = pairing.claim_pair_code(code, name)
    assert device is not None
    return device["token"]
