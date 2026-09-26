"""The speed presets are ONE table (wave D, lane S2): `edl/speed_presets.py`
over `speed_curve.CURVE_PRESETS`, read by the agent tool schema, the Prompt
Editor's validator and verifier, and the Inspector through
`GET /api/speed/presets`. These tests pin that every reader agrees with it,
and that curve and freeze plans validate (and bad ones do not)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from video_ai_editor.agent import tools
from video_ai_editor.agent.prompt import schema as Sc
from video_ai_editor.agent.prompt import verify as V
from video_ai_editor.agent.prompt.facts import TimelineFacts
from video_ai_editor.agent.prompt.validate import PlanRejected, validate_plan
from video_ai_editor.edl import speed_curve as SC
from video_ai_editor.edl import speed_presets as SP


# ------------------------------------------------------------------ the table

def test_menu_is_capcuts_six_in_order_and_every_shape_is_labelled():
    assert [p.label for p in SP.PRESETS if p.menu] == [
        "Montage", "Hero", "Bullet", "Jump Cut", "Flash In", "Flash Out"]
    assert set(SP.PRESET_IDS) == set(SC.CURVE_PRESETS)
    for p in SP.PRESETS:
        assert p.points == SC.CURVE_PRESETS[p.id]
        assert SP.curve_name(p.points) == p.id
        # Every preset is itself a valid request (the UI sends its points).
        assert SP.validate_curve_points(p.points) == p.points


def test_resolve_speed_shapes():
    assert SP.resolve_speed(factor="2.5") == 2.5
    assert SP.resolve_speed(preset="Flash out") == {"curve": SC.CURVE_PRESETS["flash_out"], "name": "flash_out"}
    got = SP.resolve_speed(curve=[[1, 2], [0, 1]])        # order does not matter
    assert got == {"curve": [[0.0, 1.0], [1.0, 2.0]], "name": "custom"}
    for bad in ({}, {"factor": 0}, {"factor": 101}, {"factor": True}, {"curve": "hero"},
                {"curve": [[0, 1], [1, 0.05]]}, {"factor": 1, "curve": [[0, 1], [1, 1]]}):
        with pytest.raises(ValueError):
            SP.resolve_speed(**bad)


def test_describe():
    assert SP.describe(2) == "2.00x" and SP.describe(None) == "1.00x"
    assert SP.describe(SP.preset_speed("bullet")) == "Bullet curve"
    assert SP.describe({"curve": [[0, 1], [1, 2]], "name": "custom"}) == "custom curve"


def test_route_serves_the_table():
    from video_ai_editor.main import app
    r = TestClient(app).get("/api/speed/presets")
    assert r.status_code == 200
    body = r.json()
    assert body == SP.presets_payload()
    assert [p["id"] for p in body["presets"]] == list(SP.PRESET_IDS)
    assert body["freeze_default"] == 3.0 and body["curve_range"] == [0.1, 10.0]


# ------------------------------------------------------------------ the agent tool

def test_tool_schema_reads_the_table_and_drops_constant_only():
    sch = next(t for t in tools.ALL_TOOLS if t["name"] == "set_speed")
    props = sch["input_schema"]["properties"]
    assert props["preset"]["enum"] == list(SP.PRESET_IDS)
    assert set(props) >= {"clip_id", "factor", "preset", "curve", "keep_pitch"}
    assert sch["input_schema"]["required"] == ["clip_id"]
    assert "Constant speed only" not in sch["description"]
    for p in SP.PRESETS:
        assert p.id in sch["description"]
    fz = next(t for t in tools.ALL_TOOLS if t["name"] == "freeze_frame")
    assert fz["input_schema"]["properties"]["duration"]["maximum"] == SP.FREEZE_RANGE[1]
    assert "default 3" in fz["description"]


# ------------------------------------------------------------------ plans

def _step(tool: str, **args) -> Sc.Step:
    return Sc.Step(tool=tool, args=args, why=f"test {tool}")


def _plan(*steps: Sc.Step) -> Sc.Plan:
    return Sc.Plan.new(intent="test", brain="claude", steps=list(steps))


@pytest.fixture
def facts():
    return TimelineFacts.minimal(v1_clip_ids=["c_v1", "c_v2"], clip_ids=["c_v1", "c_v2"],
                                 v1_boundaries=[12.0], selection="c_v2")


def test_curve_preset_and_freeze_plans_validate(facts):
    out = validate_plan(_plan(
        _step("set_speed", clip_id="c_v1", preset="Jump Cut"),
        _step("set_speed", clip_id="c_v2", curve=[[0, 1], [0.5, 0.25], [1, 1]]),
        _step("freeze_frame", time=4.0, duration=2),
    ), facts)
    assert out.steps[0].args["preset"] == "jump_cut"
    assert {s.tool for s in out.steps} == {"set_speed", "freeze_frame"}
    assert all(s.stage == Sc.TOOL_STAGE["set_speed"] for s in out.steps)
    checks = {(p.check, tuple(sorted(p.args.items()))) for p in out.postconditions}
    assert ("speed_equals", (("clip_id", "c_v1"), ("preset", "jump_cut"))) in checks
    assert ("freeze_held", (("duration", 2),)) in checks


@pytest.mark.parametrize("args, needle", [
    ({"clip_id": "c_v1"}, "needs one of factor, curve or preset"),
    ({"clip_id": "c_v1", "factor": 2, "preset": "hero"}, "ONE of factor, curve or preset"),
    ({"clip_id": "c_v1", "preset": "warp"}, "must be one of"),
    ({"clip_id": "c_v1", "curve": [[0, 1], [1, 20]]}, "outside 0.1-10x"),
    ({"clip_id": "c_v1", "curve": [[0, 1]]}, "2-32 points"),
    ({"clip_id": "c_v1", "curve": [[0, 1], ["a", 2]]}, "must be a number"),
])
def test_bad_speed_plans_are_refused(facts, args, needle):
    with pytest.raises(PlanRejected) as ei:
        validate_plan(_plan(_step("set_speed", **args)), facts)
    assert needle in "; ".join(ei.value.reasons)


def test_freeze_plan_bounds(facts):
    with pytest.raises(PlanRejected) as ei:
        validate_plan(_plan(_step("freeze_frame", time=1.0, duration=30)), facts)
    assert "freeze_frame.duration" in "; ".join(ei.value.reasons)


# ------------------------------------------------------------------ verify

def test_speed_equals_preset_and_freeze_held(tmp_path):
    from types import SimpleNamespace
    from video_ai_editor.agent.dispatch import dispatch
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import Clip
    s = EDLStore(tmp_path / "s")
    s.edl.get_track("v1").clips = [Clip(src="/nonexistent/a.mp4", in_=0, out=6, start=0, id="c_a")]
    s.commit("init", {}, "init")
    dispatch(s, "set_speed", {"clip_id": "c_a", "preset": "hero"})

    def check(name, **args):
        pc = Sc.Postcondition(check=name, args=args, human=name,
                              needs_render=Sc.CHECK_SPECS[name].needs_render,
                              headline=Sc.CHECK_SPECS[name].headline)
        ctx = SimpleNamespace(edl=s.edl, store=s)
        return V.CHECKS[name](ctx, pc)

    assert check("speed_equals", clip_id="c_a", preset="Hero").passed is True
    assert check("speed_equals", clip_id="c_a", preset="bullet").passed is False
    assert check("freeze_held").passed is False
    # An unprobeable source still freezes (at the split point).
    dispatch(s, "freeze_frame", {"time": 1.0, "duration": 2.0})
    assert check("freeze_held", duration=2.0).passed is True
    assert check("freeze_held", duration=3.0).passed is False
