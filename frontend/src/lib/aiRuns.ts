// Per-tool run state for the AI panel plus the catalog fetch, as a module-level
// Zustand store (the toast.ts pattern). Out of the component file so that
// react-refresh/only-export-components and HMR stay happy, and so a card's
// running/done state survives the Media↔AI tab switch and a card collapse.
import { create } from 'zustand'
import { api, type FeatureReport, type ToolSchema } from '../api'
import { errorMessage } from '../store'

export type RunState =
  | { status: 'idle' }
  | { status: 'running'; jobId?: string; progress: number; startedAt: number
      cancelling: boolean; reportsProgress: boolean; cancellable: boolean }
  | { status: 'done'; result: unknown; at: number }
  | { status: 'error'; message: string; cancelled: boolean }

type RunningPatch = Partial<Extract<RunState, { status: 'running' }>>

interface AiRunsState {
  runs: Record<string, RunState>
  setRun(tool: string, s: RunState): void
  // Only applies while the tool is running — a late poll must not resurrect a
  // run the user already dismissed.
  patchRun(tool: string, patch: RunningPatch): void
  tools: ToolSchema[] | null            // null until /api/tools answers
  features: FeatureReport | null        // null until /api/features answers
  loadError: string | null              // /api/tools failed — no cards can render
  featuresError: string | null          // /api/features failed — gates unknown, tools still runnable
  loading: boolean
  loadCatalog(opts?: { refresh?: boolean }): Promise<void>
  // Whether the AI tab is the one on screen. LeftPane keeps both tab panels
  // MOUNTED (the Media one hosts a live recorder) and merely hides the
  // inactive one, so "unmount" never fires — anything the panel projects
  // outside itself (the bbox guide rectangles on the preview) has to watch
  // this flag instead.
  panelVisible: boolean
  setPanelVisible(visible: boolean): void
}

export const useAiRuns = create<AiRunsState>((set, get) => ({
  runs: {},
  setRun: (tool, s) => set((st) => ({ runs: { ...st.runs, [tool]: s } })),
  patchRun: (tool, patch) => set((st) => {
    const cur = st.runs[tool]
    if (!cur || cur.status !== 'running') return {}
    return { runs: { ...st.runs, [tool]: { ...cur, ...patch } } }
  }),
  tools: null,
  features: null,
  loadError: null,
  featuresError: null,
  loading: false,
  panelVisible: false,
  setPanelVisible: (visible) => set((st) => (st.panelVisible === visible ? {} : { panelVisible: visible })),

  // Tools and features are fetched INDEPENDENTLY: cards render the moment the
  // schemas land, and the gates apply when the (slower, ~2s cold) feature
  // probe answers. Fetched once per app load — the guard on `tools` means a
  // Media↔AI tab switch never refetches; only Refresh/Retry passes `refresh`.
  loadCatalog: async (opts) => {
    const { tools, loading } = get()
    if (loading) return
    if (tools !== null && !opts?.refresh) return
    set({ loading: true, loadError: null, featuresError: null })
    const toolsDone = api.getTools()
      .then((r) => set({ tools: r.tools }))
      .catch((e: unknown) => set({ loadError: errorMessage(e) }))
    const featuresDone = api.getFeatures(!!opts?.refresh)
      .then((r) => set({ features: r }))
      .catch((e: unknown) => set({ featuresError: errorMessage(e) }))
    await Promise.all([toolsDone, featuresDone])
    set({ loading: false })
  },
}))
