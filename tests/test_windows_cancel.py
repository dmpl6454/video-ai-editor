"""CI round 3: on Windows a cancelled render kept running.

The Windows runner installs ffmpeg with Chocolatey, whose `ffmpeg.exe` on PATH
is a SHIM: a launcher that starts the real ffmpeg as its child and waits.
`Popen.kill()` terminated the launcher only. The real ffmpeg ran on, holding
the pipes (`communicate()` after the kill waited for the whole encode) and the
`.part` file. Measured there: a superseded preview answered 49.69 s after the
newer request, a 1 s deadline after 8.4 s, a cancelled proxy build wrote 15 of
15 spans, an export pause let 3 more spans through.

This Mac has no Windows, so the tests rehearse it:

* unit tests drive `platformutil`'s Windows branch with the platform flag
  set and a recording kernel32 (which Job Object calls, in which order);
* rehearsals put a launcher of the same shape in front of a slow process
  (`_pu.FFMPEG` for the proxy build) and give the fake kernel32 a
  TerminateJobObject that really kills the tree, so the product's callers are
  timed end to end. Before the fix every rehearsal waits for the orphan.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from video_ai_editor import platformutil as _pu
from video_ai_editor.render import cancel as rcancel

#: How long the orphan would live if nothing killed it; a rehearsal that takes
#: anywhere near this long waited for it.
ORPHAN_S = 30
#: A kill that lands answers within the 50 ms poll plus process teardown
#: (measured here: 0.17 s after the cancel, 0.77 s in all); 5 s is far from
#: both that and ORPHAN_S.
PROMPT_S = 5.0
CREATE_SUSPENDED = 0x4


# ---- a recording kernel32 ------------------------------------------------------

class FakeKernel32:
    """Records the Job Object calls; `assign_ok=False` is an outer job that
    forbids nesting. With `really_kill`, TerminateJobObject kills what the
    job holds, the way the real one does: the process and the children it
    started AFTER it was assigned (membership is inherited at creation; a
    child that already existed is not in the job). `assign_late` is a
    contended machine: the assignment lands only once the process has
    started a child, or after `assign_late` seconds if it never does."""

    def __init__(self, *, assign_ok: bool = True, really_kill: bool = False,
                 assign_late: float = 0.0) -> None:
        self.calls: list[tuple] = []
        self.assign_ok = assign_ok
        self.really_kill = really_kill
        self.assign_late = assign_late
        self.jobs: dict[int, int] = {}
        self.escaped: dict[int, set[int]] = {}
        self._next = 9000

    def CreateJobObjectW(self, attrs, name):
        self._next += 1
        self.calls.append(("CreateJobObjectW", self._next))
        return self._next

    def SetInformationJobObject(self, job, klass, ref, size):
        flags = ref._obj.BasicLimitInformation.LimitFlags
        self.calls.append(("SetInformationJobObject", job, klass, flags, size))
        return 1

    def AssignProcessToJobObject(self, job, handle):
        if self.assign_late:
            end = time.monotonic() + self.assign_late
            while time.monotonic() < end and not _descendants(handle):
                time.sleep(0.01)
        self.calls.append(("AssignProcessToJobObject", job, handle))
        if self.assign_ok:
            self.jobs[job] = handle
            if self.really_kill:
                self.escaped[job] = set(_descendants(handle))
        return 1 if self.assign_ok else 0

    def TerminateJobObject(self, job, code):
        self.calls.append(("TerminateJobObject", job, code))
        if self.really_kill and job in self.jobs:
            _kill_tree_posix(self.jobs[job], spare=self.escaped[job])
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _host_table() -> list[tuple[int, int, str]]:
    """This host's process table — asked of the host itself, because inside
    a rehearsal `_pu.IS_WINDOWS` is set and `_pu.process_table()` would go
    looking for PowerShell."""
    if _pu.IS_WINDOWS and os.name == "nt":
        return _pu.process_table()
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                         text=True, errors="replace").stdout
    return _pu.parse_process_table(out, None)


def _descendants(root: int) -> list[int]:
    table = _host_table()
    found, frontier = [], [root]
    while frontier:
        parent = frontier.pop()
        kids = [pid for pid, ppid, _cmd in table if ppid == parent]
        found += kids
        frontier += kids
    return found


def _kill_tree_posix(root: int, spare: set[int] = frozenset()) -> None:
    """Kill `root` and its descendants, except the `spare` ones and theirs."""
    table = _host_table()
    doomed, frontier = [root], [root]
    while frontier:
        parent = frontier.pop()
        kids = [pid for pid, ppid, _cmd in table if ppid == parent and pid not in spare]
        doomed += kids
        frontier += kids
    for pid in reversed(doomed):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class FakeProc:
    def __init__(self, pid: int = 4321, handle: int = 77) -> None:
        self.pid = pid
        self._handle = handle
        self.log: list[str] = []

    def kill(self) -> None:
        self.log.append("kill")

    def poll(self):
        return None


@pytest.fixture
def windows(monkeypatch):
    """The platform flag set, with a recording kernel32 and taskkill."""
    k32 = FakeKernel32()
    monkeypatch.setattr(_pu, "IS_WINDOWS", True)
    monkeypatch.setattr(_pu, "_kernel32", lambda: k32)
    k32.taskkills = []
    monkeypatch.setattr(_pu, "_taskkill_tree", lambda pid: k32.taskkills.append(pid))
    return k32


# ---- platformutil: the Windows branch ------------------------------------------

def test_job_limit_struct_is_the_size_windows_expects():
    """SetInformationJobObject rejects any other length; 64-bit Windows and
    this Mac lay the struct out alike (LLP64 and LP64 differ in `long`, which
    it does not use)."""
    info = _pu._job_limits_kill_on_close()
    assert ctypes.sizeof(info) == 144
    assert info.BasicLimitInformation.LimitFlags == 0x2000


def test_posix_tree_kill_is_exactly_proc_kill(monkeypatch):
    monkeypatch.setattr(_pu, "IS_WINDOWS", False)
    monkeypatch.setattr(_pu, "_kernel32", lambda: pytest.fail("kernel32 on POSIX"))
    monkeypatch.setattr(_pu, "_taskkill_tree", lambda pid: pytest.fail("taskkill on POSIX"))
    proc = FakeProc()
    assert _pu.track_process_tree(proc) is False
    _pu.kill_process_tree(proc)
    _pu.release_process_tree(proc)
    assert proc.log == ["kill"]
    assert not hasattr(proc, _pu._TREE_JOB_ATTR)


def test_windows_track_puts_the_process_in_a_kill_on_close_job(windows):
    proc = FakeProc(handle=77)
    assert _pu.track_process_tree(proc) is True
    job = windows.calls[0][1]
    assert windows.calls == [
        ("CreateJobObjectW", job),
        ("SetInformationJobObject", job, 9, 0x2000, 144),
        ("AssignProcessToJobObject", job, 77)]


def test_windows_kill_terminates_the_job_before_the_process(windows):
    proc = FakeProc()
    _pu.track_process_tree(proc)
    job = windows.calls[0][1]
    windows.TerminateJobObject = lambda j, code: (proc.log.append(f"job {j} {code}"), 1)[1]
    _pu.kill_process_tree(proc)
    assert proc.log == [f"job {job} 1", "kill"]
    assert windows.taskkills == [], "taskkill is the fallback, not the rule"


def test_windows_kill_falls_back_to_taskkill_when_no_job_holds_the_tree(monkeypatch, windows):
    windows.assign_ok = False
    proc = FakeProc(pid=4321)
    assert _pu.track_process_tree(proc) is False
    assert windows.names()[-1] == "CloseHandle", "the unused job handle leaked"
    monkeypatch.setattr(_pu, "_taskkill_tree",
                        lambda pid: proc.log.append(f"taskkill /T /F /PID {pid}"))
    _pu.kill_process_tree(proc)
    # taskkill walks parent pids: it must run while the root is still alive
    assert proc.log == ["taskkill /T /F /PID 4321", "kill"]
    assert "TerminateJobObject" not in windows.names()


def test_taskkill_fallback_kills_the_tree_by_force(monkeypatch):
    seen: list[list[str]] = []

    def run(argv, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_pu.subprocess, "run", run)
    assert _pu._taskkill_tree(4321) is True
    assert seen == [["taskkill", "/T", "/F", "/PID", "4321"]]


class _DyingProc(FakeProc):
    def kill(self) -> None:
        super().kill()
        raise PermissionError(13, "Access is denied")


def test_windows_kill_of_a_process_the_job_is_taking_down_is_not_an_error(windows):
    """`Popen.kill()` re-raises access-denied while the exit code still reads
    as running; after a tree kill that is a process on its way out, and the
    caller must get its RenderCancelled, not a PermissionError."""
    proc = _DyingProc()
    _pu.track_process_tree(proc)
    _pu.kill_process_tree(proc)
    assert proc.log == ["kill"] and "TerminateJobObject" in windows.names()


def test_windows_kill_that_reached_nothing_still_raises(windows):
    windows.assign_ok = False
    proc = _DyingProc()
    _pu.track_process_tree(proc)
    with pytest.raises(PermissionError):
        _pu.kill_process_tree(proc)


class _RecordingPopen:
    """Popen for the unit tests: records how it was started."""
    started: list[dict] = []

    def __init__(self, argv, **kw) -> None:
        self.pid, self._handle, self.argv, self.kw = 4321, 77, list(argv), kw
        self.stdin = self.stdout = self.stderr = None
        self.log: list[str] = []
        _RecordingPopen.started.append(kw)

    def kill(self) -> None:
        self.log.append("kill")

    def poll(self):
        return None

    def wait(self, timeout=None) -> int:
        self.log.append("wait")
        return 1


@pytest.fixture
def recorded_popen(monkeypatch):
    _RecordingPopen.started = []
    monkeypatch.setattr(_pu.subprocess, "Popen", _RecordingPopen)
    return _RecordingPopen.started


def test_posix_start_is_exactly_popen(monkeypatch, recorded_popen):
    monkeypatch.setattr(_pu, "IS_WINDOWS", False)
    monkeypatch.setattr(_pu, "_kernel32", lambda: pytest.fail("kernel32 on POSIX"))
    monkeypatch.setattr(_pu, "_ntdll", lambda: pytest.fail("ntdll on POSIX"))
    proc = _pu.popen_in_tree(["ffmpeg", "-i", "a"], stdin=subprocess.DEVNULL)
    assert proc.argv == ["ffmpeg", "-i", "a"]
    assert recorded_popen == [{"stdin": subprocess.DEVNULL}]
    assert not hasattr(proc, _pu._TREE_JOB_ATTR)


def test_windows_start_is_suspended_until_the_job_holds_it(monkeypatch, windows, recorded_popen):
    class Ntdll:
        def NtResumeProcess(self, handle):
            windows.calls.append(("NtResumeProcess", handle))
            return 0
    monkeypatch.setattr(_pu, "_ntdll", lambda: Ntdll())
    proc = _pu.popen_in_tree(["ffmpeg.exe"], creationflags=0x08000000, stdout=subprocess.PIPE)
    # the caller's flags are kept (CREATE_NO_WINDOW), CREATE_SUSPENDED added
    assert recorded_popen == [{"creationflags": 0x08000000 | CREATE_SUSPENDED, "stdout": subprocess.PIPE}]
    assert windows.names() == ["CreateJobObjectW", "SetInformationJobObject",
                               "AssignProcessToJobObject", "NtResumeProcess"]
    assert windows.calls[-1] == ("NtResumeProcess", 77)
    assert getattr(proc, _pu._TREE_JOB_ATTR) == windows.calls[0][1] and proc.log == []


def test_windows_start_without_a_job_still_resumes(monkeypatch, windows, recorded_popen):
    """An outer job that forbids nesting: no job, the process must run all
    the same (taskkill reaches its tree)."""
    windows.assign_ok = False
    resumed = []
    monkeypatch.setattr(_pu, "_ntdll", lambda: type("N", (), {
        "NtResumeProcess": lambda self, h: resumed.append(h) or 0})())
    proc = _pu.popen_in_tree(["ffmpeg.exe"])
    assert resumed == [77] and getattr(proc, _pu._TREE_JOB_ATTR) is None and proc.log == []


@pytest.mark.parametrize("failure", ["status", "raises"])
def test_windows_start_that_cannot_be_resumed_is_started_again_running(
        monkeypatch, windows, recorded_popen, failure):
    """A process left suspended never ends: it is killed and the tool is
    started the way it was before (running, then put in its job)."""
    class Ntdll:
        def NtResumeProcess(self, handle):
            if failure == "raises":
                raise OSError("no ntdll")
            return 0xC0000022 - (1 << 32)            # STATUS_ACCESS_DENIED
    monkeypatch.setattr(_pu, "_ntdll", lambda: Ntdll())
    proc = _pu.popen_in_tree(["ffmpeg.exe"], creationflags=0x08000000)
    assert recorded_popen == [{"creationflags": 0x08000000 | CREATE_SUSPENDED},
                              {"creationflags": 0x08000000}]
    assert proc.log == [], "the process handed back is the second one, untouched"
    jobs = [c[1] for c in windows.calls if c[0] == "CreateJobObjectW"]
    assert len(jobs) == 2 and getattr(proc, _pu._TREE_JOB_ATTR) == jobs[1]
    assert ("TerminateJobObject", jobs[0], 1) in windows.calls
    assert ("CloseHandle", jobs[0]) in windows.calls and ("CloseHandle", jobs[1]) not in windows.calls


def test_every_cancellable_start_goes_through_the_suspended_one():
    """cancel.run and the proxy encoder are the two callers; neither may
    start its process running and put it in the job afterwards."""
    from video_ai_editor.ingest import proxy
    import inspect
    assert "popen_in_tree" in inspect.getsource(rcancel.run)
    assert "subprocess.Popen(" not in inspect.getsource(rcancel.run)
    assert inspect.getsource(proxy._spawn).count("popen_in_tree(") == 2
    assert "subprocess.Popen(" not in inspect.getsource(proxy._spawn)


def test_a_kill_cannot_use_a_job_handle_that_is_being_closed(windows):
    """The proxy watcher kills from its own thread while the encode's
    `finally` releases: TerminateJobObject must never see a handle CloseHandle
    has already been given (Windows reuses handle values: it could name
    another encode's job)."""
    proc = FakeProc()
    _pu.track_process_tree(proc)
    job = windows.calls[0][1]
    entered, go = threading.Event(), threading.Event()

    def slow_terminate(j, code):
        entered.set()
        assert go.wait(5)
        windows.calls.append(("TerminateJobObject", j, code))
        return 1
    windows.TerminateJobObject = slow_terminate
    killer = threading.Thread(target=_pu.kill_process_tree, args=(proc,))
    killer.start()
    assert entered.wait(5)
    releaser = threading.Thread(target=_pu.release_process_tree, args=(proc,))
    releaser.start()
    time.sleep(0.2)
    assert ("CloseHandle", job) not in windows.calls, "closed under a running TerminateJobObject"
    go.set()
    killer.join(5)
    releaser.join(5)
    assert windows.names()[-2:] == ["TerminateJobObject", "CloseHandle"]
    _pu.kill_process_tree(proc)                      # after the release: no job call at all
    assert windows.names().count("TerminateJobObject") == 1


def test_windows_release_closes_the_job_once(windows):
    proc = FakeProc()
    _pu.track_process_tree(proc)
    job = windows.calls[0][1]
    _pu.release_process_tree(proc)
    _pu.release_process_tree(proc)
    assert windows.calls.count(("CloseHandle", job)) == 1


def test_process_table_rows_parse_on_both_shapes():
    posix = "  501     1 /sbin/launchd\n  777   501 ffmpeg -i a b.mp4\n"
    assert _pu.parse_process_table(posix, None) == [
        (501, 1, "/sbin/launchd"), (777, 501, "ffmpeg -i a b.mp4")]
    win = "4|0|\r\n6772|5444|\"C:\\x\\ffmpeg.exe\" -i a|b out.mp4\r\nnot a row\r\n"
    assert _pu.parse_process_table(win, "|") == [
        (4, 0, ""), (6772, 5444, "\"C:\\x\\ffmpeg.exe\" -i a|b out.mp4")]


def test_process_table_sees_this_process():
    me = [row for row in _pu.process_table() if row[0] == os.getpid()]
    assert me and me[0][1] == os.getppid()


# ---- rehearsals: a launcher in front of the real process ------------------------

def alive_with(marker: str) -> list[tuple[int, int, str]]:
    """Processes whose command line carries `marker` (a path of this test)."""
    return [row for row in _host_table()
            if marker in row[2] and row[0] != os.getpid()]


def _write_launcher(path: Path, real: str) -> Path:
    """What a Chocolatey shim is: start the real tool as a CHILD, hand it
    our stdio, wait for it, pass its exit code on."""
    path.write_text(f"#!{sys.executable}\nimport subprocess, sys\n"
                    f"sys.exit(subprocess.call([{real!r}, *sys.argv[1:]]))\n",
                    encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_slow_writer(path: Path) -> Path:
    """Stands in for an ffmpeg mid-encode: holds its output file open and
    keeps writing to it (and to stderr, like ffmpeg's stats) for ORPHAN_S."""
    path.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        "out = open(sys.argv[-1], 'wb')\n"
        f"end = time.monotonic() + {ORPHAN_S}\n"
        "while time.monotonic() < end:\n"
        "    out.write(b'x' * 4096); out.flush()\n"
        "    sys.stderr.write('frame\\n'); sys.stderr.flush()\n"
        "    time.sleep(0.02)\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def windows_with_launchers(monkeypatch):
    """The Windows branch with a kernel32 whose TerminateJobObject kills the
    tree for real, and every Popen carrying the `_handle` Windows gives it."""
    if _pu.IS_WINDOWS:
        pytest.skip("a rehearsal of Windows for POSIX hosts; on Windows the "
                    "real thing runs in test_preview_supersede / test_proxy")
    k32 = FakeKernel32(really_kill=True)
    real_popen = subprocess.Popen

    class _WindowsPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            flags = kw.pop("creationflags", 0)
            super().__init__(*a, **kw)
            self._handle = self.pid
            if flags & CREATE_SUSPENDED:
                # a launcher needs tens of milliseconds to start its child
                # (measured here: 40 ms); this lands microseconds after fork
                os.kill(self.pid, signal.SIGSTOP)

    class _Ntdll:
        def NtResumeProcess(self, handle):
            k32.calls.append(("NtResumeProcess", handle))
            os.kill(handle, signal.SIGCONT)
            return 0

    monkeypatch.setattr(subprocess, "Popen", _WindowsPopen)
    monkeypatch.setattr(_pu, "IS_WINDOWS", True)
    monkeypatch.setattr(_pu, "_kernel32", lambda: k32)
    monkeypatch.setattr(_pu, "_ntdll", lambda: _Ntdll(), raising=False)
    # nothing here may depend on a taskkill this Mac does not have
    monkeypatch.setattr(_pu, "_taskkill_tree", lambda pid: None)
    return k32


def _render_to_part(argv: list[str], part: Path) -> None:
    """The shape of every preview caller (compositor, segments,
    preview_loudness.regain): render to a `.part`, unlink it on any error."""
    try:
        rcancel.run([*argv, str(part)], capture_output=True, text=True)
    except BaseException:
        _pu.unlink_with_retry(part)
        raise


def _windows_unlink(monkeypatch) -> None:
    """Windows refuses to delete a file a process still has open; POSIX does
    not, so say so here: PermissionError while the writer lives."""
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if alive_with(str(self)):
            raise PermissionError(13, "The process cannot access the file because "
                                      "it is being used by another process", str(self))
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)


@pytest.mark.parametrize("stop", ["superseded", "deadline"])
def test_cancelled_render_behind_a_launcher_stops_and_leaves_no_part(
        windows_with_launchers, tmp_path, monkeypatch, stop):
    writer = _write_slow_writer(tmp_path / "real_ffmpeg.py")
    launcher = _write_launcher(tmp_path / "ffmpeg_shim.py", str(writer))
    part = tmp_path / ".seg_rehearsal.part.mp4"
    _windows_unlink(monkeypatch)
    ev = threading.Event()
    if stop == "superseded":
        threading.Timer(0.6, ev.set).start()
    t0 = time.monotonic()
    try:
        with rcancel.scope(ev, deadline_s=None if stop == "superseded" else 0.6):
            with pytest.raises(rcancel.RenderCancelled) as hit:
                _render_to_part([str(launcher)], part)
        took = time.monotonic() - t0
        left = alive_with(str(part))
        assert (hit.type is rcancel.RenderTimedOut) == (stop == "deadline")
        assert "TerminateJobObject" in windows_with_launchers.names(), (
            "the cancel never asked for the process TREE to be killed: "
            f"{windows_with_launchers.names()}")
        assert took < PROMPT_S, (
            f"cancelled after 0.6 s, answered after {took:.2f} s (the orphan lives "
            f"{ORPHAN_S} s); still alive: {left}")
        assert not left, f"the real encoder outlived its launcher: {left}"
        assert not part.exists(), f"{part.name} was left behind"
        assert windows_with_launchers.names()[-1] == "CloseHandle", "the job handle leaked"
    finally:
        for pid, _ppid, _cmd in alive_with(str(part)):
            os.kill(pid, signal.SIGKILL)


def test_render_that_finishes_releases_its_job(windows_with_launchers, tmp_path):
    ev = threading.Event()
    with rcancel.scope(ev):
        cp = rcancel.run([sys.executable, "-c", "print('done')"],
                         capture_output=True, text=True)
    assert cp.returncode == 0 and cp.stdout.strip() == "done"
    names = windows_with_launchers.names()
    assert names == ["CreateJobObjectW", "SetInformationJobObject",
                     "AssignProcessToJobObject", "NtResumeProcess", "CloseHandle"], names


def test_a_child_cannot_start_before_the_job_holds_its_launcher(
        windows_with_launchers, tmp_path, monkeypatch):
    """A contended machine: AssignProcessToJobObject lands late. A launcher
    that was already running by then had started the real encoder OUTSIDE the
    job, TerminateJobObject killed the launcher alone and the encoder ran on
    with the pipes and the `.part` (the 49.69 s supersede again). The
    process is started suspended, so it has no child before it is held."""
    k32 = windows_with_launchers
    k32.assign_late = 1.0
    writer = _write_slow_writer(tmp_path / "real_ffmpeg.py")
    launcher = _write_launcher(tmp_path / "ffmpeg_shim.py", str(writer))
    part = tmp_path / ".seg_late.part.mp4"
    _windows_unlink(monkeypatch)
    ev = threading.Event()

    def supersede_once_it_encodes() -> None:
        if _wait_for(lambda: part.exists() and part.stat().st_size > 0, ORPHAN_S):
            ev.set()
    threading.Thread(target=supersede_once_it_encodes, daemon=True).start()
    try:
        with rcancel.scope(ev), pytest.raises(rcancel.RenderCancelled):
            _render_to_part([str(launcher)], part)
        assert ev.is_set(), "the render ended before the encoder wrote anything"
        assert k32.escaped and not any(k32.escaped.values()), (
            f"the launcher had started {k32.escaped} before the job held it")
        order = k32.names()
        assert order.index("AssignProcessToJobObject") < order.index("NtResumeProcess"), order
        left = alive_with(str(part))
        assert not left, f"the real encoder outlived its launcher: {left}"
        assert not part.exists(), f"{part.name} was left behind"
    finally:
        for pid, _ppid, _cmd in alive_with(str(part)):
            os.kill(pid, signal.SIGKILL)


# ---- rehearsals: the proxy build ------------------------------------------------

@pytest.fixture
def proxy_behind_a_launcher(windows_with_launchers, tmp_path, monkeypatch):
    """A 900-frame master whose proxy encode runs through a launcher."""
    from proxy_fixtures import make_barcode_master
    from video_ai_editor import storage as _storage
    from video_ai_editor.ingest.proxy_queue import ProxyManager
    import shutil
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.setattr(_storage, "WORKDIR", wd)
    src = make_barcode_master(tmp_path / "long.mp4", frames=900, rate="30", w=960, h=540,
                              audio=False)
    real = shutil.which(_pu.FFMPEG)
    assert real, "no ffmpeg on PATH"
    monkeypatch.setattr(_pu, "FFMPEG", str(_write_launcher(tmp_path / "ffmpeg_shim.py", real)))
    manager = ProxyManager()
    yield manager, src, windows_with_launchers
    manager.shutdown()
    manager.wait_idle(10)
    for pid, _ppid, _cmd in alive_with(str(src)):
        os.kill(pid, signal.SIGKILL)


def _wait_for(pred, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _spans_on_disk(key: str) -> int:
    from video_ai_editor.ingest import proxy as P
    info = P.load_source(key)
    return sum(P.span_path(key, n).is_file() for n in range(info.spans))


def test_cancelled_proxy_build_behind_a_launcher_stops(proxy_behind_a_launcher):
    from video_ai_editor.ingest import proxy as P
    manager, src, k32 = proxy_behind_a_launcher
    key = manager.ensure(src)
    assert _wait_for(lambda: P.span_path(key, 0).is_file())
    assert manager.cancel(src) is True
    assert manager.wait_idle(20)
    done, spans = _spans_on_disk(key), P.load_source(key).spans
    assert "TerminateJobObject" in k32.names(), k32.names()
    assert 0 < done < spans, (
        f"cancel did not stop the build: {done}/{spans} spans; "
        f"still alive: {alive_with(str(src))}")
    assert _wait_for(lambda: not alive_with(str(src)), 5), alive_with(str(src))
    assert k32.names().count("CloseHandle") == k32.names().count("CreateJobObjectW")


def test_export_pause_holds_a_proxy_build_behind_a_launcher(proxy_behind_a_launcher):
    from video_ai_editor.ingest import proxy as P
    manager, src, k32 = proxy_behind_a_launcher
    key = manager.ensure(src)
    assert _wait_for(lambda: P.span_path(key, 0).is_file())
    with manager.export_in_progress():
        time.sleep(0.3)                      # the running encode is killed ...
        count = _spans_on_disk(key)
        time.sleep(1.0)                      # ... and nothing is written meanwhile
        after = _spans_on_disk(key)
        assert after == count, (
            f"{after - count} spans were written during the export pause; "
            f"still alive: {alive_with(str(src))}")
        assert not alive_with(str(src)), alive_with(str(src))
    assert "TerminateJobObject" in k32.names(), k32.names()
    assert manager.wait_idle(180)
    assert manager.stats["paused"] >= 1
    assert P.live_index(key)["state"] == "ready"
