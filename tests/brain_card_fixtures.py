"""Hand-written Editor Brain artefacts in the EB1 frozen shapes, for the
surfaces suites (tests/test_brain_card_payload.py, tests/test_brain_card_ui.py).

The EDP is what lane E's planner writes to `<session>/brain/decisions/<did>.json`
(EB1_BRIEF "Frozen contracts": EDP, Decision, the reason codes); the
footprint is what lane C's resolver records per decision while a plan runs
(`<scratch>/brain/footprint.json`: clip ids, source spans, keyframe times,
cue ranges, marker ids). Nothing here is read by product code — the
surfaces only READ these files, and until the other lanes land these are
the only instances of them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DID = "d_0a1b2c3d"
GID = "feedbeef0001"


def edp(src: str, *, did: str = DID, content_brain: str | None = "apple_intelligence") -> dict[str, Any]:
    """A 45 s talking-head reel: three cuts by reason, one kept pause, the
    hook, captions, a bed, and what this wave does not do."""
    return {
        "version": 1, "id": did, "planner_version": 1, "created": "2026-09-29T12:00:00Z",
        "graph": {"id": GID, "digest": "0" * 12},
        "controls": {"content_type": "auto", "energy": 5, "captions": "dynamic", "music": "subtle",
                     "duration_s": 45, "platform": "instagram", "ratio": "9:16", "count": 0},
        "style": "viral_reel", "seed": 0, "previous": None, "scope": None,
        "brain": "recipes", "content_brain": content_brain,
        "summary": {
            "project_type": "talking_head", "target": "Reel", "duration_s": 45.0,
            "hook": {"sent": "s_003", "src": src, "t0": 2.0, "t1": 4.5,
                     "quote": "Ninety percent of first cuts are thrown away."},
            "story": [{"beat": "hook", "sents": ["s_003"]}, {"beat": "context", "sents": ["s_001", "s_002"]}],
            "dialogue": None,
            "camera": {"angles": 1, "switches": 0, "at_cut": 0},
            "pauses_kept": 1,
            "music": {"bed": "chill", "shape": "bed", "rel_lu": -24, "duck_lu": -6},
            "captions": {"mode": "dynamic", "style": "ig_chunky", "position": "bottom"},
            "estimated_seconds": 12.0,
            "deferred": [{"asked": "per-word caption highlight", "why": "next wave"},
                         {"asked": "music intro and outro", "why": "next wave"}],
        },
        "decisions": [
            {"id": "k_0001", "kind": "cut_range", "ref": {"src": src, "t0": 3.0, "t1": 3.8}, "params": {},
             "reason": {"code": "silence", "facts": ["sil_0001"], "text": "silence of 0.8 s at 00:00:03:00"},
             "score": 0.9, "confidence": 0.95, "optional": False, "by": "recipes", "produced": None},
            {"id": "k_0002", "kind": "cut_range", "ref": {"src": src, "t0": 5.0, "t1": 5.4}, "params": {},
             "reason": {"code": "filler", "facts": ["w_0042"], "text": "filler “um” at 00:00:05:00"},
             "score": 0.8, "confidence": 0.9, "optional": False, "by": "recipes", "produced": None},
            {"id": "k_0003", "kind": "cut_range", "ref": {"src": src, "t0": 9.0, "t1": 10.0}, "params": {},
             "reason": {"code": "false_start", "facts": ["s_005"],
                        "text": "false start “so the—” before “so the cut” at 00:00:09:00"},
             "score": 0.7, "confidence": 0.8, "optional": False, "by": "recipes", "produced": None},
            {"id": "k_0004", "kind": "keep_pause", "ref": {"src": src, "t0": 7.0, "t1": 7.6}, "params": {},
             "reason": {"code": "pause_kept:emotion", "facts": ["s_004"],
                        "text": "kept 0.6 s of the pause at 00:00:07:00: after an emotional line"},
             "score": 0.6, "confidence": 0.7, "optional": True, "by": "recipes", "produced": None},
            {"id": "k_0005", "kind": "captions", "ref": None, "params": {"mode": "dynamic", "style": "ig_chunky"},
             "reason": {"code": "caption_mode", "facts": [], "text": "Captions: Dynamic (talking head, energy 5)"},
             "score": 1.0, "confidence": 1.0, "optional": False, "by": "recipes", "produced": None},
            {"id": "k_0006", "kind": "music", "ref": None, "params": {"bed": "chill"},
             "reason": {"code": "music_mood", "facts": ["sc_0001"], "text": "chill bed: talking head, arousal 0.3, subtle"},
             "score": 0.5, "confidence": 0.6, "optional": True, "by": "recipes", "produced": None},
            {"id": "k_0007", "kind": "punch_in", "ref": {"src": src, "t0": 2.2, "t1": 4.5},
             "params": {"scale": 1.12},
             "reason": {"code": "hook_emphasis", "facts": ["s_003"], "text": "punch in on the hook statement"},
             "score": 0.6, "confidence": 0.6, "optional": True, "by": "apple_intelligence", "produced": None},
        ],
        "children": [], "compiled": None, "score": None,
    }


def footprint(did: str, cut_clip_ids: list[str], *, ghost: str | None = None) -> dict[str, Any]:
    """The resolver's record of what each decision addressed. `ghost` is a
    clip id no diff mentions — an over-claim the card must not repeat."""
    fp: dict[str, Any] = {
        "version": 1, "decisions_id": did,
        "decisions": {
            "k_0001": {"clip_ids": list(cut_clip_ids), "source_spans": [{"t0": 3.0, "t1": 3.8}]},
            "k_0002": {"clip_ids": list(cut_clip_ids), "source_spans": [{"t0": 5.0, "t1": 5.4}]},
            "k_0003": {"clip_ids": list(cut_clip_ids), "source_spans": [{"t0": 9.0, "t1": 10.0}]},
            "k_0005": {"cue_range": [0.0, 12.0]},
            "k_0006": {"clip_ids": []},
        },
        "notices": [],
    }
    if ghost:
        fp["decisions"]["k_0007"] = {"clip_ids": [ghost], "keyframe_times": [0.0]}
    return fp


def write_edp(session_dir: Path, body: dict[str, Any]) -> Path:
    d = Path(session_dir) / "brain" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{body['id']}.json"
    p.write_text(json.dumps(body, indent=1), encoding="utf-8")
    return p


def write_footprint(store_dir: Path, body: dict[str, Any]) -> Path:
    d = Path(store_dir) / "brain"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "footprint.json"
    p.write_text(json.dumps(body, indent=1), encoding="utf-8")
    return p


#: A reason as an older planner wrote it: a graph key and the .normalized copy's name in the words.
DIALOGUE_ROW: dict[str, Any] = {
    "id": "k_0008", "kind": "dialogue", "code": "dialogue_lane", "optional": False, "by": "recipes", "applied": True,
    "note": None, "ref": None, "timeline_t": None,
    "text": "dialogue from src_55e24f95eb89a2019f59d349 (th_16x9.normalized.mp4) on lane a1; camera microphones muted"}


def card_seed(src: str, *, whys: list[str | None] | None = None) -> dict[str, Any]:
    """The `preview.brain` payload the card shows, as `brain_card` builds it,
    for the UI suite (which seeds a pending record on a real backend)."""
    body = edp(src)
    return {
        "decisions_id": body["id"], "tab_default": "plan",
        "rungs": {"brain": "recipes", "content_brain": "apple_intelligence",
                  "line": "via Recipes · moments by Apple Intelligence"},
        "summary": {**body["summary"], "hook": {**body["summary"]["hook"], "src": "talk.mp4", "timeline_t": 2.0},
                    "result_s": 44.7},
        # as brain_card builds it: a kept pause is neither applied nor dropped;
        # k_0007's footprint names a clip no diff mentions (E8: not applied)
        "decisions": [{"id": d["id"], "kind": d["kind"], "code": d["reason"]["code"], "text": d["reason"]["text"],
                       "optional": d["optional"], "by": d["by"],
                       "applied": None if d["kind"] == "keep_pause" else d["id"] != "k_0007",
                       "note": "dropped: its clip is not in the change list" if d["id"] == "k_0007" else None,
                       "ref": {"src": "talk.mp4", "t0": d["ref"]["t0"], "t1": d["ref"]["t1"]} if d["ref"] else None,
                       # where its source second plays in the CURRENT timeline (brain_card._timeline_at)
                       "timeline_t": d["ref"]["t0"] if d["ref"] else None}
                      for d in body["decisions"]] + [DIALOGUE_ROW],
        "deferred": body["summary"]["deferred"],
        "whys": whys or [],
        "grouped": [],
        "unexplained": [],
    }


def big_seed(src: str, *, cuts: int = 12) -> dict[str, Any]:
    """`card_seed` grown to the size of a real reel card (24 decisions, a
    long hook, two deferred lines): the worst case for the card's height."""
    seed = card_seed(src)
    extra = [{"id": f"k_{100 + i:04d}", "kind": "cut_range", "code": "filler",
              "text": f"filler “um” number {i} at 00:00:{10 + i:02d}:00, cut on the quiet between two words",
              "optional": False, "by": "recipes", "applied": True, "note": None,
              "ref": {"src": "talk.mp4", "t0": 10.0 + i, "t1": 10.4 + i}} for i in range(cuts)]
    seed["decisions"] = seed["decisions"] + extra
    seed["summary"] = {**seed["summary"], "hook": {**seed["summary"]["hook"], "quote": (
        "Ninety percent of first cuts are thrown away, and the ones that survive are the ones that "
        "start on the sentence a stranger would stop scrolling for.")}}
    return seed
