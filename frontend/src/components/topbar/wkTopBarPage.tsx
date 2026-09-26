// WKWebView page for tests/wk/test_wk_topbar.py (bundled by esbuild per run
// with the real styles.css; the app never imports it). It mounts the REAL
// TopBar — so the real useTopBarFit, ActivityChip and RatioMenu — over a
// stubbed `fetch`, seeds the worst case of LEFT_RAIL_SPEC §1.4 (a long project
// name, "Applying", a recording and a captions run, both links outdated, a
// long export error), then hosts the bar in boxes 1440, 1280, 1024 and 900 px
// wide and posts what the product's engine laid out at each: the density the
// hook settled on, whether the bar or its left group overflow, where Export,
// Stop recording and Cancel captions sit, and whether one step less would have
// fitted. Text metrics are the engine's own (the system UI font), which is
// exactly what differs between WKWebView, Playwright's WebKit and Chromium.
import { createRoot } from 'react-dom/client'
import { useStore } from '../../store'
import { useActivityStore } from '../../lib/activityStore'
import { TopBar } from '../TopBar'
import '../../styles.css'

const params = new URLSearchParams(location.search)
const token = params.get('token') ?? ''
const post = (body: unknown) => fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })

const LONG_NAME = 'Episode 14 — the part nobody tells you about pricing (final cut v3)'
const EDL = { version: 3, duration: 4, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
              tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/a.mp4', in: 0, out: 4, start: 0 }] }] }

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/__result/')) return realFetch(input, init)
  if (url.endsWith('/save_project')) {
    return json({ path: '/x/S.vae', filename: 'Episode 14.vae', url: '/api/sessions/S/files/exports/S.vae', size: 1 })
  }
  return json({})
}

useStore.setState({
  sessionId: 'S', sessionName: LONG_NAME, edl: EDL as never, edlHash: 'bbbbbbbbbbbbbbb2', pendingOps: 1,
  exportLinks: { S: { sid: 'S', url: '/api/sessions/S/files/exports/export_a.mp4', filename: 'export_a.mp4', edlHash: 'aaaaaaaaaaaaaaa1' } },
  exportError: 'RuntimeError: the encoder ran out of disk space while writing the audio track (free some space and export again)',
})
useActivityStore.setState({
  recording: { startedAt: Date.now() - 12_000, stop: () => {} },
  captions: { progress: 0.42, etaS: 31, elapsedS: 12, cancelling: false, cancel: () => {} },
})

document.body.style.margin = '0'
document.body.innerHTML = '<div id="box" style="width:1440px"><div id="root"></div></div>'
const box = document.getElementById('box')!
createRoot(document.getElementById('root')!).render(<TopBar />)

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const frames = (n: number) => new Promise<void>((r) => {
  const step = (k: number) => (k <= 0 ? r() : requestAnimationFrame(() => step(k - 1)))
  step(n)
})

function measure(width: number) {
  const q = (s: string) => document.querySelector<HTMLElement>(s)!
  const bar = q('.topbar'); const left = q('.tb-left'); const center = q('.tb-center'); const right = q('.tb-right')
  const R = (e: Element) => e.getBoundingClientRect()
  const lb = R(left)
  const btn = (name: string) => [...bar.querySelectorAll('button')].find((b) => b.getAttribute('aria-label') === name)
  const inLeft = (e: Element | undefined) => { if (!e) return null; const b = R(e); return b.left >= lb.left - 0.5 && b.right <= lb.right + 0.5 && b.width > 0 }
  const clipped: string[] = []
  for (const e of bar.querySelectorAll('button, a[href]')) {
    if (!e.getClientRects().length) continue
    const b = R(e)
    const g = e.closest('.tb-left') ? lb : { left: 0, right: width }
    if (b.left < g.left - 0.5 || b.right > g.right + 0.5 || b.width < 1) clipped.push(e.getAttribute('aria-label') ?? e.textContent ?? '')
  }
  const density = Number(bar.dataset.density)
  let lessFits: boolean | null = null
  if (density > 0) {
    bar.classList.remove(`tb-d${density}`)
    lessFits = left.scrollWidth <= left.clientWidth && bar.scrollWidth <= bar.clientWidth
    bar.classList.add(`tb-d${density}`)
  }
  const exp = R(q('.topbar-pinned button.primary'))
  const trig = R(q('.ratio-trigger'))
  return {
    width, density, lessFits, barSw: bar.scrollWidth, barCw: bar.clientWidth, leftSw: left.scrollWidth, leftCw: left.clientWidth,
    exportL: exp.left, exportR: exp.right, trigMid: (trig.left + trig.right) / 2,
    fitsHalf: R(right).width <= (width - R(center).width - 24) / 2,
    chipW: Math.round(R(q('.topbar-session')).width),
    stopIn: inLeft(btn('Stop recording')), cancelIn: inLeft(btn('Cancel captions')), clipped,
    actWords: getComputedStyle(q('.act-word')).display,
    h1Display: getComputedStyle(q('h1.topbar-brand')).display,
  }
}

async function main() {
  const out: Record<string, unknown> = { ua: navigator.userAgent }
  for (let i = 0; i < 100 && !document.querySelector('.topbar'); i++) await sleep(20)
  // The .vae link: Save (stubbed), then one more op makes it outdated.
  document.querySelector<HTMLButtonElement>('.tb-right button[aria-label="Save"]')!.click()
  for (let i = 0; i < 100 && !document.querySelector('.tb-right a.tb-dl'); i++) await sleep(20)
  useStore.setState({ ops: [{ tool: 'add_text' }] as never })
  await sleep(50)
  const rows = []
  for (const w of [1440, 1280, 1024, 900, 1440]) {
    box.style.width = `${w}px`
    await frames(4)                       // the observer's frame, then a settled one
    rows.push(measure(w))
  }
  out.rows = rows
  out.names = {
    vae: document.querySelector('.tb-right a.tb-dl')?.getAttribute('aria-label'),
    mp4: [...document.querySelectorAll('.tb-right button.tb-dl')].map((b) => b.getAttribute('aria-label')),
    error: document.querySelector('.topbar-export-error')?.getAttribute('aria-label'),
    ratio: document.querySelector('.ratio-trigger')?.getAttribute('aria-label'),
  }
  await post(out)
}

main().catch((e) => post({ error: String(e && (e as Error).stack || e) }))
