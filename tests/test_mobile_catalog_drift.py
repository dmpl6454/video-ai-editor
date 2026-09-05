"""The phone's tool catalogue is a hand-curated copy. This is the drift alarm.

`mobile/lib/catalog.ts` names backend tools, backend argument names and
`/api/features` gate keys as string literals in TypeScript. Nothing in the
TypeScript toolchain can know that `upscale` is still a tool, that
`auto_reframe` still takes `subject_track`, or that the feature report still
has a `tracking` key — so a rename on the Python side ships a phone build whose
buttons 422 at the moment of the tap, on a device with no console.

The unit tests in `mobile/__tests__/catalog.test.ts` check the catalogue's
internal consistency. THIS file is the only place the two halves are compared,
which is why it lives in the pytest suite: the backend is what moves.

Skips cleanly when `mobile/` is absent so the Python suite still runs in a
checkout that has not installed the app.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CATALOG_TS = REPO / "mobile" / "lib" / "catalog.ts"
JOBS_TS = REPO / "mobile" / "lib" / "jobs.ts"

pytestmark = pytest.mark.skipif(
    not CATALOG_TS.exists(),
    reason="mobile/ is not installed in this checkout")


def _source() -> str:
    """catalog.ts with `//` line comments stripped.

    Not cosmetic: the desktop catalogue's own header comment contains the
    literal `tool: '…'` while explaining this very mechanism, and an
    uncommented scan reports `…` as a missing backend tool.
    """
    text = CATALOG_TS.read_text(encoding="utf-8")
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _handler_arg_names(tool: str) -> set[str]:
    """Every argument name a tool really accepts.

    The union of its advertised `input_schema` and the `args.get("x")` /
    `args["x"]` reads in its handler, because the two are NOT the same set:
    `auto_reframe` reads `subject_track` but does not advertise it, and the
    desktop catalogue adds it to the form on purpose. Checking against the
    schema alone would fail a catalogue that is correct.
    """
    import ast

    from video_ai_editor.agent.dispatch import DISPATCH

    tools = _backend_tools()
    names = set((tools.get(tool, {}).get("input_schema") or {}).get("properties") or {})
    fn = DISPATCH.get(tool)
    if fn is None:
        return names
    source = (REPO / "src/video_ai_editor/agent/dispatch.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == fn.__name__:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            names |= set(re.findall(r"args(?:\.get)?[\(\[]\s*['\"]([^'\"]+)['\"]", body))
            break
    return names


def _string_values(key: str, text: str) -> set[str]:
    """Every `key: 'value'` / `key: "value"` in a TypeScript source."""
    return set(re.findall(rf"\b{key}\s*:\s*['\"]([^'\"]+)['\"]", text))


def _backend_tools() -> dict[str, dict]:
    from video_ai_editor.agent.dispatch import list_tools
    return {t["name"]: t for t in list_tools()}


def test_every_catalog_tool_still_exists_in_the_dispatcher():
    """A rename on the Python side must fail HERE, not on the owner's phone."""
    advertised = set(_backend_tools())
    named = _string_values("tool", _source())
    assert named, "no `tool:` entries found — has catalog.ts changed shape?"
    unknown = sorted(named - advertised)
    assert not unknown, (
        f"mobile/lib/catalog.ts names tools /api/tools no longer advertises: "
        f"{unknown}")


def test_every_catalog_gate_key_still_exists_in_the_feature_report():
    """A gate key that no longer exists reads as 'not unavailable', so the card
    stays enabled and the failure surfaces as a 422 after the tap."""
    from video_ai_editor.ai.features import feature_report
    report = feature_report()
    keys = {entry["key"] for group in ("available", "unavailable")
            for entry in report.get(group, []) if isinstance(entry, dict)}
    named = _string_values("gate", _source())
    unknown = sorted(named - keys)
    assert not unknown, (
        f"catalog.ts gates on feature keys the backend does not report: {unknown}")


def test_every_gate_unless_field_is_a_real_tool_argument():
    """`gateUnless: {field: 'subject_track'}` only works while the tool still
    has that argument — otherwise the escape hatch silently never fires and the
    tool is greyed out forever."""
    text = _source()
    problems: list[str] = []
    # Each entry is `tool: 'x', ... gateUnless: { field: 'y'`, in that order.
    for tool, field in re.findall(
            r"tool:\s*['\"]([^'\"]+)['\"](?:(?!tool:).)*?"
            r"gateUnless:\s*\{\s*field:\s*['\"]([^'\"]+)['\"]",
            text, flags=re.S):
        if field not in _handler_arg_names(tool):
            problems.append(f"{tool}.{field}")
    assert not problems, (
        f"gateUnless names arguments these tools no longer take: {problems}")


def test_hidden_and_ordered_fields_are_real_tool_arguments():
    """`hide: ['clip_id']` and `order: [...]` name backend arguments. A stale
    name in either is a form that quietly shows the wrong fields."""
    text = _source()
    problems: list[str] = []
    for tool, key, body in re.findall(
            r"tool:\s*['\"]([^'\"]+)['\"](?:(?!tool:).)*?"
            r"\b(hide|order):\s*\[([^\]]*)\]",
            text, flags=re.S):
        known = _handler_arg_names(tool)
        for field in re.findall(r"['\"]([^'\"]+)['\"]", body):
            if field not in known:
                problems.append(f"{tool}.{key} -> {field}")
    assert not problems, (
        f"catalog.ts hide/order names arguments these tools no longer "
        f"take: {problems}")


@pytest.mark.skipif(not JOBS_TS.exists(), reason="mobile/lib/jobs.ts not present")
def test_async_tool_list_matches_the_backend():
    """A tool the backend runs as a job but the phone calls with `wait=1` pins
    a request worker for minutes — which is the concrete mechanism behind
    "the app froze". The two lists have to be the same list."""
    from video_ai_editor.main import ASYNC_DISPATCH_TOOLS
    text = JOBS_TS.read_text(encoding="utf-8")
    match = re.search(r"ASYNC_DISPATCH_TOOLS[^=]*=\s*[^\[]*\[([^\]]*)\]", text, re.S)
    assert match, "no ASYNC_DISPATCH_TOOLS array found in mobile/lib/jobs.ts"
    named = set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))
    assert named == set(ASYNC_DISPATCH_TOOLS), (
        f"mobile only: {sorted(named - set(ASYNC_DISPATCH_TOOLS))}; "
        f"backend only: {sorted(set(ASYNC_DISPATCH_TOOLS) - named)}")
