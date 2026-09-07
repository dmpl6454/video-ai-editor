"""Tests for desktop.py's cold-start window and the export Save-dialog name.

The launcher used to create no window at all until /api/health answered and to
call sys.exit(1) with nothing on screen past VAE_STARTUP_TIMEOUT — a silent
failure to launch on any Mac slower than the build box. These cover the pure
helpers and the hand-off state machine with pywebview stubbed out (there is no
GUI toolkit in CI); the one thing they cannot cover — how the real WKWebView
window looks during the transition — has to be watched on a packaged launch.
"""
from __future__ import annotations


# --------------------------------------------------------------------------
# _suggested_save_name — what the native Save dialog proposes (G4)
# --------------------------------------------------------------------------

def test_no_suggestion_keeps_the_on_disk_hash_name():
    """An older backend sends no name; the dialog must still open."""
    from video_ai_editor import desktop
    assert desktop._suggested_save_name("export_ab12cd34.mp4", None) == "export_ab12cd34.mp4"
    assert desktop._suggested_save_name("export_ab12cd34.mp4", "") == "export_ab12cd34.mp4"
    assert desktop._suggested_save_name("export_ab12cd34.mp4", "   ") == "export_ab12cd34.mp4"
    # A non-string arriving over the JS bridge must not raise either.
    assert desktop._suggested_save_name("export_ab12cd34.mp4", 17) == "export_ab12cd34.mp4"


def test_human_suggestion_is_used_verbatim():
    from video_ai_editor import desktop
    assert desktop._suggested_save_name(
        "export_ab12cd34.mp4", "beach-clip-2026-09-04.mp4") == "beach-clip-2026-09-04.mp4"


def test_extension_always_comes_from_the_file_on_disk():
    """The bytes are whatever the container was; the name must not lie."""
    from video_ai_editor import desktop
    assert desktop._suggested_save_name("export_x.mov", "beach clip.mp4") == "beach clip.mov"
    assert desktop._suggested_save_name("export_x.mp4", "beach clip") == "beach clip.mp4"
    # …but a dot inside the name is part of the name, not an extension.
    assert desktop._suggested_save_name("export_x.mp4", "summer.2026") == "summer.2026.mp4"


def test_suggestion_is_reduced_to_a_safe_leaf():
    from video_ai_editor import desktop
    assert desktop._suggested_save_name("export_x.mp4", "../../etc/passwd") == "passwd.mp4"
    assert desktop._suggested_save_name("export_x.mp4", r"C:\tmp\cut.mp4") == "cut.mp4"
    assert desktop._suggested_save_name("export_x.mp4", 'a<b>:c|d?e*f') == "abcdef.mp4"
    # A suggestion that sanitises down to nothing falls back, never returns ".mp4".
    assert desktop._suggested_save_name("export_x.mp4", ".mp4") == "export_x.mp4"
    assert desktop._suggested_save_name("export_x.mp4", "///") == "export_x.mp4"


def test_suggestion_fits_a_filesystem_leaf():
    """Truncation is on BYTES: a title can be non-ASCII and the cap is 255."""
    from video_ai_editor import desktop
    out = desktop._suggested_save_name("export_x.mp4", "ø" * 400)
    assert len(out.encode("utf-8")) < 255
    assert out.endswith(".mp4")


# --------------------------------------------------------------------------
# _last_exception_line — what a crashed server thread gets to say (G3)
# --------------------------------------------------------------------------

def test_last_exception_line_is_the_actionable_one():
    from video_ai_editor import desktop
    tb = ("Traceback (most recent call last):\n"
          "  File \"x.py\", line 1, in <module>\n"
          "OSError: [Errno 48] Address already in use\n")
    assert desktop._last_exception_line(tb) == "OSError: [Errno 48] Address already in use"
    assert desktop._last_exception_line("") == "unknown error"
    assert len(desktop._last_exception_line("E: " + "x" * 900)) <= 200


# --------------------------------------------------------------------------
# _wait_for_server's abort predicate
# --------------------------------------------------------------------------

def test_wait_for_server_gives_up_early_when_abort_fires():
    """A dead uvicorn cannot start answering, so don't sit out the timeout."""
    import time
    from video_ai_editor import desktop
    t0 = time.time()
    assert desktop._wait_for_server("http://127.0.0.1:1/api/health",
                                    timeout=30.0, abort=lambda: True) is False
    assert time.time() - t0 < 1.0


# --------------------------------------------------------------------------
# _open_editor_when_ready — the splash → editor (or → explanation) hand-off
# --------------------------------------------------------------------------

class _FakeWindow:
    def __init__(self):
        self.urls: list[str] = []
        self.html: list[str] = []

    def load_url(self, url):
        self.urls.append(url)

    def load_html(self, html):
        self.html.append(html)


def test_backend_arriving_late_loads_the_editor(monkeypatch):
    from video_ai_editor import desktop
    monkeypatch.setattr(desktop, "_SERVER_ERROR", None)
    monkeypatch.setattr(desktop, "_wait_for_server", lambda *a, **k: True)
    win = _FakeWindow()
    desktop._open_editor_when_ready(win, "http://127.0.0.1:8765",
                                    "http://127.0.0.1:8765/api/health", 5.0)
    assert win.urls == ["http://127.0.0.1:8765"]
    assert win.html == []


def test_a_crashed_backend_explains_itself_and_stops_polling(monkeypatch):
    from video_ai_editor import desktop
    monkeypatch.setattr(desktop, "_SERVER_ERROR",
                        "OSError: [Errno 48] Address already in use")
    calls = {"n": 0}

    def _never(*a, **k):
        calls["n"] += 1
        return False

    monkeypatch.setattr(desktop, "_wait_for_server", _never)
    monkeypatch.setattr(desktop, "_LATE_RETRY_EVERY_S", 0.01)
    win = _FakeWindow()
    desktop._open_editor_when_ready(win, "http://127.0.0.1:8765",
                                    "http://127.0.0.1:8765/api/health", 5.0)
    assert win.urls == []
    assert len(win.html) == 1
    assert "Address already in use" in win.html[0]
    # Exactly one attempt: a dead server thread never comes back, so the page
    # must not promise "still checking" nor keep a retry loop running.
    assert calls["n"] == 1
    assert "Still checking" not in win.html[0]


def test_a_slow_backend_gets_the_failure_page_then_heals_into_the_editor(monkeypatch):
    """The whole point of the late retry: a Mac slower than the build box."""
    from video_ai_editor import desktop
    monkeypatch.setattr(desktop, "_SERVER_ERROR", None)
    answers = iter([False, False, True])
    monkeypatch.setattr(desktop, "_wait_for_server", lambda *a, **k: next(answers))
    monkeypatch.setattr(desktop, "_LATE_RETRY_EVERY_S", 0.01)
    win = _FakeWindow()
    desktop._open_editor_when_ready(win, "http://127.0.0.1:8765",
                                    "http://127.0.0.1:8765/api/health", 5.0)
    assert len(win.html) == 1
    assert "Still checking" in win.html[0]
    assert win.urls == ["http://127.0.0.1:8765"]


def test_handoff_never_raises_out_of_its_thread(monkeypatch):
    """It runs on a daemon thread with no caller — an exception here would be
    invisible in a windowed build (sys.stderr is None), so it must be caught."""
    from video_ai_editor import desktop
    monkeypatch.setattr(desktop, "_SERVER_ERROR", None)
    monkeypatch.setattr(desktop, "_wait_for_server", lambda *a, **k: True)

    class _Broken(_FakeWindow):
        def load_url(self, url):
            raise RuntimeError("window is gone")

    diagnosed: list[str] = []
    monkeypatch.setattr(desktop, "_diag", lambda m: diagnosed.append(m))
    desktop._open_editor_when_ready(_Broken(), "http://127.0.0.1:8765",
                                    "http://127.0.0.1:8765/api/health", 5.0)
    assert diagnosed and "window is gone" in diagnosed[-1]


# --------------------------------------------------------------------------
# The status pages themselves
# --------------------------------------------------------------------------

def test_status_pages_are_self_contained():
    """These are shown precisely when frontend/dist is NOT being served, so a
    single external reference would render as a broken box at the worst moment."""
    from video_ai_editor import desktop
    for page in (desktop._splash_html(),
                 desktop._startup_failed_html("http://127.0.0.1:8765", 60.0, None),
                 desktop._startup_failed_html("http://127.0.0.1:8765", 60.0, "boom")):
        assert page.startswith("<!doctype html>")
        for forbidden in ("<link", "<script", "<img", "src=", "href=", "@import"):
            assert forbidden not in page.lower(), forbidden


def test_status_page_escapes_its_content():
    from video_ai_editor import desktop
    page = desktop._status_page("a <b> & c", ["x <script>y</script>"], busy=False)
    assert "<script>" not in page
    assert "&lt;b&gt;" in page


def test_failure_page_points_at_the_log_and_the_port_conflict():
    from video_ai_editor import desktop
    page = desktop._startup_failed_html("http://127.0.0.1:8765", 60.0, None)
    assert "app.log" in page
    assert "quit it" in page          # another copy already holding the port
    assert "60 seconds" in page
