// fm-planner — Apple FoundationModels as a network-free child process.
//
//   fm-planner probe            → availability JSON on stdout (always exit 0)
//   fm-planner plan  < input    → {"ok":true,"draft":{IntentDraft},"model","latency_ms"}
//   fm-planner text  < input    → {"ok":true,"items":[…],"model","latency_ms"}
//
// Contract: one JSON object on stdin (≤ 64 KB, else exit 4), one single-line
// JSON object on stdout, exit status from Errors.swift. No URLSession, no
// Network, no sockets, no Data(contentsOf:) — tests/test_fm_helper_source_guard.py
// greps for them. The Python adapter scrubs the environment and runs this
// with an empty cwd, so the binary must never need a file.
import Foundation
import FoundationModels

let maxInputBytes = 64 * 1024
let defaultTimeoutMs = 8_000
let modelName = "apple-fm"

func emit<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    guard let data = try? encoder.encode(value), let line = String(data: data, encoding: .utf8) else {
        FileHandle.standardOutput.write("{\"ok\":false,\"code\":\"decode\",\"reason\":\"could not encode output\"}\n".data(using: .utf8)!)
        return
    }
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
}

func fail(_ failure: HelperFailure) -> Never {
    emit(FailureBody(code: failure.code, reason: failure.reason))
    exit(failure.exit.rawValue)
}

func readInput<T: Decodable>(_ type: T.Type) -> T {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    if data.count > maxInputBytes {
        fail(HelperFailure(code: "bad_input", exit: .badInput, reason: "stdin exceeds \(maxInputBytes) bytes"))
    }
    do {
        return try JSONDecoder().decode(type, from: data)
    } catch {
        fail(HelperFailure(code: "bad_input", exit: .badInput, reason: "stdin is not the documented JSON: \(error)"))
    }
}

func clampTimeout(_ requested: Int?) -> Int {
    guard let ms = requested, ms > 0 else { return defaultTimeoutMs }
    return min(ms, 60_000)
}

/// Race the generation against a wall clock. FoundationModels has no
/// cooperative deadline, so the loser is cancelled and the helper exits 3;
/// the Python side already gave up at `timeout_s + 2`.
func withTimeout<T: Sendable>(ms: Int, _ operation: @escaping @Sendable () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await operation() }
        group.addTask {
            try await Task.sleep(for: .milliseconds(ms))
            throw HelperFailure(code: "timeout", exit: .timeout, reason: "no answer within \(ms) ms")
        }
        guard let first = try await group.next() else {
            throw HelperFailure(code: "decode", exit: .decode, reason: "task group returned nothing")
        }
        group.cancelAll()
        return first
    }
}

func planInstructions(_ input: PlanInput) -> String {
    var lines: [String] = [
        "You turn one video-editing request into an ordered list of recipes.",
        "Use ONLY these recipe names, in the order the edits should happen:",
    ]
    for card in input.recipes {
        let slots = card.slots.keys.sorted().map { "\($0): \(card.slots[$0]!)" }.joined(separator: ", ")
        lines.append("- \(card.name): \(card.description)" + (slots.isEmpty ? "" : " Slots: \(slots)."))
    }
    lines.append(contentsOf: [
        "Rules:",
        "- Never invent a recipe name. Leave a slot empty unless the user said it.",
        "- tiktok, reels, shorts and story are 9:16 platforms; youtube is 16:9.",
        "- Anything the user said NOT to do goes in exclusions.",
        "- Add a needs_input question only when a required value is missing.",
        "- confidence is how sure you are, 0 to 1 — 0.9 when the request is clear.",
        "- reply is one short sentence to the user.",
    ])
    if let summary = input.timeline_summary, !summary.isEmpty {
        lines.append("Timeline: \(summary)")
    }
    return lines.joined(separator: "\n")
}

func runPlan() async {
    let input = readInput(PlanInput.self)
    let started = Date()
    do {
        let model = try requireAvailable()
        let instructions = planInstructions(input)
        let prompt = input.prompt
        let draft: IntentDraftOut = try await withTimeout(ms: clampTimeout(input.timeout_ms)) {
            let session = LanguageModelSession(model: model, instructions: instructions)
            let options = GenerationOptions(sampling: .greedy, maximumResponseTokens: 500)
            let response = try await session.respond(to: prompt, generating: IntentDraft.self, options: options)
            return IntentDraftOut(response.content)
        }
        let ms = Int(Date().timeIntervalSince(started) * 1000)
        emit(PlanOutput(draft: draft, model: modelName, latency_ms: ms))
        exit(HelperExit.ok.rawValue)
    } catch {
        fail(classify(error))
    }
}

func textInstructions(_ input: TextInput) -> (String, String)? {
    switch input.task {
    case "hook_candidates":
        guard let head = input.payload.transcript_head, !head.isEmpty else { return nil }
        let n = max(1, min(input.payload.n ?? 3, 6))
        let words = max(3, min(input.payload.max_words ?? 7, 12))
        return ("You write short-form video hooks. Propose \(n) hook lines, each at most \(words) words, "
                + "grounded in what the speaker actually says. No generic filler like 'wait for it'.",
                "Transcript start:\n\(head)")
    case "rank_windows":
        guard let windows = input.payload.windows, !windows.isEmpty else { return nil }
        let k = max(1, min(input.payload.k ?? windows.count, windows.count))
        let listing = windows.enumerated().map { "[\($0.offset)] \($0.element.text)" }.joined(separator: "\n")
        return ("You pick the most self-contained, engaging moments of a talk for short clips. "
                + "Return the \(k) best window indices, best first, each index at most once.",
                "Windows:\n\(listing)")
    default:
        return nil
    }
}

func runText() async {
    let input = readInput(TextInput.self)
    guard let (instructions, prompt) = textInstructions(input) else {
        fail(HelperFailure(code: "bad_input", exit: .badInput,
                           reason: "task must be hook_candidates or rank_windows with a non-empty payload"))
    }
    let started = Date()
    let task = input.task
    do {
        let model = try requireAvailable()
        let items: [AnyItem] = try await withTimeout(ms: clampTimeout(input.timeout_ms)) {
            let session = LanguageModelSession(model: model, instructions: instructions)
            let options = GenerationOptions(sampling: .greedy, maximumResponseTokens: 200)
            if task == "hook_candidates" {
                let response = try await session.respond(to: prompt, generating: Candidates.self, options: options)
                return response.content.items.map(AnyItem.text)
            }
            let response = try await session.respond(to: prompt, generating: Ranking.self, options: options)
            return response.content.order.map(AnyItem.index)
        }
        let ms = Int(Date().timeIntervalSince(started) * 1000)
        emit(TextOutput(items: items, model: modelName, latency_ms: ms))
        exit(HelperExit.ok.rawValue)
    } catch {
        fail(classify(error))
    }
}

let arguments = CommandLine.arguments.dropFirst()
switch arguments.first {
case "probe":
    emit(probe())
    exit(HelperExit.ok.rawValue)
case "plan":
    await runPlan()
case "text":
    await runText()
default:
    FileHandle.standardError.write("usage: fm-planner probe | plan | text   (JSON on stdin, JSON on stdout)\n".data(using: .utf8)!)
    exit(HelperExit.usage.rawValue)
}
