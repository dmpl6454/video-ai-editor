import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { ASYNC_DISPATCH_TOOLS, errorMessage, useStore } from '../store'
import { toast } from '../toast'
import { AI_CATALOG, filterCatalog, groupCatalog, type CatalogEntry } from '../lib/aiCatalog'
import { useAiRuns } from '../lib/aiRuns'
import { isCancelMessage } from '../lib/dispatchErrors'
import { AiToolCard } from './AiToolCard'
import './aiPanel.css'
import { featureStatus } from '../lib/featureStatus'
import { Disclosure } from './Disclosure'
import { useLayoutStore, type AiJump } from '../lib/layoutStore'
import { AiBackChip } from './rail/DeepLinkRow'
import { aiCardToggleSelector, aiGroupId, catalogEntry, forgetReturn, scrollDeltaToTop } from './rail/deepLinks'

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

/** Scroll the tool panel so `target` sits just under the sticky search head. */
function alignToTop(root: HTMLElement, target: HTMLElement): void {
  const scroller = root.closest<HTMLElement>('.tool-tabpanel')
  const head = root.querySelector<HTMLElement>('.ai-panel-head')
  if (!scroller || !head) return
  // Twice: the first pass may move the head from where it sits unscrolled to
  // where it sticks, and that differs between engines (deepLinks.ts).
  for (let pass = 0; pass < 2; pass++) {
    scroller.scrollTop += scrollDeltaToTop(target.getBoundingClientRect().top, head.getBoundingClientRect().bottom)
  }
}

interface Landing { target: HTMLElement; focus: HTMLElement }

/**
 * Land a deep link (LEFT_RAIL_SPEC §2.5, R5) in the rendered panel: expand the
 * target card through its own toggle, scroll it to the top of the tool panel
 * (just under the sticky search head) and focus the toggle. A group link, or a
 * tool this backend does not advertise, lands on the group's heading; with no
 * group on screen at all, on the search box. Returns null while the target
 * cannot be judged yet (the tool list has not loaded).
 */
function landJump(root: HTMLElement, jump: AiJump, toolsLoaded: boolean): Landing | null {
  const toggle = jump.tool ? root.querySelector<HTMLButtonElement>(aiCardToggleSelector(jump.tool)) : null
  if (jump.tool && !toggle && !toolsLoaded) return null
  const group = jump.group ?? (jump.tool ? catalogEntry(jump.tool)?.group : undefined)
  const heading = group ? document.getElementById(aiGroupId(group)) : null
  const search = root.querySelector<HTMLElement>('.ai-search')
  const target = toggle?.closest<HTMLElement>('.ai-card') ?? heading ?? search
  const focus = toggle ?? heading ?? search
  if (!target || !focus) return null
  const expand = !!toggle && toggle.getAttribute('aria-expanded') !== 'true'
  if (expand) toggle.click()
  alignToTop(root, target)
  focus.focus({ preventScroll: true })
  // The card's form renders in the update the toggle's click queued, after
  // this commit. Near the end of the list the panel cannot scroll far enough
  // until it has, so align once more before the frame paints.
  if (expand) {
    requestAnimationFrame(() => {
      if (document.activeElement === focus && target.isConnected) alignToTop(root, target)
    })
  }
  return { target, focus }
}

// `active`: whether this panel is the tab on screen. ToolPanel keeps it
// mounted and merely hidden, so this prop is the only signal it gets.
export function AiPanel({ active = true }: { active?: boolean }) {
  const tools = useAiRuns((s) => s.tools)
  const features = useAiRuns((s) => s.features)
  const loadError = useAiRuns((s) => s.loadError)
  const featuresError = useAiRuns((s) => s.featuresError)
  const loading = useAiRuns((s) => s.loading)
  const downloads = useAiRuns((s) => s.downloads)
  const loadCatalog = useAiRuns((s) => s.loadCatalog)
  const setPanelVisible = useAiRuns((s) => s.setPanelVisible)
  const [query, setQuery] = useState('')
  const aiJump = useLayoutStore((s) => s.aiJump)
  const leftTab = useLayoutStore((s) => s.leftTab)
  const clearAiJump = useLayoutStore((s) => s.clearAiJump)
  const rootRef = useRef<HTMLDivElement>(null)
  // A deep link clears the search, so its card is on screen (the adjust-state-
  // during-render pattern: the query is gone before this render commits).
  // Jumps are told apart by identity, not by nonce: layoutStore restarts the
  // nonce at 1 after clearAiJump (the back chip), so two jumps in a row with a
  // return between them carry the same nonce.
  const [seenJump, setSeenJump] = useState<AiJump | null>(null)
  if (aiJump && aiJump !== seenJump) {
    setSeenJump(aiJump)
    setQuery('')
  }

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

  // Land each jump once, before paint and before ToolPanel's focus rescue (a
  // child's layout effect runs first), so focus goes from the clicked row
  // straight to the card. A jump made before the tool list loads waits for it.
  const landed = useRef<AiJump | null>(null)
  const landing = useRef<Landing | null>(null)
  useLayoutEffect(() => {
    if (!aiJump || aiJump === landed.current || !active || query !== '' || !rootRef.current) return
    const l = landJump(rootRef.current, aiJump, tools !== null || !!loadError)
    if (!l) return
    landed.current = aiJump
    landing.current = l
  }, [aiJump, active, query, groups, tools, loadError])
  // The cards above the target grow when the feature and download reports
  // land after it ("Not installed" lines, badges), which pushes the target
  // down. While the landing still holds focus, keep it at the top; once the
  // user moves on, leave the scroll alone.
  useLayoutEffect(() => {
    const l = landing.current
    if (!l || !rootRef.current || !active) return
    if (document.activeElement !== l.focus || !l.target.isConnected) { landing.current = null; return }
    alignToTop(rootRef.current, l.target)
    if (!loading) landing.current = null
  }, [features, featuresError, downloads, loading, active])

  // The back chip lasts until the next tab change.
  useEffect(() => {
    if (leftTab !== 'ai' && aiJump) {
      clearAiJump()
      forgetReturn()
    }
  }, [leftTab, aiJump, clearAiJump])

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
    if (!sid) { fail('Open a project first'); return }
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
      fail('Open a project first')
    }
  }, [])

  const fs = featureStatus(features)
  const status = featuresError
    ? `Couldn't check which features are installed — ${featuresError}`
    : fs?.line ?? (loading ? 'Checking features…' : 'Features not checked yet')

  return (
    // data-keymap-ignore: inside the panel a focused checkbox / button keeps
    // Space for itself (keymap/engine.ts) — the generated forms are dense
    // with both, and a keyboard user must be able to toggle and press them.
    <div className="ai-panel" data-keymap-ignore="" ref={rootRef}>
      <div className="ai-panel-head">
        <AiBackChip />
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
          {fs && fs.missing.length > 0 && (
            <Disclosure className="ai-status-details" summary="Details">
              <ul>{fs.missing.map((m) => <li key={m}>{m}</li>)}</ul>
            </Disclosure>
          )}
          <button
            type="button"
            className="ai-refresh"
            disabled={loading}
            title="Check again which optional features this Mac has"
            onClick={() => { void loadCatalog({ refresh: true }) }}
          >
            {loading ? 'Checking…' : 'Refresh'}
          </button>
        </div>
      </div>

      {loadError && (
        <div className="ai-banner" role="alert">
          <b>AI tools unavailable.</b> The editor engine didn’t list its tools: {loadError}
          <button type="button" onClick={() => { void loadCatalog({ refresh: true }) }}>Retry</button>
        </div>
      )}
      {tools === null && !loadError && <p className="ai-empty">Loading tools…</p>}

      {groups.map((g) => {
        const id = aiGroupId(g.group)
        // tabIndex -1: a deep link's "All ‹group› tools" lands focus here.
        return (
          <section key={g.group} className="ai-group" aria-labelledby={id}>
            <h3 id={id} className="section-label" tabIndex={-1}>{g.group}</h3>
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
