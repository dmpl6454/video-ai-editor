import { useEffect } from 'react'
import { useStore, startSessionWatch } from './store'
import { useViewStore } from './lib/viewStore'
import { useBrainFlag } from './lib/brainFlag'
import { useKeymap } from './keymap/engine'
import { Home } from './components/home/Home'
import { EditorShell } from './components/shell/EditorShell'
import { ToastHost } from './components/Toast'
import './components/ui/ui.css'

// Two screens (design handoff 2026-10-02): the Home / projects screen and the
// four-panel editor (components/shell/EditorShell). The store, the keymap,
// the multi-window watch and the Editor Brain flag are app-wide and stay
// mounted across both.
export default function App() {
  const init = useStore((s) => s.init)
  useEffect(() => { void init() }, [init])
  useKeymap()  // customizable CapCut / Premiere / Final Cut keymaps
  // QA-105/109: notice edits made in another window (focus, visibility, a
  // light poll) and whether the engine is still there.
  useEffect(() => startSessionWatch(window), [])
  // Editor Brain (EB1): whether its surfaces show (`brain.enabled`, off by
  // default); read once, the components render nothing brain-shaped until then.
  const loadBrainFlag = useBrainFlag((s) => s.load)
  useEffect(() => { void loadBrainFlag() }, [loadBrainFlag])
  const view = useViewStore((s) => s.view)
  return (
    <>
      {view === 'home' ? <Home /> : <EditorShell />}
      <ToastHost />
    </>
  )
}
