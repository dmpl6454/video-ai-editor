import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { bannerSentence, mediaToolsProblem, type MediaToolsProblem } from '../lib/mediaTools'
import { Icon } from './Icon'

/**
 * The "ffmpeg is not installed" line (QA-108).
 *
 * Without ffmpeg the editor cannot import, preview or export anything, and it
 * used to say so one failed gesture at a time — an import blamed the user's
 * file, a preview reported "corrupt frames". The backend now reports the
 * toolchain on /api/health; this banner reads it once on launch and says what
 * to type, with the command in a box that copies it. "Check again" re-reads
 * health, and so does returning to the window (after running the command in
 * Terminal).
 *
 * In flow at the top of the centre column (wave C review): it was a fixed,
 * viewport-centred overlay with no close, and covered the Inspector/Chat tabs
 * at 1024 px and the prompt bar's Recipes/Run at 1440 px. It now pushes the
 * prompt bar down, and its X hides it for this session — Settings › Video
 * engine keeps the status and "Check again" reachable.
 */

const DISMISS_KEY = 'vai.mediaToolsBannerDismissed'

function readDismissed(): boolean {
  try { return sessionStorage.getItem(DISMISS_KEY) === '1' } catch { return false }
}

function writeDismissed(): void {
  try { sessionStorage.setItem(DISMISS_KEY, '1') } catch { /* private window: hidden until reload */ }
}

export function MediaToolsBanner() {
  const [problem, setProblem] = useState<MediaToolsProblem | null>(null)
  const [checking, setChecking] = useState(false)
  const [copied, setCopied] = useState(false)
  const [dismissed, setDismissed] = useState(readDismissed)
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Reads health and applies the answer. State is only set once the answer
  // arrives (never synchronously inside the mount effect).
  const refresh = useCallback(() => api.health()
    .then((h) => { setProblem(mediaToolsProblem(h)) })
    // Unreachable engine: the connection banner owns that state.
    .catch(() => undefined), [])

  const check = async () => {
    setChecking(true)
    try { await refresh() } finally { setChecking(false) }
  }

  useEffect(() => {
    void refresh()
    const onFocus = () => { void refresh() }
    window.addEventListener('focus', onFocus)
    return () => {
      window.removeEventListener('focus', onFocus)
      if (copiedTimer.current) clearTimeout(copiedTimer.current)
    }
  }, [refresh])

  if (!problem || dismissed) return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(problem.command)
      setCopied(true)
      if (copiedTimer.current) clearTimeout(copiedTimer.current)
      copiedTimer.current = setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  return (
    <MediaToolsBannerView problem={problem} copied={copied} checking={checking}
      onCopy={() => void copy()} onCheck={() => void check()}
      onDismiss={() => { writeDismissed(); setDismissed(true) }} />
  )
}

/** The banner's markup, pure (tested without a backend). Every icon goes
 *  through the app's one icon map (components/Icon). */
export function MediaToolsBannerView({ problem, copied, checking, onCopy, onCheck, onDismiss }: {
  problem: MediaToolsProblem
  copied: boolean
  checking: boolean
  onCopy: () => void
  onCheck: () => void
  onDismiss: () => void
}) {
  return (
    <div className="tools-banner" role="alert" data-media-tools-banner>
      <Icon name="warning" className="tools-banner-icon" />
      <div className="tools-banner-body">
        <p className="tools-banner-title">Video engine not installed</p>
        <p className="tools-banner-text">{bannerSentence(problem)}</p>
        <div className="tools-banner-command">
          <code>{problem.command}</code>
          <button type="button" className="tools-banner-copy" onClick={onCopy}
            aria-label={copied ? 'Command copied' : `Copy the command ${problem.command}`}>
            <Icon name={copied ? 'check' : 'copy'} />
            <span>{copied ? 'Copied' : 'Copy'}</span>
          </button>
        </div>
      </div>
      <button type="button" className="tools-banner-recheck" onClick={onCheck} disabled={checking}>
        <Icon name="refresh" />
        <span>{checking ? 'Checking…' : 'Check again'}</span>
      </button>
      <button type="button" className="tools-banner-close icon-btn" onClick={onDismiss}
        aria-label="Hide this notice" title="Hide until the app restarts — Settings › Video engine still shows it">
        <Icon name="close" />
      </button>
    </div>
  )
}
