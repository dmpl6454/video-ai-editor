import sys
from pathlib import Path
from video_ai_editor import platformutil as pu


def test_allowed_roots_uses_ospathsep(monkeypatch):
    """VAI_ALLOWED_ROOTS must split on os.pathsep, not a hardcoded ':',
    so Windows 'C:\\a;C:\\b' parses as two roots, not four fragments."""
    import importlib, video_ai_editor.config as cfg
    monkeypatch.setenv("VAI_RESTRICT_PATHS", "1")
    monkeypatch.setenv("VAI_ALLOWED_ROOTS",
                       ("C:\\a;C:\\b" if sys.platform == "win32" else "/a:/b"))
    importlib.reload(cfg)
    try:
        # Two user roots + WORKDIR itself = 3 total.
        assert len(cfg.ALLOWED_PATH_ROOTS) == 3
    finally:
        # monkeypatch only unwinds os.environ on teardown, which happens
        # AFTER this function returns — reloading here, with the env vars
        # still set to their test values, does NOT restore the module's
        # module-level RESTRICT_PATHS/ALLOWED_PATH_ROOTS constants to their
        # defaults (both are computed once at import time, not re-read per
        # call). A prior version of this test called reload() here (with the
        # env still overridden) as a "restore" step, which silently left
        # RESTRICT_PATHS=True baked into the shared `cfg` module for the rest
        # of the pytest process — any later test in the suite whose fixture
        # paths aren't under an allowed root would then fail with a
        # confusing assert_path_allowed ValueError, for a reason completely
        # unrelated to what it was testing. Explicitly delenv (undoing this
        # test's own monkeypatch calls immediately, not at its teardown) then
        # reload — this leaves cfg genuinely back at its real default state.
        monkeypatch.delenv("VAI_RESTRICT_PATHS", raising=False)
        monkeypatch.delenv("VAI_ALLOWED_ROOTS", raising=False)
        importlib.reload(cfg)


def test_user_config_dir_matches_platform():
    from video_ai_editor import config as cfg
    got = cfg._user_config_dir()
    assert got == pu.user_data_dir("Video AI Editor")
