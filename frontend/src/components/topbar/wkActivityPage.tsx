// WKWebView page for tests/wk/test_wk_activity.py (bundled by esbuild per run;
// the app never imports it). It mounts the REAL top-bar ActivityChip, ToolRail
// and ToolPanel (so the real VoRecorder, CaptionsPanel and lib/captionRun) over
// a stubbed `fetch` and a stand-in for desktop.py's native voiceover bridge —
// the packaged app's record path — then drives LEFT_RAIL_SPEC §8.3 case 4 in
// the product's engine and posts what it measured:
//   - the chip's polite region exists before anything runs;
//   - a take started in the Audio panel stays stoppable from the chip with the
//     Media panel selected AND the tool panel collapsed (the hidden panel keeps
//     VoRecorder mounted), and the chip's Stop reaches the recorder's stop;
//   - Fastest asks before downloading, sends nothing on Cancel, and focus
//     returns to "Generate captions" (WebKit's focus fix-up differs);
//   - Cancel from the chip shows "Stopping…" until the job acknowledges, and
//     the live region spoke state changes only.
import { createRoot } from 'react-dom/client'
import { useStore } from '../../store'
import { useLayoutStore } from '../../lib/layoutStore'
import { useActivityStore } from '../../lib/activityStore'
import { ActivityChip } from './ActivityChip'
import { ToolRail } from '../rail/ToolRail'
import { ToolPanel } from '../rail/ToolPanel'

const params = new URLSearchParams(location.search)
const token = params.get('token') ?? ''
const post = (body: unknown) => fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })

// ---- the stubbed server --------------------------------------------------

// One clip on v1: captions have footage to transcribe.
const EDL = { version: 3, duration: 4, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
              tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/a.mp4', in: 0, out: 4, start: 0 }] }] }
const calls = { vo: [] as string[], dispatch: [] as string[], cancels: 0 }
let cancelAt = 0
const ACK_MS = 1200
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/__result/')) return realFetch(input, init)
  if (url.endsWith('/api/downloads')) {
    return json({ downloads: {
      'captions:large-v3': { what: 'the accurate caption model', bytes: 3.1e9, cached: true },
      'captions:large-v3-turbo': { what: 'the fast caption model', bytes: 1.6e9, cached: false } } })
  }
  if (url.includes('/dispatch')) {
    const body = JSON.parse(String(init?.body ?? '{}')) as { tool?: string }
    calls.dispatch.push(body.tool ?? '')
    return json({ job_id: 'job-cc', status: 'running', status_url: '/api/jobs/job-cc' })
  }
  if (url.endsWith('/api/jobs/job-cc/cancel')) { calls.cancels++; cancelAt = performance.now(); return json({ id: 'job-cc', status: 'running' }) }
  if (url.endsWith('/api/jobs/job-cc')) {
    const done = cancelAt > 0 && performance.now() - cancelAt > ACK_MS
    return json({ id: 'job-cc', kind: 'dispatch', status: done ? 'cancelled' : 'running', progress: 0.42, result: null, error: null })
  }
  // What the other (hidden) panels ask for on mount: empty, well-formed answers.
  if (url.endsWith('/api/features')) {
    return json({ packaged_app: false, python: '3.13', anthropic_key_set: false, available: [], unavailable: [], summary: '' })
  }
  if (url.endsWith('/media')) return json({ media: [] })
  if (url.endsWith('/sessions/S/edl')) return json(EDL)
  if (url.endsWith('/sessions/S')) return json({ id: 'S', name: 'S', ops: [], summary: {} })
  return json({})
}
;(window as unknown as { pywebview: unknown }).pywebview = { api: {
  vo_start: async () => { calls.vo.push('start'); return { ok: true } },
  vo_stop: async () => { calls.vo.push('stop'); return { ok: true, clip_id: null } },
} }

// ---- the app state the panels read ---------------------------------------

useStore.setState({ sessionId: 'S', edl: EDL as never })
useLayoutStore.setState({ leftTab: 'media', leftOpen: true })

const live: string[] = []
useActivityStore.subscribe((s, p) => { if (s.liveMessage !== p.liveMessage) live.push(s.liveMessage) })

document.body.innerHTML = '<div id="root"></div>'
createRoot(document.getElementById('root')!).render(
  <div className="app">
    <header className="topbar"><ActivityChip /></header>
    <ToolRail />
    <ToolPanel />
  </div>,
)

// ---- helpers --------------------------------------------------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
async function until<T>(fn: () => T | null | undefined | false, what: string, ms = 8000): Promise<T> {
  const t0 = performance.now()
  for (;;) {
    const v = fn()
    if (v) return v
    if (performance.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`)
    await sleep(50)
  }
}
const byLabel = (label: string) => document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
const byText = (root: ParentNode, text: string) =>
  [...root.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === text) ?? null
const shown = (el: Element | null) => !!el && el.getClientRects().length > 0
const panel = () => document.getElementById('tool-panel')!

async function main() {
  const out: Record<string, unknown> = { ua: navigator.userAgent }
  await until(() => document.querySelector('[data-activity]'), 'the chip')
  const region = document.querySelector('[data-activity] [role=status]')
  out.regionAtBoot = { exists: !!region, live: region?.getAttribute('aria-live'), text: region?.textContent }

  // Recording: start in Audio, then Media + collapsed.
  useLayoutStore.getState().showTab('audio')
  await sleep(50)
  byText(document.getElementById('tool-panel-audio')!, 'Record voiceover')!.click()
  const stop = await until(() => byLabel('Stop recording'), 'Stop recording (after the 3-2-1)', 9000)
  useLayoutStore.getState().showTab('media')
  useLayoutStore.getState().setLeftOpen(false)
  await sleep(100)
  out.recording = {
    panelHidden: panel().hidden && !shown(panel()),
    recorderStillMounted: !!document.querySelector('#tool-panel-audio .vo-btn.is-recording'),
    stopShown: shown(stop),
    audioDot: document.getElementById('rail-tab-audio')?.getAttribute('aria-describedby'),
    bodyLabel: document.querySelector('.act-rec .act-body')?.getAttribute('aria-label'),
  }
  stop.click()
  await until(() => !byLabel('Stop recording') && calls.vo.length === 2, 'the take to stop')
  out.recordingStopped = { vo: [...calls.vo], audioDot: document.getElementById('rail-tab-audio')?.getAttribute('aria-describedby') ?? null }

  // Captions: the consent gate for Fastest.
  useLayoutStore.getState().showTab('captions')
  await sleep(300)
  const cc = document.getElementById('tool-panel-captions')!
  cc.querySelector<HTMLInputElement>('input[value="fast"]')!.click()
  await sleep(50)
  const generate = byText(cc, 'Generate captions')!
  generate.focus()
  generate.click()
  const dialog = await until(() => document.querySelector('[role=dialog][aria-labelledby="cc-consent-title"]'), 'the consent dialog')
  out.consent = {
    text: dialog.querySelector('.dialog-text')?.textContent,
    focusInside: dialog.contains(document.activeElement),
    badge: document.getElementById('cc-speed-fast-dl')?.textContent,
  }
  byText(dialog, 'Cancel')!.click()
  await until(() => !document.querySelector('[role=dialog][aria-labelledby="cc-consent-title"]'), 'the dialog to close')
  await sleep(100)
  ;(out.consent as Record<string, unknown>).focusBack = document.activeElement?.textContent?.trim()
  ;(out.consent as Record<string, unknown>).dispatched = [...calls.dispatch]

  // Captions: run, cancel from the chip with the panel collapsed.
  cc.querySelector<HTMLInputElement>('input[value="quality"]')!.click()
  await sleep(50)
  generate.click()
  const cancel = await until(() => byLabel('Cancel captions'), 'Cancel captions')
  await until(() => document.querySelector('.act-cc .act-body')?.textContent?.includes('42%'), '42 %')
  useLayoutStore.getState().showTab('media')
  useLayoutStore.getState().setLeftOpen(false)
  await sleep(100)
  out.captions = {
    cancelShown: shown(cancel),
    captionsDot: document.getElementById('rail-tab-captions')?.getAttribute('aria-describedby'),
    chipText: document.querySelector('.act-cc .act-body')?.textContent,
  }
  cancel.click()
  await until(() => document.querySelector('.act-cc .act-body')?.textContent?.includes('Stopping…'), 'Stopping…', 3000)
  ;(out.captions as Record<string, unknown>).stopping = {
    chipText: document.querySelector('.act-cc .act-body')?.textContent,
    cancelDisabled: byLabel('Cancel captions')?.disabled,
    panelText: document.querySelector('#tool-panel-captions .cc-progress')?.textContent,
    cancels: calls.cancels,
  }
  await until(() => !byLabel('Cancel captions'), 'the job to acknowledge', 8000)
  ;(out.captions as Record<string, unknown>).after = {
    captionsDot: document.getElementById('rail-tab-captions')?.getAttribute('aria-describedby') ?? null,
    generateEnabled: !byText(cc, 'Generate captions')!.disabled,
  }
  out.live = live
  out.dispatched = calls.dispatch
  return out
}

const pageErrors: string[] = []
window.addEventListener('error', (e) => pageErrors.push(String(e.message)))
window.addEventListener('unhandledrejection', (e) => pageErrors.push(String(e.reason)))
main().then(post, (e) => post({ fatal: `${(e && e.message) || e}; page errors: ${pageErrors.join(' | ')}\n${(e && e.stack) || ''}` }))
