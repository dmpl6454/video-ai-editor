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
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_voice_fx_goldens as gen  # noqa: E402

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


def test_the_browser_table_and_the_dsp_golden_are_current():
    """frontend/src/lib/voice/voiceFxTable.ts IS payload(), and the golden is
    what ffmpeg does with today's filter text (regenerate with
    tests/gen_voice_fx_goldens.py)."""
    assert gen.TABLE_TS.read_text(encoding="utf-8") == gen.table_ts(), "regenerate: tests/gen_voice_fx_goldens.py"
    import json
    doc = json.loads(gen.GOLDEN.read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(gen.generate()))
    assert doc["cases"] == fresh["cases"] and doc["reverb_ir"] == fresh["reverb_ir"], "regenerate the golden"


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
