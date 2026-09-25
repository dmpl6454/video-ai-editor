import { useState } from 'react'
import { useStore } from '../store'
import { probeEngineNow } from '../lib/connection'

/**
 * The one sticky "engine disconnected" line (QA-109).
 *
 * When the backend died nothing on screen said so; each gesture popped its own
 * raw "Failed to fetch" toast and was dropped, and a restart recovered
 * silently. Now the store refuses gestures while the engine is unreachable
 * (one notice), this banner stays up until it answers again, and the store
 * says "Reconnected" and reloads the timeline when it does.
 */
export function ConnectionBanner() {
  const engine = useStore((s) => s.engine)
  const [checking, setChecking] = useState(false)
  if (engine !== 'offline') return null
  const retry = async () => {
    setChecking(true)
    try { await probeEngineNow() } finally { setChecking(false) }
  }
  return (
    <div className="engine-banner" role="alert" data-engine-banner>
      <span className="engine-banner-dot" aria-hidden="true" />
      <span className="engine-banner-text">
        <b>Editor engine disconnected.</b> Reconnecting… Edits are paused until it’s back — nothing you
        already saved is lost.
      </span>
      <button type="button" onClick={() => void retry()} disabled={checking}>
        {checking ? 'Checking…' : 'Retry now'}
      </button>
    </div>
  )
}
