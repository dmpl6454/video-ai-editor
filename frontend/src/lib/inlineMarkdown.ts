// The chat pane's replies carry a little inline markdown — the Prompt
// Editor's clarify text says "Reply **captions**, **tighten** or
// **auto_edit**", and a Claude turn writes `code` and **bold** — which the
// pane used to print verbatim (QA-018: "Chat shows raw markdown (**tiktok**)").
//
// Deliberately tiny and pure: **bold**, __bold__ and `code` spans only, no
// HTML ever (the pieces are rendered as React text nodes, so a reply can
// never inject markup), and an unpaired marker stays literal text.

export type InlineSpan = { kind: 'text' | 'bold' | 'code'; text: string }

const TOKEN_RE = /(\*\*|__)(?=\S)([\s\S]*?\S)\1|`([^`\n]+)`/g

export function parseInlineMarkdown(text: string): InlineSpan[] {
  const out: InlineSpan[] = []
  let last = 0
  const push = (span: InlineSpan) => {
    if (!span.text) return
    const prev = out[out.length - 1]
    if (prev && prev.kind === 'text' && span.kind === 'text') prev.text += span.text
    else out.push(span)
  }
  for (const m of text.matchAll(TOKEN_RE)) {
    const at = m.index ?? 0
    push({ kind: 'text', text: text.slice(last, at) })
    if (m[3] !== undefined) push({ kind: 'code', text: m[3] })
    else push({ kind: 'bold', text: m[2] })
    last = at + m[0].length
  }
  push({ kind: 'text', text: text.slice(last) })
  return out
}
