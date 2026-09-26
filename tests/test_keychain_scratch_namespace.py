"""A scratch server can never read or write the owner's Keychain item.

WHY (wave D2 incident). A lane ran tests/test_c3_settings_ui.py against its
own QA backend (VAE_SETTINGS_UI_BASE_URL). That backend had been started with a
scratch WORKDIR but without VAI_KEYCHAIN_SERVICE, so keychain.SERVICE fell back
to the owner's real "Video AI Editor" item, and the test's Save step wrote a
fake key there (twice, from two lanes). No real key was lost, because none existed yet,
but a fake key in that item would have made every Claude call in the owner's app
fail, and a real key would have been overwritten and then deleted.

Rule: the real item is used only by a process that did not choose its own
WORKDIR (the packaged app, and ./run.sh in the repo). A process started with an
explicit WORKDIR (every QA, lane and test server) gets an item namespaced by
that folder, unless VAI_KEYCHAIN_SERVICE names one explicitly.

These run in a subprocess: tests/conftest.py rebinds keychain.SERVICE for the
whole pytest process, so only a fresh interpreter shows the import-time rule.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _service(env_overrides: dict[str, str | None]) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("WORKDIR", "VAI_KEYCHAIN_SERVICE")}
    env["PYTHONPATH"] = str(ROOT / "src")
    for k, v in env_overrides.items():
        if v is not None:
            env[k] = v
    out = subprocess.run([sys.executable, "-c", "from video_ai_editor import keychain; print(keychain.SERVICE)"],
                         env=env, capture_output=True, text=True, check=True, cwd=str(ROOT))
    return out.stdout.strip()


def test_the_app_and_run_sh_use_the_real_item():
    assert _service({}) == "Video AI Editor"


def test_a_scratch_workdir_gets_its_own_item(tmp_path):
    got = _service({"WORKDIR": str(tmp_path / "wd")})
    assert got != "Video AI Editor"
    assert got.startswith("Video AI Editor (scratch ")
    # stable per folder, distinct across folders
    assert _service({"WORKDIR": str(tmp_path / "wd")}) == got
    assert _service({"WORKDIR": str(tmp_path / "other")}) != got


def test_an_explicit_service_always_wins(tmp_path):
    assert _service({"WORKDIR": str(tmp_path / "wd"), "VAI_KEYCHAIN_SERVICE": "X TEST"}) == "X TEST"
    # the one way to point a scratch server at the real item is to say so
    assert _service({"WORKDIR": str(tmp_path / "wd"),
                     "VAI_KEYCHAIN_SERVICE": "Video AI Editor"}) == "Video AI Editor"
