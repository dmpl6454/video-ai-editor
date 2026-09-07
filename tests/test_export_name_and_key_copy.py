"""Two fresh-recipient rough edges from the 0.5.0 DMG audit.

G2 — the chat pane's key errors told the user to "check ANTHROPIC_API_KEY in
     your .env and restart". A packaged-app user has no `.env` they know of
     (it lives Finder-hidden under Application Support — exactly the problem
     `apikey.py` exists to solve) and needs no restart either, since
     `POST /api/settings/api-key` rebinds the live process. The copy must name
     the in-app route, and must stay true when run from source.

G4 — the native Save dialog proposed `export_<edl-hash>.mp4`. The on-disk name
     cannot change (it is the render cache key, and `desktop.py::save_export`
     resolves the source file by the response's `filename`), so the export
     response carries an ADDITIVE `suggested_filename` alongside it. An older
     client that reads only the three original fields must keep working.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import main as _main
from video_ai_editor.agent import loop as _loop
from video_ai_editor.edl.schema import Canvas, Clip, empty_edl


# --- G2: the key copy -------------------------------------------------------

class _AuthError(Exception):
    status_code = 401


def _key_messages() -> list[str]:
    return [_loop._NO_KEY_MESSAGE,
            _loop._friendly_anthropic_error(_AuthError("authentication_error"))]


@pytest.mark.parametrize("msg", _key_messages())
def test_key_copy_names_the_in_app_route_not_a_dotfile(msg: str):
    # The two failures a user can actually fix must point at the button that
    # fixes them. `.env` is unreachable in the packaged app and the wrong
    # advice from source too, where the same button writes the same file.
    assert ".env" not in msg
    assert "console.anthropic.com" in msg
    assert "toolbar" in msg


@pytest.mark.parametrize("msg", _key_messages())
def test_key_copy_does_not_ask_for_a_restart(msg: str):
    # `apikey.apply_key_to_process` rebinds the running process, so a restart
    # is not merely unnecessary — asking for one implies the save did nothing.
    assert "restart" not in msg.replace("no restart needed", "")


def test_the_chat_pane_can_recognise_a_key_error():
    # ChatOverlay.tsx offers the key dialog under an error matching
    # /anthropic api key/i. Both key lines must match, and the credit-balance
    # line must NOT — re-pasting a key fixes nothing when credits ran out.
    for msg in _key_messages():
        assert "anthropic api key" in msg.lower()
    credit = _loop._friendly_anthropic_error(
        Exception("Error code: 400 … your credit balance is too low"))
    assert "anthropic api key" not in credit.lower()


def test_other_failure_copy_is_untouched():
    assert "rate limited" in _loop._friendly_anthropic_error(
        type("E", (Exception,), {"status_code": 429})("rate limit"))
    assert "overloaded" in _loop._friendly_anthropic_error(
        type("E", (Exception,), {"status_code": 529})("overloaded")).lower()


# --- G4: the suggested export name -----------------------------------------

class _Res:
    """The shape `render_export` returns, reduced to what the payload reads."""
    def __init__(self, path: str):
        self.path = Path(path)


def _edl_with_v1(*srcs: str):
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    v1 = next(t for t in edl.tracks if t.id == "v1")
    for i, src in enumerate(srcs):
        v1.clips.append(Clip(id=f"c{i}", src=src, **{"in": 0.0}, out=2.0,
                             start=float(i) * 2.0))
    return edl


TODAY = date.today().isoformat()


def test_name_comes_from_the_first_v1_clip_and_strips_normalized():
    edl = _edl_with_v1("/w/s_x/uploads/My Holiday/My Holiday.normalized.mp4")
    out = _main._export_payload("s_abc123", _Res("/w/s_x/exports/export_ff00.mp4"), edl)
    assert out["suggested_filename"] == f"My_Holiday_{TODAY}.mp4"


def test_the_earliest_clip_wins_regardless_of_list_order():
    # Clips are not stored in timeline order after a reorder/repack, and the
    # name a user expects is the footage the video OPENS on.
    edl = _edl_with_v1("/u/second.normalized.mp4", "/u/first.normalized.mp4")
    v1 = next(t for t in edl.tracks if t.id == "v1")
    v1.clips[0].start, v1.clips[1].start = 5.0, 0.0
    out = _main._export_payload("s_abc123", _Res("/w/exports/export_ff00.mp4"), edl)
    assert out["suggested_filename"] == f"first_{TODAY}.mp4"


def test_a_windows_authored_src_is_split_on_the_right_separator():
    # A `.vae` made on Windows stores backslash paths; `Path()` on POSIX would
    # take the whole thing as one filename and propose the drive letter too.
    edl = _edl_with_v1("C:\\Users\\x\\uploads\\Beach Trip\\Beach Trip.normalized.mp4")
    out = _main._export_payload("s_abc123", _Res("/w/exports/export_ff00.mov"), edl)
    assert out["suggested_filename"] == f"Beach_Trip_{TODAY}.mov"


def test_the_name_is_legal_on_windows_too():
    # This string is handed straight to a native save dialog, so it must not
    # carry any of NTFS's illegal characters (< > : " | ? * and the separators).
    edl = _edl_with_v1('/u/a:b*c?"d|e<f>g.normalized.mp4')
    name = _main._export_payload("s_abc123", _Res("/w/exports/e.mp4"), edl)["suggested_filename"]
    assert not (set(name) & set('<>:"|?*\\/'))
    assert name.endswith(f"_{TODAY}.mp4")


def test_an_unrenderable_stem_degrades_to_a_word_not_a_bare_date():
    # `_safe_filename` keeps only [A-Za-z0-9._-], so an all-Devanagari stem
    # sanitises to nothing; the fallback must still say what the file is.
    edl = _edl_with_v1("/u/नमस्ते.normalized.mp4")
    name = _main._export_payload("s_abc123", _Res("/w/exports/e.mp4"), edl)["suggested_filename"]
    assert name.startswith("video_")
    assert name.endswith(f"_{TODAY}.mp4")


def test_an_empty_timeline_still_gets_a_sensible_name():
    edl = empty_edl(Canvas(w=320, h=180, fps=30))
    out = _main._export_payload("s_abc123", _Res("/w/exports/export_ff00.mp4"), edl)
    assert out["suggested_filename"] == f"video_{TODAY}.mp4"


def test_the_session_name_is_used_only_when_someone_chose_one(tmp_path, monkeypatch):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    edl = empty_edl(Canvas(w=320, h=180, fps=30))

    # Default name IS the session id — a hash by another name, so it is skipped.
    (tmp_path / "s_abc123").mkdir()
    _storage.write_meta("s_abc123", {"name": "s_abc123"})
    assert _main._export_payload("s_abc123", _Res("/w/e.mp4"), edl)["suggested_filename"] \
        == f"video_{TODAY}.mp4"

    _storage.write_meta("s_abc123", {"name": "Launch cut"})
    assert _main._export_payload("s_abc123", _Res("/w/e.mp4"), edl)["suggested_filename"] \
        == f"Launch_cut_{TODAY}.mp4"


def test_a_corrupt_meta_json_cannot_fail_an_export_that_already_rendered(
        tmp_path, monkeypatch):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    (tmp_path / "s_abc123").mkdir()
    (tmp_path / "s_abc123" / "meta.json").write_text("{not json", encoding="utf-8")
    out = _main._export_payload("s_abc123", _Res("/w/e.mp4"),
                                empty_edl(Canvas(w=320, h=180, fps=30)))
    assert out["suggested_filename"] == f"video_{TODAY}.mp4"


def test_the_on_disk_fields_are_unchanged_and_the_new_one_is_additive():
    edl = _edl_with_v1("/u/clip.normalized.mp4")
    out = _main._export_payload("s_abc123", _Res("/w/s_x/exports/export_ff00.mp4"), edl)
    # `filename` is what desktop.py::save_export resolves under exports/ — it
    # must stay the real leaf, never the pretty one.
    assert out["filename"] == "export_ff00.mp4"
    assert out["url"] == "/api/sessions/s_abc123/files/exports/export_ff00.mp4"
    assert out["path"] == str(Path("/w/s_x/exports/export_ff00.mp4"))
    assert out["suggested_filename"] != out["filename"]


def test_a_caller_with_no_edl_gets_exactly_the_old_payload():
    out = _main._export_payload("s_abc123", _Res("/w/s_x/exports/export_ff00.mp4"))
    assert set(out) == {"path", "filename", "url"}


# --- G4 over HTTP: the route actually emits the field ------------------------

@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path)
    _main._STORES.clear()
    return TestClient(_main.app)


def test_the_sync_export_route_returns_the_suggested_name(client, tmp_path, monkeypatch):
    from video_ai_editor.edl import EDLStore
    sid = "s_route01"
    sd = tmp_path / sid
    sd.mkdir(parents=True)
    store = EDLStore(sd)
    store.edl = _edl_with_v1("/u/Wedding Reel/Wedding Reel.normalized.mp4")
    _main._STORES[sid] = store

    exported = sd / "exports" / "export_ff00.mp4"
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_bytes(b"")
    # The render itself is not under test — only that the route hands the
    # payload builder the EDL it just rendered.
    monkeypatch.setattr(_main, "render_export", lambda *a, **k: _Res(str(exported)))
    monkeypatch.setattr(_main, "_require_video_engine", lambda: None)

    r = client.post(f"/api/sessions/{sid}/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "export_ff00.mp4"
    assert body["suggested_filename"] == f"Wedding_Reel_{TODAY}.mp4"
