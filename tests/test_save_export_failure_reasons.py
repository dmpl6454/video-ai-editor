"""A cancel and a failure must not look the same to the user.

`_Api.save_export` returned `str | None` for FIVE different outcomes — invalid
session id, a filename that is not a bare leaf, the source file not existing, a
failed copy, and the user cancelling the dialog — and `store.ts` reads any falsy
result as "cancelled" and deliberately shows nothing. So the notarized DMG's
headline report, "no save dialog appears on export", arrived with no toast, no
error and no log line to work from. (Its cause was the window driving a FOREIGN
backend, so the rendered file sat in another process's workdir and the
`src.exists()` guard rejected it — see tests/test_desktop_port_identity.py.)

`save_export_result` answers WHICH outcome happened. The old method stays as a
thin wrapper, because `frontend/dist` is rebuilt independently of the app and a
stale bundle has shipped before (CLAUDE.md, Release identity) — so both
directions of the bridge have to keep working:

  older store.ts -> new bridge   : `save_export` still returns a path or None
  this store.ts  -> older bridge : `save_export_result` is absent, fall back

The `store.ts` half is asserted against the source text for the same reason
tests/test_save_export_bridge_contract.py is: pytest sees Python, `tsc -b` sees
a hand-written interface, and the two meet only inside the frozen app.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from video_ai_editor import desktop, storage

STORE_TS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"
SID = "s_abc123"


class _Dialog:
    """A stand-in for pywebview's native Save dialog."""

    def __init__(self, answer, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls: list[dict] = []

    def create_file_dialog(self, kind, save_filename=None):
        self.calls.append({"kind": kind, "save_filename": save_filename})
        if self.raises is not None:
            raise self.raises
        return self.answer


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """An `_Api` over a real session dir on disk, with `webview` stubbed and
    every side effect (Finder reveal, chflags) neutered."""
    monkeypatch.setattr(storage, "WORKDIR", tmp_path)
    exports = tmp_path / SID / "exports"
    exports.mkdir(parents=True)
    (exports / "export_abcd1234.mp4").write_bytes(b"\x00mp4-bytes")

    logged: list[str] = []
    monkeypatch.setattr(desktop, "_diag", lambda m: logged.append(m))
    # `open -R` / `chflags` / `explorer /select,` are best-effort niceties that
    # must not run in a test (they would open a Finder window per assertion).
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)

    def _install(dialog: _Dialog):
        mod = types.ModuleType("webview")
        mod.windows = [dialog]                                   # type: ignore[attr-defined]
        mod.FileDialog = types.SimpleNamespace(SAVE="save")      # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "webview", mod)
        return dialog

    return types.SimpleNamespace(
        api=desktop._Api("127.0.0.1", 8765),
        install=_install,
        exports=exports,
        tmp=tmp_path,
        logged=logged,
    )


# --------------------------------------------------------------------------
# success and cancel — the two outcomes that already worked
# --------------------------------------------------------------------------

def test_a_real_save_reports_the_path_and_writes_the_bytes(bridge):
    dest = bridge.tmp / "Movies" / "beach clip.mp4"
    dest.parent.mkdir()
    bridge.install(_Dialog(str(dest)))
    res = bridge.api.save_export_result(SID, "export_abcd1234.mp4", "beach clip")
    assert res["ok"] is True
    assert res["path"] == str(dest)
    assert dest.read_bytes() == b"\x00mp4-bytes"


def test_the_dialog_is_offered_the_human_name(bridge):
    dest = bridge.tmp / "cut.mp4"
    dlg = bridge.install(_Dialog(str(dest)))
    bridge.api.save_export_result(SID, "export_abcd1234.mp4", "beach clip")
    assert dlg.calls[0]["save_filename"] == "beach clip.mp4"


def test_a_cancel_stays_silent(bridge):
    """The one outcome that must NOT produce a message. `""` is what pywebview
    returns for a dismissed dialog; `None` covers the other backends."""
    for answer in ("", None, ()):
        bridge.install(_Dialog(answer))
        res = bridge.api.save_export_result(SID, "export_abcd1234.mp4", None)
        assert res == {"ok": False, "cancelled": True}, answer
        assert "error" not in res


# --------------------------------------------------------------------------
# the four failures that used to be indistinguishable from a cancel
# --------------------------------------------------------------------------

# (reason, args) for every non-cancel rejection the bridge can decide on its
# own — i.e. before the dialog is even opened. `dialog_failed`/`copy_failed`
# need their own stubs and have a test each below.
_REJECTIONS = [
    ("bad_session", ("../../etc", "export_abcd1234.mp4")),
    ("bad_filename", (SID, "../../../etc/passwd")),
    ("missing_file", (SID, "export_deadbeef.mp4")),
]


def test_every_rejection_names_its_reason_and_says_something_actionable(bridge):
    bridge.install(_Dialog("/nowhere/x.mp4"))
    for expected, args in _REJECTIONS:
        res = bridge.api.save_export_result(*args)
        assert res["ok"] is False, expected
        assert res.get("cancelled") is not True, (
            f"{expected} is being reported as a cancel — the exact conflation "
            "that made the shipped export bug invisible")
        assert res["reason"] == expected, res
        assert res["error"] and res["error"][0].isupper() and res["error"].endswith("."), res


def test_a_missing_export_is_diagnosed_with_the_path_it_looked_in(bridge):
    """The path is the diagnosis — it names the workdir this process is using,
    which is what would have identified the foreign-backend bug in one line."""
    bridge.install(_Dialog("/nowhere/x.mp4"))
    res = bridge.api.save_export_result(SID, "export_deadbeef.mp4", None)
    assert "export_deadbeef.mp4" in res["error"]
    assert any(str(bridge.exports) in ln for ln in bridge.logged), bridge.logged


def test_every_rejection_reaches_app_log(bridge):
    """Which guard rejected has to be answerable after the fact: this bridge
    runs in a windowed process where stdout/stderr are None."""
    bridge.install(_Dialog("/nowhere/x.mp4"))
    for expected, args in _REJECTIONS:
        bridge.logged.clear()
        bridge.api.save_export_result(*args)
        assert bridge.logged, f"{expected} logged nothing"
        assert any("save_export" in ln for ln in bridge.logged), bridge.logged


def test_a_traversal_filename_never_reaches_the_dialog(bridge):
    dlg = bridge.install(_Dialog("/nowhere/x.mp4"))
    bridge.api.save_export_result(SID, "../../../etc/passwd", None)
    bridge.api.save_export_result("../../etc", "export_abcd1234.mp4", None)
    bridge.api.save_export_result(SID, "export_deadbeef.mp4", None)
    assert dlg.calls == []


def test_a_dialog_that_cannot_open_is_reported(bridge):
    """"No save dialog appeared" must never again be a silent outcome."""
    bridge.install(_Dialog(None, raises=RuntimeError("no window")))
    res = bridge.api.save_export_result(SID, "export_abcd1234.mp4", None)
    assert res["reason"] == "dialog_failed"
    assert "no window" in res["error"]
    assert res.get("cancelled") is not True


def test_a_copy_that_fails_is_reported_not_raised(bridge):
    """This used to raise THROUGH the js_api bridge, which store.ts caught and
    answered with an `<a download>` click plus "Export complete — downloading…"
    — a false success in a window where that anchor downloads nothing."""
    bridge.install(_Dialog(str(bridge.tmp / "no-such-dir" / "cut.mp4")))
    res = bridge.api.save_export_result(SID, "export_abcd1234.mp4", None)
    assert res["ok"] is False
    assert res["reason"] == "copy_failed"
    assert res.get("cancelled") is not True


# --------------------------------------------------------------------------
# backward compatibility, both directions
# --------------------------------------------------------------------------

def test_the_legacy_method_still_returns_a_path_or_none(bridge):
    """An older `frontend/dist` calls `save_export` and reads a string."""
    dest = bridge.tmp / "cut.mp4"
    bridge.install(_Dialog(str(dest)))
    assert bridge.api.save_export(SID, "export_abcd1234.mp4", "cut") == str(dest)
    # …including the two-argument call shape it was written against.
    assert bridge.api.save_export(SID, "export_abcd1234.mp4") == str(dest)


def test_every_failure_is_falsy_through_the_legacy_method(bridge):
    """The load-bearing half of backward compatibility: an older bundle prints
    `Saved to ${result}` for ANY truthy return, so a structured error object
    reaching it would read as "Saved to [object Object]" — worse than the
    silence being fixed. The legacy shape must stay strictly str-or-None."""
    bridge.install(_Dialog(""))
    assert bridge.api.save_export(SID, "export_abcd1234.mp4") is None  # cancel
    for reason, args in _REJECTIONS:
        assert bridge.api.save_export(*args) is None, reason


def test_the_structured_method_takes_the_same_three_arguments():
    import inspect
    sig = inspect.signature(desktop._Api.save_export_result)
    names = [n for n in sig.parameters if n != "self"]
    assert names[:2] == ["session_id", "filename"], names
    assert sig.parameters[names[2]].default is not inspect.Parameter.empty, (
        "the suggested name must default, or a 2-arg call from an older bundle "
        "raises inside the bridge")


# --------------------------------------------------------------------------
# the store.ts half — no other gate can see this wiring
# --------------------------------------------------------------------------

def _store_src() -> str:
    return STORE_TS.read_text(encoding="utf-8")


def test_store_ts_calls_the_structured_bridge_with_all_three_arguments():
    src = _store_src()
    call = re.search(r"api\.save_export_result\(([^)]*)\)", src)
    assert call, "store.ts does not call save_export_result, so it still cannot "\
                 "tell a failure from a cancel"
    assert len([a for a in call.group(1).split(",") if a.strip()]) == 3, call.group(1)


def test_store_ts_types_the_structured_bridge():
    """The interface is hand-written, so it drifts independently of the call."""
    decl = re.search(r"save_export_result\?:\s*\(([^)]*)\)", _store_src())
    assert decl, "the PywebviewBridge type has no save_export_result"
    assert "suggested" in decl.group(1), decl.group(1)


def test_store_ts_keeps_the_legacy_call_for_an_older_bridge():
    """This bundle can meet an app that only has `save_export`."""
    src = _store_src()
    assert re.search(r"api\.save_export\(([^)]*)\)", src), (
        "the fallback for an older bridge is gone; the packaged app would then "
        "silently do nothing on export")


def test_store_ts_stays_silent_on_a_cancel_and_speaks_up_otherwise():
    src = _store_src()
    body = src[src.index("async function triggerDownload"):]
    assert "if (r.cancelled) return" in body, (
        "a cancel must return with no toast — a message every time someone "
        "changes their mind is noise")
    assert "toast.error(r.error" in body, (
        "a reported failure must reach the user; the reason exists precisely "
        "so the toast can be acted on")
    # And the false-success path is gone: a thrown bridge call must not fall
    # through to the `<a download>` + "Export complete" toast.
    catch = body[body.index("} catch (e) {"):]
    assert "toast.error" in catch.split("}")[0] + catch[:400]
