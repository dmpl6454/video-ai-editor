// Project Details (design §2c "Details"; brief §7 "Details and settings"):
// Name, Path, Aspect ratio, Resolution, Color space, Frame rate, Imported
// media, Proxy, Arrange layers, and Modify → Project settings. Every value is
// the project's own: the canvas from the EDL, the path from the session, the
// policies from the project's own settings (lib/projectSettings). The yellow
// "Arrange layers freely" onboarding tooltip shows once, persisted.
//
// Below the rows, this product's History (undo horizon, QA-046) keeps its
// place as a disclosure, so the edit record stays one click away.
import { useState } from 'react'
import { useStore } from '../../store'
import { formatFps } from '../../lib/frameStep'
import { ratioOf, RATIOS } from '../../lib/playerControls'
import { projectLabel } from '../../lib/projectName'
import { ONBOARD_KEY, useProjectSettings } from '../../lib/projectSettings'
import { OpsLog } from '../OpsLog'
import { VersionsStrip } from '../brain/VersionsStrip'
import { Disclosure } from '../Disclosure'
import { Icon } from '../Icon'
import { openProjectSettings } from '../../lib/dialogOpeners'

export function DetailsView() {
  const edl = useStore((s) => s.edl)
  const name = useStore((s) => s.sessionName)
  const sid = useStore((s) => s.sessionId)
  const previewEngine = useStore((s) => s.previewEngine)
  const settings = useProjectSettings((s) => s.forSession(sid))
  const [onboard, setOnboard] = useState(() => {
    try { return localStorage.getItem(ONBOARD_KEY) !== '1' } catch { return false }
  })
  const dismiss = () => { setOnboard(false); try { localStorage.setItem(ONBOARD_KEY, '1') } catch { /* private mode */ } }
  const c = edl?.canvas
  const ratio = c ? (RATIOS.find((r) => r.id === ratioOf(c))?.label ?? 'Custom') : '—'
  return (
    <div className="in in-details">
      <div className="ed-panel-head">Details</div>
      <div className="in-body">
        <div className="in-rows">
          <Row label="Name">{projectLabel(name, sid)}</Row>
          <Row label="Path" title={sid ? `Project folder: workdir/${sid}` : ''}><span className="in-ellipsis">{sid ? `workdir/${sid}` : '—'}</span></Row>
          <Row label="Aspect ratio">{ratio}</Row>
          <Row label="Resolution">{c ? `${c.w} × ${c.h}` : '—'}</Row>
          <Row label="Color space">Rec. 709 SDR</Row>
          <Row label="Frame rate">{c ? `${formatFps(c.fps)} fps` : '—'}</Row>
          <Row label="Imported media">{settings.importPolicy === 'copy' ? 'Copy media to project' : 'Stay in original location'}</Row>
          <Row label="Proxy">{previewEngine === 'client' ? 'On' : settings.proxy ? 'On (preparing)' : 'Off'}</Row>
          <div className="in-row in-row-arrange">
            <span className="in-row-label">Arrange layers</span>
            <span>{settings.arrangeLayers ? 'On' : 'Off'}</span>
            {onboard && (
              <div className="in-onboard" role="note" aria-label="Arrange layers freely">
                <span className="in-onboard-caret" aria-hidden="true" />
                <Icon name="stack" size={18} />
                <div className="in-onboard-text">
                  <span className="in-onboard-title">Arrange layers freely</span>
                  <span className="in-onboard-body">Turn this on to place any clip above or below the main track. It stays on for this project.</span>
                </div>
                <button type="button" className="in-onboard-x" aria-label="Dismiss" onClick={dismiss}><Icon name="close" /></button>
              </div>
            )}
          </div>
        </div>
        <Disclosure summary="History" className="in-history" defaultOpen={false}>
          <VersionsStrip />
          <OpsLog />
        </Disclosure>
      </div>
      <div className="in-foot">
        <button type="button" className="ui-btn-secondary" onClick={openProjectSettings}>Modify</button>
      </div>
    </div>
  )
}

function Row({ label, children, title }: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <div className="in-row" title={title}>
      <span className="in-row-label">{label}</span>
      <span className="in-row-value">{children}</span>
    </div>
  )
}
