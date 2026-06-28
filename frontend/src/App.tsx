import { useEffect } from 'react'
import { useStore } from './store'
import { TopBar } from './components/TopBar'
import { MediaBin } from './components/MediaBin'
import { Preview } from './components/Preview'
import { Timeline } from './components/Timeline'
import { Properties } from './components/Properties'
import { OpsLog } from './components/OpsLog'
import { ChatOverlay } from './components/ChatOverlay'
import { Help } from './components/Help'
import { FileDropOverlay } from './components/FileDropOverlay'
import { useShortcuts } from './shortcuts'

export default function App() {
  const init = useStore((s) => s.init)
  useEffect(() => { void init() }, [init])
  useShortcuts()

  return (
    <div className="app">
      <TopBar />
      <aside className="sidebar left">
        <MediaBin />
      </aside>
      <main className="center">
        <div className="preview-pane">
          <Preview />
        </div>
        <div className="timeline-pane">
          <Timeline />
        </div>
      </main>
      <aside className="sidebar right">
        <Properties />
        <OpsLog />
      </aside>
      <ChatOverlay />
      <Help />
      <FileDropOverlay />
    </div>
  )
}
