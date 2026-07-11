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


def test_delete_session_rejects_path_traversal(tmp_path, monkeypatch):
    """sid is untrusted URL path input. A traversal-shaped sid must be
    rejected before it ever reaches shutil.rmtree, and must not delete
    anything outside WORKDIR — verified by planting a sentinel dir a level
    above WORKDIR and confirming it survives every attempt."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("WORKDIR", str(workdir))
    import importlib
    from video_ai_editor import config, storage
    importlib.reload(config)
    importlib.reload(storage)
    from video_ai_editor import main as m
    importlib.reload(m)
    try:
        sentinel = tmp_path / "sentinel_dir"
        sentinel.mkdir()
        (sentinel / "keepme.txt").write_text("do not delete", encoding="utf-8")

        client = TestClient(m.app)
        # Starlette/the ASGI transport normalizes ".."-bearing path segments
        # (raw or %-encoded) out of the URL before routing ever sees them, so a
        # literal "../x" sid 404/405s for reasons that have nothing to do with
        # our sid guard — it never reaches delete_session_route at all. That's
        # a welcome extra layer, but the real proof our guard works (regardless
        # of what a given HTTP stack/proxy normalizes) is the direct unit-level
        # call to storage.delete_session below, plus a same-segment id shape
        # that IS reachable through routing (no "/" or ".." token, just
        # malformed sid content) to prove the regex itself rejects it end to end.
        bad_but_routable_ids = [
            "s_not-a-real-session-id-shape!!!",  # fails the allowlist regex
            "s_" + "a" * 100,                    # oversized — also rejected
        ]
        for bad_sid in bad_but_routable_ids:
            r = client.delete(f"/api/sessions/{bad_sid}")
            assert r.status_code in (400, 404), f"{bad_sid!r} -> {r.status_code}"

        assert sentinel.exists()
        assert (sentinel / "keepme.txt").read_text(encoding="utf-8") == "do not delete"

        # Direct unit-level check — this is the authoritative proof the
        # storage-layer guard rejects traversal regardless of what any HTTP
        # framework/proxy does to the URL before a sid reaches this function.
        assert storage.delete_session("../sentinel_dir") is False
        assert storage.delete_session("s_../../sentinel_dir") is False
        assert sentinel.exists()
    finally:
        monkeypatch.delenv("WORKDIR", raising=False)
        importlib.reload(config)
        importlib.reload(storage)
        importlib.reload(m)
