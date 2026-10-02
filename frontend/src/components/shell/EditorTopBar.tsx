// The editor's top bar (design §2 "Editor shell", brief §1): home · brand mark
// · "AI Video Editor" · "/" · project name · save status · spacer · Layout ▾ ·
// keyboard (Shortcuts) · Share · Export. The chat toggle and the activity chip
// (recording Stop, captions Cancel) are this product's own and keep their
// place beside the status: a running job must stay cancellable from anywhere.
//
// Save status is REAL (brief §7 "Autosave status must reflect actual
// completed writes"): every edit is one dispatch the server persists before it
// answers, so "Saved" means the last dispatch has answered, "Saving…" that one
// is in flight, and "Offline" that the engine is unreachable.
import { useRef, useState } from 'react'
import { useStore } from '../../store'
import { useViewStore } from '../../lib/viewStore'
import { LAYOUT_IDS, useWorkspace, type LayoutId } from '../../lib/workspaceLayout'
import { useInspector } from '../inspector/inspectorStore'
import { projectLabel } from '../../lib/projectName'
import { useEngineState } from '../../lib/useEngineState'
import { Dropdown } from '../ui/Dropdown'
import { Icon } from '../Icon'
import { ActivityChip } from '../topbar/ActivityChip'
import { FileMenu } from './FileMenu'
import { ExportButton } from '../dialogs/ExportDialog'
import { openShare, openShortcutsDialog } from '../../lib/dialogOpeners'
import { chordLabel, useKeymapStore } from '../../keymap/engine'
import { openHelp } from '../Help'
import { openSettings } from '../../lib/settingsOpen'

export function EditorTopBar() {
  const showHome = useViewStore((s) => s.showHome)
  const name = useStore((s) => s.sessionName)
  const sid = useStore((s) => s.sessionId)
  const pendingOps = useStore((s) => s.pendingOps)
  const uploading = useStore((s) => s.uploading)
  const online = useEngineState() === 'online'
  const layout = useWorkspace((s) => s.layout)
  const setLayout = useWorkspace((s) => s.setLayout)
  const resetCurrent = useWorkspace((s) => s.resetCurrent)
  const mode = useInspector((s) => s.mode)
  const toggleChat = useInspector((s) => s.toggleChat)
  const [renaming, setRenaming] = useState(false)
  const committed = useRef(false)
  const shortcutChord = useKeymapStore((s) => s.effectiveMap()['openShortcuts']?.[0] ?? '')
  const shown = projectLabel(name, sid)

  const finishRename = async (value: string | null) => {
    if (committed.current) return
    committed.current = true
    setRenaming(false)
    if (value === null) return
    const next = value.replace(/\s+/g, ' ').trim()
    if (!next || next === name) return
    await useStore.getState().renameSession(next)
  }

  const status = !online ? { icon: 'cloudOff' as const, text: 'Offline', title: 'The editor engine is unreachable — edits are refused until it is back' }
    : pendingOps > 0 || uploading ? { icon: 'loading' as const, text: 'Saving…', title: 'An edit is being written to the project' }
    : { icon: 'ok' as const, text: 'Saved', title: 'Every edit is saved to the project as it happens' }

  return (
    <header className="ed-topbar topbar" aria-label="Project">
      <button type="button" className="ui-icon-btn" aria-label="Home" title="Home" onClick={showHome}><Icon name="home" /></button>
      <span className="ed-brand-mark" aria-hidden="true" />
      <h1 className="ed-brand">AI Video Editor</h1>
      <span className="ed-slash" aria-hidden="true">/</span>
      {renaming ? (
        <input className="ed-project-rename" aria-label="Project name" defaultValue={shown} maxLength={120} autoFocus data-keymap-ignore
               onFocus={(e) => e.currentTarget.select()}
               onKeyDown={(e) => {
                 if (e.key === 'Enter') { e.preventDefault(); void finishRename(e.currentTarget.value) }
                 if (e.key === 'Escape') { e.preventDefault(); void finishRename(null) }
               }}
               onBlur={(e) => { void finishRename(e.currentTarget.value) }} />
      ) : (
        <button type="button" className="ed-project" title="Double-click to rename the project" data-tip={`${shown} — double-click to rename`}
                onDoubleClick={() => { committed.current = false; setRenaming(true) }}>
          <span className="ed-project-name">{shown}</span>
        </button>
      )}
      <span className={`ed-save-status is-${status.icon}`} role="status" title={status.title} aria-label={status.text}>
        <Icon name={status.icon} className={status.icon === 'loading' ? 'icon-spin' : undefined} /><span className="ed-status-word">{status.text}</span>
      </span>
      <ActivityChip />
      <span className="ed-grow" />
      <FileMenu onRename={() => { committed.current = false; setRenaming(true) }} />
      <Dropdown<LayoutId>
        label="Layout"
        items={LAYOUT_IDS.map((id) => ({ id, label: id }))}
        value={layout}
        onChange={setLayout}
        width={200}
        className="is-tall ed-layout-btn"
        trigger={<><Icon name="layout" /><span className="ed-layout-word">{layout}</span><Icon name="chevronDown" /></>}
        footer={<button type="button" role="menuitem" className="ui-menu-item is-indented" onClick={resetCurrent}>Reset current layout</button>}
      />
      <button type="button" className="ui-icon-btn" aria-label="Help" title="Help — gestures, shortcuts, fonts and licences" onClick={openHelp}>
        <Icon name="help" />
      </button>
      <button type="button" className="ui-icon-btn" aria-label="Settings" title="Settings (⌘,)" onClick={openSettings}>
        <Icon name="settings" />
      </button>
      <button type="button" className="ui-icon-btn" aria-label="Customize keyboard shortcuts"
              title={`Shortcuts${shortcutChord ? ` (${chordLabel(shortcutChord)})` : ''}`} onClick={openShortcutsDialog}>
        <Icon name="keyboard" />
      </button>
      <button type="button" className={`ui-icon-btn${mode === 'chat' ? ' is-active' : ''}`} aria-label="Chat with the assistant"
              aria-pressed={mode === 'chat'} title="Chat — ask the assistant to edit for you" onClick={toggleChat}>
        <Icon name="chat" />
      </button>
      <button type="button" className="ui-btn-secondary" onClick={openShare} title="Export and share" aria-label="Share"><Icon name="share" /><span className="ed-share-word">Share</span></button>
      <span className="topbar-pinned"><ExportButton /></span>
    </header>
  )
}
