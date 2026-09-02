// Literal input_schema fixtures copied from GET /api/tools (2026-09-03), so a
// change in what the backend advertises fails here before it fails in a form.
import { describe, expect, it } from 'vitest'
import type { ToolSchema } from '../api'
import { AI_CATALOG, type CatalogEntry } from './aiCatalog'
import { buildArgs, fieldsFor, initialValues, reseedContextValues, type Field, type FormContext } from './schemaForm'

const schema = (name: string, properties: ToolSchema['input_schema']['properties'], required: string[] = []): ToolSchema => ({
  name, description: '', cancellable: false, reports_progress: false,
  input_schema: { type: 'object', properties, required },
})

const SCHEMAS: Record<string, ToolSchema> = {
  remove_silences: schema('remove_silences', {
    track: { type: 'string', default: 'v1' },
    threshold_db: { type: 'number', default: -30 },
    min_dur: { type: 'number', default: 0.5, description: 'Minimum silence duration to cut, seconds' },
    keep_pad: { type: 'number', default: 0.1, description: 'Seconds of silence to leave at each edge for breathing room' },
  }),
  auto_caption: schema('auto_caption', {
    style: { type: 'string', enum: ['default', 'ig_chunky', 'word_emphasis'], default: 'ig_chunky' },
    position: { type: 'string', enum: ['bottom', 'center', 'top'], default: 'bottom' },
    target: { type: 'string', enum: ['hi', 'en', 'hinglish', 'es'], description: 'Language of the CAPTIONS' },
    language: { type: 'string', description: 'Force the SPOKEN language' },
    model: { type: 'string', description: 'Override Whisper model' },
    max_chars: { type: 'integer', default: 42 },
    max_cps: { type: 'number', default: 17.0 },
  }),
  object_erase: schema('object_erase', {
    clip_id: { type: 'string' },
    bbox: { type: 'array', items: { type: 'number' }, description: '[x, y, w, h] normalized 0..1' },
    t_start: { type: 'number', default: 0.0 },
    t_end: { type: 'number' },
  }, ['clip_id', 'bbox']),
  name_speakers: schema('name_speakers', {
    mapping: { type: 'object', description: "{SPEAKER_XX: 'Display Name'} pairs" },
  }, ['mapping']),
  set_loudness_target: schema('set_loudness_target', {
    lufs: { type: ['number', 'null'], default: -16.0 },
  }),
  upscale: schema('upscale', {
    clip_id: { type: 'string' },
    factor: { type: 'integer', default: 2, enum: [2, 4] },
  }, ['clip_id']),
  motion_track: schema('motion_track', {
    clip_id: { type: 'string' },
    target_id: { type: 'string' },
    bbox: { type: 'array', items: { type: 'number' }, description: '[x, y, w, h] normalized 0..1 in the source frame' },
    method: { type: 'string', enum: ['mil', 'vit'], default: 'mil' },
    sample_every: { type: 'integer', default: 2 },
  }, ['clip_id', 'target_id', 'bbox']),
  remove_background: schema('remove_background', {
    clip_id: { type: 'string' },
    bg_color: { type: ['string', 'null'], default: '#00FF00' },
  }, ['clip_id']),
  chroma_key: schema('chroma_key', {
    clip_id: { type: 'string' },
    color: { type: ['string', 'null'], default: '#00FF00' },
    similarity: { type: 'number', default: 0.4 },
    smoothness: { type: 'number', default: 0.1 },
    spill_suppress: { type: 'number', default: 0.5 },
  }, ['clip_id']),
  auto_reframe: schema('auto_reframe', {
    ratio: { type: 'string', enum: ['9:16', '16:9', '1:1', '4:5'] },
  }, ['ratio']),
  cut_range: schema('cut_range', {
    track: { type: 'string' }, start: { type: 'number' }, end: { type: 'number' },
    dry_run: { type: 'boolean', default: false },
  }, ['track', 'start', 'end']),
  add_super_text: schema('add_super_text', {
    text: { type: 'string' }, start: { type: 'number' }, end: { type: 'number' },
    role: { type: 'string', enum: ['super', 'hook', 'lower_third', 'label'], default: 'super' },
    upper: { type: 'boolean', default: false }, allow_stack: { type: 'boolean', default: false },
  }, ['text', 'start', 'end']),
  remove_fillers: schema('remove_fillers', {
    words: { type: 'array', items: { type: 'string' } },
    pad: { type: 'number', default: 0.05 },
    track: { type: 'string', default: 'v1' },
  }),
  multicam: schema('multicam', {
    srcs: { type: 'array', items: { type: 'string' } },
    window_s: { type: 'number', default: 2.0 },
    replace_v1: { type: 'boolean', default: true },
  }, ['srcs']),
  find_broll: schema('find_broll', {
    query: { type: 'string' }, bin: { type: 'string' }, top_k: { type: 'integer', default: 8 },
    max_duration: { type: 'number' },
  }, ['query']),
  // Required enums with NO default — the browser shows the first option, so
  // the seeded state must agree with it.
  apply_template: schema('apply_template', {
    name: { type: 'string', enum: ['outfit_breakdown', 'tech_tip', 'explainer'] },
    inputs: { type: 'object', description: 'Template-specific inputs' },
  }, ['name']),
  apply_export_preset: schema('apply_export_preset', {
    name: { type: 'string', enum: ['reels', 'shorts', 'tiktok', 'story', 'ig_feed_1x1', 'ig_feed_4x5', 'youtube_16x9', 'youtube_4k'] },
  }, ['name']),
}

const entry = (tool: string): CatalogEntry => AI_CATALOG.find((e) => e.tool === tool)!
const fields = (tool: string): Field[] => fieldsFor(SCHEMAS[tool], entry(tool))
const field = (tool: string, name: string): Field => {
  const f = fields(tool).find((x) => x.name === name)
  if (!f) throw new Error(`${tool} has no field ${name}`)
  return f
}
const ctx = (over: Partial<FormContext> = {}): FormContext => ({
  playhead: 0, inMark: null, outMark: null, captionTargetPref: null, captionSpeedPref: null, ...over,
})

describe('fieldsFor — widget derivation', () => {
  it('maps schema types to widgets', () => {
    expect(field('remove_silences', 'threshold_db')).toMatchObject({ widget: 'number', default: -30 })
    expect(field('remove_silences', 'threshold_db').integer).toBeUndefined()
    expect(field('auto_caption', 'max_chars')).toMatchObject({ widget: 'number', integer: true, default: 42 })
    expect(field('auto_caption', 'style')).toMatchObject({ widget: 'select', options: ['default', 'ig_chunky', 'word_emphasis'] })
    expect(field('cut_range', 'track')).toMatchObject({ widget: 'select', options: ['v1', 'v2'], required: true })
    expect(field('add_super_text', 'upper')).toMatchObject({ widget: 'checkbox', default: false })
    expect(field('remove_fillers', 'words').widget).toBe('list')
    expect(field('multicam', 'srcs')).toMatchObject({ widget: 'list', required: true })
    expect(field('object_erase', 'bbox')).toMatchObject({ widget: 'bbox', required: true })
    expect(field('name_speakers', 'mapping')).toMatchObject({ widget: 'mapping', required: true })
    expect(field('find_broll', 'bin').widget).toBe('path')
    expect(field('auto_caption', 'language').widget).toBe('text')
  })

  it('normalises ["string","null"] / ["number","null"] to the base type', () => {
    expect(field('remove_background', 'bg_color')).toMatchObject({ widget: 'text', default: '#00FF00', nullable: { label: expect.any(String) } })
    expect(field('chroma_key', 'color')).toMatchObject({ widget: 'text', nullable: expect.any(Object) })
    expect(field('set_loudness_target', 'lufs')).toMatchObject({ widget: 'number', default: -16, nullable: expect.any(Object) })
    // Nullability is an override decision, never inferred from the schema.
    expect(field('chroma_key', 'similarity').nullable).toBeUndefined()
  })

  it('numeric enums stay numeric', () => {
    expect(field('upscale', 'factor')).toMatchObject({ widget: 'select', options: [2, 4], default: 2 })
  })

  it('hides injected clip_id and dry_run, appends handler-only args, honours order', () => {
    expect(fields('upscale').map((f) => f.name)).toEqual(['factor'])
    expect(fields('cut_range').map((f) => f.name)).toEqual(['track', 'start', 'end'])
    const reframe = fields('auto_reframe')
    expect(reframe.map((f) => f.name)).toEqual(['ratio', 'subject_track'])
    expect(reframe[1]).toMatchObject({ widget: 'checkbox', default: true, required: false })
    expect(fields('motion_track').map((f) => f.name)).toEqual(['target_id', 'clip_id', 'bbox', 'method', 'sample_every'])
    expect(field('motion_track', 'target_id')).toMatchObject({ widget: 'clipSelect', clipFilter: 'overlay' })
  })

  it('labels come from the override or a humanised name', () => {
    expect(field('remove_silences', 'threshold_db').label).toBe('Threshold db')
    expect(field('cut_range', 'start').label).toBe('Start (In mark)')
  })
})

describe('initialValues', () => {
  it('seeds times from the marks and playhead', () => {
    expect(initialValues(fields('cut_range'), ctx({ inMark: 1, outMark: 2.5 }))).toMatchObject({ track: 'v1', start: 1, end: 2.5 })
    expect(initialValues(fields('cut_range'), ctx())).toMatchObject({ start: '', end: '' })
    expect(initialValues(fields('add_super_text'), ctx({ playhead: 4.2 }))).toMatchObject({ start: 4.2, end: 7.2, role: 'super', upper: false })
  })

  it('seeds auto_caption from the CC button’s remembered preferences', () => {
    const v = initialValues(fields('auto_caption'), ctx({ captionTargetPref: 'hi', captionSpeedPref: 'large-v3-turbo' }))
    expect(v).toMatchObject({ target: 'hi', model: 'large-v3-turbo', style: 'ig_chunky' })
    expect(initialValues(fields('auto_caption'), ctx())).toMatchObject({ target: '', model: '' })
    // A remembered value that is no longer a choice falls back rather than
    // becoming an invisible <select> value.
    expect(initialValues(fields('auto_caption'), ctx({ captionTargetPref: 'klingon' })).target).toBe('')
  })

  it('uses schema/override defaults and blanks the rest', () => {
    expect(initialValues(fields('remove_silences'), ctx())).toEqual({ track: 'v1', threshold_db: -30, min_dur: 0.5, keep_pad: 0.1 })
    expect(initialValues(fields('object_erase'), ctx())).toMatchObject({ bbox: [0.4, 0.4, 0.2, 0.2], t_start: 0, t_end: '' })
    expect(initialValues(fields('remove_fillers'), ctx())).toMatchObject({ words: '', pad: 0.05 })
  })

  it('seeds a required enum with no default to its first option — what the <select> shows', () => {
    // The real /api/tools shape for apply_template / apply_export_preset: a
    // browser renders the first option, so '' here meant Run failed with
    // "Required" under a control that visibly had a choice.
    const template = initialValues(fields('apply_template'), ctx())
    expect(template.name).toBe('outfit_breakdown')
    expect(buildArgs(fields('apply_template'), template).errors).toEqual({})
    expect(initialValues(fields('apply_export_preset'), ctx()).name).toBe('reels')
    // An OPTIONAL enum with no default still starts blank (the form offers "—").
    expect(initialValues(fields('auto_caption'), ctx()).target).toBe('')
  })
})

describe('reseedContextValues', () => {
  it('re-applies mark / playhead seeds to fields the user has not touched', () => {
    const f = fields('cut_range')
    const stale = initialValues(f, ctx())                       // opened with no marks
    expect(stale).toMatchObject({ start: '', end: '' })
    const fresh = reseedContextValues(f, stale, new Set(), ctx({ inMark: 1, outMark: 2.5 }))
    expect(fresh).toMatchObject({ track: 'v1', start: 1, end: 2.5 })
    // Marks cleared again → back to blank, still following the source.
    expect(reseedContextValues(f, fresh, new Set(), ctx())).toMatchObject({ start: '', end: '' })
  })

  it('leaves edited fields alone and returns the same object when nothing changes', () => {
    const f = fields('add_super_text')
    const v = { ...initialValues(f, ctx({ playhead: 1 })), start: '9.5' }
    const out = reseedContextValues(f, v, new Set(['start']), ctx({ playhead: 4.2 }))
    expect(out).toMatchObject({ start: '9.5', end: 7.2 })      // end followed, start kept
    expect(reseedContextValues(f, out, new Set(['start']), ctx({ playhead: 4.2 }))).toBe(out)
    // Preference-seeded fields are not "live" sources and never get reseeded.
    const cap = initialValues(fields('auto_caption'), ctx({ captionTargetPref: 'hi' }))
    expect(reseedContextValues(fields('auto_caption'), cap, new Set(), ctx())).toBe(cap)
  })
})

describe('buildArgs', () => {
  it('omits blank optionals — never "" or NaN — and coerces numbers', () => {
    const { args, errors } = buildArgs(fields('auto_caption'), {
      style: 'ig_chunky', position: 'bottom', target: '', language: '  ', model: '', max_chars: '40.6', max_cps: '17',
    })
    expect(errors).toEqual({})
    expect(args).toEqual({ style: 'ig_chunky', position: 'bottom', max_chars: 41, max_cps: 17 })
  })

  it('flags a required blank and a non-number', () => {
    expect(buildArgs(fields('cut_range'), { track: 'v1', start: '', end: '' }).errors).toEqual({ start: 'Required', end: 'Required' })
    expect(buildArgs(fields('remove_silences'), { track: 'v1', threshold_db: 'loud' }).errors).toEqual({ threshold_db: 'Must be a number' })
  })

  it('splits lists on commas and newlines', () => {
    expect(buildArgs(fields('remove_fillers'), { words: 'um, uh\nlike ,, ' }).args).toEqual({ words: ['um', 'uh', 'like'] })
    expect(buildArgs(fields('remove_fillers'), { words: '' }).args).toEqual({})
    expect(buildArgs(fields('multicam'), { srcs: '' }).errors).toEqual({ srcs: 'Required' })
  })

  it('validates a bbox the way dispatch.py::_norm_bbox will', () => {
    const f = fields('object_erase')
    expect(buildArgs(f, { bbox: ['0.1', '0.2', '0.3', '0.4'] }).args).toEqual({ bbox: [0.1, 0.2, 0.3, 0.4] })
    expect(buildArgs(f, { bbox: [1.2, 0.2, 0.3, 0.4] }).errors.bbox).toMatch(/0\.\.1/)
    expect(buildArgs(f, { bbox: [0.1, 0.2, 0.3] }).errors.bbox).toMatch(/four numbers/)
    expect(buildArgs(f, { bbox: [0.8, 0.2, 0.3, 0.4] }).errors.bbox).toMatch(/off the frame/)
    expect(buildArgs(f, { bbox: ['', '', '', ''] }).errors).toEqual({ bbox: 'Required' })
  })

  it('parses KEY=VALUE mappings and rejects a bare line', () => {
    const f = fields('name_speakers')
    expect(buildArgs(f, { mapping: 'SPEAKER_00=Host\n SPEAKER_01 = Guest ' }).args)
      .toEqual({ mapping: { SPEAKER_00: 'Host', SPEAKER_01: 'Guest' } })
    expect(buildArgs(f, { mapping: 'Host' }).errors.mapping).toMatch(/KEY=VALUE/)
    expect(buildArgs(f, { mapping: '' }).errors).toEqual({ mapping: 'Required' })
  })

  it('sends null for a nullable field switched off', () => {
    expect(buildArgs(fields('set_loudness_target'), { lufs: null }).args).toEqual({ lufs: null })
    expect(buildArgs(fields('set_loudness_target'), { lufs: '-14' }).args).toEqual({ lufs: -14 })
    expect(buildArgs(fields('remove_background'), { bg_color: null }).args).toEqual({ bg_color: null })
  })

  it('keeps a numeric select numeric and a string select a string', () => {
    expect(buildArgs(fields('upscale'), { factor: '4' }).args).toEqual({ factor: 4 })
    expect(buildArgs(fields('auto_reframe'), { ratio: '9:16', subject_track: false }).args).toEqual({ ratio: '9:16', subject_track: false })
  })

  it('checks schema min/max when present', () => {
    const f: Field[] = [{ name: 'strength', label: 'Strength', widget: 'number', required: false, default: 0.85, min: 0, max: 1 }]
    expect(buildArgs(f, { strength: '1.5' }).errors).toEqual({ strength: 'Maximum 1' })
    expect(buildArgs(f, { strength: '0.5' }).args).toEqual({ strength: 0.5 })
  })
})
