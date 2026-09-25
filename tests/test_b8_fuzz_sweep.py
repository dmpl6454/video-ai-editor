"""QA-107: malformed API requests must be a clean 4xx, never a 500.

Two sweeps, both driving real code:

* **every HTTP route** in the app, each method, with fuzzed bodies (text/plain
  junk, broken JSON, a JSON array/null, wrong-typed fields, a junk multipart)
  and fuzzed query strings;
* **every dispatch tool** x every advertised argument x a battery of wrong
  values (null, the wrong JSON type, a dunder string, absurd numbers), through
  the same `_dispatch_sync` the `/dispatch` route uses, so the status code
  asserted here is the one a client would see.

Anything >= 500 is a failure. The three reported repros (`split_at time=null`,
`set_property path='__class__'`, a JSON body sent as text/plain) are pinned
explicitly as well so a regression names itself.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from video_ai_editor import platformutil as _pu

# Routes whose success path would do something this sweep must not do: pull a
# model (the prompt model manager), start an LLM/brain run, or open a stream.
_SKIP_ROUTE = ("/download", "/chat", "/prompt", "/mcp", "/api/pair")

# Tools that load an ML model, call a network API or run for minutes when
# they get as far as their real work. They are still swept with values the
# boundary validator must REJECT (a wrong JSON type), which is exactly the
# class of bug this item is about, but never with values that could run them.
_HEAVY = {
    "find_moments", "match_style", "generate_hook", "remove_background",
    "object_erase", "upscale", "smooth_slow_motion", "stabilize",
    "auto_reframe", "vocal_isolate", "instrumental_isolate", "tts_voiceover",
    "diarize", "assign_caption_speakers", "translate_captions", "make_shorts",
    "multicam", "motion_track", "noise_reduce", "auto_cut_to_beats",
    "auto_caption", "transcribe", "search_media", "render_preview",
    "detect_scenes", "describe_video", "denoise_audio", "enhance_voice",
    "auto_edit", "find_broll",
}


def _lavfi(dst: Path, *args: str) -> Path:
    subprocess.run([_pu.FFMPEG, "-loglevel", "error", "-y", *args, str(dst)], check=True)
    return dst


@pytest.fixture()
def app_client(monkeypatch, tmp_path: Path):
    from video_ai_editor import storage as _storage
    monkeypatch.setattr(_storage, "WORKDIR", tmp_path / "wd")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # save_show_template writes to the project-global presets dir; keep the
    # sweep's fuzzed names (None.json, 1e12.json, ...) out of the checkout.
    from video_ai_editor.show import templates as _templates
    shutil.copytree(_templates.PRESETS_DIR / "shows", tmp_path / "presets" / "shows")
    monkeypatch.setattr(_templates, "PRESETS_DIR", tmp_path / "presets")
    from video_ai_editor import main as _main
    importlib.reload(_main)
    monkeypatch.setattr(_main, "WORKDIR", tmp_path / "wd")
    _main._STORES.clear()
    return _main, TestClient(_main.app, raise_server_exceptions=False)


@pytest.fixture()
def seeded(app_client, tmp_path: Path):
    main, client = app_client
    vid = _lavfi(tmp_path / "v.mp4", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    sid = client.post("/api/sessions").json()["id"]
    with vid.open("rb") as f:
        r = client.post(f"/api/sessions/{sid}/upload", files={"file": ("v.mp4", f, "video/mp4")},
                        data={"transcribe": "false"})
    assert r.status_code == 200, r.text
    return main, client, sid


def _clip_id(client, sid) -> str:
    edl = client.get(f"/api/sessions/{sid}/edl").json()
    return next(c["id"] for t in edl["tracks"] if t["id"] == "v1" for c in t["clips"])


# ---------------------------------------------------------------------------
# The three reported repros


def test_split_at_null_time_is_400(seeded):
    _, client, sid = seeded
    r = client.post(f"/api/sessions/{sid}/dispatch", json={"tool": "split_at", "args": {"time": None}})
    assert r.status_code == 400, r.text
    assert "time" in r.json()["error"]["message"]


def test_set_property_refuses_dunder_and_private_paths(seeded):
    _, client, sid = seeded
    cid = _clip_id(client, sid)
    for path in ("__class__", "transform.__dict__", "_private", "model_config", "transform.model_fields"):
        r = client.post(f"/api/sessions/{sid}/dispatch",
                        json={"tool": "set_property", "args": {"clip_id": cid, "path": path, "value": 1}})
        assert r.status_code == 400, (path, r.text)
    # The ordinary use still works.
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "set_property", "args": {"clip_id": cid, "path": "transform.scale", "value": 1.5}})
    assert r.status_code == 200, r.text


def test_json_sent_as_text_plain_is_422_with_serialisable_details(seeded):
    _, client, sid = seeded
    r = client.post(f"/api/sessions/{sid}/dispatch", content=b'{"tool":"get_timeline","args":{}}',
                    headers={"content-type": "text/plain"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"], err


# ---------------------------------------------------------------------------
# Sweep 1: every HTTP route


_BODIES: list[tuple[str, dict]] = [
    ("text/plain junk", {"content": b"\xff\xfe not json at all", "headers": {"content-type": "text/plain"}}),
    ("json as text/plain", {"content": b'{"tool":"get_timeline"}', "headers": {"content-type": "text/plain"}}),
    ("broken json", {"content": b'{"tool": ', "headers": {"content-type": "application/json"}}),
    ("json null", {"content": b"null", "headers": {"content-type": "application/json"}}),
    ("json array", {"content": b"[1, 2, 3]", "headers": {"content-type": "application/json"}}),
    ("wrong types", {"json": {"tool": 5, "args": "x", "message": None, "height": "tall", "fps": [],
                              "crf": {}, "src": None, "file": 3, "codes": "x"}}),
    ("junk multipart", {"files": {"file": ("x.bin", b"\x00\x01garbage", "application/octet-stream")},
                        "data": {"start": "abc", "add_to_timeline": "maybe", "gain_db": "loud",
                                 "duck": "perhaps", "volume_db": "nan"}}),
    ("empty multipart", {"data": {"x": "1"}}),
]
_QUERY = "?wait=abc&since=x&t=nan&h=-5&peaks_per_sec=zz&src=%00&refresh=q"


def _fill(path: str, sid: str) -> str:
    return (path.replace("{sid}", sid).replace("{job_id}", "nope").replace("{media_id}", "0123456789ab")
            .replace("{clip_id}", "nope").replace("{kind}", "uploads").replace("{name:path}", "..%2f..%2fetc")
            .replace("{name}", "nope").replace("{seq}", "zz"))


def test_every_route_answers_fuzzed_requests_without_a_500(seeded):
    main, client, sid = seeded
    failures: list[str] = []
    seen = 0
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or any(s in route.path for s in _SKIP_ROUTE):
            continue
        url = _fill(route.path, sid)
        if "{" in url:
            failures.append(f"unfilled path param in {route.path}")
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if method == "DELETE" and url.rstrip("/") == f"/api/sessions/{sid}":
                continue  # would delete the fixture session mid-sweep
            cases = [("no body + junk query", {})] if method == "GET" else _BODIES
            for label, kw in cases:
                r = client.request(method, url + _QUERY, **kw)
                seen += 1
                if r.status_code >= 500:
                    failures.append(f"{method} {route.path} [{label}] -> {r.status_code} {r.text[:160]}")
    assert seen > 60, seen
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Sweep 2: every dispatch tool x every argument x a battery of bad values


_CANDIDATES = [{"x": 1}, [1, 2], True, "not-a-number", 3.5]


def _mismatch(declared) -> list:
    """Values that satisfy NONE of the declared JSON types (so the boundary
    validator must reject them before any handler runs)."""
    from video_ai_editor.agent.dispatch import _arg_type_ok
    kinds = declared if isinstance(declared, list) else [declared]
    return [v for v in _CANDIDATES if not any(_arg_type_ok(v, k) for k in kinds)]


_BATTERY = [None, "__class__", -1e12, 1e12, [], {}, "", "../../../../etc/passwd", "‮\U0001F525"]


def test_every_dispatch_tool_rejects_bad_arguments_without_a_500(seeded, tmp_path: Path):
    main, client, sid = seeded
    from video_ai_editor.agent.dispatch import DISPATCH
    from video_ai_editor.agent.tools import ALL_TOOLS
    from video_ai_editor.edl import EDLStore
    schemas = {t["name"]: t["input_schema"] for t in ALL_TOOLS}
    base = main.session_dir(sid)
    failures: list[str] = []
    calls = 0
    for i, tool in enumerate(sorted(DISPATCH)):
        work = tmp_path / f"t{i}"
        shutil.copytree(base, work)
        store = EDLStore(work)
        props = (schemas.get(tool) or {}).get("properties") or {}
        cases: list[dict] = []
        for key, spec in props.items():
            declared = spec.get("type") if isinstance(spec, dict) else None
            if declared is not None:
                cases += [{key: v} for v in _mismatch(declared)]
            if tool not in _HEAVY:
                cases += [{key: v} for v in _BATTERY]
        if tool not in _HEAVY:
            cases.append({k: None for k in props})
        for args in cases:
            calls += 1
            try:
                main._dispatch_sync(sid, store, main.DispatchRequest(tool=tool, args=dict(args)))
            except HTTPException as e:
                if e.status_code >= 500:
                    failures.append(f"{tool} {args!r:.120} -> {e.status_code}")
            except Exception as e:  # anything else is what the route would 500 on
                failures.append(f"{tool} {args!r:.120} -> 500 {type(e).__name__}: {str(e)[:140]}")
    assert calls > 1000, calls
    assert not failures, f"{len(failures)} of {calls} calls would 500:\n" + "\n".join(failures)
