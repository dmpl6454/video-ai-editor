// The inline CSS a transition tile needs to animate its family — pure, so a
// test can assert every preview kind produces a complete, well-formed set of
// custom properties (a missing `--cp-to` would render a frozen tile with no
// error anywhere). transitionsPanel.css owns the keyframes; this file only
// supplies the per-tile variables the keyframes read: clip-path endpoints,
// translate directions, a mask angle, and whether the OUTGOING layer is on
// top (a close/reveal/squeeze animates A away rather than B in).

import type { Direction, TransitionPreview } from './transitionCatalog'

export type PreviewVars = Record<`--${string}`, string>

const FULL = 'inset(0 0 0 0)'

// clip-path endpoints per kind (dir-dependent where it matters).
function clipEndpoints(kind: TransitionPreview['kind'], dir: Direction | null): [string, string] | null {
  switch (kind) {
    case 'wipe': {
      const from = dir === 'left' ? 'inset(0 0 0 100%)' : dir === 'right' ? 'inset(0 100% 0 0)'
        : dir === 'up' ? 'inset(100% 0 0 0)' : dir === 'down' ? 'inset(0 0 100% 0)'
        : dir === 'tl' ? 'inset(0 100% 100% 0)' : dir === 'tr' ? 'inset(0 0 100% 100%)'
        : dir === 'bl' ? 'inset(100% 100% 0 0)' : 'inset(100% 0 0 100%)'
      return [from, FULL]
    }
    case 'diag': {
      // A triangle growing from the named corner across the whole tile.
      const c = dir === 'tl' ? ['0 0', '200% 0', '0 200%'] : dir === 'tr' ? ['100% 0', '-100% 0', '100% 200%']
        : dir === 'bl' ? ['0 100%', '200% 100%', '0 -100%'] : ['100% 100%', '-100% 100%', '100% -100%']
      return [`polygon(${c[0]}, ${c[0]}, ${c[0]})`, `polygon(${c[0]}, ${c[1]}, ${c[2]})`]
    }
    case 'iris': return ['circle(0% at 50% 50%)', 'circle(75% at 50% 50%)']
    case 'iris-close': return ['circle(75% at 50% 50%)', 'circle(0% at 50% 50%)']
    case 'crop': return dir ? ['inset(45% 45% 45% 45%)', FULL] : ['circle(0% at 50% 50%)', 'circle(75% at 50% 50%)']
    case 'box': return ['inset(50% 50% 50% 50%)', FULL]
    case 'diamond': return ['polygon(50% 50%, 50% 50%, 50% 50%, 50% 50%)', 'polygon(50% -50%, 150% 50%, 50% 150%, -50% 50%)']
    case 'doors': return ['inset(0 50% 0 50%)', FULL]
    case 'doors-close': return [FULL, 'inset(0 50% 0 50%)']
    case 'curtain': return ['inset(50% 0 50% 0)', FULL]
    case 'curtain-close': return [FULL, 'inset(50% 0 50% 0)']
    case 'wave': return [
      'polygon(0 100%, 25% 100%, 50% 100%, 75% 100%, 100% 100%, 100% 100%, 0 100%)',
      'polygon(0 -12%, 25% 12%, 50% -12%, 75% 12%, 100% -12%, 100% 100%, 0 100%)',
    ]
    default: return null
  }
}

function translateFor(dir: Direction | null): { from: string; out: string } {
  // `from` = where the INCOMING clip starts; `out` = where the OUTGOING one
  // ends. "slideleft" pushes in from the right and out to the left.
  switch (dir) {
    case 'right': return { from: 'translateX(-100%)', out: 'translateX(100%)' }
    case 'up': return { from: 'translateY(100%)', out: 'translateY(-100%)' }
    case 'down': return { from: 'translateY(-100%)', out: 'translateY(100%)' }
    case 'left': default: return { from: 'translateX(100%)', out: 'translateX(-100%)' }
  }
}

const A_ON_TOP = new Set<TransitionPreview['kind']>([
  'iris-close', 'doors-close', 'curtain-close', 'reveal', 'squeeze', 'wind', 'blur',
])

/** The `--tp-*` variables the tile's CSS animations read. Total: never empty. */
export function previewStyle(p: TransitionPreview): PreviewVars {
  const vars: PreviewVars = { '--tp-dur': p.kind === 'whip' || p.kind === 'glitch' ? '0.8s' : '1.3s' }
  const clip = clipEndpoints(p.kind, p.dir)
  if (clip) { vars['--cp-from'] = clip[0]; vars['--cp-to'] = clip[1] }
  if (['slide', 'cover', 'reveal', 'whip', 'wind', 'slice'].includes(p.kind)) {
    const t = translateFor(p.dir)
    vars['--tx-from'] = t.from
    vars['--tx-out'] = t.out
  }
  if (p.kind === 'squeeze') vars['--sq'] = p.dir === 'up' || p.dir === 'down' ? 'scaleY(0)' : 'scaleX(0)'
  if (p.kind === 'blinds' || p.kind === 'slice') {
    vars['--mask-angle'] = p.dir === 'up' || p.dir === 'down' ? '0deg' : '90deg'
  }
  if (p.kind === 'bars') vars['--mask-angle'] = '0deg'
  return vars
}

/** The class list a tile's `.tp-prev` carries: its kind, direction and layer order. */
export function previewClass(p: TransitionPreview): string {
  const parts = ['tp-prev', `kind-${p.kind}`]
  if (p.dir) parts.push(`dir-${p.dir}`)
  if (A_ON_TOP.has(p.kind)) parts.push('a-top')
  return parts.join(' ')
}
