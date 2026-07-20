import { useEffect, useState } from 'react'
import { chordLabel, useKeymapStore, IS_MAC } from '../keymap/engine'
import { PRESETS } from '../keymap/presets'

let _setOpen: ((v: boolean) => void) | null = null
export function openHelp() { _setOpen?.(true) }

// Rows tagged with `cmds` read their keys from the LIVE keymap (active preset
// + user overrides) at render time, so the modal can't advertise a binding
// that isn't real for the current preset (it used to hardcode "⌘B · S" while
// no preset bound S, and Premiere splits with ⌘K) — and chordLabel renders
// platform-correct modifiers (⌘/⌥/⇧ on mac, Ctrl/Alt/Shift on Windows), so
// no mac glyph is ever hardcoded here. Rows with a static `keys` are mouse /
// non-keymap gestures only.
const SHORTCUTS: { keys?: string; label: string; cmds?: string[] }[] = [
  { cmds: ['playPause'],          label: 'Play / pause' },
  { cmds: ['shuttleReverse', 'shuttleStop', 'shuttleForward'],
                                  label: 'Shuttle reverse / pause / forward' },
  { cmds: ['frameBack', 'frameForward'],   label: 'Step 1 frame back / forward' },
  { cmds: ['secondBack', 'secondForward'], label: 'Step 1 second back / forward' },
  { cmds: ['split'],              label: 'Split clip at playhead' },
  { cmds: ['rippleDelete'],       label: 'Delete selected clip(s) (ripple)' },
  { cmds: ['duplicate'],          label: 'Duplicate selected clip(s)' },
  { keys: 'Shift-click clip',     label: 'Add to multi-selection' },
  { cmds: ['markIn', 'markOut'],  label: 'Set in / out marks (range)' },
  { cmds: ['addMarker'],          label: 'Add marker at playhead' },
  { cmds: ['zoomIn', 'zoomOut'],  label: 'Zoom timeline in / out' },
  { cmds: ['deselect'],           label: 'Clear selection + marks' },
  { cmds: ['undo', 'redo'],       label: 'Undo / redo' },
  { keys: `${IS_MAC ? '⌘' : 'Ctrl'}+scroll`, label: 'Zoom timeline (wheel)' },
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
      const tgt = e.target as HTMLElement | null
      const tag = tgt?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tgt?.isContentEditable) return
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
  // rather than falling back to a key that wouldn't work. Single-command rows
  // list every chord (e.g. CapCut split "⌘B · S"); multi-command rows list
  // each command's primary chord so the row stays scannable.
  const chordsOf = (cmd: string): string[] =>
    overrides[cmd] ?? PRESETS[presetId].map[cmd] ?? []
  const rows = SHORTCUTS.map((s) => {
    if (!s.cmds) return { label: s.label, keys: s.keys ?? '' }
    const multi = s.cmds.length > 1
    const parts = s.cmds
      .map((c) => (multi ? chordsOf(c).slice(0, 1) : chordsOf(c)).map(chordLabel).join('  ·  '))
      .filter(Boolean)
    return { label: s.label, keys: parts.join('  ·  ') || '—' }
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
