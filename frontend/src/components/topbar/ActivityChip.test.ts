// The top bar's activity chip rendered to static markup (vitest runs in node;
// react-dom/server, effects do not run). What VoiceOver and the Playwright
// helpers rely on: the always-present polite region, the exact button names
// "Stop recording" / "Cancel captions", and the clocks kept in aria-labels.
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
const { ActivityChip } = await import('./ActivityChip')
const { useActivityStore } = await import('../../lib/activityStore')

type Seedable<T> = { getInitialState(): T }
function seed<T extends object>(store: Seedable<T>, patch: Partial<T>) {
  Object.assign(store.getInitialState(), patch)
}
const html = () => renderToStaticMarkup(createElement(ActivityChip))
const buttons = (m: string) => [...m.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].map((x) => ({
  label: x[1].match(/aria-label="([^"]*)"/)?.[1], disabled: /\sdisabled=""/.test(x[1]),
  text: x[2].replace(/<[^>]+>/g, ''),
}))

beforeEach(() => seed(useActivityStore, { recording: null, captions: null, liveMessage: '' }))

describe('ActivityChip', () => {
  it('idle: only the (empty) polite live region, no buttons', () => {
    const m = html()
    expect(m).toBe('<div class="activity" data-activity="true"><span class="activity-sr-only" role="status" aria-live="polite"></span></div>')
  })

  it('the live region speaks the store\'s throttled message', () => {
    seed(useActivityStore, { liveMessage: 'Captions 50%' })
    expect(html()).toContain('aria-live="polite">Captions 50%</span>')
  })

  it('recording: "● Rec m:ss" opens Audio, and a Stop recording button', () => {
    seed(useActivityStore, { recording: { startedAt: Date.now() - 12_400, stop: () => {} } })
    const b = buttons(html())
    expect(b).toHaveLength(2)
    expect(b[0].label).toMatch(/^Recording voiceover, 0:1[23]\. Open the Audio panel$/)
    expect(b[0].text).toMatch(/^Rec0:1[23]$/)
    expect(b[1]).toMatchObject({ label: 'Stop recording', disabled: false, text: '' })
    expect(html()).toContain('data-icon="stop"')
  })

  it('captions: "Captions 42% · 0:31 left", ETA spoken in the name, and Cancel captions', () => {
    seed(useActivityStore, { captions: { progress: 0.42, etaS: 31, elapsedS: 22, cancelling: false, cancel: () => {} } })
    const b = buttons(html())
    expect(b[0].label).toBe('Captions 42%, about 31 seconds left. Open the Captions panel')
    expect(b[0].text).toBe('Captions42% · 0:31 left')
    expect(b[1]).toMatchObject({ label: 'Cancel captions', disabled: false })
  })

  it('captions before any signal: the elapsed seconds, no % and no ETA', () => {
    seed(useActivityStore, { captions: { progress: null, etaS: null, elapsedS: 5, cancelling: false, cancel: () => {} } })
    const b = buttons(html())
    expect(b[0].label).toBe('Captions. Open the Captions panel')
    expect(b[0].text).toBe('Captions · 5s')
  })

  it('cancelling: "Stopping…", and Cancel captions stays named but disabled', () => {
    seed(useActivityStore, { captions: { progress: 0.42, etaS: null, elapsedS: 40, cancelling: true, cancel: () => {} } })
    const b = buttons(html())
    expect(b[0].text).toBe('CaptionsStopping…')
    expect(b[0].label).toBe('Captions stopping. Open the Captions panel')
    expect(b[1]).toMatchObject({ label: 'Cancel captions', disabled: true })
  })

  it('both at once: recording first, then captions', () => {
    seed(useActivityStore, {
      recording: { startedAt: Date.now(), stop: () => {} },
      captions: { progress: 0.5, etaS: 10, elapsedS: 10, cancelling: false, cancel: () => {} },
    })
    expect(buttons(html()).map((x) => x.label)).toEqual([
      'Recording voiceover, 0:00. Open the Audio panel', 'Stop recording',
      'Captions 50%, about 10 seconds left. Open the Captions panel', 'Cancel captions'])
  })
})
