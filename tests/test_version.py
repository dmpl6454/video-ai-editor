"""Version contract: /api/version and /api/health must report the VERSION file.

Versioning is a durable practice for this app — the VERSION file at the repo
root is the single source of truth, surfaced to the backend (these endpoints)
and the frontend top bar. These tests fail loudly if that wiring drifts.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from video_ai_editor.main import app

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def test_version_file_is_semver():
    assert VERSION_FILE.exists(), "VERSION file must exist at repo root"
    v = VERSION_FILE.read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"VERSION must be semver, got {v!r}"


def test_version_endpoint_matches_file():
    expected = VERSION_FILE.read_text().strip()
    c = TestClient(app)
    r = c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"] == expected


def test_health_reports_version():
    expected = VERSION_FILE.read_text().strip()
    c = TestClient(app)
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == expected
