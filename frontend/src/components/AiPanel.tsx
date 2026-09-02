import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { ASYNC_DISPATCH_TOOLS, errorMessage, useStore } from '../store'
import { toast } from '../toast'
import { AI_CATALOG, filterCatalog, groupCatalog, type CatalogEntry } from '../lib/aiCatalog'
import { useAiRuns } from '../lib/aiRuns'
import { isCancelMessage } from '../lib/dispatchErrors'
import { AiToolCard } from './AiToolCard'
import './aiPanel.css'

// The AI tab: every chat/MCP-only tool as a searchable, grouped card list.
// Schemas come from /api/tools, gates from /api/features (lib/aiRuns.ts owns
// both fetches); the catalog (lib/aiCatalog.ts) decides what gets a card and
// how its form reads. Runs go through store.dispatch() like every other
// gesture, so the op log, undo and the pending-ops indicator all see them.

type Args = Record<string, unknown>

const isRec = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

function summaryOf(entry: CatalogEntry, result: unknown): string {
  const r = isRec(result) ? result : {}
  if (entry.tool.startsWith('export_') && typeof r.path === 'string') return `Wrote ${r.path}`
  return typeof r.summary === 'string' ? r.summary : `${entry.label} done`
}

// import_srt's form holds a File; the handler wants a path. The file goes to
// the session first, then the normal dispatch runs with the returned path so
// the import lands in the op log like any other edit.
async function uploadFileArgs(sid: string, args: Args): Promise<Args> {
  const out: Args = { ...args }
  for (const [k, v] of Object.entries(args)) {
    if (v instanceof File) out[k] = (await api.uploadSubtitle(sid, v)).path
  }
  return out
}

// `active`: whether this panel is the tab on screen. LeftPane keeps it
// mounted and merely hidden, so this prop is the only signal it gets.
export function AiPanel({ active = true }: { active?: boolean }) {
  const tools = useAiRuns((s) => s.tools)
  const features = useAiRuns((s) => s.features)
  const loadError = useAiRuns((s) => s.loadError)
  const featuresError = useAiRuns((s) => s.featuresError)
  const loading = useAiRuns((s) => s.loading)
  const loadCatalog = useAiRuns((s) => s.loadCatalog)
  const setPanelVisible = useAiRuns((s) => s.setPanelVisible)
  const [query, setQuery] = useState('')

  // Fetched the first time the tab is shown, not on app load: the feature
  // probe costs the backend ~2 s of ai.* imports on a cold start, at the same
  // moment the first upload / thumbnail / waveform requests land — not worth
  // paying at every launch for a tab the user may never open. aiRuns' once-
  // per-load guard keeps later tab switches free. The visibility flag is what
  // lets the bbox fields take their guide rectangles off the preview while
  // the panel is hidden (AiToolForm).
  useEffect(() => {
    setPanelVisible(active)
    if (active) void loadCatalog()
  }, [active, loadCatalog, setPanelVisible])

  const toolsByName = useMemo(() => new Map((tools ?? []).map((t) => [t.name, t])), [tools])
  // Only catalog entries this backend actually advertises get a card.
  const groups = useMemo(
    () => groupCatalog(filterCatalog(AI_CATALOG, query).filter((e) => toolsByName.has(e.tool))),
    [query, toolsByName],
  )

  const runTool = useCallback(async (entry: CatalogEntry, args: Args) => {
    const tool = entry.tool
    const runs = useAiRuns.getState()
    const schema = runs.tools?.find((t) => t.name === tool)
    // Cancel / % come from the handler signature via /api/tools — never from
    // the catalog — so the card can't promise what the backend won't deliver.
    runs.setRun(tool, {
      status: 'running', progress: 0, startedAt: Date.now(), cancelling: false,
      reportsProgress: !!schema?.reports_progress, cancellable: !!schema?.cancellable,
    })
    const fail = (message: string) =>
      runs.setRun(tool, { status: 'error', message, cancelled: isCancelMessage(message) })
    const sid = useStore.getState().sessionId
    if (!sid) { fail('No session yet'); return }
    let finalArgs: Args
    try {
      finalArgs = await uploadFileArgs(sid, args)
    } catch (e) {
      // The upload throws the raw envelope like http() does; errorMessage
      // pulls the backend's sentence ("expected a .srt, .vtt or .ass file…")
      // out of api/hardening.py's {error:{details:{message}}} wrapper.
      const message = errorMessage(e)
      fail(message)
      toast.error(message)
      return
    }
    const res = await useStore.getState().dispatch(tool, finalArgs, {
      asJob: ASYNC_DISPATCH_TOOLS.has(tool) || !!entry.runAsJob,
      onProgress: ({ jobId, progress }) => runs.patchRun(tool, { jobId, progress }),
      onError: fail,   // the store already toasted; the card only mirrors it
    })
    if (res) {
      runs.setRun(tool, { status: 'done', result: res.result, at: Date.now() })
      toast.success(summaryOf(entry, res.result))
    } else if (useAiRuns.getState().runs[tool]?.status === 'running') {
      // dispatch() returns null WITHOUT its catch when the session vanished
      // between the check above and the call (store.ts) — nothing fired
      // onError, so name it here rather than spin forever.
      fail('No session yet')
    }
  }, [])

  const status = featuresError
    ? `Feature check failed — ${featuresError}`
    : features?.summary ?? (loading ? 'Checking optional features…' : 'Optional features not checked yet')

  return (
    // data-keymap-ignore: inside the panel a focused checkbox / button keeps
    // Space for itself (keymap/engine.ts) — the generated forms are dense
    // with both, and a keyboard user must be able to toggle and press them.
    <div className="ai-panel" data-keymap-ignore="">
      <div className="ai-panel-head">
        <input
          type="search"
          className="ai-search"
          aria-label="Search AI tools"
          placeholder="Search tools…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Escape') { e.preventDefault(); setQuery('') } }}
        />
        <div className="ai-status" role="status">
          <span className="ai-status-text">{status}</span>
          <button
            type="button"
            className="ai-refresh"
            disabled={loading}
            title="Re-probe which optional features are installed (after a `uv sync`)"
            onClick={() => { void loadCatalog({ refresh: true }) }}
          >
            {loading ? 'Checking…' : 'Refresh'}
          </button>
        </div>
      </div>

      {loadError && (
        <div className="ai-banner" role="alert">
          <b>AI tools unavailable.</b> The backend didn’t answer <code>/api/tools</code>: {loadError}
          <button type="button" onClick={() => { void loadCatalog({ refresh: true }) }}>Retry</button>
        </div>
      )}
      {tools === null && !loadError && <p className="ai-empty">Loading tools…</p>}

      {groups.map((g) => {
        const id = `ai-group-${g.group.replace(/\W+/g, '-').toLowerCase()}`
        return (
          <section key={g.group} className="ai-group" aria-labelledby={id}>
            <h3 id={id}>{g.group}</h3>
            {g.entries.map((e) => (
              <AiToolCard key={e.tool} entry={e} schema={toolsByName.get(e.tool)!} onRun={runTool} />
            ))}
          </section>
        )
      })}
      {tools !== null && groups.length === 0 && (
        <p className="ai-empty">
          {query.trim() ? <>No tools match “{query}”</> : 'This backend advertises none of the catalogued tools.'}
        </p>
      )}
    </div>
  )
}
