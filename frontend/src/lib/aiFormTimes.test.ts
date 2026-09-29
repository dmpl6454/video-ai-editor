import { describe, expect, it } from 'vitest'
import type { ToolSchema } from '../api'
import type { EDL } from '../types'
import { AI_CATALOG } from './aiCatalog'
import { layoutTimeArgs } from './aiFormTimes'
import { buildArgs, fieldsFor, initialValues, type FormContext } from './schemaForm'
import { renderSpanOf } from './timelineLayout'

const schema = (name: string, properties: ToolSchema['input_schema']['properties'], required: string[] = []): ToolSchema => ({
  name, description: '', cancellable: false, reports_progress: false,
  input_schema: { type: 'object', properties, required },
})
const SCHEMAS: Record<string, ToolSchema> = {
  cut_range: schema('cut_range', {
    track: { type: 'string' }, start: { type: 'number' }, end: { type: 'number' },
    dry_run: { type: 'boolean', default: false },
  }, ['track', 'start', 'end']),
  add_lower_third: schema('add_lower_third', {
    name: { type: 'string' }, handle: { type: 'string' }, start: { type: 'number' }, end: { type: 'number' },
  }, ['name', 'start']),
  add_super_text: schema('add_super_text', {
    text: { type: 'string' }, start: { type: 'number' }, end: { type: 'number' },
  }, ['text', 'start', 'end']),
  tts_voiceover: schema('tts_voiceover', {
    text: { type: 'string' }, start: { type: 'number', default: 0 },
  }, ['text']),
}
const entry = (tool: string) => AI_CATALOG.find((e) => e.tool === tool)!
const ctx = (over: Partial<FormContext>): FormContext => ({
  playhead: 0, inMark: null, outMark: null, captionTargetPref: null, captionSpeedPref: null, ...over,
})

/** What AiToolCard.submit dispatches for `tool` with the form as seeded. */
function submitted(tool: string, c: FormContext, e: EDL, extra: Record<string, unknown> = {}) {
  const fields = fieldsFor(SCHEMAS[tool], entry(tool))
  const built = buildArgs(fields, { ...initialValues(fields, c), ...extra })
  expect(built.errors).toEqual({})
  return layoutTimeArgs(fields, built.args, e)
}

/** 25 fps. v1: the host clip split at 10 s with a 0.48 s (12-frame) fade. */
function podcast(): EDL {
  return {
    version: 1, duration: 21.52, canvas: { w: 1080, h: 1080, fps: 25 },
    tracks: [{
      id: 'v1', type: 'video', z: 0,
      transitions: [{ at: 10, type: 'fade', duration: 0.48 }],
      clips: [
        { id: 'a', track: 'v1', src: 'host.mp4', in: 0, out: 10, start: 0 },
        { id: 'b', track: 'v1', src: 'host.mp4', in: 10, out: 22, start: 10 },
      ],
    }],
  } as unknown as EDL
}

/** 30 fps slideshow: four 3 s stills, three 0.5 s (15-frame) dissolves. */
function slideshow(): EDL {
  return {
    version: 1, duration: 10.5, canvas: { w: 1080, h: 1920, fps: 30 },
    tracks: [{
      id: 'v1', type: 'video', z: 0,
      transitions: [3, 6, 9].map((at) => ({ at, type: 'dissolve', duration: 0.5 })),
      clips: [0, 3, 6, 9].map((s, i) => ({ id: `s${i}`, track: 'v1', src: `p${i}.png`, in: 0, out: 3, start: s })),
    }, { id: 'text', type: 'text', z: 10, clips: [] }],
  } as unknown as EDL
}

describe('AI form time fields are ruler times, dispatched as layout times', () => {
  it('Cut range from In 14:00 / Out 15:00 after a 0.48 s fade cuts the marked span', () => {
    const args = submitted('cut_range', ctx({ inMark: 14, outMark: 15 }), podcast())
    // Clip b starts at layout 10 and plays from ruler 9.52, so ruler 14 is
    // its source 14.48: the cut must be layout 14.48–15.48, not 14–15.
    expect(args).toMatchObject({ track: 'v1' })
    expect(args.start).toBeCloseTo(14.48, 6)
    expect(args.end).toBeCloseTo(15.48, 6)
  })

  it('v1 decodes a mark inside a crossfade into clip B, like Split at playhead', () => {
    // The fade plays at ruler 9.52–10.0; 9.76 is 0.24 s into clip b's head.
    const args = submitted('cut_range', ctx({ inMark: 9.76, outMark: 12 }), podcast())
    expect(args.start).toBeCloseTo(10.24, 6)
    // v2 is an overlay lane: the same mark snaps to the seam.
    const v2 = submitted('cut_range', ctx({ inMark: 9.76, outMark: 12 }), podcast(), { track: 'v2' })
    expect(v2.start).toBeCloseTo(10, 6)
  })

  it('Lower third from the playhead at 06:00 shows at Inspector Start 06:00', () => {
    const e = slideshow()
    const args = submitted('add_lower_third', ctx({ playhead: 6 }), e,
      { name: 'Open house Sunday', end: 8 })
    const span = renderSpanOf(e, 'text',
      { id: 'lt', track: 'text', text: 'x', start: args.start as number, end: args.end as number } as never)
    expect(span.start).toBeCloseTo(6, 6)
    expect(span.end).toBeCloseTo(8, 6)
  })

  it('Super text and AI voiceover start at the playhead the user sees', () => {
    const e = slideshow()
    const sup = submitted('add_super_text', ctx({ playhead: 6 }), e, { text: 'HI' })
    expect(sup.start).toBeCloseTo(7, 6)                      // two 0.5 s dissolves before ruler 6
    expect(sup.end).toBeCloseTo(10.5, 6)                     // playhead + 3 = ruler 9 → past the third
    const vo = submitted('tts_voiceover', ctx({ playhead: 6 }), e, { text: 'hello' })
    expect(vo.start).toBeCloseTo(7, 6)                       // = voCapture.voLayoutStart(e, 6)
  })

  it('leaves times alone without transitions', () => {
    const e = { ...podcast(), tracks: [{ ...podcast().tracks[0], transitions: [] }] } as unknown as EDL
    const args = submitted('cut_range', ctx({ inMark: 14, outMark: 15 }), e)
    expect(args).toMatchObject({ start: 14, end: 15 })
  })
})
