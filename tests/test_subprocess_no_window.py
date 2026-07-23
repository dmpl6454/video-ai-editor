"""Static invariant: every subprocess call site in src/video_ai_editor must
suppress the Windows console window.

On Windows, a windowed parent process (frozen exe built with console=False, or
pythonw) spawning a child console process (ffmpeg/ffprobe/whisper-cli/...)
pops up a visible terminal window for every task unless the call passes
``creationflags=subprocess.CREATE_NO_WINDOW``. The codebase's chosen shape is
``**_pu.SUBPROCESS_FLAGS`` (a dict that is ``{"creationflags":
subprocess.CREATE_NO_WINDOW}`` on Windows and ``{}`` elsewhere, so macOS
behavior is byte-identical).

This test parses every .py file under src/video_ai_editor with ast and asserts
that every ``subprocess.run/Popen/check_output/check_call/call`` call (including
module aliases like ``import subprocess as sp`` and ``from subprocess import
run``) carries either:

  * a ``**...SUBPROCESS_FLAGS`` dict-spread kwarg, or
  * an explicit ``creationflags=`` kwarg.

New call sites fail this test until they add the flags — the invariant is
pinned forever. tests/ call sites are deliberately NOT checked (tests run
headless in CI; not user-facing).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "video_ai_editor"

SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _collect_subprocess_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (module_aliases, bare_function_names) for subprocess in this file.

    module_aliases: names bound to the subprocess module itself
                    (``import subprocess`` -> "subprocess";
                     ``import subprocess as sp`` -> "sp").
    bare_function_names: names bound directly to a subprocess function
                    (``from subprocess import run`` -> "run";
                     ``from subprocess import run as _run`` -> "_run").
    """
    module_aliases: set[str] = set()
    bare_funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in SUBPROCESS_FUNCS:
                        bare_funcs.add(alias.asname or alias.name)
    return module_aliases, bare_funcs


def _call_name(node: ast.Call, module_aliases: set[str], bare_funcs: set[str]) -> str | None:
    """Return a display name if this Call targets a subprocess spawn fn, else None."""
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in SUBPROCESS_FUNCS
        and isinstance(func.value, ast.Name)
        and func.value.id in module_aliases
    ):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name) and func.id in bare_funcs:
        return func.id
    return None


def _has_no_window_flags(node: ast.Call) -> bool:
    """True if the call carries **SUBPROCESS_FLAGS or an explicit creationflags=."""
    for kw in node.keywords:
        if kw.arg == "creationflags":
            return True
        if kw.arg is None:  # a **spread kwarg
            # Accept any spread whose expression mentions SUBPROCESS_FLAGS:
            # _pu.SUBPROCESS_FLAGS, platformutil.SUBPROCESS_FLAGS, SUBPROCESS_FLAGS.
            if "SUBPROCESS_FLAGS" in ast.dump(kw.value):
                return True
    return False


def test_every_subprocess_call_suppresses_windows_console() -> None:
    assert SRC_ROOT.is_dir(), f"source root missing: {SRC_ROOT}"

    offenders: list[str] = []
    total_calls = 0

    for path in sorted(SRC_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module_aliases, bare_funcs = _collect_subprocess_aliases(tree)
        if not module_aliases and not bare_funcs:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node, module_aliases, bare_funcs)
            if name is None:
                continue
            total_calls += 1
            if not _has_no_window_flags(node):
                rel = path.relative_to(SRC_ROOT.parents[1])
                offenders.append(f"{rel}:{node.lineno} ({name})")

    assert total_calls > 0, (
        "sanity check failed: found zero subprocess call sites under "
        f"{SRC_ROOT} — the scanner is broken"
    )
    assert not offenders, (
        "subprocess call sites missing **_pu.SUBPROCESS_FLAGS (or an explicit "
        "creationflags=) — on Windows each of these pops a console window when "
        "the app runs windowed:\n  " + "\n  ".join(offenders)
    )


def test_subprocess_flags_shape() -> None:
    """SUBPROCESS_FLAGS must exist, be a dict, and be empty off-Windows /
    carry CREATE_NO_WINDOW on Windows."""
    import subprocess

    from video_ai_editor import platformutil as _pu

    assert isinstance(_pu.SUBPROCESS_FLAGS, dict)
    if _pu.IS_WINDOWS:
        assert _pu.SUBPROCESS_FLAGS == {
            "creationflags": subprocess.CREATE_NO_WINDOW
        }
    else:
        # macOS/Linux: empty spread — behavior byte-identical to before.
        assert _pu.SUBPROCESS_FLAGS == {}
