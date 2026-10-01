"""Editor Brain analysers (spec §3): numpy + ffmpeg + the upload's transcript.

INBUILT (spec §0.3): nothing in this package imports librosa, torch, mlx or
pyannote — `tests/test_brain_packaged_imports.py` builds both fixture graphs
with those names blocked in `sys.modules`. Every layer is a plain dict in the
frozen shape of the EB1 brief; `graph.analyse` writes them through lane C's
`brain.store`, whose Pydantic models validate every one.

Layers, per SOURCE, under `WORKDIR/analysis/<src_key>/<layer>/<params>.json`:

  audio     `audio.py`    100 Hz RMS envelope (int8 dBFS, base64), VAD, silences,
                          loudness, noise floor, clipping, own-mic energy per angle
  speakers  `speakers.py` utterances → 13 numpy MFCC → k-means(seed 42) → turns,
                          roles, angle hints by own-mic correlation first
  speech    `speech.py`   words (prob passed through, timing repaired by
                          `word_timing.py`), sentences, features, turns, false
                          starts, repeats, dead air, acoustic fillers
                          (`fillers.py`), words to check, `technical` flags
                          (`unheard_voice`: sound no word names). The
                          transcript of record is the reference's upload
                          transcript, else v1's moved onto the reference
                          clock (`transcripts.py`)
  semantic  `semantic.py` the §3.4 heuristic scores with evidence over the
                          inputs `delivery.py` derives; `rank_moments` through
                          the Gateway, blended 0.6/0.4 with provenance

`ANALYSIS_VERSION` is part of every params name and of the graph id; bump it
when a layer's bytes change for the same input.
"""
from __future__ import annotations

ANALYSIS_VERSION = 1
LAYERS: tuple[str, ...] = ("speech", "speakers", "audio", "semantic")

__all__ = ["ANALYSIS_VERSION", "LAYERS"]
