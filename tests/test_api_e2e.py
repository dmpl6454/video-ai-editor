"""End-to-end smoke: create → upload → cut → preview → export via FastAPI."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from video_ai_editor.main import app

SAMPLE = Path("/Users/sudhanshu/Downloads/Viral Videos/Outfit Breakdown ft. @wamiqagabbi.mp4")


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample not available")
def test_full_workflow_create_upload_cut_export(monkeypatch, tmp_path):
    # Redirect WORKDIR to a temp dir so we don't pollute the real workdir
    from video_ai_editor import config, storage, main
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(main, "_STORES", {})

    c = TestClient(app)

    # 1. Create session
    r = c.post("/api/sessions", json={"name": "wamiqa test"})
    assert r.status_code == 200
    sid = r.json()["id"]

    # 2. Upload + ingest
    with SAMPLE.open("rb") as f:
        r = c.post(f"/api/sessions/{sid}/upload",
                   files={"file": (SAMPLE.name, f, "video/mp4")},
                   data={"add_to_timeline": "true"})
    assert r.status_code == 200, r.text
    upload = r.json()
    assert upload["duration"] > 0

    # 3. Inspect timeline — should have 1 clip on v1
    r = c.get(f"/api/sessions/{sid}")
    summary = r.json()["summary"]
    v1 = next(t for t in summary["tracks"] if t["id"] == "v1")
    assert len(v1["clips"]) == 1
    assert v1["clips"][0]["src_name"].endswith(".mp4")

    # 4. Cut a 1s slice from the middle
    cut_start = min(1.0, upload["duration"] / 3)
    cut_end = cut_start + 0.5
    r = c.post(f"/api/sessions/{sid}/dispatch", json={
        "tool": "cut_range",
        "args": {"track": "v1", "start": cut_start, "end": cut_end},
    })
    assert r.status_code == 200, r.text

    # 5. Render preview
    r = c.post(f"/api/sessions/{sid}/preview")
    assert r.status_code == 200
    pv = r.json()
    assert pv["edl_hash"]

    # 6. Stream preview
    r = c.get(f"/api/sessions/{sid}/preview.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert len(r.content) > 0

    # 7. Export
    r = c.post(f"/api/sessions/{sid}/export", json={"crf": 24})
    assert r.status_code == 200
    exp = r.json()
    assert Path(exp["path"]).exists()

    # 8. Undo and verify
    r = c.post(f"/api/sessions/{sid}/dispatch", json={"tool": "undo", "args": {}})
    assert r.status_code == 200
    assert r.json()["result"]["ok"] is True
