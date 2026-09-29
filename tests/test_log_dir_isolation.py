"""The app's rotating log file stays out of the owner's profile when the
process is a test or a scratch backend (review RD3).

Every process that imported the app installed a RotatingFileHandler at
~/Library/Application Support/Video AI Editor/logs/app.log at IMPORT time —
before tests/conftest.py could redirect it — so the prescribed pytest command
appended QA traffic to the owner's log and rotated their diagnostics away.
`VAI_LOG_DIR` now names the directory; a pytest process and a backend with an
absolute WORKDIR keep their logs away from the profile by default.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from video_ai_editor import platformutil as _pu

LOG = ("import logging, sys\n"
       "{pre}"
       "from video_ai_editor.api import hardening\n"
       "logging.getLogger('video_ai_editor').warning('isolation probe')\n"
       "for h in logging.getLogger('video_ai_editor').handlers: h.flush()\n"
       "print([getattr(h, 'baseFilename', None) for h in logging.getLogger('video_ai_editor').handlers])\n")


def _home_env(tmp: Path) -> dict:
    """The environment that points every per-OS profile location at tmp/home:
    POSIX resolves the profile from HOME (macOS ~/Library/Application Support,
    Linux ~/.local/share), Windows from APPDATA/LOCALAPPDATA/USERPROFILE and
    never from HOME, so a probe that only sets HOME would write into the real
    profile of the account running the tests."""
    home = tmp / "home"
    return {"HOME": str(home), "USERPROFILE": str(home),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local")}


def _run(tmp: Path, env_extra: dict, pre: str = "") -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("VAI_LOG_DIR", "WORKDIR", "PYTEST_CURRENT_TEST")}
    env.update({**_home_env(tmp), **env_extra})
    r = subprocess.run([sys.executable, "-c", LOG.format(pre=pre)], env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


def _profile_logs(tmp: Path) -> Path:
    """Where the app puts app.log for the redirected home, on THIS OS, resolved
    by the same helper the app uses (hardening._log_dir -> user_data_dir)."""
    saved = {k: os.environ.get(k) for k in _home_env(tmp)}
    os.environ.update(_home_env(tmp))
    try:
        return _pu.user_data_dir("Video AI Editor") / "logs"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_vai_log_dir_names_the_log_directory(tmp_path):
    out = _run(tmp_path, {"VAI_LOG_DIR": str(tmp_path / "logs")})
    assert (tmp_path / "logs" / "app.log").read_text().count("isolation probe") == 1, out
    assert not _profile_logs(tmp_path).exists()


def test_a_pytest_process_never_logs_into_the_profile(tmp_path):
    out = _run(tmp_path, {}, pre="import pytest\n")
    assert not _profile_logs(tmp_path).exists(), out


def test_a_scratch_backend_logs_beside_its_workdir(tmp_path):
    wd = tmp_path / "wd"
    out = _run(tmp_path, {"WORKDIR": str(wd)})
    assert (wd / "logs" / "app.log").exists(), out
    assert not _profile_logs(tmp_path).exists()


def test_the_app_still_logs_to_the_profile_by_default(tmp_path):
    out = _run(tmp_path, {})
    assert (_profile_logs(tmp_path) / "app.log").exists(), out
