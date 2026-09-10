"""The brains: recipes, Apple Intelligence, local MLX model, Claude (spec §3).

Import `base` for the protocol; the adapters (`recipes_brain`, `fm`,
`mlx_brain`, `cloud_plan`) and `router` are B's and import their heavy
dependencies lazily — nothing in this package touches `mlx_lm`, `anthropic`
or a subprocess at import time, so `brains_report()` can always run and say
honestly what is missing.
"""
from __future__ import annotations

from .base import (BRAIN_IDS, BRAIN_LABELS, REASONS, Availability, Brain, BrainAnswer,
                   BrainRequest, BrainResult, BrainUnavailable, TextResult, TextTask, ToolCard)

__all__ = ["BRAIN_IDS", "BRAIN_LABELS", "REASONS", "Availability", "Brain", "BrainAnswer",
           "BrainRequest", "BrainResult", "BrainUnavailable", "TextResult", "TextTask", "ToolCard"]
