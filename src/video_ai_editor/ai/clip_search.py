"""Local CLIP visual search over project footage.

palmier-pro runs SigLIP2 via CoreML to let agents search footage by visual
content ("the shot with a sunset"). We do the same with open_clip on MPS:
extract a few keyframes per clip, embed them with CLIP, and rank against the
text query by cosine similarity. Fully local — the model downloads once
(~150 MB) to the torch cache, then everything runs on the Apple GPU.

Frame embeddings are cached on disk keyed by a content fingerprint, so a clip
is only embedded once no matter how many searches run.

Model: ViT-B-32 / laion2b_s34b_b79k. Small, fast on MPS, strong zero-shot.
Override with VAI_CLIP_MODEL / VAI_CLIP_PRETRAINED.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path

MODEL_NAME = os.environ.get("VAI_CLIP_MODEL", "ViT-B-32")
PRETRAINED = os.environ.get("VAI_CLIP_PRETRAINED", "laion2b_s34b_b79k")
FRAMES_PER_CLIP = int(os.environ.get("VAI_CLIP_FRAMES", "4"))


def available() -> bool:
    try:
        import importlib
        importlib.import_module("open_clip")
        importlib.import_module("torch")
        return True
    except ImportError:
        return False


# Lazy singletons — load the model once per process.
_MODEL = None
_PREPROCESS = None
_TOKENIZER = None
_DEVICE = None


def _load():
    global _MODEL, _PREPROCESS, _TOKENIZER, _DEVICE
    if _MODEL is not None:
        return
    import torch
    import open_clip
    _DEVICE = os.environ.get("VAI_CLIP_DEVICE") or (
        "mps" if torch.backends.mps.is_available()
        else "cuda" if torch.cuda.is_available() else "cpu"
    )
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED
    )
    model.eval().to(_DEVICE)
    _MODEL = model
    _PREPROCESS = preprocess
    _TOKENIZER = open_clip.get_tokenizer(MODEL_NAME)


def embed_text(query: str):
    """Return an L2-normalised CLIP text embedding (numpy float32)."""
    _load()
    import torch
    with torch.no_grad():
        toks = _TOKENIZER([query]).to(_DEVICE)
        feats = _MODEL.encode_text(toks)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats[0].float().cpu().numpy()


def _embed_images(pil_images):
    _load()
    import torch
    batch = torch.stack([_PREPROCESS(im) for im in pil_images]).to(_DEVICE)
    with torch.no_grad():
        feats = _MODEL.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.float().cpu().numpy()


def _clip_fingerprint(src: str, in_: float, out: float) -> str:
    try:
        mtime = Path(src).stat().st_mtime
    except OSError:
        mtime = 0
    key = f"{src}|{in_:.3f}|{out:.3f}|{mtime}|{MODEL_NAME}|{FRAMES_PER_CLIP}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _extract_frames(src: str, in_: float, out: float, n: int, work: Path) -> list:
    """Pull `n` evenly-spaced keyframes from [in_, out] as PIL images."""
    from PIL import Image
    work.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, out - in_)
    times = [in_ + dur * (i + 0.5) / n for i in range(n)]
    imgs = []
    for i, t in enumerate(times):
        fp = work / f"f{i}.jpg"
        # -ss before -i = fast seek; scale down for cheap embedding.
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", src,
             "-frames:v", "1", "-vf", "scale=320:-2", str(fp)],
            capture_output=True,
        )
        if fp.exists() and fp.stat().st_size > 0:
            try:
                imgs.append(Image.open(fp).convert("RGB"))
            except Exception:
                pass
    return imgs


def index_clip(src: str, in_: float, out: float, cache_dir: Path):
    """Embed a clip's keyframes (cached). Returns a dict {times, vectors}."""
    import numpy as np
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = _clip_fingerprint(src, in_, out)
    cache = cache_dir / f"clip_{fp}.npz"
    if cache.exists():
        d = np.load(cache)
        return {"times": d["times"], "vectors": d["vectors"]}

    work = cache_dir / f"work_{fp}"
    imgs = _extract_frames(src, in_, out, FRAMES_PER_CLIP, work)
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    if not imgs:
        return {"times": np.zeros(0), "vectors": np.zeros((0, 1))}
    vecs = _embed_images(imgs)
    dur = max(0.1, out - in_)
    times = np.array([in_ + dur * (i + 0.5) / len(imgs) for i in range(len(imgs))],
                     dtype=np.float32)
    np.savez(cache, times=times, vectors=vecs.astype(np.float32))
    return {"times": times, "vectors": vecs}


def search(query: str, clips: list[dict], cache_dir: Path, limit: int = 10) -> list[dict]:
    """Rank `clips` (each {id, src, in, out}) against `query` by best-frame
    cosine similarity. Returns [{clip_id, score, time, src_name}] sorted desc.
    """
    if not available():
        raise RuntimeError(
            "CLIP search needs open_clip + torch. `uv add open_clip_torch`.")
    import numpy as np
    qvec = embed_text(query)
    results = []
    for c in clips:
        idx = index_clip(c["src"], float(c["in"]), float(c["out"]), cache_dir)
        vectors = idx["vectors"]
        if vectors.shape[0] == 0:
            continue
        sims = vectors @ qvec  # both normalised → cosine
        best = int(sims.argmax())
        results.append({
            "clip_id": c["id"],
            "score": round(float(sims[best]), 4),
            "time": round(float(idx["times"][best]), 2),
            "src_name": Path(c["src"]).name,
        })
    results.sort(key=lambda r: -r["score"])
    return results[:limit]
