"""Whisper transcription with two backends:

  - `faster-whisper` (default) — pip-installed, runs on CPU with int8, and
    decodes in BATCHES where available (~2x faster; see `_DECODE_MODES`).
  - `whisper-cli` (whisper.cpp from `ffmpeg-full`) — Metal-accelerated on
    Apple Silicon, ~3-5× faster than faster-whisper on CPU. Opt-in by setting
    WHISPER_BACKEND=whisper_cpp env var, or by passing backend='whisper_cpp'
    to transcribe().
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel

from ..config import WHISPER_MODEL, WHISPER_DEVICE
from .. import platformutil as _pu

_log = logging.getLogger("video_ai_editor")


class TranscriptionCancelled(RuntimeError):
    """The caller's `should_cancel()` returned True mid-decode.

    A RuntimeError subclass on purpose: main.py maps RuntimeError to HTTP 422
    with the message intact, so a user who presses Cancel gets "transcription
    cancelled after 12s of 125s" rather than a 500.
    """


class Word(BaseModel):
    start: float
    end: float
    word: str
    prob: float = 1.0


class Segment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    words: list[Word] = []


class Transcript(BaseModel):
    language: str
    duration: float
    segments: list[Segment] = []

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()


# Per-model cache so picking `tiny.en` once doesn't have to re-load `small`
# the next time it's requested.
_models: dict[str, Any] = {}


def _add_cuda_dll_dirs() -> list[str]:
    """Put the pip-installed CUDA libraries where ctranslate2 can find them.

    The `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels drop their DLLs in
    `site-packages/nvidia/<lib>/bin`, which is on no search path at all — torch
    registers its own copies, ctranslate2 does not. Without this, CUDA fails at
    the FIRST FORWARD PASS with `Library cublas64_12.dll is not found or cannot
    be loaded`, long after the model has loaded successfully.

    **It must be PATH, not `os.add_dll_directory`** — verified both ways on
    Windows: with only `add_dll_directory` the load still failed with that exact
    message, and prepending the same directories to PATH worked. ctranslate2
    loads by plain library name, and `add_dll_directory` only affects
    `LoadLibraryEx` calls that opt into the altered search order.

    Lives here rather than in config.py — which is the app's PATH-augmenting
    chokepoint — because config.py is imported by every entry point and this is a
    whisper-only concern that would make it glob site-packages on every import.
    It is called from `_cuda_device_count`, which is `lru_cache`d and runs before
    any model is built, so the cost is paid once.

    No-op on POSIX (the wheels ship .so files that the linker finds through
    RPATH) and harmless when the packages are absent.
    """
    if not _pu.IS_WINDOWS:
        return []
    import glob
    roots = [Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"]
    # A frozen build puts them beside the bundled modules instead.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "nvidia")
    found: list[str] = []
    for root in roots:
        found += [d for d in glob.glob(str(root / "*" / "bin")) if Path(d).is_dir()]
    if found:
        existing = os.environ.get("PATH", "")
        # Prepend, but don't duplicate on a re-entry.
        missing = [d for d in found if d not in existing.split(os.pathsep)]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing + [existing])
    return found


@lru_cache(maxsize=1)
def _cuda_device_count() -> int:
    """How many CUDA devices ctranslate2 can actually use; 0 if none.

    Cached: it loads the CUDA runtime, and device resolution runs on every model
    load. Note this proves the runtime initialised, NOT that a model can execute
    — the CUDA *driver* provides enough for this call to return 1 on a machine
    with no cuBLAS at all, which is exactly how the missing-DLL failure got as
    far as the first forward pass. `_probe_forward_pass` is the real evidence.
    """
    try:
        _add_cuda_dll_dirs()
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception as e:                      # no ctranslate2, no driver, …
        _log.debug("CUDA unavailable (%s)", e)
        return 0


def _resolve_device() -> str:
    """Sanitize WHISPER_DEVICE into something ctranslate2 accepts.

    Users' .env files may carry a polluted value (an inline comment that a
    pre-fix parser kept as part of the value), so the value is cut at '#'.

    **`auto` PROBES for a GPU.** It used to mean cpu: the rule was
    `device if device in ("cpu","cuda") else "cpu"`, so the one value everybody
    has configured — `.env.example` ships `WHISPER_DEVICE=auto` and lists `cuda`
    right beside it — could never select the card in the machine. On a box with
    an RTX 4050 that made every caption run CPU-int8 while the GPU sat idle,
    which is a large part of why "the caption button took a lot of time": the
    fast path was documented, advertised, and unreachable.

    `mps` is listed in .env.example but ctranslate2 has no Metal backend, so it
    resolves like `auto` — cuda when there is one, else cpu. On a Mac that is
    cpu, exactly what it already did.
    """
    device = str(WHISPER_DEVICE).split("#", 1)[0].strip().lower()
    if device in ("cpu", "cuda"):
        return device
    return "cuda" if _cuda_device_count() > 0 else "cpu"


# int8 on CPU (what shipped); float16 on GPU, because that is what the tensor
# cores want — int8 on CUDA is not the faster choice there.
_COMPUTE_TYPE = {"cpu": "int8", "cuda": "float16"}

#: Half the VRAM of float16, for a card that cannot hold the bigger one.
_CUDA_FALLBACK_COMPUTE = "int8_float16"


def _resolve_compute_type(device: str) -> str:
    """WHISPER_COMPUTE_TYPE, else the right default for `device`.

    Read straight from the environment rather than config.py, matching
    WHISPER_BACKEND's precedent in this module; '#'-cut for the same
    hand-edited-.env reason as the device.
    """
    raw = str(os.environ.get("WHISPER_COMPUTE_TYPE", "")).split("#", 1)[0].strip().lower()
    return raw or _COMPUTE_TYPE.get(device, "int8")


def _probe_forward_pass(model) -> None:
    """Run ONE real forward pass, so a broken accelerator fails HERE.

    Loading a model is not proof it can execute. Measured on an RTX 4050 with no
    CUDA math libraries present: `WhisperModel(..., device="cuda")` constructed
    fine and then raised `RuntimeError: Library cublas64_12.dll is not found or
    cannot be loaded` from inside `encode`, i.e. on the first forward pass —
    minutes into a caption job, after the UI had promised progress. A
    construction-only check cannot see that.

    This is the same posture `render.compositor._usable_encoder` takes for
    hardware video encoders: `ffmpeg -encoders` LISTS h264_nvenc on a machine
    with no NVIDIA card, so it runs a real ~0.1s null encode instead of trusting
    the listing. Same reasoning, same shape — a listing is a claim, a forward
    pass is evidence.

    Cheap: two seconds of silence through the encoder, tens of milliseconds on
    any device that works.
    """
    import numpy as np
    model.detect_language(audio=np.zeros(32000, dtype=np.float32))


def _get_model(model_size: str | None = None):
    name = model_size or WHISPER_MODEL
    cached = _models.get(name)
    if cached is not None:
        return cached
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        # A bare ImportError here is neither ValueError nor RuntimeError, so
        # main.py's dispatch mapping let it through as an HTTP 500 "internal
        # server error" — the Captions button just failed with no explanation.
        # RuntimeError maps to a 422 carrying this text straight to the toast.
        # (The macOS .app deliberately excludes faster-whisper to stay ~150MB;
        # that build reaches exactly this line.)
        raise RuntimeError(
            "Speech-to-text is unavailable in this build — the 'faster-whisper' "
            "package is not installed. Run the app from source (`uv sync "
            "--all-extras`) to enable captions and transcription."
        ) from e

    device = _resolve_device()
    # A GPU that reports itself is not a GPU that fits the model. large-v3 in
    # float16 is ~3.1GB of weights and batched decoding keeps several chunks of
    # activations alive at once, on a card this app is ALSO encoding video on
    # (the render ladder probes h264_nvenc first). So degrade rather than fail:
    # smaller weights, then the CPU path that has always worked. Captions
    # getting slower is recoverable; captions erroring out is the bug this whole
    # area exists to stop.
    attempts: list[tuple[str, str]] = [(device, _resolve_compute_type(device))]
    if device == "cuda":
        attempts.append(("cuda", _CUDA_FALLBACK_COMPUTE))
        attempts.append(("cpu", _COMPUTE_TYPE["cpu"]))

    for i, (dev, ctype) in enumerate(attempts):
        try:
            cached = WhisperModel(name, device=dev, compute_type=ctype)
            if dev != "cpu":
                _probe_forward_pass(cached)
        except Exception as e:
            if i == len(attempts) - 1:
                raise
            _log.warning("whisper %s/%s unusable (%s) — falling back to %s/%s",
                         dev, ctype, e, attempts[i + 1][0], attempts[i + 1][1])
            continue
        if i:
            _log.info("whisper running on %s/%s", dev, ctype)
        _models[name] = cached
        return cached
    raise RuntimeError("no whisper device attempted")   # attempts is never empty


# Hunt for ggml-* models in the per-OS data dir, legacy XDG/brew locations, and
# ~/.cache. The per-OS dir is checked first; legacy paths are kept so an
# existing macOS install keeps finding its models.
_WHISPER_CPP_MODEL_DIRS = [
    Path(os.environ.get("WHISPER_CPP_MODELS", ""))
        if os.environ.get("WHISPER_CPP_MODELS") else None,
    _pu.user_data_dir("Video AI Editor") / "whisper-cpp",   # new, per-OS
    Path.home() / ".local" / "share" / "video-ai-editor" / "whisper-cpp",  # legacy mac/linux
    Path("/opt/homebrew/share/whisper-cpp/ggml-models"),    # legacy brew (harmless on win)
    Path("/opt/homebrew/share/whisper-cpp"),
    Path.home() / ".cache" / "whisper-cpp",
]
_WHISPER_CPP_MODEL_DIRS = [p for p in _WHISPER_CPP_MODEL_DIRS if p is not None]

_WHISPER_CPP_BIN = _pu.find_binary("whisper-cli", _WHISPER_CPP_MODEL_DIRS) or _pu.exe_name("whisper-cli")


def _whisper_cpp_available() -> bool:
    return Path(_WHISPER_CPP_BIN).exists()


def _whisper_cpp_model_path(name: str) -> Path:
    """Map faster-whisper model names → whisper.cpp ggml model file path.
    Walks the candidate dirs and returns the first hit, else the canonical
    path under the user cache (so error messages are stable)."""
    aliases = {
        "tiny.en": "ggml-tiny.en.bin",
        "tiny":    "ggml-tiny.bin",
        "base.en": "ggml-base.en.bin",
        "base":    "ggml-base.bin",
        "small.en":"ggml-small.en.bin",
        "small":   "ggml-small.bin",
        "medium":  "ggml-medium.bin",
        "large":   "ggml-large-v3.bin",
        "large-v3": "ggml-large-v3.bin",
        "large-v3-turbo": "ggml-large-v3-turbo.bin",
        "turbo":   "ggml-large-v3-turbo.bin",
    }
    fname = aliases.get(name, f"ggml-{name}.bin")
    for d in _WHISPER_CPP_MODEL_DIRS:
        cand = d / fname
        if cand.exists():
            return cand
    # Default for error message
    return _WHISPER_CPP_MODEL_DIRS[0] / fname


def _transcribe_via_whisper_cpp(audio_path: Path, language: str | None,
                                model_size: str | None,
                                task: str = "transcribe") -> Transcript:
    """Run whisper-cli (Metal-accelerated on Apple Silicon) and parse its JSON output.

    `task="translate"` maps to whisper-cli's `-tr`, the same any-language →
    English mode faster-whisper exposes as `task`. Both backends must honour it
    or the English caption target would silently return the source language on
    whichever machine happens to have whisper.cpp installed.
    """
    name = model_size or WHISPER_MODEL
    model_path = _whisper_cpp_model_path(name)
    if not model_path.exists():
        hint = ("Download it with whisper.cpp's download-ggml-model script "
                "(models/download-ggml-model.cmd on Windows, .sh on macOS), or "
                "fetch https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                f"ggml-{name}.bin into {_WHISPER_CPP_MODEL_DIRS[0]}")
        raise RuntimeError(f"whisper-cpp model not found at {model_path}. {hint}")
    # whisper-cli wants 16k mono wav input
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "in.wav"
        subprocess.run(
            [_pu.FFMPEG, "-y", "-i", str(audio_path),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            capture_output=True, check=True,
            **_pu.SUBPROCESS_FLAGS,
        )
        out_prefix = Path(td) / "out"
        # `-l auto` is REQUIRED when no language is given: whisper-cli's
        # default is `-l en` (not auto-detect), which force-decodes Hindi /
        # any non-English audio as English garbage. faster-whisper
        # auto-detects when language=None; this keeps the backends consistent.
        #
        # Anti-hallucination flags (the difference between clean captions and
        # the "लिए भी लिए लिए" repetition-loop garbage weak models emit on
        # music/ambient):
        #   -et 2.8  entropy threshold → fall back to a higher temperature when
        #            the decode looks degenerate, breaking repetition loops.
        #   -mc 0    max-context 0 → don't condition on previous text, so a
        #            hallucinated phrase can't snowball across segments.
        #
        # We do NOT use `-ml 1` (one token per segment): on Devanagari it splits
        # multibyte characters at token boundaries, writing invalid UTF-8 into
        # the JSON ('कौन' → 'क' + two broken bytes + 'न'). Segment mode keeps
        # each segment's `text` field whole and valid; we synthesize word-level
        # timing below by spreading the segment duration across its words.
        cmd = [_WHISPER_CPP_BIN, "-m", str(model_path), "-f", str(wav),
               "-of", str(out_prefix), "-oj",
               "-et", "2.8", "-mc", "0",
               "-l", language if language else "auto"]
        if task == "translate":
            cmd.append("-tr")
        # errors="replace" on the captured pipes (progress meter can split a
        # multibyte char across buffers).
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **_pu.SUBPROCESS_FLAGS)
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
        json_path = Path(f"{out_prefix}.json")
        if not json_path.exists():
            raise RuntimeError(f"whisper-cli produced no JSON output:\n{proc.stdout[-500:]}")
        # Read bytes + decode with errors="replace": the per-token array in the
        # JSON can carry split-multibyte garbage, but each segment's `text`
        # field is valid UTF-8 and survives intact (only the already-broken
        # token bytes become U+FFFD, which we never read).
        data = json.loads(json_path.read_bytes().decode("utf-8", "replace"))

    # Segment mode: each `transcription` entry is a whole sentence/segment with
    # millisecond offsets and a clean `text`. Build Segment objects directly and
    # synthesize even word timing across each segment so word_emphasis captions
    # and word-level tools still work.
    segments: list[Segment] = []
    for seg in data.get("transcription", []) or []:
        offsets = seg.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        text = (seg.get("text") or "")
        # Drop U+FFFD replacement chars left by any rare segment-text byte split,
        # then collapse the whitespace they leave behind.
        text = text.replace("�", "").strip().lstrip("-").strip()
        text = " ".join(text.split())
        # whisper emits "[Music]" / "[_TT_*]" style non-speech markers; drop them.
        if not text or (text.startswith("[") and text.endswith("]")):
            continue
        # Drop degenerate zero/near-zero-duration fragments (a sub-character
        # split occasionally produces a 14.2→14.2 stub).
        if end - start < 0.06:
            continue
        toks = [t for t in text.split(" ") if t]
        words: list[Word] = []
        if toks and end > start:
            step = (end - start) / len(toks)
            for j, tok in enumerate(toks):
                words.append(Word(start=start + j * step,
                                  end=start + (j + 1) * step, word=tok))
        segments.append(Segment(id=len(segments), start=start, end=end,
                                text=text, words=words))

    duration = segments[-1].end if segments else 0.0
    detected_lang = data.get("result", {}).get("language") if isinstance(data.get("result"), dict) else None
    return Transcript(language=str(detected_lang or language or "en"),
                      duration=duration, segments=segments)


# --- model capabilities ----------------------------------------------------
# large-v3-turbo is a distilled large-v3 (4 decoder layers instead of 32) that
# was fine-tuned on TRANSCRIPTION ONLY — the translation task was left out of
# the fine-tune. Measured here on the same batched path, both halves of that
# matter and neither is a guess:
#
#   speed      turbo ran 4.00x faster than large-v3 — 24.8s vs 99.1s for 60s of
#              audio, i.e. 0.41x realtime, faster than the video plays.
#   translate  asked to translate 25s of Hindi, turbo returned five tokens of
#              ellipses ("... ... ... ... ..."). large-v3 returned a correct
#              58-word English sentence. Not "worse" — nonfunctional.
#
# So a transcribe-only model must never serve the English caption target, which
# rides on Whisper's task="translate". That is a fact about the WEIGHTS, so it
# lives here rather than in whichever handler notices it first.
_TURBO_MARKERS = ("turbo",)

#: What to use instead when a transcribe-only model is asked to translate.
TRANSLATION_MODEL = "large-v3"


def _is_turbo(model_name: str | None) -> bool:
    """Match every spelling a turbo model arrives under.

    faster-whisper accepts "turbo" and "large-v3-turbo" as aliases for the same
    weights, and a `model` arg or WHISPER_CAPTION_MODEL can also name a HF repo
    path outright (e.g. "deepdml/faster-whisper-large-v3-turbo-ct2"), so this is
    a substring test rather than a set membership.
    """
    return any(m in str(model_name or "").lower() for m in _TURBO_MARKERS)


def translates(model_name: str | None) -> bool:
    """Can this model serve `task="translate"`? See the measurements above."""
    return not _is_turbo(model_name)


# A turbo model needs anti-repetition decode settings to be usable at all, so
# they travel WITH the model rather than being a separate knob somebody has to
# know about. Measured on 60s of Hindi, turbo batched(8) with language pinned:
#
#   plain                                   16/128 words, unique-token ratio 0.44
#                                           ("एक सब्सक्राइब" x5 — a repetition loop)
#   no_repeat_ngram_size=3                  51/128, ratio 0.96 (loop gone, words lost)
#   repetition_penalty=1.2                  68/128, ratio 0.87
#   ctx OFF + ngram 3 + rep 1.15           143/128, ratio 0.91  <-- shipped
#   + compression_ratio_threshold=1.8      143/128 (adds nothing; omitted)
#   + beam_size=8                          150/128 but 28.7s vs 25.3s (not worth it)
#
# The three together are what work; any one alone loses a third to a half of the
# speech. `no_repeat_ngram_size` is the load-bearing one — it makes a loop
# impossible to form rather than detecting it after the fact — but on its own it
# suppresses real repetition too, which is where those 77 missing words went.
#
# What this does NOT fix: turbo transliterates English loanwords into Devanagari
# ("अपने लास्ट मीटिंग" where large-v3 batched writes "अपनी last meeting"), so it
# is a speed/quality trade and not a free win. Hence it stays opt-in.
_TURBO_DECODE = {
    "condition_on_previous_text": False,
    "no_repeat_ngram_size": 3,
    "repetition_penalty": 1.15,
}


def decode_overrides(model_name: str | None) -> dict:
    """Extra decode kwargs this model needs to produce usable output.

    Keyed on `_is_turbo`, deliberately NOT on `not translates(...)`. Both facts
    happen to be true of turbo today, but they are independent claims — a future
    transcribe-only model need not loop, and a looping model need not be
    transcribe-only — so routing one through the other's predicate would make the
    next model's behaviour a coincidence.
    """
    return dict(_TURBO_DECODE) if _is_turbo(model_name) else {}


def model_for(model_name: str | None, task: str) -> str:
    """The model that can actually do `task`, substituting when it cannot.

    Callers that show the user which model ran should call this themselves so
    the two agree; `transcribe()` applies it again as the enforcement point,
    because Claude and MCP can pass `model` directly and would otherwise get
    ellipses back with no explanation.
    """
    name = str(model_name or WHISPER_MODEL)
    if task == "translate" and not translates(name):
        return TRANSLATION_MODEL
    return name


# --- batched decoding ------------------------------------------------------
# faster-whisper's BatchedInferencePipeline VAD-segments the audio and decodes
# several chunks per forward pass. That is the ONLY parallelism available on this
# path: autoregressive decode does not scale with threads (measured — 16
# cpu_threads gave 92.4s vs 94.6s for 4, i.e. nothing), but batching nearly
# halves the wall clock. Measured on 60s of real Hindi speech, large-v3 int8 on a
# 16-core Windows box:
#
#     sequential              185.6s  (3.09x realtime)   ~9.3 min for 3 minutes
#     batched, batch_size=8    95.2s  (1.59x realtime)   ~4.8 min for 3 minutes
#     batched, batch_size=16   91.0s  (1.52x realtime)
#
# 16 buys a further 4% for more resident memory, so 8 (also faster-whisper's own
# default) ships; VAI_WHISPER_BATCH_SIZE overrides it and 0 forces the old
# sequential path.
#
# Batching also produced BETTER text, not merely faster. The speaker's English
# loanwords came out in Latin — "अपनी last meeting", "STD", "PTSD" — where the
# sequential decode transliterated them into Devanagari: "अपनी लास्ट मीटिंग",
# "एस्टीडी". Each VAD chunk decodes independently, so no previous-text
# conditioning drags the script one way. That is also why two knobs an earlier
# A/B liked are deliberately NOT set here: `condition_on_previous_text=False` has
# nothing to disable once chunks are independent, and `beam_size=8` cost 24%
# (118.1s vs 95.2s) to produce byte-identical output.
#
# The cost is progress GRANULARITY. Batched decoding emitted 2 segments for 60s
# where sequential emitted 18, so `on_progress` fires roughly every 30s of audio
# instead of every 3s. The UI pairs the bar with elapsed time and an ETA, so the
# coarser bar still reads as moving — and halving the total wait is worth more
# than a smoother bar during it.
_DEFAULT_BATCH_SIZE = 8


def _batch_size() -> int:
    """Resolve VAI_WHISPER_BATCH_SIZE; 0 disables batching entirely.

    Tolerates a polluted .env value the same way `_resolve_device` does — those
    files are read literally, so an inline `# comment` after a value becomes
    part of the value.
    """
    raw = os.environ.get("VAI_WHISPER_BATCH_SIZE")
    if raw is None or not str(raw).strip():
        return _DEFAULT_BATCH_SIZE
    try:
        return max(0, int(str(raw).split("#", 1)[0].strip()))
    except ValueError:
        _log.warning("VAI_WHISPER_BATCH_SIZE=%r is not an integer — using %d",
                     raw, _DEFAULT_BATCH_SIZE)
        return _DEFAULT_BATCH_SIZE


def _open_decode(model, audio_path: Path, *, language: str | None, task: str,
                 mode: str, batch_size: int, extra: dict | None = None):
    """Start ONE decode attempt; returns faster-whisper's `(segments, info)`.

    No audio has been decoded when this returns — the work happens as the
    generator is consumed — but everything that can *fail up front* does so
    here: reading the audio, loading Silero VAD, detecting the language. That is
    what makes "try the fast mode, fall back to a simpler one" viable rather
    than a guess (see `_DECODE_MODES`).

    `extra` carries per-model decode requirements (`decode_overrides`) — the
    anti-repetition settings a turbo model cannot produce usable output without.
    Both call shapes accept the same kwargs, so it applies to either.
    """
    extra = extra or {}
    if mode == "batched":
        from faster_whisper import BatchedInferencePipeline
        return BatchedInferencePipeline(model=model).transcribe(
            str(audio_path), language=language, task=task,
            word_timestamps=True, vad_filter=True, batch_size=batch_size,
            **extra,
        )
    return model.transcribe(
        str(audio_path), language=language, task=task,
        word_timestamps=True,
        # "sequential_novad" is the last resort — see _DECODE_MODES.
        vad_filter=(mode == "sequential"),
        **extra,
    )


# Tried in order, each one degrading a capability rather than the feature:
#   batched           — fastest, needs BatchedInferencePipeline (faster-whisper
#                       >= 1.1) and a working VAD.
#   sequential        — what shipped before batching.
#   sequential_novad  — `vad_filter=True` loads Silero VAD, which faster-whisper
#       ships as a DATA FILE inside its own package (`faster_whisper/assets/
#       silero_vad_v6.onnx`), not as Python code. PyInstaller collects the module
#       but NOT a package's data files unless the spec asks, so the packaged
#       Windows app raised onnxruntime's `NoSuchFile` mid-setup. That type is
#       neither ValueError nor RuntimeError, so it escaped main.py's dispatch
#       mapping and reached the user as a bare HTTP 500 with no captions and no
#       clue — while the identical click worked in dev, where site-packages has
#       the asset. The spec now collects those data files (see "Video AI
#       Editor.spec"), but the degrade stays: VAD only trims silence before
#       decoding, so transcription is fully functional without it. A packaging
#       regression should cost caption *quality*, never the whole feature.
# The LAST mode's exception always propagates — this is a ladder, not a blanket
# swallow.
_DECODE_MODES = ("batched", "sequential", "sequential_novad")


def _collect_segments(segments_iter, info, *,
                      on_progress: Callable[[float, float, float], None] | None,
                      should_cancel: Callable[[], bool] | None) -> Transcript:
    """Drain a decode generator into a Transcript, reporting progress.

    `info.duration` is the audio length whisper is working through, so a
    segment's end time IS the progress denominator.
    """
    total = float(getattr(info, "duration", 0.0) or 0.0)
    segments: list[Segment] = []
    for s in segments_iter:
        if should_cancel is not None and should_cancel():
            raise TranscriptionCancelled(
                f"transcription cancelled after {s.start:.0f}s of {total:.0f}s"
            )
        segments.append(Segment(
            # The batched pipeline builds its own Segment objects, so don't
            # assume every field the sequential path sets is present.
            id=getattr(s, "id", len(segments)),
            start=s.start,
            end=s.end,
            text=s.text,
            words=[
                Word(start=w.start, end=w.end, word=w.word,
                     prob=getattr(w, "probability", 1.0))
                for w in (s.words or [])
            ],
        ))
        if on_progress is not None and total > 0:
            on_progress(min(1.0, float(s.end) / total), float(s.end), total)
    return Transcript(
        language=info.language,
        duration=info.duration,
        segments=segments,
    )


def detect_language(audio_path: Path, model_size: str | None = None) -> str | None:
    """Cheap probe of the SPOKEN language — seconds, not a full transcription.

    Needed because the caption target decides the decode *task*, and that
    decision cannot be made after the fact without paying for a second decode:
    Whisper translates into English only, so captioning a Chinese video in Hindi
    means asking for English on the single pass and then translating en→hi
    locally (the only direction Argos publishes a Hindi package for).

    Uses the same cached model instance the real decode will use, so the model
    load is not paid twice. Returns None when detection is unavailable — callers
    must treat that as "unknown" and stay on a route that still works.
    """
    try:
        model = _get_model(model_size)
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(audio_path), sampling_rate=16000)
        lang, _prob, _all = model.detect_language(audio=audio, vad_filter=True)
        return str(lang).lower() if lang else None
    except Exception as e:                      # missing dep, odd codec, …
        _log.warning("language detection unavailable (%s)", e)
        return None


def transcribe(audio_path: Path, language: str | None = None,
               model_size: str | None = None,
               backend: str | None = None,
               task: str = "transcribe",
               on_progress: Callable[[float, float, float], None] | None = None,
               should_cancel: Callable[[], bool] | None = None) -> Transcript:
    """Run whisper. `language=None` triggers auto-detect.

    `backend`:
      - "auto" (default)  — whisper-cli (Metal-accelerated) when the binary
        AND the ggml model for the requested size are present; otherwise
        faster-whisper. On Apple Silicon this is ~4-5x faster (measured:
        12s vs 54s for 40s of Hindi audio) — the difference between
        captions feeling instant and feeling stuck.
      - "faster_whisper"  — force CPU int8 via faster-whisper
      - "whisper_cpp"     — force whisper-cli (falls back if unavailable)

    `task`:
      - "transcribe" (default) — text in the language that was spoken.
      - "translate"            — whisper's own any-language → ENGLISH mode. It
        is one decode pass with no second model, which makes it by far the best
        route to English subtitles for foreign-language footage. Whisper cannot
        translate INTO anything but English; other targets need a separate
        translation step (see agent.dispatch.auto_caption).

    The faster-whisper path decodes in BATCHES by default (~2x faster and, on
    code-switched speech, more faithful) and falls back through plain sequential
    decoding to sequential-without-VAD if the setup for a faster mode fails —
    see `_DECODE_MODES` and the measurements above it.

    `on_progress(fraction, seconds_done, total_seconds)` is called as segments
    decode, and `should_cancel()` is polled between them. Decoding large-v3 on
    CPU still runs slower than realtime — measured 1.59x realtime batched, 3.09x
    sequential, on a 16-core Windows box — so a caller with no way to show
    progress or stop early is indistinguishable from a hung app, which is
    exactly how the Captions button was being reported. Both hooks are optional;
    passing neither keeps the previous behaviour exactly. Note batching makes
    `on_progress` COARSER (roughly one call per 30s of audio, vs ~3s
    sequentially) because it emits far fewer, longer segments.
    """
    # Enforcement point for model/task compatibility. A transcribe-only model
    # asked to translate returns ellipses, not an error, so a caller that passed
    # `model` directly (Claude, MCP) would otherwise get an empty-looking
    # caption track and nothing to explain it. `model_for` is idempotent, so a
    # caller that already resolved it — auto_caption does, to name the model it
    # actually ran — passes through unchanged.
    resolved = model_for(model_size, task)
    if model_size is not None and resolved != model_size:
        _log.warning("model %r cannot translate — using %r for task=translate",
                     model_size, resolved)
    model_size = resolved

    backend = backend or os.environ.get("WHISPER_BACKEND") or "auto"
    if backend == "auto":
        name = model_size or WHISPER_MODEL
        if _whisper_cpp_available() and _whisper_cpp_model_path(name).exists():
            backend = "whisper_cpp"
        else:
            backend = "faster_whisper"
    if backend == "whisper_cpp" and _whisper_cpp_available():
        return _transcribe_via_whisper_cpp(audio_path, language, model_size, task=task)
    model = _get_model(model_size)
    batch_size = _batch_size()
    extra = decode_overrides(model_size)
    modes = [m for m in _DECODE_MODES if m != "batched" or batch_size > 0]
    for i, mode in enumerate(modes):
        try:
            segments_iter, info = _open_decode(
                model, audio_path, language=language, task=task,
                mode=mode, batch_size=batch_size, extra=extra,
            )
            return _collect_segments(segments_iter, info,
                                     on_progress=on_progress,
                                     should_cancel=should_cancel)
        except TranscriptionCancelled:
            # The user pressed Cancel. Retrying in a slower mode would ignore
            # them and burn minutes doing it.
            raise
        except Exception as e:
            if i == len(modes) - 1:
                raise
            _log.warning("whisper %s decode failed (%s) — retrying as %s",
                         mode, e, modes[i + 1])
    raise RuntimeError("no decode mode attempted")   # modes is never empty
