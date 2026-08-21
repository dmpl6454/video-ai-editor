"""Local translation via MADLAD-400 (Google), a 450-language open NMT model
run through CTranslate2 — the SAME inference engine this app already bundles
for Whisper transcription.

**Replaces Argos Translate.** Argos was retired after two DIFFERENT,
unrelated real-clip phrases came back completely untranslated —
"Goddamn right." and, separately, "To watch a bunch of junkies." (in its
real surrounding sentence). Each translates fine in ISOLATION, so this
wasn't one fixable defect; a hand-curated dictionary of "phrases Argos
happens to fail on" was tried first and does not scale, because the very
next test on the same video found a second, unrelated failure. Verified
directly before switching: MADLAD-400 translates BOTH of those exact
failures correctly, in both Hindi and Spanish, and also fixed two more
sentences Argos had translated into outright nonsense rather than merely
leaving in English (`Argos: "आप देवदार हैं।"` — "you are a cedar tree" —
vs `MADLAD: "आप बिल्कुल सही हैं।"` — "you are absolutely right").

Model: `Heng666/madlad400-3b-mt-ct2-int8`, an int8-quantized CTranslate2
build of Google's 3B-parameter MADLAD-400. **Apache-2.0** — safe for
commercial use, unlike NLLB's CC-BY-NC restriction (NLLB was considered and
rejected specifically for that reason). Needs no torch: pure `ctranslate2` +
`sentencepiece`. That matters beyond dependency weight — it's what makes
translation properly available in BOTH packaged builds for the first time.
Argos pulled in `stanza` (PyTorch-based) for sentence splitting, and the
macOS build (`build_app.sh`) excludes torch entirely to hit its size target,
so translation was never reliably available in the packaged Mac app even
though nothing in `check_features.py` said so outright.

Downloads ~3GB on first use into the per-OS user data dir (same convention
as Real-ESRGAN/RIFE — see `_model_dir`), cached forever after. This is a
real size trade against Argos's ~50MB per-direction packages, but it matches
the app's existing "large opt-in local AI model" pattern rather than
introducing a new one — large-v3 Whisper is a comparable download.

Unlike Argos, MADLAD needs only a TARGET-language tag (`<2hi>`, `<2es>`) and
auto-detects the source language from the text itself — there is no
per-language-pair "package" to install. `from_code` stays in this module's
public function signatures for compatibility with existing callers
(dispatch.py's translate-pivot logic, `translate_captions`) and to gate the
residual-English fallback below, but it no longer selects a model.
"""
from __future__ import annotations
import re
import threading
from pathlib import Path
from typing import Iterable

from .. import platformutil as _pu

_MODEL_REPO = "Heng666/madlad400-3b-mt-ct2-int8"
_MODEL_NAME = "madlad400-3b-mt-ct2-int8"

# Decode params tuned against real failures (see module docstring). Default
# beam search left a mild phrase-doubling artifact on some sentences —
# "Say my name." -> "मेरा नाम कहो, मेरा नाम कहो।" (said twice) — the same
# class of defect Whisper turbo's decode needed `no_repeat_ngram_size` +
# `repetition_penalty` for (ingest/transcribe.py). Verified this removes the
# doubling on every case it appeared in without degrading anything else.
_BEAM_SIZE = 4
_REPETITION_PENALTY = 1.3
_NO_REPEAT_NGRAM_SIZE = 3

_lock = threading.Lock()
_translator = None
_tokenizer = None

# Loading MADLAD-400 (~3GB) and running batched inference on CPU goes through
# CTranslate2's Intel-MKL allocator, which raises a bare `RuntimeError:
# mkl_malloc: failed to allocate memory` — a native-library string with no
# hint of WHY, unlike this app's other failure paths (`_render_failure_message`
# for ffmpeg, the `download-ggml-model.sh` hint for a missing Whisper model).
# Reproduced live: running this app's dev server AND its own packaged .app/
# .exe at the same time on a 16GB machine leaves ~3-4GB free once Whisper
# large-v3 is resident in both processes, which is not enough headroom for a
# second MADLAD load — the SAME machine's translation succeeds cleanly once
# only one heavy process is running. This is a real memory ceiling, not a
# bug this module can route around (there is no smaller official MADLAD
# quantization to fall back to), so the fix is a clear, actionable message
# in place of the opaque native string — matching the house convention that
# a raised failure must say what to actually do about it.
_MEMORY_ERROR_MARKERS = ("mkl_malloc", "bad_alloc", "out of memory", "failed to allocate")


def _is_memory_error(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _MEMORY_ERROR_MARKERS)


def _reraise_as_clear_memory_error(exc: Exception) -> None:
    raise RuntimeError(
        "translation ran out of memory while loading/running MADLAD-400 "
        "(~3GB). This usually means another heavy process — most often a "
        "second copy of this app (packaged build + dev server, or two "
        "sessions transcribing/translating at once) — is holding the rest "
        "of your RAM. Close other AI-heavy processes/instances and try "
        f"again. (native error: {exc})"
    ) from exc


def _model_dir() -> Path:
    """Where the MADLAD model lives once downloaded — same per-OS user data
    dir convention as Real-ESRGAN/RIFE (ai/upscale.py::_esrgan_dir,
    ai/rife.py::_rife_dir), so it survives an app update and is one place a
    user could point at to delete/reclaim the ~3GB if they never use
    translated captions."""
    return _pu.user_data_dir("Video AI Editor") / "models" / _MODEL_NAME


def _ensure_model_downloaded() -> Path:
    d = _model_dir()
    if (d / "model.bin").exists():
        return d
    try:
        from huggingface_hub import snapshot_download
        d.mkdir(parents=True, exist_ok=True)
        snapshot_download(_MODEL_REPO, local_dir=str(d))
    except Exception as e:
        raise RuntimeError(
            f"could not download the translation model ({_MODEL_REPO}, ~3GB) — "
            f"check your internet connection: {e}"
        ) from e
    return d


def _load_translator(model_dir: Path):
    """CUDA first, CPU on ANY failure — same degrade-don't-crash ladder
    `ingest/transcribe.py::_get_model` uses for Whisper. A GPU that's merely
    visible (get_cuda_device_count() > 0) is not proof the math libraries are
    actually loadable (see `_add_cuda_dll_dirs`'s own docstring), and even a
    real CUDA failure here must not take caption generation down with it."""
    import ctranslate2
    try:
        from ..ingest.transcribe import _add_cuda_dll_dirs
        _add_cuda_dll_dirs()
        if ctranslate2.get_cuda_device_count() > 0:
            return ctranslate2.Translator(str(model_dir), device="cuda")
    except Exception:
        pass
    try:
        return ctranslate2.Translator(str(model_dir), device="cpu")
    except Exception as e:
        # Unlike the CUDA attempt above, a CPU-load failure has nowhere left
        # to degrade to — but it must still say WHY, not just fail raw.
        if _is_memory_error(e):
            _reraise_as_clear_memory_error(e)
        raise


def _get_translator_and_tokenizer():
    global _translator, _tokenizer
    if _translator is not None:
        return _translator, _tokenizer
    with _lock:
        if _translator is not None:  # re-check: lost the race to another thread
            return _translator, _tokenizer
        import sentencepiece as spm
        model_dir = _ensure_model_downloaded()
        translator = _load_translator(model_dir)
        tokenizer = spm.SentencePieceProcessor(model_file=str(model_dir / "spiece.model"))
        _translator, _tokenizer = translator, tokenizer
    return _translator, _tokenizer


def _madlad_translate_batch(texts: list[str], to_code: str) -> list[str]:
    """Translate every text in ONE `translate_batch` call. MADLAD has no
    per-pair "package" the way Argos did, so there's nothing to cache per
    call — batching here is purely a throughput win (one forward pass over
    the whole caption track instead of one call per cue)."""
    non_empty = [i for i, t in enumerate(texts) if t.strip()]
    out = list(texts)
    if not non_empty:
        return out
    translator, tokenizer = _get_translator_and_tokenizer()
    prefix = f"<2{to_code}> "
    batch = [tokenizer.encode(prefix + texts[i], out_type=str) for i in non_empty]
    try:
        results = translator.translate_batch(
            batch, beam_size=_BEAM_SIZE,
            repetition_penalty=_REPETITION_PENALTY,
            no_repeat_ngram_size=_NO_REPEAT_NGRAM_SIZE,
        )
    except Exception as e:
        if _is_memory_error(e):
            _reraise_as_clear_memory_error(e)
        raise
    for i, r in zip(non_empty, results):
        out[i] = tokenizer.decode(r.hypotheses[0])
    return out


# --- Residual-English fallback (Claude) -------------------------------------
#
# Kept as a defensive backstop even though MADLAD closed every failure case
# measured so far (see module docstring) — it is cheap when it never fires
# (a single Latin-script scan per translated string) and protects against any
# phrase this model has its own blind spot on that hasn't been found yet.
#
# The general, scalable signal: the source was pure English, so a genuinely
# successful translation into Hindi should contain (almost) no Latin-script
# prose. Any surviving run of 2+ Latin-alphabet WORDS is real evidence of a
# failed translation — as opposed to a single loanword/acronym/number
# (`P2P`, `STD`, `$130`) that both Whisper and the translator legitimately
# leave untouched. When found, that specific span (not the whole caption) is
# sent to Claude for translation and spliced back in — ANTHROPIC_API_KEY is
# already required for this app's chat assistant, so this reuses an
# existing, already-configured dependency. Missing key or any API failure
# degrades to leaving the residual text exactly as produced — never raises,
# never blocks caption generation.
_RESIDUAL_LATIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\s'\".,!?-]*[A-Za-z0-9.!?\"']")


def _residual_english_spans(text: str) -> list[tuple[int, int, str]]:
    """Maximal runs of Latin-script text remaining in `text`, filtered to
    ones containing at least 2 real (2+ letter) words — a lone acronym or
    number is not evidence of a failed translation."""
    spans = []
    for m in _RESIDUAL_LATIN_RE.finditer(text):
        run = m.group(0)
        words = [w for w in re.split(r"\s+", run) if re.search(r"[A-Za-z]{2,}", w)]
        if len(words) >= 2:
            spans.append((m.start(), m.end(), run))
    return spans


def _claude_translate_phrase(phrase: str, to_code: str) -> str | None:
    """Ask Claude to translate one short phrase. Returns None (never raises)
    on a missing key, an unsupported target, or any API/network failure —
    the caller must treat that as "leave the phrase as-is", not as an error."""
    from ..config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    lang_name = {"hi": "Hindi (Devanagari script)"}.get(to_code)
    if not ANTHROPIC_API_KEY or not lang_name:
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Translate this short phrase into {lang_name}. Reply with "
                    f"ONLY the translation — no quotes, no explanation, no "
                    f"romanization:\n\n{phrase}"
                ),
            }],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
    except Exception:
        return None


def _fill_residual_english(text: str, from_code: str, to_code: str) -> str:
    """Detect and Claude-translate any residual English left by the model.
    Scoped to en->hi: Spanish is ALSO Latin-script, so "residual Latin text"
    isn't a usable failure signal there — a genuinely correct Spanish
    translation and a failed one look the same to this regex."""
    if from_code != "en" or to_code != "hi":
        return text
    spans = _residual_english_spans(text)
    if not spans:
        return text
    # Right-to-left so earlier offsets stay valid as spans are replaced.
    for start, end, phrase in reversed(spans):
        translated = _claude_translate_phrase(phrase.strip(), to_code)
        if translated:
            text = text[:start] + translated + text[end:]
    return text


def translate_text(text: str, *, from_code: str = "en", to_code: str = "hi") -> str:
    if not text.strip():
        return text
    out = _madlad_translate_batch([text], to_code)[0]
    return _fill_residual_english(out, from_code, to_code)


def translate_segments(segments: Iterable[dict], *, from_code: str = "en",
                       to_code: str = "hi") -> list[dict]:
    """Translate the .text of each segment, leave timing untouched."""
    segs = list(segments)
    texts = [str(s.get("text", "") or "") for s in segs]
    translated = _madlad_translate_batch(texts, to_code)
    out = []
    for seg, tr in zip(segs, translated):
        tr = _fill_residual_english(tr.strip(), from_code, to_code)
        out.append({**seg, "text": tr})
    return out
