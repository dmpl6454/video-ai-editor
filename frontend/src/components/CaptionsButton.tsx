// TopBar "CC Captions" — one-click auto-captions.
//
// Dispatches `auto_caption` (agent/dispatch.py): re-transcribes the v1 source
// with Whisper large-v3 (Metal-accelerated, anti-hallucination flags), builds
// broadcast-style cues, and lays down the captions track. All args are
// optional; we send none so the backend defaults apply (style ig_chunky,
// position bottom, auto language detection — Hinglish-friendly).
//
// The call can genuinely run MINUTES (a full re-transcription), so the button
// carries its own busy state for the duration of the dispatch promise. The
// caption cues land as TextClips that TextLayer.tsx previews client-side —
// no preview re-render is needed for them to appear.
//
// Failure modes: no v1 clip → 400 ValueError; missing whisper model/binary →
// 422 RuntimeError. Both carry a user-readable message which store.dispatch
// already toasts verbatim (it returns null in that case), so this component
// only adds the success toast with the cue count from the tool result.

import { useState } from 'react'
import { useStore } from '../store'
import { toast } from '../toast'
import { isMediaClip } from '../types'
import './CaptionsButton.css'

export function CaptionsButton() {
  const dispatch = useStore((s) => s.dispatch)
  const edl = useStore((s) => s.edl)
  const [busy, setBusy] = useState(false)

  // auto_caption transcribes the first media clip on v1 — mirror that guard
  // here so the button is disabled (with an explaining tooltip) instead of
  // dispatching a guaranteed 400.
  const hasFootage = !!edl?.tracks
    .find((t) => t.id === 'v1')
    ?.clips.some(isMediaClip)

  const run = async () => {
    if (busy || !hasFootage) return
    setBusy(true)
    try {
      const res = await dispatch('auto_caption', {})
      if (res) {
        const r = res.result as { cues?: number; language?: string } | null
        toast.success(
          typeof r?.cues === 'number'
            ? `Captions added — ${r.cues} cues${r.language ? ` (${r.language})` : ''}`
            : 'Captions added',
        )
      }
      // res === null → the failure toast already fired inside store.dispatch.
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      onClick={() => { void run() }}
      disabled={busy || !hasFootage}
      title={!hasFootage
        ? 'Add a video to the timeline first — captions transcribe the main (v1) footage'
        : 'Auto-captions: re-transcribes the footage with Whisper large-v3 and lays down a caption track (can take a few minutes)'}
      style={{ fontSize: 11 }}
    >
      {busy
        ? <><span className="cc-spinner" aria-hidden="true" /> Transcribing…</>
        : <><b>CC</b> Captions</>}
    </button>
  )
}
