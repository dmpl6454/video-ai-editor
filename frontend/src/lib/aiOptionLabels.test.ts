import { describe, expect, it } from 'vitest'
import { AI_CATALOG } from './aiCatalog'
import { aiClipLabel, aiOptionLabel } from './aiOptionLabels'
import type { Track } from '../types'

const RAW = /^[a-z0-9_-]+$/

// Wave C review (QA-101): AI forms showed "v1|v2", "default|ig_chunky|
// word_emphasis", "hi|en|hinglish|es", "large-v3-turbo" and "v1 · x @ 1.0s".
describe('AI form choices in editor language', () => {
  it('never shows a raw lower-case enum value, whatever the field', () => {
    for (const [field, v] of [['position', 'top'], ['caption_lang', 'hinglish'], ['whatever', 'some_value']]) {
      expect(aiOptionLabel(field, v)).not.toMatch(RAW)
    }
  })

  it('labels every select option the catalog declares', () => {
    for (const e of AI_CATALOG) {
      for (const [name, f] of Object.entries(e.fields ?? {})) {
        for (const o of (f as { options?: (string | number)[] }).options ?? []) {
          const label = aiOptionLabel(name, o)
          if (typeof o === 'string') expect(label, `${e.tool}.${name}=${o}`).not.toMatch(RAW)
        }
      }
    }
  })

  it.each([
    ['style', 'default', 'Default'], ['style', 'ig_chunky', 'Chunky (Instagram)'],
    ['style', 'word_emphasis', 'Word emphasis'], ['model', 'large-v3-turbo', 'Fast (Turbo)'],
    ['target', 'hi', 'Hindi'], ['target', 'en', 'English'], ['target', 'es', 'Spanish'],
    ['language', 'hinglish', 'Hinglish (Hindi in Latin letters)'], ['target_lang', 'zh', 'Chinese'],
    ['track', 'v1', 'Main video'], ['track', 'v2', 'PIP / overlay video'],
    ['caption_lang', 'hi', 'Hindi'], ['source_lang', 'es', 'Spanish'],
    ['position', 'bottom', 'Bottom'], ['position', 'center', 'Center'], ['anim', 'slide_up', 'Slide up'],
  ])('%s = %s → %s', (field, value, label) => {
    expect(aiOptionLabel(field, value)).toBe(label)
  })

  it('uses the timeline’s own lane names when it has them', () => {
    const tracks = [{ id: 'v1', type: 'video', z: 0, label: 'Interview', clips: [] }] as unknown as Track[]
    expect(aiOptionLabel('track', 'v1', tracks)).toBe('Interview')
  })

  it('names a clip by lane, media name and timecode', () => {
    const t = { id: 'v2', type: 'video', z: 1, label: 'PIP / overlay video', clips: [] } as unknown as Track
    expect(aiClipLabel(t, 'Beach.mov', 1, 30)).toBe('PIP / overlay video · Beach.mov · 00:00:01:00')
  })
})
