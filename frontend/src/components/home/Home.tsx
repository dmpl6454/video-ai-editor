// The Home / projects screen (design handoff §1, brief §2 "Home and project
// navigation", S01): the account card, Home / Templates navigation, the
// gradient "Create project" banner, the project count, search, the grid/list
// switch, Trash and Project sync, and the project cards.
//
// Every card is a REAL project (GET /api/sessions): its name, its poster
// frame (QA-099-THUMBS), its timeline length and the bytes of its imported
// media. Accounts, Pro, cloud sync and Templates-as-a-store are not part of
// this build: the controls are drawn where the reference draws them and SAY
// so (a tooltip and a disabled state), never a dead button.
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api'
import { errorMessage, useStore } from '../../store'
import { toast } from '../../toast'
import { useViewStore } from '../../lib/viewStore'
import { durationPill, editedPhrase, filterProjects, placeholderHue, sizeLabel, type ProjectRow } from '../../lib/projectCards'
import { projectLabel } from '../../lib/projectName'
import { ConfirmDialog } from '../ConfirmDialog'
import { Icon } from '../Icon'
import './home.css'

type Mode = 'grid' | 'list'
const MODE_KEY = 'aive.home.mode'

export function Home() {
  const showEditor = useViewStore((s) => s.showEditor)
  const sid = useStore((s) => s.sessionId)
  const [rows, setRows] = useState<ProjectRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<Mode>(() => {
    try { return localStorage.getItem(MODE_KEY) === 'list' ? 'list' : 'grid' } catch { return 'grid' }
  })
  const [confirmDelete, setConfirmDelete] = useState<ProjectRow | null>(null)
  const [busy, setBusy] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)

  const load = async () => {
    try {
      const r = await api.listSessions()
      setError(null)
      setRows((r.sessions ?? []) as ProjectRow[])
    } catch (e) {
      setRows([])
      setError(errorMessage(e))
    }
  }
  useEffect(() => { const id = window.setTimeout(() => { void load() }, 0); return () => window.clearTimeout(id) }, [])

  const shown = useMemo(() => filterProjects(rows ?? [], query), [rows, query])

  const open = async (id: string) => {
    if (busy) return
    setBusy(true)
    try {
      if (id !== sid) await useStore.getState().openSession(id)
      showEditor()
    } catch (e) {
      toast.error(`Couldn't open the project: ${errorMessage(e)}`)
    } finally { setBusy(false) }
  }
  const create = async () => {
    if (busy) return
    setBusy(true)
    try {
      const r = await api.createSession()
      await useStore.getState().openSession(r.id)
      showEditor()
    } catch (e) {
      toast.error(`Couldn't create a project: ${errorMessage(e)}`)
    } finally { setBusy(false) }
  }
  const remove = async (row: ProjectRow) => {
    setConfirmDelete(null)
    try {
      await api.deleteSession(row.id)
      toast.info(`Deleted “${projectLabel(row.name, row.id)}”.`)
      await load()
    } catch (e) {
      toast.error(`Couldn't delete the project: ${errorMessage(e)}`)
    }
  }
  const setModeKeep = (m: Mode) => { setMode(m); try { localStorage.setItem(MODE_KEY, m) } catch { /* private mode */ } }

  return (
    <div className="home" data-screen="home">
      <aside className="home-side" aria-label="Home navigation">
        <div className="home-brand"><span className="home-brand-mark" aria-hidden="true" /><span>AI Video Editor</span></div>
        <div className="home-account">
          <div className="home-account-row">
            <span className="home-avatar" aria-hidden="true"><Icon name="user" size={18} /></span>
            <div className="home-account-text"><span className="home-account-name">Guest</span><span className="home-account-sub">Not signed in</span></div>
          </div>
          <div className="home-account-actions">
            <button type="button" className="ui-btn-secondary" disabled title="Accounts are not part of this build — projects stay on this computer">Sign in</button>
            <button type="button" className="ui-btn-primary" disabled title="Pro features are not part of this build — every tool here is included">Join Pro</button>
          </div>
        </div>
        <nav className="home-nav">
          <button type="button" className="home-nav-item is-active" aria-current="page"><Icon name="home" /> Home</button>
          <button type="button" className="home-nav-item" onClick={() => { void (async () => { if (!sid) await create(); else showEditor('Templates') })() }}
                  title="Open the editor on the Templates tab">
            <Icon name="grid" /> Templates
          </button>
        </nav>
      </aside>
      <main className="home-main">
        <button type="button" className="home-banner" onClick={() => void create()} disabled={busy}>
          <span className="home-banner-text">
            <span className="home-banner-title">Create project</span>
            <span className="home-banner-sub">Start from empty timeline or drop your media</span>
          </span>
          <span className="home-banner-plus" aria-hidden="true"><Icon name="plus" size={24} /></span>
        </button>
        <div className="home-toolbar">
          <span className="home-h">Projects</span>
          <span className="home-count" aria-label={`${rows?.length ?? 0} projects`}>{rows ? rows.length : '…'}</span>
          <span className="home-grow" />
          <label className="ui-search on-panel home-search">
            <Icon name="search" />
            <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search projects" aria-label="Search projects" />
          </label>
          <div className="home-mode" role="radiogroup" aria-label="View">
            <button type="button" role="radio" aria-checked={mode === 'grid'} className={`home-mode-btn${mode === 'grid' ? ' is-active' : ''}`}
                    aria-label="Grid" onClick={() => setModeKeep('grid')}><Icon name="grid" /></button>
            <button type="button" role="radio" aria-checked={mode === 'list'} className={`home-mode-btn${mode === 'list' ? ' is-active' : ''}`}
                    aria-label="List" onClick={() => setModeKeep('list')}><Icon name="list" /></button>
          </div>
          <button type="button" className="ui-btn-tertiary" disabled
                  title="A recoverable Trash is not part of this build — deleting a project removes it at once, after a confirmation">
            <Icon name="delete" /> Trash
          </button>
          <button type="button" className="ui-btn-tertiary" disabled
                  title="Project sync needs an account; this build keeps every project on this computer">
            <Icon name="cloudOff" /> Project sync · sign in
          </button>
        </div>
        {error && (
          <div className="home-error" role="alert">
            Couldn't list the projects: {error}
            <button type="button" className="ui-btn-secondary" onClick={() => void load()}>Reload</button>
          </div>
        )}
        {rows && rows.length === 0 && !error && (
          <div className="home-empty">No projects yet. Create one above, or open a .vae file from inside the editor.</div>
        )}
        {rows && rows.length > 0 && shown.length === 0 && (
          <div className="home-empty">No project matches “{query}”.</div>
        )}
        <div className={`home-cards${mode === 'list' ? ' is-list' : ''}`}>
          {shown.map((p) => (
            <div key={p.id} className="home-card" data-project-card>
              <button type="button" className="home-card-open" onClick={() => void open(p.id)} disabled={busy}
                      aria-label={`Open ${projectLabel(p.name, p.id)}`}>
                <span className="home-thumb" style={{ background: `linear-gradient(135deg, hsl(${placeholderHue(p.id)} 35% 22%), hsl(${(placeholderHue(p.id) + 40) % 360} 30% 12%))` }}>
                  {p.poster && <img src={p.poster} alt="" loading="lazy" draggable={false} onError={(e) => { e.currentTarget.style.display = 'none' }} />}
                  {durationPill(p.duration) && <span className="home-dur">{durationPill(p.duration)}</span>}
                </span>
                <span className="home-card-text">
                  <span className="home-card-name">{projectLabel(p.name, p.id)}{p.id === sid && <span className="home-card-current"> · open</span>}</span>
                  <span className="home-card-meta">{sizeLabel(p.size_bytes)} · {editedPhrase(p.modified_at)}</span>
                </span>
              </button>
              <button type="button" className="ui-icon-btn is-small home-card-delete" aria-label={`Delete ${projectLabel(p.name, p.id)}`}
                      title="Delete this project" onClick={() => setConfirmDelete(p)}>
                <Icon name="delete" />
              </button>
            </div>
          ))}
        </div>
      </main>
      {confirmDelete && (
        <ConfirmDialog
          title={`Delete “${projectLabel(confirmDelete.name, confirmDelete.id)}”?`}
          body="This removes the project, its imported media and its edit history from this computer. It can’t be undone."
          confirmLabel="Delete project"
          danger
          onConfirm={() => void remove(confirmDelete)}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </div>
  )
}
