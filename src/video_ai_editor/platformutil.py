"""Cross-platform helpers. The ONE place OS differences live.

macOS and Windows both import from here; every OS-conditional decision in the
codebase should route through a function in this module rather than an inline
`sys.platform` check, so platform behavior stays auditable and testable.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePath, PureWindowsPath

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Spread into every subprocess.run/Popen/check_output/check_call as
# `**_pu.SUBPROCESS_FLAGS`. On Windows, a windowed parent (frozen exe built
# with console=False, or pythonw) spawning a console child (ffmpeg/ffprobe/
# whisper-cli/...) pops up a visible terminal window for every task unless the
# call passes creationflags=subprocess.CREATE_NO_WINDOW. On macOS/Linux this is
# an empty dict, so the spread is a no-op and behavior is byte-identical.
#
# NOTE: the dict-spread raises TypeError if a call site ALSO passes its own
# creationflags= kwarg (duplicate keyword). No site does today — a future site
# that needs extra creation flags must drop the spread and OR the flag in
# manually: creationflags=subprocess.CREATE_NO_WINDOW | <extra> (guarded for
# Windows, since CREATE_NO_WINDOW only exists there).
# tests/test_subprocess_no_window.py statically enforces that every subprocess
# call site under src/video_ai_editor carries one of the two forms.
SUBPROCESS_FLAGS: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
)


#: Spread instead of SUBPROCESS_FLAGS for BACKGROUND work that must yield the
#: CPU to the interactive app (wave D preview proxies): Windows gets the
#: below-normal priority class as well as the no-window flag; POSIX gets `{}`
#: and is lowered through `low_priority_argv` instead.
LOW_PRIORITY_SUBPROCESS_FLAGS: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS}
    if IS_WINDOWS else {}
)


def low_priority_argv(argv: list[str], niceness: int = 10) -> list[str]:
    """`argv` run at a lower CPU priority. POSIX prefixes `nice -n <niceness>`
    (which EXECs the command, so the child pid is the tool itself and killing
    it needs nothing special); Windows returns `argv` unchanged and relies on
    LOW_PRIORITY_SUBPROCESS_FLAGS. `preexec_fn=os.nice` is not used: it is
    unsafe in a process with threads, and every caller of this runs on one."""
    if IS_WINDOWS:
        return list(argv)
    nice = shutil.which("nice")
    return [nice, "-n", str(int(niceness)), *argv] if nice else list(argv)


def exe_name(name: str) -> str:
    """Append `.exe` on Windows for a bare binary name (idempotent)."""
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


# Resolved once at import. Bare names are fine when on PATH; exe_name makes the
# Windows form explicit so callers can also feed these to find_binary.
FFMPEG = exe_name("ffmpeg")
FFPROBE = exe_name("ffprobe")


def ffmpeg_filter_path(path: Path | str) -> str:
    """Escape a filesystem path for embedding inside an ffmpeg *filtergraph*
    option value (e.g. `vidstabdetect=result=<here>`, `sendcmd=f=<here>`,
    `movie=filename=<here>`).

    This is NOT the same as passing a path as an ffmpeg `-i` argv element (that
    needs no escaping). Inside a filtergraph, `:` separates filter options and
    `\\` is an escape char, so a raw Windows path like `C:\\Users\\x\\a.trf`
    is mangled by the parser. The robust, empirically-verified form is:
      1. Convert `\\` to `/` — ffmpeg accepts forward slashes on Windows, which
         removes every backslash-as-escape hazard.
      2. Escape each remaining `:` (the drive-letter colon) as `\\\\:` — the
         only escaping that survives ffmpeg's two-pass filtergraph parser
         (single-backslash and single-quoting both fail).
    On POSIX a normal path has no backslashes and no colon, so it passes
    through unchanged (a rare stray colon is still escaped defensively).

    Final QA (0.8.0): that was only the colon. ffmpeg parses such a value at
    TWO levels — the option value (`\\ ' :`) and then the filtergraph
    (`\\ ' [ ] , ;`) — so a LUT named "Tom's grade, v2.cube" lost its quote
    and was split at the comma, and every preview and export failed with a
    message blaming the clip. Each level is now escaped in turn (verified
    against ffmpeg for lut3d= and movie=filename=); the drive colon comes
    out exactly as before (`\\\\:`)."""
    s = str(path).replace("\\", "/")
    level1 = _FILTER_OPTION_SPECIALS.sub(r"\\\1", s)
    return _FILTER_GRAPH_SPECIALS.sub(r"\\\1", level1)


#: Characters `ffmpeg_filter_path` escapes at each parser level.
_FILTER_OPTION_SPECIALS = re.compile(r"([\\':])")
_FILTER_GRAPH_SPECIALS = re.compile(r"([\\'\[\],;])")


def find_binary(name: str, extra_dirs: list[Path]) -> str | None:
    """Locate a native binary cross-platform.

    1. `shutil.which(exe_name(name))` — respects PATH, adds `.exe` on Windows.
    2. Each dir in `extra_dirs` (both `name` and `exe_name(name)`).
    Returns the resolved path string, or None if nowhere found.
    """
    found = shutil.which(exe_name(name))
    if found:
        return found
    for d in extra_dirs:
        for cand in (Path(d) / exe_name(name), Path(d) / name):
            if cand.exists():
                return str(cand)
    return None


def user_data_dir(app_name: str) -> Path:
    """Per-OS writable application data directory.

    Windows: %APPDATA%\\<app_name>            (roaming; falls back to ~/AppData/Roaming)
    macOS:   ~/Library/Application Support/<app_name>
    Other:   ~/.local/share/<app_name>        (XDG)
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_name
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / app_name
    return Path.home() / ".local" / "share" / app_name


def user_cache_dir(app_name: str) -> Path:
    """Per-OS cache directory (regenerable data).

    Windows: %LOCALAPPDATA%\\<app_name>\\cache
    macOS:   ~/Library/Caches/<app_name>
    Other:   ~/.cache/<app_name>
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / app_name / "cache"
    if IS_MAC:
        return Path.home() / "Library" / "Caches" / app_name
    return Path.home() / ".cache" / app_name


def read_text_utf8(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def read_text_config(path: Path | str) -> str:
    """Read a small hand-editable config file, tolerating a UTF-8 BOM.

    For files a WINDOWS user or a Windows build script may have written:
    `.env`, `VERSION`, `BUILD_ID`. Notepad and PowerShell 5.1's
    `Set-Content -Encoding utf8` both prepend a BOM (`EF BB BF`), and neither
    `read_text(encoding="utf-8")` nor `.strip()` removes it — a BOM is not
    whitespace. The leading `\\ufeff` then lands *inside* the first value:

      - `.env` -> the first key parses as `\\ufeffANTHROPIC_API_KEY`, so the real
        key is never set and the app reports it missing while the file plainly
        contains it (chat pane silently disabled).
      - `BUILD_ID` -> the version badge and `/api/version` report a sha that is
        not byte-equal to any git object, defeating the release-identity
        mechanism whose whole purpose is making "which build?" answerable.

    `utf-8-sig` decodes BOM-less UTF-8 byte-for-byte identically, so this is
    strictly more tolerant than `read_text_utf8` — never different. It is a
    separate helper rather than a change to `read_text_utf8` because that one is
    used for media/EDL/sidecar reads where silently eating a leading `\\ufeff`
    would be a content change, not a fix.
    """
    return Path(path).read_text(encoding="utf-8-sig")


def write_text_utf8(path: Path | str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def part_path(dst: Path) -> Path:
    """A unique sibling temp path for an in-progress write of `dst`.

    ffmpeg's `-y` truncates its output to 0 bytes and writes progressively, so
    pointing it straight at the final path means a concurrent reader — or a
    process killed mid-write — sees a torn file. Worse for content-addressed
    caches: the final name IS the cache key, so a truncated file becomes a
    permanently-valid-looking cache hit. Write here, then swap in with
    `replace_with_retry`.

    ffmpeg terminated by SIGTERM (or by a Windows console close, which its
    CtrlHandler maps to SIGTERM) flushes and writes a COMPLETE trailer, so the
    partial file is fully decodable and no "is it valid?" check can spot it —
    staging is the only reliable defence.

    PID + thread id keep concurrent writers of the same destination from
    clobbering each other. The suffix MUST be preserved: ffmpeg picks its muxer
    from the output path's extension, so a `.mov` staged as `.part.mp4` would be
    muxed as MP4 and then merely renamed.
    """
    return dst.with_name(
        f".{dst.stem}.{os.getpid()}.{threading.get_ident()}.part{dst.suffix}")


def pid_alive(pid: int) -> bool:
    """Is process `pid` still running? False for a non-positive pid.

    POSIX: `kill(pid, 0)` (EPERM means it exists under another user).
    Windows: OpenProcess + GetExitCodeProcess — never `os.kill(pid, 0)`,
    which on Windows TERMINATES the process (signal 0 is passed to
    TerminateProcess as the exit code)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:  # pragma: no cover - exercised on the Windows CI runner
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))    # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def list_processes() -> list[tuple[int, str]]:
    """`(pid, full command line)` of every process this user can see, for
    finding a crashed backend's orphaned encoder. POSIX through `ps`; an
    empty list on Windows (no command lines without WMI) or when `ps` fails —
    callers treat that as "nothing found"."""
    if IS_WINDOWS:  # pragma: no cover
        return []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=10, **SUBPROCESS_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs: list[tuple[int, str]] = []
    for line in out.splitlines():
        head, _sp, cmd = line.strip().partition(" ")
        if head.isdigit():
            procs.append((int(head), cmd.strip()))
    return procs


def process_table() -> list[tuple[int, int, str]]:
    """`(pid, parent pid, command line)` of every process this user can see,
    on every OS — for saying WHAT was still alive when a cancelled encode did
    not stop (the tests' failure messages). POSIX through `ps`; Windows
    through PowerShell's CIM (`wmic` is gone from Windows 11 24H2), which
    takes about a second to start, so this is a diagnostic, never a hot path.
    An empty list when the tool is missing or fails."""
    if IS_WINDOWS:  # pragma: no cover - exercised on the Windows CI runner
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object "
                "{ \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }"]
    else:
        argv = ["ps", "-axo", "pid=,ppid=,command="]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", stdin=subprocess.DEVNULL,
                             timeout=30, **SUBPROCESS_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_process_table(out, "|" if IS_WINDOWS else None)


def parse_process_table(text: str, sep: str | None) -> list[tuple[int, int, str]]:
    """Rows of `pid<sep>ppid<sep>command line` (`sep` None: whitespace)."""
    rows: list[tuple[int, int, str]] = []
    for line in (text or "").splitlines():
        parts = line.strip().split(sep, 2)
        if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            cmd = parts[2].strip() if len(parts) == 3 else ""
            rows.append((int(parts[0]), int(parts[1]), cmd))
    return rows


# ---- killing a process TREE ---------------------------------------------------
#
# CI round 3 (Windows runner, ffmpeg from `choco install ffmpeg-full`): the
# `ffmpeg.exe` on PATH is a Chocolatey SHIM ("ShimGen has successfully created
# a shim for ffmpeg.exe") — a small launcher that starts the real ffmpeg as
# its CHILD and waits for it. Scoop installs the same kind of launcher.
# `Popen.kill()` is TerminateProcess on the launcher alone: the real ffmpeg
# ran on as an orphan, holding the inherited stdout/stderr pipes (so
# `communicate()` after the kill waited for the whole encode) and the `.part`
# file (which Windows then refuses to delete). Measured on that run: a
# superseded preview answered 49.69 s after the newer request, a 1 s deadline
# after 8.4 s, a cancelled proxy build wrote 15/15 spans.
#
# So on Windows every cancellable ffmpeg is started SUSPENDED, put in a Job
# Object and only then resumed (`popen_in_tree`), and cancelling it
# terminates the JOB — the launcher and whatever it started. A process is in
# its parent's job only if the parent was in it when it was created, so the
# launcher must not run before the assignment. POSIX is untouched: `nice`
# execs the tool, ffmpeg has no children, and `kill_process_tree` there IS
# `proc.kill()`.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9      # JOBOBJECTINFOCLASS
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
#: What a killed tree exits with — the code `Popen.kill()` itself passes to
#: TerminateProcess, so "exit 1" in logs and messages reads as before.
_TREE_KILL_EXIT_CODE = 1
_TREE_JOB_ATTR = "_vai_tree_job"
_CREATE_SUSPENDED = 0x00000004                  # process creation flag
#: One job handle is read by a kill and closed by a release, from two threads
#: (the proxy watcher kills while the encode's `finally` releases). Windows
#: reuses handle values, so a TerminateJobObject after the CloseHandle could
#: name ANOTHER encode's job. Held only around those two system calls.
_TREE_JOB_LOCK = threading.Lock()


def _job_limits_kill_on_close():
    """A JOBOBJECT_EXTENDED_LIMIT_INFORMATION asking for KILL_ON_JOB_CLOSE
    (144 bytes on 64-bit Windows; the test pins the size, because
    SetInformationJobObject rejects any other)."""
    import ctypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = _Extended()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


def _kernel32():  # pragma: no cover - Windows only; the tests substitute a fake
    """kernel32 with pointer-sized HANDLEs declared (the ctypes default is a
    C int, which truncates a 64-bit handle)."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, dword, void_p = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
    k32.CreateJobObjectW.restype = handle
    k32.CreateJobObjectW.argtypes = [void_p, ctypes.c_wchar_p]
    k32.SetInformationJobObject.argtypes = [handle, ctypes.c_int, void_p, dword]
    k32.AssignProcessToJobObject.argtypes = [handle, handle]
    k32.TerminateJobObject.argtypes = [handle, ctypes.c_uint]
    k32.CloseHandle.argtypes = [handle]
    return k32


def _create_kill_job(process_handle: int) -> int | None:
    """A Job Object holding the process behind `process_handle`, or None."""
    import ctypes
    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _job_limits_kill_on_close()
    if (k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                    ctypes.byref(info), ctypes.sizeof(info))
            and k32.AssignProcessToJobObject(job, process_handle)):
        return int(job)
    k32.CloseHandle(job)
    return None


def track_process_tree(proc: subprocess.Popen) -> bool:
    """Windows puts `proc` in a Job Object, so the children it starts FROM
    NOW ON die with it in `kill_process_tree`; a child it already has is not
    in the job and is not reached, which is why `popen_in_tree` calls this
    before the process has run at all. True when the job holds it. Nothing
    at all on POSIX. Pair every call with `release_process_tree`."""
    if not IS_WINDOWS:
        return False
    try:
        job = _create_kill_job(int(getattr(proc, "_handle")))
    except Exception:  # noqa: BLE001 — no job is a slower kill, never a failed render
        job = None
    setattr(proc, _TREE_JOB_ATTR, job)
    return job is not None


def _ntdll():  # pragma: no cover - Windows only; the tests substitute a fake
    import ctypes
    nt = ctypes.WinDLL("ntdll")
    nt.NtResumeProcess.restype = ctypes.c_long          # NTSTATUS
    nt.NtResumeProcess.argtypes = [ctypes.c_void_p]
    return nt


def _resume_process(proc: subprocess.Popen) -> bool:
    """Let a process started with CREATE_SUSPENDED run. Popen closes the
    main thread's handle, so it is resumed through the process handle
    (NtResumeProcess; Popen's handle has PROCESS_ALL_ACCESS). True on
    STATUS_SUCCESS."""
    try:
        return int(_ntdll().NtResumeProcess(int(getattr(proc, "_handle")))) == 0
    except Exception:  # noqa: BLE001 — the caller starts the tool the old way
        return False


def popen_in_tree(argv, **kwargs) -> subprocess.Popen:
    """`subprocess.Popen(argv, **kwargs)` for a process that may have to be
    killed with everything it starts. POSIX: exactly that. Windows: started
    suspended, put in its Job Object (`track_process_tree`), then resumed, so
    a launcher cannot start the real tool before the job holds it. Pair with
    `release_process_tree` once it has been waited for.

    A process that cannot be resumed would never end: it is killed and the
    tool started running, then tracked, as before (the child can then get
    out first on a contended machine; `taskkill /T` is tried only when no
    job exists)."""
    if not IS_WINDOWS:
        return subprocess.Popen(argv, **{**SUBPROCESS_FLAGS, **kwargs})
    running = {**SUBPROCESS_FLAGS, **kwargs}
    kwargs = dict(running)
    flags = int(kwargs.pop("creationflags", 0) or 0)
    proc = subprocess.Popen(argv, creationflags=flags | _CREATE_SUSPENDED, **kwargs)
    track_process_tree(proc)
    if _resume_process(proc):
        return proc
    try:
        kill_process_tree(proc)
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    release_process_tree(proc)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()
    proc = subprocess.Popen(argv, **{**SUBPROCESS_FLAGS, **running})   # as it was started before
    track_process_tree(proc)
    return proc


def _taskkill_tree(pid: int) -> bool:
    """`taskkill /T /F`: the fallback when no Job Object holds the tree (an
    outer job that forbids nesting). It walks parent pids, so it must run
    while the root is still alive — before `proc.kill()`. True when
    taskkill reported the tree terminated."""
    try:
        done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10, **SUBPROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(done, "returncode", 1) == 0


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` AND every process it started. POSIX: exactly
    `proc.kill()`. Windows: terminate its Job Object (`track_process_tree`),
    else `taskkill /T /F`, then the process itself."""
    if not IS_WINDOWS:
        proc.kill()
        return
    killed = False
    with _TREE_JOB_LOCK:                # not while `release_process_tree` closes it
        job = getattr(proc, _TREE_JOB_ATTR, None)
        if job:
            try:
                killed = bool(_kernel32().TerminateJobObject(job, _TREE_KILL_EXIT_CODE))
            except Exception:  # noqa: BLE001 — fall through to taskkill
                killed = False
    if not killed and proc.poll() is None:
        killed = bool(_taskkill_tree(proc.pid))
    try:
        proc.kill()
    except OSError:
        # TerminateProcess on a process the tree kill is already taking down
        # answers "access denied" while its exit code still reads as running
        # (Popen.kill re-raises that). It is dying; a cancel must not turn
        # into a PermissionError. Without a tree kill, raise as before.
        if not killed:
            raise


def release_process_tree(proc: subprocess.Popen) -> None:
    """Close the Job Object of a process that has been waited for. The job
    is KILL_ON_JOB_CLOSE, so this also ends anything the process left
    behind. Idempotent; nothing on POSIX."""
    if not IS_WINDOWS:
        return
    with _TREE_JOB_LOCK:
        job = getattr(proc, _TREE_JOB_ATTR, None)
        if not job:
            return
        setattr(proc, _TREE_JOB_ATTR, None)
        try:
            _kernel32().CloseHandle(job)
        except Exception:  # noqa: BLE001 — a leaked handle is not worth failing a render
            pass


def replace_with_retry(src: Path | str, dst: Path | str,
                       attempts: int = 10, delay: float = 0.05) -> None:
    """os.replace with retry. On Windows, replacing a file another process has
    open (e.g. a Starlette FileResponse streaming the preview) raises
    PermissionError; a short backoff lets the reader finish. On POSIX this
    almost always succeeds on the first try."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # pragma: no cover - Windows-timing path
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def unlink_with_retry(path: Path | str,
                      attempts: int = 5, delay: float = 0.05) -> None:
    """Path.unlink(missing_ok=True) with the same Windows open-file retry."""
    p = Path(path)
    for i in range(attempts):
        try:
            p.unlink(missing_ok=True)
            return
        except PermissionError:  # pragma: no cover - Windows-timing path
            time.sleep(delay * (i + 1))
    # Best-effort: a leftover cache file is not fatal.


def rmtree_with_retry(path: Path | str,
                      attempts: int = 10, delay: float = 0.1) -> None:
    """shutil.rmtree with retry/backoff for Windows mandatory file locking.

    On Windows, a directory containing a file with any open handle (e.g. a
    Starlette FileResponse still streaming a previews/*.mp4 or exports/*.mp4,
    an in-flight render's *.part.mp4, or a lingering AV/indexer scan) cannot
    be deleted — shutil.rmtree(ignore_errors=False) raises PermissionError/
    OSError partway through, leaving the tree partially deleted. A short
    backoff lets the other handle-holder finish, mirroring
    replace_with_retry/unlink_with_retry above. On POSIX an open file can be
    unlinked while still held open, so rmtree normally succeeds on the first
    try there regardless."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError:
            # Already gone (e.g. a partial previous rmtree finished the job,
            # or a concurrent delete raced us) — nothing left to remove.
            return
        except (PermissionError, OSError) as e:  # pragma: no cover - Windows-timing path
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


# A path written on the OTHER operating system, as it arrives inside a project
# file (.vae) someone saved on Windows and opened on a Mac: "C:\...\a.mp4" or a
# UNC share "\\nas\footage\a.mp4". POSIX `Path` treats "\" as an ordinary
# filename character, so `Path(src).name` was the ENTIRE Windows path — the
# offline-media row for a clip that was not bundled read
# "C:\Users\…\interview.mp4" instead of "interview.mp4".
_WINDOWS_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def pure_path(src: str | os.PathLike) -> PurePath:
    """A pure path that splits `src` the way the machine that WROTE it did.

    A Windows-shaped absolute path (drive letter or UNC) is parsed as
    `PureWindowsPath` on every OS; anything else is this OS's own path. Only
    for looking at a path's parts — never for opening it."""
    s = os.fspath(src)
    if not IS_WINDOWS and _WINDOWS_ABS.match(s):
        return PureWindowsPath(s)
    return Path(s)


def path_leaf(src: str | os.PathLike) -> str:
    """The file name of `src`, even when `src` was written on another OS."""
    return pure_path(src).name
