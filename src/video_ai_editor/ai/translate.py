"""Local translation via Argos Translate.

Lazy-imports the package on first call. Language packages download from the
Argos repo on demand (~50 MB per direction, cached forever).
"""
from __future__ import annotations
import importlib
from typing import Iterable

# (from_code, to_code) → installed Translation. Cached to avoid re-resolving.
_PIPELINE_CACHE: dict[tuple[str, str], object] = {}


def _ensure_package(from_code: str, to_code: str) -> None:
    """Download + install the Argos package for from→to if not already installed."""
    argostranslate = importlib.import_module("argostranslate")
    package_mod = importlib.import_module("argostranslate.package")

    package_mod.update_package_index()
    available = package_mod.get_available_packages()
    installed = package_mod.get_installed_packages()

    have = any(p.from_code == from_code and p.to_code == to_code for p in installed)
    if have:
        return
    candidates = [p for p in available if p.from_code == from_code and p.to_code == to_code]
    if not candidates:
        raise RuntimeError(f"no Argos package for {from_code}->{to_code}")
    pkg_path = candidates[0].download()
    package_mod.install_from_path(pkg_path)


def _get_pipeline(from_code: str, to_code: str):
    key = (from_code, to_code)
    if key in _PIPELINE_CACHE:
        return _PIPELINE_CACHE[key]
    _ensure_package(from_code, to_code)
    translate_mod = importlib.import_module("argostranslate.translate")
    installed = translate_mod.get_installed_languages()
    src = next((l for l in installed if l.code == from_code), None)
    dst = next((l for l in installed if l.code == to_code), None)
    if not src or not dst:
        raise RuntimeError(f"language not installed: {from_code} or {to_code}")
    pl = src.get_translation(dst)
    if pl is None:
        raise RuntimeError(f"no installed translation {from_code}->{to_code}")
    _PIPELINE_CACHE[key] = pl
    return pl


def translate_text(text: str, *, from_code: str = "en", to_code: str = "hi") -> str:
    if not text.strip():
        return text
    pipe = _get_pipeline(from_code, to_code)
    return pipe.translate(text)


def translate_segments(segments: Iterable[dict], *, from_code: str = "en",
                       to_code: str = "hi") -> list[dict]:
    """Translate the .text of each segment, leave timing untouched."""
    pipe = _get_pipeline(from_code, to_code)
    out = []
    for seg in segments:
        out.append({
            **seg,
            "text": pipe.translate(seg.get("text", "") or "").strip(),
        })
    return out
