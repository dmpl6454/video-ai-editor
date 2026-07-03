"""Multi-cam switcher — pick the best take per time window across N synced inputs.

Workflow: the user has N angles of the same moment (handheld + tripod + phone,
or interview cam-A + cam-B). They feed the file paths in. We:

  1. Sync each take to a reference (the first input) by finding the audio cross-
     correlation peak — handles small clap-board offsets cleanly.
  2. Walk the project in `window_s` chunks. For each window pick the take with
     the highest "interestingness" score:
        - audio_score = RMS energy + zero-crossing rate (speech proxy)
        - motion_score = mean abs frame-diff (avoid static b-roll holds)
        - face_bonus = +0.2 if MediaPipe finds a face this window
  3. Emit a list of cuts: [{"src": path, "start_in_src": s, "end_in_src": e,
     "start_on_timeline": t}, ...] that the dispatch layer can turn into
     V1 clip ops in one shot.

Heavyish — we sample ~2 frames per window to keep it fast. A 60s 3-cam shoot
typically processes in <10s.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from typing import Iterable

from .. import platformutil as _pu


def _probe_duration(p: Path) -> float:
    out = subprocess.run(
        [_pu.FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(p)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out) if out else 0.0


def _audio_offset(ref: Path, other: Path, sr: int = 16000,
                  search_s: float = 2.0) -> float:
    """Find the offset (seconds) that aligns `other` to `ref` via FFT
    cross-correlation on the first ~5s of audio. Positive = `other` lags ref."""
    try:
        import numpy as np
        import librosa  # type: ignore
    except ImportError:
        return 0.0
    try:
        y_r, _ = librosa.load(str(ref), sr=sr, mono=True, duration=5.0)
        y_o, _ = librosa.load(str(other), sr=sr, mono=True, duration=5.0)
    except Exception:
        return 0.0
    # FFT cross-correlation
    n = max(len(y_r), len(y_o))
    n2 = 1
    while n2 < 2 * n:
        n2 *= 2
    R = np.fft.rfft(y_r, n2)
    O = np.fft.rfft(y_o, n2)
    xc = np.fft.irfft(R * np.conj(O))
    # Search window
    max_lag = int(search_s * sr)
    forward = xc[:max_lag]
    backward = xc[-max_lag:]
    best_fwd = forward.argmax()
    best_bwd = backward.argmax()
    if forward[best_fwd] >= backward[best_bwd]:
        return best_fwd / sr
    return -(max_lag - best_bwd) / sr


def _window_score(src: Path, t0: float, t1: float) -> dict:
    """Sample 2 frames + a 0.5s audio chunk in the window, return scores."""
    try:
        import numpy as np
        import librosa  # type: ignore
        import cv2
    except ImportError:
        return {"audio": 0.0, "motion": 0.0, "face": 0.0}
    # Audio energy
    try:
        y, sr = librosa.load(str(src), sr=16000, mono=True,
                             offset=t0, duration=max(0.1, t1 - t0))
        if len(y) == 0:
            audio = 0.0
        else:
            rms = float(np.sqrt(np.mean(y ** 2)))
            zcr = float(np.mean(np.abs(np.diff(np.sign(y))) > 0))
            audio = min(1.0, 4 * rms + 0.4 * zcr)
    except Exception:
        audio = 0.0
    # Motion via two frame samples
    motion = 0.0; face = 0.0
    try:
        cap = cv2.VideoCapture(str(src))
        for k, t in enumerate([(t0 + t1) / 3, (t0 + t1) * 2 / 3]):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                continue
            small = cv2.resize(frame, (160, 90))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if k == 0:
                prev = gray
            else:
                motion = float(np.mean(cv2.absdiff(gray, prev))) / 255.0
            # Face bonus
            try:
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                cascade = cv2.CascadeClassifier(cascade_path)
                faces = cascade.detectMultiScale(gray, 1.2, 4)
                if len(faces):
                    face = 0.2
            except Exception:
                pass
        cap.release()
    except Exception:
        pass
    return {"audio": audio, "motion": min(1.0, 3 * motion), "face": face,
            "score": audio * 0.6 + min(1.0, 3 * motion) * 0.3 + face}


def plan_multicam(srcs: Iterable[Path], *,
                  window_s: float = 2.0,
                  total: float | None = None) -> dict:
    """Return a switching plan + diagnostics.

    Output: {
      "cuts": [{"src": str, "start_in_src": float, "end_in_src": float,
                "start_on_timeline": float, "score": float}, ...],
      "offsets": [float per take],
      "duration": float,
    }
    """
    paths = [Path(p) for p in srcs]
    if not paths:
        raise ValueError("multicam needs at least one source")
    if len(paths) == 1:
        d = total or _probe_duration(paths[0])
        return {"cuts": [{"src": str(paths[0]), "start_in_src": 0.0,
                          "end_in_src": d, "start_on_timeline": 0.0, "score": 1.0}],
                "offsets": [0.0], "duration": d}

    durations = [_probe_duration(p) for p in paths]
    offsets = [0.0]
    for p in paths[1:]:
        offsets.append(_audio_offset(paths[0], p))

    project_end = total or min(durations)
    if project_end <= 0:
        raise RuntimeError("multicam: could not probe any input duration")

    cuts: list[dict] = []
    t = 0.0
    last_winner_idx: int | None = None
    while t < project_end:
        end = min(project_end, t + window_s)
        scores = []
        for i, p in enumerate(paths):
            s_t = max(0.0, t + offsets[i])
            e_t = max(s_t + 0.05, end + offsets[i])
            sc = _window_score(p, s_t, e_t)
            scores.append((sc["score"], i, sc))
        scores.sort(reverse=True)
        winner = scores[0][1]
        # Stickiness: prefer keeping the same camera if the runner-up is close
        if last_winner_idx is not None:
            best_score, best_i, _ = scores[0]
            for sc, i, _ in scores:
                if i == last_winner_idx and best_score - sc < 0.08:
                    winner = i
                    break
        s_t = max(0.0, t + offsets[winner])
        e_t = max(s_t + 0.05, end + offsets[winner])
        cuts.append({
            "src": str(paths[winner]),
            "start_in_src": s_t,
            "end_in_src": e_t,
            "start_on_timeline": t,
            "score": float(scores[0][0]),
        })
        last_winner_idx = winner
        t = end

    # Merge consecutive cuts from the same take
    merged: list[dict] = []
    for c in cuts:
        if merged and merged[-1]["src"] == c["src"] and \
           abs(merged[-1]["end_in_src"] - c["start_in_src"]) < 0.05:
            merged[-1]["end_in_src"] = c["end_in_src"]
        else:
            merged.append(dict(c))
    return {"cuts": merged, "offsets": offsets, "duration": project_end}
