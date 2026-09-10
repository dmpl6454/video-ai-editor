"""Every filesystem-typed tool argument, with its guard decision.

This table used to live only in `tests/test_path_guards.py` as
`EXPECTED_GUARDS`. It moved here (spec §1.3 rule 6) because the Prompt
Editor's `validate_plan` needs the SAME table at runtime: a model-authored
plan may never carry a `write` path at all, and a `read` path must be an
exact member of `facts.allowed_paths` after `Path.resolve()` — session
uploads, `presets/music/*.wav`, `presets/end_cards/*`, TTS wavs produced
earlier in the same plan. A second, hand-maintained copy in the validator
would be the seventh hole the test's docstring warns about; one table,
imported by both, cannot drift.

The test keeps deriving the candidate set independently from `/api/tools`
and asserting it equals this table, and keeps `test_the_guard_count_is_pinned`,
so adding a path-shaped argument without a row here still fails the run.

WHY THIS TABLE IS EXHAUSTIVE AND NOT A CASE STUDY (from the test)
------------------------------------------------------------------
A single-tool version — "call `add_clip` with /etc/passwd, assert it raises"
— would have passed for the entire life of 0.5.0 while `import_srt.path`,
`multicam.srcs`, `find_broll.bin` and the three `export_*.path` args were
completely unguarded. `add_clip` was the one tool anybody had thought about.
"""
from __future__ import annotations

from typing import Literal

PathGuard = Literal["read", "write", "exempt"]

#: (tool, arg) -> guard. "read" and "write" name the allowlist the handler must
#: consult; "exempt" carries the reason the argument is not a filesystem path
#: at all, and is the only way to keep something off the guarded list.
PATH_ARGS: dict[tuple[str, str], PathGuard] = {
    ("add_clip", "src"): "read",
    ("add_music", "src"): "read",
    ("add_sticker", "src"): "read",
    ("apply_brand_kit", "end_card"): "read",
    ("apply_lut", "src"): "read",
    ("apply_lut", "lut_path"): "read",   # alias of src; the handler resolves it into src BEFORE _safe_src
    ("find_broll", "bin"): "read",
    ("import_srt", "path"): "read",
    ("match_style", "reference"): "read",
    ("multicam", "srcs"): "read",
    ("export_ass", "path"): "write",
    ("export_srt", "path"): "write",
    ("export_vtt", "path"): "write",
    # `font` is a BUNDLED font NAME resolved by name inside config.FONTS_DIR
    # (apply_brand_kit validates it against a glob of that directory and
    # rejects anything else). It never reaches the filesystem as a caller path.
    ("add_text", "font"): "exempt",
    # A dotted ATTRIBUTE path — "transform.x", "audio.gain_db" — not a
    # filesystem path. The genuinely dangerous half of this tool is its
    # `value` when the leaf is `src`, which the handler guards; see
    # test_set_property_src_value_is_guarded in tests/test_path_guards.py.
    # (Plans may not use set_property at all — schema.PLAN_DENY.)
    ("set_property", "path"): "exempt",
    # Same as add_text.font: a bundled font NAME, validated against a glob of
    # config.FONTS_DIR and rejected if it is not one of them.
    ("apply_brand_kit", "font"): "exempt",
    # --- `name` args -----------------------------------------------------
    # Added in 0.6.0. `_NAME_HINT` did not match `name`, and these tools
    # advertise no description for `_DESC_HINT` to hit, so `save_show_template`
    # spent 0.5.0 as an unnoticed arbitrary-location `.json` write and
    # `apply_show_template` as the matching read — routed around every guard in
    # dispatch.py because the argument simply was not called `path`. That is
    # precisely the miss this table exists to make impossible, so `name` is now
    # part of the derivation and every tool that has one is accounted for.
    #
    # These two DO reach the filesystem, but as a leaf name inside
    # PRESETS_DIR/shows — never as a caller-controlled path — so the allowlist
    # is the wrong tool. `show/templates.py::_show_path` enforces a
    # `[A-Za-z0-9._-]` whitelist and refuses anything that is not a plain name;
    # test_show_template_names_cannot_escape_presets is the behaviour
    # half of this exemption.
    ("save_show_template", "name"): "exempt",
    ("apply_show_template", "name"): "exempt",
    # Pure in-memory dict lookups against a literal table; the value never
    # reaches the filesystem in any form.
    ("apply_export_preset", "name"): "exempt",   # _EXPORT_PRESETS
    ("apply_text_template", "name"): "exempt",   # built-in bundle table
    ("apply_template", "name"): "exempt",        # show/templates.py::TEMPLATES
    # Human display text for a lower-third graphic. Not an identifier at all.
    ("add_lower_third", "name"): "exempt",
}

#: For the count pin in tests/test_path_guards.py and test_prompt_contracts.py.
PATH_ARGS_COUNT = 22   # +1 on 2026-09-08: apply_lut.lut_path (alias of src) is now advertised


def guarded_args(kind: PathGuard) -> frozenset[tuple[str, str]]:
    return frozenset(k for k, v in PATH_ARGS.items() if v == kind)


def path_args_for(tool: str) -> dict[str, PathGuard]:
    """`{arg: guard}` for one tool, exempt rows excluded — what `validate_plan`
    walks for every step (§1.3 rule 6)."""
    return {arg: guard for (t, arg), guard in PATH_ARGS.items() if t == tool and guard != "exempt"}


__all__ = ["PathGuard", "PATH_ARGS", "PATH_ARGS_COUNT", "guarded_args", "path_args_for"]
