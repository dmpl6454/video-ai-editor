"""ai/translate.py — the MADLAD-400 backend (replaces Argos Translate).

Covers the plumbing Argos never needed: an on-demand ~3GB model download into
the per-OS user data dir, a CUDA-first/CPU-fallback loader (mirroring
ingest/transcribe.py's degrade ladder for the same reason — a visible GPU is
not proof the math libraries actually load), and batched translation. The
translation-quality claims themselves (fixes the two real Argos failures,
plus the repetition-penalty tuning) are pinned by the real, gated
integration tests in test_translate_fallback.py, which already exercise the
actual model end to end.
"""
from __future__ import annotations

import video_ai_editor.ai.translate as T


def test_model_dir_uses_the_same_per_os_convention_as_other_ai_models(monkeypatch, tmp_path):
    """Same convention as ai/upscale.py::_esrgan_dir / ai/rife.py::_rife_dir —
    <user data dir>/models/<name> — so it survives an app update."""
    monkeypatch.setattr(T._pu, "user_data_dir", lambda name: tmp_path / name)
    d = T._model_dir()
    assert d == tmp_path / "Video AI Editor" / "models" / T._MODEL_NAME


def test_ensure_model_downloaded_skips_download_when_already_present(monkeypatch, tmp_path):
    model_dir = tmp_path / T._MODEL_NAME
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"fake")
    monkeypatch.setattr(T, "_model_dir", lambda: model_dir)

    called = []
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download",
        lambda *a, **kw: called.append((a, kw)),
    )
    out = T._ensure_model_downloaded()
    assert out == model_dir
    assert called == [], "must not re-download when model.bin already exists"


def test_ensure_model_downloaded_wraps_a_network_failure_in_a_clear_message(monkeypatch, tmp_path):
    """A bare huggingface_hub exception (e.g. a connection error) must not
    surface as an opaque traceback — the message must name the model and
    hint at the real cause, matching the house 'degrade with a clear error,
    never a bare crash' convention."""
    model_dir = tmp_path / T._MODEL_NAME
    monkeypatch.setattr(T, "_model_dir", lambda: model_dir)

    def boom(*a, **kw):
        raise OSError("no route to host")
    monkeypatch.setattr("huggingface_hub.snapshot_download", boom)

    try:
        T._ensure_model_downloaded()
        assert False, "expected a RuntimeError"
    except RuntimeError as e:
        msg = str(e)
        assert "internet" in msg.lower() or "connection" in msg.lower() or "no route" in msg.lower()
        assert T._MODEL_REPO in msg


def test_load_translator_falls_back_to_cpu_when_cuda_is_visible_but_unusable(monkeypatch, tmp_path):
    """Mirrors ingest/transcribe.py's own reasoning: `get_cuda_device_count() > 0`
    only means a GPU is VISIBLE, not that the math libraries actually load —
    a real CUDA failure at Translator-construction time must degrade to CPU,
    never take caption generation down with it."""
    import ctranslate2 as real_ct2

    calls = []

    class FakeTranslator:
        def __init__(self, model_dir, device):
            calls.append(device)
            if device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found")

    monkeypatch.setattr(real_ct2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(real_ct2, "Translator", FakeTranslator)
    monkeypatch.setattr(
        "video_ai_editor.ingest.transcribe._add_cuda_dll_dirs", lambda: []
    )

    T._load_translator(tmp_path)
    assert calls == ["cuda", "cpu"], "must try cuda first, then fall back to cpu on failure"


def test_load_translator_uses_cpu_directly_when_no_cuda_device_is_visible(monkeypatch, tmp_path):
    import ctranslate2 as real_ct2

    calls = []

    class FakeTranslator:
        def __init__(self, model_dir, device):
            calls.append(device)

    monkeypatch.setattr(real_ct2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(real_ct2, "Translator", FakeTranslator)
    monkeypatch.setattr(
        "video_ai_editor.ingest.transcribe._add_cuda_dll_dirs", lambda: []
    )

    T._load_translator(tmp_path)
    assert calls == ["cpu"], "must not even attempt cuda when no device is visible"


def test_madlad_translate_batch_skips_empty_strings_without_calling_the_model(monkeypatch):
    """An empty/whitespace-only segment (e.g. a music-only gap) must not be
    sent to the model at all — batching real work with blanks would waste a
    model slot and risk the tokenizer choking on an empty string."""
    def fail_if_called():
        raise AssertionError("must not load the model for an all-blank batch")
    monkeypatch.setattr(T, "_get_translator_and_tokenizer", fail_if_called)
    out = T._madlad_translate_batch(["", "   "], "hi")
    assert out == ["", "   "]


def test_is_memory_error_recognizes_the_real_mkl_failure_string():
    """Reproduced live: running this app's dev server alongside its own
    packaged build on a 16GB machine left too little RAM for a second
    MADLAD load, and CTranslate2's CPU path raised exactly this string."""
    assert T._is_memory_error(RuntimeError("mkl_malloc: failed to allocate memory"))
    assert T._is_memory_error(MemoryError())
    assert T._is_memory_error(RuntimeError("std::bad_alloc"))


def test_is_memory_error_does_not_flag_an_unrelated_failure():
    assert not T._is_memory_error(RuntimeError("Library cublas64_12.dll is not found"))
    assert not T._is_memory_error(OSError("no route to host"))


def test_load_translator_wraps_a_cpu_memory_failure_in_an_actionable_message(monkeypatch, tmp_path):
    """A CPU-side allocation failure has nowhere left to degrade to (CPU is
    already the last rung of the ladder) — but it must not surface the bare
    native string. The message must name the real cause (another heavy
    process/instance competing for RAM) and what to do about it."""
    import ctranslate2 as real_ct2

    class FakeTranslator:
        def __init__(self, model_dir, device):
            if device == "cpu":
                raise RuntimeError("mkl_malloc: failed to allocate memory")

    monkeypatch.setattr(real_ct2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(real_ct2, "Translator", FakeTranslator)
    monkeypatch.setattr(
        "video_ai_editor.ingest.transcribe._add_cuda_dll_dirs", lambda: []
    )

    try:
        T._load_translator(tmp_path)
        assert False, "expected a RuntimeError"
    except RuntimeError as e:
        msg = str(e)
        assert "mkl_malloc" not in msg.split("native error:")[0], (
            "the raw native string must not be the headline message"
        )
        assert "ram" in msg.lower() or "memory" in msg.lower()
        assert "another" in msg.lower() or "instance" in msg.lower() or "process" in msg.lower()


def test_load_translator_does_not_rewrite_an_unrelated_cpu_failure(monkeypatch, tmp_path):
    """Only a genuine memory failure gets the friendlier rewrite — anything
    else (e.g. a corrupt model file) must propagate as-is so its real cause
    stays visible rather than being masked as an out-of-memory report."""
    import ctranslate2 as real_ct2

    class FakeTranslator:
        def __init__(self, model_dir, device):
            if device == "cpu":
                raise RuntimeError("invalid model file: bad magic number")

    monkeypatch.setattr(real_ct2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(real_ct2, "Translator", FakeTranslator)
    monkeypatch.setattr(
        "video_ai_editor.ingest.transcribe._add_cuda_dll_dirs", lambda: []
    )

    try:
        T._load_translator(tmp_path)
        assert False, "expected a RuntimeError"
    except RuntimeError as e:
        assert "bad magic number" in str(e)
        assert "another" not in str(e).lower()


def test_madlad_translate_batch_wraps_a_memory_failure_from_translate_batch_itself(monkeypatch):
    """The model can load fine and still run out of memory DURING inference
    (a big batch, or RAM pressure that appeared after load) — that failure
    needs the same actionable rewrite as a load-time one."""
    class FakeTranslator:
        def translate_batch(self, batch, **kw):
            raise RuntimeError("mkl_malloc: failed to allocate memory")

    class FakeTokenizer:
        def encode(self, text, out_type=str):
            return [text]

    monkeypatch.setattr(T, "_get_translator_and_tokenizer",
                        lambda: (FakeTranslator(), FakeTokenizer()))
    try:
        T._madlad_translate_batch(["hello"], "hi")
        assert False, "expected a RuntimeError"
    except RuntimeError as e:
        assert "another" in str(e).lower() or "instance" in str(e).lower()


def test_madlad_translate_batch_preserves_position_and_skips_blanks_within_a_mixed_batch(monkeypatch):
    class FakeHyp:
        def __init__(self, tokens):
            self.hypotheses = [tokens]

    class FakeTranslator:
        def translate_batch(self, batch, **kw):
            # Echo back a marker so we can verify which inputs were actually sent.
            return [FakeHyp([f"got:{i}"]) for i in range(len(batch))]

    class FakeTokenizer:
        def encode(self, text, out_type=str):
            return [text]

        def decode(self, tokens):
            return tokens[0]

    monkeypatch.setattr(T, "_get_translator_and_tokenizer",
                        lambda: (FakeTranslator(), FakeTokenizer()))
    out = T._madlad_translate_batch(["hello", "", "world"], "hi")
    assert out[1] == "", "the blank entry must be left untouched, not sent to the model"
    assert out[0] != "hello" and out[2] != "world", "the non-blank entries must be translated"
