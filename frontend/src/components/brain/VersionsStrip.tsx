// Editor Brain (EB1-F): the Versions strip above History (spec §7.1, §8.6).
//
// Versions are named labels on snapshots the store already wrote
// (`GET …/brain/versions`; lane C's brain/versions.py): "V1 Reel", "V1
// Premium Podcast". The strip is a `radiogroup` whose checked radio is the
// version the live tree IS; Restore on any other restorable version is ONE
// `restore_version` op (a History row, one ⌘Z) through
// `POST …/brain/versions/{id}/restore`. Shown only behind `brain.enabled`
// and only when the project has versions; it re-reads after every op.
//
// Keys: ←/→/Home/End move between the version radios (focus only — a
// restore is an edit, so it takes Enter or Space, never an arrow). A restore
// done from the keyboard keeps the keyboard: the Restore button it was pressed
// on disappears (that version is now the live one), so focus moves to the
// restored version's own radio and the polite live region says
// "Restored V1 Reel" (lib/announce — the app's one announcer).

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { api, type BrainVersionRow } from '../../api'
import { announce } from '../../lib/announce'
import { useBrainEnabled } from '../../lib/brainFlag'
import { useStore } from '../../store'
import { Icon } from '../Icon'
import './brain.css'

export function VersionsStrip() {
  const enabled = useBrainEnabled()
  const sid = useStore((s) => s.sessionId)
  const opCount = useStore((s) => s.ops.length)
  const edlHash = useStore((s) => s.edlHash)
  const refresh = useStore((s) => s.refresh)
  // The rows are keyed by the session they were read for, so a project
  // switch never shows the previous project's versions while the read is
  // in flight (and the effect only ever sets state from the response).
  const [read, setRead] = useState<{ sid: string; rows: BrainVersionRow[] }>({ sid: '', rows: [] })
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const radios = useRef<(HTMLButtonElement | null)[]>([])
  // The version a restore just made current: focus goes to its radio once the strip has re-read.
  const focusAfter = useRef<string | null>(null)

  useEffect(() => {
    if (!enabled || !sid) return
    let alive = true
    api.brainVersions(sid)
      .then((r) => { if (alive) setRead({ sid, rows: Array.isArray(r?.versions) ? r.versions : [] }) })
      .catch(() => { if (alive) setRead({ sid, rows: [] }) })
    return () => { alive = false }
  }, [enabled, sid, opCount, edlHash])

  const rows = useMemo(() => (sid && read.sid === sid ? read.rows : []), [sid, read])
  useEffect(() => {
    const want = focusAfter.current
    if (!want) return
    const at = rows.findIndex((v) => v.id === want && v.current)
    if (at < 0) return
    focusAfter.current = null
    radios.current[at]?.focus()
  }, [rows])
  if (!enabled || !sid || rows.length === 0) return null

  const restore = async (v: BrainVersionRow) => {
    if (v.current || !v.restorable || busy) return
    setBusy(v.id)
    setError(null)
    try {
      await api.brainRestore(sid, v.id)
      focusAfter.current = v.id
      await refresh()
      announce(`Restored ${v.label}`)
    } catch (e) {
      focusAfter.current = null
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return
    const els = radios.current.filter((b): b is HTMLButtonElement => !!b)
    const at = els.indexOf(document.activeElement as HTMLButtonElement)
    if (at < 0) return
    e.preventDefault()
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? els.length - 1
      : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? (at + els.length - 1) % els.length : (at + 1) % els.length
    els[next]?.focus()
  }

  const focusable = rows.findIndex((v) => v.current)
  return (
    <div className="versions-strip" role="radiogroup" aria-label="Versions" data-testid="versions-strip"
         onKeyDown={onKeyDown}>
      <span className="section-label">Versions</span>
      <ul className="versions-list">
        {rows.map((v, i) => (
          <li key={v.id} className={`version-row${v.current ? ' is-current' : ''}${v.restorable ? '' : ' is-gone'}`}>
            <button type="button" role="radio" aria-checked={v.current} className="version-radio"
                    ref={(el) => { radios.current[i] = el }}
                    tabIndex={i === (focusable < 0 ? 0 : focusable) ? 0 : -1}
                    disabled={!v.restorable && !v.current}
                    title={v.restorable ? undefined : 'No longer restorable: its snapshot was pruned'}
                    onClick={() => void restore(v)}>
              <span className="version-dot" aria-hidden="true" />{v.label}
            </button>
            {!v.current && v.restorable && (
              <button type="button" className="ghost version-restore" aria-label={`Restore ${v.label}`}
                      disabled={busy !== null} onClick={() => void restore(v)}>
                <Icon name="undo" /> Restore
              </button>
            )}
            {!v.restorable && <span className="brain-plan-dim">no longer restorable</span>}
          </li>
        ))}
      </ul>
      {error && <p className="versions-error" role="alert">{error}</p>}
    </div>
  )
}
