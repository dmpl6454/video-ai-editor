"""desktop.py is PyInstaller's ENTRY SCRIPT: the frozen .app runs it as a
top-level `__main__` with no `__package__`, so a relative import anywhere in
it raises ImportError in the shipped build only. 0.6.0 shipped two
`from .api import pairing` lines inside `_resolve_bind_host`/`main`; every
.app launch died with "attempted relative import with no known parent
package" while pytest and `python -m` — where the package IS the parent —
stayed green. These tests reproduce the frozen condition instead of trusting
the module header's comment.
"""
from __future__ import annotations

import ast
import runpy
from pathlib import Path

import video_ai_editor.desktop as _desktop_module

DESKTOP_PY = Path(_desktop_module.__file__)


def test_desktop_entry_script_has_no_relative_imports():
    tree = ast.parse(DESKTOP_PY.read_text(encoding="utf-8"))
    relative = [
        f"line {node.lineno}: from {'.' * node.level}{node.module or ''} import …"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert relative == [], (
        "desktop.py runs as PyInstaller's top-level script — a relative import "
        f"crashes the frozen app at launch: {relative}"
    )


def test_resolve_bind_host_works_when_run_as_a_top_level_script(monkeypatch):
    """Execute desktop.py exactly as the frozen EXE does — as a script with
    no package — and call the function that crashed 0.6.0. `run_name` is not
    `__main__`, so `main()` (which starts uvicorn) is not entered."""
    monkeypatch.setenv("VAE_HOST", "127.0.0.1")
    ns = runpy.run_path(str(DESKTOP_PY), run_name="desktop_as_script")
    assert ns.get("__package__") in (None, ""), "the test must model a package-less script"

    bind_host, public = ns["_resolve_bind_host"]("127.0.0.1")
    assert bind_host in {"127.0.0.1", "0.0.0.0"}
    assert isinstance(public, bool)

    # The shipped posture: the phone companion is temporarily gated off
    # (api/pairing.py::PHONE_PAIRING_ENABLED), and a build with no phone feature
    # must not be able to put itself on the network — not even when an operator
    # names a public address. Asserted through the frozen entry path because this
    # is the only code path the .app actually runs.
    monkeypatch.delenv("VAE_PHONE_PAIRING", raising=False)
    assert ns["_resolve_bind_host"]("10.0.0.5") == ("127.0.0.1", False)
    assert ns["_resolve_bind_host"]("0.0.0.0") == ("127.0.0.1", False)

    # With the feature back on, an explicit non-loopback host is honoured again
    # and always counts as public. Same function, same frozen import path.
    monkeypatch.setenv("VAE_PHONE_PAIRING", "1")
    assert ns["_resolve_bind_host"]("10.0.0.5") == ("10.0.0.5", True)
