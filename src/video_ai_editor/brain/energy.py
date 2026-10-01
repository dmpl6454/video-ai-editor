"""The Energy table (spec §4.10) and the wave's fixed constants — the ONE
place every planner pass reads a threshold from.

Editing Energy is 1-10. The spec names the columns at 1, 3, 5, 7, 9 and 10;
`knob()` interpolates a numeric row linearly between the named columns and
holds an enum/bool row at the nearest LOWER named column, so energy 6 sits
between the 5 and 7 columns and never invents a value the table does not
contain. Every row declares the direction it moves in (`ROW_DIRECTION`),
which `tests/test_brain_energy_table.py` checks knob by knob.

The constants below the table are the numbers the EB1 brief freezes for this
wave ("Energy constants used this wave"); they are named here so a pass
never carries a literal.
"""
from __future__ import annotations

from typing import Any

COLUMNS: tuple[int, ...] = (1, 3, 5, 7, 9, 10)

#: knob → {energy column → value}; numeric rows interpolate, enum rows hold.
ENERGY_ROWS: dict[str, dict[int, Any]] = {
    "min_silence_s":        {1: 1.5, 3: 1.2, 5: 0.8, 7: 0.5, 9: 0.4, 10: 0.35},
    "keep_pad_s":           {1: 0.25, 3: 0.20, 5: 0.15, 7: 0.10, 9: 0.07, 10: 0.06},
    "dead_air_s":           {1: 2.0, 3: 1.6, 5: 1.2, 7: 0.9, 9: 0.7, 10: 0.6},
    "soft_fillers":         {1: False, 3: False, 5: False, 7: True, 9: True, 10: True},
    "importance_floor":     {1: 0.10, 3: 0.15, 5: 0.20, 7: 0.28, 9: 0.35, 10: 0.40},
    "camera_min_shot_s":    {1: 4.0, 3: 3.5, 5: 2.5, 7: 2.0, 9: 1.6, 10: 1.4},
    "camera_max_hold_s":    {1: 40, 3: 30, 5: 25, 7: 14, 9: 10, 10: 8},
    "switch_cost":          {1: 0.45, 3: 0.40, 5: 0.35, 7: 0.25, 9: 0.15, 10: 0.12},
    "punch_gap_s":          {1: 40, 3: 30, 5: 20, 7: 12, 9: 6, 10: 5},
    "punch_scale":          {1: 1.08, 3: 1.08, 5: 1.10, 7: 1.12, 9: 1.14, 10: 1.15},
    "caption_mode_reel":    {1: "minimal", 3: "podcast", 5: "dynamic", 7: "dynamic", 9: "viral", 10: "viral"},
    "cold_open":            {1: "title_only", 3: "title_only", 5: "if_quotable", 7: "yes", 9: "yes", 10: "yes"},
    "turn_floor_s":         {1: 0.30, 3: 0.30, 5: 0.30, 7: 0.15, 9: 0.15, 10: 0.15},
    "protected_pauses":     {1: "all", 3: "all", 5: "all", 7: "all", 9: "laughter", 10: "laughter"},
    "acoustic_filler_conf": {1: None, 3: None, 5: 0.7, 7: 0.7, 9: 0.6, 10: 0.6},
    "jump_cut_scale":       {1: 1.06, 3: 1.08, 5: 1.08, 7: 1.12, 9: 1.12, 10: 1.12},
    "punch_push_s":         {1: 3.0, 3: 2.0, 5: 0.4, 7: 0.4, 9: 0.3, 10: 0.3},
}

#: How each row moves as energy rises: "down", "up" or "enum" (ordered by ENUM_ORDER).
ROW_DIRECTION: dict[str, str] = {
    "min_silence_s": "down", "keep_pad_s": "down", "dead_air_s": "down", "soft_fillers": "up",
    "importance_floor": "up", "camera_min_shot_s": "down", "camera_max_hold_s": "down", "switch_cost": "down",
    "punch_gap_s": "down", "punch_scale": "up", "caption_mode_reel": "enum", "cold_open": "enum",
    "turn_floor_s": "down", "protected_pauses": "enum", "acoustic_filler_conf": "enum", "jump_cut_scale": "up",
    "punch_push_s": "down",
}
ENUM_ORDER: dict[str, list[Any]] = {
    "caption_mode_reel": ["minimal", "podcast", "dynamic", "viral"],
    "cold_open": ["title_only", "if_quotable", "yes"],
    "protected_pauses": ["all", "laughter"],
    "acoustic_filler_conf": [None, 0.7, 0.6],
}

# --- the wave's fixed constants (EB1 brief "Energy constants used this wave") ---
LEAD_FRAMES = 3                 # a switch lands this many frames before the first-word onset …
LEAD_MAX_S = 0.15               # … clamped so a 24 fps project never leads by more than this
ONSET_TOL_S = 0.04              # how well an onset is known (measured, P2: -27..+36 ms over 34 turns, the late end at the seam
                                # the slice caught at 0.1558 s with 0.03): the lead is planned this much inside the clamp
SNAP_S = 0.25                   # a preceding word gap within this much of the onset takes the switch
GAP_MIN_S = 0.12                # … when the gap is at least this long
BACKCHANNEL_MAX_S = 0.6         # a turn shorter than this never switches
HIDE_CUT_MIN_S = 0.4            # a tighten seam that removed at least this much is hidden …
HIDE_TOL_S = 0.0002             # … 0.4 s to the frame is written 0.3999 (times are 4 decimals): that IS 0.4
AT_CUT_HIDE_MAX_S = 1.5         # … by the listener's close for at most this long (two closes, no wide)
MIN_SHOT_AT_CUT_S = 1.2         # min-shot for the piece an at_cut switch opens
MIN_SHOT_REEL_S = 1.2           # min-shot on a reel
SWITCH_RATE_MAX = 8             # switches per minute, incl. at_cut ones …
SWITCH_RATE_MAX_PREMIUM = 6     # … and the premium podcast's cap
LEAD_S = 0.8                    # a punch-in starts at least this far before its peak
RELEASE_WINDOW_S = 8            # a seam within this much after the sentence releases the punch as a step
RELEASE_MIN_S = 1.2             # a timed release eases out over at least this long
PUNCH_HOOK_SCALE = 1.12         # the hook statement's punch
PUNCH_SAME_CLIP_GAP_S = 4.0     # never two punches on one clip within this
REEL_PUNCH_GAP_S = 6            # a reel's punch cadence
AIR_MIN_S = 0.04                # air before a kept onset, always
MIN_PIECE_S = 0.25              # no piece of the programme is shorter: a camera change within this of a cut edge closes the gap
TROUGH_SEARCH_S = 0.08          # the trough is looked for within ± this of the word boundary
TIE_DB = 1.0                    # troughs within this many dB tie
VOICED_INSET_S = 0.02           # a word's voiced span is [t0 + this, t1 − this]
KEEP_PAUSE_FRAC = 0.45          # a protected pause keeps max(pad, this × its length) …
KEEP_PAUSE_MAX_S = 1.2          # … capped here
PROTECT_EMOTION = 0.6           # a pause after a scene this emotional is protected
PROTECT_HOOK = 0.6              # … or before an answer to a question this strong
PROTECT_IMPORTANCE = 0.7        # … or after a conclusion this important
BED_REL_LU = 24                 # Subtle: the bed sits this far under speech …
DUCK_LU = 6                     # … and ducks this much further under it
BED_LUFS = -18.0                # the bundled beds' own loudness (presets/music/*.json)
SEAM_FADE_S = 0.005             # the dialogue lane's fade at every internal seam
REEL_MAX_S = 90.0               # a reel is at most this long (else: an episode)
OPEN_ON_MAX_S = 60.0            # `open_on` only for reels up to this long
OPEN_ON_EARLY_S = 8.0           # a hook already inside the first N s of the body stays put
HOOK_STANDALONE_MIN = 0.75      # the hook opens the reel only when it stands alone this well
JOIN_STANDALONE_MIN = 0.6       # a join passes when the follower stands alone this well vs its new predecessor
DURATION_TOL_FRAC = 0.08        # the whole-sentence fit stops within this share of the asked length
BODY_CONTIGUOUS_MIN = 0.6       # at least this share of the body stays contiguous
HEAD_KEEP_S = 0.3               # an episode opens at most this long before its first sound
HIDE_STEP_MIN = 0.06            # a scale-step hide is claimed only when the two sides differ by at least this (as decoded)
HIDE_PUNCH_ROOM = 0.05          # a punch that opens on a hold pushes at least this much further (the review's bar for "a push the
                                # viewer can see": 1.08 -> 1.10 is 2 %): it is raised to hold + this when its own scale is less
HIDE_KEY_LAG_FRAMES = 2         # a hold's second key sits this many frames after its first: two keys, or the export drops it
FULL_TURN_MIN_S = BACKCHANNEL_MAX_S   # a turn at least this long is a full turn: it is shown on its speaker's close
OVERLAP_TURN_SHARE = 0.5        # a turn this share of which is spoken over another voice is a true overlap: it never switches


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def knob(name: str, energy: int) -> Any:
    """The table's value for `name` at `energy` (clamped to 1..10)."""
    row = ENERGY_ROWS[name]
    e = max(COLUMNS[0], min(COLUMNS[-1], int(energy)))
    if e in row:
        return row[e]
    lo = max(c for c in COLUMNS if c < e)
    hi = min(c for c in COLUMNS if c > e)
    a, b = row[lo], row[hi]
    if ROW_DIRECTION[name] == "enum" or isinstance(a, bool) or a is None or b is None:
        return a
    f = (e - lo) / (hi - lo)
    v = _lerp(float(a), float(b), f)
    return int(round(v)) if isinstance(a, int) and isinstance(b, int) else round(v, 4)


def knobs(energy: int) -> dict[str, Any]:
    """Every knob at `energy` — the `ctx.energy` a pass reads."""
    out = {name: knob(name, energy) for name in ENERGY_ROWS}
    out["energy"] = max(COLUMNS[0], min(COLUMNS[-1], int(energy)))
    return out


def lead_s(fps: float) -> float:
    """The anticipation lead: `LEAD_FRAMES` at the project rate, never more
    than `LEAD_MAX_S` once the onset's own uncertainty is counted."""
    return min(LEAD_MAX_S - ONSET_TOL_S, LEAD_FRAMES / max(1.0, float(fps)))


#: A style as a person says it ("the premium podcast style"), never its identifier.
STYLE_NAMES = {"viral_reel": "reel", "premium_podcast": "premium podcast", "clean_professional": "clean professional",
               "luxury": "luxury"}


def style_name(style: str | None) -> str:
    return STYLE_NAMES.get(str(style), str(style or "").replace("_", " ") or "this")


def rate_cap(style: str | None) -> int:
    return SWITCH_RATE_MAX_PREMIUM if (style or "").startswith("premium") else SWITCH_RATE_MAX


__all__ = ["COLUMNS", "ENERGY_ROWS", "ROW_DIRECTION", "ENUM_ORDER", "knob", "knobs", "rate_cap", "lead_s", "STYLE_NAMES", "style_name"]
