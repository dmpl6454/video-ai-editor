"""The macOS bundle's Info.plist version must come from VERSION.

PyInstaller's CLI mode (build_app.sh) has no version flag, so without an
explicit stamp the .app ships as CFBundleShortVersionString "0.0.0" — which
is exactly what 0.4.1 shipped with. Pin the stamp the same way
test_build_spec_guard.py pins --specpath: by reading the script, because no
dev path ever runs a full PyInstaller build.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_build_app_stamps_bundle_version_from_VERSION():
    script = (ROOT / "build_app.sh").read_text(encoding="utf-8")
    assert "CFBundleShortVersionString" in script
    assert "CFBundleVersion" in script
    assert "< VERSION" in script, "the stamp must read the repo-root VERSION file"
    # The stamp has to land before the signing stage: a plist edit after
    # codesign invalidates the signature.
    assert script.index("CFBundleShortVersionString") < script.index("codesign --force")
