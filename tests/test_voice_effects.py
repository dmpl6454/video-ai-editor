"""Voice effects (wave E, F3): the ONE table and everything that reads it.

* the table: every stage kind has a render, a reverb is the last stage, the
  aliases are unambiguous, the served payload is `payload()`, and the
  browser's `voiceFxTable.ts` and the DSP golden are what the generator
  writes today (tests/gen_voice_fx_goldens.py);
* `set_voice_effect`: one clip, several, a whole lane (v1, overlay, VO,
  music); off again; intensity kept for the same effect and reset for a new
  one; ONE undo step; crisp refusals (unknown effect, a text clip, a freeze,
  intensity out of range, a locked lane);
* the agent tool schema names the same presets;
* the Inspector's audition (`POST /api/sessions/{sid}/voice/preview`): a WAV
  of the clip's window through the effect, rendered by the export's chain
  (a pitch preset measured), nothing committed.
"""
from __future__ import annotations

import io
import subprocess
import sys
import warnings
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_voice_fx_goldens as gen  # noqa: E402
import golden_env  # noqa: E402

from video_ai_editor import platformutil as _pu  # noqa: E402
from video_ai_editor.agent.dispatch import dispatch  # noqa: E402
from video_ai_editor.edl import EDLStore  # noqa: E402
from video_ai_editor.edl import voice_effects as V  # noqa: E402
from video_ai_editor.edl.schema import Canvas, Clip, TextClip, empty_edl  # noqa: E402
from video_ai_editor.render import audio_mix  # noqa: E402

SR = 48000


# ------------------------------------------------------------------ the table

def test_the_table_is_consistent():
    assert len(V.PRESETS) == 11 and len(set(V.PRESET_IDS)) == 11
    for p in V.PRESETS:
        assert p.label and p.hint and p.icon.startswith("voice") and p.stages
        kinds = [s.kind for s in p.stages]
        assert set(kinds) <= set(V.STAGE_KINDS)
        if "reverb" in kinds:                       # the preview convolves it LAST
            assert kinds[-1] == "reverb" and kinds.count("reverb") == 1
        for s in p.stages:
            assert set(s.scale) <= set(s.params), (p.id, s)
        # every stage renders (a new kind must land in audio_mix too)
        assert audio_mix.voice_filters(type("A", (), {"voice_effect": p.id, "voice_intensity": 1.0})(), "t")
    # every alias names exactly one preset
    seen: dict[str, str] = {}
    for p in V.PRESETS:
        for a in (p.id, p.label, *p.aliases):
            k = " ".join(a.lower().replace("-", " ").split())
            assert seen.setdefault(k, p.id) == p.id, (a, seen[k], p.id)
    assert V.preset_id("Hall") == V.preset_id("reverb") == "reverb"
    assert V.preset_id("WALKIE-TALKIE") == "radio" and V.preset_id("banana") is None


def test_the_browser_table_is_current():
    """frontend/src/lib/voice/voiceFxTable.ts IS payload() (pure Python: the
    same on every machine)."""
    assert gen.TABLE_TS.read_text(encoding="utf-8") == gen.table_ts(), "regenerate: tests/gen_voice_fx_goldens.py"


#: What the browser's DSP is held to against this same golden
#: (frontend/src/lib/voice/voiceFx.test.ts: every window sample within 2e-5
#: of full scale). Off the recording machine ffmpeg's own samples are held to
#: it too: measured on both x86 runners of CI run 36599751632 (ffmpeg 6.1 and
#: 9.0.2), at most 1.13e-6 (Telephone, left channel).
SAMPLE_PARITY = 2e-5
#: The pitch presets (Chipmunk, Deep, Monster) are compared as per-block
#: spectral centroid and level, not samples. Both x86 runners of the same run
#: measured at most 2e-5 Hz and 1e-6 dB from the golden (tests/golden_env.py);
#: the bounds are fifty and a hundred times that, and still a thousandth of a
#: hertz and a ten-thousandth of a decibel: nothing a listener or the
#: browser's parity test could tell from the golden.
CENTROID_PARITY_HZ = 1e-3
RMS_PARITY_DB = 1e-4
SCOPED = "float samples and spectra differ in the 8th significant digit"
PORTABLE = ("the filter text, lengths and window layout are equal, every sample is finite and within 2e-5, and "
            "every block centroid within 1e-3 Hz and level within 1e-4 dB "
            "(test_the_dsp_golden_holds_on_every_machine)")


@pytest.fixture(scope="module")
def dsp() -> tuple[dict, dict]:
    """(the golden, what ffmpeg does with today's filter text here)."""
    import json
    return json.loads(gen.GOLDEN.read_text(encoding="utf-8")), json.loads(json.dumps(gen.generate()))


def _windows_hold(got: list[dict], want: list[dict], what: str) -> float:
    """The largest sample difference; fails on a layout change, a sample
    that is not a number, or one further than SAMPLE_PARITY."""
    assert [(w["start"], len(w["L"]), len(w["R"])) for w in got] == \
        [(w["start"], len(w["L"]), len(w["R"])) for w in want], what
    worst = 0.0
    for g, w in zip(got, want, strict=True):
        for ch in ("L", "R"):
            a, b = np.array(g[ch], dtype=np.float64), np.array(w[ch], dtype=np.float64)
            assert np.isfinite(a).all(), (what, w["start"], ch, "not a number at sample",
                                          w["start"] + int(np.argmin(np.isfinite(a))))
            d = np.abs(a - b)
            assert d.max() <= SAMPLE_PARITY, (what, w["start"], ch, "sample", w["start"] + int(d.argmax()),
                                              float(a[d.argmax()]), float(b[d.argmax()]))
            worst = max(worst, float(d.max()))
    return worst


def _blocks_hold(got: dict, want: dict, what: str) -> dict[str, float]:
    """The largest centroid (Hz) and level (dB) difference of a pitch case;
    fails on a changed shape, a value that is not a number, or one further
    than CENTROID_PARITY_HZ / RMS_PARITY_DB."""
    worst = {}
    for key, unit, bound in (("centroid", "centroid_hz", CENTROID_PARITY_HZ), ("rms_db", "rms_db", RMS_PARITY_DB)):
        a, b = np.array(got[key], dtype=np.float64), np.array(want[key], dtype=np.float64)
        assert a.shape == b.shape and np.isfinite(a).all(), (what, key)
        d = np.abs(a - b)
        assert d.max() <= bound, (what, key, "block", int(d.argmax()), float(a[d.argmax()]), float(b[d.argmax()]))
        worst[unit] = float(d.max())
    return worst


def _dsp_deviation(doc: dict, fresh: dict) -> dict[str, float]:
    """Portable checks of every case and the reverb's impulse response;
    returns the largest differences (samples, Hz, dB)."""
    skeleton = lambda c: (c["effect"], c["intensity"], c["filter"], c["length"], sorted(c))   # noqa: E731
    assert [skeleton(c) for c in fresh["cases"]] == [skeleton(c) for c in doc["cases"]]
    worst = {"sample": 0.0, "centroid_hz": 0.0, "rms_db": 0.0}
    for got, want in zip(fresh["cases"], doc["cases"], strict=True):
        what = f"{want['effect']} at {want['intensity']}"
        if "windows" in want:
            worst["sample"] = max(worst["sample"], _windows_hold(got["windows"], want["windows"], what))
            continue
        for unit, d in _blocks_hold(got["blocks"], want["blocks"], what).items():
            worst[unit] = max(worst[unit], d)
    ir, want_ir = fresh["reverb_ir"], doc["reverb_ir"]
    assert (ir["params"], ir["samples"]) == (want_ir["params"], want_ir["samples"])
    worst["sample"] = max(worst["sample"], _windows_hold(ir["windows"], want_ir["windows"], "reverb_ir"))
    return worst


def test_the_dsp_golden_holds_on_every_machine(dsp):
    """The golden still describes today's filter text on THIS machine: the
    same presets, filter text, output lengths and window layout, every
    sample a number and within the browser's own parity bound."""
    doc, fresh = dsp
    _dsp_deviation(doc, fresh)


def scope_to_the_recording_machine(doc: dict, fresh: dict) -> None:
    """Skip (naming both machines and what THIS one measured) anywhere but
    on the CPU the golden was recorded on: arm64 + ffmpeg 9.0.1 reproduced
    every float of the ffmpeg 8.1.1 golden, both x86 runners did not."""
    why = golden_env.foreign_reason(golden_env.voice_fx(doc), SCOPED, portable=PORTABLE)
    if why is None:
        return
    w = _dsp_deviation(doc, fresh)
    why = (f"{why}; measured here: at most {w['sample']:.3g} of full scale, {w['centroid_hz']:.3g} Hz "
           f"and {w['rms_db']:.3g} dB from the golden")
    warnings.warn(why, stacklevel=2)
    pytest.skip(why)


def test_the_browser_table_and_the_dsp_golden_are_current(dsp):
    """frontend/src/lib/voice/voiceFxTable.ts IS payload(), and the golden is
    what ffmpeg does with today's filter text (regenerate with
    tests/gen_voice_fx_goldens.py): float for float, on the CPU it was
    recorded on (tests/golden_env.py)."""
    assert gen.TABLE_TS.read_text(encoding="utf-8") == gen.table_ts(), "regenerate: tests/gen_voice_fx_goldens.py"
    doc, fresh = dsp
    scope_to_the_recording_machine(doc, fresh)
    assert doc["cases"] == fresh["cases"] and doc["reverb_ir"] == fresh["reverb_ir"], "regenerate the golden"


@pytest.mark.parametrize("major,machine,foreign", [
    (8, "arm64", False),              # the recording machine
    (9, "arm64", False),              # CI macOS: ffmpeg 9.0.1 reproduced every float
    (9, "x86_64", True),              # CI Windows (9.0.2)
    (8, "x86_64", True),              # CI ubuntu, pinned to ffmpeg 8
])
def test_the_exact_floats_are_held_only_on_the_recording_cpu(major, machine, foreign, monkeypatch):
    """Both directions on any machine, with the golden as its own output."""
    import json
    monkeypatch.setattr(golden_env, "probe_ffmpeg_major", lambda: major)
    monkeypatch.setattr(golden_env, "probe_machine", lambda: machine)
    doc = json.loads(gen.GOLDEN.read_text(encoding="utf-8"))
    if not foreign:
        scope_to_the_recording_machine(doc, doc)             # nothing skips
        return
    with pytest.warns(UserWarning), pytest.raises(pytest.skip.Exception) as skipped:
        scope_to_the_recording_machine(doc, doc)
    why = str(skipped.value)
    assert f"voice_fx_cases.json recorded on ffmpeg 8 arm64; this is ffmpeg {major} {machine}: " in why
    assert "8th significant digit" in why and "at most 0 of full scale" in why


def test_the_portable_lines_fail_what_is_not_rounding():
    """What Windows (ffmpeg 9.0.2) produced for Vibrato at 0.5 in CI run
    36599751632 — `nan, nan, …, 0.85199648` where the golden is silence — is
    not rounding, and neither is a changed filter text or length."""
    import copy
    import json
    doc = json.loads(gen.GOLDEN.read_text(encoding="utf-8"))
    i = next(i for i, c in enumerate(doc["cases"]) if c["effect"] == "vibrato" and c["intensity"] == 0.5)
    for sample, value in ((0, float("nan")), (39, 0.85199648), (5, 1.0830695e-06 + 3e-5)):
        bad = copy.deepcopy(doc)
        bad["cases"][i]["windows"][0]["L"][sample] = value
        with pytest.raises(AssertionError, match="vibrato at 0.5"):
            _dsp_deviation(doc, bad)
    for key, value in (("length", 48001), ("filter", ",vibrato=f=6:d=0.3")):
        bad = copy.deepcopy(doc)
        bad["cases"][i][key] = value
        with pytest.raises(AssertionError):
            _dsp_deviation(doc, bad)


def test_the_portable_lines_hold_the_pitch_presets_too():
    """A pitch case (compared as block centroid and level) a hertz or a
    hundredth of a decibel off is not rounding either: the x86 runners
    measured 2e-5 Hz and 1e-6 dB. Every pitch case, both quantities."""
    import copy
    import json
    doc = json.loads(gen.GOLDEN.read_text(encoding="utf-8"))
    pitched = [i for i, c in enumerate(doc["cases"]) if "blocks" in c]
    assert sorted({doc["cases"][i]["effect"] for i in pitched}) == ["chipmunk", "deep", "monster"]
    for i in pitched:
        what = f"{doc['cases'][i]['effect']} at {doc['cases'][i]['intensity']}"
        for key, off in (("centroid", 1.0), ("rms_db", 0.01), ("centroid", float("nan"))):
            bad = copy.deepcopy(doc)
            bad["cases"][i]["blocks"][key][-1] += off
            with pytest.raises(AssertionError, match=what):
                _dsp_deviation(doc, bad)
        near = copy.deepcopy(doc)                    # what the runners measured still holds
        near["cases"][i]["blocks"]["centroid"][-1] += 2e-5
        near["cases"][i]["blocks"]["rms_db"][-1] += 1e-6
        _dsp_deviation(doc, near)


def test_presets_are_served(tmp_path, monkeypatch):
    from video_ai_editor import main
    r = TestClient(main.app).get("/api/voice/presets")
    assert r.status_code == 200
    assert r.json() == V.payload()
    assert [p["id"] for p in r.json()["presets"]] == list(V.PRESET_IDS)


def test_the_agent_tool_names_the_same_presets():
    from video_ai_editor.agent import tools
    sch = next(t for t in tools.ALL_TOOLS if t["name"] == "set_voice_effect")["input_schema"]
    assert sch["properties"]["effect"]["enum"] == [*V.PRESET_IDS, "none"]
    assert sch["properties"]["intensity"]["minimum"] == 0.0 and sch["properties"]["intensity"]["maximum"] == 1.0


# ------------------------------------------------------------------ the op

def _src(path: Path, seconds=4.0) -> Path:
    voice = "+".join(f"{0.25 / k:.4f}*sin(2*PI*{140 * k}*t)" for k in range(1, 16))
    subprocess.run([_pu.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s=64x36:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"aevalsrc=exprs='{voice}|{voice}':s={SR}:d={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_f32le", "-shortest", str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> Path:
    return _src(tmp_path_factory.mktemp("vfx-op") / "voice.mov")


def _store(tmp_path: Path, media: Path) -> EDLStore:
    sd = tmp_path / "s"
    sd.mkdir()
    e = empty_edl(Canvas(w=64, h=36, fps=30))
    e.get_track("v1").clips += [Clip(src=str(media), out=2.0, start=0.0, id="a"),
                                Clip(src=str(media), out=2.0, start=2.0, id="b")]
    e.get_track("v2").clips.append(Clip(src=str(media), out=1.0, start=0.5, id="p"))
    e.get_track("vo").clips += [Clip(src=str(media), out=1.0, start=0.0, id="v1c"),
                                Clip(src=str(media), out=1.0, start=2.0, id="v2c")]
    e.get_track("music").clips.append(Clip(src=str(media), out=3.0, start=0.0, id="m"))
    e.get_track("v1").clips.append(Clip(src=str(media), **{"in": 1.0}, out=1.0 + 1 / 30, start=4.0,
                                        freeze=1.0, id="fz"))
    e.recompute_duration()
    (sd / "edl.json").write_text(e.model_dump_json(by_alias=True))
    return EDLStore(sd)


def _fx(st: EDLStore, cid: str) -> tuple[str | None, float]:
    a = st.edl.get_clip(cid)[1].audio
    return a.voice_effect, a.voice_intensity


def test_set_voice_effect_on_every_kind_of_lane(tmp_path, media):
    st = _store(tmp_path, media)
    for cid in ("a", "p", "v1c", "m"):
        r = dispatch(st, "set_voice_effect", {"clip_id": cid, "effect": "Robot"})
        assert r["effect"] == "robot" and _fx(st, cid) == ("robot", 1.0)
    r = dispatch(st, "set_voice_effect", {"track": "vo", "effect": "echo", "intensity": 0.6})
    assert r["clip_ids"] == ["v1c", "v2c"]
    assert [_fx(st, c) for c in ("v1c", "v2c")] == [("echo", 0.6), ("echo", 0.6)]
    # the same effect again keeps its intensity; a new one resets it
    dispatch(st, "set_voice_effect", {"clip_id": "v1c", "effect": "echo"})
    assert _fx(st, "v1c") == ("echo", 0.6)
    dispatch(st, "set_voice_effect", {"clip_id": "v1c", "effect": "hall"})
    assert _fx(st, "v1c") == ("reverb", 1.0)
    dispatch(st, "set_voice_effect", {"clip_ids": ["a", "b"], "effect": "none"})
    assert [_fx(st, c) for c in ("a", "b")] == [(None, 1.0), (None, 1.0)]
    # nothing written for a clip without an effect (the JSON is unchanged)
    assert "voice_" not in st.edl.get_clip("b")[1].model_dump_json(by_alias=True)


def test_one_undo_step_for_a_whole_lane(tmp_path, media):
    st = _store(tmp_path, media)
    dispatch(st, "set_voice_effect", {"clip_id": "a", "effect": "deep"})
    before = st.edl.hash()
    dispatch(st, "set_voice_effect", {"track": "vo", "effect": "chipmunk"})
    assert st.edl.hash() != before
    assert [_fx(st, c)[0] for c in ("v1c", "v2c")] == ["chipmunk", "chipmunk"]
    dispatch(st, "undo", {})
    assert st.edl.hash() == before
    assert [_fx(st, c)[0] for c in ("a", "v1c", "v2c")] == ["deep", None, None]


@pytest.mark.parametrize("args,msg", [
    ({"clip_id": "a", "effect": "banana"}, "unknown voice effect"),
    ({"clip_id": "a", "effect": "robot", "intensity": 1.5}, "intensity"),
    ({"clip_id": "a", "effect": "robot", "intensity": float("nan")}, "intensity"),
    ({"clip_id": "fz", "effect": "robot"}, "freeze frame"),
    ({"clip_id": "nope", "effect": "robot"}, "not found"),
    ({"effect": "robot"}, "needs a clip_id"),
])
def test_refusals_are_crisp_and_change_nothing(tmp_path, media, args, msg):
    st = _store(tmp_path, media)
    before = st.edl.hash()
    with pytest.raises(ValueError, match=msg):
        dispatch(st, "set_voice_effect", args)
    assert st.edl.hash() == before


def test_a_text_clip_and_a_locked_lane_are_refused(tmp_path, media):
    st = _store(tmp_path, media)
    dispatch(st, "add_text", {"text": "Hi", "start": 0.0, "end": 1.0})
    tid = next(c.id for t in st.edl.tracks for c in t.clips if isinstance(c, TextClip))
    with pytest.raises(ValueError, match="no sound"):
        dispatch(st, "set_voice_effect", {"clip_id": tid, "effect": "robot"})
    dispatch(st, "set_track_locked", {"track": "vo", "locked": True})
    before = st.edl.hash()
    with pytest.raises(ValueError):
        dispatch(st, "set_voice_effect", {"clip_id": "v1c", "effect": "robot"})
    assert st.edl.hash() == before


# ------------------------------------------------------------------ the audition

def test_the_audition_is_the_exports_chain_and_commits_nothing(tmp_path, media, monkeypatch):
    from video_ai_editor import main
    st = _store(tmp_path, media)
    monkeypatch.setattr(main, "_store", lambda sid: st)
    from video_ai_editor.api import voice_routes
    monkeypatch.setattr(voice_routes, "_resolve_store", lambda sid: st)
    before = st.edl.hash()
    c = TestClient(main.app)
    r = c.post("/api/sessions/x/voice/preview", json={"clip_id": "m", "effect": "chipmunk", "at": 0.5,
                                                      "seconds": 2.0})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert (w.getframerate(), w.getnchannels()) == (SR, 2)
        n = w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, 2)[:, 0].astype(np.float64) / 32768
    assert n == 2 * SR
    seg = x[12000:84000] - x[12000:84000].mean()
    f = np.fft.rfft(seg, 2 * len(seg))
    ac = np.fft.irfft(f * np.conj(f))[:len(seg)]
    lo, hi = SR // 900, SR // 60
    k = lo + int(np.argmax(ac[lo:hi]))
    assert abs(1200 * np.log2((SR / k) / (140 * 2 ** (9 / 12)))) < 20      # 235 Hz, not 140
    assert st.edl.hash() == before
    # a bad request is a 400, not a render
    assert c.post("/api/sessions/x/voice/preview", json={"clip_id": "m", "effect": "banana"}).status_code == 400
    assert c.post("/api/sessions/x/voice/preview", json={"clip_id": "fz", "effect": "robot"}).status_code == 400
    assert c.post("/api/sessions/x/voice/preview", json={"clip_id": "m", "effect": "robot",
                                                         "extra": 1}).status_code == 422


# ------------------------------------------------------------------ the Prompt Editor

def test_the_plan_validator_holds_set_voice_effect_to_the_table():
    from video_ai_editor.agent.prompt import schema as Sc
    from video_ai_editor.agent.prompt.facts import TimelineFacts
    from video_ai_editor.agent.prompt.validate import PLAN_TOOLS, PlanRejected, validate_plan
    f = TimelineFacts.minimal(v1_clip_ids=["c_v1"], clip_ids=["c_v1"], selection="c_v1")

    def plan(**args):
        return Sc.Plan.new(intent="test", brain="claude",
                           steps=[Sc.Step(tool="set_voice_effect", args=args, why="test")])
    assert "set_voice_effect" in PLAN_TOOLS and Sc.TOOL_STAGE["set_voice_effect"] == Sc.STAGE_AUDIO
    ok = validate_plan(plan(clip_id="c_v1", effect="robot", intensity=0.5), f)
    assert [p.check for p in ok.postconditions] == ["voice_effect_is"]
    validate_plan(plan(clip_id="c_v1", effect="none"), f)
    for bad, needle in (({"clip_id": "c_v1", "effect": "banana"}, "not one of"),
                        ({"clip_id": "c_v1", "effect": "robot", "intensity": 3}, "intensity")):
        with pytest.raises(PlanRejected) as ei:
            validate_plan(plan(**bad), f)
        assert needle in "; ".join(ei.value.reasons), ei.value.reasons


def test_the_voice_check_measures_the_edl(tmp_path, media):
    from types import SimpleNamespace
    from video_ai_editor.agent.prompt import verify as Vf
    from video_ai_editor.agent.prompt.schema import Postcondition
    st = _store(tmp_path, media)
    ctx = SimpleNamespace(edl=st.edl)

    def run(**args):
        return Vf.c_voice_effect_is(ctx, Postcondition(check="voice_effect_is", human="voice", args=args)).passed
    assert run(clip_id="a", effect=None) is True and run(clip_id="a", effect="robot") is False
    dispatch(st, "set_voice_effect", {"track": "vo", "effect": "echo", "intensity": 0.5})
    ctx.edl = st.edl
    assert run(track="vo", effect="echo") is True
    assert run(clip_id=["v1c", "v2c"], effect="echo", intensity=0.5) is True
    assert run(clip_id=["v1c", "v2c"], effect="echo", intensity=1.0) is False
    assert run(clip_id=["v1c", "a"], effect="echo") is False
