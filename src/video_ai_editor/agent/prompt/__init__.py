"""Prompt Editor — plan, validate, execute and verify an edit from one sentence.

Package map (owner in brackets; see docs/design/PROMPT_EDITOR_SPEC.md §0.1):

  schema.py    [P]  Plan / Step / NeedsInput / Postcondition / IntentDraft,
                    PLAN_JSON_SCHEMA, TOOL_STAGE, DEFAULT_POSTCONDITIONS,
                    CHECK_SPECS — the shape every brain emits and the
                    executor consumes.
  facts.py     [P]  TimelineFacts — pure reads of the EDL/transcript in
                    TIMELINE seconds; the only input the planner sees.
  recipes.py   [P]  recipe names, slot names, cards() for the on-device
                    brains, from_intents() = IntentDraft → Plan.
  validate.py  [P]  validate_plan() — THE security boundary (§1.3).
  brains/      [B]  Brain protocol + the four brains and their router.
  executor.py  [X]  runs a validated Plan inside one EDLStore.batch().
  verify.py    [X]  postconditions measured on the EDL / render.
  service.py   [X]  the SSE turn, EVENT_TYPES, route table.

WHY this package imports nothing at package level: brains/ adapters and the
benchmark import `schema` from processes that must stay light (a Swift child
process wrapper, a TestClient that has not loaded ffmpeg). Import the module
you need explicitly — `from video_ai_editor.agent.prompt import schema`.
"""
from __future__ import annotations

#: Bumped only with the `$id` in schema.PLAN_JSON_SCHEMA (`vai://plan/<n>`).
PLAN_VERSION = 1

__all__ = ["PLAN_VERSION"]
