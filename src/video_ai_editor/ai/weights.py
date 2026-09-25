"""Which AI tools will download model weights on their next run, and how much.

QA-065: the Captions button's "Fastest" (large-v3-turbo, ~1.6 GB) and the AI
panel's Translate captions (~3 GB MADLAD), Remove background (~170 MB u2net),
Erase object (~200 MB LaMa), Isolate vocals (~80 MB htdemucs) and AI voiceover
(~60 MB Piper voice) all fetch weights the first time they run — and nothing
said so. The Prompt Editor already asks before a download (`first_use` in its
planner facts); the panels had no such fact to ask with. `/api/features` now
carries this report so every surface can badge an uncached option with its
size and ask before the first run.

Cheap on purpose: existence checks only, never an import of torch, rembg or
faster_whisper — the report is recomputed on every `/api/features` call (it
changes the moment a download finishes, unlike the feature probes).
"""
from __future__ import annotations

import os
from pathlib import Path

#: key → (what the user is told, approximate bytes). Keys are `<feature>` or
#: `<feature>:<model>` for a feature with several models.
WEIGHTS: dict[str, tuple[str, int]] = {
    "captions:large-v3": ("the accurate caption model", 3_100_000_000),
    "captions:large-v3-turbo": ("the fast caption model", 1_600_000_000),
    "translate": ("the translation model", 3_000_000_000),
    "tts": ("the voiceover voice", 60_000_000),
    "bg_remove": ("the background-removal model", 176_000_000),
    "object_erase": ("the object-removal model", 206_000_000),
    "stems": ("the voice-separation model", 84_000_000),
}


def _torch_checkpoints() -> Path:
    home = os.environ.get("TORCH_HOME")
    if home:
        return Path(home).expanduser() / "hub" / "checkpoints"
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache).expanduser() / "torch" / "hub" / "checkpoints"


def _u2net_cached() -> bool:
    home = os.environ.get("U2NET_HOME") or os.path.join(os.environ.get("XDG_DATA_HOME", "~"), ".u2net")
    return (Path(home).expanduser() / "u2net.onnx").is_file()


def _lama_cached() -> bool:
    env = os.environ.get("LAMA_MODEL")
    if env:
        return Path(env).expanduser().is_file()
    return (_torch_checkpoints() / "big-lama.pt").is_file()


def _htdemucs_cached() -> bool:
    # demucs names its htdemucs checkpoint by hash; any .th in the hub cache
    # from the htdemucs release is the model (955717e8-8726e21a.th today).
    d = _torch_checkpoints()
    return d.is_dir() and any(p.name.startswith("955717e8") for p in d.glob("*.th"))


def _first_use_missing() -> dict[str, int]:
    """The Prompt Editor's own probe (whisper / MADLAD / Piper), reused."""
    try:
        from ..agent.prompt import facts as _facts
        return _facts.first_use_probe(_facts._transcript_backend())
    except Exception:
        return {}


def weights_report() -> dict[str, dict]:
    """{key: {what, bytes, cached}} for every tool whose first run downloads."""
    missing = _first_use_missing()
    cached = {
        "captions:large-v3": "whisper:large-v3" not in missing,
        "captions:large-v3-turbo": "whisper:large-v3-turbo" not in missing,
        "translate": "madlad" not in missing,
        "tts": "piper:en_US-amy-medium" not in missing,
        "bg_remove": _u2net_cached(),
        "object_erase": _lama_cached(),
        "stems": _htdemucs_cached(),
    }
    return {k: {"what": what, "bytes": size, "cached": bool(cached[k])}
            for k, (what, size) in WEIGHTS.items()}
