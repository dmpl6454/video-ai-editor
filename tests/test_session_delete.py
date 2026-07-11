from fastapi.testclient import TestClient


def test_delete_session_removes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config, storage
    importlib.reload(config)
    importlib.reload(storage)
    from video_ai_editor import main as m
    importlib.reload(m)
    try:
        client = TestClient(m.app)
        sid = client.post("/api/sessions").json()["id"]
        assert storage.session_exists(sid)
        r = client.delete(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert not storage.session_exists(sid)
    finally:
        # Restore module state so later tests aren't poisoned (see CLAUDE.md:
        # undo reload AFTER monkeypatch teardown, not before — delenv here
        # immediately, then reload, so cfg/storage/main are back at their real
        # defaults for the rest of the pytest process).
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)
        importlib.reload(m)


def test_delete_session_missing_returns_404(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKDIR", str(tmp_path))
    import importlib
    from video_ai_editor import config, storage
    importlib.reload(config)
    importlib.reload(storage)
    from video_ai_editor import main as m
    importlib.reload(m)
    try:
        client = TestClient(m.app)
        r = client.delete("/api/sessions/s_doesnotexist")
        assert r.status_code == 404
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)
        importlib.reload(m)
