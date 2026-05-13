"""One-shot setup for pyannote diarization.

Walks the user through:
  1. Confirming pyannote.audio is installed.
  2. Detecting an HF token (env or prompt).
  3. Persisting it to .env so future launches pick it up.
  4. Verifying by attempting to load each preferred pipeline.

Run with:  uv run python -m video_ai_editor.cli.setup_pyannote
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

from ..ai.diarize import (
    PYANNOTE_PIPELINES,
    pyannote_status,
    _hf_token,
    _hf_token_setup_message,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"


def _persist_token(token: str) -> None:
    """Write/replace HUGGINGFACE_TOKEN= in .env, preserving every other key."""
    lines: list[str] = []
    if ENV_PATH.exists():
        for ln in ENV_PATH.read_text().splitlines():
            if ln.strip().startswith("HUGGINGFACE_TOKEN=") or ln.strip().startswith("HF_TOKEN="):
                continue
            lines.append(ln)
    lines.append(f"HUGGINGFACE_TOKEN={token}")
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n")


def _verify_pipelines(token: str) -> tuple[bool, str | None]:
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError:
        return False, "pyannote.audio not installed (run `uv add pyannote.audio`)"
    last_err: Exception | None = None
    for name in PYANNOTE_PIPELINES:
        try:
            Pipeline.from_pretrained(name, token=token)
            return True, name
        except Exception as e:
            last_err = e
            continue
    return False, str(last_err) if last_err else "unknown failure"


def main() -> int:
    print("=== pyannote diarization setup ===\n")
    s = pyannote_status()
    print(f"pyannote installed:  {'✓' if s['pyannote_installed'] else '✗'}"
          f"   ({s['pyannote_version'] or '—'})")
    print(f"HF token in env:     {'✓' if s['hf_token_present'] else '✗'}")
    if s["models_cached"]:
        print(f"models already cached locally:")
        for m in s["models_cached"]:
            print(f"   • {m}")
    print()

    if not s["pyannote_installed"]:
        print("Install pyannote first:\n  uv add pyannote.audio\n")
        return 1

    token = _hf_token()
    if not token:
        print(_hf_token_setup_message())
        print()
        try:
            entered = input("Paste your HF token (or press Enter to skip): ").strip()
        except EOFError:
            entered = ""
        if not entered:
            print("Skipped. The diarize tool will fall back to the librosa heuristic.")
            return 0
        _persist_token(entered)
        os.environ["HUGGINGFACE_TOKEN"] = entered
        token = entered
        print(f"Saved HUGGINGFACE_TOKEN to {ENV_PATH}\n")

    print("Verifying pyannote can load a pipeline (this downloads the model on first run)…")
    ok, info = _verify_pipelines(token)
    if ok:
        print(f"  ✓ {info} loaded successfully.")
        print("Diarize is ready to use.")
        return 0
    print(f"  ✗ Could not load any pyannote pipeline.")
    print(f"  Last error: {info}")
    print()
    print("If the error mentions a 401 / gated repo, you still need to accept the EULA at:")
    for name in PYANNOTE_PIPELINES:
        print(f"   • https://hf.co/{name}")
    print("   • https://hf.co/pyannote/segmentation-3.0")
    return 2


if __name__ == "__main__":
    sys.exit(main())
