"""One honest answer to "does this install actually have feature X?".

Every optional capability already has its own `available()` probe, but nothing
aggregated them and nothing exposed them to the chat agent. So when a tool
failed, Claude had no way to find out WHY and simply invented a diagnosis — it
told a user to run `uv add noisereduce soundfile` for a noisereduce that was
installed and working, and reported vocal isolation as "not installed" when
demucs, torch and the model were all present and the real fault was a
torchcodec DLL (see separate.py). Guessed remedies are worse than no remedy:
the user runs commands that change nothing and loses trust in the tool that
does work.

`feature_report()` is cheap on purpose — import-spec lookups and file-existence
checks, never a model load or a network call — so the agent can call it freely
before making any claim about what this machine can do.
"""
from __future__ import annotations
import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Callable


def _has(*mods: str) -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in mods)
    except (ImportError, ValueError):
        return False


@dataclass
class Feature:
    key: str
    label: str
    tools: list[str]
    probe: Callable[[], bool]
    # How to get it, when it is missing. Empty when the feature is always on.
    fix: str = ""
    # Extra detail that is true regardless of availability (caveats, fallbacks).
    note: str = ""
    # False when this feature's Python dependency is excluded from a packaged
    # bundle and therefore CANNOT be present in it. Such a feature's pip `fix`
    # is actively wrong there — you cannot install into a frozen bundle — so
    # `feature_report()` substitutes the real remedy.
    #
    # "a packaged bundle" means EITHER build, because the two exclude lists are
    # not the same: `build_app.sh` (macOS) additionally drops torch,
    # torchvision, torchaudio, faster_whisper, open_clip, timm and scipy, which
    # the Windows `.spec` keeps. Deriving the flag from the `.spec` alone left
    # every macOS user of the packaged app with exactly the impossible advice
    # this field exists to prevent (`visual_search` → "run uv sync", inside a
    # frozen .app). Marking a feature False costs nothing on the build where it
    # IS bundled: `fix` is only ever emitted for a feature that probed
    # unavailable, and there it probes available.
    #
    # Features backed by an external BINARY (ffmpeg/realesrgan/rife) stay True:
    # dropping the binary in place works identically in both builds.
    in_packaged_app: bool = True
    # Set when a feature has a route that DOES work inside a bundle, so the
    # blanket "run from source" answer would be under-selling it. Used in
    # preference to PACKAGED_FIX when frozen.
    packaged_fix: str = ""


def _whisper_ok() -> bool:
    if _has("faster_whisper"):
        return True
    try:
        from ..ingest import transcribe as _t
        return bool(_t._whisper_cpp_available())
    except Exception:
        return False


def _binary(mod: str, attr: str) -> Callable[[], bool]:
    def probe() -> bool:
        try:
            m = importlib.import_module(f"..ai.{mod}", __package__)
            return bool(getattr(m, attr)())
        except Exception:
            return False
    return probe


PIP_EXTRA = "uv sync --all-extras --group dev"

FEATURES: list[Feature] = [
    Feature("captions", "Auto captions / subtitles / transcription",
            ["auto_caption", "add_caption_track", "get_transcript"],
            _whisper_ok,
            fix=f"`{PIP_EXTRA}` (installs faster-whisper), or drop a whisper.cpp "
                "binary + ggml model in the models/ dir",
            # faster-whisper IS bundled on Windows and is NOT on macOS
            # (build_app.sh excludes it), so on a packaged Mac this reports
            # unavailable and the pip half of `fix` above is impossible. The
            # binary half is not — whisper.cpp is a drop-in — so this feature
            # gets a route rather than the blanket "run from source".
            packaged_fix="drop a whisper.cpp binary (`whisper-cli`) + a ggml model "
                         "into the models/ dir, or run the app from source — the "
                         "packaged macOS build excludes faster-whisper and nothing "
                         "can be pip-installed into a frozen app",
            note="First use of the caption model downloads it (~1.5GB for large-v3)."),
    Feature("noise_reduce", "Background-noise removal",
            ["noise_reduce"], lambda: _has("noisereduce", "soundfile"),
            fix=f"`{PIP_EXTRA}`",
            in_packaged_app=False),
    Feature("stems", "Vocal / instrumental isolation",
            ["vocal_isolate", "instrumental_isolate"],
            _binary("separate", "available"),
            fix=f"`{PIP_EXTRA}` (needs demucs + torch + soundfile)",
            note="Excluded from the packaged app — run from source. CPU-only; "
                 "roughly 6x realtime. Decodes audio itself rather than through "
                 "torchcodec, which cannot load its DLLs on a standard Windows "
                 "ffmpeg install.",
            in_packaged_app=False),
    Feature("bg_remove", "Background removal (green-screen-free)",
            ["remove_background"], _binary("bgremove", "available"),
            fix=f"`{PIP_EXTRA}` (installs rembg)",
            in_packaged_app=False),
    Feature("visual_search", "Visual search over your footage",
            ["search_media"], _binary("clip_search", "available"),
            fix=f"`{PIP_EXTRA}` (installs open_clip + torch)",
            # Bundled on Windows, excluded on macOS (build_app.sh drops torch
            # AND open_clip), so a packaged Mac must not be told to pip-install.
            in_packaged_app=False),
    Feature("diarize", "Speaker diarization",
            ["diarize", "assign_caption_speakers"], lambda: _has("librosa"),
            fix=f"`{PIP_EXTRA}` (installs librosa)",
            note="Runs out of the box on a librosa heuristic. Set HUGGINGFACE_TOKEN "
                 "and accept the pyannote EULA for the high-quality model — "
                 "`pyannote_status` reports exactly what is missing.",
            in_packaged_app=False),
    Feature("tracking", "Motion tracking / auto-reframe / object erase",
            ["motion_track", "auto_reframe", "object_erase"],
            lambda: _has("cv2"),
            fix=f"`{PIP_EXTRA}` (installs opencv-python)",
            note="auto_reframe uses mediapipe for face/subject detection when "
                 "present, else a saliency heuristic."),
    Feature("beats", "Beat detection / cut-to-music",
            ["auto_cut_to_beats"], lambda: _has("librosa"),
            fix=f"`{PIP_EXTRA}` (installs librosa)",
            in_packaged_app=False),
    Feature("tts", "Text-to-speech voiceover",
            ["tts_voiceover"], lambda: _has("piper"),
            fix=f"`{PIP_EXTRA}` (installs piper-tts)"),
    Feature("translate", "Caption translation",
            ["translate_captions"], lambda: _has("argostranslate"),
            fix="`uv add argostranslate`",
            note="Language packs download on first use.",
            in_packaged_app=False),
    Feature("stabilize", "Video stabilization",
            ["stabilize"], _binary("stabilize", "available"),
            fix="install an ffmpeg built with libvidstab "
                "(macOS: `brew install ffmpeg-full`; Windows: the full Gyan build)"),
    Feature("upscale", "AI upscaling",
            ["upscale"], _binary("upscale", "available"),
            fix="download `realesrgan-ncnn-vulkan` and put it in models/realesrgan/ "
                "(or %APPDATA%/Video AI Editor on Windows)"),
    Feature("interpolate", "Smooth slow motion (frame interpolation)",
            ["smooth_slow_motion"], _binary("rife", "available"),
            fix="download `rife-ncnn-vulkan` and put it in models/rife/"),
]


PACKAGED_FIX = (
    "NOT available in the packaged app, and cannot be installed into it — the "
    "heavy ML libraries are excluded from the bundle on purpose to keep the "
    "download small. To get this feature, run the app from source instead: "
    f"`{PIP_EXTRA}` then `uv run video-ai-editor` (macOS: `bash run.sh`, "
    "Windows: `run.ps1`). Nothing is broken and no install command will fix it "
    "in this build."
)


def feature_report() -> dict:
    """{available: [...], unavailable: [...]} with a concrete fix for each gap."""
    frozen = bool(getattr(sys, "frozen", False))
    avail, missing = [], []
    for f in FEATURES:
        try:
            ok = bool(f.probe())
        except Exception:                       # a probe must never take the app down
            ok = False
        entry = {"key": f.key, "feature": f.label, "tools": f.tools}
        if f.note:
            entry["note"] = f.note
        if ok:
            avail.append(entry)
        else:
            # In a frozen app a pip fix is not merely unhelpful, it is WRONG:
            # the user runs it, nothing changes, and the tool that told them to
            # loses the trust this module exists to protect. Measured on the
            # real Windows build: 8 of 13 features report unavailable there,
            # so this is the common path in a packaged install, not an edge
            # case — and it is very likely what "most of the tools are not
            # installed" meant in the tester report.
            if frozen and f.packaged_fix:
                # A feature with a route that works inside a bundle (a drop-in
                # binary) gets that route, not the blanket "run from source".
                entry["fix"] = f.packaged_fix
                entry["packaged_app_excluded"] = True
            elif frozen and not f.in_packaged_app:
                entry["fix"] = PACKAGED_FIX
                entry["packaged_app_excluded"] = True
            else:
                entry["fix"] = f.fix
            missing.append(entry)
    return {
        "packaged_app": frozen,
        "python": sys.version.split()[0],
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "available": avail,
        "unavailable": missing,
        "summary": (f"{len(avail)}/{len(FEATURES)} optional features available"
                    + (f"; missing: {', '.join(m['key'] for m in missing)}"
                       if missing else "")),
    }
