import { useEffect, useState } from 'react'
import { COMMANDS, CATEGORIES, type Command } from '../keymap/commands'
import {
  useKeymapStore, chordFromEvent, chordLabel, setCaptureMode, IS_MAC,
} from '../keymap/engine'
import { PRESETS, PRESET_IDS, type PresetId } from '../keymap/presets'
import { Dialog } from './Dialog'
import { Icon } from './Icon'
import './shortcutsSettings.css'

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

  if (!open) return null

  // One dialog (COHERENCE, wave C review): this was a fourth hand-rolled
  // modal — no role=dialog, no focus trap, the editor behind it reachable
  // by Tab, a text "Close" and inline colours. <Dialog> gives it the X,
  // focus-in, the Tab trap, Escape, the inert editor and focus restore.
  // While a chord is being captured, the capture listener above (window,
  // capture phase) stops Escape before the dialog sees it, so Escape
  // cancels the capture rather than closing the dialog.
  const customised = Object.keys(overrides).length > 0
  return (
    <Dialog open title="Keyboard shortcuts" labelId="shortcuts-dialog-title" className="shortcuts-dialog"
      onClose={() => { setCapturing(null); setOpen(false) }}>
      <div className="shortcuts-presets">
        <span className="shortcuts-label" id="shortcuts-preset-label">Preset</span>
        <div className="export-dialog-seg" role="radiogroup" aria-labelledby="shortcuts-preset-label">
          {PRESET_IDS.map((id) => (
            <button key={id} type="button" role="radio" aria-checked={presetId === id}
              onClick={() => setPreset(id as PresetId)}>
              {PRESETS[id].label}
            </button>
          ))}
        </div>
        <span className="spacer" />
        {customised && (
          <button type="button" onClick={resetAll} title="Discard all custom rebinds">Reset all</button>
        )}
      </div>

      <p className="shortcuts-help">
        Click a shortcut to rebind it, then press the new key combo. Esc cancels.
        {customised && <> · customised (<Icon name="custom" label="star" />)</>}
      </p>
      {warning && <p role="alert" className="shortcuts-warning">{warning}</p>}

      {CATEGORIES.map((cat) => {
        const cmds = COMMANDS.filter((c) => c.category === cat)
        if (!cmds.length) return null
        return (
          <section key={cat} className="shortcuts-group" aria-label={cat}>
            <div className="section-label">{cat}</div>
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
          </section>
        )
      })}
    </Dialog>
  )
}

function Row({ cmd, chords, overridden, capturing, conflict, onCapture, onReset }: {
  cmd: Command; chords: string[]; overridden: boolean; capturing: boolean;
  conflict: boolean; onCapture: () => void; onReset: () => void;
}) {
  return (
    <div className="shortcuts-row">
      <span className="shortcuts-name">
        {cmd.label}
        {overridden && <span className="shortcuts-custom" title="customised"><Icon name="custom" label="customised" /></span>}
      </span>
      <div className="shortcuts-keys">
        {capturing ? (
          <span className="shortcuts-cap capturing">press keys…</span>
        ) : chords.length ? (
          chords.map((ch, i) => (
            <button key={i} type="button" onClick={onCapture} data-keycap
              className={`shortcuts-cap${conflict ? ' conflict' : ''}`}
              title={conflict ? 'Conflicts with another command' : 'Click to rebind'}
              aria-label={`${cmd.label}: ${chordLabel(ch)}. Rebind`}>
              {chordLabel(ch)}
            </button>
          ))
        ) : (
          <button type="button" onClick={onCapture} className="shortcuts-cap empty"
            aria-label={`${cmd.label}: no shortcut. Set one`}><span aria-hidden="true">—</span></button>
        )}
        {overridden && (
          <button type="button" onClick={onReset} title="Reset to preset default"
            aria-label={`Reset ${cmd.label} to the preset default`} className="icon-btn shortcuts-reset">
            <Icon name="reset" />
          </button>
        )}
      </div>
    </div>
  )
}
