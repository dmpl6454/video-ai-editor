// Final QA r3: ⌘Z did nothing right after applying a transition or adding a
// clip from the Media panel. The Transitions panel and every media row were
// `[data-keymap-ignore]` scopes, which drop EVERY 'default' command — undo,
// redo, J/K/L, N — while they only needed their own few keys (the tile grid's
// arrows / Backspace / [ ], a row's Enter). The release notes promise "⌘Z, J,
// K, L and N keep working wherever focus is".
//
// The panels are rendered to static markup (vitest runs in node, no DOM
// library) and the markup is parsed into a small element tree, so `shouldRun`
// reads the REAL attributes and ancestors the components render, not a
// stand-in's.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { KeyTarget } from './engine'

// The built-in list's first family with looks, so the grid has tiles.
vi.stubGlobal('localStorage', {
  getItem: (k: string) => (k === 'vai.transitionsTab' ? 'Glitch/Stylised' : null), setItem: () => {}, removeItem: () => {},
})
const { shouldRun, notePointerFocus } = await import('./engine')
const { COMMAND_BY_ID } = await import('./commands')
const { PRESETS } = await import('./presets')
const { TransitionsPanel } = await import('../components/TransitionsPanel')
const { MediaRow } = await import('../components/MediaBin')

// ---------------------------------------------------------------- markup → tree

interface Node extends KeyTarget {
  tagName: string
  attrs: Record<string, string>
  parent: Node | null
  children: Node[]
}

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'])

function matches(n: Node, sel: string): boolean {
  return sel.split(',').map((s) => s.trim()).some((s) => {
    let m: RegExpMatchArray | null
    if ((m = s.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/))) return m[1] in n.attrs && (m[2] === undefined || n.attrs[m[1]] === m[2])
    if ((m = s.match(/^\.([\w-]+)$/))) return (n.attrs.class ?? '').split(/\s+/).includes(m[1])
    if ((m = s.match(/^#([\w-]+)$/))) return n.attrs.id === m[1]
    if (/^[a-z]+$/i.test(s)) return n.tagName === s.toUpperCase()
    throw new Error(`selector not supported here: ${s}`)
  })
}

function node(tag: string, attrs: Record<string, string>, parent: Node | null): Node {
  const n: Node = {
    tagName: tag.toUpperCase(), attrs, parent, children: [],
    type: attrs.type,
    isContentEditable: attrs.contenteditable === 'true',
    getAttribute: (k: string) => (k in attrs ? attrs[k] : null),
    closest(sel: string) {
      for (let e: Node | null = n; e; e = e.parent) if (matches(e, sel)) return e
      return null
    },
  }
  return n
}

function parse(html: string): Node {
  const root = node('#root', {}, null)
  let cur = root
  const tag = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*(\/?)>/g
  for (let m: RegExpExecArray | null; (m = tag.exec(html));) {
    const [, close, name, rawAttrs, selfClose] = m
    if (close) {
      if (cur.parent) cur = cur.parent
      continue
    }
    const attrs: Record<string, string> = {}
    for (const a of rawAttrs.matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] ?? ''
    const el = node(name, attrs, cur)
    cur.children.push(el)
    if (!selfClose && !VOID.has(name.toLowerCase())) cur = el
  }
  return root
}

function findAll(root: Node, sel: string): Node[] {
  const out: Node[] = []
  const stack = [...root.children]
  while (stack.length) {
    const n = stack.shift()!
    if (matches(n, sel)) out.push(n)
    stack.push(...n.children)
  }
  return out
}

function find(root: Node, sel: string): Node {
  const n = findAll(root, sel)[0]
  if (!n) throw new Error(`no ${sel} in the markup`)
  return n
}

// ---------------------------------------------------------------- the rule

/** Whether `chord` runs its bound command (CapCut preset) with focus on
 *  `t`; an unbound chord as a 'default' command (a user may bind one). */
function runs(chord: string, t: KeyTarget): boolean {
  const binds = PRESETS.capcut.map as Record<string, readonly string[]>
  const id = Object.keys(binds).find((k) => binds[k].includes(chord))
  const cmd = id ? COMMAND_BY_ID[id] : undefined
  return shouldRun(cmd?.scope ?? 'default', chord, t, { alsoInText: cmd?.alsoInText })
}

/** The editor's everywhere-keys (release notes: "wherever focus is"). */
const EVERYWHERE = ['Mod+KeyZ', 'Mod+Shift+KeyZ', 'KeyJ', 'KeyK', 'KeyL', 'KeyN']

describe('the Transitions panel keeps only its own keys', () => {
  const html = renderToStaticMarkup(createElement(TransitionsPanel, { active: true }))
  const root = parse(html)
  const [tile, clickedTile] = findAll(root, '.trp-tile')
  notePointerFocus(clickedTile)

  it('lets ⌘Z, ⇧⌘Z, J, K, L and N through with a tile focused (by the keyboard or a click)', () => {
    for (const t of [tile, clickedTile]) for (const chord of EVERYWHERE) expect(runs(chord, t), chord).toBe(true)
  })
  it('plays on Space after a click on a tile; a keyboard-focused tile keeps Space (it applies)', () => {
    expect(runs('Space', clickedTile)).toBe(true)
    expect(runs('Space', tile)).toBe(false)
  })
  it('keeps the grid keys: arrows, Home / End, Backspace / Delete and [ ]', () => {
    for (const chord of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'Backspace', 'Delete',
      'Shift+Delete', 'BracketLeft', 'BracketRight']) {
      expect(runs(chord, tile), chord).toBe(false)
    }
  })
})

describe('a media row keeps only its own keys', () => {
  const row = {
    id: 'm1', src: '/w/uploads/a/a.mp4', name: 'Beach.mp4', kind: 'video' as const, duration: 8, width: 1920,
    height: 1080, uses: 1, clipIds: ['c1'], missing: false, still: false,
  }
  const noop = () => {}
  const root = parse(renderToStaticMarkup(createElement(MediaRow, {
    row, sid: 's_1', onRemove: noop, onInsert: noop, onRelinked: noop,
  })))
  const rowEl = find(root, '[data-media-row]')
  const add = find(root, '.media-add')
  notePointerFocus(add)

  it('lets ⌘Z, ⇧⌘Z, J, K, L and N through from the row and from its + button after a click', () => {
    for (const t of [rowEl, add]) for (const chord of EVERYWHERE) expect(runs(chord, t), chord).toBe(true)
  })
  it('keeps Enter (it adds the clip) and Delete / Backspace (no ripple delete behind the row)', () => {
    for (const chord of ['Enter', 'Delete', 'Backspace', 'Shift+Delete']) expect(runs(chord, rowEl), chord).toBe(false)
  })
})
