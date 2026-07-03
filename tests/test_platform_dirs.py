import sys
from pathlib import Path


def test_broll_default_dir_is_platform_appropriate(monkeypatch):
    """Default b-roll dir must be ~/Videos/broll on Windows, ~/Movies/broll on Mac."""
    import video_ai_editor.agent.dispatch  # noqa: F401 - ensure it's imported
    dispatch_mod = sys.modules["video_ai_editor.agent.dispatch"]
    got = dispatch_mod._default_broll_dir()  # helper introduced by this task
    if sys.platform == "win32":
        assert got == Path.home() / "Videos" / "broll"
    else:
        assert got == Path.home() / "Movies" / "broll"
