// The clarify card rendered to static markup (no DOM library in the tree —
// vitest runs in node, so `react-dom/server` is the render path; effects do
// not run, which is fine for what is asserted here: which buttons exist and
// which one owns Esc).
//
// The defect this guards: the long-run gate ("… Start?") used to render its
// own "Cancel" option AND the card-level "Cancel Esc" — two Cancels side by
// side. When a question offers a run-ending option, that option IS the
// card's cancel: it renders once, carries the Esc hint and shortcut.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { NeedsInput } from '../lib/promptEvents'
import { ClarifyCard } from './ClarifyCard'

interface Btn { attrs: string; text: string }

/** Every <button> in the markup: its attribute string and tag-stripped text. */
function buttons(html: string): Btn[] {
  return [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
    .map((m) => ({ attrs: m[1], text: m[2].replace(/<[^>]+>/g, '') }))
}

const render = (questions: NeedsInput[]) =>
  renderToStaticMarkup(createElement(ClarifyCard, { questions, onSubmit: () => {}, onCancel: () => {} }))

const GO: NeedsInput = { key: 'go', question: 'This will take about 2 minutes (reframe the canvas to 9:16). Start?',
                         kind: 'confirm', required: true,
                         options: [{ value: 'yes', label: 'Start' }, { value: 'no', label: 'Cancel' }] }
const DOWNLOADS: NeedsInput = { key: 'downloads', question: 'First use downloads MADLAD (3 GB). Start the download?',
                                kind: 'confirm', required: true,
                                options: [{ value: 'yes', label: 'Download' }, { value: 'no', label: 'Skip' }] }
const GATE: NeedsInput = { key: 'gate_auto_caption', question: 'auto captions is not available on this machine. Continue without it, or stop?',
                           kind: 'choice', required: true,
                           options: [{ value: 'skip', label: 'Continue without auto captions', hint: 'The rest of the edit still runs' },
                                     { value: 'abort', label: 'Stop', hint: 'Nothing changes; see the AI panel for the fix' }] }
const LANG: NeedsInput = { key: 'target_lang', question: 'Which language for the captions?', kind: 'choice', required: true,
                           default: 'hi', options: [{ value: 'hi', label: 'Hindi' }, { value: 'en', label: 'English' }] }

describe('the long-run gate card', () => {
  const btns = buttons(render([GO]))
  it('has exactly one element whose text starts with "Cancel"', () => {
    expect(btns.filter((b) => b.text.startsWith('Cancel'))).toHaveLength(1)
    expect(btns.map((b) => b.text)).toEqual(['Start↵', 'CancelEsc'])
  })
  it('puts the Esc shortcut on that option and Enter on Start', () => {
    const cancel = btns.find((b) => b.text.startsWith('Cancel'))!
    expect(cancel.attrs).toContain('aria-keyshortcuts="Escape"')
    expect(cancel.attrs).not.toContain('ghost')
    expect(btns[0].attrs).toContain('aria-keyshortcuts="Enter"')
  })
})

describe('a feature gate card', () => {
  it('makes Stop the Esc target and renders no card-level Cancel', () => {
    const btns = buttons(render([GATE]))
    expect(btns.filter((b) => b.text.startsWith('Cancel'))).toHaveLength(0)
    expect(btns.map((b) => b.text)).toEqual(['Why?', 'Continue without auto captions↵', 'StopEsc'])
    expect(btns[2].attrs).toContain('aria-keyshortcuts="Escape"')
  })
})

describe('the download gate card', () => {
  it('keeps the card-level Cancel because Skip continues the run', () => {
    const btns = buttons(render([DOWNLOADS]))
    expect(btns.map((b) => b.text)).toEqual(['Download↵', 'Skip', 'CancelEsc'])
    expect(btns[1].attrs).not.toContain('aria-keyshortcuts')
    expect(btns[2].attrs).toContain('aria-keyshortcuts="Escape"')
  })
})

describe('a multi-question card', () => {
  it('keeps the card-level Cancel when no option is an abort', () => {
    const html = render([LANG])
    const btns = buttons(html)
    expect(btns.filter((b) => b.text.startsWith('Cancel'))).toHaveLength(1)
    expect(btns.find((b) => b.text.startsWith('Cancel'))!.attrs).toContain('aria-keyshortcuts="Escape"')
    expect(html).toContain('Esc drops the question')
  })
  it('drops the card-level Cancel when a chip is an abort, and Esc moves onto that chip', () => {
    const html = render([LANG, GATE])
    const btns = buttons(html)
    expect(btns.filter((b) => b.text.startsWith('Cancel'))).toHaveLength(0)
    const stop = btns.find((b) => b.text.startsWith('Stop'))!
    expect(stop.attrs).toContain('role="radio"')
    expect(stop.attrs).toContain('aria-keyshortcuts="Escape"')
    expect(stop.text).toContain('Esc')
    expect(html).not.toContain('Esc drops the question')
  })
})
