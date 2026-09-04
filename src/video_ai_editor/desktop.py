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
from collections.abc import Callable
from html import escape as _escape
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


def _diag(msg: str) -> None:
    """Emit a startup diagnostic that survives a WINDOWED launch.

    `print(..., file=sys.stderr)` is not enough here. A windowed process — the
    macOS `--windowed` .app, `pythonw.exe`, or anything launched detached — gets
    `sys.stdout is None` and `sys.stderr is None`, and Python's `print` then
    SILENTLY DOES NOTHING (verified on 3.13: it returns without raising). So
    every diagnostic on this path, including "backend didn't start", was written
    into the void in exactly the builds where a user most needs it — which is why
    a reported windowed hang came with nothing to look at.

    Keep the print for the console case AND log to the rotating file.
    """
    print(msg, file=sys.stderr, flush=True)
    try:
        # ABSOLUTE import — this module is PyInstaller's entry script, so it runs
        # as top-level `__main__` with no `__package__`, and a `from .` form would
        # raise ImportError in the frozen app ONLY (invisible in every dev path).
        from video_ai_editor.api.hardening import get_logger
        get_logger().warning("[desktop] %s", msg)
    except Exception:
        pass


def _wait_for_server(url: str, timeout: float = 15.0,
                     abort: Callable[[], bool] | None = None) -> bool:
    """Poll `url` until it answers 200, or `timeout` elapses.

    `abort` is checked between attempts and short-circuits the wait. The only
    caller that passes one uses it for "the server thread already recorded a
    crash": no amount of further polling can make a dead uvicorn answer, and
    staring at a splash for the full startup timeout when the reason is
    already known is the same silent failure this path exists to remove.
    """
    end = time.time() + timeout
    while time.time() < end:
        if abort is not None and abort():
            return False
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def _frontend_is_stale(repo: Path, dist: Path) -> bool:
    """True when any frontend source is newer than the built bundle.

    A bare `dist/index.html` existence check was NOT enough: it let a bundle
    built weeks ago satisfy the guard forever, so `bash run.sh` silently served
    a stale UI. Three consecutive tester rounds re-reported bugs that were
    already fixed in source because the fixes never reached the bundle they
    were running (mute checkbox, text coordinates, PNG stickers, path labels…).

    Cheap mtime comparison — no git, no hashing — so it costs ~a few ms even on
    a large src tree, and it degrades to "not stale" if anything is unreadable
    rather than forcing a rebuild loop.
    """
    try:
        built = (dist / "index.html").stat().st_mtime
    except OSError:
        return True
    fe = repo / "frontend"
    watch = [fe / "package.json", fe / "package-lock.json", fe / "index.html",
             fe / "vite.config.ts", fe / "tsconfig.app.json"]
    try:
        for p in watch:
            if p.exists() and p.stat().st_mtime > built:
                return True
        for p in (fe / "src").rglob("*"):
            # Directory mtimes change on any add/remove inside them too, so we
            # deliberately do not filter to files only.
            if p.stat().st_mtime > built:
                return True
    except OSError:
        return False
    return False


def _ensure_frontend_built() -> None:
    """Make sure frontend/dist exists AND is not older than frontend/src.

    In a PyInstaller .app the frontend is bundled under sys._MEIPASS, so there
    is nothing to build (npm isn't available) — just return. In dev, build it
    on first run if missing, and rebuild it when sources have moved on.

    Failure policy differs by case on purpose: a MISSING bundle is fatal (there
    is nothing to serve), but a STALE bundle only warns — bricking a working
    launch because npm is unavailable would be worse than serving old bits.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        if (Path(meipass) / "frontend" / "dist" / "index.html").exists():
            return
        _diag("bundled frontend missing — rebuild the .app")
        sys.exit(1)
    repo = Path(__file__).resolve().parents[2]
    dist = repo / "frontend" / "dist"
    missing = not (dist.exists() and (dist / "index.html").exists())
    if missing:
        reason = "frontend/dist missing"
    elif _frontend_is_stale(repo, dist):
        reason = "frontend/dist is older than frontend/src"
    else:
        return
    print(f"[desktop] {reason} — running `npm run build`…", flush=True)
    import subprocess
    proc = subprocess.run(
        [_npm_cmd(), "run", "build"],
        cwd=str(repo / "frontend"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_pu.SUBPROCESS_FLAGS,
    )
    if proc.returncode != 0:
        _diag(f"npm build failed:\n{proc.stderr[-1500:]}")
        if missing:
            sys.exit(1)
        _diag("WARNING: serving the STALE bundle in frontend/dist. "
              "Fix the build or your UI will not match the source.")


# Set by _serve when uvicorn dies, read by the startup watcher. A crash (port
# already bound, a bad import) is knowable in ~1s, so the launcher must not sit
# out the whole startup timeout before saying anything.
_SERVER_ERROR: str | None = None


def _last_exception_line(traceback_text: str) -> str:
    """The trailing `Type: message` line of a traceback, sized for a window.

    That last line is the only part a user can act on ("Address already in
    use"); the frames above it belong in the log, which _diag already writes.
    """
    lines = [ln.strip() for ln in traceback_text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "unknown error"


def _serve(host: str, port: int) -> None:
    """Run uvicorn in this thread.

    This runs as a daemon thread with no caller-side try/except, so an
    exception here previously vanished silently: Python's default thread
    excepthook prints to sys.stderr, which is None in a windowed/frozen
    build (see _diag docstring) — the main thread would then just spin
    until the startup-wait timeout with zero diagnostic. Catch and route
    through _diag so a real import/bind failure is never indistinguishable
    from "still importing".
    """
    global _SERVER_ERROR
    try:
        import uvicorn
        # log_config=None is required, not optional, in a windowed/frozen
        # launch: uvicorn's default logging setup builds a DefaultFormatter
        # whose __init__ calls sys.stdout.isatty() to decide on colorizing,
        # and a genuine no-console Windows launch (a real double-click, NOT
        # one with stdio redirected for capture) gives the process
        # sys.stdout = None — so that call raises AttributeError, which
        # crashes this daemon thread before uvicorn ever binds the socket.
        # The app's own JSON/file logging (api/hardening.py) already covers
        # everything uvicorn's logging would, so skipping it here is free.
        uvicorn.run("video_ai_editor.main:app", host=host, port=port,
                    reload=False, log_level="warning", access_log=False,
                    log_config=None)
    except SystemExit as e:
        # The arm that actually fires in production. uvicorn does NOT raise on
        # the two failures a user hits — a port already in use, or a fatal
        # startup error — it LOGS and calls sys.exit(1). In a daemon thread
        # that raises SystemExit, which `except Exception` does not catch
        # (SystemExit derives from BaseException), so without this arm
        # _SERVER_ERROR stayed None, the watcher's abort predicate never
        # tripped, and the honest "why it failed" page was unreachable code:
        # the user waited out the whole startup timeout instead.
        _SERVER_ERROR = (
            f"the backend exited during startup (code {e.code}). "
            f"Another copy of Video AI Editor may already be running."
        )
        _diag(f"server thread exited via SystemExit(code={e.code})")
    except Exception:
        import traceback
        tb = traceback.format_exc()
        _SERVER_ERROR = _last_exception_line(tb)
        _diag("server thread crashed:\n" + tb)


def _avfoundation_default_audio_index() -> str:
    """Probe `ffmpeg -f avfoundation -list_devices true -i ""` for the best
    audio input device index. avfoundation numbers audio devices
    independently of video devices (e.g. `[0] FaceTime HD Camera` under
    "video devices" and a *separate* `[0] MacBook Air Microphone` under
    "audio devices"), so index 0 is a reasonable default but not guaranteed —
    probing beats hardcoding.

    ffmpeg prints the device list to stderr with a *non-zero* return code
    (it's a probe, not a real capture — there's no "-i \"\"" device to open),
    so we must NOT gate parsing on `proc.returncode == 0`; only a raised
    exception (ffmpeg missing entirely, timeout, etc.) falls back to "0".
    Within the "AVFoundation audio devices:" section, prefer a device whose
    name contains "microphone" (case-insensitive) — built-in/USB mics
    consistently self-report that word, whereas aggregate/virtual devices
    (e.g. "BlackHole", loopback devices) sort first on some Macs and would
    otherwise silently win by being merely first-in-list. Falls back to the
    first audio device found, then to "0" if the section is missing/empty."""
    try:
        proc = subprocess.run(
            [_pu.FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
            **_pu.SUBPROCESS_FLAGS,
        )
    except Exception:
        return "0"
    in_audio_section = False
    first_audio_idx: str | None = None
    for line in proc.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_audio_section = False
            continue
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if not in_audio_section:
            continue
        m = re.search(r"\[(\d+)\]\s*(.*)$", line)
        if not m:
            # A non-matching line while inside the section ends it (ffmpeg's
            # device dump has no other content interleaved).
            if first_audio_idx is not None or line.strip():
                break
            continue
        idx, name = m.group(1), m.group(2)
        if first_audio_idx is None:
            first_audio_idx = idx
        if "microphone" in name.lower():
            return idx
    return first_audio_idx if first_audio_idx is not None else "0"


def _ensure_mic_authorized_mac() -> tuple[bool, str]:
    """Request macOS mic authorization from the APP process (via AVFoundation)
    so TCC attributes the prompt to this bundle, and the ffmpeg subprocess it
    then spawns inherits the granted permission.

    This is the fix for the classic "works in code, silently denied in the
    packaged app" failure mode: under an ad-hoc-signed / non-hardened-runtime
    bundle, TCC's attribution of a *subprocess's* mic request (ffmpeg via
    avfoundation) is unreliable and can be denied with zero prompt and zero
    error — it just produces an empty/silent WAV. Requesting authorization
    here, from the long-lived app process itself, via AVCaptureDevice, makes
    TCC show (or have already recorded) the decision against "Video AI
    Editor" specifically, and the child ffmpeg process inherits that grant
    since it's spawned by (and shares the responsible-process attribution
    of) this same app.

    Returns (authorized, detail). If pyobjc's AVFoundation bridge is
    unavailable for any reason, degrades to (True, ...) — i.e. don't block
    recording on this pre-check; fall through to the subprocess-level prompt/
    denial as before, which is strictly the pre-existing behavior."""
    try:
        import AVFoundation
    except Exception as e:
        return True, f"AVFoundation unavailable ({e}); relying on subprocess prompt"

    AVMediaTypeAudio = "soun"
    try:
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    except Exception as e:
        return True, f"AVFoundation authorization check failed ({e}); relying on subprocess prompt"

    # AVAuthorizationStatus: 0 notDetermined, 1 restricted, 2 denied, 3 authorized.
    if status == 3:
        return True, "already authorized"
    if status in (1, 2):
        return False, ("microphone access denied — enable it in System Settings "
                        "› Privacy & Security › Microphone, then relaunch the app")

    # notDetermined: request synchronously. The completion handler fires on an
    # arbitrary AVFoundation-internal queue, not necessarily this thread, so
    # block on a threading.Event rather than assuming same-thread delivery.
    result: dict[str, bool] = {"granted": False}
    done = threading.Event()

    def _cb(granted: bool) -> None:
        result["granted"] = bool(granted)
        done.set()

    try:
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, _cb
        )
    except Exception as e:
        return True, f"AVFoundation request failed ({e}); relying on subprocess prompt"

    if not done.wait(timeout=30):
        return False, "microphone permission prompt timed out waiting for a response"
    return (result["granted"],
            "granted" if result["granted"] else "user dismissed or denied the mic prompt")


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


# Characters no mainstream filesystem accepts in a leaf, minus the separators
# (those are split off first, below). The Windows set is the strict superset and
# is applied on both platforms, so the same project proposes the same filename
# on either OS.
_UNSAFE_LEAF_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def _suggested_save_name(disk_name: str, suggested: str | None) -> str:
    """The name the native Save dialog proposes.

    `disk_name` is the export's real leaf under `exports/` — today
    `export_<edl hash>.mp4`, which means nothing to a user and is byte-identical
    every time the same timeline is exported, so two exports of two different
    cuts can land on the same proposed filename. The backend now sends a human
    one alongside it; nothing about WHERE the file is written changes, only what
    the dialog offers.

    Falls back to `disk_name` whenever the suggestion is missing (an older
    backend that doesn't send one), empty, or sanitises down to nothing: a
    dialog proposing a hash is far better than one that fails to open.

    The EXTENSION always comes from `disk_name`. The export is .mp4 or .mov and
    the bytes are what they are — a suggestion carrying a different container
    (or none at all) must not mislabel the saved file.
    """
    ext = Path(disk_name).suffix
    if not isinstance(suggested, str):
        return disk_name
    # Take the last path segment first (both separators, on both platforms):
    # stripping the separators instead would fuse "a/b.mp4" into "ab.mp4".
    leaf = re.split(r"[\\/]", suggested)[-1]
    stem = _UNSAFE_LEAF_CHARS.sub("", leaf).strip()
    # Drop a trailing container extension — the disk one is re-applied below.
    # An allowlist, not "any short suffix": a title legitimately ending in
    # ".2026" or ".v2" must keep it rather than lose a piece of its name.
    for known in (ext, ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"):
        if known and stem.lower().endswith(known.lower()):
            stem = stem[: -len(known)]
            break
    # Leading dots would make it a hidden file; trailing ones are stripped by
    # Windows anyway.
    stem = stem.strip().strip(".").strip()
    # APFS/HFS+ and NTFS cap a leaf at 255 *bytes*, and a name derived from a
    # project title can be non-ASCII, so truncate the encoded form.
    stem = stem.encode("utf-8")[:200].decode("utf-8", "ignore").strip()
    return (stem + ext) if stem else disk_name


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
            return {"ok": False, "unsupported": True,
                    "error": "native mic capture is only implemented on macOS"}

        # Trigger the TCC prompt from THIS process before spawning ffmpeg —
        # see _ensure_mic_authorized_mac's docstring for why this is the
        # single highest-leverage fix for the packaged-app mic-denial bug.
        authorized, detail = _ensure_mic_authorized_mac()
        if not authorized:
            return {"ok": False, "error": detail}

        vo_dir = session_dir(session_id) / "uploads" / "vo"
        vo_dir.mkdir(parents=True, exist_ok=True)
        out_path = vo_dir / f"native_rec_{uuid.uuid4().hex[:10]}.wav"
        audio_idx = _avfoundation_default_audio_index()
        try:
            proc = subprocess.Popen(
                [_pu.FFMPEG, "-y", "-f", "avfoundation", "-i", f":{audio_idx}", str(out_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **_pu.SUBPROCESS_FLAGS,
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

        stderr_tail = ""
        if proc.poll() is None:
            # SIGINT is ffmpeg's documented graceful-stop signal — unlike
            # kill()/terminate() (SIGTERM), it lets ffmpeg finalize the WAV
            # header/trailer before exiting, so the file isn't truncated/torn.
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                _, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, err = proc.communicate(timeout=5)
        else:
            _, err = proc.communicate()
        try:
            stderr_tail = (err or b"").decode("utf-8", errors="replace")[-800:]
        except Exception:
            stderr_tail = ""

        if not out_path.exists() or out_path.stat().st_size == 0:
            # Distinguish a TCC denial from a device-index mismatch from a
            # genuine no-audio device by surfacing ffmpeg's own stderr tail —
            # a bare "recording produced no audio" is indistinguishable
            # across all three causes and gives the user nothing to act on.
            base = "recording produced no audio (mic may be unavailable or denied)"
            if stderr_tail.strip():
                return {"ok": False, "error": f"{base}\nffmpeg said: {stderr_tail.strip()}"}
            return {"ok": False, "error": base}

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

    def save_export(self, session_id: str, filename: str,
                    suggested_name: str | None = None) -> str | None:
        """Copy an exported file to a user-chosen location via the native
        save dialog. Returns the chosen destination path, or None if the
        session/file is invalid or the user cancelled the dialog.

        `filename` LOCATES the file: it is the export's real leaf in the
        session's `exports/` dir. `suggested_name` is only what the dialog
        proposes — the export response carries it so the user is offered
        something like "beach-clip-2026-09-04.mp4" instead of the edl hash.
        It is optional in both directions: an older frontend calls this with
        two arguments and gets exactly the old behaviour, and a backend that
        sends no name falls back to the hash rather than failing.
        """
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
            webview.FileDialog.SAVE,
            save_filename=_suggested_save_name(filename, suggested_name),
        )
        if not dest:
            return None
        dest_path = dest if isinstance(dest, str) else dest[0]
        shutil.copy2(src, dest_path)
        # macOS Spotlight's `com.apple.metadata.mdflagwriter` daemon marks some
        # freshly-created files with UF_HIDDEN within ~1s (the same daemon behind
        # this repo's `.pth` hidden-flag gotcha — see CLAUDE.md). When it hits a
        # just-copied export, Finder renders the file GRAYED OUT and it reads as
        # missing even though the bytes are perfect. Proactively clear the hidden
        # flag so exports always appear normally. macOS-only; best-effort.
        if _pu.IS_MAC:
            try:
                subprocess.run(["chflags", "nohidden", dest_path], **_pu.SUBPROCESS_FLAGS)
            except Exception:
                pass
        # On a case-insensitive volume (default APFS/HFS+), writing "DemoVid.mp4"
        # over a pre-existing "demovid.mp4" overwrites it IN PLACE and keeps the
        # old on-disk case — so the path we were handed (and would toast) can
        # differ in case from what Finder/Spotlight actually show, reading as
        # "the file isn't where it said". Resolve the true stored leaf by
        # matching the parent dir case-insensitively (realpath alone won't do it:
        # on a case-insensitive volume it returns whatever case you asked for),
        # so the reported path always matches what the user sees on disk.
        dp = Path(dest_path)
        try:
            for entry in dp.parent.iterdir():
                if entry.name.lower() == dp.name.lower():
                    dest_path = str(entry)
                    break
        except OSError:
            pass
        # Take the user straight to the saved file so they never have to hunt for
        # it in a crowded Downloads folder (the exported video is easy to lose
        # among dozens of files). Best-effort: a reveal failure must NEVER undo
        # the save that already succeeded, so swallow every error and still
        # return the path. macOS: `open -R` selects it in Finder; Windows:
        # `explorer /select,` selects it in Explorer.
        try:
            if _pu.IS_MAC:
                subprocess.run(["open", "-R", dest_path], **_pu.SUBPROCESS_FLAGS)
            elif _pu.IS_WINDOWS:
                subprocess.run(["explorer", f"/select,{dest_path}"], **_pu.SUBPROCESS_FLAGS)
        except Exception:
            pass
        return dest_path


# Window chrome for everything shown BEFORE the editor loads. Kept in step with
# frontend/src/styles.css (--bg-0 / --text / --text-dim): these pages live in the
# same window the editor then loads into, so a mismatched background is a visible
# flash at hand-off — and pywebview's own default is white.
_BG = "#0e0e10"
_FG = "#e6e6eb"
_FG_DIM = "#9b9ba5"

# How long to wait for the backend before putting ANY window on screen. A warm
# start answers in ~1.5s, so the ordinary case still goes straight to the editor
# with no splash at all and behaves exactly as it did before; only a slow start
# (35s measured on the notarized 0.5.0 DMG's first launch) gets the splash —
# which is the case where the old behaviour was a bouncing Dock icon and nothing
# else for half a minute.
_SPLASH_AFTER_S = 2.0
# Past the startup timeout we keep checking quietly. A Mac slower than the build
# box can finish a cold start late, and healing into the editor beats making the
# user quit and relaunch. Bounded so the thread cannot outlive a real failure.
_LATE_RETRY_FOR_S = 600.0
_LATE_RETRY_EVERY_S = 2.0


def _status_page(headline: str, lines: list[str], *, busy: bool) -> str:
    """A self-contained page for the window while there is no editor to show.

    Everything is inline on purpose: this is displayed precisely when
    `frontend/dist` is NOT being served yet, so a single external stylesheet,
    font or image reference would render as a broken box at the exact moment
    the user is deciding whether the app is working.

    The content fades in on a delay, so a backend that answers a moment after
    the window opens hands over to the editor before any of this is legible —
    a fast launch shows the app's own background colour and nothing more.
    """
    body = "\n  ".join(f"<p>{_escape(ln)}</p>" for ln in lines)
    spinner = '<div class="spin"></div>' if busy else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Video AI Editor</title><style>
  html,body{{height:100%;margin:0;background:{_BG};color:{_FG};
    font:13px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI','Inter',sans-serif;
    -webkit-font-smoothing:antialiased;-webkit-user-select:none;cursor:default}}
  .wrap{{height:100%;display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:14px;padding:0 48px;text-align:center;
    animation:fade .35s ease .4s both}}
  h1{{font-size:15px;font-weight:600;margin:0;letter-spacing:.2px}}
  /* Body is user-select:none so the window feels native, but the failure page
     prints the app.log path — the one actionable string on it — and a user
     being told to look at a file they cannot copy is a dead end. */
  p{{margin:0;max-width:520px;color:{_FG_DIM};
    -webkit-user-select:text;user-select:text;cursor:text}}
  .spin{{width:18px;height:18px;border-radius:50%;border:2px solid #ffffff22;
    border-top-color:{_FG_DIM};animation:rot .8s linear infinite}}
  @keyframes rot{{to{{transform:rotate(360deg)}}}}
  @keyframes fade{{from{{opacity:0}}to{{opacity:1}}}}
</style></head><body><div class="wrap">
  {spinner}
  <h1>{_escape(headline)}</h1>
  {body}
</div></body></html>"""


def _splash_html() -> str:
    return _status_page("Starting Video AI Editor", [
        "Warming up the video engine. The first launch after installing takes "
        "the longest — later ones are quick.",
    ], busy=True)


def _startup_failed_html(url: str, timeout: float, error: str | None) -> str:
    """What the window says when the backend never answered.

    Exiting silently here (the old behaviour) is indistinguishable from the app
    refusing to launch, and it happens on exactly the machines nobody tested on
    — slower than the build box. Everything below is something the recipient of
    a DMG can actually do.
    """
    log = _pu.user_data_dir("Video AI Editor") / "logs" / "app.log"
    lines = []
    if error:
        lines.append(f"The engine stopped with: {error}")
    else:
        lines.append(f"The engine did not answer on {url} within "
                     f"{timeout:.0f} seconds.")
    lines.append("If another copy of Video AI Editor is already open, quit it "
                 "and open this one again.")
    lines.append(f"Details are in {log}")
    if not error:
        # Only honest while the watcher is still polling — a crashed server
        # thread never comes back, so that branch does not make the promise.
        lines.append("Still checking — the editor will open here by itself if "
                     "the engine answers.")
    return _status_page("The editor could not start", lines, busy=False)


def _open_editor_when_ready(window, url: str, health_url: str,
                            timeout: float) -> None:
    """Hold the splash until the backend answers, then hand the window over.

    Deliberately a daemon thread of our own rather than `webview.start(func=…)`:
    pywebview starts that one non-daemon, so a user who gives up and closes the
    splash would keep a process alive until the poll finished.

    `Window.load_url`/`load_html` are safe from here — both wait on the window's
    `shown` event and marshal to the UI thread (AppHelper.callAfter on Cocoa),
    and `load_url` only clears the pending-event flags, so the Windows
    accelerator-key handler bound to `events.loaded` survives the hand-off and
    fires again for the editor.
    """
    try:
        if _wait_for_server(health_url, timeout=timeout,
                            abort=lambda: _SERVER_ERROR is not None):
            window.load_url(url)
            return
        err = _SERVER_ERROR
        _diag((f"backend crashed before serving {url}: {err}" if err else
               f"backend didn't answer on {url} within {timeout:.0f}s")
              + " — the window is showing the failure page.")
        window.load_html(_startup_failed_html(url, timeout, err))
        if err:
            return  # the server thread is gone; polling it cannot help
        deadline = time.time() + _LATE_RETRY_FOR_S
        while time.time() < deadline:
            time.sleep(_LATE_RETRY_EVERY_S)
            if _SERVER_ERROR:
                return
            if _wait_for_server(health_url, timeout=1.0):
                window.load_url(url)
                return
    except Exception:
        import traceback
        _diag("startup hand-off failed:\n" + traceback.format_exc())


def main() -> None:
    _ensure_frontend_built()
    host = os.environ.get("VAE_HOST", "127.0.0.1")
    port = int(os.environ.get("VAE_PORT", "8765"))
    url = f"http://{host}:{port}"

    server_thread = threading.Thread(target=_serve, args=(host, port), daemon=True)
    server_thread.start()
    # The frozen bundle's first cold import (torch et al.) can exceed the old
    # hard-coded 15s default on a loaded Windows box, so the launcher gave up
    # before uvicorn bound (E2E report ISSUE-05). Default to 60s; overridable.
    startup_timeout = float(os.environ.get("VAE_STARTUP_TIMEOUT", "60"))
    health_url = f"{url}/api/health"

    import webview
    # Give the backend a moment to answer BEFORE creating the window: a warm
    # start wins that race and opens straight into the editor, exactly as it
    # always did. Only a slow start gets a window it has something to say in —
    # previously it got no window at all, then (past the timeout) a process
    # that exited without a word, which reads as "the app won't launch".
    ready = _wait_for_server(health_url, timeout=_SPLASH_AFTER_S)
    window = webview.create_window(
        title="Video AI Editor",
        url=url if ready else None,
        html=None if ready else _splash_html(),
        width=1480, height=920,
        min_size=(1100, 700),
        easy_drag=False,
        background_color=_BG,
        js_api=_Api(host, port),
    )
    if not ready:
        # Spend the rest of the caller's budget waiting, then say so on screen.
        threading.Thread(
            target=_open_editor_when_ready,
            args=(window, url, health_url,
                  max(1.0, startup_timeout - _SPLASH_AFTER_S)),
            daemon=True,
        ).start()
    if _pu.IS_WINDOWS:
        # WebView2 honors browser accelerator keys by default, so F5 reloads
        # the editor mid-session and Ctrl+F/Ctrl+P/Ctrl+W open find/print/
        # close — none are app shortcuts. Disable them (and Ctrl+wheel page
        # zoom, which fights the timeline's Ctrl+wheel zoom). The native-tree
        # traversal varies across pywebview versions → degrade gracefully.
        #
        # `window.events.loaded` fires on a pywebview-internal worker thread
        # ("Thread-4", verified via py-spy), NOT the WinForms UI/STA thread
        # that owns the CoreWebView2 control. Touching `.CoreWebView2` (and
        # its .Settings) directly from that worker thread deadlocks — the
        # pythonnet/COM marshaling call blocks forever without releasing the
        # GIL, which freezes the ENTIRE interpreter (including the unrelated
        # uvicorn/asyncio server thread), reproducing as "app not responding"
        # + backend unreachable, moments after every launch. pywebview's own
        # BrowserForm methods (_show, _toggle, etc.) all avoid this the same
        # way: marshal onto the UI thread via Form.Invoke before touching any
        # native control. Do the same here instead of calling cross-thread.
        def _harden_webview(w=window):
            def _apply():
                try:
                    core = w.native.Controls[0].CoreWebView2
                    core.Settings.AreBrowserAcceleratorKeysEnabled = False
                    core.Settings.IsZoomControlEnabled = False
                except Exception:
                    pass
            # CoreWebView2 and the WinForms Invoke marshaling are WINDOWS-ONLY.
            # On macOS pywebview uses a Cocoa WKWebView, `System` (pythonnet)
            # isn't installed, and there is no accelerator-key problem to fix.
            # Explicit rather than relying on the ImportError below to no-op:
            # a silent exception is indistinguishable from a real failure, and
            # per CLAUDE.md OS branches belong behind platformutil.
            if not _pu.IS_WINDOWS:
                return
            try:
                from System import Func, Type
                if w.native.InvokeRequired:
                    w.native.Invoke(Func[Type](_apply))
                else:
                    _apply()
            except Exception:
                pass
        window.events.loaded += _harden_webview
    try:
        webview.start()
    except Exception as e:  # WebView2 Runtime missing / init failure on Windows
        if _pu.IS_WINDOWS:
            _diag("Could not start the WebView2 window. Install the "
                  "Microsoft Edge WebView2 Runtime (Evergreen) from "
                  "https://developer.microsoft.com/microsoft-edge/webview2/ "
                  f"and relaunch.\n  Underlying error: {e}")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
