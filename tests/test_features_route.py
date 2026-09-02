"""The three backend contracts the AI panel is built on.

  GET  /api/features                       — `check_features` over HTTP, memoised
  GET  /api/tools                          — + `cancellable` / `reports_progress`
  POST /api/sessions/{sid}/subtitle_upload — a .srt/.vtt/.ass into the session

The panel greys a tool out BEFORE the click from `/api/features` (showing the
exact `fix` string instead of a 422 afterwards), and offers Cancel / a % bar
only where the handler's signature says the backend will honour them —
api/jobs.py's cancel merely sets an event, so a handler that never reads it
runs to completion and commits regardless.
"""
from __future__ import annotations
import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.agent.dispatch import DISPATCH, dispatch
from video_ai_editor.ai.features import FEATURES
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import Canvas, empty_edl
from video_ai_editor.main import app

ROOT = Path(__file__).resolve().parents[1]

SRT_TWO_CUES = (
    "1\n00:00:00,000 --> 00:00:01,500\nhello there\n\n"
    "2\n00:00:01,500 --> 00:00:03,000\ngeneral kenobi\n"
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage, main as _main
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    # The report is memoised per process; start each test cold so a fake
    # installed by one test can never leak into another's assertions.
    monkeypatch.setattr(_main, "_FEATURE_REPORT_CACHE", None)
    _main._STORES.clear()
    return TestClient(app)


def _store(path: Path) -> EDLStore:
    path.mkdir(parents=True, exist_ok=True)
    (path / "edl.json").write_text(
        empty_edl(Canvas(w=320, h=180, fps=30)).model_dump_json())
    return EDLStore(path)


# ------------------------------------------------------------ /api/features

def test_features_route_reports_every_feature_with_a_fix_for_each_gap(client):
    r = client.get("/api/features")
    assert r.status_code == 200
    body = r.json()
    for key in ("packaged_app", "python", "anthropic_key_set",
                "available", "unavailable", "summary"):
        assert key in body, key
    assert len(body["available"]) + len(body["unavailable"]) == len(FEATURES)
    for entry in body["unavailable"]:
        assert entry["fix"], f"{entry['key']} reports no way to fix it"
    for entry in body["available"]:
        assert "fix" not in entry, f"{entry['key']} is available yet carries a fix"


def test_features_route_is_memoised_until_refresh(client, monkeypatch):
    """The probes cost ~2.2 s cold and the answer only changes when someone
    installs something — so one probe per process, re-run on `?refresh=1`."""
    from video_ai_editor.ai import features as F
    calls: list[int] = []

    def fake_report():
        calls.append(1)
        return {"packaged_app": False, "python": "x", "anthropic_key_set": False,
                "available": [], "unavailable": [], "summary": "fake"}

    monkeypatch.setattr(F, "feature_report", fake_report)
    assert client.get("/api/features").json()["summary"] == "fake"
    client.get("/api/features")
    assert len(calls) == 1
    client.get("/api/features?refresh=1")
    assert len(calls) == 2


def test_route_matches_check_features_tool(client, tmp_path):
    """One function behind both surfaces — chat and the panel must never
    disagree about what this install can do."""
    over_http = client.get("/api/features").json()
    via_tool = dispatch(_store(tmp_path / "store"), "check_features", {})
    assert over_http == via_tool


# --------------------------------------------------------------- /api/tools

def _declares(fn, param: str) -> bool:
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def test_tools_advertise_hook_flags(client):
    tools = client.get("/api/tools").json()["tools"]
    assert tools
    for t in tools:
        assert isinstance(t["cancellable"], bool), t["name"]
        assert isinstance(t["reports_progress"], bool), t["name"]
        fn = DISPATCH[t["name"]]
        assert t["cancellable"] == _declares(fn, "cancel_event"), t["name"]
        assert t["reports_progress"] == _declares(fn, "set_progress"), t["name"]
    # The derivation is live, not a stub: at least one tool (auto_caption
    # today) genuinely honours the hooks.
    assert any(t["cancellable"] for t in tools)
    assert any(t["reports_progress"] for t in tools)


def test_every_catalogued_tool_is_advertised(client):
    """The AI panel's catalog (frontend/src/lib/aiCatalog.ts) is the one list
    of tools it offers; every entry must be a tool /api/tools advertises and
    dispatch() knows — read from the source, not duplicated here."""
    catalog = ROOT / "frontend/src/lib/aiCatalog.ts"
    if not catalog.exists():
        pytest.skip("frontend/src/lib/aiCatalog.ts not present in this checkout")
    names = set(re.findall(r"tool: '([a-z_]+)'", catalog.read_text(encoding="utf-8")))
    assert names, "no `tool: '…'` entries found in aiCatalog.ts"
    advertised = {t["name"] for t in client.get("/api/tools").json()["tools"]}
    assert names <= advertised, f"catalogued but not advertised: {sorted(names - advertised)}"
    assert names <= set(DISPATCH), f"catalogued but not dispatchable: {sorted(names - set(DISPATCH))}"


def test_feature_tools_are_advertised(client):
    """A gate that names a tool the panel can't see would grey out nothing."""
    advertised = {t["name"] for t in client.get("/api/tools").json()["tools"]}
    for f in FEATURES:
        for t in f.tools:
            assert t in advertised, f"{f.key} names {t!r}, which /api/tools does not advertise"


# ---------------------------------------------------------- subtitle_upload

def test_subtitle_upload_stores_the_file_in_the_session(client, tmp_path):
    from video_ai_editor import main as _main
    sid = client.post("/api/sessions").json()["id"]
    ops_before = len(client.get(f"/api/sessions/{sid}/ops").json()["ops"])
    r = client.post(f"/api/sessions/{sid}/subtitle_upload",
                    files={"file": ("my captions.srt", SRT_TWO_CUES.encode("utf-8"),
                                    "application/x-subrip")})
    assert r.status_code == 200, r.text
    body = r.json()
    path = Path(body["path"])
    assert path.exists()
    assert body["name"] == path.name == "my_captions.srt"     # _safe_filename applied
    assert path.is_relative_to(tmp_path)                      # under the session dir
    # It must NOT dispatch: the panel follows with import_srt itself so the
    # import lands in the op log / undo like every other edit.
    assert len(client.get(f"/api/sessions/{sid}/ops").json()["ops"]) == ops_before
    store = _main._store(sid)
    result = dispatch(store, "import_srt", {"path": str(path)})
    assert result["segments"] == 2
    # The panel's two-step flow — the imported cues must reach the timeline.
    # add_caption_track used to read only whisper's ingest.json (which this
    # session doesn't have), so this laid down zero cues after an import.
    laid = dispatch(store, "add_caption_track", {"style": "default", "position": "bottom"})
    assert laid["lines"] == 2
    cues = [(round(c.start, 3), round(c.end, 3), c.text)
            for c in store.edl.get_track("captions").clips]
    assert cues == [(0.0, 1.5, "hello there"), (1.5, 3.0, "general kenobi")]
    # …and get_transcript (remove_fillers / find_moments / generate_hook read
    # through it) sees the same import.
    texts = [s["text"] for s in dispatch(store, "get_transcript", {})["segments"]]
    assert texts == ["hello there", "general kenobi"]


def test_subtitle_upload_rejects_non_subtitle_files(client):
    sid = client.post("/api/sessions").json()["id"]
    r = client.post(f"/api/sessions/{sid}/subtitle_upload",
                    files={"file": ("notes.txt", b"not captions", "text/plain")})
    assert r.status_code == 422
    assert "unsupported_subtitle" in r.text


def test_subtitle_upload_unknown_session_is_404(client):
    r = client.post("/api/sessions/does-not-exist/subtitle_upload",
                    files={"file": ("c.srt", SRT_TWO_CUES.encode("utf-8"), "text/plain")})
    assert r.status_code == 404
