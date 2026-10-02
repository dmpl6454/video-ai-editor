// Export and share (design §6; brief §8 "Export and share S57"): the preview,
// Name, the Space row gated on sign-in, the read-only render summary, and
// Export and share — disabled until a destination exists. This build has no
// account or cloud destination, so the gate stays and says so; Export (the
// local file) is one click away.
import { useEffect, useId, useState } from 'react'
import { useStore } from '../../store'
import { estimateBytes, estimateVideoKbps, exportDimensions, type CanvasLike } from '../../lib/exportOptions'
import { humanBytes } from '../../lib/promptEvents'
import { formatTimecode } from '../../lib/timecode'
import { formatFps } from '../../lib/frameStep'
import { Dialog } from '../Dialog'
import { PosterFrame } from './PosterFrame'
import { Icon } from '../Icon'
import './dialogs.css'

import { registerOpener } from '../../lib/dialogOpeners'

export function ShareDialog() {
  const [open, setOpen] = useState(false)
  useEffect(() => { registerOpener('share', () => setOpen(true)); return () => registerOpener('share', null) }, [])
  if (!open) return null
  return <ShareForm onClose={() => setOpen(false)} />
}

function ShareForm({ onClose }: { onClose: () => void }) {
  const id = useId()
  const edl = useStore((s) => s.edl)
  const sessionName = useStore((s) => s.sessionName)
  const [name, setName] = useState(sessionName)
  const canvas = (edl?.canvas ?? { w: 1280, h: 720, fps: 30 }) as CanvasLike
  const [w, h] = exportDimensions(canvas.w, canvas.h, 720)
  const kbps = estimateVideoKbps(canvas, 720, 18, null)
  const dur = edl?.duration ?? 0
  return (
    <Dialog open title="Export and share" labelId={`${id}-title`} onClose={onClose} className="dlg dlg-share"
            footer={<><span className="spacer" /><button type="button" className="ui-btn-secondary dlg-btn" onClick={onClose}>Cancel</button>
              <button type="button" className="ui-btn-primary dlg-btn is-dim" disabled title="Sign in and choose a Space first — sharing destinations are not part of this build">Export and share</button></>}>
      <div className="dlg-share-grid">
        <PosterFrame />
        <div className="dlg-form">
          <label className="dlg-row dlg-row-90"><span className="dlg-label">Name</span><input className="ui-input on-modal" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <div className="dlg-row dlg-row-90"><span className="dlg-label">Space</span>
            <div className="dlg-space"><Icon name="lock" /><span>Sign in to choose a Space</span>
              <button type="button" className="dlg-space-btn" disabled title="Accounts are not part of this build">Sign in</button></div></div>
          <div className="ui-hairline" />
          <div className="dlg-summary">
            <span className="dlg-label">Resolution</span><span>720P · {w} × {h}</span>
            <span className="dlg-label">Format</span><span>mov · H.264</span>
            <span className="dlg-label">Frame rate</span><span>{formatFps(canvas.fps)} FPS</span>
            <span className="dlg-label">Duration</span><span style={{ fontVariantNumeric: 'tabular-nums' }}>{formatTimecode(dur, canvas.fps)}</span>
            <span className="dlg-label">Size</span><span style={{ fontVariantNumeric: 'tabular-nums' }}>≈ {humanBytes(estimateBytes(kbps, dur))}</span>
          </div>
        </div>
      </div>
    </Dialog>
  )
}
