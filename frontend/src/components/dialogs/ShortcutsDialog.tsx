// Shortcuts (design §7; brief §9): tabs Timeline / Player / Basic / Other, a
// profile dropdown (the keymap presets: CapCut, Premiere Pro, Final Cut Pro),
// a two-column list with a key cap per command, Reset · Cancel / Save. Every
// row is a REAL registered command (keymap/commands); clicking a cap captures
// a new chord with conflict detection (keymap/engine), Cancel discards the
// changes made since the dialog opened, Save keeps them.
import { useEffect, useId, useState } from 'react'
import { COMMANDS, type Command } from '../../keymap/commands'
import { chordFromEvent, chordLabel, IS_MAC, setCaptureMode, useKeymapStore } from '../../keymap/engine'
import { PRESETS, PRESET_IDS, type PresetId } from '../../keymap/presets'
import { Dialog } from '../Dialog'
import { Dropdown } from '../ui/Dropdown'
import { Icon } from '../Icon'
import './dialogs.css'

import { registerOpener } from '../../lib/dialogOpeners'

type Tab = 'Timeline' | 'Player' | 'Basic' | 'Other'
const TABS: Tab[] = ['Timeline', 'Player', 'Basic', 'Other']
const TAB_OF: Record<Command['category'], Tab> = {
  Editing: 'Timeline', Marks: 'Timeline', View: 'Timeline', Selection: 'Timeline', Transport: 'Player',
  History: 'Basic', Navigation: 'Basic', Panels: 'Other',
}

const RESERVED: Record<string, string> = IS_MAC
  ? { 'Mod+KeyQ': 'quits the app', 'Mod+KeyW': 'closes the window', 'Mod+KeyT': 'opens a new tab', 'Mod+KeyN': 'opens a new window', 'Mod+KeyM': 'minimises the window', 'Mod+KeyH': 'hides the app' }
  : { 'Mod+KeyW': 'closes the window', 'Mod+KeyT': 'opens a new tab', 'Mod+KeyN': 'opens a new window', 'Alt+F4': 'closes the window' }

export function ShortcutsDialog() {
  const [open, setOpen] = useState(false)
  useEffect(() => { registerOpener('shortcuts', () => setOpen(true)); return () => registerOpener('shortcuts', null) }, [])
  if (!open) return null
  return <ShortcutsForm onClose={() => setOpen(false)} />
}

function ShortcutsForm({ onClose }: { onClose: () => void }) {
  const id = useId()
  const presetId = useKeymapStore((s) => s.presetId)
  const overrides = useKeymapStore((s) => s.overrides)
  const setPreset = useKeymapStore((s) => s.setPreset)
  const rebind = useKeymapStore((s) => s.rebind)
  const resetAll = useKeymapStore((s) => s.resetAll)
  const effective = useKeymapStore((s) => s.effectiveMap)()
  const [tab, setTab] = useState<Tab>('Timeline')
  const [capturing, setCapturing] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  // Cancel restores what was in force when the dialog opened.
  const [snapshot] = useState(() => ({ presetId, overrides: { ...overrides } }))

  const owners: Record<string, string[]> = {}
  for (const c of COMMANDS) for (const ch of effective[c.id] ?? []) (owners[ch] ||= []).push(c.id)

  useEffect(() => {
    if (!capturing) return
    setCaptureMode(true)
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault(); e.stopPropagation()
      if (e.code === 'Escape') { setCapturing(null); return }
      const chord = chordFromEvent(e)
      if (!chord) return
      if (RESERVED[chord]) { setWarning(`${chordLabel(chord)} can't be bound — it ${RESERVED[chord]}.`); return }
      setWarning(null)
      rebind(capturing, [chord])
      setCapturing(null)
    }
    window.addEventListener('keydown', onKey, true)
    return () => { window.removeEventListener('keydown', onKey, true); setCaptureMode(false) }
  }, [capturing, rebind])

  const cancel = () => {
    const st = useKeymapStore.getState()
    st.setPreset(snapshot.presetId as PresetId)
    st.resetAll()
    for (const [cid, chords] of Object.entries(snapshot.overrides)) st.rebind(cid, chords as string[])
    onClose()
  }
  const rows = COMMANDS.filter((c) => TAB_OF[c.category] === tab)
  const half = Math.ceil(rows.length / 2)
  const cols = [rows.slice(0, half), rows.slice(half)]

  return (
    <Dialog open title="Shortcuts" labelId={`${id}-title`} onClose={cancel} className="dlg dlg-shortcuts" showClose={false}
            footer={<><button type="button" className="ui-btn-ghost dlg-btn" onClick={resetAll} title="Back to the profile's defaults">Reset</button><span className="spacer" />
              <button type="button" className="ui-btn-secondary dlg-btn" onClick={cancel}>Cancel</button>
              <button type="button" className="ui-btn-primary dlg-btn" onClick={onClose}>Save</button></>}>
      <div className="dlg-sc-head">
        <div className="dlg-sc-tabs" role="tablist" aria-label="Shortcut groups">
          {TABS.map((t) => <button key={t} type="button" role="tab" aria-selected={tab === t} className={`dlg-sc-tab${tab === t ? ' is-active' : ''}`} onClick={() => setTab(t)}>{t}</button>)}
        </div>
        <Dropdown label="Shortcut profile" value={presetId} width={170} className="is-tall is-bordered"
                  items={PRESET_IDS.map((p) => ({ id: p, label: PRESETS[p as PresetId].label }))}
                  onChange={(p) => setPreset(p as PresetId)}
                  trigger={<>{PRESETS[presetId as PresetId]?.label ?? presetId}<Icon name="chevronDown" /></>} />
      </div>
      {warning && <p role="alert" className="ui-warn" style={{ margin: '0 0 6px' }}>{warning}</p>}
      <div className="dlg-sc-cols">
        {cols.map((col, i) => (
          <div key={i} className="dlg-sc-col">
            {col.map((c) => {
              const chords = effective[c.id] ?? []
              const conflict = chords.some((ch) => (owners[ch] ?? []).length > 1)
              return (
                <div key={c.id} className="dlg-sc-row">
                  <span className="dlg-sc-name">{c.label}{c.id in overrides && <span className="dlg-sc-custom" title="customised"><Icon name="custom" /></span>}</span>
                  {capturing === c.id ? <span className="dlg-cap is-capturing">press keys…</span>
                    : chords.length ? chords.map((ch, k) => (
                      <button key={k} type="button" className={`dlg-cap${conflict ? ' is-conflict' : ''}`} data-keycap onClick={() => setCapturing(c.id)}
                              title={conflict ? 'Conflicts with another command — click to rebind' : 'Click to rebind'} aria-label={`${c.label}: ${chordLabel(ch)}. Rebind`}>{chordLabel(ch)}</button>
                    )) : <button type="button" className="dlg-cap is-empty" onClick={() => setCapturing(c.id)} aria-label={`${c.label}: no shortcut. Set one`}><span aria-hidden="true">—</span></button>}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </Dialog>
  )
}
