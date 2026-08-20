"""Whisper backend auto-selection + whisper-cli invocation flags.

Locks in the two fine-tune fixes:
  1. backend="auto" (the new default) routes to whisper.cpp when the binary
     AND the ggml model exist, else falls back to faster-whisper.
  2. whisper-cli is ALWAYS invoked with an explicit `-l` — its built-in
     default is `en` (not auto-detect), which force-decoded Hindi uploads
     as English garbage until we passed `-l auto`.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from video_ai_editor.ingest import transcribe as T


def test_auto_routes_to_whisper_cpp_when_available(monkeypatch, tmp_path: Path):
    sentinel = T.Transcript(language="hi", duration=1.0, segments=[])
    calls = {}

    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    fake_model = tmp_path / "ggml-tiny.bin"; fake_model.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_model)
    def fake_cpp(audio_path, language, model_size, task="transcribe"):
        calls["used"] = "whisper_cpp"; calls["task"] = task; return sentinel
    monkeypatch.setattr(T, "_transcribe_via_whisper_cpp", fake_cpp)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)

    out = T.transcribe(tmp_path / "in.wav", model_size="tiny")
    assert out is sentinel
    assert calls["used"] == "whisper_cpp"


def test_auto_falls_back_when_model_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    monkeypatch.setattr(T, "_whisper_cpp_model_path",
                        lambda name: tmp_path / "missing.bin")
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)

    # faster-whisper path will be taken; stub the model loader so no real
    # model loads (we only care about routing). Batching off so the stub's
    # `transcribe` is reached directly rather than through the fallback ladder.
    monkeypatch.setenv("VAI_WHISPER_BATCH_SIZE", "0")
    class FakeInfo:
        language = "en"; duration = 0.0
    class FakeModel:
        def transcribe(self, *a, **kw): return iter(()), FakeInfo()
    monkeypatch.setattr(T, "_get_model", lambda size=None: FakeModel())

    out = T.transcribe(tmp_path / "in.wav", model_size="tiny")
    assert out.language == "en"  # came through the faster-whisper branch


def test_env_override_still_wins(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WHISPER_BACKEND", "faster_whisper")
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: True)
    fake_model = tmp_path / "ggml-tiny.bin"; fake_model.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_model)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("cpp used despite env"))
    monkeypatch.setattr(T, "_transcribe_via_whisper_cpp", boom)
    monkeypatch.setenv("VAI_WHISPER_BATCH_SIZE", "0")
    class FakeInfo:
        language = "en"; duration = 0.0
    class FakeModel:
        def transcribe(self, *a, **kw): return iter(()), FakeInfo()
    monkeypatch.setattr(T, "_get_model", lambda size=None: FakeModel())

    out = T.transcribe(tmp_path / "in.wav")
    assert out.language == "en"


def test_whisper_cli_gets_explicit_language_flag(monkeypatch, tmp_path: Path):
    """The cmd must contain `-l auto` when no language is given, and `-l hi`
    when one is. Captured by stubbing subprocess.run inside the module."""
    fake_model = tmp_path / "ggml-tiny.bin"; fake_model.write_bytes(b"x")
    monkeypatch.setattr(T, "_whisper_cpp_model_path", lambda name: fake_model)
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append([str(c) for c in cmd])
        class P:
            returncode = 0; stderr = ""; stdout = ""
        # First call is ffmpeg wav extract; second is whisper-cli. For the
        # whisper-cli call, drop a minimal JSON next to the -of prefix.
        if "whisper-cli" in str(cmd[0]) or str(cmd[0]).endswith("whisper-cli"):
            of = cmd[cmd.index("-of") + 1]
            Path(f"{of}.json").write_text('{"transcription": [], "result": {"language": "hi"}}')
        return P()

    monkeypatch.setattr(T.subprocess, "run", fake_run)

    T._transcribe_via_whisper_cpp(tmp_path / "in.mp4", language=None, model_size="tiny")
    cli = captured[-1]
    assert "-l" in cli and cli[cli.index("-l") + 1] == "auto"

    T._transcribe_via_whisper_cpp(tmp_path / "in.mp4", language="hi", model_size="tiny")
    cli = captured[-1]
    assert cli[cli.index("-l") + 1] == "hi"


# ---------------------------------------------------------------------------
# Packaged-build failure modes.
#
# Both of these reproduce ONLY in a PyInstaller bundle, never in dev — which is
# exactly why they shipped. The Windows app imported faster_whisper fine and
# then died inside `model.transcribe(..., vad_filter=True)` on a missing
# `faster_whisper/assets/silero_vad_v6.onnx` (PyInstaller collects modules, not
# a package's data files). onnxruntime's NoSuchFile is neither ValueError nor
# RuntimeError, so main.py's dispatch mapping passed it straight through as a
# bare HTTP 500 — the user saw "internal server error" and no captions, with
# the identical click working in the browser.
# ---------------------------------------------------------------------------

def test_missing_vad_asset_degrades_instead_of_raising(monkeypatch, tmp_path: Path):
    """VAD only trims silence — a missing asset must cost quality, not the
    whole feature (and must never surface as an unmapped 500)."""
    attempts: list[bool] = []

    class _Info:
        language = "en"; duration = 1.0

    class _FakeModel:
        def transcribe(self, path, *, language=None, task="transcribe",
                       word_timestamps=True, vad_filter=True):
            attempts.append(vad_filter)
            if vad_filter:
                # The real shape: onnxruntime raises its own exception type,
                # deliberately NOT a ValueError/RuntimeError subclass.
                raise Exception(
                    "[ONNXRuntimeError] : 3 : NO_SUCHFILE : Load model from "
                    r"...\_internal\faster_whisper\assets\silero_vad_v6.onnx failed"
                )
            return iter([]), _Info()

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _FakeModel())
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: False)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)
    # Batching off: this test is about the VAD rung of the ladder specifically,
    # and the batched rung needs VAD too (covered separately below).
    monkeypatch.setenv("VAI_WHISPER_BATCH_SIZE", "0")

    out = T.transcribe(tmp_path / "in.mp4", model_size="tiny")
    assert attempts == [True, False], "should retry exactly once, without VAD"
    assert out.language == "en"


# ---------------------------------------------------------------------------
# Batched decoding.
#
# Measured on 60s of real Hindi speech (large-v3 int8, 16-core Windows):
# sequential 185.6s vs batched(8) 95.2s — 1.95x, and the batched text kept the
# speaker's English loanwords in Latin ("अपनी last meeting", "STD", "PTSD")
# where sequential transliterated them into Devanagari. So this is a
# correctness-adjacent change, not only a speed one, and the fallback ladder
# must not silently swallow it.
#
# A FAKE faster_whisper module is injected rather than monkeypatching the real
# one, so these run identically whether or not the package is installed — which
# also lets the "old faster-whisper has no BatchedInferencePipeline" case be
# expressed exactly: a module without the attribute.
# ---------------------------------------------------------------------------

class _Info:
    language = "hi"; duration = 12.0


class _Seg:
    def __init__(self, i: int, start: float, end: float, text: str):
        self.id, self.start, self.end, self.text = i, start, end, text
        self.words = []


def _install_fake_fw(monkeypatch, pipeline_cls):
    """Put a stand-in `faster_whisper` module in sys.modules.

    `_open_decode` imports BatchedInferencePipeline at CALL time, so replacing
    the module is enough. Pass None to simulate faster-whisper < 1.1, where the
    class does not exist at all.
    """
    import sys, types
    mod = types.ModuleType("faster_whisper")
    if pipeline_cls is not None:
        mod.BatchedInferencePipeline = pipeline_cls
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)


def _fake_pipeline(record: dict, *, segments=None, boom: Exception | None = None):
    class _FakePipe:
        def __init__(self, model=None):
            record["constructed"] = True

        def transcribe(self, path, **kw):
            record["kw"] = kw
            if boom is not None:
                raise boom
            segs = segments if segments is not None else [
                _Seg(0, 0.0, 6.0, "pehla"), _Seg(1, 6.0, 12.0, "doosra"),
            ]
            return iter(segs), _Info()
    return _FakePipe


@pytest.fixture
def fw_only(monkeypatch):
    """Route to faster-whisper with no real model load."""
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: False)
    monkeypatch.delenv("WHISPER_BACKEND", raising=False)
    monkeypatch.delenv("VAI_WHISPER_BATCH_SIZE", raising=False)
    monkeypatch.setattr(T, "_get_model", lambda model_size=None: object())


def test_batching_is_the_default_and_asks_for_word_timestamps(fw_only, monkeypatch,
                                                              tmp_path: Path):
    rec: dict = {}
    _install_fake_fw(monkeypatch, _fake_pipeline(rec))

    out = T.transcribe(tmp_path / "in.mp4", model_size="tiny")

    assert rec.get("constructed"), "batched pipeline was not used by default"
    assert rec["kw"]["batch_size"] == 8
    # Cue building is word-timed (caption_format.build_cues), so losing this
    # would degrade every caption to segment-level timing.
    assert rec["kw"]["word_timestamps"] is True
    # Batching IS VAD chunking — there is no batched mode without it.
    assert rec["kw"]["vad_filter"] is True
    assert [s.text for s in out.segments] == ["pehla", "doosra"]
    assert out.language == "hi"


def test_the_task_reaches_the_batched_pipeline(fw_only, monkeypatch, tmp_path: Path):
    """The English caption target rides on task="translate". Dropping it here
    would silently return the spoken language instead."""
    rec: dict = {}
    _install_fake_fw(monkeypatch, _fake_pipeline(rec))
    T.transcribe(tmp_path / "in.mp4", model_size="tiny", task="translate")
    assert rec["kw"]["task"] == "translate"


def test_old_faster_whisper_without_the_pipeline_falls_back_to_sequential(
        fw_only, monkeypatch, tmp_path: Path):
    _install_fake_fw(monkeypatch, None)      # no BatchedInferencePipeline attr
    seen: list[dict] = []

    class _Model:
        def transcribe(self, path, **kw):
            seen.append(kw)
            return iter([_Seg(0, 0.0, 3.0, "theek")]), _Info()

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _Model())
    out = T.transcribe(tmp_path / "in.mp4", model_size="tiny")

    assert len(seen) == 1 and seen[0]["vad_filter"] is True
    assert [s.text for s in out.segments] == ["theek"]


def test_a_batched_failure_degrades_through_sequential_to_no_vad(
        fw_only, monkeypatch, tmp_path: Path):
    """The packaged-app case: the Silero VAD asset is missing, so BOTH the
    batched rung and the sequential-with-VAD rung fail. Captions must still
    come out — VAD only trims silence."""
    rec: dict = {}
    onnx = Exception("[ONNXRuntimeError] : 3 : NO_SUCHFILE : silero_vad_v6.onnx")
    _install_fake_fw(monkeypatch, _fake_pipeline(rec, boom=onnx))
    vad_attempts: list[bool] = []

    class _Model:
        def transcribe(self, path, **kw):
            vad_attempts.append(kw["vad_filter"])
            if kw["vad_filter"]:
                raise onnx
            return iter([_Seg(0, 0.0, 3.0, "chala")]), _Info()

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _Model())
    out = T.transcribe(tmp_path / "in.mp4", model_size="tiny")

    assert rec.get("constructed"), "batched rung was skipped"
    assert vad_attempts == [True, False]
    assert [s.text for s in out.segments] == ["chala"]


def test_the_last_rung_propagates_its_error(fw_only, monkeypatch, tmp_path: Path):
    """A ladder, not a blanket swallow: if nothing works the caller must see
    why rather than get an empty transcript."""
    _install_fake_fw(monkeypatch, _fake_pipeline({}, boom=Exception("batched no")))

    class _Model:
        def transcribe(self, path, **kw):
            raise Exception("decode really failed")

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _Model())
    with pytest.raises(Exception, match="decode really failed"):
        T.transcribe(tmp_path / "in.mp4", model_size="tiny")


def test_cancel_is_never_retried_in_a_slower_mode(fw_only, monkeypatch,
                                                  tmp_path: Path):
    """Pressing Cancel must stop. Treating it as a failed rung would ignore the
    user AND spend minutes doing so, since each fallback is slower."""
    segs = [_Seg(0, 0.0, 6.0, "ek")]
    _install_fake_fw(monkeypatch, _fake_pipeline({}, segments=segs))
    sequential_used: list[str] = []

    class _Model:
        def transcribe(self, path, **kw):
            sequential_used.append("yes")
            return iter([]), _Info()

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _Model())

    with pytest.raises(T.TranscriptionCancelled):
        T.transcribe(tmp_path / "in.mp4", model_size="tiny",
                     should_cancel=lambda: True)
    assert sequential_used == [], "cancel was retried in another mode"


def test_progress_is_reported_from_batched_segments(fw_only, monkeypatch,
                                                    tmp_path: Path):
    """Batching emits far fewer, longer segments (2 for 60s vs 18), so the bar
    is coarse — it must still be monotonic and clamped.

    It does NOT always reach 1.0: measured on real audio, VAD trimmed 3.7s of
    silence so the last segment ended at 57.2s of a 60.0s duration and the final
    call was 0.953. That is fine because auto_caption reserves the last tenth of
    the bar for post-processing (DECODE_SHARE) and the job's own completion
    supplies 100% — so assert what is guaranteed, not what a tidy fixture
    happens to produce."""
    _install_fake_fw(monkeypatch, _fake_pipeline({}))
    seen: list[float] = []
    T.transcribe(tmp_path / "in.mp4", model_size="tiny",
                 on_progress=lambda frac, done, total: seen.append(frac))
    assert seen, "no progress reported at all"
    assert seen == sorted(seen), "progress must not go backwards"
    assert all(0.0 <= f <= 1.0 for f in seen), f"fraction out of range: {seen}"
    # These fixture segments DO span the full duration, so this one reaches 1.0.
    assert seen[-1] == pytest.approx(1.0)


@pytest.mark.parametrize("raw,expected", [
    ("0", 0),            # explicit opt-out → sequential
    ("16", 16),
    ("  8  ", 8),
    ("8 # tuned for this box", 8),   # .env values are read literally
    ("-4", 0),
    ("banana", 8),       # unparseable → the shipped default, with a warning
    ("", 8),
])
def test_batch_size_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("VAI_WHISPER_BATCH_SIZE", raw)
    assert T._batch_size() == expected


def test_batch_size_zero_skips_the_batched_rung_entirely(fw_only, monkeypatch,
                                                         tmp_path: Path):
    monkeypatch.setenv("VAI_WHISPER_BATCH_SIZE", "0")
    rec: dict = {}
    _install_fake_fw(monkeypatch, _fake_pipeline(rec))

    class _Model:
        def transcribe(self, path, **kw):
            return iter([_Seg(0, 0.0, 3.0, "seq")]), _Info()

    monkeypatch.setattr(T, "_get_model", lambda model_size=None: _Model())
    out = T.transcribe(tmp_path / "in.mp4", model_size="tiny")
    assert "constructed" not in rec
    assert [s.text for s in out.segments] == ["seq"]


# ---------------------------------------------------------------------------
# Device resolution.
#
# `.env.example` ships WHISPER_DEVICE=auto and lists `cuda` beside it, but the
# old rule was `device if device in ("cpu","cuda") else "cpu"` — so `auto`, the
# value everybody has configured, could never select the GPU in the machine.
# On an RTX 4050 box that meant every caption run was CPU int8 with the card
# idle, which is a large part of why captions "took a lot of time".
#
# The other half is that a GPU which LOADS a model is not a GPU that can RUN
# one: measured, `WhisperModel(device="cuda")` constructed fine and then raised
# `Library cublas64_12.dll is not found or cannot be loaded` from inside
# `encode` — the first forward pass, minutes into a job. Hence the probe.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cuda_probe_cache():
    """`_cuda_device_count` is lru_cached, so a test that fakes it must not
    leak that answer into the next test (or into the rest of the process)."""
    T._cuda_device_count.cache_clear()
    yield
    T._cuda_device_count.cache_clear()


@pytest.mark.parametrize("env,cuda_count,expected", [
    ("auto", 1, "cuda"),          # the whole point: auto must find the GPU
    ("auto", 0, "cpu"),
    ("auto  # auto | cpu | cuda | mps", 1, "cuda"),   # real .env line
    ("cpu", 1, "cpu"),            # an explicit choice always wins
    ("cuda", 0, "cuda"),          # ...even a wrong one; the ladder handles it
    ("mps", 1, "cuda"),           # ctranslate2 has no Metal backend
    ("mps", 0, "cpu"),            # ...which on a Mac means cpu, as before
    ("", 0, "cpu"),
    ("nonsense", 1, "cuda"),
])
def test_device_resolution(monkeypatch, env, cuda_count, expected):
    monkeypatch.setattr(T, "WHISPER_DEVICE", env)
    monkeypatch.setattr(T, "_cuda_device_count", lambda: cuda_count)
    assert T._resolve_device() == expected


def test_compute_type_defaults_per_device_and_is_overridable(monkeypatch):
    monkeypatch.delenv("WHISPER_COMPUTE_TYPE", raising=False)
    assert T._resolve_compute_type("cpu") == "int8"
    # float16 on GPU: int8 is not the faster choice on tensor cores.
    assert T._resolve_compute_type("cuda") == "float16"
    monkeypatch.setenv("WHISPER_COMPUTE_TYPE", "bfloat16  # try it")
    assert T._resolve_compute_type("cuda") == "bfloat16"


def test_a_gpu_that_loads_but_cannot_execute_falls_back_to_cpu(monkeypatch):
    """The measured failure: construction succeeds, the first forward pass
    raises a missing-library error. A construction-only check cannot see it, so
    captions would have died minutes into a job."""
    import sys, types
    monkeypatch.setattr(T, "_models", {})
    monkeypatch.setattr(T, "WHISPER_DEVICE", "auto")
    monkeypatch.setattr(T, "_cuda_device_count", lambda: 1)

    built: list[tuple[str, str]] = []

    class _Model:
        def __init__(self, name, device="cpu", compute_type="int8"):
            built.append((device, compute_type))
            self.device = device

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _Model
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)

    def _probe(model):
        if model.device != "cpu":
            raise RuntimeError(
                "Library cublas64_12.dll is not found or cannot be loaded")

    monkeypatch.setattr(T, "_probe_forward_pass", _probe)

    out = T._get_model("large-v3")
    assert out.device == "cpu", "should have degraded to the path that works"
    # float16 first, then the half-size weights, then cpu — never skipping a rung.
    assert built == [("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")]


def test_the_cpu_path_is_not_probed(monkeypatch):
    """The probe exists for accelerators. CPU int8 is what has always worked, so
    paying an extra forward pass for it on every model load is waste."""
    import sys, types
    monkeypatch.setattr(T, "_models", {})
    monkeypatch.setattr(T, "WHISPER_DEVICE", "cpu")
    probed: list[object] = []

    class _Model:
        def __init__(self, name, device="cpu", compute_type="int8"):
            self.device = device

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _Model
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(T, "_probe_forward_pass", lambda m: probed.append(m))

    T._get_model("small")
    assert probed == []


def test_an_explicit_cuda_choice_also_degrades_and_says_why(monkeypatch, caplog):
    """Even an explicit WHISPER_DEVICE=cuda degrades, because this is the house
    stance ("every AI feature degrades rather than crashes when a dep/model is
    missing") and because a broken GPU would otherwise surface as a 422 on the
    Captions button minutes in. The compensation is that the log has to NAME the
    missing library — a silent downgrade to a 12x slower device is its own bug.
    """
    import logging, sys, types
    monkeypatch.setattr(T, "_models", {})
    monkeypatch.setattr(T, "WHISPER_DEVICE", "cuda")

    class _Model:
        def __init__(self, name, device="cpu", compute_type="int8"):
            self.device = device

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _Model
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(T, "_probe_forward_pass", lambda m: (_ for _ in ()).throw(
        RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")))

    # `caplog` collects on the ROOT logger, and `api.hardening.install()` sets
    # `video_ai_editor`.propagate = False without ever restoring it — so any
    # earlier test that built the FastAPI app makes this invisible. Forcing
    # propagation back on for this test is what makes it order-independent;
    # without it the test passes alone and fails in the full suite.
    app_log = logging.getLogger("video_ai_editor")
    monkeypatch.setattr(app_log, "propagate", True)
    with caplog.at_level(logging.WARNING, logger="video_ai_editor"):
        out = T._get_model("large-v3")
    assert out.device == "cpu"
    assert "cublas64_12.dll" in caplog.text, "the downgrade must be diagnosable"


# ---------------------------------------------------------------------------
# Model capabilities: turbo cannot translate.
#
# large-v3-turbo is ~4x faster (24.8s vs 99.1s for 60s, measured on the same
# batched path) but was fine-tuned on transcription only. Asked to translate 25s
# of Hindi it returned five tokens of ellipses — "... ... ... ... ..." — where
# large-v3 returned a correct 58-word English sentence. It does not raise, so
# nothing downstream can tell that apart from "the speaker said nothing".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,can", [
    ("large-v3", True),
    ("small", True),
    ("medium", True),
    ("large-v3-turbo", False),
    ("turbo", False),
    ("TURBO", False),                      # names arrive from .env and args
    ("deepdml/faster-whisper-large-v3-turbo-ct2", False),
    (None, True),
])
def test_translation_capability(name, can):
    assert T.translates(name) is can


def test_model_for_substitutes_only_for_translate():
    assert T.model_for("turbo", "transcribe") == "turbo"
    assert T.model_for("turbo", "translate") == T.TRANSLATION_MODEL
    assert T.model_for("large-v3", "translate") == "large-v3"
    # Idempotent, so the handler resolving it first and transcribe() resolving
    # it again cannot disagree.
    once = T.model_for("turbo", "translate")
    assert T.model_for(once, "translate") == once


def test_turbo_carries_its_anti_repetition_settings(fw_only, monkeypatch,
                                                    tmp_path: Path):
    """Turbo without these collapses into a repetition loop — 16 of 128 words,
    "एक सब्सक्राइब" five times. With them it recovers 143. They must travel with
    the model, not be a knob somebody has to know to set."""
    rec: dict = {}
    _install_fake_fw(monkeypatch, _fake_pipeline(rec))
    T.transcribe(tmp_path / "in.mp4", model_size="large-v3-turbo")
    kw = rec["kw"]
    assert kw["no_repeat_ngram_size"] == 3
    assert kw["condition_on_previous_text"] is False
    assert kw["repetition_penalty"] > 1.0


def test_a_normal_model_gets_no_decode_overrides(fw_only, monkeypatch,
                                                 tmp_path: Path):
    """large-v3 needs none of it, and beam/penalty changes measurably cost time
    (beam 8 was 24% slower for byte-identical output once batched)."""
    rec: dict = {}
    _install_fake_fw(monkeypatch, _fake_pipeline(rec))
    T.transcribe(tmp_path / "in.mp4", model_size="large-v3")
    assert "no_repeat_ngram_size" not in rec["kw"]
    assert "repetition_penalty" not in rec["kw"]


def test_decode_overrides_are_keyed_on_turbo_not_on_translation_capability():
    """These are independent claims about a model. Routing one through the
    other's predicate would make the next model's behaviour a coincidence."""
    assert T.decode_overrides("turbo")
    assert T.decode_overrides("large-v3-turbo")
    assert T.decode_overrides("large-v3") == {}
    assert T.decode_overrides(None) == {}
    # A returned dict must not be the shared module constant, or a caller
    # mutating it would silently reconfigure every later decode.
    a, b = T.decode_overrides("turbo"), T.decode_overrides("turbo")
    a["no_repeat_ngram_size"] = 99
    assert b["no_repeat_ngram_size"] == 3


def test_transcribe_refuses_to_translate_on_a_transcribe_only_model(
        fw_only, monkeypatch, tmp_path: Path):
    """The enforcement point: Claude and MCP can pass `model` directly."""
    loaded: list[str | None] = []
    monkeypatch.setattr(T, "_get_model",
                        lambda model_size=None: loaded.append(model_size) or object())
    _install_fake_fw(monkeypatch, _fake_pipeline({}))

    T.transcribe(tmp_path / "in.mp4", model_size="turbo", task="translate")
    assert loaded == [T.TRANSLATION_MODEL]

    loaded.clear()
    T.transcribe(tmp_path / "in.mp4", model_size="turbo", task="transcribe")
    assert loaded == ["turbo"], "transcription is exactly what turbo is for"


def test_missing_faster_whisper_raises_runtime_error(monkeypatch):
    """A bare ImportError escapes main.py's dispatch mapping as a 500; a
    RuntimeError becomes a 422 carrying an actionable message. The macOS .app
    excludes faster-whisper by design, so it reaches this line for real."""
    import sys
    monkeypatch.setattr(T, "_models", {})
    # Setting a sys.modules entry to None makes `import` raise ImportError.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(RuntimeError) as ei:
        T._get_model("tiny")
    msg = str(ei.value).lower()
    assert "faster-whisper" in msg and "not installed" in msg


def test_spec_bundles_faster_whisper_data_files():
    """The Windows package is built from this .spec (build_win.ps1). Losing
    this line silently reintroduces the 500 above, and no dev path would
    notice — so assert it rather than trusting review."""
    spec = Path(__file__).resolve().parents[1] / "Video AI Editor.spec"
    text = spec.read_text(encoding="utf-8")
    assert "collect_data_files('faster_whisper')" in text


def test_missing_faster_whisper_is_a_422_over_http_not_a_500(tmp_path, monkeypatch):
    """The macOS .app EXCLUDES faster-whisper by design (build_app.sh, to stay
    ~150MB), so every Mac user of the packaged app reaches that import. Unit
    tests prove the handler raises; only HTTP proves the client sees 422 with
    a readable sentence rather than a bare 500 "internal server error" — which
    is exactly what the Windows build did before this fix.
    """
    import subprocess
    import sys
    from fastapi.testclient import TestClient
    from video_ai_editor import main as m
    from video_ai_editor.storage import new_session_id, session_dir
    from video_ai_editor.edl import EDLStore
    from video_ai_editor.edl.schema import Clip

    src = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=160x90:rate=30:duration=1",
         "-f", "lavfi", "-i", "sine=f=440:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)], check=True, capture_output=True)

    sid = new_session_id()
    sd = session_dir(sid)
    for sub in ("uploads", "previews", "exports", "cache", "snapshots"):
        (sd / sub).mkdir(parents=True, exist_ok=True)
    store = EDLStore(sd)
    store.edl.get_track("v1").clips.append(Clip(src=str(src), in_=0.0, out=1.0, start=0.0))
    store.commit("test", {}, "seed")

    monkeypatch.setattr(T, "_models", {})
    monkeypatch.setattr(T, "_whisper_cpp_available", lambda: False)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    client = TestClient(m.app)
    r = client.post(f"/api/sessions/{sid}/dispatch",
                    json={"tool": "auto_caption", "args": {}})
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:400]}"
    assert "faster-whisper" in r.text
