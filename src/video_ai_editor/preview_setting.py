"""The ``preview.engine`` setting: which preview the app runs (wave D).

Spec: docs/design/INSTANT_PREVIEW_SPEC.md §1 G6, §7, §12.

  * ``server`` — today's preview: the server renders ``preview.mp4`` and one
    ``<video>`` plays it. The DEFAULT for this milestone (1a), so shipping the
    server contracts changes nothing a user can see.
  * ``client`` — the instant-preview engine, whatever the capability probe
    says (development and the WK acceptance suites).
  * ``auto``  — client when the capabilities pass, else server (the default
    once Phase 1 ships).

Stored with the app settings (``settings.json`` next to the phone-pairing
state, read through ``api.pairing.load_settings`` — one file, one mtime
cache) under ``"preview": {"engine": ...}``. ``VAI_PREVIEW_ENGINE`` overrides
it for development and the harness. The frontend reads it at
``GET /api/settings/preview`` and writes it at ``PUT /api/settings/preview``
(``set_preview_engine``: validated to ``auto | client | server``, written
read-modify-write under the settings lock so a concurrent phone-pairing
write is never lost).

It also gates BACKGROUND work: eager proxy builds after an import run only
when the engine is not ``server`` (on-demand spans always work), so the
default build spends no CPU or disk on proxies nobody plays.
"""
from __future__ import annotations

import os

ENGINES: tuple[str, ...] = ("auto", "client", "server")
DEFAULT_ENGINE = "server"
ENV_VAR = "VAI_PREVIEW_ENGINE"


def _normalise(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in ENGINES else None


def preview_engine() -> tuple[str, str]:
    """(engine, source) where source is ``env`` | ``settings`` | ``default``.
    An unknown value anywhere falls through to the next source, never raises:
    a typo in settings.json must not change what the preview does."""
    env = _normalise(os.environ.get(ENV_VAR))
    if env:
        return env, "env"
    try:
        from .api import pairing
        data = pairing.load_settings()
    except Exception:
        data = {}
    section = data.get("preview") if isinstance(data, dict) else None
    stored = _normalise(section.get("engine")) if isinstance(section, dict) else None
    if stored:
        return stored, "settings"
    return DEFAULT_ENGINE, "default"


def set_preview_engine(value: object) -> str:
    """Store ``preview.engine`` in the app settings; returns the stored
    value. Raises ValueError for anything but ``auto | client | server``
    (exact names; surrounding space and case are forgiven, like the reader).
    Other keys of the ``preview`` section and of the file are kept."""
    engine = _normalise(value)
    if engine is None:
        raise ValueError(f"preview.engine must be one of {', '.join(ENGINES)}")
    from .api import pairing

    def apply(data: dict) -> dict:
        out = dict(data)
        section = out.get("preview")
        section = dict(section) if isinstance(section, dict) else {}
        section["engine"] = engine
        out["preview"] = section
        return out

    pairing._mutate(apply)
    return engine


def eager_proxies_enabled() -> bool:
    """Whether imports queue a full proxy build in the background."""
    flag = (os.environ.get("VAI_PROXY_EAGER") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return preview_engine()[0] != "server"


__all__ = ["ENGINES", "DEFAULT_ENGINE", "ENV_VAR", "preview_engine", "set_preview_engine",
           "eager_proxies_enabled"]
