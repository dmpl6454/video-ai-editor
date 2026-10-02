// Auto captions (design §2a "Auto captions"; brief §4 "Auto captions S08"):
// Spoken language, Bilingual captions (reveals the target language), Auto
// highlight keywords, Identify filler words, Delete current captions, and
// Generate — on the app's one captions run (lib/captionRun: a real job with
// progress, cancel, the model-download consent and the speed choice).
//
// Honest where the engine stops: keyword highlighting and filler-word
// marking are shown where the reference shows them, disabled, with the
// reason; "Delete current captions" is a real confirm before the run.
import { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { SPEEDS, SPOKEN_LANGUAGES, TARGETS, etaSeconds, formatEta, hasCaptionFootage, speedDownload, useCaptionRun, type Target } from '../../lib/captionRun'
import { consentText, downloadBadge } from '../../lib/modelDownloads'
import { Toggle } from '../ui/Toggle'
import { Checkbox } from '../ui/Checkbox'
import { Segmented } from '../ui/Segmented'
import { Dialog } from '../Dialog'
import { ConfirmDialog } from '../ConfirmDialog'
import { Icon } from '../Icon'

export function AutoCaptionsForm() {
  const hasFootage = useStore((s) => hasCaptionFootage(s.edl))
  const edl = useStore((s) => s.edl)
  const dispatch = useStore((s) => s.dispatch)
  const target = useCaptionRun((s) => s.target)
  const speed = useCaptionRun((s) => s.speed)
  const language = useCaptionRun((s) => s.language)
  const downloads = useCaptionRun((s) => s.downloads)
  const consent = useCaptionRun((s) => s.consent)
  const busy = useCaptionRun((s) => s.busy)
  const progress = useCaptionRun((s) => s.progress)
  const elapsed = useCaptionRun((s) => s.elapsed)
  const cancelling = useCaptionRun((s) => s.cancelling)
  const [replace, setReplace] = useState(false)
  const [confirmReplace, setConfirmReplace] = useState(false)
  useEffect(() => { void useCaptionRun.getState().refreshDownloads() }, [])

  const run = useCaptionRun.getState()
  const bilingual = target !== 'as-spoken'
  const existing = (edl?.tracks ?? []).filter((t) => t.type === 'captions').reduce((n, t) => n + t.clips.length, 0)
  const pct = Math.round(progress * 100)
  const eta = etaSeconds(elapsed, progress)
  const dl = speedDownload(downloads, speed)
  const badge = downloadBadge(dl)

  const generate = async () => {
    if (replace && existing > 0) { setConfirmReplace(true); return }
    await run.run()
  }
  const reallyReplace = async () => {
    setConfirmReplace(false)
    const caps = (edl?.tracks ?? []).filter((t) => t.type === 'captions').flatMap((t) => t.clips.map((c) => c.id))
    if (caps.length) await dispatch('bulk_delete', { clip_ids: caps })
    await run.run()
  }

  return (
    <div className="ab-form" data-auto-captions>
      <label className="ab-field">
        <span className="ui-label">Spoken language</span>
        <select className="ui-select" value={language} onChange={(e) => run.pickLanguage(e.target.value)} disabled={busy}>
          {SPOKEN_LANGUAGES.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
      </label>
      <div className="ui-row-between">
        <span>Bilingual captions</span>
        <Toggle on={bilingual} label="Bilingual captions" disabled={busy}
                title="Caption in another language than the one spoken"
                onChange={(on) => run.pickTarget(on ? (TARGETS.find((t) => t.id !== 'as-spoken')?.id ?? 'en') : 'as-spoken')} />
      </div>
      {bilingual && (
        <label className="ab-field">
          <span className="ui-label">Target language</span>
          <select className="ui-select" value={target} onChange={(e) => run.pickTarget(e.target.value as Target)} disabled={busy}>
            {TARGETS.filter((t) => t.id !== 'as-spoken').map((t) => <option key={t.id} value={t.id} title={t.hint}>{t.label}</option>)}
          </select>
        </label>
      )}
      <Checkbox checked={false} onChange={() => undefined} disabled title="Keyword highlighting is not part of this build — every word stays editable in the caption's text">Auto highlight keywords</Checkbox>
      <Checkbox checked={false} onChange={() => undefined} disabled title="Marking filler words for review is not part of this build. “Remove filler words” under Media › AI media cuts them, with a preview first.">Identify filler words</Checkbox>
      <Checkbox checked={replace} onChange={setReplace} disabled={busy || existing === 0}
                title={existing === 0 ? 'There are no captions to delete yet' : `Delete the ${existing} caption${existing === 1 ? '' : 's'} already on the timeline before generating`}>
        Delete current captions{existing > 0 ? ` (${existing})` : ''}
      </Checkbox>
      <div className="ab-field">
        <span className="ui-label">Speed</span>
        <Segmented label="Caption speed" value={speed} onChange={(id) => run.pickSpeed(id)} disabled={busy}
                   options={SPEEDS.map((s) => ({ id: s.id, label: s.label, title: s.hint }))} />
        {badge && <span className="ui-faint">{badge}</span>}
      </div>
      {!busy ? (
        <button type="button" className="ui-btn-primary ab-self-start" disabled={!hasFootage} onClick={() => void generate()}
                title={hasFootage ? 'Transcribe the video and lay down a caption track' : 'Add a video with speech to the timeline first'}>
          Generate
        </button>
      ) : (
        <div className="ab-progress" role="status" aria-live="polite">
          <span className="ui-spinner"><Icon name="loading" className="icon-spin" /></span>
          <div className="ab-progress-text">
            <span>{cancelling ? 'Cancelling…' : pct > 0 ? `Transcribing · ${pct}%${eta != null ? ` · ${formatEta(eta)} left` : ''}` : 'Starting the transcription…'}</span>
            <div className="ab-bar"><div className="ab-bar-fill" style={{ width: `${Math.max(3, pct)}%` }} /></div>
          </div>
          <button type="button" className="ui-small-btn" disabled={cancelling} onClick={() => void run.cancel()}>Cancel</button>
        </div>
      )}
      <Dialog open={!!consent} title="Download a caption model?" labelId="ab-consent-title" onClose={() => run.dismissConsent()}
              footer={<><span className="spacer" /><button type="button" onClick={() => run.dismissConsent()}>Not now</button>
                <button type="button" className="primary" onClick={() => run.acceptConsent()}>Download and caption</button></>}>
        {consent && <p className="dialog-text">{consentText(consent)} Captions start when it finishes.</p>}
      </Dialog>
      {confirmReplace && (
        <ConfirmDialog title={`Delete ${existing} caption${existing === 1 ? '' : 's'} and regenerate?`}
                       body="Hand edits to those captions are lost. Undo brings them back." confirmLabel="Delete and generate" danger
                       onConfirm={() => void reallyReplace()} onCancel={() => setConfirmReplace(false)} />
      )}
    </div>
  )
}
