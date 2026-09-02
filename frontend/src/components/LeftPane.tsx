import { useRef, useState, type KeyboardEvent } from 'react'
import { MediaBin } from './MediaBin'
import { AiPanel } from './AiPanel'
import './aiPanel.css'

// The left sidebar's two tabs. The strip IS the panel heading — aiPanel.css
// hides MediaBin's own <h2>Media</h2> so the word isn't shown twice — and
// "Media" stays the default so the first paint still carries the label
// tests/test_frontend_smoke.py looks for.
type Tab = 'media' | 'ai'
const TABS: { id: Tab; label: string; title: string }[] = [
  { id: 'media', label: 'Media', title: 'Footage, music, voiceover, stickers and effects' },
  { id: 'ai', label: 'AI', title: 'Auto-edit, captions, clean-up, cutout and search tools' },
]
const STORE_KEY = 'vai.leftTab'

function readTab(): Tab {
  try {
    const v = localStorage.getItem(STORE_KEY)
    if (v === 'ai' || v === 'media') return v
  } catch { /* private mode / storage disabled — Media is the right default */ }
  return 'media'
}

export function LeftPane() {
  const [tab, setTab] = useState<Tab>(readTab)
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ media: null, ai: null })

  const pick = (t: Tab, focus = false) => {
    setTab(t)
    try { localStorage.setItem(STORE_KEY, t) } catch { /* not worth failing over */ }
    if (focus) tabRefs.current[t]?.focus()
  }

  // Roving tabindex: the strip is ONE tab stop; arrows / Home / End move focus
  // and activate (WAI-ARIA "automatic activation" tabs). The keymap engine
  // leaves these keys to a focused button (engine.ts CONTROL_NAV_KEYS).
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === tab)
    const next =
      e.key === 'ArrowRight' ? (i + 1) % TABS.length
      : e.key === 'ArrowLeft' ? (i - 1 + TABS.length) % TABS.length
      : e.key === 'Home' ? 0
      : e.key === 'End' ? TABS.length - 1
      : -1
    if (next < 0) return
    e.preventDefault()
    pick(TABS[next].id, true)
  }

  return (
    <div className="left-pane">
      <div className="left-tabs" role="tablist" aria-label="Left panel" onKeyDown={onKey}>
        {TABS.map((t) => (
          <button
            key={t.id}
            ref={(el) => { tabRefs.current[t.id] = el }}
            type="button"
            role="tab"
            id={`left-tab-${t.id}`}
            className="left-tab"
            aria-selected={tab === t.id}
            aria-controls={`left-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            title={t.title}
            onClick={() => pick(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {/* Both panels stay MOUNTED and the inactive one is `hidden`: MediaBin
          hosts VoRecorder, which owns a live MediaRecorder (or a native
          capture in the packaged app) with the only Stop button — unmounting
          it mid-recording would orphan the recording. Hiding also keeps the
          AI cards' expanded forms and typed values across a tab switch. */}
      <div role="tabpanel" id="left-panel-media" aria-labelledby="left-tab-media" hidden={tab !== 'media'}>
        <MediaBin />
      </div>
      <div role="tabpanel" id="left-panel-ai" aria-labelledby="left-tab-ai" hidden={tab !== 'ai'}>
        {/* `active` is how the hidden-not-unmounted panel learns it left the
            screen: it defers the tools/features fetch until first shown and
            takes its bbox guide rectangles off the preview while hidden. */}
        <AiPanel active={tab === 'ai'} />
      </div>
    </div>
  )
}
