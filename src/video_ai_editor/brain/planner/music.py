"""Music, this wave (spec §4.8 reels only): a Subtle bed by mood at
`speech_lufs − 24 LU`, ducked a further 6 LU under speech; an episode gets
no bed (intro/outro are EB2, said in `deferred[]`). Levels are relative to
the measured speech loudness, never absolute.
"""
from __future__ import annotations

from .. import energy as E
from .. import reasons as R
from .graph_view import Graph
from .types import Ctx, decision

BEDS = {"upbeat": "upbeat_120bpm", "chill": "chill_90bpm", "cinematic": "cinematic_70bpm", "lofi": "lofi_85bpm"}


def mood_for(g: Graph, ctx: Ctx) -> str:
    control = ctx.controls.get("mood")
    if control in BEDS:
        return str(control)
    if ctx.reel and ctx.level >= 7:
        return "upbeat"
    if ctx.project_type == "interview":
        return "cinematic"
    hint = g.music_mood
    return hint if hint in BEDS else "chill"


def _arousal(g: Graph) -> tuple[float, list[str]]:
    scored = sorted(((float(g.scores(s["id"]).get("emotion") or 0.0), s["id"]) for s in g.sentences), reverse=True)
    if not scored:
        return 0.0, []
    mean = sum(v for v, _ in scored) / len(scored)
    return round(mean, 2), [sid for _, sid in scored[:2]]


def run(g: Graph, ctx: Ctx, decisions: list[dict]) -> list[dict]:
    control = str(ctx.controls.get("music") or ("subtle" if ctx.reel else "off"))
    if control in ("off", "none") or not ctx.reel:
        ctx.state["music"] = None
        if ctx.target == "episode":
            ctx.defer("music intro/outro", "next wave; no bed under an episode")
        return decisions
    mood = mood_for(g, ctx)
    arousal, facts = _arousal(g)
    gain_db = round(max(-40.0, min(0.0, (g.speech_lufs - E.BED_REL_LU) - E.BED_LUFS)), 1)
    ctx.state["music"] = {"bed": BEDS[mood], "shape": "bed", "rel_lu": float(E.BED_REL_LU), "duck_lu": float(E.DUCK_LU)}
    decisions.append(decision(
        "music", params={"bed": BEDS[mood], "mood": mood, "volume_db": gain_db, "duck_db": -float(E.DUCK_LU), "loop": True,
                         "speech_lufs": g.speech_lufs},
        reason=R.reason("music_mood", facts + ["music_hint"] if g.music_mood else facts, mood=mood,
                        content_type=ctx.project_type.replace("_", " "), arousal=f"{arousal:.2f}", control="Music: Subtle"),
        score=0.9, confidence=0.9))
    return decisions


__all__ = ["run", "mood_for", "BEDS"]
