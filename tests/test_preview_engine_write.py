"""PUT /api/settings/preview: the preview.engine WRITE path (wave D,
INSTANT_PREVIEW_SPEC §1 G6, §7, §12 — the per-phase kill switch).

Stored with the app settings (settings.json, beside the phone-pairing state)
through the one read-modify-write the pairing code uses. Every test here
points settings.json at its own tmp file: the owner's real settings file is
never read or written.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import preview_setting
from video_ai_editor.api import hardening, pairing
from video_ai_editor.main import app

ROUTE = "/api/settings/preview"


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "app-data" / "settings.json"
    monkeypatch.setattr(pairing, "settings_path", lambda: path)
    monkeypatch.setattr(pairing, "_cache", None)
    monkeypatch.delenv(preview_setting.ENV_VAR, raising=False)
    monkeypatch.delenv("VAI_PROXY_EAGER", raising=False)
    real = Path(pairing._pu.user_data_dir("Video AI Editor")) / "settings.json"
    assert path != real
    return path


@pytest.fixture
def client(settings_file):
    hardening.RATE.windows.clear()
    return TestClient(app)


def _put(client, body, **headers):
    """PUT `body` as JSON (any JSON value, a bare string included)."""
    return client.put(ROUTE, content=json.dumps(body), headers={
        "Content-Type": "application/json", **headers})


@pytest.mark.parametrize("engine", ["auto", "client", "server"])
def test_each_engine_is_stored_and_read_back(client, settings_file, engine):
    r = _put(client, {"engine": engine})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["engine"] == engine and r.json()["source"] == "settings"
    assert json.loads(settings_file.read_text())["preview"] == {"engine": engine}
    assert client.get(ROUTE).json()["engine"] == engine
    # eager proxy builds follow the engine (only `server` spends nothing).
    assert client.get(ROUTE).json()["eager_proxies"] is (engine != "server")


def test_the_write_keeps_every_other_setting(client, settings_file):
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"version": 1, "lan_enabled": True,
                                         "devices": [{"id": "d1", "name": "Phone"}],
                                         "preview": {"engine": "server", "future": 7}}))
    pairing._cache = None
    assert _put(client, {"engine": "client"}).status_code == 200
    data = json.loads(settings_file.read_text())
    assert data["lan_enabled"] is True and data["devices"] == [{"id": "d1", "name": "Phone"}]
    assert data["preview"] == {"engine": "client", "future": 7}
    assert (settings_file.stat().st_mode & 0o777) == 0o600


def test_case_and_space_are_forgiven_like_the_reader(client, settings_file):
    assert _put(client, {"engine": "  Client "}).json()["engine"] == "client"
    assert json.loads(settings_file.read_text())["preview"]["engine"] == "client"


@pytest.mark.parametrize("body", [
    {"engine": "warp"}, {"engine": ""}, {"engine": None}, {"engine": 1}, {"engine": ["auto"]},
])
def test_invalid_engine_is_422_and_nothing_is_written(client, settings_file, body):
    r = _put(client, body)
    assert r.status_code == 422
    d = r.json()["error"]["details"]
    assert d["code"] == "invalid_engine" and d["choices"] == ["auto", "client", "server"]
    assert not settings_file.exists()


@pytest.mark.parametrize("body", [{}, {"engine": "auto", "extra": 1}, [], None, "auto"])
def test_malformed_bodies_are_400(client, settings_file, body):
    r = _put(client, body)
    assert r.status_code == 400, r.text
    assert not settings_file.exists()


def test_not_json_is_400(client, settings_file):
    r = client.put(ROUTE, content=b"{engine: auto", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert not settings_file.exists()


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x", "application/jsonp"])
def test_json_only(client, settings_file, ctype):
    headers = {"Content-Type": ctype} if ctype else {}
    r = client.put(ROUTE, content=b'{"engine": "client"}', headers=headers)
    assert r.status_code == 415
    assert not settings_file.exists()


def test_oversized_body_is_413(client, settings_file):
    r = client.put(ROUTE, content=b'{"engine": "auto", "pad": "' + b"x" * 4096 + b'"}',
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_cross_site_and_foreign_hosts_are_refused(client, settings_file):
    assert _put(client, {"engine": "client"}, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert _put(client, {"engine": "client"}, **{"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert _put(client, {"engine": "client"}, Host="evil.example").status_code == 421
    assert _put(client, {"engine": "client"}, Origin="http://localhost:5173",
                Host="127.0.0.1:8765").status_code == 403
    assert not settings_file.exists()


def test_same_origin_is_accepted(client, settings_file):
    r = _put(client, {"engine": "auto"}, **{"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_not_loopback_is_refused(client, settings_file, monkeypatch):
    from video_ai_editor.api import auth
    monkeypatch.setattr(auth, "_is_loopback", lambda request: False)
    assert _put(client, {"engine": "client"}).status_code in (401, 403)
    assert not settings_file.exists()


def test_env_override_still_wins_and_says_so(client, settings_file, monkeypatch):
    monkeypatch.setenv(preview_setting.ENV_VAR, "server")
    body = _put(client, {"engine": "client"}).json()
    assert (body["engine"], body["source"]) == ("server", "env")
    assert json.loads(settings_file.read_text())["preview"]["engine"] == "client"


def test_concurrent_writes_never_lose_a_pairing_change(client, settings_file):
    """The write is one read-modify-write under the settings lock: a phone
    revoke racing a preview-engine write keeps both."""
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"version": 1, "lan_enabled": False, "devices": []}))
    pairing._cache = None
    errors: list[BaseException] = []

    def engine_writer():
        try:
            for i in range(40):
                preview_setting.set_preview_engine(("auto", "client", "server")[i % 3])
        except BaseException as e:   # pragma: no cover - reported below
            errors.append(e)

    def device_writer():
        try:
            for i in range(40):
                pairing._mutate(lambda d, i=i: {**d, "devices": [*d["devices"], {"id": f"d{i}"}]})
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=engine_writer), threading.Thread(target=device_writer)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errors
    data = json.loads(settings_file.read_text())
    assert len(data["devices"]) == 40 and data["preview"]["engine"] == "auto"  # the 40th write


def test_set_preview_engine_validates():
    with pytest.raises(ValueError):
        preview_setting.set_preview_engine("fast")
    with pytest.raises(ValueError):
        preview_setting.set_preview_engine(None)
