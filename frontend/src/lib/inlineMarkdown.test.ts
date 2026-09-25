import { describe, expect, it } from 'vitest'
import { parseInlineMarkdown } from './inlineMarkdown'

describe('parseInlineMarkdown', () => {
  it('turns the clarify reply bold markers into bold spans, not literal asterisks', () => {
    const spans = parseInlineMarkdown('I did not catch that. Reply **captions**, **tighten** or **auto_edit**.')
    expect(spans.filter((s) => s.kind === 'bold').map((s) => s.text)).toEqual(['captions', 'tighten', 'auto_edit'])
    expect(spans.map((s) => s.text).join('')).toBe('I did not catch that. Reply captions, tighten or auto_edit.')
    expect(spans.some((s) => s.text.includes('**'))).toBe(false)
  })

  it('reads `code` and __bold__ too', () => {
    expect(parseInlineMarkdown('run `remove_silences` on __v1__')).toEqual([
      { kind: 'text', text: 'run ' },
      { kind: 'code', text: 'remove_silences' },
      { kind: 'text', text: ' on ' },
      { kind: 'bold', text: 'v1' },
    ])
  })

  it('leaves unpaired markers and plain text alone', () => {
    expect(parseInlineMarkdown('2 ** 3 is not bold')).toEqual([{ kind: 'text', text: '2 ** 3 is not bold' }])
    expect(parseInlineMarkdown('plain')).toEqual([{ kind: 'text', text: 'plain' }])
    expect(parseInlineMarkdown('')).toEqual([])
  })

  it('never produces markup — a tag stays text', () => {
    const spans = parseInlineMarkdown('**<img src=x onerror=alert(1)>**')
    expect(spans).toEqual([{ kind: 'bold', text: '<img src=x onerror=alert(1)>' }])
  })
})
