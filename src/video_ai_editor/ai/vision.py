"""Lazy vision pipeline.

On demand we extract one keyframe per detected shot, then ask Claude vision to
describe it. Results are cached per (source_path, shot_index) forever — once a
shot is captioned, future find_moments calls hit the cache instantly.
"""
from __future__ import annotations
import base64
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Iterable

from ..config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from ..ingest.scenes import detect_shots, Shot
from ..ingest.probe import probe
from .. import platformutil as _pu


def _shot_key(src: Path, idx: int) -> str:
    h = hashlib.sha256(str(src).encode()).hexdigest()[:12]
    return f"{h}_{idx:04d}"


def shot_index_for(src: Path, cache_dir: Path) -> list[Shot]:
    """Return shots for a video. Cached on disk; safe to re-call."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(src).encode()).hexdigest()[:12]
    idx_path = cache_dir / f"shots_{key}.json"
    if idx_path.exists():
        try:
            data = json.loads(idx_path.read_text())
            return [Shot(**s) for s in data]
        except Exception:
            pass
    shots = detect_shots(src)
    idx_path.write_text(json.dumps([s.model_dump() for s in shots], indent=2))
    return shots


def extract_keyframe(src: Path, t: float, dst: Path, max_w: int = 384) -> Path:
    """Grab one frame at time `t`, scaled small for API cost."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return dst
    subprocess.run(
        [_pu.FFMPEG, "-y", "-ss", f"{t:.3f}", "-i", str(src),
         "-vf", f"scale={max_w}:-2", "-frames:v", "1", "-q:v", "3",
         str(dst)],
        capture_output=True, check=False,
    )
    return dst


def describe_shot(src: Path, shot: Shot, cache_dir: Path) -> str:
    """Return a 1–2 sentence description of a shot via Claude vision. Cached."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _shot_key(src, shot.index)
    desc_path = cache_dir / f"desc_{key}.txt"
    if desc_path.exists():
        return desc_path.read_text()
    if not ANTHROPIC_API_KEY:
        return ""

    # Pull a frame at the middle of the shot
    mid_t = (shot.start + shot.end) / 2
    frame = cache_dir / f"frame_{key}.jpg"
    extract_keyframe(src, mid_t, frame)
    if not frame.exists() or frame.stat().st_size == 0:
        return ""

    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    img_b64 = base64.standard_b64encode(frame.read_bytes()).decode()
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=120,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": "Describe this video frame in 1–2 sentences. Focus on visible subjects, action, setting, and mood. Be specific."},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    desc_path.write_text(text)
    return text


def warm_scene_index(src: Path, cache_dir: Path, *, max_shots: int | None = None) -> list[dict]:
    """Pre-warm vision descriptions for all shots (used by match_style)."""
    shots = shot_index_for(src, cache_dir)
    out = []
    for s in shots[:max_shots] if max_shots else shots:
        out.append({"index": s.index, "start": s.start, "end": s.end,
                    "description": describe_shot(src, s, cache_dir)})
    return out


# --- find_moments ---

def _score_segment_against_query(text: str, query: str) -> float:
    """Cheap keyword overlap; good enough for transcript-first ranking."""
    if not text:
        return 0.0
    q = query.lower().split()
    t = text.lower()
    return sum(1.0 for w in q if w in t) / max(1, len(q))


def find_moments(query: str, transcript: dict, src: Path, cache_dir: Path,
                 *, top_k: int = 3, vision_verify: int = 3) -> list[dict]:
    """Rank transcript segments by query overlap; verify top candidates with vision."""
    segs = transcript.get("segments", []) if transcript else []
    scored = sorted(
        [(s, _score_segment_against_query(s.get("text") or "", query)) for s in segs],
        key=lambda x: x[1], reverse=True,
    )
    top = [s for s, sc in scored if sc > 0][:vision_verify]
    if not top:
        # Fall back: vision-only over all shots (capped)
        shots = shot_index_for(src, cache_dir)
        candidates = []
        for s in shots[:8]:
            d = describe_shot(src, s, cache_dir)
            score = _score_segment_against_query(d, query)
            if score > 0:
                candidates.append({"start": s.start, "end": s.end, "description": d, "score": score})
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_k]

    # Vision verify each candidate's covering shot
    shots = shot_index_for(src, cache_dir)
    results = []
    for seg in top:
        sstart = seg.get("start", 0)
        shot = next((s for s in shots if s.start <= sstart < s.end), None)
        d = describe_shot(src, shot, cache_dir) if shot else ""
        results.append({
            "start": seg.get("start"), "end": seg.get("end"),
            "transcript": seg.get("text"),
            "shot_description": d,
            "score": _score_segment_against_query((seg.get("text") or "") + " " + d, query),
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
