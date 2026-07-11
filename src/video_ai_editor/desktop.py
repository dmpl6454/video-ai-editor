"""Desktop launcher — boots uvicorn in a thread and opens a PyWebView window.

Usage:
    uv run python -m video_ai_editor.desktop
"""
from __future__ import annotations
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Absolute (not `from .`): the frozen PyInstaller EXE runs this file as the
# top-level `__main__` script, so `__package__` is unset and a relative import
# has no parent to anchor to. The package is bundled via collect_submodules, so
# the absolute name resolves in the EXE, under `-m`, and under pytest alike.
from video_ai_editor import platformutil as _pu
from video_ai_editor.storage import is_valid_session_id, session_dir, session_path


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


def _avfoundation_default_audio_index() -> str:
    """Probe `ffmpeg -f avfoundation -list_devices true -i ""` for the first
    listed audio input device index. avfoundation numbers audio devices
    independently of video devices (e.g. `[0] FaceTime HD Camera` under
    "video devices" and a *separate* `[0] MacBook Air Microphone` under
    "audio devices"), so index 0 is a reasonable default but not guaranteed —
    probing beats hardcoding. Falls back to "0" if parsing fails for any
    reason (missing ffmpeg build, unexpected output format, etc.)."""
    try:
        proc = subprocess.run(
            [_pu.FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except Exception:
        return "0"
    in_audio_section = False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if in_audio_section:
            m = re.search(r"\[(\d+)\]", line)
            if m:
                return m.group(1)
            break  # a non-matching line ends the audio-devices section
    return "0"


def _post_multipart_file(url: str, field_name: str, file_path: Path,
                         extra_fields: dict[str, str],
                         timeout: float = 30.0) -> dict:
    """Minimal stdlib multipart/form-data POST (no `requests` dependency —
    it isn't a declared project dependency, and PyInstaller's --exclude-module
    list for the packaged .app doesn't account for it).

    Uploads `file_path` under `field_name` plus each `extra_fields` entry as a
    plain form field, mirroring what a browser's FormData would send to the
    same /vo_record endpoint VoRecorder.tsx already posts to. Raises on any
    non-2xx response or connection error; caller handles/reports it."""
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    parts: list[bytes] = []
    for key, value in extra_fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
         f'filename="{file_path.name}"\r\nContent-Type: {content_type}\r\n\r\n').encode("utf-8")
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        import json as _json
        return _json.loads(resp.read().decode("utf-8"))


class _Api:
    """Bridge exposed to the frontend as `window.pywebview.api`.

    The packaged WKWebView/WebView2 window has no reliable way to surface an
    OS "Save As" dialog for a plain `<a download>` anchor click (unlike a real
    browser), so exports silently appear to do nothing. This bridge lets the
    frontend ask Python — which *can* drive a native file dialog via
    pywebview — to copy the already-rendered export out of the session's
    `exports/` dir to a user-chosen location instead.

    Also bridges native microphone capture (`vo_start`/`vo_stop`): pywebview's
    Cocoa WKWebView backend implements no media-capture permission delegate,
    AND the app is served over a non-TLS custom-port origin that WKWebView
    does not treat as a secure context — so `getUserMedia` cannot work in this
    window regardless of the Info.plist entitlement. These two methods bypass
    the browser media APIs entirely: Python shells out to ffmpeg's macOS
    `avfoundation` input device to record straight to a WAV file, then posts
    that WAV to the existing `/vo_record` endpoint itself (mirroring what
    VoRecorder.tsx's browser-dev path already does with a MediaRecorder blob),
    so both code paths land on the identical dispatch/commit machinery.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._rec_proc: subprocess.Popen | None = None
        self._rec_path: Path | None = None

    def vo_start(self, session_id: str) -> dict:
        """Begin a native mic recording for `session_id`. Non-blocking: spawns
        ffmpeg and returns immediately so the pywebview js_api call (which
        runs synchronously on the calling thread) doesn't block the UI for
        the whole recording. Returns {"ok": True} or {"ok": False, "error": ...}."""
        if not is_valid_session_id(session_id):
            return {"ok": False, "error": "invalid session id"}
        if self._rec_proc is not None and self._rec_proc.poll() is None:
            return {"ok": False, "error": "a recording is already in progress"}
        if not _pu.IS_MAC:
            # avfoundation is macOS-only; Windows/other platforms still rely
            # on getUserMedia (WebView2 has no equivalent secure-context/
            # capture-delegate gap — see CLAUDE.md's Windows section).
            return {"ok": False, "error": "native mic capture is only implemented on macOS"}

        vo_dir = session_dir(session_id) / "uploads" / "vo"
        vo_dir.mkdir(parents=True, exist_ok=True)
        out_path = vo_dir / f"native_rec_{uuid.uuid4().hex[:10]}.wav"
        audio_idx = _avfoundation_default_audio_index()
        try:
            proc = subprocess.Popen(
                [_pu.FFMPEG, "-y", "-f", "avfoundation", "-i", f":{audio_idx}", str(out_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except Exception as e:
            return {"ok": False, "error": f"could not start ffmpeg: {e}"}
        self._rec_proc = proc
        self._rec_path = out_path
        return {"ok": True}

    def vo_stop(self, session_id: str, start: float = 0.0, gain_db: float = 0.0) -> dict:
        """Stop the in-flight native recording, upload the resulting WAV to
        the same /vo_record endpoint the browser-dev MediaRecorder path uses,
        and return its response (typically {"clip_id": ...}) so the frontend
        can select/flash the new clip exactly like the getUserMedia path does."""
        proc, out_path = self._rec_proc, self._rec_path
        self._rec_proc = None
        self._rec_path = None
        if proc is None or out_path is None:
            return {"ok": False, "error": "no recording in progress"}

        if proc.poll() is None:
            # SIGINT is ffmpeg's documented graceful-stop signal — unlike
            # kill()/terminate() (SIGTERM), it lets ffmpeg finalize the WAV
            # header/trailer before exiting, so the file isn't truncated/torn.
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
        else:
            proc.communicate()

        if not out_path.exists() or out_path.stat().st_size == 0:
            return {"ok": False, "error": "recording produced no audio (mic may be unavailable or denied)"}

        url = f"http://{self._host}:{self._port}/api/sessions/{session_id}/vo_record"
        try:
            result = _post_multipart_file(
                url, "file", out_path,
                {"start": str(start), "gain_db": str(gain_db)},
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:800]
            return {"ok": False, "error": f"vo_record upload failed ({e.code}): {detail}"}
        except Exception as e:
            return {"ok": False, "error": f"vo_record upload failed: {e}"}
        finally:
            # Best-effort cleanup: vo_record's own handler stores its own
            # transcoded copy under the same uploads/vo dir; this raw capture
            # WAV was only ever a transfer artifact.
            _pu.unlink_with_retry(out_path)

        return {"ok": True, **result}

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
        js_api=_Api(host, port),
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
