// WCAG 2.x contrast math, and the design tokens' contrast contract (QA-103).
//
// The editor's colours are tokens on :root in styles.css. Text contrast was
// only ever eyeballed, and three pairs shipped below AA: white on the pink
// accent (3.21:1, every primary button), the empty-timeline hint (2.46-2.65:1)
// and the version badge (3.84:1 once its opacity was applied). This module is
// the arithmetic; lib/contrast.test.ts holds the pairs the UI actually uses
// against the real stylesheet so a token edit that breaks one fails a test.

/** Parse `#rgb`, `#rrggbb` or `rgb()/rgba()` into 0-255 channels + alpha. */
export function parseColor(input: string): [number, number, number, number] | null {
  const s = input.trim().toLowerCase()
  const hex = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/)
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].split('').map((c) => c + c).join('') : hex[1]
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1]
  }
  const fn = s.match(/^rgba?\(([^)]+)\)$/)
  if (fn) {
    const p = fn[1].split(/[\s,/]+/).filter(Boolean).map(Number)
    if (p.length < 3 || p.some((n) => Number.isNaN(n))) return null
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]
  }
  if (s === 'white') return [255, 255, 255, 1]
  if (s === 'black') return [0, 0, 0, 1]
  return null
}

const channel = (v: number): number => {
  const c = v / 255
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
}

/** WCAG relative luminance of an opaque colour. */
export function relativeLuminance(rgb: readonly [number, number, number, ...number[]]): number {
  return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])
}

/** Composite a translucent foreground over an opaque background. */
export function over(
  fg: readonly [number, number, number, number],
  bg: readonly [number, number, number, ...number[]],
): [number, number, number, number] {
  const a = fg[3]
  return [fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1]
}

/** Contrast ratio (1-21) of `fg` drawn on `bg`; a translucent fg is composited first. */
export function contrastRatio(fg: string, bg: string): number {
  const f = parseColor(fg)
  const b = parseColor(bg)
  if (!f || !b) throw new Error(`contrastRatio: cannot parse ${!f ? fg : bg}`)
  const top = over(f, b)
  const l1 = relativeLuminance(top)
  const l2 = relativeLuminance(b)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}

/** AA minimum: 4.5:1 for body text, 3:1 for large text (>= 24px, or >= 18.66px bold). */
export function aaMinimum(fontPx: number, bold = false): number {
  return fontPx >= 24 || (bold && fontPx >= 18.66) ? 3 : 4.5
}

/** `--name: value;` declarations of the first `:root { … }` block in a stylesheet. */
export function rootTokens(css: string): Record<string, string> {
  const block = css.match(/:root\s*\{([^}]*)\}/)
  const out: Record<string, string> = {}
  if (!block) return out
  for (const m of block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim()
  return out
}

/** Resolve `var(--x)` / `var(--x, fallback)` (recursively) against a token map. */
export function resolveToken(value: string, tokens: Record<string, string>, depth = 0): string {
  const v = value.trim()
  const m = v.match(/^var\(\s*(--[\w-]+)\s*(?:,\s*([^)]+))?\)$/)
  if (!m) return v
  if (depth > 8) throw new Error(`resolveToken: cycle at ${v}`)
  const hit = tokens[m[1]] ?? m[2]
  if (hit == null) throw new Error(`resolveToken: ${m[1]} is not defined`)
  return resolveToken(hit, tokens, depth + 1)
}

/** The declarations of the (last) rule whose selector list is exactly `selector`. */
export function ruleDeclarations(css: string, selector: string): Record<string, string> {
  const out: Record<string, string> = {}
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s*')
  const re = new RegExp(`(?:^|[}\\n])\\s*${esc}\\s*\\{([^}]*)\\}`, 'g')
  for (const m of css.matchAll(re)) {
    for (const d of m[1].matchAll(/([\w-]+)\s*:\s*([^;]+);?/g)) out[d[1]] = d[2].trim()
  }
  return out
}
