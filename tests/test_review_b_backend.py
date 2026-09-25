"""Wave-B review findings on the backend (fixer lane), one test per finding.

Each test executes the real code path (dispatch, the FastAPI app through
TestClient, real ffmpeg renders measured with ffprobe/astats) and failed on
the tree before the fix.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import (EDL, Canvas, CaptionsConfig, Clip, TextClip, TextStyle,
                                        Track)


def _ff(*args: str) -> None:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("rb_media")
    clip12 = d / "clip12.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", str(clip12))
    clip2 = d / "clip2.mp4"
    _ff("-f", "lavfi", "-i", "color=c=green:size=160x90:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip2))
    # A rising tone (amplitude grows with t): played backwards it FALLS.
    ramp = d / "ramp.mp4"
    _ff("-f", "lavfi", "-i", "color=c=gray:size=160x90:rate=30:duration=4",
        "-f", "lavfi", "-i", "aevalsrc='0.02+0.2*t*sin(2*PI*440*t)':s=48000:d=4:c=stereo",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(ramp))
    bed = d / "bed.wav"   # an off-grid length, like a real song (3.0213 s)
    _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=3.0213:sample_rate=48000", str(bed))
    short_wav = d / "short.wav"
    _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=0.2:sample_rate=48000", str(short_wav))
    return {"clip12": clip12, "clip2": clip2, "ramp": ramp, "bed": bed, "short_wav": short_wav}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from video_ai_editor import config, main as _main, storage as _storage
    from video_ai_editor.api.hardening import RATE
    wd = tmp_path / "wd"
    for mod in (config, _storage, _main):
        monkeypatch.setattr(mod, "WORKDIR", wd, raising=False)
    wd.mkdir()
    RATE.windows.clear()
    _main._STORES.clear()
    return _main, TestClient(_main.app), wd


def _session(c: TestClient, name: str = "rb") -> str:
    return c.post("/api/sessions", json={"name": name}).json()["id"]


def _d(c: TestClient, sid: str, tool: str, **args):
    return c.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})


def _edl(c: TestClient, sid: str) -> dict:
    return c.get(f"/api/sessions/{sid}/edl").json()


def _clips(edl: dict, track: str) -> list[dict]:
    return next((t for t in edl["tracks"] if t["id"] == track), {"clips": []})["clips"]


def _frames(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return int(out)


# ------------------------------------------------ QA-041: set_property bounds

@pytest.mark.parametrize("path,value", [
    ("start", 1e9), ("in", 1e9), ("out", 1e9), ("speed", 1e6),
    ("audio.gain_db", 1e6), ("out", -5.0),
])
def test_set_property_refuses_absurd_media_clip_values(client, media, path, value):
    _m, c, _wd = client
    sid = _session(c)
    assert _d(c, sid, "add_clip", track="v1", src=str(media["clip2"]), **{"in": 0, "out": 2, "start": 0}).status_code == 200
    cid = _clips(_edl(c, sid), "v1")[0]["id"]
    r = _d(c, sid, "set_property", clip_id=cid, path=path, value=value)
    assert r.status_code == 400, r.text
    assert "set_property." not in r.json()["error"]["message"] if isinstance(r.json().get("error"), dict) else True
    e = _edl(c, sid)
    assert e["duration"] <= 21600
    clip = _clips(e, "v1")[0]
    assert (clip["start"], clip["in"], clip["out"], clip["speed"]) == (0.0, 0.0, 2.0, None)


def test_the_model_clamps_what_set_property_would_refuse():
    """An EDL that already holds a bad value stays loadable (clamped)."""
    c = Clip(src="x.mp4", in_=0, out=1e12, start=1e9, speed=1e6)
    assert c.start == 21600 and c.out == 21600 and c.speed == 100
    assert Clip(src="x.mp4", speed=0).speed is None
    with pytest.raises(ValueError):
        Clip(src="x.mp4", start=float("nan"))
    st = TextStyle(size=1e6, stroke_w=1e5)
    assert st.size == 2000 and st.stroke_w == 200


def test_preview_deadline_is_capped():
    from video_ai_editor.render.cancel import DEADLINE_BASE_S, DEADLINE_PER_TIMELINE_S, preview_deadline_s
    cap = DEADLINE_BASE_S + DEADLINE_PER_TIMELINE_S * 21600
    assert preview_deadline_s(1e9) == cap
    assert preview_deadline_s(float("nan")) == cap
    assert preview_deadline_s(10.0) == DEADLINE_BASE_S + DEADLINE_PER_TIMELINE_S * 10


# ------------------------------------------------ QA-107: text bounds + 422

def test_text_size_and_stroke_are_bounded_everywhere(tmp_path):
    cues = [TextClip(id="t_c0", text="hi", start=0, end=1, role="caption")]
    edl = EDL(canvas=Canvas(w=320, h=240), tracks=[
        Track(id="captions", type="captions", config=CaptionsConfig(enabled=True), clips=cues),
        Track(id="text", type="text", clips=[TextClip(id="t_x", text="hi", start=0, end=1)])])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    for tool, args in (("set_caption_style", {"size": 1e6}), ("set_caption_style", {"stroke_w": 1e5}),
                       ("set_property", {"clip_id": "t_x", "path": "style.size", "value": 1e6}),
                       ("set_property", {"clip_id": "t_x", "path": "style.stroke_w", "value": 1e5})):
        with pytest.raises(ValueError):
            dispatch(store, tool, args)
    assert store.edl.get_clip("t_x")[1].style.size == 96


@pytest.mark.parametrize("exc", ["oserror", "valueerror", "freetype"])
def test_a_render_that_raises_non_runtime_errors_is_422_not_500(client, media, monkeypatch, exc):
    main, c, _wd = client
    sid = _session(c)
    _d(c, sid, "add_clip", track="v1", src=str(media["clip2"]), **{"in": 0, "out": 2, "start": 0})

    def boom(*a, **k):
        if exc == "oserror":
            raise OSError("invalid pixel size")
        if exc == "valueerror":
            raise ValueError("bad layout")
        import freetype
        raise freetype.FT_Exception(0x62)

    monkeypatch.setattr(main, "render_export", boom)
    monkeypatch.setattr(main, "render_preview", boom)
    r = c.post(f"/api/sessions/{sid}/export")
    assert r.status_code == 422, r.text
    assert "render_failed" in r.text
    r = c.post(f"/api/sessions/{sid}/preview")
    assert r.status_code == 422, r.text


# ------------------------------------------------ REV-B8-MASTER-HASH

def test_export_reports_the_timelines_hash_not_the_master_views(client, media, monkeypatch):
    main, c, _wd = client
    sid = _session(c)
    _d(c, sid, "add_clip", track="v1", src=str(media["clip2"]), **{"in": 0, "out": 2, "start": 0})
    master = media["clip2"].with_name("clip2.master1234.mp4")
    if not master.exists():
        master.write_bytes(media["clip2"].read_bytes())

    def fake_export_edl(edl, height, **kw):     # what ingest.masters.export_edl does
        out = edl.model_copy(deep=True)
        for t in out.tracks:
            for cl in t.clips:
                if getattr(cl, "src", None):
                    cl.src = str(master)
        return out

    monkeypatch.setattr(main, "_export_edl", fake_export_edl)
    r = c.post(f"/api/sessions/{sid}/export", json={"height": 90})
    assert r.status_code == 200, r.text
    head = c.get(f"/api/sessions/{sid}/head").json()
    assert r.json()["edl_hash"] == head["edl_hash"], (r.json(), head)


# ------------------------------------------------ REV-B8-PREVIEW-JOB-OFFLINE

def test_preview_job_slates_missing_media_like_the_sync_path(client, media, tmp_path):
    main, c, _wd = client
    sid = _session(c)
    src = tmp_path / "gone.mp4"
    src.write_bytes(media["clip2"].read_bytes())
    _d(c, sid, "add_clip", track="v1", src=str(src), **{"in": 0, "out": 2, "start": 0})
    src.unlink()
    r = c.post(f"/api/sessions/{sid}/preview?wait=0")
    assert r.status_code == 202, r.text
    job = r.json()["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        j = c.get(f"/api/jobs/{job}").json()
        if j["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.2)
    assert j["status"] == "completed", j
    sync = c.post(f"/api/sessions/{sid}/preview").json()
    assert j["result"]["edl_hash"] == sync["edl_hash"]


# ------------------------------------------------ REV-B2-SID-ROUTES

@pytest.mark.parametrize("bad", [".", ".."])
def test_malformed_session_ids_never_materialise_a_store(client, bad):
    main, _c, wd = client
    from fastapi import HTTPException
    before = sorted(p.name for p in wd.iterdir())
    for fn in (lambda: main.render_cache_usage(bad), lambda: main.clear_render_cache(bad),
               lambda: main._store(bad), lambda: main.save_project_endpoint(bad)):
        with pytest.raises(HTTPException) as ei:
            fn()
        assert ei.value.status_code == 400
    assert sorted(p.name for p in wd.iterdir()) == before
    assert not (wd / "cache").exists() and not (wd.parent / "cache").exists()


# ------------------------------------------------ REV-B5-CLEAR-CACHE

def test_clear_render_cache_keeps_the_offline_preview_on_screen(client, media, tmp_path):
    main, c, wd = client
    sid = _session(c)
    src = tmp_path / "gone2.mp4"
    src.write_bytes(media["clip2"].read_bytes())
    _d(c, sid, "add_clip", track="v1", src=str(src), **{"in": 0, "out": 2, "start": 0})
    src.unlink()
    shown = c.post(f"/api/sessions/{sid}/preview").json()["edl_hash"]   # the slated view
    p = wd / sid / "previews" / f"{shown}.mp4"
    assert p.exists()
    r = c.delete(f"/api/sessions/{sid}/render-cache")
    assert r.status_code == 200
    assert p.exists(), "Clear deleted the preview that is playing"


def test_clear_render_cache_spares_fresh_entries_while_a_render_runs(tmp_path, monkeypatch):
    from video_ai_editor.render import cache_budget, compositor
    sd = tmp_path / "s_clearcache1"
    chunk = sd / "cache" / "chunks" / "chunk_x.mp4"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"x" * 4096)
    with compositor._INFLIGHT_LOCK:
        compositor._INFLIGHT[f"{sd.name}/abc"] = object()   # a preview is rendering
    try:
        assert compositor.session_render_in_flight(sd)
        recent = cache_budget.PROTECT_RECENT_S if compositor.session_render_in_flight(sd) else 0.0
        cache_budget.clear(sd, protect_recent_s=recent)
        assert chunk.exists()
    finally:
        with compositor._INFLIGHT_LOCK:
            compositor._INFLIGHT.pop(f"{sd.name}/abc", None)
    assert not compositor.session_render_in_flight(sd)
    cache_budget.clear(sd)
    assert not chunk.exists()


# ------------------------------------------------ REV-B8-RELINK-LEAK

def test_a_refused_audio_relink_keeps_no_file(client, media, tmp_path):
    main, c, wd = client
    sid = _session(c)
    src = wd / sid / "uploads" / "audio" / "bed_12345678.wav"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(media["bed"].read_bytes())
    assert _d(c, sid, "add_music", src=str(src), start=0, out=3.0).status_code == 200
    src.rename(src.with_name("bed_12345678.wav.moved"))
    items = c.get(f"/api/sessions/{sid}/media").json()["media"]
    [item] = [it for it in items if it["missing"]]
    before = sorted(p.name for p in (wd / sid / "uploads" / "audio").iterdir())
    with media["short_wav"].open("rb") as f:
        r = c.post(f"/api/sessions/{sid}/media/{item['id']}/relink",
                   files={"file": ("tiny.wav", f, "audio/wav")})
    assert r.status_code == 422, r.text
    assert sorted(p.name for p in (wd / sid / "uploads" / "audio").iterdir()) == before
    names = [it["name"] for it in c.get(f"/api/sessions/{sid}/media").json()["media"]]
    assert "tiny.wav" not in names


# ------------------------------------------------ REV-B8-MUSIC-GRID / QA-083

def test_a_second_music_import_starts_on_the_frame_grid(client, media):
    main, c, _wd = client
    sid = _session(c)
    for _ in range(2):
        with media["bed"].open("rb") as f:
            r = c.post(f"/api/sessions/{sid}/audio_upload", files={"file": ("bed.wav", f, "audio/wav")})
        assert r.status_code == 200, r.text
    edl = _edl(c, sid)
    fps = edl["canvas"]["fps"]
    starts = sorted(cl["start"] for cl in _clips(edl, "music"))
    assert len(starts) == 2
    for s in starts:
        assert abs(s * fps - round(s * fps)) < 1e-6, starts
    assert starts[1] >= 3.0213, "the second bed must not overlap the first"
    # the dispatch path too (no start → lane end, ceiled)
    assert _d(c, sid, "add_music", src=_clips(edl, "music")[0]["src"], out=3.0213).status_code == 200
    s3 = max(cl["start"] for cl in _clips(_edl(c, sid), "music"))
    assert abs(s3 * fps - round(s3 * fps)) < 1e-6 and s3 >= starts[1] + 3.0213 - 1e-6


# ------------------------------------------------ set_canvas fps requantise

def test_set_canvas_fps_requantises_the_timeline_to_the_new_grid(client, media, tmp_path):
    main, c, wd = client
    sid = _session(c)
    _d(c, sid, "add_clip", track="v1", src=str(media["clip12"]), **{"in": 0, "out": 12, "start": 0})
    assert _d(c, sid, "set_canvas", fps=29.97).status_code == 200
    v1 = _clips(_edl(c, sid), "v1")
    _d(c, sid, "split_at", clip_id=v1[0]["id"], time=6.0)
    _d(c, sid, "split_at", clip_id=_clips(_edl(c, sid), "v1")[1]["id"], time=9.0)
    assert _d(c, sid, "add_transition", at=6.0, type="fade", duration=0.4).status_code == 200
    edl = _edl(c, sid)
    fps = 30000 / 1001
    v1 = _clips(edl, "v1")
    tr = next(t for t in edl["tracks"] if t["id"] == "v1")["transitions"][0]
    assert abs(tr["at"] - v1[1]["start"]) < 1e-9, (tr, v1[1])
    for cl in v1:
        for k in ("start", "in", "out"):
            assert abs(cl[k] * fps - round(cl[k] * fps)) < 1e-6, (k, cl)
    n = edl["duration"] * fps
    assert abs(n - round(n)) < 1e-6, edl["duration"]
    r = c.post(f"/api/sessions/{sid}/export", json={"height": 90})
    assert r.status_code == 200, r.text
    assert _frames(Path(r.json()["path"])) == round(n)


# ------------------------------------------------ captions: no stub on a cut

def test_cut_range_over_whole_cues_leaves_no_overlapping_captions(tmp_path, media):
    cues = [TextClip(id="a", text="before", start=8.0, end=10.5, role="caption"),
            TextClip(id="b", text="the grip", start=10.5, end=12.0, role="caption"),
            TextClip(id="c", text="is deeper", start=12.0, end=13.0, role="caption"),
            TextClip(id="d", text="so you can see", start=20.0, end=20.9, role="caption")]
    edl = EDL(canvas=Canvas(w=320, h=240, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="v", src=str(media["clip12"]), in_=0, out=12, start=0),
                                            Clip(id="w", src=str(media["clip12"]), in_=0, out=12, start=12)]),
        Track(id="captions", type="captions", config=CaptionsConfig(enabled=True), clips=cues)])
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    dispatch(store, "cut_range", {"track": "v1", "start": 10.0, "end": 20.0})
    caps = sorted(store.edl.get_track("captions").clips, key=lambda x: x.start)
    assert [x.id for x in caps] == ["a", "d"], [(x.id, x.start, x.end) for x in caps]
    for x, y in zip(caps, caps[1:]):
        assert x.end <= y.start + 1e-9, (x, y)


# ------------------------------------------------ QA-099: control characters

def test_project_names_lose_control_characters(client):
    main, c, _wd = client
    sid = _session(c)
    r = c.patch(f"/api/sessions/{sid}", json={"name": "\u0000x"})
    assert r.status_code == 200, r.text
    rows = {s["id"]: s for s in c.get("/api/sessions").json()["sessions"]}
    assert rows[sid]["name"] == "x"
    assert c.patch(f"/api/sessions/{sid}", json={"name": "\u0000"}).status_code == 400
    assert c.patch(f"/api/sessions/{sid}", json={"name": "क्‍ष ‮trip"}).status_code == 200
    rows = {s["id"]: s for s in c.get("/api/sessions").json()["sessions"]}
    assert rows[sid]["name"] == "क्‍ष trip"


# ------------------------------------------------ QA-098: the .vae is named

def test_saved_project_is_named_after_the_project(client, media):
    main, c, _wd = client
    sid = _session(c)
    _d(c, sid, "add_clip", track="v1", src=str(media["clip2"]), **{"in": 0, "out": 2, "start": 0})
    assert c.patch(f"/api/sessions/{sid}", json={"name": "My Trip — दिल्ली"}).status_code == 200
    r = c.post(f"/api/sessions/{sid}/save_project")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "My Trip — दिल्ली.vae"
    got = c.get(body["url"])
    assert got.status_code == 200 and got.content[:2] == b"PK"


# ------------------------------------------------ REV-B7-DETACH-REVERSE

def _rms(path: Path, a: float, b: float) -> float:
    proc = subprocess.run([_pu.FFMPEG, "-v", "info", "-ss", f"{a}", "-t", f"{b - a}", "-i", str(path),
                           "-af", "astats=measure_overall=RMS_level:measure_perchannel=none", "-f", "null", "-"],
                          capture_output=True, text=True)
    vals = [float(x) for x in __import__("re").findall(r"RMS level dB:\s*(-?[\d.]+)", proc.stderr)]
    assert vals, proc.stderr[-600:]
    return vals[-1]


def test_detaching_a_reversed_clips_sound_keeps_it_playing_backwards(tmp_path, media):
    from video_ai_editor.render import render_preview
    edl = EDL(canvas=Canvas(w=160, h=90, fps=30), tracks=[
        Track(id="v1", type="video", clips=[Clip(id="c", src=str(media["ramp"]), in_=0, out=4, start=0,
                                                 reverse=True)])])
    edl.canvas.loudness_lufs = None
    (tmp_path / "edl.json").write_text(edl.model_dump_json())
    store = EDLStore(tmp_path)
    before = render_preview(store.edl, tmp_path, height=90).path
    assert _rms(before, 0.2, 1.0) > _rms(before, 3.0, 3.8) + 3, "fixture: reversed ramp must fall"
    dispatch(store, "detach_audio", {"clip_id": "c"})
    after = render_preview(store.edl, tmp_path, height=90).path
    head, tail = _rms(after, 0.2, 1.0), _rms(after, 3.0, 3.8)
    assert head > tail + 3, f"detached sound plays forwards: head {head} tail {tail}"


# ------------------------------------------------ QA-085 remainder: VO takes

def test_a_take_recorded_over_an_earlier_one_goes_on_a_free_lane(client, media, tmp_path):
    main, c, _wd = client
    sid = _session(c)
    take = tmp_path / "take.wav"
    _ff("-f", "lavfi", "-i", "sine=frequency=330:duration=3:sample_rate=48000", str(take))
    lanes = []
    for start in (2.0, 3.0, 20.0):
        with take.open("rb") as f:
            r = c.post(f"/api/sessions/{sid}/vo_record", files={"file": ("vo.wav", f, "audio/wav")},
                       data={"start": str(start), "gain_db": "0"})
        assert r.status_code == 200, r.text
        lanes.append(r.json()["track"])
    assert lanes[0] == "vo" and lanes[1] != "vo" and lanes[2] == "vo", lanes
    edl = _edl(c, sid)
    for t in edl["tracks"]:
        spans = sorted((cl["start"], cl["start"] + cl["out"] - cl["in"]) for cl in t["clips"] if "src" in cl)
        for a, b in zip(spans, spans[1:]):
            assert a[1] <= b[0] + 1e-6, (t["id"], spans)
