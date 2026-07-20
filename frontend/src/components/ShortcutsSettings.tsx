import { useEffect, useState } from 'react'
import { COMMANDS, CATEGORIES, type Command } from '../keymap/commands'
import {
  useKeymapStore, chordFromEvent, chordLabel, setCaptureMode, IS_MAC,
} from '../keymap/engine'
import { PRESETS, PRESET_IDS, type PresetId } from '../keymap/presets'

let _setOpen: ((v: boolean) => void) | null = null
/** Open the Keyboard Shortcuts settings (from the TopBar / Help). */
export function openShortcuts() { _setOpen?.(true) }

// Chords the host swallows or acts on before/despite the page — a binding on
// these can never fire reliably, so the rebind UI refuses them with a reason
// instead of saving a dead (or destructive) binding. macOS: ⌘Q quits the
// desktop app / Safari and ⌘W closes the window — neither is interceptable
// (menu key-equivalents win); ⌘T/⌘N open tabs/windows in browsers. Windows /
// Linux: Ctrl+W/T/N are browser-reserved (Chrome won't let a page prevent
// them; in the WebView2 app Ctrl+W closes the window) and Alt+F4 closes any
// window at the OS level.
const RESERVED_CHORDS: Record<string, string> = IS_MAC
  ? {
      'Mod+KeyQ': 'quits the app before the page sees it',
      'Mod+KeyW': 'closes the window/tab before the page sees it',
      'Mod+KeyT': 'opens a new tab in browsers',
      'Mod+KeyN': 'opens a new window in browsers',
      'Mod+KeyM': 'minimises the window in browsers',
      'Mod+KeyH': 'hides the app (macOS system shortcut)',
    }
  : {
      'Mod+KeyW': 'closes the window/tab before the page sees it',
      'Mod+KeyT': 'opens a new tab in browsers',
      'Mod+KeyN': 'opens a new window in browsers',
      'Alt+F4': 'closes the window at the OS level',
    }

export function ShortcutsSettings() {
  const [open, setOpen] = useState(false)
  useEffect(() => { _setOpen = setOpen; return () => { _setOpen = null } }, [])

  const presetId = useKeymapStore((s) => s.presetId)
  const overrides = useKeymapStore((s) => s.overrides)
  const setPreset = useKeymapStore((s) => s.setPreset)
  const rebind = useKeymapStore((s) => s.rebind)
  const resetCommand = useKeymapStore((s) => s.resetCommand)
  const resetAll = useKeymapStore((s) => s.resetAll)
  const effective = useKeymapStore((s) => s.effectiveMap)()

  // which command is currently capturing a new chord
  const [capturing, setCapturing] = useState<string | null>(null)
  // why the last capture was refused (reserved chord), shown under the hint
  const [warning, setWarning] = useState<string | null>(null)

  // chord → commandId, to flag conflicts
  const chordOwners: Record<string, string[]> = {}
  for (const c of COMMANDS) {
    for (const ch of (effective[c.id] || [])) {
      (chordOwners[ch] ||= []).push(c.id)
    }
  }

  // Capture the next keypress while rebinding.
  useEffect(() => {
    if (!capturing) return
    setCaptureMode(true)
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault()
      e.stopPropagation()
      if (e.code === 'Escape') { setCapturing(null); return }
      const chord = chordFromEvent(e)
      if (!chord) return  // bare modifier — keep waiting
      const reserved = RESERVED_CHORDS[chord]
      if (reserved) {
        setWarning(`${chordLabel(chord)} can't be bound — it ${reserved}. Press another combo.`)
        return  // keep capturing so the user can try again
      }
      setWarning(null)
      rebind(capturing, [chord])
      setCapturing(null)
    }
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      setCaptureMode(false)
    }
  }, [capturing, rebind])

  // Esc closes the panel (when not capturing)
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.code === 'Escape' && !capturing) setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, capturing])

  if (!open) return null

  return (
    <div
      onClick={() => setOpen(false)}
      style={{
        position: 'fixed', inset: 0, zIndex: 10000,
        background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(3px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(720px, 92vw)', maxHeight: '86vh', overflow: 'auto',
          background: 'var(--bg-2, #15171f)', color: 'var(--text, #eee)',
          border: '1px solid var(--line, #2a2d3a)', borderRadius: 14,
          padding: 20, boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>Keyboard Shortcuts</h2>
          <button onClick={() => setOpen(false)} style={btnStyle}>Close</button>
        </div>

        {/* Preset picker */}
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '14px 0' }}>
          <span style={{ fontSize: 12, color: 'var(--text-dim,#9aa)' }}>Preset:</span>
          {PRESET_IDS.map((id) => (
            <button
              key={id}
              onClick={() => setPreset(id as PresetId)}
              style={{
                ...chipStyle,
                background: presetId === id ? 'var(--accent,#6c8cff)' : 'var(--bg-3,#222)',
                color: presetId === id ? '#fff' : 'inherit',
                fontWeight: presetId === id ? 700 : 400,
              }}
            >
              {PRESETS[id].label}
            </button>
          ))}
          <div style={{ flex: 1 }} />
          {Object.keys(overrides).length > 0 && (
            <button onClick={resetAll} style={btnStyle} title="Discard all custom rebinds">
              Reset all
            </button>
          )}
        </div>

        <div style={{ fontSize: 11, color: 'var(--text-dim,#9aa)', marginBottom: 12 }}>
          Click a shortcut to rebind it, then press the new key combo. Esc cancels.
          {Object.keys(overrides).length > 0 && ' · customised (★)'}
        </div>
        {warning && (
          <div style={{ fontSize: 11, color: '#e0556d', marginBottom: 12 }}>
            {warning}
          </div>
        )}

        {CATEGORIES.map((cat) => {
          const cmds = COMMANDS.filter((c) => c.category === cat)
          if (!cmds.length) return null
          return (
            <div key={cat} style={{ marginBottom: 14 }}>
              <div style={{
                fontSize: 11, textTransform: 'uppercase', letterSpacing: 1,
                color: 'var(--text-dim,#9aa)', margin: '6px 0',
              }}>{cat}</div>
              {cmds.map((c) => (
                <Row
                  key={c.id} cmd={c}
                  chords={effective[c.id] || []}
                  overridden={c.id in overrides}
                  capturing={capturing === c.id}
                  conflict={(effective[c.id] || []).some((ch) => (chordOwners[ch] || []).length > 1)}
                  onCapture={() => setCapturing(c.id)}
                  onReset={() => resetCommand(c.id)}
                />
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Row({ cmd, chords, overridden, capturing, conflict, onCapture, onReset }: {
  cmd: Command; chords: string[]; overridden: boolean; capturing: boolean;
  conflict: boolean; onCapture: () => void; onReset: () => void;
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '5px 0', borderBottom: '1px solid var(--line,#23252f)',
    }}>
      <span style={{ fontSize: 13 }}>
        {cmd.label}
        {overridden && <span title="customised" style={{ color: 'var(--accent,#6c8cff)' }}> ★</span>}
      </span>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        {capturing ? (
          <span style={{ ...kbd, background: 'var(--accent,#6c8cff)', color: '#fff' }}>press keys…</span>
        ) : chords.length ? (
          chords.map((ch, i) => (
            <button key={i} onClick={onCapture}
              title={conflict ? 'Conflicts with another command' : 'Click to rebind'}
              style={{ ...kbd, cursor: 'pointer', border: conflict ? '1px solid #e0556d' : kbd.border }}>
              {chordLabel(ch)}
            </button>
          ))
        ) : (
          <button onClick={onCapture} style={{ ...kbd, cursor: 'pointer', opacity: 0.5 }}>—</button>
        )}
        {overridden && (
          <button onClick={onReset} title="Reset to preset default"
            style={{ ...btnStyle, padding: '1px 6px', fontSize: 11 }}>↺</button>
        )}
      </div>
    </div>
  )
}

const btnStyle: React.CSSProperties = {
  background: 'var(--bg-3,#222)', color: 'inherit', border: '1px solid var(--line,#2a2d3a)',
  borderRadius: 6, padding: '4px 10px', fontSize: 12, cursor: 'pointer',
}
const chipStyle: React.CSSProperties = { ...btnStyle, padding: '4px 12px' }
const kbd: React.CSSProperties = {
  fontFamily: 'ui-monospace, monospace', fontSize: 12, padding: '2px 8px',
  background: 'var(--bg-3,#222)', border: '1px solid var(--line,#2a2d3a)',
  borderRadius: 5, color: 'inherit', minWidth: 24, textAlign: 'center',
}
