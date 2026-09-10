// The @Generable shapes the model fills, and the plain Codable shapes the
// helper prints (spec §3.2, §3.7).
//
// WHY typed scalar slots instead of an `args_json` string: constrained
// decoding can only keep the model honest about fields it can see. A JSON
// string would let it invent arg names again (baseline finding 12). Python
// maps every non-nil field of `IntentItem` to a slot of the same name.
import Foundation
import FoundationModels

@Generable
struct IntentDraft {
    @Guide(description: "Ordered edits, each naming one of the recipes given")
    var intents: [IntentItem]
    @Guide(description: "Things the user said NOT to do")
    var exclusions: [String]
    @Guide(description: "Only when a required value is missing")
    var needs_input: [Question]
    @Guide(description: "How sure you are, 0 to 1")
    var confidence: Double
    @Guide(description: "One short sentence to the user")
    var reply: String
}

@Generable
struct IntentItem {
    @Guide(description: "Exactly one of the recipe names given")
    var recipe: String
    var style: String?
    var target: String?
    var ratio: String?
    var platform: String?
    var mood: String?
    var look: String?
    var count: Int?
    var duration_s: Double?
    var factor: Double?
    var text: String?
    var handle: String?
    var name: String?
    var lufs: Double?
    var words: [String]?
}

@Generable
struct Question {
    var key: String
    var question: String
    var options: [String]
    var default_value: String?
}

@Generable
struct Candidates {
    @Guide(description: "Short hook lines, each at most seven words")
    var items: [String]
}

@Generable
struct Ranking {
    @Guide(description: "Window indices, best first")
    var order: [Int]
}

// --- wire output (Codable, Sendable) ------------------------------------

struct IntentItemOut: Codable, Sendable {
    var recipe: String
    var style: String?
    var target: String?
    var ratio: String?
    var platform: String?
    var mood: String?
    var look: String?
    var count: Int?
    var duration_s: Double?
    var factor: Double?
    var text: String?
    var handle: String?
    var name: String?
    var lufs: Double?
    var words: [String]?

    init(_ item: IntentItem) {
        recipe = item.recipe; style = item.style; target = item.target; ratio = item.ratio
        platform = item.platform; mood = item.mood; look = item.look; count = item.count
        duration_s = item.duration_s; factor = item.factor; text = item.text
        handle = item.handle; name = item.name; lufs = item.lufs; words = item.words
    }
}

struct QuestionOut: Codable, Sendable {
    var key: String
    var question: String
    var options: [String]
    var default_value: String?

    init(_ q: Question) {
        key = q.key; question = q.question; options = q.options; default_value = q.default_value
    }
}

struct IntentDraftOut: Codable, Sendable {
    var intents: [IntentItemOut]
    var exclusions: [String]
    var needs_input: [QuestionOut]
    var confidence: Double
    var reply: String

    init(_ d: IntentDraft) {
        intents = d.intents.map(IntentItemOut.init)
        exclusions = d.exclusions
        needs_input = d.needs_input.map(QuestionOut.init)
        confidence = min(1.0, max(0.0, d.confidence))
        reply = d.reply
    }
}

struct PlanOutput: Encodable, Sendable {
    let ok = true
    let draft: IntentDraftOut
    let model: String
    let latency_ms: Int
}

struct TextOutput: Encodable, Sendable {
    let ok = true
    let items: [AnyItem]
    let model: String
    let latency_ms: Int
}

/// `[String]` for hook_candidates, `[Int]` for rank_windows — one encoder.
enum AnyItem: Encodable, Sendable {
    case text(String)
    case index(Int)

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .text(let s): try c.encode(s)
        case .index(let i): try c.encode(i)
        }
    }
}

// --- wire input --------------------------------------------------------

struct RecipeCardIn: Decodable, Sendable {
    let name: String
    let description: String
    let slots: [String: String]
}

struct PlanInput: Decodable, Sendable {
    let prompt: String
    let recipes: [RecipeCardIn]
    let timeline_summary: String?
    let timeout_ms: Int?
}

struct WindowIn: Decodable, Sendable {
    let start: Double
    let end: Double
    let text: String
}

struct TextPayload: Decodable, Sendable {
    let transcript_head: String?
    let n: Int?
    let max_words: Int?
    let windows: [WindowIn]?
    let k: Int?
}

struct TextInput: Decodable, Sendable {
    let task: String
    let payload: TextPayload
    let timeout_ms: Int?
}
