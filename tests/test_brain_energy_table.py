"""The Energy table (spec §4.10, `brain/energy.py`): every knob the planner
passes read comes from ONE table, each row monotone in its declared
direction from energy 1 to 10, and the wave's fixed constants are the
brief's numbers."""
from __future__ import annotations

import pytest

from video_ai_editor.brain import energy as E

ENERGIES = range(1, 11)


def test_rows_monotone():
    for name, direction in E.ROW_DIRECTION.items():
        values = [E.knob(name, e) for e in ENERGIES]
        if direction == "enum":
            order = E.ENUM_ORDER[name]
            ranks = [order.index(v) for v in values]
            assert ranks == sorted(ranks), (name, values)
            continue
        pairs = list(zip(values, values[1:]))
        if direction == "down":
            assert all(b <= a for a, b in pairs), (name, values)
        else:
            assert all(b >= a for a, b in pairs), (name, values)
        assert values[0] != values[-1], (name, "a row that never moves is not a knob")


def test_columns_are_the_spec_table_at_the_named_energies():
    assert E.knob("min_silence_s", 5) == 0.8 and E.knob("min_silence_s", 1) == 1.5 and E.knob("min_silence_s", 10) == 0.35
    assert E.knob("keep_pad_s", 5) == 0.15 and E.knob("keep_pad_s", 10) == 0.06
    assert E.knob("dead_air_s", 5) == 1.2
    assert E.knob("camera_min_shot_s", 5) == 2.5 and E.knob("camera_min_shot_s", 9) == 1.6
    assert E.knob("punch_scale", 5) == 1.10 and E.knob("punch_push_s", 5) == 0.4 and E.knob("punch_push_s", 1) == 3.0
    assert E.knob("turn_floor_s", 5) == 0.30 and E.knob("turn_floor_s", 7) == 0.15
    assert E.knob("caption_mode_reel", 5) == "dynamic" and E.knob("caption_mode_reel", 9) == "viral"
    assert E.knob("protected_pauses", 5) == "all" and E.knob("protected_pauses", 9) == "laughter"
    assert E.knob("acoustic_filler_conf", 5) == 0.7 and E.knob("acoustic_filler_conf", 3) is None
    assert E.knob("jump_cut_scale", 5) == 1.08 and E.knob("jump_cut_scale", 7) == 1.12


@pytest.mark.parametrize("name,value", [
    ("LEAD_FRAMES", 3), ("LEAD_MAX_S", 0.15), ("BACKCHANNEL_MAX_S", 0.6), ("HIDE_CUT_MIN_S", 0.4),
    ("SWITCH_RATE_MAX", 8), ("SWITCH_RATE_MAX_PREMIUM", 6), ("LEAD_S", 0.8), ("RELEASE_WINDOW_S", 8),
    ("PUNCH_HOOK_SCALE", 1.12), ("AIR_MIN_S", 0.04), ("BED_REL_LU", 24), ("DUCK_LU", 6), ("MIN_SHOT_AT_CUT_S", 1.2),
])
def test_wave_constants(name, value):
    assert getattr(E, name) == value


def test_between_columns_interpolates_and_every_pass_reads_the_table():
    k = E.knobs(6)
    assert 0.5 < k["min_silence_s"] < 0.8
    assert set(E.ROW_DIRECTION) <= set(k)
