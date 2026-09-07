"""Captions must work from the DMG, not only from a checkout.

The 0.5.0 notarized DMG was audited as a fresh recipient (clean Mac, no
Homebrew, no API key) and everything worked except this: `build_app.sh` passed
`--exclude-module faster_whisper`, so pressing Captions answered

    "Speech-to-text is unavailable in this build — the 'faster-whisper'
     package is not installed. Run the app from source (`uv sync --all-extras`)"

on the one feature a video editor is most likely to reach for, with advice a
DMG recipient has no checkout to follow. whisper.cpp was the documented escape
hatch and is not one — Homebrew's `whisper-cli` is a wrapper linking @rpath
dylibs, so shipping it means dylib surgery plus a ggml auto-downloader nobody
has written.

faster-whisper is the right thing to bundle because it needs NO torch: its
engine is ctranslate2, which this app already ships for MADLAD translation.
These tests pin that, the collect flags that are each individually load-bearing,
and the build-time receipt — because a `--collect-*` flag is a request, and
this whole class of bug is the difference between a request and a receipt.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_SH = (ROOT / "build_app.sh").read_text(encoding="utf-8")


def _mac_excludes() -> set[str]:
    return set(re.findall(r"--exclude-module\s+(\S+)", BUILD_SH))


def test_the_macos_build_no_longer_excludes_faster_whisper():
    """The one-line cause of the whole gap.

    NOTE: `test_tester_round8.py::test_mac_only_exclusions_are_flagged_too`
    asserts the OPPOSITE — it hardcodes `faster_whisper` into the set of
    libraries the macOS build drops, which was true when it was written. Its own
    failure message ("build_app.sh no longer drops these — re-check the feature
    flags") is the instruction: drop `faster_whisper` from that literal. The
    feature flag it guards is already correct (`captions.in_packaged_app` is
    True, pinned below), so nothing in features.py needs to move with it.
    """
    assert "faster_whisper" not in _mac_excludes(), (
        "build_app.sh is excluding faster-whisper again — the packaged app "
        "will tell DMG users to run from source, which they cannot do")


def test_the_heavy_libraries_are_still_excluded():
    """Bundling faster-whisper must not have been a doorway for torch.

    faster-whisper's own dependency set is ctranslate2 + huggingface-hub +
    tokenizers + onnxruntime + av + tqdm. If torch ever reappears here, the
    ~150MB bundle has quietly become a multi-gigabyte one and
    `features.py`'s `in_packaged_app=False` flags start lying in the other
    direction.
    """
    mac = _mac_excludes()
    for lib in ("torch", "torchaudio", "torchvision", "demucs", "mediapipe",
                "open_clip", "transformers"):
        assert lib in mac, f"build_app.sh stopped excluding {lib}"


def test_the_macos_build_collects_faster_whispers_data_files():
    """The Silero VAD model ships as package DATA, not as Python code.

    PyInstaller collects the module graph and never a package's data files
    unless asked, so without this the app imports faster_whisper fine and dies
    inside `transcribe(vad_filter=True)` with onnxruntime's NoSuchFile — the
    exact bug the Windows .spec's `collect_data_files('faster_whisper')` line
    already exists to stop (test_transcribe_backend.py). The macOS build does
    NOT use that .spec (CLAUDE.md), so it needs its own flag and its own test.
    """
    assert "--collect-data faster_whisper" in BUILD_SH


def test_the_macos_build_verifies_faster_whisper_actually_landed():
    """`--collect-*` is a request, not a receipt.

    This file's established rule, and the reason the ffmpeg and espeak-ng-data
    checks are fatal: a bundle that quietly lost a payload is
    indistinguishable from a working one until a user presses the button.
    """
    assert "silero_vad_v6.onnx" in BUILD_SH, (
        "no post-build check that faster-whisper's VAD asset is in the bundle")
    assert "PYZ-00.toc" in BUILD_SH, (
        "faster_whisper is pure Python, so it leaves no directory in the .app "
        "— PyInstaller's own PYZ table of contents is the only receipt that "
        "its module code was archived")
    # Fatal, in the style of the ffmpeg/espeak gates, not a warning.
    for marker in ("Silero VAD asset is not in the bundle",
                   "NOT in the PYZ archive"):
        assert marker in BUILD_SH
        after = BUILD_SH.split(marker, 1)[1].split("\nfi", 1)[0]
        assert "exit 1" in after, f"the {marker!r} check does not fail the build"


# --- what the packaged app now says about captions ---------------------------

def test_captions_are_claimed_to_be_in_the_packaged_app():
    from video_ai_editor.ai.features import FEATURES

    captions = next(f for f in FEATURES if f.key == "captions")
    assert captions.in_packaged_app, (
        "faster-whisper is bundled in both builds now; flagging captions as "
        "excluded would make feature_report() hand out the blanket "
        "'run from source' answer this fix exists to remove")


def test_the_packaged_advice_no_longer_blames_a_deliberate_exclusion():
    """Reaching the unavailable branch in a frozen app used to be BY DESIGN and
    now means the bundle is broken. Those need different answers: one is "this
    build doesn't have it", the other is "please report this"."""
    from video_ai_editor.ai.features import FEATURES

    captions = next(f for f in FEATURES if f.key == "captions")
    fix = captions.packaged_fix.lower()
    assert fix, "captions still needs a packaged-specific answer"
    assert "excludes faster-whisper" not in fix, "stale claim: it is bundled now"
    assert "packaging fault" in fix or "report" in fix


def test_the_download_note_states_the_measured_size():
    """The note said "~1.5GB for large-v3". Measured on the real cache, the
    repo is 3.09GB (model.bin alone is 3,087,284,237 bytes) — and it is the
    only warning a user gets before a multi-gigabyte download starts on a
    button press. `small`, which an import's quick transcript uses, is ~465MB.
    """
    from video_ai_editor.ai.features import FEATURES

    note = next(f for f in FEATURES if f.key == "captions").note
    assert "1.5GB" not in note, "the old, wrong figure is back"
    assert "3GB" in note, "large-v3's real download size is not stated"
    assert "465MB" in note and "small" in note, (
        "the import-time transcript pulls a different, much smaller model — "
        "a user seeing only the 3GB figure cannot tell which download is which")


def test_from_source_the_report_keeps_the_pip_fix(monkeypatch):
    """The packaged answer must stay conditional: from a checkout, `uv sync` IS
    the right thing to say."""
    from video_ai_editor.ai.features import feature_report

    rep = feature_report()
    assert rep["packaged_app"] is False
    by_key = {e["key"]: e for e in rep["unavailable"]}
    if "captions" in by_key:                    # only if faster-whisper is absent
        assert "uv sync" in by_key["captions"]["fix"]


# --- the substantive claim: it runs with every excluded module unimportable ---

_IMPORT_UNDER_EXCLUDES = textwrap.dedent(
    """
    import importlib.abc, json, re, sys, pathlib
    root = pathlib.Path(sys.argv[1])
    banned = set(re.findall(r"--exclude-module\\s+(\\S+)",
                            (root / "build_app.sh").read_text(encoding="utf-8")))

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in banned:
                raise ModuleNotFoundError(name, name=name)

    sys.meta_path.insert(0, Block())
    sys.path.insert(0, str(root / "src"))

    from faster_whisper import BatchedInferencePipeline, WhisperModel, decode_audio
    from faster_whisper.vad import SileroVADModel          # -> onnxruntime
    from video_ai_editor.ingest import transcribe          # the app's own path

    leaked = sorted(m for m in sys.modules if m.split(".")[0] in banned)
    print(json.dumps({"banned": sorted(banned), "leaked": leaked}))
    """
)


def test_faster_whisper_imports_with_every_bundle_exclusion_enforced():
    """The reason bundling it is affordable at all.

    Every module `build_app.sh` excludes is made unimportable — exactly what
    PyInstaller does in the bundle — and the whole faster-whisper import chain
    is walked anyway. torch is the one that matters: faster-whisper reaches it
    only through `ctranslate2/specs/model_spec.py`, inside a
    `try/except ImportError`, which is why this works and why it would stop
    working silently if that guard were ever removed upstream.

    Read from build_app.sh rather than hardcoded, so tightening the exclude
    list re-tests this claim instead of drifting past it. Runs in a subprocess:
    a meta-path blocker installed in the pytest process would poison every
    later test in it.
    """
    pytest.importorskip("faster_whisper")
    proc = subprocess.run([sys.executable, "-c", _IMPORT_UNDER_EXCLUDES, str(ROOT)],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    import json
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "torch" in result["banned"], "the exclude list was not read"
    assert result["leaked"] == [], (
        f"faster-whisper pulled in modules the bundle excludes: {result['leaked']}")
