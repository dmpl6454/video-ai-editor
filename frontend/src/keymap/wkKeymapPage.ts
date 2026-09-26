// WKWebView page for tests/wk/test_wk_keymap.py (bundled by esbuild per run;
// the app never imports it). It mounts the REAL keymap engine (useKeymap) and
// the real command registry over stand-ins for the controls the commands
// press, so native NSEvent keys delivered by the harness travel exactly the
// app's path: AppKit → WKWebView → WebCore keydown → engine → command.
//
// For every keydown the page records what the engine saw (code, modifiers,
// the chord, the command it resolves to, whether the scope rule lets it run,
// whether it was handled) and the layout store after it. The harness sends
// its steps, then calls window.__keysDone(), which posts the log.
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { chordFromEvent, shouldRun, useKeymap, useKeymapStore } from './engine'
import { COMMAND_BY_ID } from './commands'
import { useLayoutStore } from '../lib/layoutStore'

interface Entry {
  code: string
  key: string
  meta: boolean
  alt: boolean
  shift: boolean
  ctrl: boolean
  target: string
  chord: string
  command: string | null
  runs: boolean
  handled: boolean
  leftTab: string
  leftOpen: boolean
  clicked: string[]
}

// A known start, whatever this origin's storage kept from an earlier run
// (setState does not persist).
useKeymapStore.setState({ presetId: 'capcut', overrides: {} })
useLayoutStore.setState({ leftTab: 'media', leftOpen: true })

const params = new URLSearchParams(location.search)
const token = params.get('token') ?? ''
const log: Entry[] = []
const clicked: string[] = []

document.body.innerHTML = `
  <header class="topbar"><div class="topbar-pinned">
    <button class="primary" aria-haspopup="dialog" id="export">Export</button>
    <button aria-label="Customize keyboard shortcuts" id="customize">K</button>
  </div></header>
  <nav class="rail"><div role="tablist"><button role="tab" aria-selected="true" id="rail-tab-media">Media</button></div></nav>
  <div data-keymap-ignore><div class="item media-row" data-media-row tabindex="0" id="row">clip.mp4</div></div>
  <div data-text-presets><button id="addtext">Add text at playhead</button></div>
  <main class="center"><textarea id="prompt"></textarea><div id="timeline" tabindex="0">timeline</div></main>
  <div id="root"></div>`
for (const id of ['export', 'customize', 'addtext', 'rail-tab-media']) {
  document.getElementById(id)?.addEventListener('click', () => clicked.push(id))
}

// Record in the capture phase BEFORE the engine (registered first); once the
// dispatch is over, defaultPrevented says whether the engine handled it (it
// stops propagation, so no later listener would see the event).
window.addEventListener('keydown', (e) => {
  const chord = chordFromEvent(e)
  const id = useKeymapStore.getState().chordToCommand()[chord] ?? null
  const cmd = id ? COMMAND_BY_ID[id] : undefined
  const entry: Entry = {
    code: e.code, key: e.key, meta: e.metaKey, alt: e.altKey, shift: e.shiftKey, ctrl: e.ctrlKey,
    target: (e.target as HTMLElement | null)?.id || (e.target as HTMLElement | null)?.tagName || '',
    chord, command: cmd ? cmd.id : null,
    runs: !!cmd && shouldRun(cmd.scope, chord, e.target as HTMLElement | null),
    handled: false, leftTab: '', leftOpen: true, clicked: [],
  }
  log.push(entry)
  setTimeout(() => { entry.handled = e.defaultPrevented }, 0)
}, true)
// After the keyup's default action (a focused button's Space click lands there).
window.addEventListener('keyup', () => {
  const last = log[log.length - 1]
  if (!last) return
  setTimeout(() => {
    const s = useLayoutStore.getState()
    last.leftTab = s.leftTab
    last.leftOpen = s.leftOpen
    last.clicked = [...clicked]
  }, 0)
})

function Keymap() {
  useKeymap()
  return null
}
createRoot(document.getElementById('root')!).render(createElement(Keymap))

declare global {
  interface Window { __keysReady?: boolean; __keysDone?: () => void; __focus?: (id: string) => string }
}
window.__focus = (id: string) => {
  document.getElementById(id)?.focus()
  return document.activeElement?.id ?? ''
}
window.__keysDone = () => {
  void fetch(`/__result/${token}`, {
    method: 'POST',
    body: JSON.stringify({ log, ua: navigator.userAgent, platform: navigator.platform }),
  })
}
// After React's first commit, so the engine's listener is installed.
requestAnimationFrame(() => setTimeout(() => { window.__keysReady = true }, 0))
