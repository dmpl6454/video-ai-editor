"""Desktop launcher — boots uvicorn in a thread and opens a PyWebView window.

Usage:
    uv run python -m video_ai_editor.desktop
"""
from __future__ import annotations
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Absolute (not `from .`): the frozen PyInstaller EXE runs this file as the
# top-level `__main__` script, so `__package__` is unset and a relative import
# has no parent to anchor to. The package is bundled via collect_submodules, so
# the absolute name resolves in the EXE, under `-m`, and under pytest alike.
from video_ai_editor import platformutil as _pu
from video_ai_editor.storage import is_valid_session_id, session_path


def _npm_cmd() -> str:
    """Resolve the npm launcher. On Windows npm is npm.cmd (a batch file), so a
    bare 'npm' FileNotFounds. Try the platform-suffixed names, then fall back to
    the bare name (subprocess PATHEXT may still find it)."""
    candidates = ["npm.cmd", "npm"] if _pu.IS_WINDOWS else ["npm"]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return candidates[0]


def _wait_for_server(url: str, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def _ensure_frontend_built() -> None:
    """Make sure frontend/dist exists.

    In a PyInstaller .app the frontend is bundled under sys._MEIPASS, so there
    is nothing to build (npm isn't available) — just return. In dev, build it
    on first run if missing."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        if (Path(meipass) / "frontend" / "dist" / "index.html").exists():
            return
        print("[desktop] bundled frontend missing — rebuild the .app", file=sys.stderr)
        sys.exit(1)
    repo = Path(__file__).resolve().parents[2]
    dist = repo / "frontend" / "dist"
    if dist.exists() and (dist / "index.html").exists():
        return
    print("[desktop] frontend/dist missing — running `npm run build`…", flush=True)
    import subprocess
    proc = subprocess.run(
        [_npm_cmd(), "run", "build"],
        cwd=str(repo / "frontend"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("[desktop] npm build failed:\n", proc.stderr[-1500:], file=sys.stderr)
        sys.exit(1)


def _serve(host: str, port: int) -> None:
    """Run uvicorn in this thread."""
    import uvicorn
    # Disable reload in desktop mode; users get fresh bits next launch.
    uvicorn.run("video_ai_editor.main:app", host=host, port=port,
                reload=False, log_level="warning", access_log=False)


class _Api:
    """Bridge exposed to the frontend as `window.pywebview.api`.

    The packaged WKWebView/WebView2 window has no reliable way to surface an
    OS "Save As" dialog for a plain `<a download>` anchor click (unlike a real
    browser), so exports silently appear to do nothing. This bridge lets the
    frontend ask Python — which *can* drive a native file dialog via
    pywebview — to copy the already-rendered export out of the session's
    `exports/` dir to a user-chosen location instead.
    """

    def save_export(self, session_id: str, filename: str) -> str | None:
        """Copy an exported file to a user-chosen location via the native
        save dialog. Returns the chosen destination path, or None if the
        session/file is invalid or the user cancelled the dialog."""
        if not is_valid_session_id(session_id):
            return None
        # Reject any filename that isn't a bare leaf (e.g. "../../etc/passwd")
        # before it ever touches the filesystem — the same belt-and-suspenders
        # posture as storage.delete_session's path-traversal guard.
        if not filename or Path(filename).name != filename:
            return None
        src = session_path(session_id) / "exports" / filename
        if not src.exists():
            return None
        import webview  # lazy: mirrors main()'s import, keeps this module
                         # importable (e.g. under pytest) without a GUI toolkit
        win = webview.windows[0]
        dest = win.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=filename,
        )
        if not dest:
            return None
        dest_path = dest if isinstance(dest, str) else dest[0]
        shutil.copy2(src, dest_path)
        return dest_path


def main() -> None:
    _ensure_frontend_built()
    host = os.environ.get("VAE_HOST", "127.0.0.1")
    port = int(os.environ.get("VAE_PORT", "8765"))
    url = f"http://{host}:{port}"

    server_thread = threading.Thread(target=_serve, args=(host, port), daemon=True)
    server_thread.start()
    if not _wait_for_server(f"{url}/api/health"):
        print(f"[desktop] backend didn't start on {url}", file=sys.stderr)
        sys.exit(1)

    import webview
    webview.create_window(
        title="Video AI Editor",
        url=url,
        width=1480, height=920,
        min_size=(1100, 700),
        easy_drag=False,
        js_api=_Api(),
    )
    try:
        webview.start()
    except Exception as e:  # WebView2 Runtime missing / init failure on Windows
        if _pu.IS_WINDOWS:
            print("[desktop] Could not start the WebView2 window. Install the "
                  "Microsoft Edge WebView2 Runtime (Evergreen) from "
                  "https://developer.microsoft.com/microsoft-edge/webview2/ "
                  f"and relaunch.\n  Underlying error: {e}", file=sys.stderr)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
