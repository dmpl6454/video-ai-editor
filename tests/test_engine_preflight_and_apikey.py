"""Two ways the shipped app lied to a first-time user, and their fixes.

Both were confirmed against a notarized DMG, so neither is hypothetical.

1. **The app blamed the user's footage for its own missing dependency.**
   Nothing bundles ffmpeg/ffprobe, so on a Mac without Homebrew every media
   path fails — and each one failed by saying something untrue. `upload`'s
   catch-all relabelled the `FileNotFoundError` that `subprocess` raises for a
   missing BINARY as "Couldn't import this file — it may not be a valid
   video", sending people off to re-encode footage that was fine. The render
   endpoints were worse: they map `RuntimeError` (which is how the render layer
   wraps ffmpeg's *stderr*), and a binary that never executes produces no
   stderr — so that case alone fell through to hardening's generic handler as
   HTTP 500 "internal server error", with the real cause discarded into a log.
   `/readyz` has known the answer all along and nothing in the app calls it.

2. **There was no in-app way to supply ANTHROPIC_API_KEY.** The only route was
   hand-creating a hidden `.env` inside a Finder-hidden folder, so the chat pane
   was permanently dead for anyone who did not already know that path.

The tests below pin the contract of each fix: a DISTINCT, actionable error that
names ffmpeg (and never the user's file), no 500s from the render layer, genuine
media failures still reporting their existing message — and, for the key: shape
validation, the key never being readable back, an unrelated `.env` line
surviving the write, and mode 0600.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import apikey
from video_ai_editor import main as _main
from video_ai_editor import platformutil as _pu
from video_ai_editor.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sid(client) -> str:
    return client.post("/api/sessions", json={"name": "t"}).json()["id"]


def _details(res) -> dict:
    """hardening nests a dict HTTPException detail under `error.details` (and
    hardcodes `error.message` to "request failed"), which is why every
    user-facing sentence in main.py lives in that dict's `message`."""
    body = res.json()
    return body.get("error", {}).get("details") or {}


# ---------------------------------------------------------------------------
# 1. The video-engine preflight

def test_engine_probe_latches_only_on_success(monkeypatch):
    """Cheap on the good path, self-healing on the bad one.

    /thumb calls this once per filmstrip tile, so a positive answer must be
    latched. A NEGATIVE one must not be: the user's fix for it is installing
    ffmpeg, and latching would mean the app stayed broken until a restart.
    """
    monkeypatch.setattr(_main, "_VIDEO_ENGINE_OK", False)

    monkeypatch.setattr(_main.shutil, "which", lambda _n: None)
    # The probe reports the names it actually looked up, which are _pu.FFMPEG /
    # _pu.FFPROBE — "ffmpeg.exe"/"ffprobe.exe" on Windows, and absolute bundle
    # paths in a frozen app. Compare against those, not a macOS-shaped literal:
    # hardcoding ["ffmpeg", "ffprobe"] failed the windows-latest CI job, which is
    # the source of truth for Windows behaviour (CLAUDE.md, Testing conventions).
    expected = [_pu.FFMPEG, _pu.FFPROBE]
    assert _main._video_engine_missing() == expected
    assert _main._video_engine_missing() == expected  # re-probed, not latched

    monkeypatch.setattr(_main.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert _main._video_engine_missing() == []
    # Latched: a later disappearance is not re-probed, which is what makes the
    # per-request cost zero once the engine is known good.
    monkeypatch.setattr(_main.shutil, "which", lambda _n: None)
    assert _main._video_engine_missing() == []


@pytest.mark.parametrize("call", [
    lambda c, sid: c.post(f"/api/sessions/{sid}/preview"),
    lambda c, sid: c.post(f"/api/sessions/{sid}/preview?wait=0"),
    lambda c, sid: c.post(f"/api/sessions/{sid}/export"),
    lambda c, sid: c.get(f"/api/sessions/{sid}/preview.mp4"),
])
def test_render_endpoints_name_ffmpeg_when_it_is_missing(client, sid, monkeypatch, call):
    monkeypatch.setattr(_main, "_video_engine_missing", lambda: ["ffmpeg", "ffprobe"])
    res = call(client, sid)
    assert res.status_code == 422, res.text
    d = _details(res)
    assert d["error"] == "ffmpeg_missing"
    assert d["missing"] == ["ffmpeg", "ffprobe"]
    assert "ffmpeg" in d["message"]
    # The whole point: it must not send the user hunting through their media.
    assert "not a valid video" not in d["message"]
    assert "corrupt" not in d["message"].lower()


def test_upload_does_not_blame_the_file_for_a_missing_ffmpeg(client, sid, monkeypatch):
    monkeypatch.setattr(_main, "_video_engine_missing", lambda: ["ffmpeg"])
    res = client.post(f"/api/sessions/{sid}/upload",
                      files={"file": ("clip.mp4", b"\x00\x01\x02", "video/mp4")},
                      data={"transcribe": "false"})
    assert res.status_code == 422, res.text
    d = _details(res)
    assert d["error"] == "ffmpeg_missing"          # NOT "couldn't_import"
    assert "may not be a valid video" not in d["message"]


def test_audio_upload_reports_the_engine_not_request_failed(client, sid, monkeypatch):
    """audio_upload's probe arm returns `{"error": str(e)}` with no `message`
    key, so before the preflight a missing binary reached the user as the
    envelope's bare "request failed"."""
    monkeypatch.setattr(_main, "_video_engine_missing", lambda: ["ffprobe"])
    res = client.post(f"/api/sessions/{sid}/audio_upload",
                      files={"file": ("song.mp3", b"\x00", "audio/mpeg")})
    assert res.status_code == 422
    assert _details(res)["error"] == "ffmpeg_missing"
    assert "ffmpeg" in _details(res)["message"]


@pytest.mark.parametrize("path,method", [
    ("/preview", "post"),
    ("/export", "post"),
])
def test_filenotfounderror_from_the_render_layer_is_not_a_500(
        client, sid, monkeypatch, path, method):
    """The one render failure that used to be an opaque 500.

    `render_preview`/`render_export` wrap ffmpeg's stderr in a RuntimeError, but
    subprocess raises FileNotFoundError BEFORE any process exists — so there is
    no stderr, nothing for `_render_failure_message` to classify, and the
    exception escaped every arm.
    """
    def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")
    monkeypatch.setattr(_main, "render_preview", _boom)
    monkeypatch.setattr(_main, "render_export", _boom)
    # Engine present at preflight time, gone by the time the render runs — the
    # only way to reach the handler rather than the preflight.
    monkeypatch.setattr(_main, "_video_engine_missing", lambda: [])

    res = getattr(client, method)(f"/api/sessions/{sid}{path}")
    assert res.status_code == 422, res.text
    d = _details(res)
    assert d["error"] == "missing_file"
    assert "ffmpeg" in d["message"]        # the filename it could not execute


def test_genuine_ffmpeg_failures_keep_their_existing_message(client, sid, monkeypatch):
    """Only the missing-binary case changed. A real ffmpeg failure must still be
    classified by `_render_failure_message` exactly as before."""
    monkeypatch.setattr(_main, "_video_engine_missing", lambda: [])

    def _boom(*a, **k):
        raise RuntimeError("ffmpeg failed\n[AVFilterGraph] Error reinitializing filters")
    monkeypatch.setattr(_main, "render_preview", _boom)

    res = client.post(f"/api/sessions/{sid}/preview")
    assert res.status_code == 422
    d = _details(res)
    assert d["error"] == "render_failed"
    assert "inconsistent video" in d["message"]


# ---------------------------------------------------------------------------
# 2. The API key

@pytest.fixture
def key_env(tmp_path: Path, monkeypatch):
    """Point the key store at a temp `.env` and undo every global it touches.

    `apply_key_to_process` deliberately rebinds `ANTHROPIC_API_KEY` across
    already-imported modules — that is the whole feature — so a test that did
    not restore them would leave the rest of the pytest process believing a key
    is configured, and CI forces the variable empty precisely so no test reaches
    a real endpoint.
    """
    # setenv("") rather than delenv(): delenv records an undo entry ONLY when the
    # name is already set, so on a machine where it is absent the endpoint's own
    # os.environ write (apply_key_to_process) would survive teardown and leak a
    # fake key into the rest of the pytest process. setenv always records the
    # prior state — including "was absent" — and "" reads as not-configured.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    saved = [(m, getattr(m, apikey.ENV_KEY))
             for name, m in list(sys.modules.items())
             if m is not None and name.split(".")[0] == "video_ai_editor"
             and isinstance(getattr(m, apikey.ENV_KEY, None), str)]
    env = tmp_path / ".env"
    monkeypatch.setattr(apikey, "user_env_path", lambda: env)
    yield env
    for mod, val in saved:
        setattr(mod, apikey.ENV_KEY, val)


GOOD_KEY = "sk-ant-api03-" + "A" * 80


@pytest.mark.parametrize("bad", [
    "", "   ", "nope", "ant-api03-xxxxxxxxxxxxxxxxxxxx",
    "sk-short", "sk-ant-key with spaces", "sk-ant-line1\nline2",
])
def test_a_key_that_is_not_shaped_like_one_is_rejected(client, key_env, bad):
    res = client.post("/api/settings/api-key", json={"key": bad})
    assert res.status_code == 400, res.text
    assert "Anthropic API key" in _details(res)["message"]
    assert not key_env.exists()            # nothing written on a rejection


def test_the_key_is_never_readable_back(client, key_env):
    assert client.post("/api/settings/api-key",
                       json={"key": GOOD_KEY}).status_code == 200
    res = client.get("/api/settings/api-key")
    assert res.status_code == 200
    assert res.json()["configured"] is True
    # Not the key, and not a fragment of it either.
    assert GOOD_KEY not in res.text
    assert GOOD_KEY[-4:] not in res.text


def test_status_is_false_before_a_key_is_set(client, key_env):
    assert client.get("/api/settings/api-key").json()["configured"] is False


def test_saving_preserves_unrelated_settings_already_in_the_env_file(client, key_env):
    key_env.write_text(
        "# my settings\n"
        "HUGGINGFACE_TOKEN=hf_abc123\n"
        "WHISPER_MODEL=large-v3\n",
        encoding="utf-8")

    assert client.post("/api/settings/api-key",
                       json={"key": GOOD_KEY}).status_code == 200

    text = key_env.read_text(encoding="utf-8")
    assert "# my settings" in text
    assert "HUGGINGFACE_TOKEN=hf_abc123" in text
    assert "WHISPER_MODEL=large-v3" in text
    assert f"ANTHROPIC_API_KEY={GOOD_KEY}" in text


def test_the_env_file_is_owner_only(client, key_env):
    assert client.post("/api/settings/api-key",
                       json={"key": GOOD_KEY}).status_code == 200
    mode = stat.S_IMODE(key_env.stat().st_mode)
    if os.name == "nt":       # Windows models only the read-only bit
        pytest.skip("POSIX permission bits are not modelled on Windows")
    assert mode == 0o600, oct(mode)
    # And no stage file left behind next to it.
    assert [p.name for p in key_env.parent.iterdir()] == [".env"]


def test_replacing_a_key_leaves_exactly_one_assignment(client, key_env):
    """`config._apply_env_file` lets a LATER entry win, so a stale second line
    would silently override the value we were just asked to store."""
    key_env.write_text(f"ANTHROPIC_API_KEY=sk-ant-old\nFOO=bar\n"
                       f"ANTHROPIC_API_KEY=sk-ant-older\n", encoding="utf-8")
    assert client.post("/api/settings/api-key",
                       json={"key": GOOD_KEY}).status_code == 200
    lines = key_env.read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if ln.startswith("ANTHROPIC_API_KEY")] == \
        [f"ANTHROPIC_API_KEY={GOOD_KEY}"]
    assert "FOO=bar" in lines


def test_a_saved_key_is_live_without_a_restart(client, key_env):
    """`agent/loop.py` did `from ..config import ANTHROPIC_API_KEY` at import
    time, so setting os.environ alone leaves the chat pane dead with the key
    plainly saved."""
    from video_ai_editor import config as _config
    assert client.post("/api/settings/api-key",
                       json={"key": GOOD_KEY}).status_code == 200
    assert os.environ["ANTHROPIC_API_KEY"] == GOOD_KEY
    assert _config.ANTHROPIC_API_KEY == GOOD_KEY
    loop = sys.modules.get("video_ai_editor.agent.loop")
    if loop is not None:
        assert loop.ANTHROPIC_API_KEY == GOOD_KEY


def test_saving_refreshes_the_memoised_feature_report(client, key_env, monkeypatch):
    """`anthropic_key_set` is memoised for the life of the process, so without
    an invalidation the AI panel keeps showing "needs ANTHROPIC_API_KEY" for a
    key it just accepted."""
    monkeypatch.setattr(_main, "_FEATURE_REPORT_CACHE",
                        {"anthropic_key_set": False, "available": [],
                         "unavailable": [], "summary": "", "packaged_app": False,
                         "python": "3.13"})
    assert client.get("/api/features").json()["anthropic_key_set"] is False
    assert client.post("/api/settings/api-key",
                       json={"key": GOOD_KEY}).status_code == 200
    assert client.get("/api/features").json()["anthropic_key_set"] is True


def test_merge_is_a_pure_function_over_env_text():
    """Unit-level, because every interesting case here is a text edge case and
    a round-trip through HTTP proves none of them."""
    m = apikey.merge_env_text
    # No trailing newline in the source, and a commented-out assignment must be
    # left alone rather than treated as the value to replace.
    assert m("#ANTHROPIC_API_KEY=old\nFOO=1", "ANTHROPIC_API_KEY", "sk-new") == \
        "#ANTHROPIC_API_KEY=old\nFOO=1\nANTHROPIC_API_KEY=sk-new\n"
    # Empty input.
    assert m("", "K", "v") == "K=v\n"
    # Whitespace around the name is how config's own parser reads it.
    assert m("  K = old  \n", "K", "v") == "K=v\n"
