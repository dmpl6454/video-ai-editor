import { describe, expect, it } from 'vitest'
import { CARD_HOST_SELECTOR, cardFocusSpot, cardMayTakeFocus } from './cardFocus'

// A tiny stand-in for the DOM (vitest runs in node here): a tree of nodes
// with the three members the rule reads — tagName, closest and contains.
interface FakeNode { tagName: string; cls: string[]; parent: FakeNode | null; closest: (s: string) => FakeNode | null; contains: (n: FakeNode) => boolean }
function node(tagName: string, cls: string[], parent: FakeNode | null): FakeNode {
  const n: FakeNode = {
    tagName, cls, parent,
    closest(sel: string) {
      const wanted = sel.split(',').map((s) => s.trim().replace(/^\./, ''))
      for (let x: FakeNode | null = n; x; x = x.parent) if (x.cls.some((c) => wanted.includes(c))) return x
      return null
    },
    contains(other: FakeNode) {
      for (let x: FakeNode | null = other; x; x = x.parent) if (x === n) return true
      return false
    },
  }
  return n
}

function page() {
  const html = node('HTML', [], null)
  const body = node('BODY', [], html)
  const bar = node('FORM', ['prompt-bar'], body)
  const prompt = node('TEXTAREA', [], bar)
  const card = node('SECTION', ['prompt-preview'], bar)
  const apply = node('BUTTON', ['primary'], card)
  const chat = node('DIV', ['chat-pane'], body)
  const chatBox = node('TEXTAREA', [], chat)
  const chatCard = node('DIV', ['clarify'], chat)
  const timecode = node('INPUT', [], body)
  return { html, body, prompt, card, apply, chatBox, chatCard, timecode }
}

const spot = (a: FakeNode | null, c: FakeNode, b: FakeNode) =>
  cardFocusSpot(a as unknown as Element, c as unknown as Element, b as unknown as Element)

describe('cardFocus — a card never takes focus from a field the person is using', () => {
  it('the Playhead timecode field keeps focus when a preview card appears (Enter must not Apply)', () => {
    const p = page()
    expect(spot(p.timecode, p.card, p.body)).toBe('elsewhere')
    expect(cardMayTakeFocus(spot(p.timecode, p.card, p.body))).toBe(false)
  })

  it('takes focus when nothing holds it, or focus is inside its own host', () => {
    const p = page()
    expect(cardMayTakeFocus(spot(null, p.card, p.body))).toBe(true)
    expect(cardMayTakeFocus(spot(p.body, p.card, p.body))).toBe(true)
    expect(cardMayTakeFocus(spot(p.html, p.card, p.body))).toBe(true)
    expect(spot(p.prompt, p.card, p.body)).toBe('inside-host')
    expect(spot(p.apply, p.card, p.body)).toBe('inside-host')
  })

  it('a chat clarify card may take focus from the chat box, not from the Prompt bar', () => {
    const p = page()
    expect(spot(p.chatBox, p.chatCard, p.body)).toBe('inside-host')
    expect(spot(p.prompt, p.chatCard, p.body)).toBe('elsewhere')
  })

  it('names both hosts', () => {
    expect(CARD_HOST_SELECTOR).toContain('.prompt-bar')
    expect(CARD_HOST_SELECTOR).toContain('.chat-pane')
  })
})
