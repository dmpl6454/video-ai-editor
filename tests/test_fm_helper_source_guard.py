"""Tripwire for the Apple Intelligence helper (spec §3.5/§3.6): a plain
child process with no network code and no dependencies. The BOUNDARY is
`validate_plan`; this test makes sure the helper never grows a second way
out. Patterns are word-bounded so `ProcessInfo` does not false-positive."""
from __future__ import annotations

import re
from pathlib import Path


HELPER = Path(__file__).resolve().parents[1] / "tools" / "fm-planner"
SOURCES = HELPER / "Sources" / "fm-planner"

FORBIDDEN = [re.compile(p, re.M) for p in (
    r"\bURLSession\b", r"\bNWConnection\b", r"\bNWListener\b", r"^import Network\b", r"\bProcess\(",
    r"\bsocket\(", r"\bconnect\(", r"\bCFSocket", r"\bNSURL", r"Data\(contentsOf", r"String\(contentsOf",
    r"\bFileManager\b.*\bcreateFile", r"\bNSTask\b", r"\bdlopen\(", r"\bsystem\(",
)]


def _swift_files() -> list[Path]:
    files = sorted(SOURCES.glob("*.swift"))
    assert files, "no Swift sources found — the helper package is missing"
    return files


def _code(path: Path) -> str:
    """Source minus `//` comments, so prose about a forbidden API cannot
    trip (or satisfy) an assertion."""
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("//"))


def test_the_expected_files_exist():
    names = {p.name for p in _swift_files()}
    assert {"main.swift", "IntentDraft.swift", "Availability.swift", "Errors.swift"} <= names
    assert (HELPER / "Package.swift").exists() and (HELPER / "README.md").exists()


def test_no_network_or_process_spawning_apis_anywhere():
    for path in _swift_files():
        code = _code(path)
        for pat in FORBIDDEN:
            assert not pat.search(code), f"{path.name}: forbidden API {pat.pattern}"
        assert "ProcessInfo.processInfo.operatingSystemVersion" in code or path.name != "Availability.swift"


def test_package_declares_zero_dependencies_and_macos_26():
    manifest = _code(HELPER / "Package.swift")
    assert re.search(r"^// swift-tools-version: 6\.\d", (HELPER / "Package.swift").read_text(encoding="utf-8"), re.M)
    assert ".macOS(.v26)" in manifest
    assert ".package(" not in manifest
    deps = re.findall(r"dependencies:\s*\[([^\]]*)\]", manifest)
    assert deps and all(d.strip() == "" for d in deps), deps
    assert "import FoundationModels" in _code(SOURCES / "main.swift")


def test_error_map_is_exhaustive_over_the_nine_generation_error_cases():
    errors = _code(SOURCES / "Errors.swift")
    for case in ("exceededContextWindowSize", "assetsUnavailable", "guardrailViolation", "unsupportedGuide",
                 "unsupportedLanguageOrLocale", "decodingFailure", "rateLimited", "concurrentRequests", "refusal"):
        assert f"case .{case}" in errors, case
    assert "@unknown default" in errors
    exits = dict(re.findall(r"case (\w+) = (\d+)", errors))
    assert exits == {"ok": "0", "usage": "1", "unavailable": "2", "timeout": "3", "badInput": "4",
                     "guardrail": "5", "decode": "6", "language": "7", "context": "8", "busy": "9"}


def test_input_cap_permissive_guardrails_greedy_sampling_and_typed_slots():
    main = _code(SOURCES / "main.swift")
    assert "64 * 1024" in main and "maxInputBytes" in main
    assert "GenerationOptions(sampling: .greedy, maximumResponseTokens: 500)" in main
    assert ".permissiveContentTransformations" in _code(SOURCES / "Availability.swift")
    draft = _code(SOURCES / "IntentDraft.swift")
    assert "@Generable" in draft and "args_json" not in draft
    for field in ("style", "target", "ratio", "platform", "mood", "look", "count", "duration_s", "factor",
                  "text", "handle", "name", "lufs", "words"):
        assert re.search(rf"\bvar {field}: ", draft), field


def test_readme_documents_the_contract():
    readme = (HELPER / "README.md").read_text(encoding="utf-8")
    for token in ("probe", "plan", "text", "timeout_ms", "appleIntelligenceNotEnabled", "exit", "64 KB",
                  "VAI_FM_HELPER", ".binpath"):
        assert token in readme, token
    for code in range(2, 10):
        assert re.search(rf"\b{code}\b", readme), f"exit code {code} undocumented"


def test_gitignore_covers_the_build_products():
    gi = (HELPER.parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "tools/fm-planner/.build/" in gi and "tools/fm-planner/.binpath" in gi
