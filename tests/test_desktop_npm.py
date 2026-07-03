def test_npm_command_resolves_on_windows(monkeypatch):
    from video_ai_editor import desktop
    from video_ai_editor import platformutil as pu
    monkeypatch.setattr(pu, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop.shutil, "which",
                        lambda n: "C:/Program Files/nodejs/npm.cmd" if n in ("npm.cmd", "npm") else None)
    assert desktop._npm_cmd() == "C:/Program Files/nodejs/npm.cmd"


def test_npm_command_plain_off_windows(monkeypatch):
    from video_ai_editor import desktop
    from video_ai_editor import platformutil as pu
    monkeypatch.setattr(pu, "IS_WINDOWS", False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/local/bin/npm")
    assert desktop._npm_cmd() == "/usr/local/bin/npm"
