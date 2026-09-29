"""The ``prompt.confirm_before_apply`` setting: "Ask before applying Prompt
bar edits" (0.8.0, Preview then apply).

  * ON (the default) — a plan from an on-device brain (Recipes, Apple
    Intelligence, the local model) is dry-run and shown as a card; nothing
    changes until the person presses Apply (agent/prompt/preview.py).
  * OFF — the pre-0.8.0 behaviour: the plan runs at once. The K3 safety net
    still judges every run; only the card is skipped.

The Claude cloud brain and the chat assistant with a key are never
previewed (owner decision). Stored with the app settings (``settings.json``,
read through ``api.pairing.load_settings`` — the same file and mtime cache
``preview_setting`` uses) under ``"prompt": {"confirm_before_apply": bool}``.
``VAI_PROMPT_CONFIRM`` (``1``/``0``) overrides it for development and test
harnesses: the existing prompt suites test PLAN semantics, so their harness
sets it to ``0`` (tests/conftest.py) and the preview suites turn it on. The
frontend reads ``GET /api/settings/prompt`` and writes ``PUT`` (loopback,
same-origin, JSON only — like every other app-settings write).
"""
from __future__ import annotations

import os

DEFAULT_CONFIRM = True
ENV_VAR = "VAI_PROMPT_CONFIRM"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _parse(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return None


def confirm_before_apply() -> tuple[bool, str]:
    """(on, source) where source is ``env`` | ``settings`` | ``default``. An
    unreadable value anywhere falls through to the next source."""
    env = _parse(os.environ.get(ENV_VAR))
    if env is not None:
        return env, "env"
    try:
        from .api import pairing
        data = pairing.load_settings()
    except Exception:  # noqa: BLE001 — a broken settings file must not change behaviour
        data = {}
    section = data.get("prompt") if isinstance(data, dict) else None
    stored = _parse(section.get("confirm_before_apply")) if isinstance(section, dict) else None
    if stored is not None:
        return stored, "settings"
    return DEFAULT_CONFIRM, "default"


def set_confirm_before_apply(value: object) -> bool:
    """Store the setting; returns the stored value. Only a real boolean is
    accepted (ValueError otherwise). Other keys are kept."""
    if not isinstance(value, bool):
        raise ValueError("prompt.confirm_before_apply must be true or false")
    from .api import pairing

    def apply(data: dict) -> dict:
        out = dict(data)
        section = out.get("prompt")
        section = dict(section) if isinstance(section, dict) else {}
        section["confirm_before_apply"] = value
        out["prompt"] = section
        return out

    pairing._mutate(apply)
    return value


__all__ = ["DEFAULT_CONFIRM", "ENV_VAR", "confirm_before_apply", "set_confirm_before_apply"]
