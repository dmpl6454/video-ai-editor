"""Speaker diarization via pyannote.audio (gated HuggingFace model).

Reads HUGGINGFACE_TOKEN (or HF_TOKEN) from the environment, lazy-imports
pyannote on first call. Returns a list of (speaker_label, start_s, end_s)
turns + a derived map of word_idx → speaker for the project transcript.

Pipeline preference order:
  1. `pyannote/speaker-diarization-community-1` — pyannote 4.x's open-community
     model. Higher quality than 3.1 on most modern audio. Still EULA-gated
     (one free click + a token), so a token is required.
  2. `pyannote/speaker-diarization-3.1` — legacy 3.x pipeline, still excellent.
  3. heuristic_diarize — librosa MFCC + KMeans. Works without a token, but
     materially worse quality than either pyannote pipeline.
"""
from __future__ import annotations
import os
import json
import subprocess
from pathlib import Path

from .. import platformutil as _pu

PYANNOTE_PIPELINES = (
    "pyannote/speaker-diarization-community-1",
    "pyannote/speaker-diarization-3.1",
)


def _hf_token() -> str | None:
    return (os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_HUB_TOKEN"))


def _hf_token_setup_message() -> str:
    return (
        "Pyannote diarization needs a HuggingFace token + EULA acceptance "
        "(one click each, free).\n"
        "  1. Open https://hf.co/pyannote/speaker-diarization-community-1 and click "
        "'Agree and access repository'.\n"
        "  2. Open https://hf.co/pyannote/segmentation-3.0 and accept too "
        "(used by both pipelines).\n"
        "  3. Create a token at https://hf.co/settings/tokens (Read scope is enough).\n"
        "  4. Set it: `export HUGGINGFACE_TOKEN=hf_...` or add it to "
        "`~/video-ai-editor/.env`.\n"
        "  5. Retry the diarize call.\n"
        "Or pass `fallback=true` to use the librosa-based heuristic without a token."
    )


def pyannote_status() -> dict:
    """Tell the caller exactly what's missing so they can fix it without trial-
    and-error. Pure-local checks: no network, no model load."""
    from pathlib import Path as _P
    cache_root = _P.home() / ".cache" / "huggingface" / "hub"
    cached = []
    if cache_root.exists():
        for p in cache_root.glob("models--pyannote*"):
            cached.append(p.name.replace("models--", "").replace("--", "/"))
    try:
        import pyannote.audio  # type: ignore
        ver = pyannote.audio.__version__
        installed = True
    except ImportError:
        ver = None
        installed = False
    return {
        "pyannote_installed": installed,
        "pyannote_version": ver,
        "hf_token_present": bool(_hf_token()),
        "models_cached": cached,
        "preferred_pipelines": list(PYANNOTE_PIPELINES),
        "setup_help": _hf_token_setup_message(),
    }


def _audio_extract(src: Path, dst: Path) -> Path:
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [_pu.FFMPEG, "-y", "-i", str(src), "-vn",
         "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(dst)],
        capture_output=True,
        **_pu.SUBPROCESS_FLAGS,
    )
    if proc.returncode != 0:
        # Common cause: source has no audio track at all.
        err = proc.stderr.decode(errors="replace")
        if "Output file does not contain any stream" in err or \
           "does not contain any stream" in err:
            raise RuntimeError(
                f"Diarization needs an audio track on {src.name} — none found.")
        raise RuntimeError(f"audio extract failed: {err[-500:]}")
    return dst


def diarize(src: Path, cache_dir: Path, *, fallback: bool = True,
            num_speakers: int = 2) -> list[dict]:
    """Run pyannote on the source's audio. Returns a list of speaker turns:
    [{"speaker": "SPEAKER_00", "start": 0.0, "end": 4.2}, ...].

    If `fallback=True` and there's no HF token, drops down to a librosa-based
    heuristic (silence-segment + MFCC clustering) — much weaker than pyannote
    but works out of the box for 2-speaker interview audio.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"diarize_{src.stem}.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            # Handle both legacy (list) and current (dict-with-turns) shapes.
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "turns" in data:
                return data["turns"]
        except Exception:
            pass

    token = _hf_token()
    pipeline_used: str | None = None
    last_err: Exception | None = None

    if token:
        try:
            from pyannote.audio import Pipeline  # type: ignore
        except ImportError as e:
            if not fallback:
                raise RuntimeError(
                    "pyannote.audio not installed. `uv add pyannote.audio` and retry."
                ) from e
            return _heuristic_diarize(src, cache_dir, num_speakers=num_speakers)

        audio_wav = cache_dir / f"audio16k_{src.stem}.wav"
        _audio_extract(src, audio_wav)

        # Force CPU on Mac (CUDA unavailable, MPS still flaky for some pyannote ops).
        # Users with a CUDA GPU set TORCH_DEVICE=cuda explicitly.
        import torch
        device_name = os.environ.get("TORCH_DEVICE", "cpu")
        device = torch.device(device_name)

        for name in PYANNOTE_PIPELINES:
            try:
                pipeline = Pipeline.from_pretrained(name, token=token)
                if pipeline is None:
                    raise RuntimeError(f"Pipeline.from_pretrained({name}) returned None")
                pipeline.to(device)
                diarization = pipeline(str(audio_wav))
                pipeline_used = name
                break
            except Exception as e:
                last_err = e
                continue

        if pipeline_used is None:
            if not fallback:
                raise RuntimeError(
                    f"All pyannote pipelines failed to load. Last error: {last_err}\n\n"
                    + _hf_token_setup_message()
                )
            turns = _heuristic_diarize(src, cache_dir, num_speakers=num_speakers)
            cache_path.write_text(json.dumps(
                {"_warning": f"pyannote failed: {last_err}",
                 "_pipeline": "heuristic",
                 "turns": turns}, indent=2), encoding="utf-8")
            return turns

        turns: list[dict] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({"speaker": str(speaker),
                          "start": float(turn.start),
                          "end": float(turn.end)})
        cache_path.write_text(json.dumps(
            {"_pipeline": pipeline_used, "turns": turns}, indent=2), encoding="utf-8")
        return turns

    if not fallback:
        raise RuntimeError(_hf_token_setup_message())
    turns = _heuristic_diarize(src, cache_dir, num_speakers=num_speakers)
    cache_path.write_text(json.dumps(
        {"_pipeline": "heuristic", "turns": turns}, indent=2), encoding="utf-8")
    return turns


def _heuristic_diarize(src: Path, cache_dir: Path, *,
                       num_speakers: int = 2) -> list[dict]:
    """Heuristic 2-speaker diarization without external models.

    Pipeline:
      1. Extract 16 kHz mono wav.
      2. silencedetect via ffmpeg → utterance boundaries.
      3. For each utterance, compute mean MFCC (13 coeffs) via librosa.
      4. KMeans (numpy, no sklearn dep) into `num_speakers` clusters.

    Limitations: assumes speakers don't overlap; same speaker speaking after
    a long pause may flip clusters. Fine for a "draft" first pass the user
    can fix via `name_speakers` / manual edits.
    """
    audio_wav = cache_dir / f"audio16k_{src.stem}.wav"
    _audio_extract(src, audio_wav)

    # 1) Silence detect
    proc = subprocess.run(
        [_pu.FFMPEG, "-i", str(audio_wav), "-af",
         "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_pu.SUBPROCESS_FLAGS,
    )
    starts: list[float] = [0.0]
    ends: list[float] = []
    for line in proc.stderr.splitlines():
        if "silence_start" in line:
            try:
                ends.append(float(line.rsplit("silence_start:", 1)[1].split()[0]))
            except Exception:
                pass
        elif "silence_end" in line:
            try:
                starts.append(float(line.rsplit("silence_end:", 1)[1].split("|")[0].strip()))
            except Exception:
                pass

    # 2) Probe duration
    try:
        dur = float(subprocess.run(
            [_pu.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(audio_wav)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
            **_pu.SUBPROCESS_FLAGS,
        ).stdout.strip())
    except Exception:
        dur = 0.0
    ends.append(dur)
    n = min(len(starts), len(ends))
    utts = [(starts[i], ends[i]) for i in range(n) if ends[i] - starts[i] > 0.4]
    if not utts:
        return []

    # 3) MFCC features per utterance — librosa where it exists (the historical
    #    path, byte-identical), else the numpy port below (the packaged .app).
    import numpy as np
    try:
        import librosa  # type: ignore
    except ImportError:
        librosa = None
    if librosa is not None:
        y, sr = librosa.load(str(audio_wav), sr=16000, mono=True)
        mfcc_of = lambda seg: librosa.feature.mfcc(y=seg, sr=sr, n_mfcc=13)  # noqa: E731
    else:
        y, sr = _read_wav16k(audio_wav)
        mfcc_of = lambda seg: mfcc13_numpy(seg.astype(np.float64), sr, librosa_compatible=True)  # noqa: E731
    feats = []
    for s, e in utts:
        i0, i1 = int(s * sr), int(min(len(y), e * sr))
        if i1 - i0 < 800:
            feats.append(np.zeros(13, dtype=np.float32))
            continue
        feats.append(mfcc_of(y[i0:i1]).mean(axis=1))
    X = np.stack(feats)

    # 4) KMeans (numpy, k-means++ init, 25 iterations)
    labels = _kmeans_numpy(X, k=max(1, int(num_speakers)), iters=25, seed=42)
    return [
        {"speaker": f"SPEAKER_{int(labels[i]):02d}",
         "start": utts[i][0], "end": utts[i][1]}
        for i in range(len(utts))
    ]


def _read_wav16k(path: Path):
    """(float32 mono samples, rate) of the 16 kHz wav `_audio_extract` wrote."""
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if wf.getnchannels() > 1:
            data = data.reshape(-1, wf.getnchannels()).mean(axis=1)
    return data, sr


# --- numpy MFCC (no librosa) -------------------------------------------------

def _hz_to_mel(f):
    """Slaney's scale (librosa's default, `htk=False`)."""
    import numpy as np
    f = np.asarray(f, dtype=np.float64)
    mel = f / (200.0 / 3)
    min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-12) / min_log_hz) / logstep, mel)


def _mel_to_hz(m):
    import numpy as np
    m = np.asarray(m, dtype=np.float64)
    f = 200.0 / 3 * m
    min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f)


def mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float = 0.0, fmax: float | None = None):
    """librosa.filters.mel(…, htk=False, norm="slaney") in numpy."""
    import numpy as np
    fmax = float(sr) / 2 if fmax is None else fmax
    fftfreqs = np.linspace(0, float(sr) / 2, 1 + n_fft // 2)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    weights = np.zeros((n_mels, len(fftfreqs)))
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2: n_mels + 2] - mel_f[:n_mels])
    return weights * enorm[:, None]


def _dct2_ortho(x):
    """DCT-II with orthonormal scaling along axis 0 (scipy.fftpack.dct type 2, norm="ortho")."""
    import numpy as np
    n = x.shape[0]
    k = np.arange(n)[:, None]
    m = np.arange(n)[None, :]
    basis = np.cos(np.pi * k * (2 * m + 1) / (2 * n))
    scale = np.full((n, 1), np.sqrt(2.0 / n))
    scale[0, 0] = np.sqrt(1.0 / n)
    return scale * (basis @ x)


def mfcc13_numpy(y, sr: int, *, n_mfcc: int = 13, n_fft: int = 2048, hop: int = 512, n_mels: int = 128,
                 preemph: float = 0.0, top_db: float | None = 80.0, librosa_compatible: bool = False):
    """13 MFCC (n_mfcc × frames) in numpy: optional pre-emphasis, a centred
    periodic-Hann STFT (zero padding, as librosa ≥ 0.10), a power mel
    spectrogram on Slaney's filterbank, `power_to_db(ref=1, amin=1e-10,
    top_db)`, DCT-II ortho. `librosa_compatible=True` pins librosa's defaults
    (n_fft 2048, hop 512, 128 mels, no pre-emphasis), which
    tests/test_brain_speakers.py holds within 1e-3 of `librosa.feature.mfcc`;
    the brain's speaker features use 25 ms / 10 ms / 26 mel / 0.97."""
    import numpy as np
    y = np.asarray(y, dtype=np.float64)
    if librosa_compatible:
        n_fft, hop, n_mels, preemph = 2048, 512, 128, 0.0
    if preemph:
        y = np.append(y[0], y[1:] - preemph * y[:-1])
    pad = n_fft // 2
    y = np.pad(y, (pad, pad), mode="constant")
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    n_frames = 1 + (len(y) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)
    spec = np.abs(np.fft.rfft(y[idx] * window, axis=1)) ** 2          # (frames, bins)
    mel = mel_filterbank(sr, n_fft, n_mels) @ spec.T                    # (mels, frames)
    log_spec = 10.0 * np.log10(np.maximum(1e-10, mel))
    if top_db is not None:
        log_spec = np.maximum(log_spec, log_spec.max() - top_db)
    return _dct2_ortho(log_spec)[:n_mfcc]


def _kmeans_numpy(X, k: int, iters: int = 25, seed: int = 0):
    """Tiny KMeans with k-means++ initialisation; returns int labels."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    if n == 0:
        return np.zeros(0, dtype=int)
    # k-means++ init
    centers = [X[rng.integers(0, n)]]
    for _ in range(1, k):
        d2 = np.min(((X[:, None, :] - np.stack(centers)[None, :, :]) ** 2).sum(-1), axis=1)
        if d2.sum() == 0:
            centers.append(X[rng.integers(0, n)])
            continue
        probs = d2 / d2.sum()
        centers.append(X[rng.choice(n, p=probs)])
    C = np.stack(centers)
    for _ in range(iters):
        d2 = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1)
        labels = d2.argmin(axis=1)
        new_C = np.stack([X[labels == j].mean(0) if (labels == j).any() else C[j]
                          for j in range(k)])
        if np.allclose(new_C, C, atol=1e-4):
            break
        C = new_C
    return labels
