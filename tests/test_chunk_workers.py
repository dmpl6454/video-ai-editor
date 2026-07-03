def test_chunk_workers_no_sysctl_off_mac(monkeypatch):
    from video_ai_editor.render import chunks
    from video_ai_editor import platformutil as pu
    monkeypatch.setattr(pu, "IS_MAC", False)
    called = {"sysctl": False}
    real_run = chunks.subprocess.run
    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "sysctl":
            called["sysctl"] = True
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(chunks.subprocess, "run", spy)
    n = chunks._chunk_workers(4)
    assert n >= 1
    assert called["sysctl"] is False  # must not spawn sysctl off macOS
