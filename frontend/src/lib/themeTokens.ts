// Read a design token (a custom property on :root in styles.css) from code that
// cannot use `var()` — a <canvas> 2D context takes only concrete colours.
//
// The timeline canvas hard-coded its own copy of the palette, and its copy of
// "dim" (#5a5a64) was a shade no token has: the empty-timeline hint came out at
// 2.46-2.65:1 (QA-103). Reading the token keeps the canvas on the same audited
// palette as the DOM. `fallback` covers a non-browser caller (tests) and a
// token that is missing, and is expected to equal the token's value.
//
// Resolved ONCE PER FRAME (wave-B review): the timeline's draw path called
// this ~16 times, several of them per clip, and each call ran
// getComputedStyle(document.documentElement) — on every scroll/drag frame.
// The first read in a frame resolves the style; the cache is dropped on the
// next animation frame, so a theme change still lands on the next paint.

let frameStyle: CSSStyleDeclaration | null = null
const frameCache = new Map<string, string>()

function dropFrameCache(): void {
  frameStyle = null
  frameCache.clear()
}

export function cssToken(name: string, fallback: string): string {
  if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') return fallback
  let v = frameCache.get(name)
  if (v === undefined) {
    if (!frameStyle) {
      frameStyle = getComputedStyle(document.documentElement)
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(dropFrameCache)
      else setTimeout(dropFrameCache, 0)
    }
    v = frameStyle.getPropertyValue(name).trim()
    frameCache.set(name, v)
  }
  return v || fallback
}

const UI_STACK = "-apple-system, BlinkMacSystemFont, 'Inter', 'Helvetica Neue', sans-serif"

/** A canvas `font` in the app's UI face. `ctx.font` does not accept `var()`,
 *  so '10px var(--font-ui)' was silently ignored (the canvas kept its default
 *  10px sans-serif) — the family is resolved from --font-ui here. */
export function uiFont(px: number, weight = ''): string {
  return `${weight ? `${weight} ` : ''}${px}px ${cssToken('--font-ui', UI_STACK)}`
}
