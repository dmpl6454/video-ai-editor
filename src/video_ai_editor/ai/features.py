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
    # torchvision, torchaudio, open_clip, timm and scipy, which the Windows
    # `.spec` keeps. (faster_whisper was on that list too until captions were
    # made to work from the DMG.) Deriving the flag from the `.spec` alone left
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


def _cuda_math_libs_present() -> bool:
    """Is cuBLAS findable? Three ways, because a false negative here would tell
    someone to install what they already have — the exact failure this module
    exists to prevent.

    The CUDA *driver* is not enough: ctranslate2 needs the math libraries, and
    without them a model loads fine and dies on the first forward pass with
    "Library cublas64_12.dll is not found or cannot be loaded".
    """
    if _has("nvidia.cublas"):                       # the `cuda` extra's wheel
        return True
    import ctypes.util
    if ctypes.util.find_library("cublas"):          # ldconfig on Linux
        return True
    # A system-wide CUDA toolkit puts the versioned name on PATH (Windows) or in
    # a linker dir; find_library misses the Windows spelling, so scan for it.
    names = ("cublas64_12.dll", "cublas64_13.dll") if sys.platform == "win32" \
        else ("libcublas.so.12", "libcublas.so")
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            if any(os.path.exists(os.path.join(d, n)) for n in names):
                return True
        except OSError:                             # unreadable PATH entry
            continue
    return False


def _gpu_transcribe_ok() -> bool:
    """Will transcription actually run on the GPU here?

    Deliberately does NOT load a model — this module's contract is import-spec
    and file-existence checks only, so the agent can call it freely. So it is a
    claim about configuration, not a guarantee: `transcribe._probe_forward_pass`
    is what proves execution, at model-load time.

    Both halves are required. A device without the math libraries is the measured
    failure above; math libraries without a device (or with WHISPER_DEVICE=cpu)
    means CPU regardless.
    """
    try:
        from ..ingest import transcribe as _t
        _t._add_cuda_dll_dirs()          # so a wheel install is on PATH to find
        return _t._resolve_device() == "cuda" and _cuda_math_libs_present()
    except Exception:
        return False


def _tts_ok() -> bool:
    """Piper's Python package AND the espeak-ng dictionaries it phonemizes with.

    `_has("piper")` alone was not merely optimistic here, it was the most
    dangerous answer this module can give. In the packaged app piper imports
    fine, so TTS reported AVAILABLE — and then `tts_voiceover` KILLED THE WHOLE
    PROCESS (audited against the shipped bundle: `/api/health` 200, run the
    tool, no process). `piper/phonemize_espeak.py` hands
    `<piper package dir>/espeak-ng-data` straight to espeak-ng, and espeak-ng
    answers a missing phoneme table with `exit(1)` — inside our own
    interpreter, so there is no exception to catch, no traceback, and every
    other session in the process dies with it. `build_app.sh` did not collect
    that data (it is package DATA, invisible to PyInstaller's import analysis),
    so the app was inviting users into a crash. It now passes
    `--collect-data piper`; this probe is the second half, so a build that ever
    loses the data again reports the feature missing instead of fatal.

    Cheap and non-raising, per this module's contract: `find_spec` locates the
    package WITHOUT importing it (importing piper is itself a risk here), and
    the rest is one `os.path.isfile`. `phontab` is the marker rather than the
    directory, because an empty `espeak-ng-data/` fails exactly the same way.
    """
    try:
        spec = importlib.util.find_spec("piper")
    except (ImportError, ValueError, AttributeError):
        return False
    if spec is None:
        return False
    # Frozen builds go through PyInstaller's FrozenImporter, which sets
    # submodule_search_locations to [<sys._MEIPASS>/piper] — the same directory
    # piper's own `Path(__file__).parent` resolves to there.
    roots = list(getattr(spec, "submodule_search_locations", None) or [])
    if not roots and spec.origin:
        roots = [os.path.dirname(spec.origin)]
    for root in roots:
        try:
            if os.path.isfile(os.path.join(root, "espeak-ng-data", "phontab")):
                return True
        except OSError:
            continue
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
            # faster-whisper is now bundled in BOTH packaged builds. It used to
            # be `--exclude-module`d on macOS, so the notarized DMG answered the
            # Captions button with transcribe.py's "run the app from source
            # (`uv sync --all-extras`)" — on the one feature a video editor is
            # most likely to reach for, and advice a DMG recipient cannot act
            # on. The whisper.cpp escape hatch this text used to lead with is
            # not a real one either: Homebrew's `whisper-cli` is a wrapper that
            # links @rpath dylibs, so it is not a drop-in without dylib surgery.
            # Reaching this line in a frozen app therefore means the BUNDLE is
            # broken, not that the feature was deliberately left out — say so,
            # since a packaging fault and a deliberate omission need different
            # responses from whoever reads it. The drop-in route stays named
            # because it does still work inside a bundle for anyone who has a
            # portable whisper-cli.
            packaged_fix="this build is supposed to ship faster-whisper and does "
                         "not appear to — a packaging fault, not a missing extra, "
                         "so please report it. Workaround: put a portable "
                         "whisper.cpp binary (`whisper-cli`) + a ggml model in the "
                         "models/ dir, or run the app from source. Nothing can be "
                         "pip-installed into a frozen app.",
            # Measured, not estimated: the large-v3 repo is 3.09GB on disk
            # (model.bin alone is 3,087,284,237 bytes) and `small` is ~465MB.
            # This note said "~1.5GB" and was the only warning a user got
            # before a multi-gigabyte download started.
            note="Captions transcribe with large-v3, downloaded on first use "
                 "(~3GB, once, cached). The quick transcript made when you "
                 "import a clip uses `small` instead (~465MB). Set "
                 "WHISPER_CAPTION_MODEL to a smaller name to skip the big one."),
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
    Feature("tracking", "Motion tracking / auto-reframe",
            ["motion_track", "auto_reframe"],
            lambda: _has("cv2"),
            fix=f"`{PIP_EXTRA}` (installs opencv-python)",
            note="auto_reframe uses mediapipe for face/subject detection when "
                 "present, else a saliency heuristic."),
    # `object_erase` used to be grouped under "tracking" and gated on cv2 —
    # wrong dependency entirely. `ai/lama.py` (what object_erase actually
    # calls) never imports cv2; its only import is `simple_lama_inpainting`,
    # a BASE pyproject dependency, so this was invisible from source (always
    # True there) and reported "available" in BOTH packaged builds, where
    # `simple_lama_inpainting` IS excluded (`build_app.sh` AND the Windows
    # `.spec` both list it). So a packaged-app user asking to erase an object
    # got a confident "yes" from check_features and then a bare RuntimeError
    # from the actual call — precisely the failure mode this module exists to
    # prevent, reproduced by a coarse-grained Feature grouping rather than a
    # missing probe.
    Feature("object_erase", "Object removal (LaMa inpainting)",
            ["object_erase"], lambda: _has("simple_lama_inpainting"),
            fix=f"`{PIP_EXTRA}` (installs simple-lama-inpainting)",
            note="Downloads ~200MB of LaMa weights on first use. Runs on CPU "
                 "even where CUDA is available — the bundled torchscript model "
                 "has CUDA-tagged ops that crash on Mac, so it always loads "
                 "with map_location='cpu'.",
            in_packaged_app=False),
    Feature("beats", "Beat detection / cut-to-music",
            ["auto_cut_to_beats"], lambda: _has("librosa"),
            fix=f"`{PIP_EXTRA}` (installs librosa)",
            in_packaged_app=False),
    Feature("tts", "Text-to-speech voiceover",
            ["tts_voiceover"], _tts_ok,
            fix=f"`{PIP_EXTRA}` (installs piper-tts, which carries its own "
                "espeak-ng-data)",
            # piper IS bundled in both packaged builds, so the blanket "run from
            # source" answer would be wrong; what a bundle can be missing is
            # piper's espeak-ng DATA, and nothing can be pip-installed into a
            # frozen app to add it.
            packaged_fix="this build is missing piper's espeak-ng-data (voice "
                         "synthesis would take the app down, so it is reported "
                         "unavailable instead); rebuild with `--collect-data "
                         "piper`, or run the app from source",
            note="Voices download on first use (~60MB each) into the app's "
                 "cache dir."),
    Feature("translate", "Caption translation",
            ["translate_captions"], lambda: _has("ctranslate2", "sentencepiece"),
            fix=f"`{PIP_EXTRA}` (installs ctranslate2 + sentencepiece)",
            note="Downloads a ~3GB MADLAD-400 translation model on first use. "
                 "Needs no torch, unlike the Argos-based translator this "
                 "replaced (which pulled in stanza for sentence splitting) — "
                 "so unlike before, this is genuinely available in BOTH "
                 "packaged builds, including macOS's torch-excluded one."),
    Feature("stabilize", "Video stabilization",
            ["stabilize"], _binary("stabilize", "available"),
            fix="install an ffmpeg built with libvidstab "
                "(macOS: `brew install ffmpeg-full`; Windows: the full Gyan build)"),
    Feature("upscale", "AI upscaling",
            ["upscale"], _binary("upscale", "available"),
            fix="`uv run python -m video_ai_editor.cli.setup_ai_binaries --which upscale` "
                "(downloads realesrgan-ncnn-vulkan, ~45-52MB, and places it where "
                "ai/upscale.py looks). Manual alternative: download it yourself and put "
                "it in models/realesrgan/ (or %APPDATA%/Video AI Editor on Windows)."),
    Feature("interpolate", "Smooth slow motion (frame interpolation)",
            ["smooth_slow_motion"], _binary("rife", "available"),
            fix="`uv run python -m video_ai_editor.cli.setup_ai_binaries --which interpolate` "
                "(downloads rife-ncnn-vulkan, ~430MB — RIFE ships every model generation "
                "in one archive, there is no smaller official build). Manual alternative: "
                "download it yourself and put it in models/rife/."),
    # Not a capability — a SPEED tier for one that already works. It is listed
    # anyway because "captions take forever" was reported as a broken button,
    # and this is the answer: measured on 60s of Hindi with large-v3, decode ran
    # 95.2s on CPU int8 and 8.4s on CUDA float16 (11.3x) for the same transcript,
    # turning a 3-minute video from ~9.3 minutes into ~25 seconds. Without a
    # probe the agent cannot tell a user whether their GPU is being used, and
    # `auto` silently meant CPU until recently — so this is exactly the class of
    # question this module exists to answer instead of guess.
    Feature("gpu_transcribe", "GPU-accelerated transcription (NVIDIA)",
            ["auto_caption", "get_transcript"], _gpu_transcribe_ok,
            # The whole command, not just `--group cuda`: uv sync PRUNES, so the
            # short form drops the extras and uninstalls pytest/movis/pyside6
            # from a dev checkout. Verified with `--dry-run`.
            fix=f"`{PIP_EXTRA} --group cuda` (adds cuBLAS + cuDNN, ~1.3GB) on a "
                "machine with an NVIDIA GPU and a current driver",
            note="Optional speedup, not a requirement — captions work on CPU, "
                 "roughly 11x slower. Needs an NVIDIA card: there is no Metal "
                 "backend in ctranslate2, so a Mac always transcribes on CPU.",
            # The CUDA wheels are in neither bundle, and pip cannot add them to
            # a frozen app.
            in_packaged_app=False),
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
                # …but only call it EXCLUDED if it actually is. captions now
                # ships in both packaged builds and still carries a packaged_fix
                # (for the "bundled but somehow missing" case), so stamping this
                # unconditionally told the UI to grey out a feature the build
                # contains and label it deliberately left out.
                entry["packaged_app_excluded"] = not f.in_packaged_app
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
