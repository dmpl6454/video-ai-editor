import { useEffect, useState } from 'react'
import { chordLabel, useKeymapStore } from '../keymap/engine'
import { PRESETS } from '../keymap/presets'

let _setOpen: ((v: boolean) => void) | null = null
export function openHelp() { _setOpen?.(true) }

// Rows tagged with a `cmd` read their keys from the LIVE keymap (active preset
// + user overrides) at render time, so the modal can't advertise a binding
// that isn't real for the current preset (it used to hardcode "⌘B · S" while
// no preset bound S, and Premiere splits with ⌘K). The static `keys` on those
// rows is never shown.
const SHORTCUTS: { keys: string; label: string; cmd?: string }[] = [
  { keys: 'Space',                label: 'Play / pause' },
  { keys: 'J  ·  K  ·  L',        label: 'Shuttle reverse / pause / forward' },
  { keys: ',  ·  .',              label: 'Step 1 frame back / forward' },
  { keys: '←  ·  →',              label: 'Step 1 frame (Shift = 1 second)' },
  { keys: '⌘B',                   label: 'Split clip at playhead', cmd: 'split' },
  { keys: 'Backspace',            label: 'Delete selected clip(s) (ripple)' },
  { keys: '⌘D',                   label: 'Duplicate selected clip(s)' },
  { keys: 'Shift-click clip',     label: 'Add to multi-selection' },
  { keys: '[  ·  ]',              label: 'Set in / out marks (range)' },
  { keys: 'M',                    label: 'Add marker at playhead' },
  { keys: 'Esc',                  label: 'Clear selection + marks' },
  { keys: '⌘Z  ·  ⌘⇧Z',           label: 'Undo / redo' },
  { keys: '⌘+scroll',             label: 'Zoom timeline' },
  { keys: 'Right-click clip',     label: 'Context menu (split / mute / lock / delete)' },
  { keys: '?',                    label: 'Toggle this help' },
]

export function Help() {
  const [open, setOpen] = useState(false)
  // Live keymap inputs for the `cmd`-tagged rows. Subscribed (not getState())
  // so a preset switch re-renders an already-open modal too.
  const presetId = useKeymapStore((s) => s.presetId)
  const overrides = useKeymapStore((s) => s.overrides)
  // expose a handle so the topbar's ? button can open us
  useEffect(() => {
    _setOpen = setOpen
    return () => { _setOpen = null }
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      // `?` is shift+/ on US layouts; accept either
      if (e.key === '?' || (e.shiftKey && e.code === 'Slash')) {
        e.preventDefault()
        setOpen((o) => !o)
      } else if (e.code === 'Escape') {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (!open) return null
  // Per-command override replaces the preset's chords wholesale — the same
  // merge rule as the engine's effectiveMap(). An unbound command shows "—"
  // rather than falling back to a key that wouldn't work.
  const rows = SHORTCUTS.map((s) => {
    if (!s.cmd) return s
    const chords = overrides[s.cmd] ?? PRESETS[presetId].map[s.cmd] ?? []
    return { ...s, keys: chords.map(chordLabel).join('  ·  ') || '—' }
  })
  return (
    <div
      onClick={() => setOpen(false)}
      style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-1)', border: '1px solid var(--line)',
          borderRadius: 10, padding: 24, width: 'min(540px, 92vw)', maxHeight: '80vh', overflow: 'auto',
          boxShadow: '0 20px 60px rgba(0,0,0,0.6)',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <h2 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Keyboard shortcuts</h2>
          <button onClick={() => setOpen(false)}>Close</button>
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <tbody>
            {rows.map((s, i) => (
              <tr key={s.keys + i} style={{ borderBottom: '1px solid var(--line)' }}>
                <td style={{ padding: '8px 0', width: 200 }}>
                  <span className="kbd" style={{ fontSize: 11 }}>{s.keys}</span>
                </td>
                <td style={{ padding: '8px 0', color: 'var(--text-dim)' }}>{s.label}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ marginTop: 16, fontSize: 11, color: 'var(--text-dim)' }}>
          Tip: drag clips from the Media bin onto the timeline. Drag clips between
          tracks to move them. Drag clip edges to trim. Edges and the playhead
          snap when within 8 px.
        </div>
      </div>
    </div>
  )
}
