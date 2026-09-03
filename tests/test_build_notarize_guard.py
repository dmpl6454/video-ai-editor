"""build_notarize.sh must keep the properties that make notarization pass.

Apple's notary service rejects a PyInstaller bundle for reasons that are
invisible locally — the app still launches fine — and each round trip costs
minutes plus a real signing identity, so CI cannot exercise the pipeline.
This pins the script text instead, the way tests/test_build_spec_guard.py
pins build_app.sh:

- hardened runtime (`--options runtime`) and a secure `--timestamp` on
  every signature, with the timestamp omitted ONLY for the ad-hoc dry run;
- entitlements.plist on the main bundle and nowhere else;
- inside-out order (nested Mach-O signed before the bundle) and no `--deep`
  on any SIGNING invocation — `--deep` applies the outer options (incl.
  entitlements) to whatever nested code it finds and only recurses into
  standard locations, so on a PyInstaller layout what got signed with which
  flags is unpredictable (`--verify --deep` is fine and expected);
- the identity must be a "Developer ID Application" certificate: checked on
  the signature's Authority chain, since an "Apple Development" cert passes
  every other local check and fails only at Apple;
- the summary describes a DMG only when THIS run built it (DMG_BUILT), so a
  stale --sign-only DMG can never be reported as the notarized artifact;
- both artifacts submitted with `notarytool submit --wait`, both stapled,
  both assessed with `spctl --assess`;
- no credentials handling anywhere: no `--password`, no `--apple-id`, and
  never `store-credentials` with a password flag.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build_notarize.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _code() -> str:
    """The script minus comment lines, so prose about a flag can't satisfy
    (or trip) an assertion about the flag."""
    return "\n".join(ln for ln in _text().splitlines() if not ln.strip().startswith("#"))


def _function(code: str, name: str) -> str:
    """Body of a top-level `name() { … }` function (closing brace at column 0)."""
    m = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", code, re.M | re.S)
    assert m, f"{name}() is not defined in build_notarize.sh"
    return m.group(1)


def _sign_lines(code: str) -> list[str]:
    """Every `codesign` invocation that signs (as opposed to verifies /
    displays), joined across backslash continuations."""
    joined = re.sub(r"\\\n\s*", " ", code)
    return [ln for ln in joined.splitlines()
            if "codesign" in ln and "--sign" in ln and "--verify" not in ln]


def test_script_is_executable_and_strict():
    assert os.access(SCRIPT, os.X_OK), "build_notarize.sh must be chmod +x"
    assert "set -euo pipefail" in _code()


def test_every_signing_invocation_uses_hardened_runtime_or_is_the_dmg():
    lines = _sign_lines(_code())
    assert lines, "no codesign --sign invocations found"
    for ln in lines:
        # A DMG is a flat file: `--options runtime` does not apply to it.
        if '"$DMG"' in ln:
            continue
        assert "--options runtime" in ln, ln


def test_timestamp_is_applied_everywhere_and_only_conditional_on_adhoc():
    code = _code()
    # The literal flag lives in exactly one place — the array assignment —
    # so every invocation routes through it and the ad-hoc branch can empty it.
    # (log/die message text may mention the flag; only code lines count.)
    flag_lines = [ln for ln in code.splitlines()
                  if "--timestamp" in ln and not re.match(r"\s*(log|die|echo) ", ln)]
    assert flag_lines == ["  TIMESTAMP_ARGS=(--timestamp)"], flag_lines
    assert "TIMESTAMP_ARGS=(--timestamp)" in code
    assert "TIMESTAMP_ARGS=()" in _function(code, "resolve_identity")
    adhoc_branch = re.search(r'if \[ "\$SIGN_IDENTITY" = "-" \]; then(.*?)return', _function(code, "resolve_identity"), re.S)
    assert adhoc_branch and "TIMESTAMP_ARGS=()" in adhoc_branch.group(1)
    for ln in _sign_lines(code):
        assert "TIMESTAMP_ARGS" in ln, f"signing without the timestamp array: {ln}"


def test_entitlements_only_on_the_main_bundle():
    code = _code()
    assert "--entitlements" in _function(code, "sign_main_bundle")
    assert "--entitlements" not in _function(code, "sign_nested_machos")
    assert "--entitlements" not in _function(code, "sign_dmg")
    # Signing invocations carrying entitlements: exactly one, the bundle's.
    with_ents = [ln for ln in _sign_lines(code) if "--entitlements" in ln]
    assert len(with_ents) == 1, with_ents
    assert "$stage_app" in with_ents[0]


def test_no_deep_on_signing_invocations():
    for ln in _sign_lines(_code()):
        assert "--deep" not in ln, f"--deep on a signing invocation makes nested flags unpredictable: {ln}"
    # …while the strict verify still walks the whole bundle.
    assert "codesign --verify --strict --deep" in _code()


def test_inside_out_order_nested_then_bundle():
    body = _function(_code(), "sign_app")
    nested = body.index("sign_nested_machos ")
    bundle = body.index("sign_main_bundle ")
    verify = body.index("verify_signature ")
    assert nested < bundle < verify, "must sign nested Mach-O before the bundle, then verify"


def test_nested_signing_skips_the_main_executable_and_symlinks():
    lister = _function(_code(), "list_nested_machos")
    assert '! -path "$main_exe"' in lister
    assert "-type f" in lister


def test_notarizes_and_staples_both_artifacts():
    code = _code()
    joined = re.sub(r"\\\n\s*", " ", code)
    submit = [ln for ln in joined.splitlines() if "xcrun notarytool submit" in ln]
    assert len(submit) == 1, submit
    assert "--wait" in submit[0] and "--keychain-profile" in submit[0]
    assert "notarytool log" in code, "an Invalid verdict must fetch the notary log"
    assert "stapler staple" in code and "stapler validate" in code
    main = _function(code, "main")
    assert 'notarize "$APP" app' in main and 'notarize "$DMG" dmg' in main
    assert 'staple_and_validate "$APP"' in main and 'staple_and_validate "$DMG"' in main
    # Order in the notarizing path (the sign-only branch above it also
    # builds a DMG, so anchor after the app submission).
    after = main.index('notarize "$APP" app')
    assert main.index('staple_and_validate "$APP"', after) < main.index("make_dmg", after), \
        "the DMG must be built from the STAPLED app"
    assert main.index('notarize "$DMG" dmg', after) > main.index("sign_dmg", after)


def test_gatekeeper_assessed_for_app_and_dmg():
    code = _code()
    assert "spctl --assess --type execute" in code
    assert "spctl --assess --type open --context context:primary-signature" in code
    assert "source=Notarized Developer ID" in _function(code, "assess")
    main = _function(code, "main")
    assert "assess app required" in main and "assess dmg required" in main


def test_identity_must_be_developer_id_application():
    code = _code()
    assert 'Authority=Developer ID Application' in _function(code, "verify_signature")
    explicit = _function(code, "resolve_identity")
    assert '*"Developer ID Application"*) ;;' in explicit, \
        "an explicit VAE_SIGN_IDENTITY must be checked against the keychain listing before signing"


def test_summary_and_done_line_keyed_on_dmg_built_this_run():
    code = _code()
    assert "DMG_BUILT=1" in _function(code, "make_dmg")
    summary = _function(code, "print_summary")
    assert '[ "$DMG_BUILT" = 1 ]' in summary
    assert '[ -f "$DMG" ]' not in summary, "a pre-existing DMG must not be reported as this run's artifact"
    main = _function(code, "main")
    assert 'if [ "$DMG_BUILT" = 1 ]; then' in main
    assert "warn_stale_dmg" in main


def test_help_survives_a_closed_pipe():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    proc = subprocess.run(
        ["bash", "-o", "pipefail", "-c", f'bash "{SCRIPT}" --help | head -1; exit "${{PIPESTATUS[0]}}"'],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"--help | head exited {proc.returncode}: {proc.stderr}"
    assert "Sign, notarize" in proc.stdout


def test_adhoc_never_reaches_notarization():
    main = _function(_code(), "main")
    gate = main.index('[ "$IS_ADHOC" = 1 ]')
    assert gate < main.index('notarize "$APP" app')


def test_never_handles_credentials():
    text = _text()
    assert "--password" not in text
    assert "--apple-id" not in text
    assert "@" not in _code() or not re.search(r"[\w.]+@[\w.]+\.\w+", _code()), "no e-mail addresses in the script"
    for ln in text.splitlines():
        assert not ("store-credentials" in ln and "password" in ln.lower()), ln
    assert "store-credentials" not in _code(), "the script must never store notary credentials itself"


def test_build_notarize_sh_parses():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
