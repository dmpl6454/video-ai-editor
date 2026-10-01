import { useRef, type KeyboardEvent } from 'react'
import { useLayoutStore } from '../lib/layoutStore'
import type { RightTab } from '../lib/rightTab'
import { Properties } from './Properties'
import { OpsLog } from './OpsLog'
import { VersionsStrip } from './brain/VersionsStrip'
import { ChatOverlay } from './ChatOverlay'
import { Icon } from './Icon'
import { useFocusRescue } from './rail/focusRescue'
import { useRailChord } from './rail/useRailChord'
import './rail/rail.css'

// The right panel (docs/design/LEFT_RAIL_SPEC.md §2.9), extracted from App.tsx:
// the Inspector (Properties + History) and the docked Chat as tabs, with the
// collapse toggle at the end of the header.
//
// Collapsed, it is a 36 px rail: "Show the Inspector and Chat panel" (the name
// the expanded panel's toggle has always paired with, critique M2) restores the
// last tab; "Show the Inspector" and "Show the Chat" jump straight to one (Chat
// also focuses its message box). Both tab panels stay MOUNTED while hidden or
// collapsed: a chat turn keeps streaming and the conversation is still there
// when the tab is reopened.
//
// Focus rescue (§5.3): if focus was inside a part that just hid, it moves to
// the selected tab (switch) or to the expand button (collapse).

const TAB_ID: Record<RightTab, string> = { inspect: 'right-tab-inspect', chat: 'right-tab-chat' }

export function RightPanel() {
  const rightOpen = useLayoutStore((s) => s.rightOpen)
  const rightTab = useLayoutStore((s) => s.rightTab)
  const setRightOpen = useLayoutStore((s) => s.setRightOpen)
  const setRightTab = useLayoutStore((s) => s.setRightTab)
  const showRight = useLayoutStore((s) => s.showRight)
  const expandRef = useRef<HTMLButtonElement>(null)
  const chatRef = useRef<HTMLDivElement>(null)
  const inspectKey = useRailChord('showInspector')
  const chatKey = useRailChord('showChat')

  const ownerOf = () => {
    const s = useLayoutStore.getState()
    return s.rightOpen ? document.getElementById(TAB_ID[s.rightTab]) : expandRef.current
  }
  const rescue = useFocusRescue(ownerOf, [rightOpen, rightTab])

  const onTabKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Home' && e.key !== 'End') return
    e.preventDefault()
    const next: RightTab = e.key === 'Home' ? 'inspect' : e.key === 'End' ? 'chat' : rightTab === 'chat' ? 'inspect' : 'chat'
    setRightTab(next)
    document.getElementById(TAB_ID[next])?.focus()
  }

  const collapse = () => {
    setRightOpen(false)
    expandRef.current?.focus()
  }
  const expand = (tab?: RightTab) => {
    if (tab) showRight(tab)
    else setRightOpen(true)
    const t = tab ?? useLayoutStore.getState().rightTab
    // After the commit that un-hides the header / chat.
    requestAnimationFrame(() => {
      if (t === 'chat' && tab) chatRef.current?.querySelector<HTMLElement>('textarea, input')?.focus()
      else document.getElementById(TAB_ID[t])?.focus()
    })
  }

  return (
    <aside
      id="right-panel"
      className={`sidebar right right-panel${rightOpen ? '' : ' collapsed'}`}
      aria-label="Inspector and Chat"
      onFocus={rescue.onFocus}
      onBlur={rescue.onBlur}
    >
      <div className="right-head" hidden={!rightOpen}>
        {/* ←/→ belong to the tabs (keymap rule 4: a focused button keeps its
            arrows) and Space/Enter to the focused tab (rule 5), like the
            rail's; ⌘Z, J/K/L and N still work here (review RD2). */}
        <div className="right-tabs" role="tablist" aria-label="Right panel" onKeyDown={onTabKey}>
          <button type="button" role="tab" id={TAB_ID.inspect} aria-controls="right-panel-inspect"
                  aria-selected={rightTab === 'inspect'} tabIndex={rightTab === 'inspect' ? 0 : -1}
                  aria-keyshortcuts={inspectKey.aria || undefined}
                  onClick={() => setRightTab('inspect')}>
            <Icon name="inspector" />Inspector
          </button>
          <button type="button" role="tab" id={TAB_ID.chat} aria-controls="right-panel-chat"
                  aria-selected={rightTab === 'chat'} tabIndex={rightTab === 'chat' ? 0 : -1}
                  aria-keyshortcuts={chatKey.aria || undefined}
                  onClick={() => setRightTab('chat')}>
            <Icon name="chat" />Chat
          </button>
        </div>
        <button
          type="button"
          className="right-head-btn"
          aria-label="Hide the Inspector and Chat panel"
          aria-expanded={rightOpen}
          aria-controls="right-panel"
          data-tip="Hide the Inspector and Chat"
          onClick={collapse}
        >
          <Icon name="chevronRight" />
        </button>
      </div>
      <div role="tabpanel" id="right-panel-inspect" aria-labelledby={TAB_ID.inspect} className="right-body"
           hidden={!rightOpen || rightTab !== 'inspect'}>
        <Properties />
        {/* Editor Brain (EB1): named versions above History; renders nothing
            unless brain.enabled is on AND the project has versions. */}
        <VersionsStrip />
        <OpsLog />
      </div>
      <div ref={chatRef} role="tabpanel" id="right-panel-chat" aria-labelledby={TAB_ID.chat} className="right-chat"
           hidden={!rightOpen || rightTab !== 'chat'}>
        <ChatOverlay onClose={() => setRightTab('inspect')} />
      </div>
      <div className="right-rail" hidden={rightOpen}>
        <button ref={expandRef} type="button" aria-label="Show the Inspector and Chat panel"
                aria-expanded={rightOpen} aria-controls="right-panel"
                data-tip="Show the Inspector and Chat" onClick={() => expand()}>
          <Icon name="chevronLeft" />
        </button>
        <span className="right-rail-sep" aria-hidden="true" />
        <button type="button" aria-label="Show the Inspector" aria-keyshortcuts={inspectKey.aria || undefined}
                data-tip="Inspector" data-kbd={inspectKey.label || undefined} onClick={() => expand('inspect')}>
          <Icon name="inspector" />
        </button>
        <button type="button" aria-label="Show the Chat" aria-keyshortcuts={chatKey.aria || undefined}
                data-tip="Chat" data-kbd={chatKey.label || undefined} onClick={() => expand('chat')}>
          <Icon name="chat" />
        </button>
      </div>
    </aside>
  )
}
