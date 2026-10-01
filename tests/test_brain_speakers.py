"""EB1-D: numpy diarization (k, turns, DER), angle hints by own-mic energy,
the numpy MFCC against librosa, and seed stability."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_analysis_fixtures as F  # noqa: E402


@pytest.fixture(scope="module")
def p2():
    return F.p2_or_skip()


@pytest.fixture(scope="module")
def p2_layers(p2):
    from video_ai_editor.brain.analysis import audio as A
    from video_ai_editor.brain.analysis import pcm as P
    from video_ai_editor.brain.analysis import speakers as S
    rec = p2.p2.recorder_wav
    pcm = P.read_pcm(rec)
    audio = A.build_audio_layer(rec, pcm=pcm)
    spk = S.build_speakers_layer(pcm, audio, transcript=F.ensure_transcript(rec))
    return pcm, audio, spk


def _map_clusters(layer: dict, truth) -> dict[str, str]:
    """cluster id → truth speaker (S1/S2), by talking-time overlap."""
    hz = 100
    masks = {s: F.speaker_mask(truth, s, hz) for s in ("S1", "S2")}
    out = {}
    for sp in layer["speakers"]:
        m = np.zeros(max(len(masks["S1"]), len(masks["S2"])), dtype=bool)
        for u in layer["utterances"]:
            if u["spk"] == sp["id"]:
                m[int(u["t0"] * hz): int(u["t1"] * hz) + 1] = True
        out[sp["id"]] = max(("S1", "S2"), key=lambda s: int((m[: len(masks[s])] & masks[s]).sum()))
    return out


def test_p2_k2_turns_within_0_3s_der_leq_15(p2, p2_layers):
    _pcm, _audio, layer = p2_layers
    from video_ai_editor.brain.analysis import speakers as S
    truth = p2.p2.truth
    assert layer["k"] == 2, layer["k_method"]
    assert layer["engine"] == "numpy-mfcc13-kmeans"
    cmap = _map_clusters(layer, truth)
    assert set(cmap.values()) == {"S1", "S2"}, cmap
    hz = 100
    truth_masks = {s: F.speaker_mask(truth, s, hz) for s in ("S1", "S2")}
    n = len(truth_masks["S1"])
    labelled = {s: np.zeros(n, dtype=bool) for s in ("S1", "S2")}
    for u in layer["utterances"]:
        labelled[cmap[u["spk"]]][int(u["t0"] * hz): int(u["t1"] * hz) + 1] = True
    speech = truth_masks["S1"] | truth_masks["S2"]
    wrong = ((labelled["S1"] & truth_masks["S2"] & ~truth_masks["S1"]) |
             (labelled["S2"] & truth_masks["S1"] & ~truth_masks["S2"]))
    missed = speech & ~(labelled["S1"] | labelled["S2"])
    der = (int(wrong.sum()) + int(missed.sum())) / max(1, int(speech.sum()))
    assert der <= 0.15, f"DER {der:.3f}"
    # turn boundaries: >= 90 % of the truth speaker changes have a layer turn edge within 0.3 s
    # (an utterance shorter than UTT_MIN_S — a 0.33 s "Right.", a 0.28 s "um" — is by
    # construction not a speaker turn here; the planner's backchannel test reads the words)
    changes = [u.voiced[0] for a, u in zip(truth.utts, truth.utts[1:])
               if a.speaker != u.speaker and a.kind not in ("pause", "gap", "silence")
               and u.kind not in ("pause", "gap", "silence") and u.voiced[1] - u.voiced[0] >= S.UTT_MIN_S]
    edges = [t["t0"] for t in layer["turns"]] + [t["t1"] for t in layer["turns"]]
    hit = sum(1 for c in changes if min(abs(c - e) for e in edges) <= 0.3)
    assert hit / len(changes) >= 0.9, (hit, len(changes))


def test_roles_host_and_guest(p2, p2_layers):
    """host = the higher question share with the lower speech share, by §3.4."""
    from video_ai_editor.brain.analysis import speech as SP
    pcm, audio, layer = p2_layers
    transcript = F.ensure_transcript(p2.p2.recorder_wav)
    speech = SP.build_speech_layer(transcript, audio_layer=audio, pcm=pcm, speakers_layer=layer)
    rows = SP.speaker_roles(speech, layer)
    roles = {s["id"]: s["role_guess"] for s in rows}
    cmap = _map_clusters(layer, p2.p2.truth)
    truth_roles = {sid: p2.p2.truth.speakers[sid]["role"] for sid in ("S1", "S2")}
    assert {cmap[k]: v for k, v in roles.items()} == truth_roles, (roles, truth_roles)


def test_angle_hints_by_own_mic_margin_geq_0_3(p2, p2_layers):
    from video_ai_editor.brain.analysis import audio as A
    from video_ai_editor.brain.analysis import pcm as P
    from video_ai_editor.brain.analysis import speakers as S
    truth = p2.p2.truth
    _pcm, ref_audio, layer = p2_layers
    paths = {"cam_a": p2.p2.cam_a, "cam_b": p2.p2.cam_b}
    angles = {path: A.build_audio_layer(path, pcm=P.read_pcm(path)) for path in paths.values()}
    hints = S.angle_hints(layer, ref_audio, angles, offsets={paths[k]: truth.offsets[k] for k in paths})
    cmap = _map_clusters(layer, truth)
    for sid, h in hints.items():
        assert h["by"] == "own_mic", h
        assert h["angle"] == paths[next(k for k, spk in truth.own_mic.items() if spk == cmap[sid])], (sid, h)
        assert h["confidence"] >= 0.3, h
    assert len(hints) == 2


def test_numpy_mfcc_matches_librosa_1e_3():
    librosa = pytest.importorskip("librosa")
    from video_ai_editor.ai.diarize import mfcc13_numpy
    sr = 16000
    rng = np.random.default_rng(7)
    t = np.arange(sr * 2) / sr
    y = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
         + 0.05 * rng.standard_normal(len(t))).astype(np.float64)
    ours = mfcc13_numpy(y, sr, librosa_compatible=True)
    ref = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    assert ours.shape == ref.shape, (ours.shape, ref.shape)
    assert float(np.max(np.abs(ours - ref))) <= 1e-3


def test_kmeans_and_mfcc_import_without_librosa():
    import subprocess
    code = ("import sys; sys.modules['librosa']=None\n"
            "import numpy as np\n"
            "from video_ai_editor.ai.diarize import _kmeans_numpy, mfcc13_numpy\n"
            "y=np.sin(np.arange(16000)/16000*2*np.pi*200)\n"
            "m=mfcc13_numpy(y,16000)\n"
            "print(m.shape[0], int(_kmeans_numpy(np.random.default_rng(1).random((20,3)), k=2, seed=42).max()))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parents[1]),
                         env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", "HF_HUB_OFFLINE": "1"})
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.split() == ["13", "1"], out.stdout


def test_seed_stable(p2, p2_layers):
    from video_ai_editor.brain.analysis import speakers as S
    pcm, audio, layer = p2_layers
    again = S.build_speakers_layer(pcm, audio, transcript=F.ensure_transcript(p2.p2.recorder_wav))
    assert json.dumps(again, sort_keys=True) == json.dumps(layer, sort_keys=True)
