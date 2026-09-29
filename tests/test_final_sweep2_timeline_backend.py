"""Final sweep 2 (timeline backend): confirmed findings, each pinned by a test
that failed before its fix.

* set_speed on a main-track clip shortened the picture but left the music
  bed running on over a black tail (the r3 retime lost `_ripple_overlays`'s
  final `_fit_music_to_video`).
* auto_caption transcribed only the FIRST v1 clip's file: a second v1 source
  and the voiceover lane were never captioned.
* History printed add_clip / add_transition at their LAYOUT time, not where
  the ruler (and the Transitions panel) shows them after a transition.
* add_transition at a time that is not a cut stored a do-nothing transition
  that a later plain split turned into a crossfade.
* remove_silences padded the clip-slice edges too, leaving 0.1 s slivers at
  every seam a silence crossed.
* add_effect stored unchecked params (a bad value broke every render) and a
  lut `params.src` skipped the read-path allowlist.
* chroma_key spliced its colour raw into the ffmpeg filtergraph.
* GET /history, GET /files, POST sticker_upload and POST vo_record created a
  project (or wrote outside WORKDIR) for an unknown or path-shaped sid.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_ai_editor.agent import dispatch as D
from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.edl import EDLStore
from video_ai_editor.edl.schema import EDL, Canvas, ChromaKey, Clip, Effect, Track

W, H, FPS = 160, 90, 30


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("sweep2_media")
    out = {"a": d / "a.mp4", "b": d / "b.mp4", "bed": d / "bed.wav",
           "narr": d / "narration.wav", "gaps": d / "gaps.mp4"}
    for key in ("a", "b"):
        _ff("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=20:r={FPS}",
            "-f", "lavfi", "-i", "sine=f=440:d=20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out[key]))
    _ff("-f", "lavfi", "-i", "sine=f=220:d=10", str(out["bed"]))
    _ff("-f", "lavfi", "-i", "sine=f=330:d=6", str(out["narr"]))
    # Tone at source 0-3, 5-8 and 10-12: silence across 3-5 and 8-10.
    _ff("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:d=12:r={FPS}",
        "-f", "lavfi", "-i", "sine=f=440:d=12",
        "-af", "volume=enable='between(t,3,5)+between(t,8,10)':volume=0",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out["gaps"]))
    return out


def _store(tmp: Path, v1: list[Clip], *, extra: list[Track] = ()) -> EDLStore:
    tracks = [Track(id="v1", type="video", clips=list(v1)),
              Track(id="v2", type="video", z=1),
              Track(id="music", type="music", z=0),
              Track(id="vo", type="vo", z=0),
              *extra]
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS, loudness_lufs=None), tracks=tracks)
    edl.recompute_duration()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "edl.json").write_text(edl.model_dump_json())
    return EDLStore(tmp)


# ------------------------------------------------ 1. set_speed fits the music bed

@pytest.mark.parametrize("speed", [{"factor": 2}, {"preset": "flash_in"}])
def test_speeding_up_the_last_clip_ends_the_music_with_the_picture(tmp_path, media, speed):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=5, start=0),
                                 Clip(id="b", src=str(media["b"]), in_=0, out=5, start=5)])
    dispatch(st, "add_music", {"src": str(media["bed"]), "start": 0, "in": 0, "out": 10})
    dispatch(st, "set_speed", {"clip_id": "b", **speed})
    edl = st.edl
    video_end = edl.render_video_end()
    assert video_end < 9.0                                   # the picture got shorter
    music = [c for c in edl.get_track("music").clips if isinstance(c, Clip)]
    music_end = max(c.start + c.effective_duration for c in music)
    assert music_end == pytest.approx(video_end, abs=1.5 / FPS)
    assert edl.duration == pytest.approx(video_end, abs=1.5 / FPS)


def test_a_trim_and_a_speed_up_fit_the_music_the_same_way(tmp_path, media):
    """The trim path always fitted the bed (QA-018); the speed path matches it."""
    ends = []
    for i, (tool, args) in enumerate([("trim_clip", {"clip_id": "b", "in": 0, "out": 2.5}),
                                      ("set_speed", {"clip_id": "b", "factor": 2})]):
        st = _store(tmp_path / f"s{i}", [Clip(id="a", src=str(media["a"]), in_=0, out=5, start=0),
                                         Clip(id="b", src=str(media["b"]), in_=0, out=5, start=5)])
        dispatch(st, "add_music", {"src": str(media["bed"]), "start": 0, "in": 0, "out": 10})
        dispatch(st, tool, args)
        ends.append(st.edl.duration)
    assert ends[0] == pytest.approx(7.5, abs=1e-3)
    assert ends[1] == pytest.approx(ends[0], abs=1.5 / FPS)


# ------------------------------------------------ 2. auto_caption: every speech source

class _FakeTx:
    def __init__(self, segments: list[dict], language: str = "en"):
        self._d = {"language": language, "duration": 20.0, "segments": segments}
        self.language = language
        self.duration = 20.0

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self._d))


def _seg(words: list[tuple[str, float, float]]) -> dict:
    return {"start": words[0][1], "end": words[-1][2], "text": " ".join(w for w, _a, _b in words),
            "words": [{"word": w, "start": a, "end": b} for w, a, b in words]}


@pytest.fixture
def fake_tx(monkeypatch):
    """Per-file speech; no model runs."""
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
    """Per-test copies: auto_caption writes the upgraded transcript beside the
    primary source, which must not leak into the module-scoped fixture."""
    import shutil
    d = tmp_path / "m"
    d.mkdir()
    return {k: Path(shutil.copy(media[k], d / media[k].name)) for k in ("a", "b", "narr")}


def _cue_spans(st: EDLStore) -> list[tuple[float, float, str]]:
    cap = st.edl.get_track("captions")
    return [(c.start, c.end, c.text.replace("\n", " ")) for c in (cap.clips if cap else [])]


def test_speech_in_the_second_main_track_clip_is_captioned(tmp_path, cmedia, fake_tx):
    speech, calls = fake_tx
    speech["b.mp4"] = [_seg([("hello", 1.0, 1.4), ("world", 1.5, 2.0)])]
    st = _store(tmp_path / "s", [Clip(id="a", src=str(cmedia["a"]), in_=0, out=5, start=0),
                                 Clip(id="b", src=str(cmedia["b"]), in_=0, out=5, start=5)])
    out = dispatch(st, "auto_caption", {})
    assert "b.mp4" in calls
    cues = _cue_spans(st)
    assert out["cues"] > 0 and cues
    assert all(5.0 - 1e-6 <= a and b <= 10.0 + 1e-6 for a, b, _t in cues), cues
    assert "hello world" in " ".join(t for *_s, t in cues).lower()
    assert cues[0][0] == pytest.approx(6.0, abs=1e-3)


def test_a_voiceover_is_captioned_at_its_timeline_position(tmp_path, cmedia, fake_tx):
    speech, calls = fake_tx
    speech["narration.wav"] = [_seg([("hey", 0.5, 0.9), ("everyone", 1.0, 1.5)])]
    st = _store(tmp_path / "s", [Clip(id="a", src=str(cmedia["a"]), in_=0, out=10, start=0)])
    st.edl.get_track("vo").clips.append(Clip(id="n", src=str(cmedia["narr"]), in_=0, out=6, start=3))
    st.commit("seed", {}, "seed")
    dispatch(st, "auto_caption", {})
    assert "narration.wav" in calls
    cues = _cue_spans(st)
    assert cues and cues[0][0] == pytest.approx(3.5, abs=1e-3)
    assert "hey everyone" in " ".join(t for *_s, t in cues).lower()


def test_the_voiceover_wins_over_footage_sound_under_it(tmp_path, cmedia, fake_tx):
    """A narrated vlog: the clip's own ambient words under the narration are
    not stacked on top of it."""
    speech, _calls = fake_tx
    speech["a.mp4"] = [_seg([("um", 0.1, 0.6), ("it", 1.0, 1.2), ("tracks", 1.3, 1.8)])]
    speech["narration.wav"] = [_seg([("today", 0.0, 0.5), ("I", 0.6, 0.7), ("look", 0.8, 1.5)])]
    st = _store(tmp_path / "s", [Clip(id="a", src=str(cmedia["a"]), in_=0, out=10, start=0)])
    st.edl.get_track("vo").clips.append(Clip(id="n", src=str(cmedia["narr"]), in_=0, out=6, start=0))
    st.commit("seed", {}, "seed")
    dispatch(st, "auto_caption", {})
    text = " ".join(t for *_s, t in _cue_spans(st)).lower()
    assert "today" in text and "tracks" not in text
    spans = sorted((a, b) for a, b, _t in _cue_spans(st))
    assert all(b0 <= a1 + 1e-6 for (_a0, b0), (a1, _b1) in zip(spans, spans[1:])), spans


def test_a_voiceover_behind_a_transition_is_captioned_where_it_is_heard(tmp_path, cmedia, fake_tx):
    from video_ai_editor.render.clock import render_time
    speech, _calls = fake_tx
    speech["narration.wav"] = [_seg([("word", 0.0, 0.5)])]
    st = _store(tmp_path / "s", [Clip(id="a", src=str(cmedia["a"]), in_=0, out=5, start=0),
                                 Clip(id="b", src=str(cmedia["b"]), in_=0, out=5, start=5)])
    dispatch(st, "add_transition", {"at": 5.0, "type": "fade", "duration": 0.5})
    st.edl.get_track("vo").clips.append(Clip(id="n", src=str(cmedia["narr"]), in_=0, out=3, start=6.5))
    st.commit("seed", {}, "seed")
    dispatch(st, "auto_caption", {})
    (a, _b, _t), = _cue_spans(st)
    assert render_time(st.edl, a) == pytest.approx(6.0, abs=1e-3)   # the ruler's 06:00


def test_the_summary_names_the_captioned_sources(tmp_path, cmedia, fake_tx):
    speech, _calls = fake_tx
    speech["b.mp4"] = [_seg([("hello", 1.0, 1.4)])]
    st = _store(tmp_path / "s", [Clip(id="a", src=str(cmedia["a"]), in_=0, out=5, start=0),
                                 Clip(id="b", src=str(cmedia["b"]), in_=0, out=5, start=5)])
    out = dispatch(st, "auto_caption", {})
    assert "b.mp4" in out["summary"]


# ------------------------------------------------ 3. History names ruler times

def _dissolved(tmp: Path, media) -> EDLStore:
    st = _store(tmp, [Clip(id=f"c{i}", src=str(media["a"]), in_=0, out=5, start=5 * i)
                      for i in range(4)])
    for at in (5.0, 10.0):
        dispatch(st, "add_transition", {"at": at, "type": "dissolve", "duration": 0.5})
    return st


def test_add_clip_on_an_overlay_lane_names_the_rulers_time(tmp_path, media):
    st = _dissolved(tmp_path / "s", media)
    out = dispatch(st, "add_clip", {"track": "v2", "src": str(media["b"]),
                                    "in": 0, "out": 2, "start": 6.5})
    assert "6.00s" in out["summary"] and "6.50s" not in out["summary"], out["summary"]


def test_add_transition_names_the_cut_as_the_transitions_panel_does(tmp_path, media):
    from video_ai_editor.render.clock import render_time
    st = _dissolved(tmp_path / "s", media)
    out = dispatch(st, "add_transition", {"at": 15.0, "type": "glitch"})
    want = f"{render_time(st.edl, 15.0):.2f}s"
    assert want != "15.00s"
    assert f"at {want}" in out["summary"], out["summary"]


# ------------------------------------------------ 4. add_transition needs a cut

def test_add_transition_off_a_cut_is_refused_and_a_later_split_changes_nothing(tmp_path, media):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=20, start=0)])
    dispatch(st, "split_at", {"track": "v1", "time": 10.0})
    dispatch(st, "add_transition", {"at": 10.0, "type": "fade", "duration": 1.0})
    before = [(t.at, t.duration) for t in st.edl.get_track("v1").transitions]
    dur = st.edl.duration
    with pytest.raises(ValueError, match=r"no cut at 5\.00s.*10\.00s"):
        dispatch(st, "add_transition", {"at": 5.0, "type": "fade", "duration": 0.5})
    assert [(t.at, t.duration) for t in st.edl.get_track("v1").transitions] == before
    dispatch(st, "split_at", {"track": "v1", "time": 5.0})
    assert st.edl.duration == pytest.approx(dur, abs=1e-6)


def test_add_transition_on_a_track_with_no_cut_is_refused(tmp_path, media):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=8, start=0)])
    with pytest.raises(ValueError, match="no cut"):
        dispatch(st, "add_transition", {"at": 4.0, "type": "fade"})
    assert st.edl.get_track("v1").transitions == []


# ------------------------------------------------ 5. remove_silences: no seam slivers

def test_dead_air_across_seams_leaves_no_slivers(tmp_path, media):
    src = str(media["gaps"])
    st = _store(tmp_path / "s", [Clip(id="a", src=src, in_=0, out=4, start=0),
                                 Clip(id="b", src=src, in_=4, out=8, start=4),
                                 Clip(id="c", src=src, in_=8, out=12, start=8)])
    dispatch(st, "remove_silences", {"threshold_db": -30, "min_dur": 0.5, "keep_pad": 0.1})
    clips = sorted((c for c in st.edl.get_track("v1").clips if isinstance(c, Clip)),
                   key=lambda c: c.start)
    spans = [(round(c.in_, 2), round(c.out, 2)) for c in clips]
    assert all(c.effective_duration >= 0.5 for c in clips), spans
    # the air next to the speech is kept
    assert spans[0][1] == pytest.approx(3.1, abs=0.06), spans


# ------------------------------------------------ 6. add_effect params are checked

@pytest.mark.parametrize("etype,params", [
    ("grain", {"strength": "abc"}), ("grain", {"strength": 1e9}),
    ("rgb_split", {"offset": 100000}), ("blur", {"radius": "inf"}),
    ("glow", {"strength": float("nan")}), ("sharpen", {"amount": [1]}),
    ("blur", {"radius": 8, "bogus": 1}),
])
def test_add_effect_refuses_params_that_cannot_render(tmp_path, media, etype, params):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=4, start=0)])
    with pytest.raises(ValueError):
        dispatch(st, "add_effect", {"clip_id": "a", "type": etype, "params": params})
    assert st.edl.get_clip("a")[1].effects == []


@pytest.mark.parametrize("etype,params", [
    ("blur", {"radius": 8}), ("sharpen", {"amount": 1.0}), ("vignette", {}),
    ("grain", {"strength": 20}), ("vintage", {}), ("vhs", {}), ("glow", {"strength": 0.4}),
    ("rgb_split", {"offset": 6}), ("hflip", {}), ("vflip", {}),
    ("color", {"brightness": 0.1, "contrast": 1.2, "saturation": 1.1, "temp": 0.5, "tint": -0.2}),
])
def test_the_effects_panel_presets_are_still_accepted(tmp_path, media, etype, params):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=4, start=0)])
    dispatch(st, "add_effect", {"clip_id": "a", "type": etype, "params": params})
    assert [e.type for e in st.edl.get_clip("a")[1].effects] == [etype]


def test_add_effect_lut_goes_through_the_read_path_allowlist(tmp_path, media):
    from video_ai_editor import config
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=4, start=0)])
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(True)
    try:
        with pytest.raises(ValueError):
            dispatch(st, "add_effect", {"clip_id": "a", "type": "lut",
                                        "params": {"src": "/etc/hosts"}})
    finally:
        config.enable_path_restriction(before)
    assert st.edl.get_clip("a")[1].effects == []


def test_add_effect_lut_with_a_non_cube_file_is_refused(tmp_path, media):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=4, start=0)])
    bad = tmp_path / "bad.cube"
    bad.write_text("not a lut\n")
    with pytest.raises(ValueError):
        dispatch(st, "add_effect", {"clip_id": "a", "type": "lut", "params": {"src": str(bad)}})


@pytest.mark.parametrize("etype,params", [
    ("grain", {"strength": "abc"}), ("grain", {"strength": 1e9}),
    ("rgb_split", {"offset": 100000}), ("blur", {"radius": "inf"}),
    ("glow", {"strength": "nan"}), ("vignette", {"angle": "x"}),
    ("color", {"contrast": "inf", "brightness": 50, "temp": 1e9}),
    ("lut", {"src": 5, "intensity": "abc"}),
])
def test_an_old_edl_holding_a_bad_param_still_renders(tmp_path, etype, params):
    from video_ai_editor.render.effects import effect_chain
    chain = effect_chain([Effect(type=etype, params=params)], "c1")
    if not chain:
        return
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc2=s=320x180:d=0.1:r={FPS}", "-filter_complex", chain,
                    "-frames:v", "1", "-f", "null", "-"], check=True, capture_output=True)


# ------------------------------------------------ 7. chroma_key colour never reaches the graph raw

INJECT = "0x00FF00:0.001:0,drawbox=x=0:y=0:w=iw:h=ih:c=red:t=fill,chromakey=0x00FF00"


@pytest.mark.parametrize("color", [INJECT, "green,drawbox", "#00FF00;x", "chartreuse", "#00FF0", ""])
def test_chroma_key_refuses_a_colour_that_is_not_a_colour(tmp_path, media, color):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=4, start=0)])
    with pytest.raises(ValueError):
        dispatch(st, "chroma_key", {"clip_id": "a", "color": color})
    assert st.edl.get_clip("a")[1].chromakey is None


@pytest.mark.parametrize("color,want", [("#00ff00", "#00FF00"), ("0000FF", "#0000FF"),
                                        ("green", "#008000"), ("Blue", "#0000FF"),
                                        ("0x00ff00", "#00FF00")])
def test_chroma_key_accepts_hex_and_screen_colour_names(tmp_path, media, color, want):
    st = _store(tmp_path / "s", [Clip(id="a", src=str(media["a"]), in_=0, out=4, start=0)])
    dispatch(st, "chroma_key", {"clip_id": "a", "color": color})
    assert st.edl.get_clip("a")[1].chromakey.color == want


def test_a_saved_edl_holding_an_injected_key_colour_loads_with_the_default(tmp_path):
    from video_ai_editor.render.effects import build_chromakey_filter
    ck = ChromaKey(color=INJECT)
    assert ck.color == "#00FF00"
    edl = EDL(canvas=Canvas(w=W, h=H, fps=FPS),
              tracks=[Track(id="v1", type="video",
                            clips=[Clip(id="a", src="/x.mp4", in_=0, out=1, start=0)])])
    raw = json.loads(edl.model_dump_json(by_alias=True))
    raw["tracks"][0]["clips"][0]["chromakey"] = {"color": INJECT}
    loaded = EDL.model_validate_json(json.dumps(raw))
    assert loaded.tracks[0].clips[0].chromakey.color == "#00FF00"
    # the builder never hands caller text to the graph, even if a model is
    # built around the validator
    bypass = ChromaKey.model_construct(color=INJECT, similarity=0.4, smoothness=0.1,
                                       spill_suppress=0.5)
    f = build_chromakey_filter(bypass)
    assert "drawbox" not in f and f.count("chromakey=") == 1


def test_key_colour_names_render_as_ffmpeg_drew_them_before(tmp_path):
    """Names map to ffmpeg's own values, so a key saved as 'green' keys the
    same colour it always did (no render-behaviour change)."""
    from video_ai_editor.render.effects import build_chromakey_filter
    assert "chromakey=0x008000:" in build_chromakey_filter(ChromaKey(color="green"))
    assert "chromakey=0x00FF00:" in build_chromakey_filter(ChromaKey(color="#00ff00"))


# ------------------------------------------------ 8/9. no project from a GET or an upload to a bad sid

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


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_get_history_and_files_for_an_unknown_sid_create_nothing(client):
    main, c, wd = client
    before = _tree(wd.parent)
    assert c.get("/api/sessions/s_nope00001/history").status_code == 404
    assert c.get("/api/sessions/s_nope00002/files/uploads/x").status_code == 404
    for bad in ("%2E", "%2E%2E"):
        r = c.get(f"/api/sessions/{bad}/history")
        assert r.status_code in (400, 404), (bad, r.status_code)
    assert _tree(wd.parent) == before
    assert c.get("/api/sessions").json() == c.get("/api/sessions").json()
    assert not any(p.name.startswith("s_nope") for p in wd.iterdir())


def test_get_history_of_a_real_session_still_answers(client):
    main, c, wd = client
    sid = c.post("/api/sessions", json={"name": "h"}).json()["id"]
    r = c.get(f"/api/sessions/{sid}/history")
    assert r.status_code == 200 and r.json() == {"history": []}


@pytest.mark.parametrize("route,field,name,body", [
    ("sticker_upload", "file", "s.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64),
    ("vo_record", "file", "v.wav", b"RIFF0000WAVEfmt " + b"0" * 64),
])
def test_uploads_to_an_unknown_or_path_shaped_sid_write_nothing(client, route, field, name, body):
    main, c, wd = client
    before = _tree(wd.parent)
    r = c.post(f"/api/sessions/s_nope00001/{route}", files={field: (name, body)})
    assert r.status_code == 404, r.text
    r = c.post(f"/api/sessions/%2E%2E/{route}", files={field: (name, body)})
    assert r.status_code in (400, 404), r.text
    assert _tree(wd.parent) == before
