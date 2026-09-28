import { useEffect, useId, useState } from 'react'
import { chordLabel, useKeymapStore, IS_MAC } from '../keymap/engine'
import { PRESETS } from '../keymap/presets'
import { COMMANDS, CATEGORIES } from '../keymap/commands'
import { gestureRows, helpGroups, type HelpRow } from '../lib/helpShortcuts'
import { openShortcuts } from './ShortcutsSettings'
import { Dialog } from './Dialog'
import { FONT_LICENCE_URL, parseFontLicences, type FontLicences } from '../lib/fontLicences'
import './help.css'
import { Disclosure } from './Disclosure'

let _setOpen: ((v: boolean) => void) | null = null
export function openHelp() { _setOpen?.(true) }

// The list is GENERATED (QA-110, lib/helpShortcuts): every command in the
// registry (keymap/commands.ts), grouped by category, with the chords the
// active preset plus the user's overrides bind — so it can never advertise a
// key that is not real, nor omit one that is. It used to be a hand-written
// table of 17 rows that had already dropped N, ⌘\, Home/End, ⌥←/⌥→, ⌘C/⌘V
// and ⌘A. chordLabel renders platform-correct modifiers. Only the mouse
// gestures (lib/helpShortcuts.gestureRows) are not keymap commands.

/** "Video AI Editor 0.7.2 · build 45d3e15" from GET /api/version. */
function aboutLine(v: { version?: string; build?: string } | null): string | null {
  if (!v?.version) return null
  return `Video AI Editor ${v.version}${v.build ? ` · build ${v.build}` : ''}`
}

function Row({ r }: { r: HelpRow }) {
  return (
    <tr data-help-row={r.id}>
      <td className="help-keys">
        {r.keys.length
          ? r.keys.map((k, i) => <kbd key={k + i} className="kbd">{k}</kbd>)
          : <span className="help-unbound">No key</span>}
      </td>
      <td className="help-label">{r.label}</td>
    </tr>
  )
}

export function Help() {
  const [open, setOpen] = useState(false)
  const [version, setVersion] = useState<{ version?: string; build?: string } | null>(null)
  const titleId = useId()
  useEffect(() => {
    if (!open || version) return
    fetch('/api/version').then((r) => (r.ok ? r.json() : null)).then((d) => { if (d) setVersion(d) })
      .catch((e) => console.warn('[Help] version fetch failed:', e))
  }, [open, version])
  // The bundled fonts' OFL-1.1 notices, from the licence file shipped next to
  // them (public/fonts/OFL.txt; scripts/font_licences.py writes it).
  const [fonts, setFonts] = useState<FontLicences | null>(null)
  const [fontsError, setFontsError] = useState(false)
  useEffect(() => {
    if (!open || fonts) return
    fetch(FONT_LICENCE_URL).then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status}`))))
      .then((t) => { setFonts(parseFontLicences(t)); setFontsError(false) })
      .catch((e) => { console.warn('[Help] font licence fetch failed:', e); setFontsError(true) })
  }, [open, fonts])
  // Subscribed (not getState()) so a preset switch or a rebind re-renders an
  // already-open list too.
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
      // `?` is shift+/ on US layouts; accept either. Escape is the Dialog's.
      if (e.key === '?' || (e.shiftKey && e.code === 'Slash')) {
        e.preventDefault()
        setOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (!open) return null
  const about = aboutLine(version)
  // The engine's own merge rule (effectiveMap): an override replaces the
  // preset's chords for that command wholesale.
  const keymap = { ...PRESETS[presetId].map, ...overrides }
  const groups = helpGroups(COMMANDS, CATEGORIES, keymap, chordLabel)
  return (
    <Dialog open title="Keyboard shortcuts" labelId={titleId} onClose={() => setOpen(false)}
            className="help-dialog"
            footer={<>
              <span className="help-preset">{PRESETS[presetId].label} keys</span>
              <span className="spacer" />
              <button type="button" onClick={() => { setOpen(false); openShortcuts() }}>Change keys…</button>
              <button type="button" onClick={() => setOpen(false)}>Close</button>
            </>}>
      <div className="help-groups">
        {groups.map((g) => (
          <section key={g.title} className="help-group" aria-label={g.title}>
            <h3>{g.title}</h3>
            <table><tbody>{g.rows.map((r) => <Row key={r.id} r={r} />)}</tbody></table>
          </section>
        ))}
        <section className="help-group" aria-label="Mouse">
          <h3>Mouse</h3>
          <table><tbody>{gestureRows(IS_MAC).map((r) => <Row key={r.id} r={r} />)}</tbody></table>
        </section>
      </div>
      <p className="help-tip">
        Drag clips from the Media panel onto the timeline, between tracks to move
        them, and by their edges to trim. Edges, markers and the playhead snap
        together; the magnet above the timeline turns snapping on and off.
      </p>
      {/* Item 26: Safari's Tab skips buttons and menus unless you hold
          Option (or turn on "Press Tab to highlight each item"); the app
          window has no such setting and needs neither. */}
      <p className="help-tip" data-help-tip="safari-tab">
        Using the editor in Safari? Safari&apos;s Tab key skips buttons unless you hold Option: press{' '}
        <kbd className="kbd">{IS_MAC ? '⌥' : 'Alt'}</kbd><kbd className="kbd">Tab</kbd> to move through every
        control, or turn on &ldquo;Press Tab to highlight each item&rdquo; in Safari Settings &rsaquo; Advanced.
        The app window does not need either.
      </p>
      {/* About: the build identity lives here and in the version's tooltip,
          not as developer text in the toolbar (QA-101). */}
      <section className="help-licences" aria-labelledby={`${titleId}-fonts`}>
        <h3 id={`${titleId}-fonts`}>Fonts and licences</h3>
        {fonts ? (
          <>
            <p className="help-licences-lead">
              These typefaces ship with the app and are licensed under the SIL Open Font License 1.1.
            </p>
            <ul className="help-licences-list">
              {fonts.entries.map((e) => (
                <li key={e.family} data-font-licence={e.family}>
                  <b>{e.family}</b> <span className="help-licences-files">{e.files.join(', ')}</span>
                  <div className="help-licences-cr">{e.copyright}</div>
                </li>
              ))}
            </ul>
            <Disclosure className="help-licences-text" summary="SIL Open Font License 1.1 (full text)">
              <pre>{fonts.licence}</pre>
            </Disclosure>
          </>
        ) : (
          <p className="help-licences-lead">{fontsError
            ? 'The font licence file could not be read. Every bundled font is under the SIL Open Font License 1.1.'
            : 'Loading…'}</p>
        )}
      </section>
      {about && <div className="help-about">{about}</div>}
    </Dialog>
  )
}
