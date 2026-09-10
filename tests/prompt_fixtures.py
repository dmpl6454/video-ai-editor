"""Shared setup for the Prompt Editor tests (executor, verify, routes, chat).

Not a conftest.py on purpose — same reasoning as `lan_fixtures.py`: a handful
of files need the same synthetic session and nothing else in the suite does.

What is here:
  * `speech_clip` — a 12 s lavfi clip laid out the way `ingest_upload` writes
    it (`uploads/<name>/<name>.normalized.mp4`), tone in three windows so
    `remove_silences` has something to find;
  * `transcript` / `write_ingest` — a hand-written word-level transcript in
    SOURCE seconds beside the clip, so no Whisper model ever loads;
  * `make_store` — an EDLStore with that clip on v1 (via `add_clip`, so the
    path guard and the canvas rules run as they would for a real upload);
  * `facts_for` — a `TimelineFacts` for that store with the fields the
    executor and verifier read (ids, allowed paths, ingest path); it never
    calls `build_facts` so these tests stay independent of the planner;
  * `identity_validator` — stands in for `validate_plan` where the test is
    about execution, not validation (the security-boundary tests use the
    executor's own guard, which runs regardless);
  * `FakeRouted` / `route_with` — a stand-in for the brains router so a turn
    can be driven end to end with a known Plan;
  * `collect` — drain an async event generator into a list.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from video_ai_editor.agent.dispatch import dispatch
from video_ai_editor.agent.prompt.facts import TimelineFacts
from video_ai_editor.agent.prompt.schema import Plan, Postcondition, Step
from video_ai_editor.edl.snapshot import EDLStore

CLIP_DUR = 12.0
TONE_WINDOWS = ((0.0, 3.0), (5.0, 8.0), (10.0, 12.0))
WORDS = [
    ("so", 0.20, 0.50), ("um", 0.60, 0.90), ("hello", 1.00, 1.50), ("there", 1.60, 2.10),
    ("friends", 2.20, 2.80),
    ("uh", 5.50, 5.80), ("today", 6.00, 6.50), ("we", 6.60, 6.80), ("start", 7.00, 7.60),
    ("um", 10.50, 10.80), ("goodbye", 11.00, 11.60),
]


def speech_clip(root: Path, name: str = "talk", *, w: int = 320, h: int = 180,
                dur: float = CLIP_DUR) -> Path:
    d = root / "uploads" / name
    d.mkdir(parents=True, exist_ok=True)
    src = d / f"{name}.normalized.mp4"
    gate = "+".join(f"between(t\\,{a}\\,{b})" for a, b in TONE_WINDOWS)
    expr = f"0.6*sin(440*2*PI*t)*({gate})"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=blue:s={w}x{h}:d={dur}:r=30",
         "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={dur}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        check=True, capture_output=True)
    return src


def music_bed(root: Path, name: str = "bed.wav", *, dur: float = 4.0, freq: int = 200) -> Path:
    d = root / "uploads" / "audio"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=f={freq}:duration={dur}", "-ar", "48000", str(p)],
                   check=True, capture_output=True)
    return p


def transcript(words=WORDS, duration: float = CLIP_DUR, language: str = "en") -> dict:
    text = " ".join(w for w, _, _ in words)
    return {"language": language, "duration": duration,
            "segments": [{"id": 0, "start": words[0][1], "end": words[-1][2], "text": text,
                          "words": [{"word": w, "start": s, "end": e, "prob": 1.0}
                                    for w, s, e in words]}]}


def write_ingest(src: Path, tx: dict | None = None, *, with_transcript: bool = True) -> Path:
    p = src.parent / "ingest.json"
    body: dict[str, Any] = {"src": str(src)}
    if with_transcript:
        body["transcript"] = tx or transcript()
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def make_store(root: Path, *, src: Path | None = None, with_transcript: bool = True,
               name: str = "sess") -> EDLStore:
    store = EDLStore(root / name)
    src = src or speech_clip(root)
    if not (src.parent / "ingest.json").exists():
        write_ingest(src, with_transcript=with_transcript)
    dispatch(store, "add_clip", {"track": "v1", "src": str(src), "in": 0, "out": CLIP_DUR, "start": 0})
    return store


def facts_for(store: EDLStore, **over: Any) -> TimelineFacts:
    from video_ai_editor.agent.dispatch import _current_v1_ingest_json
    edl = store.edl
    v1 = edl.get_track("v1")
    v1_ids = [c.id for c in (v1.clips if v1 else [])]
    all_ids = [c.id for t in edl.tracks for c in t.clips]
    uploads = Path(store.dir).parent / "uploads"
    allowed = {str(p.resolve()) for p in uploads.rglob("*") if p.is_file()} if uploads.exists() else set()
    ing = _current_v1_ingest_json(store)
    base = dict(
        session_id=Path(store.dir).name, duration=edl.duration or CLIP_DUR,
        canvas_w=edl.canvas.w, canvas_h=edl.canvas.h, fps=edl.canvas.fps,
        aspect="16:9" if edl.canvas.w > edl.canvas.h else "9:16",
        v1_clip_ids=v1_ids, clip_ids=all_ids, track_ids=[t.id for t in edl.tracks],
        has_transcript=ing is not None, allowed_paths=allowed,
        ingest_json_path=str(ing) if ing else None, loudness_lufs=edl.canvas.loudness_lufs,
    )
    return TimelineFacts(**{**base, **over})


def identity_validator(plan: Plan, facts: TimelineFacts) -> Plan:
    return plan


def step(tool: str, **args: Any) -> Step:
    optional = bool(args.pop("_optional", False))
    return Step(tool=tool, args=args, why=f"test {tool}", optional=optional)


def plan_of(*steps: Step, intent: str = "test", brain: str = "recipes",
            postconditions: list[Postcondition] | None = None, **fields: Any) -> Plan:
    return Plan.new(intent=intent, brain=brain, steps=list(steps),
                    postconditions=postconditions or [], **fields)


class FakeRouted(SimpleNamespace):
    """The subset of `brains.router.RoutedPlan` the service reads."""

    def __init__(self, plan: Plan | None, *, brain: str | None = None, clarify: Any = None,
                 note: str = "", emit_events: bool = True):
        super().__init__(plan=plan, brain=brain or (plan.brain if plan else None),
                         attempts=(), clarify=clarify, note=note, emit_events=emit_events)


def route_with(monkeypatch, routed: FakeRouted):
    """Make `service._route` answer with `routed`, emitting the brain events
    the real router would (trying → answered for the plan's brain)."""
    from video_ai_editor.agent.prompt import service

    def _fake_route(req, *, brain, emit):
        b = routed.brain or "recipes"
        if routed.emit_events:
            emit(service._brain_event("trying", b))
            emit(service._brain_event("answered" if routed.plan is not None else "failed", b,
                                      latency_ms=1))
        return routed

    monkeypatch.setattr(service, "_route", _fake_route)


def collect(agen) -> list[dict]:
    async def _go():
        return [e async for e in agen]
    return asyncio.run(_go())


@pytest.fixture
def desktop_posture():
    """Filesystem allowlist OFF, as the shipped desktop runs (see
    tests/test_transcript_timemap.py::_desktop_path_posture for why a Mac that
    paired a phone would otherwise arm it for the whole process)."""
    from video_ai_editor import config
    before = config._FORCED_RESTRICT
    config.enable_path_restriction(False)
    try:
        yield
    finally:
        config.enable_path_restriction(before)


@pytest.fixture
def no_downloads(monkeypatch):
    """Any download attempt in the prompt path is a test failure."""
    def _boom(*a, **k):
        raise AssertionError("network download attempted from the prompt path")
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _boom)
    try:
        import piper.download_voices as dv
        monkeypatch.setattr(dv, "download_voice", _boom)
    except ImportError:
        pass
    from video_ai_editor.ai import tts as _tts
    monkeypatch.setattr(_tts, "download_voice", _boom, raising=False)
