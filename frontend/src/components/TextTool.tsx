// TopBar "T Text" tool — the CapCut-style one-click text overlay.
//
// The main button dispatches `add_text` at the playhead with a 3s default
// span. Two contract details from agent/dispatch.py's add_text handler:
//   - It returns {summary, id} — we use the id to select + flash the new clip
//     so the Properties inspector opens on it immediately.
//   - By default it REPLACES any same-role text clip on the same track whose
//     [start, end) window overlaps the new one ("double subtitle" guard for
//     chat/MCP callers). A UI insert must never silently delete existing
//     content, so we pass allow_stack: true and let the user manage overlaps.
//
// The adjacent ▾ opens a small presets popover backed by `apply_text_template`
// (same handler file): {name, start, end, fields:{text, hashtag, handle}}.
// Presets that need a field the user hasn't typed are disabled rather than
// producing an empty text clip.

import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useStore } from '../store'

const PRESETS = [
  { name: 'countdown_3_2_1', label: '3 · 2 · 1', title: 'Center-screen countdown (pop in, fade out)', needsField: false },
  { name: 'callout_arrow', label: 'Callout →', title: 'Arrow callout label (uses the text above, or → alone)', needsField: false },
  { name: 'hashtag_chunky', label: '#Hashtag', title: 'Chunky hashtag near the bottom — type the hashtag above first', needsField: true },
  { name: 'watermark_handle', label: '@Handle', title: 'Corner watermark — type your handle above first', needsField: true },
] as const

/** Selects + flashes a freshly added text clip. Refreshes the EDL immediately
    (not the ~120ms debounced refreshSoon dispatch() already queued) so the
    Properties panel can actually find the clip the moment it's selected. */
async function selectNewClip(result: unknown): Promise<void> {
  const id = (result as { id?: string } | null | undefined)?.id
  if (!id) return
  const s = useStore.getState()
  await s.refresh()
  s.setSelection(id)
  s.flashClip(id)
}

export function TextTool() {
  const dispatch = useStore((s) => s.dispatch)
  const [presetsOpen, setPresetsOpen] = useState(false)
  const [presetText, setPresetText] = useState('')
  const btnRef = useRef<HTMLButtonElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)

  // Position + outside-click close — same portal pattern as TopBar's session
  // picker / export popovers (.topbar clips overflow, so an in-flow dropdown
  // would be cut off; and the rect is read in an effect, not mid-render).
  useEffect(() => {
    if (!presetsOpen) return
    const rect = btnRef.current?.getBoundingClientRect()
    if (rect) setPos({ left: rect.left, top: rect.bottom + 4 })
    const close = (e: MouseEvent) => {
      const tgt = e.target as HTMLElement
      if (!tgt.closest('[data-text-presets]')) setPresetsOpen(false)
    }
    setTimeout(() => window.addEventListener('mousedown', close), 0)
    return () => window.removeEventListener('mousedown', close)
  }, [presetsOpen])

  const addDefaultText = async () => {
    const start = useStore.getState().playhead
    const res = await dispatch('add_text', {
      text: 'Your text',
      start,
      end: start + 3,
      role: 'super',
      // Never replace an existing overlay from the UI tool (see header note).
      allow_stack: true,
    })
    if (res) await selectNewClip(res.result)
  }

  const applyPreset = async (name: string) => {
    setPresetsOpen(false)
    const start = useStore.getState().playhead
    const v = presetText.trim()
    const res = await dispatch('apply_text_template', {
      name,
      start,
      end: start + 3,
      // apply_text_template picks the slot it needs per preset; passing the
      // one typed value into all three slots keeps the UI a single field.
      fields: { text: v, hashtag: v, handle: v },
    })
    if (res) await selectNewClip(res.result)
  }

  return (
    <div data-text-presets style={{ position: 'relative', display: 'inline-flex', gap: 2 }}>
      <button
        onClick={() => { void addDefaultText() }}
        title="Add a text overlay at the playhead (edit it in Properties)"
        style={{ fontSize: 11 }}
      >
        <b>T</b> Text
      </button>
      <button
        ref={btnRef}
        onClick={() => setPresetsOpen((o) => !o)}
        title="Text presets"
        style={{ fontSize: 11, padding: '2px 5px' }}
      >
        ▾
      </button>
      {presetsOpen && pos && createPortal(
        <div
          data-text-presets
          style={{
            position: 'fixed',
            left: pos.left,
            top: pos.top,
            zIndex: 1000,
            background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 6,
            boxShadow: '0 8px 24px rgba(0,0,0,0.5)', minWidth: 220,
            padding: 10, display: 'flex', flexDirection: 'column', gap: 8,
          }}
        >
          <label style={{ fontSize: 11, color: 'var(--text-dim)', display: 'flex', flexDirection: 'column', gap: 3 }}>
            Text / #hashtag / @handle
            <input
              value={presetText}
              onChange={(e) => setPresetText(e.target.value)}
              placeholder="e.g. fyp or @myhandle"
              style={{ fontSize: 12, padding: '3px 4px' }}
            />
          </label>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {PRESETS.map((p) => (
              <button
                key={p.name}
                title={p.title}
                disabled={p.needsField && !presetText.trim()}
                onClick={() => { void applyPreset(p.name) }}
                style={{ fontSize: 11 }}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
