"""`docs/PROMPT_EDITOR.md` and CLAUDE.md describe the same Prompt bar.

0.8.0 added preview-then-apply (a key-free plan changes nothing until Apply;
`agent/prompt/preview.py`, `prompt_setting.py`). CLAUDE.md's Prompt Editor
section documented it the day it landed; the user-facing doc did not, so the
two disagreed about what happens after you press Enter. This pins the doc to
the code and to CLAUDE.md: the setting's exact label and environment variable
are the ones the app uses, the doc names Apply and Change, and every module
CLAUDE.md credits for the feature is named in the doc's "Where things live".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from video_ai_editor import prompt_setting

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "PROMPT_EDITOR.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
SETTING_TS = ROOT / "frontend" / "src" / "lib" / "promptApplySetting.ts"

# The modules CLAUDE.md's "Preview, then apply" paragraph credits.
PREVIEW_MODULES = ("preview.py", "changes.py", "change_rules.py", "change_words.py", "prompt_setting.py")


def _frontend_label() -> str:
    m = re.search(r"PROMPT_APPLY_LABEL = '([^']+)'", SETTING_TS.read_text(encoding="utf-8"))
    assert m, "promptApplySetting.ts must export PROMPT_APPLY_LABEL"
    return m.group(1)


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claude_md() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


def test_doc_has_a_preview_then_apply_section(doc: str):
    assert re.search(r"^## .*[Pp]review.*[Aa]pply", doc, re.M), \
        "docs/PROMPT_EDITOR.md needs a '## Preview, then apply' section"


def test_doc_names_the_setting_exactly_as_the_app_does(doc: str):
    label = _frontend_label()
    assert label in doc, f"the doc must name the switch as the app does: {label!r}"
    assert prompt_setting.ENV_VAR in doc, f"the doc must name {prompt_setting.ENV_VAR}"
    default = "on" if prompt_setting.DEFAULT_CONFIRM else "off"
    assert re.search(rf"\b{default} by default\b", doc, re.I), \
        f"the doc must say the setting is {default} by default"


def test_doc_names_apply_and_change(doc: str):
    assert re.search(r"\*\*Apply\*\*", doc), "the doc must describe the Apply button"
    assert re.search(r"\*\*Change\*\*", doc), "the doc must describe the Change button"
    assert "Nothing has changed yet" in doc, "the card's own words belong in the doc"


def test_doc_and_claude_md_credit_the_same_modules(doc: str, claude_md: str):
    for mod in PREVIEW_MODULES:
        assert mod in claude_md, f"CLAUDE.md no longer names {mod}; update PREVIEW_MODULES"
        assert mod in doc, f"docs/PROMPT_EDITOR.md must name {mod} where things live"


def test_doc_and_claude_md_agree_on_when_a_preview_happens(doc: str, claude_md: str):
    # CLAUDE.md: the card appears only for a plan whose brain is not claude.
    assert "brain is not `claude`" in claude_md
    assert re.search(r"Claude.*(never|not) previewed|never previewed", doc, re.S), \
        "the doc must say the Claude rung / a keyed chat is never previewed"
    # Both say what a stale card does: re-plan, never a stale apply.
    assert "never a stale apply" in claude_md
    assert re.search(r"fresh (card|preview)", doc), "the doc must say a stale card is re-planned into a fresh one"
    # Both say the safety net still runs with the setting off.
    assert "the net still runs" in claude_md
    assert re.search(r"safety net still", doc), "the doc must say the safety net still judges every run when the switch is off"
