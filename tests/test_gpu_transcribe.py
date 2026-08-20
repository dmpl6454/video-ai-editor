"""GPU transcription plumbing, and the Windows-only traps around it.

Context, all measured on one 60s clip of Hindi speech with large-v3 on a 16-core
Windows box with an RTX 4050:

    sequential  CPU int8        185.6s   3.09x realtime   117 words
    batched(8)  CPU int8         95.2s   1.59x realtime   128 words
    batched(8)  CUDA float16      8.4s   0.14x realtime   128 words

The GPU path is the only speedup here that costs nothing in accuracy — same
weights, same transcript — and it was unreachable because `WHISPER_DEVICE=auto`
resolved to cpu. These tests pin the plumbing that makes it reachable AND the
two Windows footguns that make it silently not work: DLLs on no search path, and
a model download that cannot create a symlink.
"""
from __future__ import annotations

import os

import pytest

from video_ai_editor import config as cfg
from video_ai_editor import platformutil as _pu
from video_ai_editor.ingest import transcribe as T


@pytest.fixture(autouse=True)
def _no_cached_cuda_answer():
    T._cuda_device_count.cache_clear()
    yield
    T._cuda_device_count.cache_clear()


# --- the DLL search path ----------------------------------------------------

def test_cuda_dll_dirs_go_on_PATH_not_just_add_dll_directory(monkeypatch, tmp_path):
    """`os.add_dll_directory` is NOT sufficient and this is the one thing that
    took two attempts to get right: with only that call, loading still failed
    with "Library cublas64_12.dll is not found or cannot be loaded"; prepending
    the same directories to PATH worked. ctranslate2 loads by plain library name,
    which searches PATH.
    """
    if not _pu.IS_WINDOWS:
        pytest.skip("PATH-based DLL search is a Windows concern")

    site = tmp_path / "Lib" / "site-packages" / "nvidia"
    for lib in ("cublas", "cudnn"):
        (site / lib / "bin").mkdir(parents=True)
    monkeypatch.setattr(T.sys, "prefix", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\already\\here")

    found = T._add_cuda_dll_dirs()
    assert len(found) == 2, found
    on_path = os.environ["PATH"].split(os.pathsep)
    for d in found:
        assert d in on_path, f"{d} was found but never put on PATH"
    # Prepended, so a wheel's copy wins over a mismatched system-wide one.
    assert on_path.index(found[0]) < on_path.index("C:\\already\\here")


def test_adding_the_dll_dirs_twice_does_not_duplicate_PATH(monkeypatch, tmp_path):
    """It is called from an lru_cached probe AND from the features report, so
    re-entry has to be free — an unbounded PATH is its own failure."""
    if not _pu.IS_WINDOWS:
        pytest.skip("PATH-based DLL search is a Windows concern")

    (tmp_path / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr(T.sys, "prefix", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\already\\here")

    T._add_cuda_dll_dirs()
    first = os.environ["PATH"]
    T._add_cuda_dll_dirs()
    assert os.environ["PATH"] == first


def test_no_nvidia_wheels_means_no_PATH_change(monkeypatch, tmp_path):
    monkeypatch.setattr(T.sys, "prefix", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\already\\here")
    assert T._add_cuda_dll_dirs() == []
    assert os.environ["PATH"] == "C:\\already\\here"


# --- the download that cannot make a symlink -------------------------------

def test_hf_symlinks_are_disabled_on_windows_by_default(monkeypatch):
    """A first-run model download died with `OSError: [WinError 1314] A required
    privilege is not held by the client` while creating a cache symlink. The hub
    warns that the machine does not support symlinks and then tries anyway, so
    its own detection cannot be trusted."""
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    cfg._avoid_hf_symlink_failures()
    if _pu.IS_WINDOWS:
        assert os.environ.get("HF_HUB_DISABLE_SYMLINKS") == "1"
    else:
        assert "HF_HUB_DISABLE_SYMLINKS" not in os.environ


def test_an_explicit_hf_symlink_choice_is_never_overridden(monkeypatch):
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS", "0")
    cfg._avoid_hf_symlink_failures()
    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "0"


# --- macOS: everything must be a no-op, not a different behaviour ----------
#
# There is no CUDA on Apple hardware and no Metal backend in ctranslate2, so a
# Mac transcribes on the CPU exactly as it did before. These run on EVERY
# platform (the Windows-ness is monkeypatched) precisely because the macOS CI job
# would otherwise be the only place they execute — and the tests above that need
# real Windows path semantics skip there.

@pytest.fixture
def as_posix(monkeypatch):
    """Pretend we are not on Windows, with no CUDA device present."""
    monkeypatch.setattr(_pu, "IS_WINDOWS", False)
    monkeypatch.setattr(T._pu, "IS_WINDOWS", False)
    monkeypatch.setattr(cfg._pu, "IS_WINDOWS", False)
    monkeypatch.setattr(T, "_cuda_device_count", lambda: 0)


def test_the_dll_path_hack_is_a_no_op_off_windows(as_posix, monkeypatch, tmp_path):
    """The nvidia wheels ship .so files the linker finds via RPATH, and
    `sys.prefix/Lib/site-packages` is a Windows layout that does not exist on a
    Mac at all."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert T._add_cuda_dll_dirs() == []
    assert os.environ["PATH"] == "/usr/bin:/bin"


@pytest.mark.parametrize("env", ["auto", "auto  # auto | cpu | cuda | mps",
                                 "mps", "cpu", ""])
def test_a_mac_still_resolves_to_cpu(as_posix, monkeypatch, env):
    """Byte-for-byte the old behaviour: before this change every one of these
    returned cpu, and on a Mac every one still does. `mps` in particular is
    documented in .env.example but ctranslate2 has no Metal backend."""
    monkeypatch.setattr(T, "WHISPER_DEVICE", env)
    assert T._resolve_device() == "cpu"
    assert T._resolve_compute_type("cpu") == "int8"


def test_hf_symlink_default_is_not_applied_off_windows(as_posix, monkeypatch):
    """WinError 1314 is a Windows failure. Disabling the hub's symlinks on a Mac
    would cost disk for nothing."""
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    cfg._avoid_hf_symlink_failures()
    assert "HF_HUB_DISABLE_SYMLINKS" not in os.environ


def test_batching_is_device_independent_so_a_mac_gets_it_too(as_posix, monkeypatch,
                                                             tmp_path):
    """The GPU win does not reach a Mac, but the batching one does: 185.6s ->
    95.2s measured on CPU, plus the loanword-script fix. That is the whole macOS
    speed story for the faster-whisper backend, so it must not be gated on the
    device."""
    import sys as _sys
    import types

    class _Info:
        language = "hi"; duration = 6.0

    class _Seg:
        id = 0; start = 0.0; end = 6.0; text = "theek"; words = []

    rec: dict = {}

    class _Pipe:
        def __init__(self, model=None):
            rec["batched"] = True

        def transcribe(self, path, **kw):
            rec["kw"] = kw
            return iter([_Seg()]), _Info()

    mod = types.ModuleType("faster_whisper")
    mod.BatchedInferencePipeline = _Pipe
    monkeypatch.setitem(_sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(T, "_get_model", lambda model_size=None: object())
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: False)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)
    monkeypatch.delenv("VAI_WHISPER_BATCH_SIZE", raising=False)

    T.transcribe(tmp_path / "in.mp4", model_size="large-v3")
    assert rec.get("batched") is True
    assert rec["kw"]["batch_size"] == 8


def test_the_turbo_translate_guard_protects_the_whisper_cpp_path_too(monkeypatch,
                                                                    tmp_path):
    """macOS prefers the whisper.cpp backend, so a guard applied only to
    faster-whisper would miss the platform most likely to hit it. `model_for`
    runs BEFORE backend selection, which also means the ggml model looked up on
    disk is the substituted one rather than the turbo weights.
    """
    seen: dict = {}

    def fake_cpp(audio_path, language, model_size, task="transcribe"):
        seen["model"] = model_size
        seen["task"] = task
        return T.Transcript(language="hi", duration=1.0, segments=[])

    fake_ggml = tmp_path / "ggml-large-v3.bin"
    fake_ggml.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_ggml)
    monkeypatch.setattr(T, "_transcribe_via_whisper_cpp", fake_cpp)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)

    T.transcribe(tmp_path / "in.mp4", model_size="turbo", task="translate")
    assert seen["model"] == T.TRANSLATION_MODEL, (
        "turbo reached whisper.cpp for a translate it cannot perform")
    assert seen["task"] == "translate"


# --- the probe is a claim about configuration, and must stay cheap ---------

def test_the_cuda_probe_never_raises(monkeypatch):
    """It runs during device resolution on every model load. A probe that throws
    on a machine missing a dependency takes the whole feature down with it —
    the same rule ai/features.py states for its own probes."""
    def boom() -> list[str]:
        raise OSError("permission denied walking site-packages")

    monkeypatch.setattr(T, "_add_cuda_dll_dirs", boom)
    assert T._cuda_device_count() == 0
    assert T._resolve_device() == "cpu"


def test_device_resolution_does_not_probe_when_the_answer_is_explicit(monkeypatch):
    """`WHISPER_DEVICE=cpu` must not pay for a CUDA runtime load."""
    calls: list[int] = []
    monkeypatch.setattr(T, "_cuda_device_count", lambda: calls.append(1) or 0)
    monkeypatch.setattr(T, "WHISPER_DEVICE", "cpu")
    assert T._resolve_device() == "cpu"
    monkeypatch.setattr(T, "WHISPER_DEVICE", "cuda")
    assert T._resolve_device() == "cuda"
    assert calls == [], "probed despite an explicit device"
