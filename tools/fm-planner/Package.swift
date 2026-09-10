// swift-tools-version: 6.2
// WHY 6.2: `.macOS(.v26)` is only spelled in tools 6.2+; FoundationModels
// itself is macOS 26-only. Zero dependencies on purpose — this binary is a
// network-free child process (README, spec §3.2/§3.5) and
// tests/test_fm_helper_source_guard.py asserts the empty list stays empty.
import PackageDescription

let package = Package(
    name: "fm-planner",
    platforms: [.macOS(.v26)],
    dependencies: [],
    targets: [
        .executableTarget(
            name: "fm-planner",
            dependencies: [],
            path: "Sources/fm-planner"
        )
    ]
)
