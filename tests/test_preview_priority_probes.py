"""POST /preview?priority=low niced EVERY process it spawns (wave D3; follow-up
12 of wave D2): not only the render ffmpegs (test_preview_priority.py) but the
short probes a render makes on the way — the encoder capability probes, the
has-audio ffprobe, the AAC encoder probe, the container-duration ffprobe —
and the ``-c:v copy -an`` stream copy that files the video-only cache entry.
compositor.py used to start those with a plain ``subprocess.run``, outside
``cancel.run``, so they ran at the server's own priority.

Measured on the REAL processes, sampled from outside while they run: the
kernel nice value (`os.getpriority`) of each spawned ffmpeg/ffprobe.
"""
from __future__ import annotations

import os

import pytest

from video_ai_editor.render import cancel as C
from video_ai_editor.render import compositor, sar

from test_preview_priority import BASE, LOW, _Sampler, _session, client  # noqa: F401  (client: fixture)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX nice; Windows uses a priority class")


def _fresh_probe_caches() -> None:
    """A fresh process as far as the cached probes go: they run again."""
    for name in dir(compositor):
        fn = getattr(compositor, name)
        if callable(fn) and hasattr(fn, "cache_clear") and name.startswith("_"):
            fn.cache_clear()
    sar._probe.cache_clear()


#: the modules whose direct subprocess.run calls this wave routed through
#: cancel.run_prioritised (render/sar.py since review RD3)
OURS = {"render/compositor.py", "render/segments.py", "render/sar.py"}


def _trace_origins(monkeypatch) -> dict[int, str | None]:
    """pid → the render module (``render/x.py``) that started it."""
    import subprocess
    import traceback
    origins: dict[int, str | None] = {}
    watched = subprocess.Popen

    class Traced(watched):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            super().__init__(args, *a, **kw)
            mod = None
            for f in reversed(traceback.extract_stack()[:-1]):
                name = f.filename.replace(os.sep, "/")
                if "/video_ai_editor/" in name and not name.endswith("render/cancel.py"):
                    mod = name.split("/video_ai_editor/", 1)[1]
                    break
            origins[self.pid] = mod

    monkeypatch.setattr(subprocess, "Popen", Traced)
    return origins


def _kind(argv: list[str]) -> str | None:
    text = " ".join(map(str, argv))
    exe = next((os.path.basename(str(a)) for a in argv if os.path.basename(str(a)) in ("ffmpeg", "ffprobe")), None)
    if exe is None:
        return None
    if exe == "ffprobe":
        return "ffprobe"
    if "-encoders" in argv:
        return "encoders-listing"
    if "lavfi" in argv and argv[-2:] == ["null", "-"]:
        return "encoder-probe"
    if "-c:v" in argv and argv[argv.index("-c:v") + 1] == "copy" and "-an" in argv and "-filter_complex" not in text:
        return "video-only-copy"
    return "render"


def test_priority_low_nices_the_probes_and_the_video_only_copy_too(client, tmp_path, monkeypatch):  # noqa: F811
    _fresh_probe_caches()
    sid = _session(client, tmp_path)
    sampler = _Sampler()
    sampler.install(monkeypatch)
    origin = _trace_origins(monkeypatch)
    r = client.post(f"/api/sessions/{sid}/preview", params={"priority": "low"})
    sampler.join()
    assert r.status_code == 200, r.text
    seen: dict[str, list[set[int]]] = {}
    for pid, argv in sampler.argv.items():
        kind = _kind(argv)
        # this wave's scope: what compositor.py and segments.py start
        if kind is None or origin.get(pid) not in OURS:
            continue
        nices = {nice for name, nice in sampler.samples.get(pid, []) if name and ("ffmpeg" in name or "ffprobe" in name)}
        if nices:
            seen.setdefault(kind, []).append(nices)
    report = sampler.report()
    # the processes this test is about were really spawned and observed
    assert "video-only-copy" in seen and "ffprobe" in seen and "render" in seen, (sorted(seen), report)
    # render/sar.py's source probe was among them (review RD3)
    assert any(origin.get(pid) == "render/sar.py" and _kind(argv) == "ffprobe"
               for pid, argv in sampler.argv.items()), report
    for kind, rows in seen.items():
        bad = [" ".join(map(str, sampler.argv[pid]))[:200] for pid in sampler.argv if _kind(sampler.argv[pid]) == kind
               and {n for name, n in sampler.samples.get(pid, []) if name and ("ffmpeg" in name or "ffprobe" in name)} - {LOW}]
        assert all(n == {LOW} for n in rows), (kind, bad)
    assert os.getpriority(os.PRIO_PROCESS, 0) == BASE


def test_the_probes_keep_the_servers_priority_outside_priority_low(tmp_path):
    """run_prioritised is a no-op outside low_priority(): same argv."""
    import subprocess
    calls: list[list[str]] = []

    class Rec:
        def __init__(self, *a, **k):
            calls.append(list(a[0]))
            self.returncode, self.stdout, self.stderr = 0, "", ""

    real = subprocess.run
    try:
        subprocess.run = lambda argv, **kw: Rec(argv)  # type: ignore[assignment]
        C.run_prioritised(["ffprobe", "-version"])
        with C.low_priority():
            C.run_prioritised(["ffprobe", "-version"])
    finally:
        subprocess.run = real  # type: ignore[assignment]
    assert calls[0] == ["ffprobe", "-version"]
    assert os.path.basename(calls[1][0]) == "nice" and calls[1][1:3] == ["-n", "10"] and calls[1][3:] == ["ffprobe", "-version"]
