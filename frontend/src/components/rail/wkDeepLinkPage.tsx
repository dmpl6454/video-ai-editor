// WKWebView page for tests/wk/test_wk_deeplinks.py (bundled by esbuild per run
// with the real stylesheets; the app never imports it). It mounts the REAL
// ToolRail and ToolPanel — so the real MediaBin, Audio/Text/Captions panels,
// EffectsPanel, AiPanel and AiToolCards — over a stubbed `fetch`, then drives
// every AI deep link of LEFT_RAIL_SPEC §2.5 (R5) in the product's engine and
// posts what it measured:
//   - each row names its catalogue label and mirrors its card's status;
//   - a row (clicked with and without focus on it) selects AI, clears the
//     search, expands THAT card, has it just under the sticky head in the
//     first frame that paints, and gives its toggle focus;
//   - the back chip returns to the origin panel, restores its scroll and
//     focuses the row; group links land on the group heading.
// WebKit's focus fix-up (activeElement left on a hidden element) and its
// sticky-positioning metrics are what differ from Chromium here.
import { createRoot } from 'react-dom/client'
import { useStore } from '../../store'
import { useLayoutStore } from '../../lib/layoutStore'
import { AI_CATALOG } from '../../lib/aiCatalog'
import { ToolRail } from './ToolRail'
import { ToolPanel } from './ToolPanel'
import { DEEP_LINKS, allToolsLabel, catalogEntry, type DeepLinkPanel } from './deepLinks'
import '../../styles.css'

const params = new URLSearchParams(location.search)
const token = params.get('token') ?? ''
const post = (body: unknown) => fetch(`/__result/${token}`, { method: 'POST', body: JSON.stringify(body) })

// ---- the stubbed server --------------------------------------------------

const EDL = { version: 3, duration: 4, canvas: { w: 1080, h: 1920, fps: 30, bg: '#000' },
              tracks: [{ id: 'v1', type: 'video', clips: [{ id: 'c1', src: '/a.mp4', in: 0, out: 4, start: 0 }] }] }
const TOOLS = AI_CATALOG.map((e) => ({
  name: e.tool, description: '', cancellable: false, reports_progress: false,
  input_schema: { type: 'object', properties: {}, required: [] },
}))
const FEATURES = {
  packaged_app: true, python: '3.13', anthropic_key_set: false, summary: '', available: [],
  unavailable: [{ key: 'stems', feature: 'Voice separation', tools: ['vocal_isolate', 'instrumental_isolate'], fix: '' }],
}
const DOWNLOADS = { tts: { what: 'the voiceover voice', bytes: 63_000_000, cached: false } }
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/__result/')) return realFetch(input, init)
  if (url.endsWith('/api/tools')) return json({ tools: TOOLS })
  if (url.includes('/api/features')) return json(FEATURES)
  if (url.endsWith('/api/downloads')) return json({ downloads: DOWNLOADS })
  if (url.endsWith('/media')) return json({ media: [] })
  if (url.endsWith('/sessions/S/edl')) return json(EDL)
  if (url.endsWith('/sessions/S')) return json({ id: 'S', name: 'S', ops: [], summary: {} })
  return json({})
}

useStore.setState({ sessionId: 'S', edl: EDL as never })
useLayoutStore.setState({ leftTab: 'media', leftOpen: true })

document.body.style.margin = '0'
document.body.innerHTML = '<div id="root"></div>'
createRoot(document.getElementById('root')!).render(
  <div style={{ display: 'grid', gridTemplateColumns: '64px 300px', gridTemplateAreas: '"rail left"', height: '560px' }}>
    <ToolRail />
    <ToolPanel />
  </div>,
)

// ---- helpers --------------------------------------------------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const frame = () => new Promise<void>((r) => requestAnimationFrame(() => r()))
async function until<T>(fn: () => T | null | undefined | false, what: string, ms = 8000): Promise<T> {
  const t0 = performance.now()
  for (;;) {
    const v = fn()
    if (v) return v
    if (performance.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`)
    await sleep(50)
  }
}
const $ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document) => root.querySelector<T>(sel)
const row = (panel: string, key: string) =>
  [...document.querySelectorAll<HTMLButtonElement>(`#tool-panel-${panel} [data-deep-link]`)].find((b) => b.dataset.deepLink === key) ?? null
const RAIL_LABEL: Record<DeepLinkPanel, string> = { media: 'Media', audio: 'Audio', text: 'Text', effects: 'Effects', captions: 'Captions' }

/** Where a jump landed, read in the frame it first paints (a rAF queued after
 *  the jump's own re-align runs after it, before the paint). */
function landedNow() {
  const a = document.activeElement as HTMLElement | null
  const scroller = document.getElementById('tool-panel-ai')!
  const head = $('.ai-panel-head', scroller)!
  const card = a?.closest<HTMLElement>('.ai-card') ?? null
  const target = card ?? a
  const t = target?.getBoundingClientRect()
  return {
    tag: a?.tagName ?? null, id: a?.id ?? null, text: a?.textContent?.trim() ?? null,
    controls: a?.getAttribute('aria-controls') ?? null, expanded: a?.getAttribute('aria-expanded') ?? null,
    title: card?.querySelector('.ai-card-title')?.textContent ?? null,
    hasBody: !!card?.querySelector('.ai-card-body'),
    aiSelected: document.getElementById('rail-tab-ai')?.getAttribute('aria-selected'),
    search: $<HTMLInputElement>('.ai-search', scroller)!.value,
    chip: $('.ai-back-chip')?.textContent ?? null,
    gap: t ? t.top - head.getBoundingClientRect().bottom : null,
    atEnd: Math.abs(scroller.scrollTop - (scroller.scrollHeight - scroller.clientHeight)) <= 1,
    badges: card ? [...card.querySelectorAll('.ai-badge')].map((b) => b.textContent) : [],
  }
}
type Landed = ReturnType<typeof landedNow>

async function jump(el: HTMLElement, withFocus: boolean) {
  // A user's focus lands a task (and a rendering update) before their key
  // press; WebKit scrolls a focused element into view in that update, so
  // focus-then-click in one task would leave the reveal pending until the
  // panel shows again.
  if (withFocus) { el.focus(); await frame(); await frame() }
  // The origin panel's scroll at the moment of the jump (focus may have
  // scrolled the row into view): the back chip must bring exactly this back.
  const originScroll = el.closest<HTMLElement>('.tool-tabpanel')?.scrollTop ?? -1
  el.click()
  const firstFrame = await new Promise<Landed>((r) => requestAnimationFrame(() => r(landedNow())))
  await sleep(120)
  return { originScroll, firstFrame, settled: landedNow() }
}

async function back(panel: string, withFocus: boolean) {
  const chip = $<HTMLButtonElement>('.ai-back-chip')
  if (!chip) return { chip: false }
  const name = chip.textContent
  if (withFocus) { chip.focus(); await frame() }
  chip.click()
  const sync = document.getElementById(`tool-panel-${panel}`)!.scrollTop
  await frame()
  const f1 = document.getElementById(`tool-panel-${panel}`)!.scrollTop
  await frame()
  const a = document.activeElement as HTMLElement | null
  return {
    chip: true, name,
    focused: a?.dataset.deepLink ?? a?.id ?? a?.tagName ?? null,
    selected: document.getElementById(`rail-tab-${panel}`)?.getAttribute('aria-selected'),
    chipAfter: !!$('.ai-back-chip'),
    scroll: document.getElementById(`tool-panel-${panel}`)!.scrollTop, sync, f1,
  }
}

async function show(panel: string) {
  useLayoutStore.getState().showTab(panel as never)
  await frame()
  await sleep(60)
}

async function main() {
  const out: Record<string, unknown> = { ua: navigator.userAgent }
  await until(() => $('#tool-panel-media [data-deep-link]'), 'the Media rows')

  // Rows: names, and statuses once the catalogue has loaded (Audio loads it).
  await show('audio')
  await until(() => $('#tool-panel-audio .deep-link-status'), 'the Audio statuses')
  const rows: Record<string, unknown>[] = []
  for (const p of Object.keys(DEEP_LINKS) as DeepLinkPanel[]) {
    for (const b of document.querySelectorAll<HTMLButtonElement>(`#tool-panel-${p} .deep-link`)) {
      const lab = document.getElementById(b.getAttribute('aria-labelledby') ?? '')
      const desc = b.getAttribute('aria-describedby')
      rows.push({ panel: p, key: b.dataset.deepLink, name: lab?.textContent,
                  status: desc ? document.getElementById(desc)?.textContent : null })
    }
  }
  out.rows = rows

  // Case 8: a leftover search, a scrolled Captions panel, the row focused and
  // pressed (a keyboard user's Enter is a click on the focused button).
  await show('ai')
  const search = $<HTMLInputElement>('#tool-panel-ai .ai-search')!
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'zzz no such tool')
  search.dispatchEvent(new Event('input', { bubbles: true }))
  await sleep(50)
  out.searchBefore = search.value
  await show('captions')
  const cc = document.getElementById('tool-panel-captions')!
  cc.scrollTop = cc.scrollHeight
  const ccScroll = cc.scrollTop
  const case8 = await jump(row('captions', 'tool:diarize')!, true)
  out.case8 = { ...case8, ccScroll, back: await back('captions', true) }

  // Every row and group link, alternating focus-then-click and a bare click
  // (WebKit does not focus a button a mouse clicks).
  const all: Record<string, unknown>[] = []
  let withFocus = false
  for (const p of Object.keys(DEEP_LINKS) as DeepLinkPanel[]) {
    const spec = DEEP_LINKS[p]
    const keys = [...spec.tools.map((t) => `tool:${t}`), ...(spec.group ? [`group:${spec.group}`] : [])]
    for (const key of keys) {
      await show(p)
      const el = row(p, key)
      if (!el) { all.push({ panel: p, key, missing: true }); continue }
      withFocus = !withFocus
      const tool = key.startsWith('tool:') ? key.slice(5) : null
      const r = await jump(el, withFocus)
      all.push({
        panel: p, key, withFocus, label: tool ? catalogEntry(tool)?.label : spec.group,
        allLabel: spec.group ? allToolsLabel(spec.group) : null, rowText: el.textContent,
        origin: RAIL_LABEL[p], ...r, back: await back(p, withFocus),
      })
    }
  }
  out.all = all
  await post(out)
}

main().catch((e: unknown) => post({ fatal: String(e instanceof Error ? `${e.message}\n${e.stack}` : e) }))
