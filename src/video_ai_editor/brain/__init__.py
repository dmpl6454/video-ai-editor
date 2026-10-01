"""The Editor Brain (EDITOR_BRAIN_SPEC.md; wave EB1).

A deterministic planner over a cached, on-device Content Graph whose output
is a frozen Edit Decision Plan (EDP) that compiles into an ordinary Prompt
Editor `Plan` of existing dispatch tools plus one declared sentinel family,
`$brain:<kind>`, resolved against the live store immediately before each
step's dispatch (`brain/resolve.py`, called from `agent/prompt/live.py`).

Package map (owner lane in brackets, wave EB1):

  schema.py    [C]  the Pydantic shapes: graph header, layers, scenes,
                    angles, EDP, Decision; canonical JSON + digest; caps
  store.py     [C]  WORKDIR/analysis/<src_key>/ + <session>/brain/ files,
                    atomic writes, identity + content keys, refs.json
  resolve.py   [C]  `$brain:*` resolution through agent/timemap, footprint,
                    the fan-out cap, the stale-graph refusal
  checks.py    [C]  the two blocking verifier checks of this wave
  versions.py  [C]  named versions on snapshots commit() already wrote
  analysis/, graph.py, gateway.py, digest.py   [D]
  planner/, energy.py, reasons.py, compile.py  [E]

WHY nothing is imported at package level: `agent/prompt/schema.py` and the
validator import pieces of this package from processes that must stay
light; import the module you need explicitly.
"""
from __future__ import annotations

#: Bumped when an analyser's OUTPUT changes for the same input (a layer's
#: params carry it, so old files are simply not found — never re-read).
ANALYSIS_VERSION = 1
#: Bumped when the planner's decisions change for the same graph (the
#: planner goldens are regenerated only with a bump).
PLANNER_VERSION = 2
#: The EDP wire version (`EDP.version`).
EDP_VERSION = 1

__all__ = ["ANALYSIS_VERSION", "PLANNER_VERSION", "EDP_VERSION"]
