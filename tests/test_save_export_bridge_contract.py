"""The native save bridge is a contract split across two languages.

`desktop.py::_Api.save_export` is called from `frontend/src/store.ts` over
pywebview's `js_api`. Nothing type-checks that call: Python sees a method,
TypeScript sees a hand-written `interface PywebviewBridge`, and the two only
meet at runtime inside the packaged app — the one place neither `pytest`,
`tsc -b` nor `vitest` runs.

That gap already cost a fix. The human export filename was threaded from
`main.py`'s `suggested_filename` through the store's `triggerDownload(...)`
and into a `save_export` that accepted it — but the call site still passed
only two arguments, so the pretty name worked in browser-dev (the `<a download>`
path) and was silently dropped in the packaged app, which is the ONLY place the
native Save dialog exists and therefore the only place the fix was for. Every
suite stayed green.

These tests pin both ends and, deliberately, the wiring between them.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from video_ai_editor import desktop

STORE_TS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"


def test_save_export_accepts_a_suggested_name():
    params = [p for p in inspect.signature(desktop._Api.save_export).parameters
              if p != "self"]
    assert params[:2] == ["session_id", "filename"], params
    assert len(params) >= 3, (
        "save_export lost its suggested-name parameter; the native Save dialog "
        "would go back to proposing export_<hash>.mp4"
    )


def test_the_suggested_name_is_optional():
    """An older frontend bundle must keep working against a newer app.

    The two halves ship in one bundle today, but `frontend/dist` is rebuilt
    independently and a stale one has shipped before (CLAUDE.md, Release
    identity), so a 2-argument call must not raise.
    """
    sig = inspect.signature(desktop._Api.save_export)
    third = [p for n, p in sig.parameters.items() if n != "self"][2]
    assert third.default is not inspect.Parameter.empty, (
        f"{third.name} must default, or a 2-arg call from an older bundle raises"
    )


def test_store_ts_actually_passes_the_third_argument():
    """The half that was wrong, and that no other gate can see.

    Asserted against the source text because there is no runtime in this suite
    where the TS call and the Python method meet.
    """
    src = STORE_TS.read_text(encoding="utf-8")
    call = re.search(r"api\.save_export\(([^)]*)\)", src)
    assert call, "store.ts no longer calls save_export at all"
    args = [a.strip() for a in call.group(1).split(",")]
    assert len(args) >= 3, (
        f"store.ts calls save_export with {len(args)} args ({args}); the "
        "suggested filename is dropped and the packaged app falls back to the "
        "hash name"
    )


def test_store_ts_bridge_type_declares_the_third_argument():
    """The TS interface is hand-written, so it can drift from the call it types
    and from the Python signature independently."""
    src = STORE_TS.read_text(encoding="utf-8")
    decl = re.search(r"save_export\?:\s*\(([^)]*)\)", src)
    assert decl, "the PywebviewBridge save_export type is gone"
    assert "suggested" in decl.group(1), (
        "the bridge interface still types save_export as 2-arg, so passing a "
        "third would not type-check: " + decl.group(1)
    )
