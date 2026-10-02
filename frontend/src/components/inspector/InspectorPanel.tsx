// The inspector column (design §2c): Details when nothing is selected, Source
// for a previewed asset, the Clip inspector for a selection — and the
// product's Chat, toggled from the top bar into the same column.
import { useInspector } from './inspectorStore'
import { DetailsView } from './DetailsView'
import { SourceView } from './SourceView'
import { ClipInspector } from './ClipInspector'
import { ChatOverlay } from '../ChatOverlay'
import './inspector.css'

export function InspectorPanel() {
  const mode = useInspector((s) => s.mode)
  const closeChat = useInspector((s) => s.closeChat)
  switch (mode) {
    case 'chat':
      return (
        <div className="in in-chat" id="right-panel-chat">
          <div className="ed-panel-head"><span>Chat</span><span className="ed-panel-mode">Assistant</span></div>
          <div className="in-chat-body"><ChatOverlay onClose={closeChat} /></div>
        </div>
      )
    case 'source': return <SourceView />
    case 'clip': return <ClipInspector />
    default: return <DetailsView />
  }
}
