"""The ``brain.enabled`` setting: the Editor Brain's surfaces, on or off (EB1).

  * OFF (the default until the brain's last wave lands) — the Prompt bar,
    the preview card, the right panel and the routes are exactly the 0.8.0
    ones: no Plan tab, no Versions strip, and every ``…/brain/*`` session
    route answers 404 ``brain_disabled``.
  * ON — a brain run's card opens on its Plan tab with the decisions and
    their reasons, the Changes tab carries a why per line, the Versions
    strip sits above History, and the brain routes answer.

Stored with the app settings (``settings.json``, read through
``api.pairing.load_settings`` — the same file and mtime cache the prompt and
preview settings use) under ``"brain": {"enabled": bool}`` — the row this
module adds to that file's shape:

    key             type   default  read by
    brain.enabled   bool   false    brain_setting.enabled(); GET/PUT /api/settings/brain

``VAI_BRAIN_ENABLED`` (``1``/``0``) overrides it for development, the test
harnesses and as the kill switch: ``VAI_BRAIN_ENABLED=0`` hides everything
whatever the file says. The frontend reads ``GET /api/settings/brain`` and
writes ``PUT`` (loopback, same-origin, JSON only — like every other
app-settings write; api/brain_routes.py).
"""
from __future__ import annotations

import os

DEFAULT_ENABLED = False
ENV_VAR = "VAI_BRAIN_ENABLED"
SECTION = "brain"
KEY = "enabled"
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


def enabled() -> tuple[bool, str]:
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
    section = data.get(SECTION) if isinstance(data, dict) else None
    stored = _parse(section.get(KEY)) if isinstance(section, dict) else None
    if stored is not None:
        return stored, "settings"
    return DEFAULT_ENABLED, "default"


def is_enabled() -> bool:
    return enabled()[0]


def set_enabled(value: object) -> bool:
    """Store the setting; returns the stored value. Only a real boolean is
    accepted (ValueError otherwise). Other keys are kept."""
    if not isinstance(value, bool):
        raise ValueError("brain.enabled must be true or false")
    from .api import pairing

    def apply(data: dict) -> dict:
        out = dict(data)
        section = out.get(SECTION)
        section = dict(section) if isinstance(section, dict) else {}
        section[KEY] = value
        out[SECTION] = section
        return out

    pairing._mutate(apply)
    return value


__all__ = ["DEFAULT_ENABLED", "ENV_VAR", "SECTION", "KEY", "enabled", "is_enabled", "set_enabled"]
