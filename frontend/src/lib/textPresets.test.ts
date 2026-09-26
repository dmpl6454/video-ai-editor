// lib/textPresets: the Text panel's inserts, moved out of the top bar's
// TextTool (LEFT_RAIL_SPEC §2.5). The args each insert dispatches are pinned
// exactly — the panel and (from R4) the ⌥T command must send what the old
// button and popover sent.
import { describe, expect, it } from 'vitest'
import type { EDL } from '../types'
import { TEXT_STYLE_PRESETS } from './textStyles'
import {
  DEFAULT_TEXT_S, TEXT_TEMPLATES, defaultTextArgs, insertDefaultText, insertTextStyle, insertTextTemplate,
  overlaySpan, templateArgs, templateEnabled, type TextInsertDeps,
} from './textPresets'

const v1 = (end: number): EDL => ({
  version: 3, duration: end, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
  tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/a.mp4', in: 0, out: end, start: 0 }] }],
} as unknown as EDL)

function deps(edl: EDL | null, playhead: number, answer: { result: unknown } | null = { result: { id: 't_1' } }) {
  const sent: { tool: string; args: Record<string, unknown> }[] = []
  const selected: unknown[] = []
  const d: TextInsertDeps = {
    state: () => ({ edl, playhead }),
    dispatch: async (tool, args) => { sent.push({ tool, args }); return answer },
    select: async (r) => { selected.push(r) },
  }
  return { d, sent, selected }
}

describe('the templates (TextTool PRESETS, verbatim)', () => {
  it('are the four templates in order, #Hashtag and @Handle needing the field', () => {
    expect(TEXT_TEMPLATES.map((t) => [t.name, t.label, t.needsField])).toEqual([
      ['countdown_3_2_1', '3 · 2 · 1', false],
      ['callout_arrow', 'Callout →', false],
      ['hashtag_chunky', '#Hashtag', true],
      ['watermark_handle', '@Handle', true],
    ])
  })
  it('templateEnabled: a needsField template waits for non-blank text', () => {
    const [countdown, , hashtag] = TEXT_TEMPLATES
    expect(templateEnabled(countdown, '')).toBe(true)
    expect(templateEnabled(hashtag, '')).toBe(false)
    expect(templateEnabled(hashtag, '   ')).toBe(false)
    expect(templateEnabled(hashtag, 'fyp')).toBe(true)
  })
})

describe('where a new overlay goes', () => {
  it('starts at the playhead and runs 3 s', () => {
    expect(overlaySpan(v1(10), 2)).toEqual({ start: 2, end: 2 + DEFAULT_TEXT_S })
  })
  it('never outlives the picture (the lone overlay must not hold the timeline open)', () => {
    const { start, end } = overlaySpan(v1(4), 1.375)
    expect(start).toBe(1.375)
    expect(end).toBeCloseTo(4, 6)
  })
})

describe('the inserts send what the old Text tool sent', () => {
  it('Add text at playhead: add_text "Your text", role super, allow_stack, then selects it', async () => {
    const { d, sent, selected } = deps(v1(10), 1)
    await insertDefaultText(d)
    expect(sent).toEqual([{ tool: 'add_text', args: { text: 'Your text', start: 1, end: 4, role: 'super', allow_stack: true } }])
    expect(selected).toEqual([{ id: 't_1' }])
    expect(defaultTextArgs(0, 3)).toEqual({ text: 'Your text', start: 0, end: 3, role: 'super', allow_stack: true })
  })

  it('a style: ONE add_text with its whole look and the field text', async () => {
    const { d, sent } = deps(v1(10), 0)
    const pop = TEXT_STYLE_PRESETS.find((p) => p.id === 'yellow_pop')!
    await insertTextStyle(d, pop, '  fyp ')
    expect(sent).toEqual([{ tool: 'add_text', args: {
      role: 'hook', color: '#FFD400', anim_in: 'pop', anim_dur: 0.45, text: 'fyp', start: 0, end: 3, allow_stack: true } }])
  })

  it('a template: apply_text_template with the typed value in all three slots', async () => {
    const { d, sent } = deps(v1(10), 5)
    await insertTextTemplate(d, 'hashtag_chunky', ' fyp ')
    expect(sent).toEqual([{ tool: 'apply_text_template', args: {
      name: 'hashtag_chunky', start: 5, end: 8, fields: { text: 'fyp', hashtag: 'fyp', handle: 'fyp' } } }])
    expect(templateArgs('countdown_3_2_1', '', 0, 3)).toEqual({
      name: 'countdown_3_2_1', start: 0, end: 3, fields: { text: '', hashtag: '', handle: '' } })
  })

  it('selects nothing when the dispatch failed (its toast already fired)', async () => {
    const { d, selected } = deps(v1(10), 0, null)
    expect(await insertDefaultText(d)).toBeNull()
    expect(selected).toEqual([])
  })
})
