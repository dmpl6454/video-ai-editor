"""Every argument a handler READS must be advertised in its tool schema.

WHY this exists: a key-free Prompt Editor validates a local model's plan
against `GET /api/tools` and REJECTS any argument the schema does not list, so
a small model can never smuggle a path or an unknown knob through. That rule
is only safe once the schema is complete — the day it was introduced, 13 tools
read 22 arguments their `input_schema` never mentioned (`auto_reframe.
subject_track`, `add_text.size`, `apply_lut.lut_path`, …), so a correct plan
would have been refused.

The census below is recomputed from source on every run, not hand-listed. It
reads each DISPATCH handler with `inspect.getsource` and harvests the argument
names through the idioms dispatch.py actually uses:

  (a) `args.get("k")`, `args.pop("k")`, `args["k"]`, `"k" in args`
  (b) helpers that take the key as a literal: `_num(args, "k")`,
      `_enum_arg(args, "k", …)` — a bare `args.get` regex would miss these and
      the test would go green by blindness, so a self-check below proves the
      idiom is covered
  (c) helpers/handlers the handler forwards `args` to verbatim:
      `_kf_props_arg(args)`, `diarize(store, args)` — followed recursively,
      because `assign_caption_speakers` reads `fallback` only through `diarize`
  (d) a loop over a tuple of names whose body reads `args[k]` / `k in args` /
      `_num(args, k)` / `args.get(k)` — `set_canvas` reads w/h/fps the first
      way, `add_text` reads anim_in/anim_out the last way
  (e) `{… for k, v in args.items() if k in ("a", "b", …)}` — `color_grade`
      accepts `sat` this way and nowhere else

A NEW idiom in dispatch.py that none of these match would silently shrink the
census; `test_census_sees_every_read_idiom` pins each idiom to a real handler
so that regression shows up here rather than as a rejected plan.
"""
from __future__ import annotations

import importlib
import inspect
import re
from typing import Callable

import pytest

from video_ai_editor.agent import tools as tools_mod
from video_ai_editor.agent.tools import list_tools, unknown_args

# `video_ai_editor.agent` re-exports the dispatch FUNCTION under the module's
# name, so `from … import dispatch` hands back the callable, not the module.
D = importlib.import_module("video_ai_editor.agent.dispatch")

_STR = r'''["']([A-Za-z_]\w*)["']'''
_DIRECT = re.compile(r'args(?:\.get|\.pop)\(\s*' + _STR)                    # (a)
_INDEX = re.compile(r'args\[\s*' + _STR + r'\s*\]')                        # (a)
_MEMBER = re.compile(_STR + r'\s+(?:not\s+)?in\s+args\b')                   # (a)
_HELPER_KEY = re.compile(r'\b(_\w+)\(\s*args\s*,\s*' + _STR)                # (b)
_FORWARD = re.compile(r'\b(\w+)\(\s*(?:store\s*,\s*)?args\s*[,)]')          # (c)
_LOOP = re.compile(                                                         # (d)
    r'for\s+([A-Za-z_]\w*)(?:\s*,\s*\w+)*\s+in\s+\((.*?)\)\s*:\s*\n((?:[ \t]+.*\n?)+)',
    re.S)
_ITEMS = re.compile(                                                        # (e)
    r'for\s+([A-Za-z_]\w*)\s*,\s*\w+\s+in\s+args\.items\(\)(.*?)\b\1\s+in\s+\((.*?)\)',
    re.S)


def _tuple_keys(text: str) -> set[str]:
    """Names in a tuple literal. Nested tuples (`("w", LO, HI), …`) contribute
    their FIRST string — that is the key; the rest are bounds."""
    nested = re.findall(r'\(\s*' + _STR, text)
    return set(nested) if nested else set(re.findall(_STR, text))


def handler_reads(fn: Callable, _seen: set | None = None) -> set[str]:
    """Every argument name `fn` reads, following forwarded `args` recursively."""
    seen = _seen if _seen is not None else set()
    if fn in seen:
        return set()
    seen.add(fn)
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return set()
    keys = set(_DIRECT.findall(src)) | set(_INDEX.findall(src)) | set(_MEMBER.findall(src))
    keys |= {k for _, k in _HELPER_KEY.findall(src)}
    for var, tup, body in _LOOP.findall(src):
        # Every way a body can read the loop variable out of args — the last
        # alternative (`args.get(k)` / `args.pop(k)`) is how add_text reads
        # anim_in/anim_out; without it a key read only that way is invisible.
        reads_var = (rf'\b{var}\s+(?:not\s+)?in\s+args\b'
                     rf'|args\[\s*{var}\s*\]'
                     rf'|\(\s*args\s*,\s*{var}\b'
                     rf'|args\.(?:get|pop)\(\s*{var}\b')
        if re.search(reads_var, body):
            keys |= _tuple_keys(tup)
    for _var, _, tup in _ITEMS.findall(src):
        keys |= _tuple_keys(tup)
    for name in _FORWARD.findall(src):
        callee = getattr(D, name, None)
        if callable(callee) and callee is not fn:
            keys |= handler_reads(callee, seen)
    return keys


def _advertised() -> dict[str, set[str]]:
    return {t["name"]: set(t["input_schema"]["properties"]) for t in list_tools()}


def _schema(tool: str) -> dict:
    return next(t["input_schema"]["properties"] for t in list_tools() if t["name"] == tool)


def census() -> dict[str, list[str]]:
    """{tool: [handler-only args]} for every advertised tool — expected empty."""
    out: dict[str, list[str]] = {}
    for tool, props in _advertised().items():
        fn = D.DISPATCH.get(tool)
        if fn is None:
            continue
        extra = sorted(handler_reads(fn) - props)
        if extra:
            out[tool] = extra
    return out


# --- the contract -----------------------------------------------------------

def test_every_argument_a_handler_reads_is_advertised():
    gaps = census()
    assert gaps == {}, (
        "handlers read arguments their input_schema does not list — a "
        "schema-validating client would reject a correct call. Add each to "
        f"agent/tools.py (or stop reading it): {gaps}")


def test_every_advertised_tool_has_a_handler():
    missing = sorted(set(_advertised()) - set(D.DISPATCH))
    assert missing == [], f"advertised but not dispatchable: {missing}"


@pytest.mark.parametrize("handler,expected,idiom", [
    ("add_clip", {"in", "out"}, "(b) _num(args, 'in')"),
    ("add_caption_track", {"style", "position"}, "(b) _enum_arg(args, 'style', …)"),
    ("add_keyframe", {"prop", "props"}, "(c) _kf_props_arg(args)"),
    ("assign_caption_speakers", {"fallback"}, "(c) diarize(store, args)"),
    ("set_canvas", {"w", "h", "fps"}, "(d) for key, lo, hi in ((\"w\", …), …)"),
    ("add_text", {"anim_in", "anim_out"}, "(d) for k, dst in ((…)): args.get(k)"),
    ("color_grade", {"sat", "saturation"}, "(e) args.items() if k in (…)"),
])
def test_census_sees_every_read_idiom(handler, expected, idiom):
    """Guards the census against going green by blindness: each regex idiom is
    pinned to a handler known to use it. If dispatch.py stops using an idiom
    here, update the pin — do not delete it."""
    got = handler_reads(D.DISPATCH[handler])
    assert expected <= got, f"idiom {idiom} no longer detected on {handler}: {sorted(got)}"


# --- newly advertised enums must equal what the handler accepts -----------------

def _handler_src(tool: str) -> str:
    return inspect.getsource(D.DISPATCH[tool])


def test_set_clip_fit_mode_enum_matches_handler():
    m = re.search(r'if mode not in \(([^)]*)\)', _handler_src("set_clip_fit"))
    assert m, "set_clip_fit no longer validates `mode` with a tuple literal"
    accepted = set(re.findall(_STR, m.group(1)))
    props = _schema("set_clip_fit")
    assert set(props["mode"]["enum"]) == accepted == set(props["fit"]["enum"])


def test_auto_reframe_aspect_enum_matches_handler():
    m = re.search(r'ratios = \{(.*?)\}', _handler_src("auto_reframe"))
    assert m, "auto_reframe no longer keeps its ratio table as a dict literal"
    accepted = set(re.findall(r'"([\d]+:[\d]+)"\s*:', m.group(1)))
    props = _schema("auto_reframe")
    assert set(props["aspect"]["enum"]) == accepted == set(props["ratio"]["enum"])


@pytest.mark.parametrize("spelling", ["target", "target_lang", "caption_lang"])
def test_auto_caption_target_spellings_share_enum_and_defer_to_handler(spelling):
    """The handler normalises 'hindi'/'roman'/'english'/'spanish' itself and
    raises a clear error against CAPTION_TARGETS; the generic boundary enum
    check must stand down on ALL three spellings. Until `target` carried the
    flag, target='hindi' was a 400 while target_lang='hindi' reached the
    normaliser — an alias strictly more permissive than its canonical name."""
    props = _schema("auto_caption")
    assert set(props[spelling]["enum"]) == set(D.CAPTION_TARGETS)
    assert props[spelling].get("x-validated-by-handler") is True


# The caption pipeline stubs live with the tests that own auto_caption's
# routing; reusing them (rather than forking a copy here) keeps this proof
# valid the day the handler grows a new collaborator that fixture must stand
# in for. `spy` is re-exported on purpose so pytest can inject it below.
from test_caption_targets import _store as _caption_store, spy  # noqa: E402,F401


def test_target_hindi_reaches_the_handler_through_dispatch_and_becomes_hi(tmp_path, spy):
    """End to end through dispatch(): the boundary lets the spelling through
    unchanged and the handler is the one that turns it into the canonical
    code, exactly as it already did for the alias spellings."""
    out = D.dispatch(_caption_store(tmp_path), "auto_caption", {"target": "hindi"})
    assert out["target"] == "hi"


def test_target_still_type_checked_at_the_boundary():
    """Standing the ENUM check down must not stand the TYPE check down. A bool
    is the boundary's canonical non-string (numbers are accepted as strings on
    purpose — see _arg_type_ok and VAI-03)."""
    with pytest.raises(ValueError, match="auto_caption.target must be of type string"):
        D._validate_tool_args("auto_caption", {"target": True})
    # A wrong spelling is refused by the handler's own check, not silently
    # captioned in the spoken language — proven by test_caption_targets::
    # test_an_unknown_target_is_a_clean_error_naming_the_choices; here we only
    # pin that the boundary no longer pre-empts it.
    D._validate_tool_args("auto_caption", {"target": "hindi"})  # must not raise


@pytest.mark.parametrize("tool,alias,canonical", [
    ("remove_effect", "idx", "index"),
    ("apply_lut", "lut_path", "src"),
    ("translate_captions", "to", "target_lang"),
    ("color_grade", "sat", "saturation"),
    ("set_clip_fit", "mode", "fit"),
    ("auto_reframe", "aspect", "ratio"),
])
def test_alias_is_typed_like_its_canonical_and_says_so(tool, alias, canonical):
    props = _schema(tool)
    assert props[alias]["type"] == props[canonical]["type"]
    assert canonical in props[alias]["description"], "alias must name what it aliases"
    assert "prefer" in props[alias]["description"].lower()


def test_add_text_style_defaults_match_the_model():
    """The advertised defaults are the TextStyle/Transform defaults the handler
    falls back to, so a client rendering a form pre-fills what omission does."""
    from video_ai_editor.edl.schema import TextStyle, Transform
    props = _schema("add_text")
    ts, tf = TextStyle(), Transform()
    assert props["size"]["default"] == ts.size
    assert props["stroke"]["default"] == ts.stroke
    assert props["stroke_w"]["default"] == ts.stroke_w
    assert props["scale"]["default"] == tf.scale
    assert props["rotation"]["default"] == tf.rotation
    assert props["opacity"]["default"] == tf.opacity


def test_required_lists_only_advertised_properties():
    for t in list_tools():
        schema = t["input_schema"]
        stray = sorted(set(schema.get("required") or []) - set(schema["properties"]))
        assert stray == [], f"{t['name']}: required names unadvertised {stray}"


# --- the validator core a schema-driven client will use -------------------------

def test_unknown_args_names_only_the_unadvertised_keys():
    assert unknown_args("add_music", {"src": "x.mp3", "gain_db": -6}) == ["gain_db"]
    assert unknown_args("auto_reframe", {"ratio": "9:16", "subject_track": False}) == []
    assert unknown_args("no_such_tool", {"a": 1}) == ["a"]


def test_unknown_args_is_not_wired_into_dispatch():
    """Documented decision, not an oversight: dispatch() still tolerates extras
    (see _validate_tool_args). If someone wires rejection in, this test — and
    the smoke suite that passes extras — is where the change must be owned."""
    assert "unknown_args" not in inspect.getsource(D.dispatch)
    assert tools_mod.unknown_args is unknown_args
