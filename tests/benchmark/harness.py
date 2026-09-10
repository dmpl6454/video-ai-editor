"""The benchmark harness (spec §6.1): one in-process app, one uploaded and
transcribed fixture session per media variant, session CLONES per case, a
prompt runner that auto-answers clarifications, and a socket-level egress
guard that fails any outbound connection.

WHY TestClient and not a uvicorn on a port: the run thread, the session lock,
the LRU store cache and the SSE generator are all process-local, so a
TestClient exercises exactly the code the desktop app runs — and it needs no
port, which matters on a Mac where a dev backend may already own :8765.

WHY clone by copying the session directory: every fixture is uploaded and
transcribed ONCE (whisper-small on the 85 s fixture is ~7 s; the upload's
ingest normalisation ~2 s) and every case starts from a byte-identical copy.
`ingest_upload` writes absolute paths into `edl.json` / `ingest.json` /
`ops.json` / the snapshots, so a plain copy would still point at the ORIGINAL
session's uploads — and a case that deletes the transcript (case 5) or lets
`transcribe` rewrite `ingest.json` (case 24) would poison every later case.
`clone_session_dir` rewrites the prefix in every JSON file it copies, so a
clone is self-contained and `_current_v1_ingest_json` resolves inside it.

WHY the egress guard is at the socket layer: "no network without a yes"
(§1.4) is a property of the whole process, not of one module. Patching
`socket.socket.connect` (+ `create_connection` + `getaddrinfo` for names)
catches huggingface_hub, piper's voice downloader, urllib, httpx and anything
else, without knowing their names. Loopback and AF_UNIX stay allowed — the
app's own loopback-only routes are part of what is under test.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest

from .media import MediaSet, build_media_set, ensure_preset_beds, scene_cuts_for
from .narration import HindiVoiceUnavailable, Narration

#: Fixture variants a case may ask for (`Case.fixture`).
FIXTURES: tuple[str, ...] = ("en_16x9", "en_9x16", "presplit_16x9", "hi_16x9", "long_12min", "loop_200s")

#: Files a clone never inherits: per-run state of the SOURCE session.
_CLONE_DROP: tuple[str, ...] = ("prompt_run.json", "prompt_pending.json", "chat.json")
_CLONE_DROP_DIRS: tuple[str, ...] = ("cache/prompt_snap", "cache/verify", "previews", "exports")

#: The env the whole benchmark runs under (§6.1). Set for the process while
#: the app is open and restored afterwards.
BENCH_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": "",
    "HUGGINGFACE_TOKEN": "",
    "HF_HUB_OFFLINE": "1",
    "WHISPER_BACKEND": "faster_whisper",
    "WHISPER_MODEL": "small",
    "VAI_PROMPT_CLOUD": "0",
}

#: How many clarification rounds a single prompt may take before the runner
#: gives up (downloads + go + one slot question is the realistic maximum).
MAX_CLARIFY_ROUNDS = 4

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})


# --------------------------------------------------------------------------
# egress guard
# --------------------------------------------------------------------------

class EgressAttempted(AssertionError):
    """A socket tried to reach a non-loopback address while the guard was on."""


def _host_of(address: Any) -> str | None:
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    if isinstance(address, (str, bytes)):
        return None            # AF_UNIX path
    return str(address)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return True
    h = host.strip("[]").lower()
    return h in _LOOPBACK_HOSTS or h.startswith("127.")


class EgressGuard:
    """Refuse every non-loopback socket connection for the guarded block and
    remember what was attempted (`attempts`), so a test can assert both that
    nothing leaked and that a deliberate `urlopen` DOES trip it."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._orig: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _record(self, host: str | None, via: str) -> None:
        with self._lock:
            self.attempts.append(f"{via}:{host}")
        raise EgressAttempted(f"outbound connection attempted via {via} to {host!r} — "
                              "the benchmark must run with zero network egress")

    def __enter__(self) -> "EgressGuard":
        guard = self
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex
        orig_create = socket.create_connection
        orig_gai = socket.getaddrinfo

        def connect(sock, address):
            host = _host_of(address)
            if sock.family != socket.AF_UNIX and not _is_loopback_host(host):
                guard._record(host, "connect")
            return orig_connect(sock, address)

        def connect_ex(sock, address):
            host = _host_of(address)
            if sock.family != socket.AF_UNIX and not _is_loopback_host(host):
                guard._record(host, "connect_ex")
            return orig_connect_ex(sock, address)

        def create_connection(address, *a, **k):
            host = _host_of(address)
            if not _is_loopback_host(host):
                guard._record(host, "create_connection")
            return orig_create(address, *a, **k)

        def getaddrinfo(host, *a, **k):
            if not _is_loopback_host(None if host is None else str(host)):
                guard._record(str(host), "getaddrinfo")
            return orig_gai(host, *a, **k)

        self._orig = {"connect": orig_connect, "connect_ex": orig_connect_ex,
                      "create_connection": orig_create, "getaddrinfo": orig_gai}
        socket.socket.connect = connect            # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex      # type: ignore[method-assign]
        socket.create_connection = create_connection
        socket.getaddrinfo = getaddrinfo
        return self

    def __exit__(self, *exc: Any) -> None:
        socket.socket.connect = self._orig["connect"]            # type: ignore[method-assign]
        socket.socket.connect_ex = self._orig["connect_ex"]      # type: ignore[method-assign]
        socket.create_connection = self._orig["create_connection"]
        socket.getaddrinfo = self._orig["getaddrinfo"]


# --------------------------------------------------------------------------
# session cloning (pure filesystem work; tested on its own)
# --------------------------------------------------------------------------

def _rewrite_json_paths(root: Path, old_prefix: str, new_prefix: str) -> int:
    """Replace `old_prefix` with `new_prefix` in every .json under `root`
    (text replacement on both the plain and the JSON-escaped spelling, so a
    Windows path with `\\\\` survives). Returns how many files changed."""
    changed = 0
    escaped_old, escaped_new = json.dumps(old_prefix)[1:-1], json.dumps(new_prefix)[1:-1]
    for p in root.rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        new = text.replace(escaped_old, escaped_new)
        if escaped_old != old_prefix:
            new = new.replace(old_prefix, new_prefix)
        if new != text:
            p.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def clone_session_dir(src: Path, dst: Path) -> Path:
    """Copy a session directory to `dst` and re-point every absolute path in
    its JSON files (EDL, ops, snapshots, ingest, meta) from `src` to `dst`.
    Per-run state (prompt_run.json, pending, chat, verify/prompt caches) is
    not copied. `dst` must not exist."""
    if dst.exists():
        raise FileExistsError(f"clone target exists: {dst}")
    ignore = shutil.ignore_patterns(*_CLONE_DROP)
    shutil.copytree(src, dst, ignore=ignore)
    for sub in _CLONE_DROP_DIRS:
        shutil.rmtree(dst / sub, ignore_errors=True)
    for sub in ("uploads", "previews", "exports", "cache", "snapshots"):
        (dst / sub).mkdir(exist_ok=True)
    _rewrite_json_paths(dst, str(src.resolve()), str(dst.resolve()))
    if str(src) != str(src.resolve()):
        _rewrite_json_paths(dst, str(src), str(dst))
    return dst


# --------------------------------------------------------------------------
# SSE parsing + the prompt runner's result
# --------------------------------------------------------------------------

def parse_sse(text: str) -> list[dict[str, Any]]:
    """`data: <json>\\n\\n` frames → event dicts, in order. A frame that is not
    JSON is kept as `{"type": "_unparseable", "raw": …}` so the contract test
    sees it rather than a silent drop."""
    out: list[dict[str, Any]] = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        for line in frame.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            try:
                out.append(json.loads(payload))
            except ValueError:
                out.append({"type": "_unparseable", "raw": payload})
    return out


@dataclass
class PromptRun:
    """Everything one prompt (plus its clarification rounds) produced."""
    sid: str
    prompt: str
    turns: list[list[dict[str, Any]]] = field(default_factory=list)
    answers_given: list[dict[str, Any]] = field(default_factory=list)
    wall_s: float = 0.0
    http_status: list[int] = field(default_factory=list)

    @property
    def events(self) -> list[dict[str, Any]]:
        return [e for turn in self.turns for e in turn]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == kind]

    @property
    def plans(self) -> list[dict[str, Any]]:
        return [e["plan"] for e in self.of_type("plan")]

    @property
    def plan(self) -> dict[str, Any] | None:
        plans = self.plans
        return plans[-1] if plans else None

    @property
    def brain(self) -> str | None:
        answered = [e for e in self.of_type("brain") if e.get("status") == "answered"]
        return answered[-1].get("brain") if answered else None

    @property
    def content_brain(self) -> str | None:
        return (self.plan or {}).get("content_brain")

    @property
    def verify(self) -> dict[str, Any] | None:
        vs = self.of_type("verify")
        return vs[-1] if vs else None

    @property
    def ops(self) -> list[dict[str, Any]]:
        return self.of_type("op")

    @property
    def errors(self) -> list[str]:
        return [str(e.get("message")) for e in self.of_type("error")]

    @property
    def clarifies(self) -> list[dict[str, Any]]:
        return self.of_type("clarify")

    @property
    def first_text(self) -> str:
        texts = self.of_type("text_delta")
        return str(texts[0].get("text", "")) if texts else ""

    @property
    def reply(self) -> str:
        texts = self.of_type("text_delta")
        return str(texts[-1].get("text", "")) if texts else ""

    @property
    def done_count(self) -> int:
        return len(self.of_type("done"))

    def steps(self, status: str | None = None) -> list[dict[str, Any]]:
        """Final `step` event per (turn, index) — the last status a step
        reached — optionally filtered by that status."""
        final: dict[tuple[int, int], dict[str, Any]] = {}
        for t, turn in enumerate(self.turns):
            for e in turn:
                if e.get("type") == "step":
                    final[(t, int(e.get("index", -1)))] = e
        rows = list(final.values())
        return [s for s in rows if status is None or s.get("status") == status]

    def ran(self, tool: str) -> bool:
        return any(s.get("tool") == tool for s in self.steps("ok"))

    def tool_uses(self, tool: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.of_type("tool_use") if tool is None or e.get("name") == tool]

    @property
    def skipped_tools(self) -> list[str]:
        return [str(s.get("tool")) for s in self.steps("skipped")]


# --------------------------------------------------------------------------
# the app under test
# --------------------------------------------------------------------------

@dataclass
class FixtureSession:
    name: str
    sid: str
    media_path: str
    narration: Narration | None
    upload_s: float
    transcribe_s: float | None
    words: int | None


@dataclass
class BenchEnv:
    workdir: Path
    media: MediaSet
    client: Any                                   # fastapi.testclient.TestClient
    egress: EgressGuard
    brain: str | None = None
    fixtures: dict[str, FixtureSession] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    _clones: int = 0

    # --- sessions -------------------------------------------------------------
    def session_dir(self, sid: str) -> Path:
        return self.workdir / sid

    def store(self, sid: str):
        """The app's own (LRU-cached) store for `sid` — what a run mutates."""
        from video_ai_editor import main as _main
        return _main._store(sid)

    def new_session(self, name: str) -> str:
        r = self.client.post("/api/sessions", json={"name": name})
        r.raise_for_status()
        return r.json()["id"]

    def dispatch(self, sid: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(f"/api/sessions/{sid}/dispatch", json={"tool": tool, "args": args})
        if r.status_code != 200:
            raise RuntimeError(f"dispatch {tool} → {r.status_code}: {r.text[:400]}")
        return r.json()

    def upload_video(self, sid: str, path: Path) -> dict[str, Any]:
        with path.open("rb") as f:
            r = self.client.post(f"/api/sessions/{sid}/upload",
                                 files={"file": (path.name, f, "video/mp4")},
                                 data={"add_to_timeline": "true", "transcribe": "false"})
        if r.status_code != 200:
            raise RuntimeError(f"upload {path.name} → {r.status_code}: {r.text[:400]}")
        return r.json()

    def upload_audio(self, sid: str, path: Path, *, add_to_music: bool = False) -> dict[str, Any]:
        with path.open("rb") as f:
            r = self.client.post(f"/api/sessions/{sid}/audio_upload",
                                 files={"file": (path.name, f, "audio/wav")},
                                 data={"add_to_music": "true" if add_to_music else "false"})
        if r.status_code != 200:
            raise RuntimeError(f"audio_upload {path.name} → {r.status_code}: {r.text[:400]}")
        return r.json()

    # --- fixtures -------------------------------------------------------------
    def fixture(self, name: str) -> FixtureSession:
        """The uploaded + transcribed session for a media variant, built on
        first request. Raises `pytest.skip` for a Hindi variant without a
        local Hindi voice."""
        if name not in FIXTURES:
            raise KeyError(f"unknown fixture {name!r}; one of {FIXTURES}")
        if name not in self.fixtures:
            self.fixtures[name] = self._build_fixture(name)
        return self.fixtures[name]

    def _media_for(self, name: str) -> tuple[Path, Narration | None, bool]:
        """(video path, narration ground truth, transcribe?) per variant."""
        m = self.media
        if name in ("en_16x9", "presplit_16x9"):
            return Path(m.video_16x9), m.narration, True
        if name == "en_9x16":
            return Path(m.video_9x16), m.narration, True
        if name == "hi_16x9":
            try:
                path, nar = m.hindi()
            except HindiVoiceUnavailable as e:
                pytest.skip(str(e))
            return path, nar, True
        if name == "long_12min":
            # The run-time gate (case 23) needs a long timeline, not a
            # transcript: `transcribe` on 12 minutes IS the > 90 s estimate.
            return m.long_fixture(), None, False
        return Path(m.loop_fixture), None, False        # loop_200s

    def _build_fixture(self, name: str) -> FixtureSession:
        path, narration, transcribe = self._media_for(name)
        sid = self.new_session(f"bench {name}")
        t0 = time.monotonic()
        self.upload_video(sid, path)
        upload_s = time.monotonic() - t0
        transcribe_s: float | None = None
        words: int | None = None
        if transcribe:
            t1 = time.monotonic()
            res = self.dispatch(sid, "transcribe", {"model": "small"})
            transcribe_s = time.monotonic() - t1
            words = int(res.get("result", res).get("words", 0)) if isinstance(res, dict) else None
        if name == "presplit_16x9":
            for at in scene_cuts_for(self.media.duration):
                self.dispatch(sid, "split_at", {"track": "v1", "time": float(at)})
        if name == "loop_200s":
            self.upload_audio(sid, Path(self.media.bench_bed.path), add_to_music=False)
        self.timings[f"fixture:{name}:upload_s"] = round(upload_s, 2)
        if transcribe_s is not None:
            self.timings[f"fixture:{name}:transcribe_s"] = round(transcribe_s, 2)
        return FixtureSession(name=name, sid=sid, media_path=str(path), narration=narration,
                              upload_s=upload_s, transcribe_s=transcribe_s, words=words)

    def clone(self, fixture: str, *, label: str = "case") -> str:
        """A fresh session that is a byte-identical, self-contained copy of the
        fixture session (see the module docstring)."""
        from video_ai_editor import main as _main
        from video_ai_editor.storage import new_session_id
        src_sid = self.fixture(fixture).sid
        sid = new_session_id()
        clone_session_dir(self.session_dir(src_sid), self.session_dir(sid))
        meta = self.session_dir(sid) / "meta.json"
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
            data["name"] = f"{label} ({fixture})"
            meta.write_text(json.dumps(data), encoding="utf-8")
        _main._STORES.pop(sid, None)
        self._clones += 1
        return sid

    # --- transcript state helpers (cases 5 and 24) ------------------------------
    def ingest_json(self, sid: str) -> Path:
        from video_ai_editor.agent.dispatch import _current_v1_ingest_json
        p = _current_v1_ingest_json(self.store(sid))
        if p is None:
            raise FileNotFoundError(f"session {sid} has no ingest.json for its v1 clip")
        return p

    def delete_transcript(self, sid: str) -> dict[str, Any]:
        """Remove the persisted transcript from the session's ingest.json and
        return it (so a caller can put it back later). The mtime is pushed
        into the past so `transcript_pending` reads False (case 5: a real
        prerequisite, not a wait)."""
        p = self.ingest_json(sid)
        data = json.loads(p.read_text(encoding="utf-8"))
        tx = data.pop("transcript", None)
        data.pop("spoken_language", None)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        old = time.time() - 3600
        os.utime(p, (old, old))
        (self.session_dir(sid) / "transcript.json").unlink(missing_ok=True)
        return tx or {}

    def simulate_upload_transcript(self, sid: str, *, delay_s: float = 3.0) -> threading.Thread:
        """Case 24: make the session look like an upload whose background
        transcription is still running — no transcript, a fresh ingest.json —
        and write the transcript back after `delay_s`, the way
        `main.upload`'s `_bg_transcribe` does. Returns the writer thread."""
        p = self.ingest_json(sid)
        tx = self.delete_transcript(sid)
        now = time.time()
        os.utime(p, (now, now))

        def _land() -> None:
            time.sleep(delay_s)
            data = json.loads(p.read_text(encoding="utf-8"))
            data["transcript"] = tx
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        t = threading.Thread(target=_land, name="bench-upload-transcript", daemon=True)
        t.start()
        return t

    # --- the prompt runner -------------------------------------------------------
    def answer_for(self, question: dict[str, Any], *, allow_downloads: bool,
                   answers: dict[str, Any] | None) -> Any:
        """The auto-answer policy (§6.1): a case-supplied answer wins (case 23
        answers `go` with no); else `downloads` → skip unless the case allows
        them; `go` → yes; else the default; else the first option; else the
        smallest sensible value."""
        key = question.get("key")
        if answers and key in answers:
            return answers[key]
        if key == "downloads":
            return "yes" if allow_downloads else "no"
        if key == "go":
            return "yes"
        if question.get("default") is not None:
            return question["default"]
        options = question.get("options") or []
        if options:
            first = options[0]
            return first.get("value") if isinstance(first, dict) else first
        kind = question.get("kind")
        if kind in ("number", "duration"):
            return question.get("min") if question.get("min") is not None else 1
        if kind == "confirm":
            return "yes"
        return ""

    def _post_stream(self, url: str, body: dict[str, Any], run: PromptRun) -> list[dict[str, Any]]:
        r = self.client.post(url, json=body)
        run.http_status.append(r.status_code)
        if r.status_code != 200:
            raise RuntimeError(f"{url} → {r.status_code}: {r.text[:400]}")
        events = parse_sse(r.text)
        run.turns.append(events)
        return events

    def run_prompt(self, sid: str, prompt: str, *, allow_downloads: bool = False,
                   answers: dict[str, Any] | None = None, brain: str | None = None,
                   ui_state: dict[str, Any] | None = None) -> PromptRun:
        """POST the prompt, then answer every clarification it raises until
        the turn ends without one. The whole exchange is one `PromptRun`."""
        run = PromptRun(sid=sid, prompt=prompt)
        body: dict[str, Any] = {"message": prompt, **(ui_state or {})}
        chosen = brain or self.brain
        if chosen:
            body["brain"] = chosen
        t0 = time.monotonic()
        events = self._post_stream(f"/api/sessions/{sid}/prompt", body, run)
        rounds = 0
        while True:
            clarify = next((e for e in events if e.get("type") == "clarify"), None)
            if clarify is None:
                break
            rounds += 1
            if rounds > MAX_CLARIFY_ROUNDS:
                raise RuntimeError(f"prompt {prompt!r} asked more than {MAX_CLARIFY_ROUNDS} rounds of questions")
            reply = {q["key"]: self.answer_for(q, allow_downloads=allow_downloads, answers=answers)
                     for q in clarify.get("questions", [])}
            run.answers_given.append(reply)
            events = self._post_stream(f"/api/sessions/{sid}/prompt/answer",
                                       {"token": clarify["token"], "answers": reply}, run)
        run.wall_s = time.monotonic() - t0
        return run

    def prompt_run_record(self, sid: str) -> dict[str, Any] | None:
        r = self.client.get(f"/api/sessions/{sid}/prompt/run")
        return r.json().get("run") if r.status_code == 200 else None

    def brains_report(self) -> dict[str, Any]:
        r = self.client.get("/api/prompt/brains")
        r.raise_for_status()
        return r.json()


# --------------------------------------------------------------------------
# opening the app
# --------------------------------------------------------------------------

def _forbid_downloads(mp: pytest.MonkeyPatch) -> None:
    """Belt to the egress guard's braces: the library entry points the
    product would use to fetch a model or a voice can only resolve from the
    local cache. `huggingface_hub.snapshot_download` is NOT replaced outright
    — faster-whisper calls it to LOCATE a cached model too (`WhisperModel(
    "small")` → `download_model` → `snapshot_download`), so a stub that
    raises would break every transcription; forcing `local_files_only=True`
    keeps the cached path working and turns a fetch into a loud
    `LocalEntryNotFoundError` instead of a 480 MB side effect."""
    def _boom(*a: Any, **k: Any) -> None:
        raise EgressAttempted("a model/voice download was attempted from the prompt path")
    try:
        import huggingface_hub
        real = huggingface_hub.snapshot_download

        def _local_only(*a: Any, **k: Any):
            return real(*a, **{**k, "local_files_only": True})
        mp.setattr(huggingface_hub, "snapshot_download", _local_only)
        try:
            import faster_whisper.utils as _fwu
            mp.setattr(_fwu.huggingface_hub, "snapshot_download", _local_only, raising=False)
        except (ImportError, AttributeError):
            pass
    except ImportError:
        pass
    try:
        import piper.download_voices as dv
        mp.setattr(dv, "download_voice", _boom)
    except ImportError:
        pass
    from video_ai_editor.ai import tts as _tts
    mp.setattr(_tts, "download_voice", _boom, raising=False)


@contextmanager
def open_bench(workdir: Path, *, media: MediaSet | None = None, brain: str | None = None,
               guard_egress: bool = True) -> Iterator[BenchEnv]:
    """The app under test: WORKDIR redirected to `workdir`, the benchmark env
    applied, the desktop path posture (allowlist OFF, as the shipped app),
    the store cache emptied, the preset beds present, and — unless
    `guard_egress=False` — the socket guard armed for the whole block."""
    from fastapi.testclient import TestClient
    from video_ai_editor import config, main as _main, storage
    from video_ai_editor.ai import features as _features

    media = media or build_media_set()
    ensure_preset_beds()
    mp = pytest.MonkeyPatch()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    for k, v in BENCH_ENV.items():
        mp.setenv(k, v)
    if brain:
        mp.setenv("VAI_BRAIN", brain)
    else:
        mp.delenv("VAI_BRAIN", raising=False)
    mp.setattr(config, "WORKDIR", workdir)
    mp.setattr(storage, "WORKDIR", workdir)
    mp.setattr(_main, "WORKDIR", workdir)
    mp.setattr(_main, "_STORES", OrderedDict())
    mp.setattr(_features, "_REPORT_CACHE", None)
    _forbid_downloads(mp)
    restrict_before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    guard = EgressGuard()
    try:
        if guard_egress:
            guard.__enter__()
        with TestClient(_main.app) as client:
            yield BenchEnv(workdir=workdir, media=media, client=client, egress=guard,
                           brain=brain or (os.environ.get("VAI_BRAIN") or None))
    finally:
        if guard_egress:
            guard.__exit__(None, None, None)
        config.enable_path_restriction(restrict_before)
        mp.undo()


def requirement_missing(name: str) -> str | None:
    """Why a case requirement cannot be met on this machine, or None. Cheap
    `Path.exists` probes only — never a load, never a download."""
    if name == "madlad":
        from video_ai_editor.ai import translate as _tr
        d = _tr._model_dir()
        return None if d.exists() and any(d.iterdir()) else f"MADLAD not cached at {d} (~3 GB; the benchmark never downloads)"
    if name == "hindi_voice":
        from .narration import hindi_backend
        return None if hindi_backend() else "no Hindi TTS voice (Piper hi_IN not cached, macOS `say -v Lekha` absent)"
    if name == "whisper_small":
        from video_ai_editor.agent.dispatch import whisper_model_on_disk
        return None if whisper_model_on_disk("small") else "faster-whisper small is not cached (480 MB; the benchmark never downloads)"
    if name == "validate_module":
        try:
            import video_ai_editor.agent.prompt.validate  # noqa: F401
        except ImportError as e:
            return f"agent/prompt/validate.py is not importable ({e}) — every executed plan must pass it first"
        return None
    raise KeyError(f"unknown requirement {name!r}")


__all__ = ["FIXTURES", "BENCH_ENV", "MAX_CLARIFY_ROUNDS", "EgressAttempted", "EgressGuard",
           "clone_session_dir", "parse_sse", "PromptRun", "FixtureSession", "BenchEnv",
           "open_bench", "requirement_missing"]
