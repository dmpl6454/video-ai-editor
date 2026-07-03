import sys
from pathlib import Path
from video_ai_editor import platformutil as pu


def test_allowed_roots_uses_ospathsep(monkeypatch):
    """VAI_ALLOWED_ROOTS must split on os.pathsep, not a hardcoded ':',
    so Windows 'C:\\a;C:\\b' parses as two roots, not four fragments."""
    monkeypatch.setenv("VAI_RESTRICT_PATHS", "1")
    monkeypatch.setenv("VAI_ALLOWED_ROOTS",
                       ("C:\\a;C:\\b" if sys.platform == "win32" else "/a:/b"))
    import importlib, video_ai_editor.config as cfg
    importlib.reload(cfg)
    # Two user roots + WORKDIR itself = 3 total.
    assert len(cfg.ALLOWED_PATH_ROOTS) == 3
    importlib.reload(cfg)  # restore default state for other tests


def test_user_config_dir_matches_platform():
    from video_ai_editor import config as cfg
    got = cfg._user_config_dir()
    assert got == pu.user_data_dir("Video AI Editor")
