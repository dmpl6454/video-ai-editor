// QA-101: History / chat / run-log labels speak editor language. The summaries
// below are VERBATIM from a live backend's ops log (dispatch.py strings).
import { describe, expect, it } from 'vitest'
import { cleanSummary, editorSummary, opLabel, toolTitle } from './opLabels'
import { _resetTransitionCatalogCache, loadTransitionCatalog } from './transitionCatalog'

const ID = /\b[a-z]{1,3}_[0-9a-f]{6,}\b/

describe('op labels (QA-101)', () => {
  const real = [
    ['add_clip', 'Add clip c_12cab508 to v1 (0.00–85.00)'],
    ['add_text', "Added text None 'OVER FC30' (1.00–3.00s)"],
    ['set_clip_transform', 'Transform c_12cab508: rotation=0 scale=1.2 x=0 y=0 opacity=1'],
    ['add_effect', 'Add effect vignette → c_12cab508'],
    ['color_grade', "Color grade 1 clip(s): {'brightness': 0.1}"],
    ['ripple_delete', 'Delete c_12cab508 (ripple)'],
    ['split_at', 'Split at 5.00s on v1 (1 clip(s) split)'],
  ] as const

  it('never shows a tool id, clip id, None or a Python dict', () => {
    for (const [tool, summary] of real) {
      const l = opLabel({ tool, summary })
      const shown = `${l.title} ${l.detail}`
      expect(shown, tool).not.toMatch(ID)
      expect(shown, tool).not.toContain(tool)
      expect(shown, tool).not.toMatch(/\bNone\b|[{}]|\(s\)|=/)
    }
  })

  it('reads like an editor', () => {
    expect(opLabel({ tool: 'split_at', summary: real[6][1] })).toMatchObject({ title: 'Split', detail: 'at 5.00s on V1 (1 clip split)' })
    expect(opLabel({ tool: 'add_text', summary: real[1][1] }).detail).toBe("Added text 'OVER FC30' (1.00–3.00s)")
    expect(cleanSummary(real[2][1])).toBe('Transform: rotation 0 scale 1.2 x 0 y 0 opacity 1')
    expect(cleanSummary(real[4][1])).toBe('Color grade 1 clip: brightness 0.1')
    expect(cleanSummary(real[5][1])).toBe('Delete (ripple)')
  })

  it('keeps the raw text for the hover title (bug reports)', () => {
    expect(opLabel({ tool: 'ripple_delete', summary: real[5][1] }).raw).toBe('ripple_delete — Delete c_12cab508 (ripple)')
  })

  it('titles unknown tools readably', () => {
    expect(toolTitle('some_new_tool')).toBe('Some new tool')
    expect(toolTitle('auto_reframe')).toBe('Reframe')
    expect(toolTitle('set_clip_reverse')).toBe('Reverse')   // QA-037 prompt tool
  })
})

describe('History in project terms (QA-101 remainder)', () => {
  const ctx = {
    fps: 30,
    names: new Map([['/w/s_1/uploads/audio/narration_en_92b62330.wav', 'narration (final).wav']]),
    tracks: [{ id: 'v1', type: 'video', z: 1, clips: [] }, { id: 'vo', type: 'vo', z: 0, clips: [] }] as never,
  }
  it('names media by the library, lanes by the timeline, times as timecode', () => {
    const music = opLabel({ tool: 'add_music', summary: 'Add music narration_en_92b62330.wav @ 0.0s, -12dB, ducked' }, ctx)
    expect(music.detail).toBe('Add music narration (final).wav at 00:00:00:00, -12dB, ducked')
    const clip = opLabel({ tool: 'add_clip', summary: 'Add clip c_12cab508 to vo (0.00–8.00)' }, ctx)
    expect(clip.detail).toBe('to Voiceover (00:00:00:00–00:00:08:00)')
    // an unlisted upload still loses its disk suffix
    expect(editorSummary('Add music vo8_49bd5f30.wav @ 2.5s', ctx)).toBe('Add music vo8.wav at 00:00:02:15')
  })
  it('drops the upload suffix from an imported .cube LUT (final QA)', () => {
    // History read "Apply LUT warm_teal_81e3e270.cube (×1.00) to 4 clips".
    expect(editorSummary('Apply LUT warm_teal_81e3e270.cube (×1.00) to 4 clips', ctx))
      .toBe('Apply LUT warm_teal.cube (×1.00) to 4 clips')
  })
  it('an added clip reads as its place on the timeline, not its source range (Final QA r2)', () => {
    const at = (t: number) => opLabel({ tool: 'add_clip', summary: `Add clip c_12cab508 to v1 at ${t.toFixed(2)}s (5.00s long)` }, {
      ...ctx, tracks: [{ id: 'v1', type: 'video', z: 1, clips: [] }] as never,
    }).detail
    const rows = [0, 5, 10, 15].map(at)
    expect(new Set(rows).size).toBe(4)
    expect(rows[1]).toMatch(/^to .*video at 00:00:05:00 \(5.00s long\)$/i)
  })
  it('drops canvas pixel coordinates (QA-101 sweep: "Sticker — 😁 @ (960,594)")', () => {
    // Verbatim add_sticker summary from a live backend.
    const l = opLabel({ tool: 'add_sticker', summary: 'Sticker 😁 @ (960,594) 1.03–4.03s' }, ctx)
    expect(l.detail).not.toMatch(/\(\d+,\d+\)|@/)
    expect(l.detail).toContain('😁')
  })
  it('does not say the title twice', () => {
    expect(opLabel({ tool: 'prompt', summary: 'Prompt: Reframe (2 steps)' }, ctx)).toMatchObject({ title: 'Prompt', detail: 'Reframe (2 steps)' })
  })
})

describe('wave C review copy', () => {
  it('names vocal isolation the way its card does', () => {
    expect(toolTitle('vocal_isolate')).toBe('Isolate vocals')
    expect(toolTitle('instrumental_isolate')).toBe('Isolate instrumental')
  })
  it('pluralises a step count saved before the fix ("(1 steps)")', () => {
    expect(opLabel({ tool: 'prompt', summary: 'Prompt: Mute (1 steps)' }).detail).toBe('Mute (1 step)')
    expect(opLabel({ tool: 'prompt', summary: 'Prompt: Shorts (3 steps)' }).detail).toBe('Shorts (3 steps)')
  })
})

describe('review RD3: split-descendant ids and what they leave behind', () => {
  const ctx = { fps: 30 }
  it('hides a split-descendant id (c_627c3ffb_6b1f58) and the arrow it leaves', () => {
    const l = opLabel({ tool: 'set_speed', summary: 'Speed c_627c3ffb_6b1f58 → 2.00x (now 1.00s on timeline)' }, ctx)
    expect(l).toMatchObject({ title: 'Speed', detail: '2.00x (now 1.00s on timeline)' })
  })
  it('reads a trim\'s in / out as timecode', () => {
    const l = opLabel({ tool: 'trim_clip', summary: 'Trim c_627c3ffb_6b1f58 → in=8.00 out=18.00' }, ctx)
    expect(l.detail).toBe('in 00:00:08:00 out 00:00:18:00')
  })
  it('drops the empty brackets a hidden id leaves', () => {
    const l = opLabel({ tool: 'freeze_frame',
      summary: 'Freeze frame at 3.83s for 2.00s (c_a674b046_51af85_c97bf3_81540d_cf9af5_955e62_88cd77)' }, ctx)
    expect(l.detail).toBe('at 00:00:03:25 for 2.00s')
    expect(cleanSummary('Freeze frame at 3.83s for 2.00s (c_a674b046_51af85)')).toBe('Freeze frame at 3.83s for 2.00s')
  })
})

describe('wave E (F4b) ops read like an editor', () => {
  it('flip and remove-filter titles and details carry no ids or jargon', () => {
    const flip = opLabel({ tool: 'flip_clip', summary: 'Flip c_243538e3_03c462 horizontally' })
    expect(flip.title).toBe('Flip')
    expect(`${flip.title} ${flip.detail}`).not.toMatch(/c_[0-9a-f]{6}|flip_clip/)
    const rm = opLabel({ tool: 'remove_effects', summary: 'Remove the filter from 2 clips' })
    expect(rm.title).toBe('Remove filter')
    expect(`${rm.title} ${rm.detail}`).not.toMatch(/remove_effects|\blut\b|\(s\)/)
  })
})

// Final QA: labels read in History that still leaked internals or broke.
describe('History labels, final sweep', () => {
  it('drops a clip-id clause whole instead of leaving a dangling "from"', () => {
    const l = opLabel({ tool: 'remove_effect', summary: 'Remove effect lut from c_682eda22' })
    expect(`${l.title} — ${l.detail}`).toBe('Remove effect — LUT')
  })

  it('names transitions as the Transitions panel does', () => {
    expect(opLabel({ tool: 'add_transition', summary: 'Add radial transition at 5.00s (0.50s)' }, { fps: 30 }).detail)
      .toBe('Add Clock Wipe transition at 00:00:05:00 (0.50s)')
    expect(opLabel({ tool: 'add_transition', summary: 'Add slideleft transition at 5.00s (0.35s)' }).detail)
      .toBe('Add Slide Left transition at 5.00s (0.35s)')
    expect(opLabel({ tool: 'add_transition', summary: 'Replace fade transition at 4.00s (1.00s)' }).detail)
      .toBe('Replace Fade transition at 4.00s (1.00s)')
    // an alias the backend resolved: the look that renders
    expect(opLabel({ tool: 'add_transition', summary: 'Add wipe → wipeleft transition at 2.00s (0.40s)' }).detail)
      .toBe('Add Wipe Left transition at 2.00s (0.40s)')
  })

  // Final sweep 2: the render base ("custom", "slideleft") named the look the
  // user never picked — History said "Custom" for Glitch, "Slide Left" for Whip.
  it('names the look the user picked, never its render base', async () => {
    _resetTransitionCatalogCache()
    const glitch = 'Add glitch → custom transition at 5.00s (0.30s)'
    const whip = 'Add whip → slideleft transition at 10.00s (0.27s)'
    // before the catalog loads
    expect(cleanSummary(glitch)).toBe('Add Glitch transition at 5.00s (0.30s)')
    expect(cleanSummary(whip)).toBe('Add Whip Pan Left transition at 10.00s (0.27s)')
    expect(cleanSummary('Add spiral → custom transition at 1.00s (0.60s)')).toBe('Add Spiral transition at 1.00s (0.60s)')
    // with the real list_transitions entries (verbatim subset)
    const entries = [
      { name: 'glitch', display: 'Glitch', family: 'Glitch/Stylised', category: 'stylized', default_duration: 0.3, description: '', aliases: [], kind: 'custom' },
      { name: 'whip', display: 'Whip Pan Left', family: 'Glitch/Stylised', category: 'stylized', default_duration: 0.25, description: '', aliases: ['whippan'], kind: 'post' },
      { name: 'slideleft', display: 'Slide Left', family: 'Slide', category: 'slides', default_duration: 0.35, description: '', aliases: ['push', 'slide'], kind: 'native' },
      { name: 'wipeleft', display: 'Wipe Left', family: 'Wipe', category: 'wipes', default_duration: 0.4, description: '', aliases: [], kind: 'native' },
      { name: 'spiral', display: 'Spiral', family: 'Glitch/Stylised', category: 'stylized', default_duration: 0.6, description: '', aliases: ['spin', 'swirl'], kind: 'custom' },
    ]
    await loadTransitionCatalog('s', async () => ({ catalog: { entries } }))
    try {
      expect(cleanSummary(glitch)).toBe('Add Glitch transition at 5.00s (0.30s)')
      expect(cleanSummary(whip)).toBe('Add Whip Pan Left transition at 10.00s (0.27s)')
      expect(cleanSummary('Add whippan → slideleft transition at 1.00s (0.25s)')).toBe('Add Whip Pan Left transition at 1.00s (0.25s)')
      expect(cleanSummary('Add swirl → custom transition at 1.00s (0.60s)')).toBe('Add Spiral transition at 1.00s (0.60s)')
      // a true alias still names the look that renders
      expect(cleanSummary('Add wipe → wipeleft transition at 2.00s (0.40s)')).toBe('Add Wipe Left transition at 2.00s (0.40s)')
      expect(cleanSummary('Add push → slideleft transition at 2.00s (0.35s)')).toBe('Add Slide Left transition at 2.00s (0.35s)')
    } finally {
      _resetTransitionCatalogCache()
    }
  })

  it('names caption styles and models as the Captions form does', () => {
    const l = opLabel({ tool: 'auto_caption',
      summary: 'Auto-captioned (large-v3, en): 11 ig_chunky cues. e.g. “today I am taking a close look”' }, { fps: 30 })
    expect(l.detail).toBe('Auto-captioned (Most accurate, en): 11 cues, Chunky (Instagram) style. e.g. “today I am taking a close look”')
    expect(l.detail).not.toMatch(/_|V3/)
  })

  it('does not say "Text" twice for a text edit', () => {
    const l = opLabel({ tool: 'set_property', summary: 'Text: “Hello there”', args: { path: 'text', value: 'Hello there' } })
    expect(`${l.title} — ${l.detail}`).toBe('Text — “Hello there”')
  })
})
