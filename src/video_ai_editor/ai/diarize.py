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
            data = json.loads(cache_path.read_text())
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
                 "turns": turns}, indent=2))
            return turns

        turns: list[dict] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({"speaker": str(speaker),
                          "start": float(turn.start),
                          "end": float(turn.end)})
        cache_path.write_text(json.dumps(
            {"_pipeline": pipeline_used, "turns": turns}, indent=2))
        return turns

    if not fallback:
        raise RuntimeError(_hf_token_setup_message())
    turns = _heuristic_diarize(src, cache_dir, num_speakers=num_speakers)
    cache_path.write_text(json.dumps(
        {"_pipeline": "heuristic", "turns": turns}, indent=2))
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
        capture_output=True, text=True,
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
            capture_output=True, text=True, check=True,
        ).stdout.strip())
    except Exception:
        dur = 0.0
    ends.append(dur)
    n = min(len(starts), len(ends))
    utts = [(starts[i], ends[i]) for i in range(n) if ends[i] - starts[i] > 0.4]
    if not utts:
        return []

    # 3) MFCC features per utterance
    try:
        import librosa  # type: ignore
        import numpy as np
    except ImportError:
        # Without librosa we can't get features — return everything as one speaker.
        return [{"speaker": "SPEAKER_00", "start": s, "end": e} for s, e in utts]

    y, sr = librosa.load(str(audio_wav), sr=16000, mono=True)
    feats = []
    for s, e in utts:
        i0, i1 = int(s * sr), int(min(len(y), e * sr))
        if i1 - i0 < 800:
            feats.append(np.zeros(13, dtype=np.float32))
            continue
        mfcc = librosa.feature.mfcc(y=y[i0:i1], sr=sr, n_mfcc=13)
        feats.append(mfcc.mean(axis=1))
    X = np.stack(feats)

    # 4) KMeans (numpy, k-means++ init, 25 iterations)
    labels = _kmeans_numpy(X, k=max(1, int(num_speakers)), iters=25, seed=42)
    return [
        {"speaker": f"SPEAKER_{int(labels[i]):02d}",
         "start": utts[i][0], "end": utts[i][1]}
        for i in range(len(utts))
    ]


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
