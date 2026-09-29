"""Final sweep 2, round 2 (timeline backend): confirmed findings, each pinned
by a test that failed before its fix.

* auto_caption skipped speech on a PIP / overlay video lane, so the guest of
  a stacked split-screen podcast got no captions.
* move_clip on the main lane accepted a same-lane move into free space: it
  opened a black gap that the next unrelated edit silently closed.
* Two stickers over the same time shared one lane; the later bar hid the
  other's start edge.
* GET /preview.mp4?h= joined the raw `h` onto the previews folder, so a
  relative or absolute `h` served any .mp4 on the Mac.
* undo()/redo() mutated memory before writing, with non-atomic writes: on a
  full disk the saved Redo history was wiped and later Redo presses were dead.
* An opened .vae kept an unreadable undo snapshot: every Undo then failed
  with a raw pydantic dump and pushed a phantom Redo entry.
"""
from __future__ import annotations

import errno
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, Clip, Track

W, H, FPS = 160, 90, 30


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("sweep2r2_media")
    out = {"host": d / "host.mp4", "guest": d / "guest.mp4"}
    for key, f in (("host", 440), ("guest", 660)):
        _ff("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=12:r={FPS}",
            "-f", "lavfi", "-i", f"sine=f={f}:d=12",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out[key]))
    return out


def _store(tmp: Path, v1: list[Clip], v2: list[Clip] = ()) -> EDLStore:
    tracks = [Track(id="v1", type="video", clips=list(v1)),
              Track(id="v2", type="video", z=1, clips=list(v2)),
              Track(id="music", type="music", z=0),
              Track(id="vo", type="vo", z=0)]
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=tracks)
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


# ------------------------------------------------ 1. captions on a PIP lane

class _FakeTx:
    def __init__(self, segments: list[dict]):
        self._d = {"language": "en", "duration": 12.0, "segments": segments}
        self.language = "en"
        self.duration = 12.0

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self._d))


def _seg(words: list[tuple[str, float, float]]) -> dict:
    return {"start": words[0][1], "end": words[-1][2], "text": " ".join(w for w, _a, _b in words),
            "words": [{"word": w, "start": a, "end": b} for w, a, b in words]}


@pytest.fixture
def fake_tx(monkeypatch):
    from video_ai_editor.ingest import transcribe as T
    speech: dict[str, list[dict]] = {}
    calls: list[str] = []

    def fake(path, language=None, model_size=None, backend=None, task="transcribe",
             on_progress=None, should_cancel=None):
        calls.append(Path(path).name)
        return _FakeTx(speech.get(Path(path).name, []))

    monkeypatch.setattr(T, "transcribe", fake)
    return speech, calls


@pytest.fixture
def cmedia(tmp_path, media) -> dict[str, Path]:
    d = tmp_path / "m"
    d.mkdir()
    return {k: Path(shutil.copy(v, d / v.name)) for k, v in media.items()}


def _cues(st: EDLStore) -> list[tuple[float, float, str]]:
    cap = st.edl.get_track("captions")
    return [(c.start, c.end, c.text.replace("\n", " ").lower()) for c in (cap.clips if cap else [])]


def test_speech_sources_include_a_pip_lane(tmp_path, cmedia):
    from video_ai_editor.agent.caption_sources import TIER, speech_sources
    st = _store(tmp_path / "s", [Clip(id="h", src=str(cmedia["host"]), in_=0, out=10, start=0)],
                [Clip(id="g", src=str(cmedia["guest"]), in_=0, out=10, start=0)])
    got = [(Path(s.src).name, s.kind, s.track_id) for s in speech_sources(st.edl)]
    assert ("guest.mp4", "pip", "v2") in got, got
    assert TIER["v1"] < TIER["pip"] < TIER["audio"]


def test_a_muted_pip_lane_and_a_pip_copy_of_the_main_file_are_not_captioned_twice(tmp_path, cmedia):
    from video_ai_editor.agent.caption_sources import speech_sources
    st = _store(tmp_path / "s", [Clip(id="h", src=str(cmedia["host"]), in_=0, out=10, start=0)],
                [Clip(id="g", src=str(cmedia["host"]), in_=0, out=4, start=2)])
    assert [s.kind for s in speech_sources(st.edl)] == ["v1"]
    st.edl.get_track("v2").clips = [Clip(id="g", src=str(cmedia["guest"]), in_=0, out=4, start=2)]
    st.edl.get_track("v2").muted = True
    assert [s.kind for s in speech_sources(st.edl)] == ["v1"]


def test_the_guest_on_a_pip_lane_is_captioned_with_the_host(tmp_path, cmedia, fake_tx):
    speech, calls = fake_tx
    speech["host.mp4"] = [_seg([("welcome", 0.5, 1.0), ("back", 1.1, 1.5)])]
    speech["guest.mp4"] = [_seg([("thanks", 4.0, 4.5), ("for", 4.6, 4.8), ("having", 4.9, 5.4)])]
    st = _store(tmp_path / "s", [Clip(id="h", src=str(cmedia["host"]), in_=0, out=10, start=0)],
                [Clip(id="g", src=str(cmedia["guest"]), in_=0, out=10, start=0)])
    out = dispatch(st, "auto_caption", {})
    assert "guest.mp4" in calls
    text = " ".join(t for *_s, t in _cues(st))
    assert "welcome" in text and "thanks" in text, _cues(st)
    assert "guest.mp4" in out["summary"] and "host.mp4" in out["summary"]
    thanks = next(a for a, _b, t in _cues(st) if "thanks" in t)
    assert thanks == pytest.approx(4.0, abs=1e-3)


def test_a_trimmed_pip_is_captioned_where_it_is_heard(tmp_path, cmedia, fake_tx):
    """A PIP at 02:00 whose source starts at 1 s: a word at source 1.5 s is
    heard (and captioned) at 02:15, and a word before its in-point is not."""
    speech, _calls = fake_tx
    speech["guest.mp4"] = [_seg([("cut", 0.2, 0.6)]), _seg([("kept", 1.5, 2.0)])]
    st = _store(tmp_path / "s", [Clip(id="h", src=str(cmedia["host"]), in_=0, out=10, start=0)],
                [Clip(id="g", src=str(cmedia["guest"]), in_=1, out=5, start=2)])
    dispatch(st, "auto_caption", {})
    cues = _cues(st)
    assert not any("cut" in t for *_s, t in cues), cues
    (a, _b, _t), = [c for c in cues if "kept" in c[2]]
    assert a == pytest.approx(2.5, abs=1e-3)


# ------------------------------------------------ 2. the main lane keeps no gaps

def _three(tmp: Path, media) -> EDLStore:
    return _store(tmp, [Clip(id=f"c{i}", src=str(media["host"]), in_=0, out=4, start=4 * i)
                        for i in range(3)])


def _v1(st: EDLStore) -> list[tuple[str, float, float, float]]:
    return [(c.id, round(c.start, 3), c.in_, c.out) for c in st.edl.get_track("v1").clips]


def test_moving_the_last_main_clip_into_free_space_is_refused(tmp_path, media):
    st = _three(tmp_path / "s", media)
    before = _v1(st)
    with pytest.raises(ValueError, match="end to end"):
        dispatch(st, "move_clip", {"clip_id": "c2", "new_start": 10.0})
    assert _v1(st) == before
    assert st.edl.video_extent() == pytest.approx(12.0)
    # A later unrelated trim therefore packs the lane the ordinary way.
    dispatch(st, "trim_clip", {"clip_id": "c0", "out": 3.5})
    assert [c[1] for c in _v1(st)] == [0.0, 3.5, 7.5]


def test_moving_a_middle_main_clip_past_the_end_is_refused(tmp_path, media):
    st = _three(tmp_path / "s", media)
    with pytest.raises(ValueError, match="end to end"):
        dispatch(st, "move_clip", {"clip_id": "c1", "new_start": 800.0})
    assert st.edl.video_extent() == pytest.approx(12.0)


def test_a_one_frame_nudge_of_the_last_main_clip_is_refused(tmp_path, media):
    st = _three(tmp_path / "s", media)
    with pytest.raises(ValueError, match="end to end"):
        dispatch(st, "move_clip", {"clip_id": "c2", "new_start": 8.0 + 1 / FPS})


def test_a_drag_reorder_and_a_move_that_closes_a_legacy_gap_still_work(tmp_path, media):
    st = _three(tmp_path / "s", media)
    dispatch(st, "move_clip", {"clip_id": "c2", "new_start": 0.0, "close_gap": True})
    assert [c[0] for c in _v1(st)] == ["c2", "c0", "c1"]
    # A lane that already holds a gap (an older project) may have it closed.
    legacy = _store(tmp_path / "l", [Clip(id="a", src=str(media["host"]), in_=0, out=4, start=0),
                                     Clip(id="b", src=str(media["host"]), in_=0, out=4, start=6)])
    dispatch(legacy, "move_clip", {"clip_id": "b", "new_start": 4.0})
    assert [c[1] for c in _v1(legacy)] == [0.0, 4.0]


# ------------------------------------------------ 3. overlapping stickers get their own lane

def _sticker_lanes(st: EDLStore) -> dict[str, list[tuple[float, float]]]:
    return {t.id: [(c.start, c.end) for c in t.clips]
            for t in st.edl.tracks if t.type == "sticker" and t.clips}


def test_two_stickers_at_the_same_time_land_on_different_lanes(tmp_path, media):
    st = _three(tmp_path / "s", media)
    a = dispatch(st, "add_sticker", {"emoji": "\U0001f52a", "start": 2.0, "end": 5.0})["sticker_id"]
    out = dispatch(st, "add_sticker", {"emoji": "⭐", "start": 0.0, "end": 3.0})
    b = out["sticker_id"]
    assert out["sticker_count"] == 2     # every sticker lane counts
    ta, tb = st.edl.get_clip(a)[0], st.edl.get_clip(b)[0]
    assert ta.id != tb.id, _sticker_lanes(st)
    assert ta.type == tb.type == "sticker" and ta.z == tb.z
    # One that overlaps neither goes back on the first lane.
    c = dispatch(st, "add_sticker", {"emoji": "\U0001f525", "start": 6.0, "end": 8.0})["sticker_id"]
    assert st.edl.get_clip(c)[0].id == "stickers"


# ------------------------------------------------ 4. /preview.mp4 serves only this session's renders

@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    wd = tmp_path / "wd"
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd, raising=False)
    wd.mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return _main, TestClient(_main.app), wd


def test_preview_h_cannot_name_a_file_outside_the_previews_folder(client):
    _main, c, wd = client
    sid = c.post("/api/sessions", json={"name": "p"}).json()["id"]
    outside = wd.parent / "outside"
    outside.mkdir()
    secret = outside / "private.mp4"
    secret.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"S" * 4000)
    other = wd / sid / "exports"
    other.mkdir(parents=True, exist_ok=True)
    (other / "mine.mp4").write_bytes(b"E" * 3000)
    hdr = {"Sec-Fetch-Site": "same-origin"}
    for h in ("../../../outside/private", str(outside / "private"), "../exports/mine",
              "0123456789abcdef/../../exports/mine"):
        r = c.get(f"/api/sessions/{sid}/preview.mp4", params={"h": h}, headers=hdr)
        assert r.status_code == 404, (h, r.status_code)
        assert b"SSSS" not in r.content and b"EEEE" not in r.content
    # A real render hash that is on disk is still served.
    real = wd / sid / "previews" / "0123456789abcdef.mp4"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"R" * 2000)
    r = c.get(f"/api/sessions/{sid}/preview.mp4", params={"h": "0123456789abcdef"}, headers=hdr)
    assert r.status_code == 200 and r.content == b"R" * 2000


# ------------------------------------------------ 5. undo/redo are all-or-nothing on a full disk

def _texts(st: EDLStore) -> list[str]:
    return sorted(c.text for t in st.edl.tracks if t.type == "text" for c in t.clips)


def _history(tmp: Path) -> EDLStore:
    st = EDLStore(tmp)
    for i in range(3):
        dispatch(st, "add_text", {"text": f"Title {i}", "start": 3.0 * i, "end": 3.0 * i + 2.0})
    for _ in range(3):
        assert st.undo()
    assert st.redo()
    assert _texts(st) == ["Title 0"]
    return st


@pytest.fixture
def full_disk(monkeypatch):
    """Every Path.write_text fails with ENOSPC while armed."""
    armed = {"on": False}
    real = Path.write_text

    def write_text(self, *a, **k):
        if armed["on"]:
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", write_text)
    return armed


def _files(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_undo_on_a_full_disk_keeps_the_redo_history(tmp_path, full_disk):
    st = _history(tmp_path / "s")
    disk = _files(st.dir)
    redo_n, ops_n, depth = len(st._redo_stack), len(st.ops.ops), st.undo_depth
    full_disk["on"] = True
    for _ in range(2):
        with pytest.raises(OSError):
            st.undo()
    full_disk["on"] = False
    assert _files(st.dir) == disk
    assert (len(st._redo_stack), len(st._redo_ops), len(st.ops.ops), st.undo_depth) \
        == (redo_n, redo_n, ops_n, depth)
    assert _texts(st) == ["Title 0"]
    # Space is back: every Redo restores a real step, and a reopened store agrees.
    assert st.redo() and _texts(st) == ["Title 0", "Title 1"]
    assert st.redo() and _texts(st) == ["Title 0", "Title 1", "Title 2"]
    assert not st.redo()
    assert _texts(EDLStore(st.dir)) == ["Title 0", "Title 1", "Title 2"]


def test_redo_on_a_full_disk_changes_nothing(tmp_path, full_disk):
    st = _history(tmp_path / "s")
    disk = _files(st.dir)
    redo_n, ops_n = len(st._redo_stack), len(st.ops.ops)
    full_disk["on"] = True
    with pytest.raises(OSError):
        st.redo()
    full_disk["on"] = False
    assert _files(st.dir) == disk
    assert (len(st._redo_stack), len(st.ops.ops)) == (redo_n, ops_n)
    assert _texts(st) == ["Title 0"]
    assert st.redo() and _texts(st) == ["Title 0", "Title 1"]


# ------------------------------------------------ 6. a damaged undo snapshot ends history cleanly

def _truncate(p: Path) -> None:
    raw = p.read_text(encoding="utf-8")
    p.write_text(raw[: len(raw) // 2], encoding="utf-8")


def _edited(tmp: Path, n: int = 5) -> EDLStore:
    st = EDLStore(tmp)
    for i in range(n):
        dispatch(st, "add_text", {"text": f"T{i}", "start": 3.0 * i, "end": 3.0 * i + 2.0})
    return st


def test_undo_onto_an_unreadable_snapshot_stops_cleanly(tmp_path):
    st = _edited(tmp_path / "s")
    snaps = st._snapshot_files()
    _truncate(snaps[-2])                       # the one the first undo reads
    before = _texts(st)
    with pytest.raises(ValueError) as ei:
        st.undo()
    msg = str(ei.value)
    assert "damaged" in msg.lower() and "validation error" not in msg.lower()
    assert "input_value" not in msg
    # History now ends here: later presses are a plain "nothing to undo".
    for _ in range(2):
        assert not st.undo()
    assert _texts(st) == before
    assert not st.redo_available and not st._redo_stack
    assert st.undo_depth == 0
    assert _texts(EDLStore(st.dir)) == before


def test_undo_reaches_the_readable_steps_before_a_damaged_one(tmp_path):
    st = _edited(tmp_path / "s")
    snaps = st._snapshot_files()
    _truncate(snaps[1])                        # an older one
    assert st.undo() and st.undo() and st.undo()
    assert _texts(st) == ["T0", "T1"]
    with pytest.raises(ValueError, match="(?i)damaged"):
        st.undo()
    assert _texts(st) == ["T0", "T1"] and len(st._redo_stack) == 3
    assert st.redo() and _texts(st) == ["T0", "T1", "T2"]


def test_opening_a_project_drops_an_unreadable_snapshot_and_everything_older(tmp_path):
    from video_ai_editor.storage_project import _write_state_files
    src = _edited(tmp_path / "src")
    assert src.undo()                          # one redo entry too
    unpack = tmp_path / "unpack"
    shutil.copytree(src.dir, unpack)
    snaps = sorted((unpack / "snapshots").glob("*.json"))
    _truncate(snaps[-3])
    bad_name = snaps[-3].name
    sd = tmp_path / "wd" / "s_new000001"
    sd.mkdir(parents=True)
    _write_state_files(sd, unpack, {})
    kept = sorted(p.name for p in (sd / "snapshots").glob("*.json"))
    assert kept == [p.name for p in snaps[-2:]], (kept, bad_name)
    st = EDLStore(sd)
    assert st.undo_depth == 1 and len(st._redo_stack) == 1
    assert st.undo()
    assert not st.undo()
    assert len(st._redo_stack) == 2


def test_opening_a_project_keeps_the_readable_top_of_a_damaged_redo_stack(tmp_path):
    from video_ai_editor.storage_project import _write_state_files
    src = _edited(tmp_path / "src")
    for _ in range(3):
        assert src.undo()
    unpack = tmp_path / "unpack"
    shutil.copytree(src.dir, unpack)
    stack = json.loads((unpack / "redo_stack.json").read_text())
    stack[0] = {"tracks": "not a list"}        # the OLDEST undone step (redone last)
    (unpack / "redo_stack.json").write_text(json.dumps(stack))
    sd = tmp_path / "wd" / "s_new000002"
    sd.mkdir(parents=True)
    _write_state_files(sd, unpack, {})
    st = EDLStore(sd)
    assert len(st._redo_stack) == 2
    assert st.redo() and _texts(st) == ["T0", "T1", "T2"]
    assert st._redo_ops[-1] is not None and st._redo_ops[-1].summary.startswith("Added")
    assert st.redo() and _texts(st) == ["T0", "T1", "T2", "T3"]
    assert not st.redo()
