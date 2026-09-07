"""Session-wide test isolation from the developer's own machine state.

Why this exists: `api/auth.install()` → `pairing.sync_path_restriction()` reads
the REAL `~/Library/Application Support/Video AI Editor/settings.json` when the
app is imported. On a Mac where the owner has turned on "Allow my iPhone to
connect", that file says `lan_enabled: true`, `config.enable_path_restriction`
arms the VAI_ALLOWED_ROOTS allowlist for the whole process, and every later
test that hands a pytest tmp path (/private/var/folders/…) to a tool fails
with a 400 — 14 spurious failures that appear and disappear with a toggle in
the UI (found 2026-09-08 while completing the tool schemas).

WHY MODULE-LEVEL, NOT A FIXTURE: test modules do
`from video_ai_editor.main import app` at import, i.e. during COLLECTION, and
that import is what arms the allowlist. A fixture — even session-scoped and
autouse — runs after collection, which is too late (the first version of this
file was exactly that and changed nothing). conftest.py is imported before the
test modules in its directory are collected.

WHY A PROXY ON THE CONSUMERS' `_pu` ALIAS, NOT `platformutil.user_data_dir`:
replacing the function on the platformutil module made tests/test_platformutil
fail — those tests exist to pin the real per-OS behaviour. Every consumer in
src/ reaches the function through a module alias (`_pu.user_data_dir(...)`),
so redirecting the alias in the two modules that read at import/app-install
time (pairing: settings.json + device registry; hardening: the log dir)
isolates the tests without touching the function under test. Model caches
under `user_cache_dir` stay real on purpose — redirecting them would
re-download gigabytes per session.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from video_ai_editor import platformutil
from video_ai_editor.api import hardening as _hardening
from video_ai_editor.api import pairing as _pairing

_ISOLATED_DATA_ROOT = Path(tempfile.mkdtemp(prefix="vae-test-user-data-"))


class _PlatformutilProxy:
    """platformutil, except `user_data_dir` points into the test sandbox."""

    def __getattr__(self, name: str):
        return getattr(platformutil, name)

    @staticmethod
    def user_data_dir(app_name: str) -> Path:
        return _ISOLATED_DATA_ROOT / app_name


_pairing._pu = _PlatformutilProxy()    # settings.json, paired devices — read at app import
_hardening._pu = _PlatformutilProxy()  # request logs — keep them out of the real profile


@pytest.fixture(scope="session")
def isolated_user_data_dir() -> Path:
    """The per-session stand-in for ~/Library/Application Support."""
    return _ISOLATED_DATA_ROOT
