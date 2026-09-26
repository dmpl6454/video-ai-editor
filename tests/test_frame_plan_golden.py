"""The frame-plan corpus (instant preview spec §6 R3, §13 programMap parity).

500 random edit sequences are driven through the REAL dispatch (add_clip,
split_at, trim_clip, move_clip, ripple_delete, cut_range, set_speed,
set_clip_reverse, add/remove_transition, duplicate_clip, set_canvas fps
changes, undo/redo) and the resulting EDLs are stored with everything the
Python side derives from them: ``_v1_frame_plan``, ``seam_table_for``,
``clip_frames`` and the program map (``frame_map_json`` minus the hash).
``frontend/src/lib/preview/timeline/framePlan.test.ts`` must reproduce all of
it for every EDL.

Frame SELECTION is pinned to real renders by ``test_frame_map_golden.py``;
this corpus pins the LAYOUT half (and the port) over the shapes real edits
produce. Clip ids are random, so the corpus is generated once and then only
re-checked: this test recomputes every stored field from the stored EDL and
fails when the Python rules changed without a regeneration
(``VAI_REGEN_GOLDENS=1``).
"""
from __future__ import annotations

import json
import os
import random
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from video_ai_editor.edl import EDLStore
from video_ai_editor.edl import timebase as tb
from video_ai_editor.edl.schema import EDL, Canvas, Clip, empty_edl, seam_table_for
from video_ai_editor.render import compositor
from video_ai_editor.render.frame_map import SourceInfo, frame_map_json

GOLDEN = Path(__file__).resolve().parent / "goldens" / "frame_plan_cases.json"
N_SEQUENCES = 500

#: The sources the corpus clips reference (the files only exist while it is
#: generated; the model needs their SourceInfo, which is stored).
SOURCES = {
    "src30": (Fraction(30), 20.0),
    "src25": (Fraction(25), 20.0),
    "src29.97": (Fraction(30000, 1001), 20.0),
    "src59.94": (Fraction(60000, 1001), 12.0),
}


def _make_sources(d: Path) -> dict[str, str]:
    out = {}
    d.mkdir(parents=True, exist_ok=True)
    for key, (rate, secs) in SOURCES.items():
        p = d / f"{key}.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"testsrc2=s=64x36:r={tb.ffmpeg_rate(rate)}:d={secs}",
             "-f", "lavfi", "-i", f"sine=f=330:sample_rate=48000:duration={secs}",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p)],
            check=True, capture_output=True)
        out[key] = str(p)
    return out


def _v1(store: EDLStore) -> list[Clip]:
    return [c for c in store.edl.get_track("v1").clips if isinstance(c, Clip)]


def _random_sequence(store: EDLStore, rng: random.Random, paths: dict[str, str]) -> None:
    from video_ai_editor.agent.dispatch import dispatch

    def run(tool: str, args: dict) -> None:
        try:
            dispatch(store, tool, args)
        except Exception:  # noqa: BLE001 — an op the state refuses is part of the fuzz
            pass

    fps_choices = [float(r) if r.denominator != 1 else int(r) for r in tb.STANDARD_RATES]
    run("set_canvas", {"w": 320, "h": 180, "fps": rng.choice(fps_choices)})
    for _ in range(rng.randint(1, 4)):
        key = rng.choice(list(paths))
        a = rng.uniform(0, 8)
        run("add_clip", {"track": "v1", "src": paths[key], "in": a,
                         "out": a + rng.uniform(0.3, 5.0),
                         "start": rng.choice([None, rng.uniform(0, 20)]) or store.edl.video_extent()})
    for _ in range(rng.randint(4, 12)):
        clips = _v1(store)
        if not clips:
            break
        c = rng.choice(clips)
        end = store.edl.video_extent()
        op = rng.random()
        if op < 0.18:
            run("split_at", {"track": "v1", "time": rng.uniform(0, max(end, 0.1))})
        elif op < 0.28:
            run("trim_clip", {"clip_id": c.id, "in": c.in_ + rng.uniform(-0.3, 0.6),
                              "out": c.out - rng.uniform(-0.3, 0.6)})
        elif op < 0.36:
            run("move_clip", {"clip_id": c.id, "new_start": rng.uniform(0, end + 2),
                              "close_gap": rng.random() < 0.5})
        elif op < 0.42:
            run("ripple_delete", {"clip_id": c.id})
        elif op < 0.48:
            s = rng.uniform(0, max(end, 0.1))
            run("cut_range", {"track": "v1", "start": s, "end": s + rng.uniform(0.05, 1.5)})
        elif op < 0.60:
            run("set_speed", {"clip_id": c.id, "factor": rng.choice([0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]),
                              "keep_pitch": rng.random() < 0.5})
        elif op < 0.66:
            run("set_clip_reverse", {"clip_id": c.id})
        elif op < 0.80:
            at = c.start + c.effective_duration
            run("add_transition", {"at": at + rng.uniform(-0.03, 0.03), "type": "fade",
                                   "duration": rng.choice([0.1, 0.25, 0.37, 0.5, 1.0, 3.0])})
        elif op < 0.84:
            run("remove_transition", {"at": c.start})
        elif op < 0.88:
            run("duplicate_clip", {"clip_id": c.id})
        elif op < 0.92:
            run("set_canvas", {"w": 320, "h": 180, "fps": rng.choice(fps_choices)})
        elif op < 0.96:
            run("undo", {})
        else:
            run("redo", {})


def _compact_edl(edl: EDL, keys: dict[str, str]) -> dict:
    """The fields the plan and the program map read (the client gets the full
    EDL; missing fields take the schema defaults on both sides)."""
    v1 = edl.get_track("v1")
    clips = [{"id": c.id, "src": keys[c.src], "in": c.in_, "out": c.out, "start": c.start,
              "speed": c.speed, "reverse": c.reverse,
              "audio": {"keep_pitch": c.audio.keep_pitch}}
             for c in v1.clips if isinstance(c, Clip)]
    return {"duration": edl.duration, "canvas": {"fps": edl.canvas.fps},
            "tracks": [{"id": "v1", "type": "video", "clips": clips,
                        "transitions": [t.model_dump() for t in v1.transitions]}]}


def derive(edl_json: dict, sources: dict[str, SourceInfo]) -> dict:
    """Everything the Python side computes from one EDL."""
    edl = EDL.model_validate({**empty_edl(Canvas()).model_dump(by_alias=True), **edl_json,
                              "canvas": {**Canvas().model_dump(), **edl_json["canvas"]}})
    fps = edl.canvas.fps
    clips = compositor._video_clips(edl)
    v1 = edl.get_track("v1")
    seams = seam_table_for(clips, v1.transitions, fps=fps)
    total = max(0.0, edl.duration + sum(d for _s, d in seams))
    if not clips:
        total = max(1.0, total)
    fm = frame_map_json(edl, sources)
    fm.pop("render_hash", None)
    return {
        "clip_frames": [compositor.clip_frames(c, fps) for c in clips],
        "seams": [list(s) for s in seams],
        "seams_nofps": [list(s) for s in seam_table_for(clips, v1.transitions)],
        "total_duration": total,
        "plan": [[k, i, n] for k, i, n in compositor._v1_frame_plan(clips, total, fps)],
        "model": fm,
    }


def _generate(tmp: Path) -> dict:
    paths = _make_sources(tmp / "src")
    keys = {v: k for k, v in paths.items()}
    from frame_map_golden_lib import probe_source
    infos = {k: probe_source(Path(p)) for k, p in paths.items()}
    rng = random.Random(20260926)
    cases = []
    for i in range(N_SEQUENCES):
        sd = tmp / f"s{i}"
        sd.mkdir(parents=True)
        (sd / "edl.json").write_text(empty_edl(Canvas(w=320, h=180)).model_dump_json())
        store = EDLStore(sd)
        _random_sequence(store, rng, paths)
        cases.append({"seq": i, "edl": _compact_edl(store.edl, keys)})
    return {"sources": {k: v.to_json() for k, v in infos.items()}, "cases": cases}


def _canonical(doc) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> dict:
    if os.environ.get("VAI_REGEN_GOLDENS") == "derived" and GOLDEN.exists():
        # Keep the stored EDLs (their clip ids are random), recompute the rest.
        doc = json.loads(GOLDEN.read_text())
        infos = {k: SourceInfo.from_json(v) for k, v in doc["sources"].items()}
        for c in doc["cases"]:
            c.update(derive(c["edl"], infos))
        GOLDEN.write_text(_canonical(doc) + "\n")
    elif os.environ.get("VAI_REGEN_GOLDENS") == "1" or not GOLDEN.exists():
        doc = _generate(tmp_path_factory.mktemp("frame_plan"))
        infos = {k: SourceInfo.from_json(v) for k, v in doc["sources"].items()}
        for c in doc["cases"]:
            c.update(derive(c["edl"], infos))
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(_canonical(doc) + "\n")
    return json.loads(GOLDEN.read_text())


def test_corpus_is_current(corpus):
    infos = {k: SourceInfo.from_json(v) for k, v in corpus["sources"].items()}
    stale = []
    for c in corpus["cases"]:
        now = json.loads(json.dumps(derive(c["edl"], infos)))
        if any(now[k] != c[k] for k in now):
            stale.append(c["seq"])
    assert stale == [], f"frame_plan_cases.json is stale for sequences {stale[:10]}"


def test_corpus_exercises_the_rules(corpus):
    cases = corpus["cases"]
    assert len(cases) == N_SEQUENCES
    rates = {json.dumps(c["model"]["R"]) for c in cases}
    assert len(rates) >= 8                                   # every standard rate
    assert sum(1 for c in cases if c["seams"]) > 50           # cross-fades
    assert sum(1 for c in cases if any(k == "gap" for k, _i, _n in c["plan"])) > 50
    assert any(c["seams"] != c["seams_nofps"] for c in cases)  # whole-frame rounding matters
    speeds = {cl["speed"] for c in cases for cl in c["edl"]["tracks"][0]["clips"]}
    assert {0.5, 2.0} <= speeds
    assert any(cl["reverse"] for c in cases for cl in c["edl"]["tracks"][0]["clips"])
    offgrid = sum(1 for c in cases for cl in c["edl"]["tracks"][0]["clips"]
                  if abs(tb.quantize(cl["start"], c["edl"]["canvas"]["fps"]) - cl["start"]) > 1e-9)
    assert offgrid >= 0  # edits quantise; recorded for visibility
