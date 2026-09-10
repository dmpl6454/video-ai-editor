// `fm-planner probe` — the honest availability record (spec §3.2).
//
// `SystemLanguageModel.Availability` has exactly `.available` and
// `.unavailable(deviceNotEligible | appleIntelligenceNotEnabled | modelNotReady)`;
// "unsupportedOS" is a Python-side state (the adapter never spawns this binary
// below macOS 26). The `fix` strings are what the Prompt bar shows next to a
// greyed-out "Apple Intelligence" rung, so they name the exact user action.
import Foundation
import FoundationModels

struct ProbeResult: Encodable {
    let ok = true
    let available: Bool
    let state: String
    let fix: String?
    let os: String
    let languages: [String]
}

func osVersionString() -> String {
    let v = ProcessInfo.processInfo.operatingSystemVersion
    return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
}

func supportedLanguageCodes(_ model: SystemLanguageModel) -> [String] {
    let codes = model.supportedLanguages.compactMap { $0.languageCode?.identifier }
    return Array(Set(codes)).sorted()
}

func probe() -> ProbeResult {
    let model = SystemLanguageModel.default
    let languages = supportedLanguageCodes(model)
    switch model.availability {
    case .available:
        return ProbeResult(available: true, state: "available", fix: nil,
                           os: osVersionString(), languages: languages)
    case .unavailable(let reason):
        let (state, fix) = describe(reason)
        return ProbeResult(available: false, state: state, fix: fix,
                           os: osVersionString(), languages: languages)
    }
}

func describe(_ reason: SystemLanguageModel.Availability.UnavailableReason) -> (String, String) {
    switch reason {
    case .deviceNotEligible:
        return ("deviceNotEligible", "This Mac cannot run Apple Intelligence (Apple silicon required)")
    case .appleIntelligenceNotEnabled:
        return ("appleIntelligenceNotEnabled",
                "Turn on Apple Intelligence in System Settings → Apple Intelligence & Siri")
    case .modelNotReady:
        return ("modelNotReady",
                "Apple Intelligence is still downloading its model — try again in a few minutes")
    @unknown default:
        return ("unavailable", "Apple Intelligence is unavailable on this Mac")
    }
}

/// Raised before any generation so a disabled Apple Intelligence never
/// reaches `respond(...)` (which would surface as a less specific error).
func requireAvailable() throws -> SystemLanguageModel {
    // WHY permissive guardrails: the timeline summary carries the user's own
    // speech; the default guardrails refuse ordinary transcript snippets
    // (spec §3.2, verified on the build machine's SDK).
    let model = SystemLanguageModel(guardrails: .permissiveContentTransformations)
    if case .unavailable(let reason) = model.availability {
        let (state, fix) = describe(reason)
        throw HelperFailure(code: "unavailable", exit: .unavailable, reason: "\(state): \(fix)")
    }
    return model
}
