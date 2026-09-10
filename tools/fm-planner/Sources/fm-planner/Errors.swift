// Exit codes and the exhaustive GenerationError map (spec §3.2).
//
// The Python adapter (brains/fm.py) reads BOTH the JSON `code` on stdout and
// the process exit status; the exit status is authoritative when the JSON is
// missing (a crash, a killed child). Keep the two tables in sync with
// tools/fm-planner/README.md — tests/test_prompt_fm_adapter.py drives every
// row through a fake helper.
import Foundation
import FoundationModels

enum HelperExit: Int32 {
    case ok = 0
    case usage = 1
    case unavailable = 2   // assetsUnavailable / Apple Intelligence off — router marks unavailable 5 min
    case timeout = 3
    case badInput = 4      // stdin > 64 KB or not the documented JSON
    case guardrail = 5     // guardrailViolation AND refusal — both "fall through"
    case decode = 6        // decodingFailure / unsupportedGuide / anything unknown
    case language = 7      // unsupportedLanguageOrLocale — Python negative-caches the script
    case context = 8       // exceededContextWindowSize — Python retries with ≤ 8 cards
    case busy = 9          // rateLimited / concurrentRequests — 60 s negative cache
}

struct HelperFailure: Error, Sendable {
    let code: String
    let exit: HelperExit
    let reason: String
}

struct FailureBody: Encodable {
    let ok = false
    let code: String
    let reason: String
}

/// Every `LanguageModelSession.GenerationError` case has a row here; the
/// `@unknown default` keeps a future SDK case from crashing the helper (it
/// becomes `decode`, which the router treats as "try the next brain").
func classify(_ error: Error) -> HelperFailure {
    if let failure = error as? HelperFailure { return failure }
    if error is CancellationError {
        return HelperFailure(code: "timeout", exit: .timeout, reason: "cancelled")
    }
    guard let generation = error as? LanguageModelSession.GenerationError else {
        return HelperFailure(code: "decode", exit: .decode, reason: String(describing: error))
    }
    switch generation {
    case .guardrailViolation(let ctx):
        return HelperFailure(code: "guardrail", exit: .guardrail, reason: ctx.debugDescription)
    case .refusal(_, let ctx):
        return HelperFailure(code: "refusal", exit: .guardrail, reason: ctx.debugDescription)
    case .unsupportedLanguageOrLocale(let ctx):
        return HelperFailure(code: "language", exit: .language, reason: ctx.debugDescription)
    case .exceededContextWindowSize(let ctx):
        return HelperFailure(code: "context", exit: .context, reason: ctx.debugDescription)
    case .rateLimited(let ctx):
        return HelperFailure(code: "busy", exit: .busy, reason: ctx.debugDescription)
    case .concurrentRequests(let ctx):
        return HelperFailure(code: "busy", exit: .busy, reason: ctx.debugDescription)
    case .decodingFailure(let ctx):
        return HelperFailure(code: "decode", exit: .decode, reason: ctx.debugDescription)
    case .unsupportedGuide(let ctx):
        return HelperFailure(code: "decode", exit: .decode, reason: ctx.debugDescription)
    case .assetsUnavailable(let ctx):
        return HelperFailure(code: "unavailable", exit: .unavailable, reason: ctx.debugDescription)
    @unknown default:
        return HelperFailure(code: "decode", exit: .decode, reason: String(describing: generation))
    }
}
