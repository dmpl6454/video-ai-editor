import { useEffect, useRef } from 'react'
import { useStore } from '../../store'
import {
  SPEEDS, TARGETS, etaSeconds, formatEta, hasCaptionFootage, speedDownload, useCaptionRun,
} from '../../lib/captionRun'
import { consentText, downloadBadge } from '../../lib/modelDownloads'
import { openCaptionStyle } from '../../lib/captionStyleOpen'
import { Dialog } from '../Dialog'
import { Icon } from '../Icon'
import { DeepLinks } from '../rail/DeepLinkRow'
import './captionsPanel.css'

// The Captions tool panel (docs/design/LEFT_RAIL_SPEC.md §2.5, R2): the top
// bar's "CC Captions" split button and its language / speed menu, laid out
// inline. The run itself — start, poll, ETA, cancel and its "Stopping…" state,
// the model-substitution toast and the download-consent gate — is
// lib/captionRun's ONE module-level run, which the top-bar activity chip reads
// too, so the chip's Cancel and this panel's Cancel are the same cancel.
//
// The language and speed are radio lists that keep every option's hint (the
// hint describes the radio; the label alone is its name). "Fastest" wears its
// download size while its model is missing, and a first run that would
// download a model still asks first in the same dialog. While a run is live
// the choices are disabled: they describe the run on screen, and the running
// job's language cannot change.
//
// The panel's progress card is NOT a live region: the activity chip's throttled
// region announces captions (a second, per-second one would chatter, §2.8).
//
// Last, the Captions & speech (AI) deep links (R5): Translate captions, Detect
// and Name speakers, Import subtitles and Export .srt open their AI cards.

export function CaptionsPanel({ active }: { active: boolean }) {
  const hasFootage = useStore((s) => hasCaptionFootage(s.edl))
  const target = useCaptionRun((s) => s.target)
  const speed = useCaptionRun((s) => s.speed)
  const downloads = useCaptionRun((s) => s.downloads)
  const consent = useCaptionRun((s) => s.consent)
  const busy = useCaptionRun((s) => s.busy)
  const progress = useCaptionRun((s) => s.progress)
  const elapsed = useCaptionRun((s) => s.elapsed)
  const cancelling = useCaptionRun((s) => s.cancelling)
  const mainRef = useRef<HTMLButtonElement | null>(null)

  // QA-065: which caption models are on disk, re-read whenever the panel
  // shows (the old menu re-read it on open) so "Fastest" wears the right badge.
  useEffect(() => {
    if (active) void useCaptionRun.getState().refreshDownloads()
  }, [active])

  const run = useCaptionRun.getState()
  const current = TARGETS.find((t) => t.id === target) ?? TARGETS[0]
  const currentSpeed = SPEEDS.find((s) => s.id === speed) ?? SPEEDS[0]
  const pct = Math.round(progress * 100)
  const eta = etaSeconds(elapsed, progress)

  return (
    <div className="captions-panel">
      <Dialog
        open={!!consent}
        title="Download the caption model?"
        labelId="cc-consent-title"
        triggerRef={mainRef}
        onClose={() => run.dismissConsent()}
        footer={(
          <>
            <button type="button" onClick={() => run.dismissConsent()}>Cancel</button>
            <button type="button" className="primary" onClick={() => run.acceptConsent()}>
              Download and caption
            </button>
          </>
        )}
      >
        {consent && <p className="dialog-text">{consentText(consent)} Captions start when it finishes.</p>}
      </Dialog>

      <button
        ref={mainRef}
        type="button"
        className="panel-btn cc-generate"
        onClick={() => { void run.run() }}
        disabled={!hasFootage || busy}
        title={!hasFootage
          ? 'Add a video to the timeline first — captions transcribe the main (v1) footage'
          : `Auto-captions in ${current.label}, ${currentSpeed.label.toLowerCase()} — `
            + 'transcribes the footage again. '
            + 'A long clip can take a while; progress and a Cancel button appear while it runs.'}
      >
        <Icon name="captions" /> Generate captions
      </button>
      {!hasFootage && (
        <p className="cc-panel-hint">Add a video to the timeline first: captions transcribe the main (v1) footage.</p>
      )}

      {busy && (
        <div className="cc-progress" role="group" aria-label="Captions progress">
          <div className="cc-progress-row">
            <Icon name="loading" className="icon-spin" />
            {cancelling ? (
              <span className="cc-busy-text">
                Stopping…
                <span className="cc-busy-sub">finishing the current chunk · {elapsed}s</span>
              </span>
            ) : (
              <span className="cc-busy-text">
                Transcribing… {pct > 0 ? `${pct}%` : ''}
                <span className="cc-busy-sub">{eta ? formatEta(eta) : `${elapsed}s`}</span>
              </span>
            )}
          </div>
          <span className="cc-bar" aria-hidden="true">
            <span className="cc-bar-fill" style={{ transform: `scaleX(${Math.max(2, pct) / 100})` }} />
          </span>
          {!cancelling && (
            <button type="button" className="panel-btn cc-cancel" onClick={() => { void run.cancel() }}
                    title="Stop transcribing">Cancel</button>
          )}
        </div>
      )}

      <fieldset className="cc-opts" disabled={busy}>
        <legend className="section-label tool-section-label">Language</legend>
        {TARGETS.map((t) => (
          <label key={t.id} className="cc-opt">
            <input type="radio" name="cc-target" value={t.id} checked={t.id === target}
                   onChange={() => run.pickTarget(t.id)}
                   aria-labelledby={`cc-target-${t.id}`} aria-describedby={`cc-target-${t.id}-hint`} />
            <span id={`cc-target-${t.id}`} className="cc-opt-label">{t.label}</span>
            <span id={`cc-target-${t.id}-hint`} className="cc-opt-hint">{t.hint}</span>
          </label>
        ))}
      </fieldset>

      <fieldset className="cc-opts" disabled={busy}>
        <legend className="section-label tool-section-label">Speed</legend>
        {SPEEDS.map((s) => {
          const badge = downloadBadge(speedDownload(downloads, s.id))
          return (
            <label key={s.id} className="cc-opt">
              <input type="radio" name="cc-speed" value={s.id} checked={s.id === speed}
                     onChange={() => run.pickSpeed(s.id)}
                     aria-labelledby={`cc-speed-${s.id}`}
                     aria-describedby={`cc-speed-${s.id}-hint${badge ? ` cc-speed-${s.id}-dl` : ''}`} />
              <span id={`cc-speed-${s.id}`} className="cc-opt-label">{s.label}</span>
              {badge && <span id={`cc-speed-${s.id}-dl`} className="cc-opt-dl">{badge}</span>}
              <span id={`cc-speed-${s.id}-hint`} className="cc-opt-hint">{s.hint}</span>
            </label>
          )
        })}
      </fieldset>

      {/* QA-075: the captions' look and position, one click from Captions. */}
      <button type="button" className="panel-btn cc-style" onClick={() => openCaptionStyle()}>
        Caption style…
      </button>
      <p className="cc-panel-hint">Position, font, colour and box for every caption.</p>
      <DeepLinks from="captions" active={active} />
    </div>
  )
}
